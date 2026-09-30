import time

from app.config import config


class DataValidator:
    @staticmethod
    def validate_quote(quote):
        if not quote:
            return False, "MISSING"
        age = None
        local_ts = quote.get("local_timestamp")
        if local_ts:
            age = time.time() - local_ts
        if age is not None and age > config.MAX_STALE_SECONDS:
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
    def compare_ltp(a, b, tolerance_pct=0.5):
        if a is None or b is None:
            return None
        if a == 0 or b == 0:
            return None
        diff_pct = abs(a - b) / ((a + b) / 2.0) * 100.0
        return diff_pct <= tolerance_pct


validator = DataValidator()
