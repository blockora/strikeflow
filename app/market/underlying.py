import math

from app.data.cache import cache
from app.market.indicators import atr, ema, pct_change, realized_volatility, sma, vwap


def _safe_float(value):
    try:
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            return None
        return v
    except (TypeError, ValueError):
        return None


class UnderlyingEngine:
    def __init__(self, symbol="NIFTY"):
        self.symbol = symbol
        self.price_history = []
        self.high_history = []
        self.low_history = []
        self.volume_history = []

    def _ensure_history(self, quote):
        if quote is None:
            return
        ltp = _safe_float(quote.get("ltp"))
        high = _safe_float(quote.get("high"))
        low = _safe_float(quote.get("low"))
        vol = _safe_float(quote.get("volume"))
        if ltp is not None:
            self.price_history.append(ltp)
            if len(self.price_history) > 300:
                self.price_history = self.price_history[-300:]
            if high is not None:
                self.high_history.append(high)
                if len(self.high_history) > 300:
                    self.high_history = self.high_history[-300:]
            if low is not None:
                self.low_history.append(low)
                if len(self.low_history) > 300:
                    self.low_history = self.low_history[-300:]
            if vol is not None:
                self.volume_history.append(vol)
                if len(self.volume_history) > 300:
                    self.volume_history = self.volume_history[-300:]

    def update_from_cache(self):
        quote = cache.get_underlying(self.symbol)
        self._ensure_history(quote)
        return quote

    def snapshot(self):
        closes = self.price_history
        highs = self.high_history
        lows = self.low_history
        volumes = self.volume_history

        if not closes:
            return {
                "symbol": self.symbol,
                "spot": None,
                "return_1m": None,
                "return_3m": None,
                "return_5m": None,
                "ma_fast": None,
                "ma_slow": None,
                "vwap": None,
                "atr": None,
                "realized_volatility": None,
                "trend": "NEUTRAL",
            }

        last = closes[-1]
        prev = closes[-2] if len(closes) > 1 else None
        ret_1m = pct_change(last, prev)

        ret_3m = None
        if len(closes) > 3:
            ret_3m = pct_change(last, closes[-4])

        ret_5m = None
        if len(closes) > 5:
            ret_5m = pct_change(last, closes[-6])

        ma_fast = ema(closes, 5) or sma(closes, 5)
        ma_slow = ema(closes, 20) or sma(closes, 20)

        # Use actual high/low history; fall back to closes if insufficient
        if highs and len(highs) >= len(closes):
            effective_highs = highs
        else:
            effective_highs = closes
        if lows and len(lows) >= len(closes):
            effective_lows = lows
        else:
            effective_lows = closes

        vw = vwap(effective_highs, effective_lows, closes, volumes)
        atr_val = atr(effective_highs, effective_lows, closes, 14)
        vol = realized_volatility(
            [pct_change(closes[i], closes[i - 1]) for i in range(1, len(closes))]
        )

        trend = "NEUTRAL"
        if ma_fast is not None and ma_slow is not None:
            if last > ma_fast > ma_slow:
                trend = "BULLISH"
            elif last < ma_fast < ma_slow:
                trend = "BEARISH"

        return {
            "symbol": self.symbol,
            "spot": last,
            "return_1m": ret_1m,
            "return_3m": ret_3m,
            "return_5m": ret_5m,
            "ma_fast": ma_fast,
            "ma_slow": ma_slow,
            "vwap": vw,
            "atr": atr_val,
            "realized_volatility": vol,
            "trend": trend,
        }
