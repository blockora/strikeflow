"""BLOCKORA Option Strike Selector — live decision-support entrypoint.

Implements the §31 cycle exactly:
1 session check, 2 connectivity, 3 live data update, 4 timestamp validation,
5 underlying update, 6 option chain update, 7 candidate generation,
8 ten valid strikes, 9 metrics, 10 score, 11 previous-cycle comparison,
12 entry, 13 SL, 14 target, 15 R:R, 16 confidence, 17 gates,
18 BEST / NO CLEAR STRIKE, 19 SQLite persistence, 20 terminal output,
21 wait for next minute boundary (scheduler, no blind sleep).

Decision-support only: no order placement, modification or cancellation
exists anywhere in this codebase (§54, §88).
"""

import logging
import os
import signal
import sys
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
from app.history.database import db
from app.history.outcomes import OutcomeEngine, detect_state
from app.history.signals import SignalHistory
from app.market.greeks import black_scholes_greeks
from app.market.option_chain import OptionChainEngine
from app.market.regime import MarketRegimeEngine
from app.market.underlying import UnderlyingEngine
from app.output.terminal import print_cycle_report, print_status_line
from app.scheduler import market_session_state, next_minute_boundary, sleep_until_next_cycle
from app.selector.candidates import CandidateGenerator
from app.selector.confidence import ConfidenceEngine
from app.selector.ranking import RankingEngine
from app.selector.scoring import ScoringEngine

IST = pytz.timezone("Asia/Kolkata")

# Terminal + rotating file logging without printing secrets (§52, §59).
os.makedirs(config.LOG_DIR, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stderr),
        logging.FileHandler(os.path.join(config.LOG_DIR, "app.log"), encoding="utf-8"),
    ],
)
logger = logging.getLogger("blockora")


