from app.config import config


class MarketRegimeEngine:
    """Market regime classification (BLOCKORA §12).

    Multi-input classification (returns, trend structure, ATR%, realized
    volatility). Thresholds are initial documented values from configuration;
    they must be validated through walk-forward testing before tuning
    (BLOCKORA §15, §67). NO_CLEAR_REGIME is produced when evidence is absent
    rather than guessing.
    """

    def classify(self, underlying_snapshot, option_chain_stats=None):
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

        # Breakout/breakdown: elevated ATR% plus aligned directional evidence.
        if atr_pct is not None and atr_pct > config.BREAKOUT_ATR_PCT:
            if aligned_bull >= 3:
                return "BREAKOUT"
            if aligned_bear >= 3:
                return "BREAKDOWN"
            return "HIGH_VOLATILITY"

        # Low volatility: quiet realized vol and no directional majority.
        if rv is not None and rv < config.LOW_VOL_RV_THRESHOLD and aligned_bull < 2 and aligned_bear < 2:
            return "LOW_VOLATILITY"

        if aligned_bull >= 3:
            return "BULLISH"
        if aligned_bear >= 3:
            return "BEARISH"
        if aligned_bull < 2 and aligned_bear < 2:
            return "CHOPPY"
        return "NEUTRAL"
