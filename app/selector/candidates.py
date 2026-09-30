import math


def _valid_ltp(val):
    """Validate LTP is a proper positive number.

    Returns True if val is a valid LTP, False otherwise.
    Rejects: None, non-numeric, 0, negative, NaN, infinity.
    """
    if val is None:
        return False
    try:
        f = float(val)
    except (TypeError, ValueError):
        return False
    if math.isnan(f) or math.isinf(f):
        return False
    if f <= 0:
        return False
    return True


def _safe_get(candidate_dict, key, default=None):
    val = candidate_dict.get(key, default)
    if val is None:
        return default
    return val


from app.config import config
from app.market.option_chain import OptionChainEngine


class CandidateGenerator:
    def __init__(self, symbol="NIFTY"):
        self.symbol = symbol
        self.chain_engine = OptionChainEngine(symbol)

    def nearest_atm(self, spot, interval):
        if spot is None or interval in (None, 0):
            return None
        return int(round(spot / interval) * interval)

    def generate(self, spot, regime):
        interval = config.STRIKE_INTERVAL
        atm = self.nearest_atm(spot, interval)
        if atm is None:
            return []

        chain = self.chain_engine.get_normalized_chain()
        if not chain:
            return []

        strikes = sorted({row["strike"] for row in chain if row.get("strike") is not None})
        if not strikes:
            return []

        preferred = []
        if regime in ("BULLISH", "BREAKOUT"):
            preferred = ["CE", "PE"]
        elif regime in ("BEARISH", "BREAKDOWN"):
            preferred = ["PE", "CE"]
        else:
            preferred = ["CE", "PE"]

        candidates = []
        seen = set()

        for side in preferred:
            for offset_steps in range(0, 8):
                for direction in ([0] if offset_steps == 0 else [-1, 1]):
                    strike = atm + direction * offset_steps * interval
                    if strike in seen:
                        continue
                    row = self._find_row(chain, strike)
                    if not row:
                        continue
                    opt = row.get(side.lower()) or {}
                    ltp = _safe_get(opt, "ltp")
                    if not _valid_ltp(ltp):
                        continue
                    candidates.append({
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
                        "source": "JUGAAD",
                    })
                    seen.add(strike)
                    if len(candidates) >= config.CANDIDATE_COUNT:
                        return candidates[:config.CANDIDATE_COUNT]
        return candidates[:config.CANDIDATE_COUNT]

    @staticmethod
    def _find_row(chain, strike):
        for row in chain:
            if row.get("strike") == strike:
                return row
        return None
