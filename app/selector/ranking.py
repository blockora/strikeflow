"""Ranking and gate application (BLOCKORA §24, §27, §57, §58).

select_best applies the hard gates and returns (best, top3, gate_reason).
A candidate without a valid target/risk_reward is rejected with
NO_VALID_TARGET per §27 — it must never become BEST.
"""

from app.config import config
from app.risk.entry import EntryEngine
from app.risk.stoploss import StopLossEngine
from app.risk.target import TargetEngine


def _safe_spread_pct(bid, ask):
    if bid is None or ask is None:
        return None
    try:
        b, a = float(bid), float(ask)
    except (TypeError, ValueError):
        return None
    if b <= 0 or a <= 0:
        return None
    mid = (a + b) / 2.0
    if mid <= 0:
        return None
    return ((a - b) / mid) * 100.0


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
                c, entry_high, sl, config.MIN_RR, underlying
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

    def hard_filter(self, candidate):
        """All §57 gates. Returns (ok, reason)."""
        score = candidate.get("total_score")
        if score is None or score < config.MIN_SCORE:
            return False, "SCORE_BELOW_MIN"

        rr = candidate.get("risk_reward")
        if rr is None:
            return False, "NO_VALID_TARGET"
        try:
            rr = float(rr)
        except (TypeError, ValueError):
            return False, "NO_VALID_TARGET"
        if rr < config.MIN_RR:
            return False, "RR_BELOW_MIN"

        spread_pct = _safe_spread_pct(candidate.get("bid"), candidate.get("ask"))
        if spread_pct is not None and spread_pct > config.MAX_SPREAD_PERCENT:
            return False, "SPREAD_TOO_WIDE"

        liquidity_score = candidate.get("scores", {}).get("liquidity")
        if liquidity_score is not None and liquidity_score <= 0:
            return False, "LIQUIDITY_REJECTED"

        return True, "OK"

    def select_best(self, scored_candidates):
        """Returns (best, top3, gate_reason). best is None when all fail gates."""
        if not scored_candidates:
            return None, [], "NO_CANDIDATES"

        best = scored_candidates[0]
        ok, reason = self.hard_filter(best)
        top3 = scored_candidates[:3]
        if not ok:
            return None, top3, reason
        return best, top3, "OK"
