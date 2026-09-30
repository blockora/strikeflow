from app.data.cache import cache
from app.data.normalizer import normalize_jugaad_option_chain


class OptionChainEngine:
    def __init__(self, symbol="NIFTY"):
        self.symbol = symbol

    def get_normalized_chain(self):
        raw = cache.get_option_chain(self.symbol)
        if not raw:
            return []
        return normalize_jugaad_option_chain(raw)

    def current_expiry(self):
        chain = self.get_normalized_chain()
        if not chain:
            return None
        return chain[0].get("expiry")

    def find_row(self, strike, option_type):
        chain = self.get_normalized_chain()
        for row in chain:
            if row.get("strike") == strike:
                return row.get(option_type.lower())
        return None

    def support_resistance_from_oi(self):
        chain = self.get_normalized_chain()
        if not chain:
            return None, None

        # Filter to only the current expiry
        current_expiry = chain[0].get("expiry")
        active_chain = [
            row for row in chain if _safe_get(row, "expiry") == current_expiry
        ] if current_expiry else chain

        if not active_chain:
            return None, None

        max_ce = None
        max_pe = None
        max_ce_oi = -1
        max_pe_oi = -1

        for row in active_chain:
            ce = row.get("ce") or {}
            pe = row.get("pe") or {}
            ce_oi = _safe_get(ce, "oi", 0)
            pe_oi = _safe_get(pe, "oi", 0)
            try:
                ce_oi = int(ce_oi) if ce_oi is not None else 0
                pe_oi = int(pe_oi) if pe_oi is not None else 0
            except (TypeError, ValueError):
                ce_oi = 0
                pe_oi = 0
            if ce_oi > max_ce_oi:
                max_ce_oi = ce_oi
                max_ce = row.get("strike")
            if pe_oi > max_pe_oi:
                max_pe_oi = pe_oi
                max_pe = row.get("strike")

        return max_ce, max_pe


def _safe_get(obj, key, default=None):
    val = obj.get(key, default) if isinstance(obj, dict) else default
    if val is None:
        return default
    return val
