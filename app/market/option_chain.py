"""Option-chain analysis engine (BLOCKORA §5, §13, §20).

Reads normalized Jugaad chain rows from the cache, overlays Angel WebSocket
quotes per contract when available, computes support/resistance from real OI,
and flags data conflicts between sources (BLOCKORA §6).
"""

import logging

from app.config import config
from app.data.cache import cache
from app.data.normalizer import normalize_jugaad_option_chain
from app.data.validator import validator

logger = logging.getLogger(__name__)


def _safe_float(value):
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):
        return None
    return v


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

    def chain_age_seconds(self):
        return cache.chain_age(self.symbol)

    def find_row(self, strike, option_type):
        chain = self.get_normalized_chain()
        for row in chain:
            if row.get("strike") == strike:
                return row.get(option_type.lower())
        return None

    def overlay_angel_quotes(self, chain):
        """Overlay live Angel ticks onto Jugaad chain rows (BLOCKORA §5).

        Angel bid/ask/OI/volume are added under angel_* keys with provenance
        intact; Jugaad values are never silently overwritten.
        """
        if not chain:
            return chain
        for row in chain:
            for side in ("ce", "pe"):
                side_data = row.get(side)
                if not side_data:
                    continue
                contract = angel_find_contract(row.get("strike"), side.upper(), row.get("expiry"))
                if not contract:
                    continue
                token = str(contract.get("token"))
                quote = cache.get_quote(token)
                if not quote:
                    continue
                ok, reason = validator.validate_quote(quote, max_stale_seconds=config.MAX_STALE_SECONDS)
                if not ok:
                    logger.debug("Angel quote for token %s rejected (%s)", token, reason)
                    continue
                side_data["angel_bid"] = _safe_float(quote.get("bid"))
                side_data["angel_ask"] = _safe_float(quote.get("ask"))
                side_data["angel_ltp"] = _safe_float(quote.get("ltp"))
                side_data["angel_oi"] = _safe_float(quote.get("oi"))
                side_data["angel_volume"] = _safe_float(quote.get("volume"))
                side_data["angel_timestamp"] = quote.get("local_timestamp")
        return chain

    def support_resistance_from_oi(self):
        """Max-OI CE strike acts as resistance, max-OI PE strike as support."""
        chain = self.get_normalized_chain()
        if not chain:
            return None, None
        current_expiry = chain[0].get("expiry")
        active_chain = [r for r in chain if r.get("expiry") == current_expiry] if current_expiry else chain
        if not active_chain:
            return None, None

        max_ce = None
        max_pe = None
        max_ce_oi = -1
        max_pe_oi = -1
        for row in active_chain:
            for side, best_ref in (("ce", "max_ce"), ("pe", "max_pe")):
                data = row.get(side) or {}
                oi = _safe_float(data.get("oi"))
                if oi is None:
                    continue
                if side == "ce" and oi > max_ce_oi:
                    max_ce_oi = oi
                    max_ce = row.get("strike")
                if side == "pe" and oi > max_pe_oi:
                    max_pe_oi = oi
                    max_pe = row.get("strike")
        return max_ce, max_pe

    def spot_conflict(self, underlying_spot):
        """Compare chain underlying value to the live spot (BLOCKORA §6).

        Returns True (agree), False (conflict) or None (cannot compare).
        """
        chain = self.get_normalized_chain()
        if not chain:
            return None
        underlying_value = _safe_float(chain[0].get("underlying_value"))
        if underlying_value is None or underlying_spot is None:
            return None
        diff_pct = abs(underlying_value - underlying_spot) / underlying_spot * 100.0
        return diff_pct <= config.SPOT_TOLERANCE_PCT


def angel_find_contract(strike, option_type, expiry):
    """Scrip-master lookup helper kept lazy to avoid import cycles."""
    from app.data.angel import angel_source

    if angel_source.scrip_master is None:
        return None
    return angel_source.find_option_contract(strike, option_type, expiry)
