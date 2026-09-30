from app.config import config
from app.risk.entry import EntryEngine
from app.risk.stoploss import StopLossEngine
from app.risk.target import TargetEngine


class RankingEngine:
    def __init__(self):
        self.entry_engine = EntryEngine()
        self.sl_engine = StopLossEngine()
        self.target_engine = TargetEngine()

    def enrich_candidates(self, candidates, underlying):
        enriched = []
        for c in candidates:
            c = dict(c)
            entry_low, entry_high, entry_basis = self.entry_engine.build_entry_zone(c)
            sl, sl_reason = self.sl_engine.build_stoploss(c, underlying)
            target, rr, reward = self.target_engine.build_target(
                c, entry_high, sl, config.MIN_RR
            )
            risk = None
            if entry_high is not None and sl is not None:
                risk = round(entry_high - sl, 2)

            c["entry_low"] = entry_low
            c["entry_high"] = entry_high
            c["entry_basis"] = entry_basis
            c["stop_loss"] = sl
            c["sl_reason"] = sl_reason
            c["target"] = target
            c["risk"] = risk
            c["reward"] = reward
            c["risk_reward"] = rr
            enriched.append(c)
        return enriched

    @staticmethod
    def _safe_spread_pct(bid, ask):
        """Calculate spread percentage safely, avoiding division by zero."""
        if bid is None or ask is None:
            return None
        try:
            b = float(bid)
            a = float(ask)
        except (TypeError, ValueError):
            return None
        if b <= 0 or a <= 0:
            return None
        mid = (a + b) / 2.0
        if mid <= 0:
            return None
        return ((a - b) / mid) * 100.0

    @staticmethod
    def hard_filter(candidate):
        score = candidate.get("total_score")
        rr = candidate.get("risk_reward")
        bid = candidate.get("bid")
        ask = candidate.get("ask")

        if score is None or score < config.MIN_SCORE:
            return False, "SCORE_BELOW_MIN"
        if rr is None or rr < config.MIN_RR:
            return False, "RR_BELOW_MIN"

        spread_pct = _safe_spread_pct(bid, ask)
        if spread_pct is not None and spread_pct > config.MAX_SPREAD_PERCENT:
            return False, "SPREAD_TOO_WIDE"
        return True, "OK"

    def select_best(self, scored_candidates):
        if not scored_candidates:
            return None, []

        best = scored_candidates[0]
        ok, reason = self.hard_filter(best)
        if not ok:
            return None, scored_candidates[:3]
        return best, scored_candidates[:3]
