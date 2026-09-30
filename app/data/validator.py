import logging
import time

from app.config import config

logger = logging.getLogger(__name__)


class DataValidator:
    """Freshness and sanity validation (BLOCKORA §6, §7, §46).

    Missing values stay missing. Stale data is reported as STALE, never
    silently refreshed from another source without provenance.
    """

    @staticmethod
    def validate_quote(quote, max_stale_seconds=None):
        if not quote:
            return False, "MISSING"
        limit = max_stale_seconds if max_stale_seconds is not None else config.MAX_STALE_SECONDS
        age = None
        local_ts = quote.get("local_timestamp")
        if local_ts:
            age = time.time() - local_ts
        if age is not None and age > limit:
            return False, "STALE"
        if quote.get("ltp") is None:
            return False, "MISSING_LTP"
        return True, "OK"

    @staticmethod
    def validate_option_row(row):
        required = ["strike", "expiry"]
        for field in required:
            if row.get(field) is None:
                return False, f"MISSING_{field.upper()}"
        side_fields = ["ltp", "bid", "ask", "oi", "volume"]
        present = sum(1 for f in side_fields if row.get(f) is not None)
        if present < 3:
            return False, "INSUFFICIENT_OPTION_DATA"
        return True, "OK"

    @staticmethod
    def compare_ltp(a, b, tolerance_pct=None):
        """Cross-source LTP agreement check (BLOCKORA §6 DATA CONFLICT).

        Returns True (agree), False (conflict) or None (cannot compare).
        """
        if a is None or b is None:
            return None
        try:
            a = float(a)
            b = float(b)
        except (TypeError, ValueError):
            return None
        if a == 0 or b == 0:
            return None
        tol = tolerance_pct if tolerance_pct is not None else config.SPOT_TOLERANCE_PCT
        diff_pct = abs(a - b) / ((a + b) / 2.0) * 100.0
        return diff_pct <= tol


validator = DataValidator()
