class StopLossEngine:
    @staticmethod
    def build_stoploss(candidate, underlying=None):
        ltp = candidate.get("ltp")
        bid = candidate.get("bid")
        atr = underlying.get("atr") if underlying else None
        spot = underlying.get("spot") if underlying else None

        if ltp is None:
            return None, None

        if atr is not None and spot is not None and spot > 0:
            atr_pct = atr / spot
            sl = ltp * (1 - max(0.04, atr_pct * 2))
            reason = "VOLATILITY_ADJUSTED_PREMIUM_STOP"
        elif bid is not None:
            sl = min(ltp * 0.93, bid * 0.97)
            reason = "BID_STRUCTURE_STOP"
        else:
            sl = ltp * 0.93
            reason = "FIXED_PREMIUM_INVALIDATION"

        return round(sl, 2), reason
