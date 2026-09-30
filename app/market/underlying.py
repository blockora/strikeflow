import logging
import time

from app.config import config
from app.data.cache import cache
from app.data.validator import validator
from app.market.indicators import atr, ema, pct_change, realized_volatility, sma, vwap

logger = logging.getLogger(__name__)


def _safe_float(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


class UnderlyingEngine:
    """Underlying (spot) analysis engine (BLOCKORA §12, §16).

    Keeps a bounded history of real quotes for trend/momentum/volatility
    computation. Quotes are appended only when the validator accepts them, so
    stale or missing data never enters the trend picture.
    """

    MAX_HISTORY = 300

    def __init__(self, symbol="NIFTY"):
        self.symbol = symbol
        self.price_history = []
        self.high_history = []
        self.low_history = []
        self.volume_history = []
        self.timestamps = []
        self._last_quote_ltp = None

    def _ensure_history(self, quote):
        if quote is None:
            return
        ltp = _safe_float(quote.get("ltp"))
        if ltp is None:
            return
        now = time.time()
        # At most one sample per second: dedupes repeated REST polls and
        # WebSocket bursts without inventing intermediate prices.
        if self.timestamps and now - self.timestamps[-1] < 1.0:
            return
        high = _safe_float(quote.get("high"))
        low = _safe_float(quote.get("low"))
        vol = _safe_float(quote.get("volume"))
        self.price_history.append(ltp)
        self.timestamps.append(now)
        self.high_history.append(high if high is not None else ltp)
        self.low_history.append(low if low is not None else ltp)
        self.volume_history.append(vol if vol is not None else 0.0)
        if len(self.price_history) > self.MAX_HISTORY:
            self.price_history = self.price_history[-self.MAX_HISTORY:]
            self.timestamps = self.timestamps[-self.MAX_HISTORY:]
            self.high_history = self.high_history[-self.MAX_HISTORY:]
            self.low_history = self.low_history[-self.MAX_HISTORY:]
            self.volume_history = self.volume_history[-self.MAX_HISTORY:]

    def update_from_cache(self):
        quote = cache.get_underlying(self.symbol)
        ok, reason = validator.validate_quote(quote)
        if not ok:
            if quote is not None:
                logger.warning("Underlying quote rejected (%s) for %s", reason, self.symbol)
            return quote
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
                "recent_high": None,
                "recent_low": None,
                "trend": "NEUTRAL",
                "samples": 0,
            }

        last = closes[-1]
        prev = closes[-2] if len(closes) > 1 else None
        ret_1m = pct_change(last, prev)
        ret_3m = pct_change(last, closes[-4]) if len(closes) > 3 else None
        ret_5m = pct_change(last, closes[-6]) if len(closes) > 5 else None

        ma_fast = ema(closes, 5) or sma(closes, 5)
        ma_slow = ema(closes, 20) or sma(closes, 20)

        vw = vwap(highs, lows, closes, volumes)
        atr_val = atr(highs, lows, closes, 14)
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
            "recent_high": max(highs) if highs else None,
            "recent_low": min(lows) if lows else None,
            "trend": trend,
            "samples": len(closes),
        }
