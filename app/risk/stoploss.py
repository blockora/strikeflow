"""Stop-loss calculation (BLOCKORA §26).

SL is derived from setup invalidation, not one universal percentage: primary
is a volatility-adjusted premium stop bounded to a sane range; fallbacks use
bid structure and then a fixed premium invalidation. The SL reason is always
exposed.
"""


class StopLossEngine:
    MIN_STOP_PCT = 0.04   # never tighter than 4% below premium
    MAX_STOP_PCT = 0.10   # never wider than 10% below premium

    @staticmethod
    def build_stoploss(candidate, underlying=None):
        ltp = candidate.get("ltp")
        bid = candidate.get("bid")
        atr = underlying.get("atr") if underlying else None
        spot = underlying.get("spot") if underlying else None

        try:
            ltp = float(ltp) if ltp is not None else None
        except (TypeError, ValueError):
            ltp = None
        if ltp is None or ltp <= 0:
            return None, None

        try:
            bid = float(bid) if bid is not None else None
        except (TypeError, ValueError):
            bid = None

        if atr is not None and spot is not None and spot > 0:
            atr_pct = atr / spot
            stop_pct = max(self_min := StopLossEngine.MIN_STOP_PCT, min(StopLossEngine.MAX_STOP_PCT, atr_pct * 2))
            sl = ltp * (1 - stop_pct)
            reason = "VOLATILITY_ADJUSTED_PREMIUM_STOP"
        elif bid is not None and bid > 0:
            sl = min(ltp * 0.93, bid * 0.97)
            reason = "BID_STRUCTURE_STOP"
        else:
            sl = ltp * 0.93
            reason = "FIXED_PREMIUM_INVALIDATION"

        return round(sl, 2), reason
