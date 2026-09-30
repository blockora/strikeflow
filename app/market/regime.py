class MarketRegimeEngine:
    @staticmethod
    def classify(underlying_snapshot, option_chain_stats=None):
        if not underlying_snapshot:
            return "NO_CLEAR_REGIME"

        trend = underlying_snapshot.get("trend", "NEUTRAL")
        ret_1m = underlying_snapshot.get("return_1m")
        ret_3m = underlying_snapshot.get("return_3m")
        ret_5m = underlying_snapshot.get("return_5m")
        atr = underlying_snapshot.get("atr")
        spot = underlying_snapshot.get("spot")
        rv = underlying_snapshot.get("realized_volatility")

        if spot is None:
            return "NO_CLEAR_REGIME"

        atr_pct = None
        if atr is not None and spot:
            atr_pct = (atr / spot) * 100.0

        aligned_bull = sum([
            ret_1m is not None and ret_1m > 0,
            ret_3m is not None and ret_3m > 0,
            ret_5m is not None and ret_5m > 0,
            trend == "BULLISH",
        ])

        aligned_bear = sum([
            ret_1m is not None and ret_1m < 0,
            ret_3m is not None and ret_3m < 0,
            ret_5m is not None and ret_5m < 0,
            trend == "BEARISH",
        ])

        if atr_pct is not None and atr_pct > 0.35:
            if aligned_bull >= 3:
                return "BREAKOUT"
            if aligned_bear >= 3:
                return "BREAKDOWN"
            return "HIGH_VOLATILITY"

        if rv is not None and rv < 0.03 and aligned_bull < 2 and aligned_bear < 2:
            return "LOW_VOLATILITY"

        if aligned_bull >= 3:
            return "BULLISH"
        if aligned_bear >= 3:
            return "BEARISH"
        if aligned_bull < 2 and aligned_bear < 2:
            return "CHOPPY"
        return "NEUTRAL"
