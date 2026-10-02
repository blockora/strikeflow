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

    Returns are computed on true wall-clock time, not on sample position.
    The price_history/timestamps arrays are kept in chronological order and a
    _lookback() walk searches forward from the series start for a sample that
    is covered by at least the requested number of seconds.  This keeps the
    returned 1m/3m/5m values meaningful even when Angel WebSocket ticks arrive
    many times per second, and it never borrows data from a previous cycle.
    """

    # Bounded, still tiny on Android 14: 600 samples x 5 float lists is roughly
    # 100 KB.  At the 1-second dedupe floor that is ~10 minutes of wall-clock
    # history, which leaves real margin over DIRECTION_LOOKBACK_MINUTES (5 min)
    # so the direction origin is not pinned to the single oldest sample.
    MAX_HISTORY = 600
    SOURCE_FIELD = "local_timestamp"

    def __init__(self, symbol="NIFTY"):
        self.symbol = symbol
        self.price_history = []
        self.high_history = []
        self.low_history = []
        self.volume_history = []
        self.timestamps = []
        self._last_quote_ltp = None

    # ------------------------------------------------------------------ helpers

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

    @staticmethod
    def _validated_price(prices, timestamps, idx):
        """Return the price at position `idx` if the cell is usable as a
        "past" anchor for a lookback return.

        The cell is usable only when it is not the current latest price cell
        (no future data, even a non-latest tick from later in the same cycle)
        and the price is present.  Coverage was already established by
        _lookback() before this cell is accepted, so this function only
        guards against the latest cell and against missing data.

        Returns None when the cell is not usable, otherwise the price.
        """
        if idx < 0 or idx >= len(prices):
            return None
        if prices[idx] is None:
            return None
        # Never use the latest cell as a "past" anchor: it is the current
        # price, not a price observed earlier in this cycle.
        if idx == len(prices) - 1:
            return None
        return prices[idx]

    def _lookback(self, pct_col_idx, seconds):
        """Return (price, first_valid_position) for `seconds` of wall-clock
        coverage, or (None, None) when the history is too thin.

        The caller must never construct an origin from a cell that failed
        validation, and must never reuse a position from another day or
        another cycle.
        """
        if not self.timestamps:
            return None, None
        if len(self.price_history) < 2:
            return None, None

        now = self.timestamps[-1]
        if now is None:
            return None, None

        # Look for the LATEST valid position whose timestamp is covered by
        # >= seconds of wall time.  The anchor must be a real past sample; it
        # must not be the current latest cell.  If no sample reaches the
        # requested coverage, there is insufficient history.
        valid_position = None
        covered = 0.0
        walk = 0
        while walk < len(self.timestamps):
            covered = now - self.timestamps[walk]
            if covered >= seconds:
                # The latest qualifying cell is the rightmost one with
                # coverage >= seconds (earlier cells are even older).
                valid_position = walk
            walk += 1

        if valid_position is None:
            return None, None

        position = self._validated_price(
            self.price_history, self.timestamps, valid_position
        )
        if position is None:
            return None, None
        return position, valid_position

    def underlying_age(self):
        """Age of the most recent underlying sample in seconds (None if no
        samples).  Used by the direction engine and data-quality checks."""
        if not self.price_history:
            return None
        return time.time() - self.timestamps[-1]

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
        # Wall-clock returns: walk from the series start for the requested
        # elapsed time, then apply pct_change to the LATEST price.
        calc = self._lookback(1, 60.0)
        t3 = self._lookback(1, 180.0)
        t5 = self._lookback(5, 300.0)

        position_1m, pos_1m = calc
        position_3m, pos_3m = t3
        position_5m, pos_5m = t5

        # If the required horizon does not have enough covered history, return
        # None for that horizon.  A short series (fewer than two samples) is
        # treated exactly the same as insufficient coverage.
        if pos_1m is None:
            ret_1m = None
        else:
            anchor = closes[pos_1m]
            ret_1m = pct_change(last, anchor)

        if pos_3m is None:
            ret_3m = None
        else:
            anchor = closes[pos_3m]
            ret_3m = pct_change(last, anchor)

        if pos_5m is None:
            ret_5m = None
        else:
            anchor = closes[pos_5m]
            ret_5m = pct_change(last, anchor)

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
