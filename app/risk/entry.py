class EntryEngine:
    @staticmethod
    def build_entry_zone(candidate):
        ltp = candidate.get("ltp")
        bid = candidate.get("bid")
        ask = candidate.get("ask")

        if ltp is None:
            return None, None, None

        if bid is not None and ask is not None and ask > bid:
            mid = (bid + ask) / 2.0
            low = bid
            high = ask
            basis = "BID_ASK"
        else:
            low = ltp * 0.985
            high = ltp * 1.015
            basis = "LTP_BUFFER"

        return round(low, 2), round(high, 2), basis
