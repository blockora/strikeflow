"""Entry zone calculation (BLOCKORA §25).

Entry Zone (a range) is preferred over a false exact-price prediction. The
zone comes from the real bid/ask when usable, otherwise a documented
±1.5% band around the live LTP. No valid price -> no entry (None).
"""


class EntryEngine:
    @staticmethod
    def build_entry_zone(candidate):
        ltp = candidate.get("ltp")
        bid = candidate.get("bid")
        ask = candidate.get("ask")

        try:
            ltp = float(ltp) if ltp is not None else None
        except (TypeError, ValueError):
            ltp = None
        if ltp is None or ltp <= 0:
            return None, None, None

        try:
            bid = float(bid) if bid is not None else None
            ask = float(ask) if ask is not None else None
        except (TypeError, ValueError):
            bid = ask = None

        if bid is not None and ask is not None and 0 < bid <= ask:
            low, high = bid, ask
            basis = "BID_ASK"
        else:
            low = ltp * 0.985
            high = ltp * 1.015
            basis = "LTP_BUFFER"

        return round(low, 2), round(high, 2), basis