def _safe_float(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


class OptionStrikeSelector:
    """Live 1-minute decision-support engine."""

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
        self.cycle_count = 0
        self.last_data_ts = None
        self.jugaad_ok = False
        self.angel_ltp_ok = False
        self._open_signals = {}  # signal_id -> tracked premium state (§64)

    # ------------------------------------------------------------------ data

    def start_data_feeds(self):
        """Authenticate Angel, resolve the underlying token, start streaming."""
        try:
            angel_source.login()
        except Exception as exc:
            logger.warning("Angel login failed: %s (Jugaad-only mode)", exc)
            db.log_event("ANGEL_LOGIN_FAILED", str(exc)[:200])
            return

        try:
            angel_source.underlying_token_row()
            row = angel_source.underlying_token_row()
            if row and row.get("token"):
                self.underlying_token = str(row["token"])
            else:
                self.underlying_token = config.UNDERLYING_TOKEN
                logger.info("Using configured underlying token %s", self.underlying_token)
            angel_source.set_underlying_token(self.underlying_token)
        except Exception as exc:
            logger.warning("Scrip master load failed: %s", exc)
            self.underlying_token = config.UNDERLYING_TOKEN
            angel_source.set_underlying_token(self.underlying_token)

        try:
            angel_source._reconnect_tick_hook = self._on_angel_tick
            angel_source.start_websocket_thread(
                tokens_nse=[self.underlying_token],
                on_tick=self._on_angel_tick,
            )
            logger.info("Angel WebSocket streaming started for token %s", self.underlying_token)
        except Exception as exc:
            logger.error("Angel WebSocket start failed: %s", exc)
            db.log_event("ANGEL_WS_START_FAILED", str(exc)[:200])

    def _on_angel_tick(self, payload):
        """Update underlying cache from live underlying ticks (§4, §72).

        The tick arrives already normalized (rupees) from angel._on_data via
        normalize_angel_quote; bid/ask are aggregate top-of-book quantities
        from QUOTE mode and stay None unless the SDK provides them.
        """
        if str(payload.get("token")) == str(self.underlying_token):
            ltp = _safe_float(payload.get("ltp"))
            if ltp is not None:
                cache.update_underlying(config.UNDERLYING, {
                    "source": "ANGEL",
                    "ltp": ltp,
                    "bid": _safe_float(payload.get("bid")),
                    "ask": _safe_float(payload.get("ask")),
                    "volume": _safe_float(payload.get("volume")),
                    "oi": _safe_float(payload.get("oi")),
                    "exchange_timestamp": payload.get("exchange_timestamp"),
                    "local_timestamp": payload.get("local_timestamp"),
                })
                self.last_data_ts = time.time()
                self.angel_ltp_ok = True

    def fetch_underlying_rest_loop(self):
        """REST fallback poll for the underlying quote (§71: low frequency)."""
        while self.running:
            try:
                if angel_source.connected and getattr(self, "underlying_token", None):
                    data = angel_source.ltp_data(
                        config.EXCHANGE,
                        f"{config.UNDERLYING}-EQ",
                        self.underlying_token,
                    )
                    quote = (data or {}).get("data", {})
                    if isinstance(quote, dict):
                        ltp = _safe_float(quote.get("ltp"))
                        if ltp is not None:
                            cache.update_underlying(config.UNDERLYING, {
                                "source": "ANGEL",
                                "ltp": ltp,
                                "bid": _safe_float(quote.get("bp") or quote.get("bidprice")),
                                "ask": _safe_float(quote.get("ap") or quote.get("askprice")),
                                "volume": _safe_float(quote.get("volume") or quote.get("tradeVolume")),
                                "oi": _safe_float(quote.get("oi") or quote.get("openinterest")),
                                "local_timestamp": time.time(),
                            })
                            self.last_data_ts = time.time()
            except Exception as exc:
                logger.debug("Underlying REST poll error: %s", exc)
            time.sleep(config.ANGEL_UNDERLYING_POLL_SECONDS)

    def fetch_jugaad_loop(self):
        """Continuous option-chain refresh into the cache (§5, §72)."""
        while self.running:
            try:
                chain = jugaad_source.option_chain_index(config.UNDERLYING)
                if chain.get("status") == "success" and chain.get("raw"):
                    cache.update_option_chain(config.UNDERLYING, chain)
                    self.jugaad_ok = True
                else:
                    self.jugaad_ok = False
                    logger.info("Jugaad chain unavailable: %s (%s)", chain.get("status"), chain.get("error"))
            except Exception:
                logger.exception("Jugaad fetch loop error")
                self.jugaad_ok = False
            time.sleep(config.JUGAAD_REFRESH_SECONDS)

    # ------------------------------------------------------------------ cycle

    def compute_data_quality(self):
        """Per-cycle data-quality score (§45, §46) with failure detail."""
        checks = {}
        underlying = cache.get_underlying(config.UNDERLYING)
        ok, reason = validator.validate_quote(underlying)
        checks["UNDERLYING_FRESH"] = ok
        checks["ANGEL_CONNECTED"] = angel_source.connected
        chain_age = cache.chain_age(config.UNDERLYING)
        checks["CHAIN_FRESH"] = chain_age is not None and chain_age <= config.MAX_CHAIN_STALE_SECONDS
        conflict = self.chain_engine.spot_conflict(
            (underlying or {}).get("ltp")
        )
        checks["SOURCE_AGREEMENT"] = conflict is not False
        self.last_quality_reasons = [k for k, v in checks.items() if not v]
        self.source_agreement = conflict
        score = round(sum(1 for v in checks.values() if v) / len(checks) * 100, 1)
        return score

    def attach_greeks(self, candidates, spot):
        """Attach calculated Greeks when valid inputs exist (§13, §22)."""
        if not config.ALLOW_CALCULATED_GREEKS:
            return candidates
        expiry = self.chain_engine.current_expiry()
        if not expiry or spot is None:
            return candidates
        try:
            expiry_dt = datetime.strptime(expiry, "%d-%b-%Y")
        except (TypeError, ValueError):
            logger.warning("Unparseable expiry %r; Greeks skipped", expiry)
            return candidates
        for c in candidates:
            if c.get("iv") is None:
                continue  # no IV: Greeks stay missing (§7)
            greeks = black_scholes_greeks(
                spot=spot,
                strike=c.get("strike"),
                expiry_dt=expiry_dt,
                option_type=c.get("option_type"),
                iv=_safe_float(c.get("iv")),
            )
            if greeks:
                c.update(greeks)
        return candidates

    def run_cycle(self):
        """One complete §31 cycle. Returns the report dict."""
        self.cycle_count += 1
        now = datetime.now(IST).strftime("%H:%M:%S")

        # 1. session, 2. connectivity, 3. live data, 4. timestamps
        session_state = market_session_state(config)
        angel_source.ensure_connected()  # §47 reconnect with rate limit
        self.underlying_engine.update_from_cache()
        underlying_snapshot = self.underlying_engine.snapshot()
        spot = underlying_snapshot.get("spot")
        data_quality = self.compute_data_quality()

        # §46/§57 hard gate: stale critical data must never produce a signal.
        if data_quality < config.MIN_DATA_QUALITY:
            report = self._no_signal_report(now, spot, regime=None, data_quality=data_quality)
            report["no_signal_reasons"] = [
                f"Data quality {data_quality} below minimum {config.MIN_DATA_QUALITY}; "
                + ", ".join(self.last_quality_reasons or ["critical data stale/missing"])
            ]
            return report

        # 5./6. underlying and option chain in snapshot form
        chain = self.chain_engine.get_normalized_chain()
        chain = self.chain_engine.overlay_angel_quotes(chain)

        # 12. regime
        regime = self.regime_engine.classify(underlying_snapshot)

        # 7./8. candidates (exactly 10 when sufficient data)
        candidates, candidate_status = self.candidate_generator.generate(spot, regime)

        report = self._no_signal_report(now, spot, regime, data_quality=data_quality)
        report["candidate_status"] = candidate_status

        if data_quality < 100 and self.last_quality_reasons:
            report["data_note"] = "Failed: " + ", ".join(self.last_quality_reasons)

        if spot is None:
            report["no_signal_reasons"] = ["No live underlying data (stale or missing)"]
            return report

        if candidate_status != "OK":
            report["no_signal_reasons"] = [f"INSUFFICIENT VALID CANDIDATES ({candidate_status})"]
            return report
        # 9. metrics: Greeks then risk numbers
        candidates = self.attach_greeks(candidates, spot)
        candidates = self.ranking_engine.enrich_candidates(candidates, underlying_snapshot)

        # 10. score
        scored = self.scoring_engine.score_candidates(candidates, underlying_snapshot, regime)

        # 11. previous cycle, 16. confidence, 17./18. gates
        previous_cycle = CycleHistory.latest_cycle()
        best, top3, gate_reason = self.ranking_engine.select_best(scored)
        if best is not None and previous_cycle is not None:
            # §35 state transition from objective score/identity rules.
            report["signal_state"] = detect_state(
                previous_cycle, best, best["total_score"]
            )
        elif best is not None:
            report["signal_state"] = "NEW"

        if scored:
            report["candidates"] = scored[: config.CANDIDATE_COUNT]
            report["best_raw_score"] = scored[0].get("total_score")

        if best is None:
            report["gate_reason"] = gate_reason
            report["no_signal_reasons"] = self._gate_failure_reasons(gate_reason, scored)
            report["signal_state"] = "NO SIGNAL"
            return report

        historical_stats = self.outcome_engine.historical_setup_stats(
            config.UNDERLYING, best.get("strike"), best.get("option_type")
        )
        confidence_value, confidence_label = self.confidence_engine.calculate(
            best, data_quality, regime, previous_cycle, historical_stats
        )
        report["confidence_value"] = confidence_value
        report["confidence_label"] = confidence_label

        if confidence_value < config.MIN_CONFIDENCE:
            report["gate_reason"] = "CONFIDENCE_BELOW_MIN"
            report["no_signal_reasons"] = [
                f"Confidence {confidence_value} below minimum {config.MIN_CONFIDENCE}"
            ]
            report["signal_state"] = "NO SIGNAL"
            return report

        report["best"] = best
        report["previous_symbol"] = previous_cycle.get("best_symbol") if previous_cycle else None
        report["previous_score"] = previous_cycle.get("best_score") if previous_cycle else None
        if report["previous_score"] is not None:
            report["score_change"] = round(best["total_score"] - float(report["previous_score"]), 2)
        return report

    def _no_signal_report(self, now, spot, regime=None, data_quality=None):
        """Base report skeleton for a NO SIGNAL / early-return cycle."""
        return {
            "cycle_id": None,
            "time": now,
            "underlying": config.UNDERLYING,
            "spot": spot,
            "expiry": self.chain_engine.current_expiry(),
            "regime": regime if regime is not None else "NO_CLEAR_REGIME",
            "data_quality": data_quality,
            "candidates": [],
            "best": None,
            "candidate_status": None,
            "best_raw_score": None,
            "required_score": config.MIN_SCORE,
            "gate_reason": None,
            "no_signal_reasons": [],
            "confidence_value": None,
            "confidence_label": None,
            "previous_symbol": None,
            "previous_score": None,
            "score_change": None,
            "signal_state": "NO SIGNAL",
        }

    def _gate_failure_reasons(self, gate_reason, scored):
        reasons = []
        if gate_reason == "SCORE_BELOW_MIN":
            reasons.append(f"Best score {scored[0].get('total_score')} below required {config.MIN_SCORE}")
        elif gate_reason == "RR_BELOW_MIN":
            reasons.append("Risk/Reward insufficient")
        elif gate_reason == "NO_VALID_TARGET":
            reasons.append("NO VALID TARGET for best candidate")
        elif gate_reason == "SPREAD_TOO_WIDE":
            reasons.append("Candidate spread exceeds maximum")
        elif gate_reason == "LIQUIDITY_REJECTED":
            reasons.append("Candidate liquidity rejected")
        else:
            reasons.append(f"Gate failed: {gate_reason}")
        reasons.append("No high-quality setup")
        return reasons

    # ------------------------------------------------------------ persistence

    def persist_cycle(self, report):
        """Step 19: save cycle, snapshots, candidates, signals (§32, §59-64)."""
        # §40/§64: resolve open signal outcomes from the latest live premium
        # BEFORE writing this cycle's new state.
        try:
            resolved = self.outcome_engine.monitor_open_signals(self._open_signals)
            for signal_id in resolved:
                self._open_signals.pop(signal_id, None)
        except Exception:
            logger.exception("Open-signal monitoring failed")

        best = report.get("best")
        cycle_id = CycleHistory.save_cycle({
            "underlying": report["underlying"],
            "spot": report["spot"],
            "expiry": report.get("expiry"),
            "regime": report.get("regime"),
            "data_quality": report.get("data_quality"),
            "best_symbol": (
                f"{best['underlying']} {best['strike']} {best['option_type']}" if best else None
            ),
            "best_score": best.get("total_score") if best else None,
            "confidence": report.get("confidence_value"),
            "entry": best.get("entry_high") if best else None,
            "stop_loss": best.get("stop_loss") if best else None,
            "target": best.get("target") if best else None,
            "risk_reward": best.get("risk_reward") if best else None,
            "signal_state": report.get("signal_state"),
        })
        report["cycle_id"] = cycle_id

        if report.get("candidates"):
            CycleHistory.save_candidate_scores(cycle_id, report["candidates"])
            CycleHistory.save_option_snapshot(cycle_id, report["underlying"], report["candidates"])
        # §60/§42: market snapshot row enables genuine historical replay later.
        # Source provenance comes from the actual underlying quote, not hardcoded.
        _snap = self.underlying_engine.snapshot()
        _uq = cache.get_underlying(config.UNDERLYING) or {}
        CycleHistory.save_market_snapshot(
            cycle_id,
            {
                "symbol": report["underlying"],
                "spot": report.get("spot"),
                "vwap": _snap.get("vwap"),
                "atr": _snap.get("atr"),
                "trend": _snap.get("trend"),
            },
            report.get("regime"),
            report.get("data_quality"),
            source=_uq.get("source") or "UNKNOWN",
        )

        # §63: only genuine BEST signals create signal records; NO SIGNAL
        # cycles write no fake signal row (signals PK is not null-capable).
        if best:
            signal_id = SignalHistory.create_signal_id(
                best["underlying"], best["strike"], best["option_type"]
            )
            created = SignalHistory.save_signal({
                "signal_id": signal_id,
                "underlying": best["underlying"],
                "expiry": best.get("expiry"),
                "strike": best.get("strike"),
                "option_type": best.get("option_type"),
                "entry": best.get("entry_high"),
                "stop_loss": best.get("stop_loss"),
                "target": best.get("target"),
                "score": best.get("total_score"),
                "confidence": report.get("confidence_value"),
                "regime": report.get("regime"),
                "state": report.get("signal_state"),
            })
            if created:
                SignalHistory.record_state(
                    signal_id, report.get("signal_state"), best.get("total_score"),
                    report.get("confidence_value"),
                )
                # §64: track the open signal so later cycles can resolve its
                # outcome from live premium. Token via Scrip Master; None when
                # unavailable (monitoring then skips — never fabricated).
                contract = angel_source.find_option_contract(
                    best.get("strike"), best.get("option_type"), best.get("expiry")
                )
                self._open_signals[signal_id] = {
                    "token": str(contract.get("token")) if contract else None,
                    "entry": best.get("entry_high"),
                    "stop_loss": best.get("stop_loss"),
                    "target": best.get("target"),
                    "highest": None,
                    "lowest": None,
                    "created": datetime.now(IST),
                }
        return cycle_id

    # -------------------------------------------------------------------- run

    def run(self):
        self.running = True
        logger.info(
            "BLOCKORA starting: underlying=%s db=%s",
            config.UNDERLYING, db.db_path,
        )
        db.log_event("STARTUP", f"underlying={config.UNDERLYING}")

        self.start_data_feeds()
        threading.Thread(target=self.fetch_jugaad_loop, daemon=True, name="jugaad-fetch").start()
        threading.Thread(target=self.fetch_underlying_rest_loop, daemon=True, name="angel-rest").start()

        # Allow the first data ingestion before the first cycle.
        time.sleep(3)

        try:
            while self.running:
                state = market_session_state(config)
                if state != "MARKET_OPEN":
                    logger.info("Session state %s: no live strike selection (§69)", state)
                    print_status_line(
                        "CONNECTED" if angel_source.connected else "DISCONNECTED",
                        "OK" if self.jugaad_ok else "DOWN",
                        "OK",
                        datetime.fromtimestamp(self.last_data_ts).strftime("%H:%M:%S") if self.last_data_ts else "N/A",
                        datetime.fromtimestamp(next_minute_boundary()).strftime("%H:%M:%S"),
                    )
                    sleep_until_next_cycle()
                    continue

                try:
                    report = self.run_cycle()
                    cycle_id = self.persist_cycle(report)
                    report["cycle_id"] = cycle_id
                    print_cycle_report(report)
                except Exception:
                    logger.exception("Cycle failed")
                    db.log_event("CYCLE_ERROR", "see logs")

                print_status_line(
                    "CONNECTED" if angel_source.connected else "DISCONNECTED",
                    "OK" if self.jugaad_ok else "DOWN",
                    "OK",
                    datetime.fromtimestamp(self.last_data_ts).strftime("%H:%M:%S") if self.last_data_ts else "N/A",
                    datetime.fromtimestamp(next_minute_boundary()).strftime("%H:%M:%S"),
                )
                sleep_until_next_cycle()
        finally:
            self.running = False
            angel_source.stop_websocket()
            db.log_event("SHUTDOWN", "engine stopped")
            logger.info("BLOCKORA stopped")


def main():
    selector = OptionStrikeSelector()

    def _handle(sig, frame):
        logger.info("Received signal %s; shutting down", sig)
        selector.running = False

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)
    selector.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
