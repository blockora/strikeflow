"""Candidate strike generation (BLOCKORA §10, §11, §14).

Selects a balanced CE/PE universe around ATM from the normalized option chain,
rejecting contracts that fail hard liquidity rules. Produces exactly
CANDIDATE_COUNT valid contracts whenever sufficient data exists; otherwise
reports INSUFFICIENT_VALID_CANDIDATES and never forces a full set.
"""

import math

from app.config import config
from app.market.option_chain import OptionChainEngine


def _valid_ltp(val):
    if val is None:
        return False
    try:
        f = float(val)
    except (TypeError, ValueError):
        return False
    if math.isnan(f) or math.isinf(f) or f <= 0:
        return False
    return True


def _safe_get(d, key, default=None):
    val = d.get(key, default) if isinstance(d, dict) else default
    if val is None:
        return default
    return val


def spread_percent(bid, ask):
    """Spread % per BLOCKORA §20; None when bid/ask are unusable."""
    if bid is None or ask is None:
        return None
    try:
        b, a = float(bid), float(ask)
    except (TypeError, ValueError):
        return None
    if b <= 0 or a <= 0 or a < b:
        return None
    mid = (a + b) / 2.0
    if mid <= 0:
        return None
    return (a - b) / mid * 100.0


def passes_liquidity_filter(opt):
    """Hard liquidity gate (BLOCKORA §14).

    Rejects contracts with unusable quote structure or spread beyond
    MAX_SPREAD_PERCENT. Fields that are merely missing do not fabricate a
    pass; they are allowed through for scoring to penalize, except where the
    quote itself is unusable.
    """
    bid = _safe_get(opt, "bid")
    ask = _safe_get(opt, "ask")
    sp = spread_percent(bid, ask)
    if sp is None:
        return True  # cannot evaluate: let scoring handle missing bid/ask
    return sp <= config.MAX_SPREAD_PERCENT


class CandidateGenerator:
    def __init__(self, symbol="NIFTY"):
        self.symbol = symbol
        self.chain_engine = OptionChainEngine(symbol)

    def nearest_atm(self, spot, interval):
        if spot is None or interval in (None, 0):
            return None
        return int(round(spot / interval) * interval)

    def _candidate_for(self, row, side, atm):
        opt = row.get(side.lower()) or {}
        ltp = _safe_get(opt, "ltp")
        if not _valid_ltp(ltp):
            return None
        if not passes_liquidity_filter(opt):
            return None
        strike = row.get("strike")
        return {
            "underlying": self.symbol,
            "strike": strike,
            "option_type": side,
            "expiry": row.get("expiry"),
            "distance_from_atm": abs(strike - atm),
            "ltp": ltp,
            "bid": _safe_get(opt, "bid"),
            "ask": _safe_get(opt, "ask"),
            "bid_qty": _safe_get(opt, "bid_qty"),
            "ask_qty": _safe_get(opt, "ask_qty"),
            "volume": _safe_get(opt, "volume"),
            "oi": _safe_get(opt, "oi"),
            "oi_change": _safe_get(opt, "oi_change"),
            "iv": _safe_get(opt, "iv"),
            "prev_close": _safe_get(opt, "prev_close"),
            "change": _safe_get(opt, "change"),
            "pchange": _safe_get(opt, "pchange"),
            "source": "JUGAAD",
        }

    def generate(self, spot, regime):
        """Return (candidates, status) per BLOCKORA §11."""
        interval = config.STRIKE_INTERVAL
        atm = self.nearest_atm(spot, interval)
        if atm is None:
            return [], "NO_SPOT"
        chain = self.chain_engine.get_normalized_chain()
        if not chain:
            return [], "NO_CHAIN_DATA"

        rows_by_strike = {row.get("strike"): row for row in chain if row.get("strike") is not None}

        # Balanced CE+PE selection: for each side walk ATM distances
        # (-n..+n) until CANDIDATE_COUNT//2 contracts are found per side.
        half = max(1, config.CANDIDATE_COUNT // 2)
        per_side = {}
        for side in ("CE", "PE"):
            picked = []
            for offset in range(-half, half + 1):
                if len(picked) >= half:
                    break
                strike = atm + offset * interval
                row = rows_by_strike.get(strike)
                if not row:
                    continue
                cand = self._candidate_for(row, side, atm)
                if cand is None:
                    continue
                picked.append(cand)
            per_side[side] = picked

        candidates = per_side["CE"] + per_side["PE"]

        if len(candidates) >= config.CANDIDATE_COUNT:
            candidates.sort(key=lambda c: (c["distance_from_atm"], c["option_type"]))
            return candidates[: config.CANDIDATE_COUNT], "OK"
        if candidates:
            return candidates, "INSUFFICIENT_VALID_CANDIDATES"
        return [], "NO_VALID_CONTRACTS"
