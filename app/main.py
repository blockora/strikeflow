import threading
import time
from datetime import datetime

import pytz

from app.config import config

from app.data.angel import angel_source

from app.data.cache import cache

from app.data.jugaad import jugaad_source

from app.data.validator import validator

from app.history.cycles import CycleHistory

from app.history.outcomes import OutcomeEngine

from app.history.signals import SignalHistory

from app.market.underlying import UnderlyingEngine

from app.market.regime import MarketRegimeEngine

from app.market.option_chain import OptionChainEngine

from app.market.greeks import black_scholes_greeks

from app.output.terminal import print_cycle_report

from app.scheduler import market_session_state, sleep_until_next_cycle

from app.selector.candidates import CandidateGenerator

from app.selector.confidence import ConfidenceEngine

from app.selector.ranking import RankingEngine

from app.selector.scoring import ScoringEngine

from app.notifications.telegram import send_recommendation

IST = pytz.timezone("Asia/Kolkata")


class OptionStrikeSelector:
    def __init__(self):
        self.underlying_engine = UnderlyingEngine(config.UNDERLYING)
        self.chain_engine = OptionChainEngine(config.UNDERLYING)
        self.regime_engine = MarketRegimeEngine()
        self.candidate_generator = CandidateGenerator(config.UNDERLYING)
        self.scoring_engine = ScoringEngine()
        self.ranking_engine = RankingEngine()
        self.confidence_engine = ConfidenceEngine()
        self.outcome_engine = OutcomeEngine()
        self.running = False

    def start_data_feeds(self):
        try:
            angel_source.login()
        except Exception as exc:
            print(f"[WARN] Angel login failed: {exc}")

    def fetch_jugaad_loop(self):
        while self.running:
            try:
                chain = jugaad_source.option_chain_index(config.UNDERLYING)
                if bool(chain and chain.get("status") == "success" and chain.get("raw", False)):
                    cache.update_option_chain(config.UNDERLYING, chain)
            except Exception:
                pass
            time.sleep(15)

    def fetch_underlying_loop(self):
        while self.running:
            try:
                if angel_source.connected:
                    data = angel_source.ltp_data(
                        config.EXCHANGE,
                        f"{config.UNDERLYING}-EQ",
                        config.UNDERLYING_TOKEN,
                    )
                    if data and data.get("data") and len(data["data"]) > 0:
                        quote = data["data"][0]
                        ltp = _safe_float(quote.get("ltp"))
                        bid = _safe_float(quote.get("bp") or quote.get("bidprice"))
                        ask = _safe_float(quote.get("ap") or quote.get("askprice"))
                        volume = _safe_float(quote.get("volume") or quote.get("total_traded_voi"))
                        oi = _safe_float(quote.get("openinterest") or quote.get("open_interest"))
                        timestamp = time.time()
                        if ltp is not None:
                            cache.update_underlying(config.UNDERLYING, {
                                "source": "ANGEL",
                                "ltp": ltp,
                                "bid": bid,
                                "ask": ask,
                                "volume": volume,
                                "oi": oi,
                                "timestamp": timestamp,
                            })
            except Exception as e:
                import sys
                print(f"[WARN] Underlying data fetch error: {e}", file=sys.stderr)
            time.sleep(5)

    def compute_data_quality(self):
        checks = []
        underlying = cache.get_underlying(config.UNDERLYING)
        chain = cache.get_option_chain(config.UNDERLYING)
        ok, _ = validator.validate_quote(underlying or {})
        checks.append(ok)
        checks.append(bool(chain and chain.get("raw")))
        checks.append(angel_source.connected)
        if not checks:
            return 0
        return round((sum(checks) / len(checks)) * 100, 2)

    def attach_greeks(self, candidates):
        expiry = self.chain_engine.current_expiry()
        if not expiry:
            return candidates
        try:
            expiry_dt = datetime.strptime(expiry, "%d-%b-%Y")
        except Exception:
            return candidates
        underlying = cache.get_underlying(config.UNDERLYING) or {}
        spot = underlying.get("ltp")
        if spot is None:
            return candidates
        for c in candidates:
            greeks = black_scholes_greeks(
                spot=spot,
                strike=c.get("strike"),
                expiry_dt=IST.localize(expiry_dt),
            option_type=c.get("option_type"),
            iv=c.get("iv"),
            )
            if greeks:
                c.update(greeks)
        return candidates

    def run_cycle(self):
        self.underlying_engine.update_from_cache()
        underlying_snapshot = self.underlying_engine.snapshot()
        regime = self.regime_engine.classify(underlying_snapshot)
        data_quality = self.compute_data_quality()
        spot = underlying_snapshot.get("spot")
        candidates = self.candidate_generator.generate(spot, regime)
        candidates = self.attach_greeks(candidates)
        candidates = self.ranking_engine.enrich_candidates(candidates, underlying_snapshot)
        scored = self.scoring_engine.score_candidates(candidates, underlying_snapshot, regime)
        best, top3 = self.ranking_engine.select_best(scored)
        previous_cycle = CycleHistory.latest_cycle()
        previous_symbol = previous_cycle["best_symbol"] if previous_cycle else None
        previous_score = previous_cycle["best_score"] if previous_cycle else None
        if best:
            signal_id = SignalHistory.create_signal_id(config.UNDERLYING, best.get("strike"), best.get("option_type"))
            signal = SignalHistory.save_signal({
                "signal_id": signal_id,
                "underlying": config.UNDERLYING,
                "expiry": self.chain_engine.current_expiry(),
                "strike": best.get("strike"),
                "option_type": best.get("option_type"),
                "entry": best.get("entry_high"),
                "stop_loss": best.get("stop_loss"),
                "target": best.get("target"),
            })
        else:
            signal_id = None
            signal = SignalHistory.save_signal({
                "signal_id": signal_id,
                "underlying": config.UNDERLYING,
                "expiry": self.chain_engine.current_expiry(),
                "strike": None,
                "option_type": None,
                "entry": None,
                "stop_loss": None,
                "target": None,
            })
        if best:
            confidence_value, confidence_label = self.confidence_engine.calculate(
                best, data_quality, regime, previous_cycle
            )
        else:
            confidence_value = 0.0
            confidence_label = "NO CLEAR SIGNAL"
            signal_state = "NO SIGNAL"
        score_change = None
        CycleHistory.save_candidate_scores(previous_cycle["cycle_id"] if previous_cycle else None, scored)
        report = {
            "cycle_id": previous_cycle["cycle_id"] if previous_cycle else None,
            "time": datetime.now(IST).strftime("%H:%M:%S"),
            "underlying": config.UNDERLYING,
            "spot": spot,
            "expiry": self.chain_engine.current_expiry(),
            "regime": regime,
            "data_quality": data_quality,
            "candidates": scored[:10],
        }
