"""Field mapping from NSE NextApi option-chain rows (Jugaad secondary source).

Keys verified against live NSE NextApi GetQuoteApi getSymbolDerivativesData
responses: lastPrice, pchange, change, totalTradedVolume, openInterest,
changeinOpenInterest, impliedVolatility, bidprice, askPrice, bidQty, askQty,
prevClose, expiryDate, strikePrice, optionType, underlyingValue.

Values are passed through as-is; missing values remain None (BLOCKORA §7).
"""


def side_from_row(row):
    """Map one raw NSE option row to the normalized side dict (all fields)."""
    return {
        "ltp": row.get("lastPrice"),
        "bid": row.get("bidprice"),
        "ask": row.get("askPrice"),
        "bid_qty": row.get("bidQty"),
        "ask_qty": row.get("askQty"),
        "volume": row.get("totalTradedVolume"),
        "oi": row.get("openInterest"),
        "oi_change": row.get("changeinOpenInterest"),
        "iv": row.get("impliedVolatility"),
        "prev_close": row.get("prevClose"),
        "change": row.get("change"),
        "pchange": row.get("pchange"),
    }


def normalize_angel_quote(raw_quote):
    """Normalize an Angel One WebSocket tick (BLOCKORA §4, §6).

    Accepts the RAW SmartApi smartWebSocketV2 tick (per the installed SDK's
    _parse_binary_data, QUOTE mode): last_traded_price / open_price_of_the_day /
    high_price_of_the_day / low_price_of_the_day / closed_price arrive in
    paise (value * 100) and are converted to rupees here; day volume is
    volume_trade_for_the_day; OI (open_interest) exists only in SNAP_QUOTE
    mode, so it stays None on QUOTE ticks — never fabricated (§7).
    REST-style fallback key names are accepted for non-WebSocket payloads.
    """
    if not raw_quote:
        return None

    def _div100(*keys):
        for k in keys:
            v = raw_quote.get(k)
            if v is not None:
                try:
                    return float(v) / 100.0
                except (TypeError, ValueError):
                    return None
        return None

    def _raw_int(*keys):
        for k in keys:
            v = raw_quote.get(k)
            if v is not None:
                return v
        return None

    return {
        "source": "ANGEL",
        "token": str(raw_quote.get("token")),
        "ltp": _div100("last_traded_price", "ltp"),
        "open": _div100("open_price_of_the_day", "open"),
        "high": _div100("high_price_of_the_day", "high"),
        "low": _div100("low_price_of_the_day", "low"),
        "close": _div100("closed_price", "close"),
        "volume": _raw_int("volume_trade_for_the_day", "volume"),
        "oi": _raw_int("open_interest", "oi"),
        "timestamp": raw_quote.get("exchange_timestamp"),
        "local_timestamp": raw_quote.get("local_timestamp"),
    }


def normalize_jugaad_option_chain(raw_chain):
    """Normalize NSE NextApi option-chain payload into per-strike CE/PE rows.

    Input: dict as produced by app/data/jugaad.py option_chain_index:
        {"source": "JUGAAD", "symbol": ..., "raw": {...}, "error": None,
         "status": "success"}

    Returns a list of dicts:
        {"source": "JUGAAD", "underlying_value": ..., "expiry": ...,
         "strike": float, "ce": {...}, "pe": {...}}
    Only the nearest expiry is selected (EXPIRY_MODE=current_weekly, §9).
    """
    import logging

    logger = logging.getLogger(__name__)

    if not raw_chain or not raw_chain.get("raw"):
        return []

    raw = raw_chain["raw"]
    if not isinstance(raw, dict):
        logger.warning("normalize_jugaad_option_chain: raw is not a dict")
        return []

    data_rows = raw.get("data")
    if data_rows is None or not isinstance(data_rows, list):
        logger.warning("normalize_jugaad_option_chain: raw['data'] missing or not a list")
        return []

    ce_by_expiry = {}
    pe_by_expiry = {}
    for row in data_rows:
        if not isinstance(row, dict):
            continue
        expiry = row.get("expiryDate")
        strike_raw = row.get("strikePrice")
        option_type = row.get("optionType")
        if not expiry or strike_raw in (None, ""):
            continue
        try:
            strike = float(strike_raw)
        except (TypeError, ValueError):
            logger.warning("normalize_jugaad_option_chain: bad strike %r", strike_raw)
            continue
        side = side_from_row(row)
        if option_type == "CE":
            ce_by_expiry.setdefault(expiry, {})[strike] = side
        elif option_type == "PE":
            pe_by_expiry.setdefault(expiry, {})[strike] = side

    if not ce_by_expiry and not pe_by_expiry:
        logger.warning("normalize_jugaad_option_chain: no option rows found")
        return []

    # Nearest expiry by calendar date across sides (never inferred from text).
    def _expiry_sort_key(expiry):
        try:
            from datetime import datetime
            return datetime.strptime(expiry, "%d-%b-%Y")
        except (TypeError, ValueError):
            return None

    all_expiries = set(ce_by_expiry) | set(pe_by_expiry)
    dated = [(e, _expiry_sort_key(e)) for e in all_expiries]
    dated = [(e, k) for e, k in dated if k is not None]
    if not dated:
        logger.warning("normalize_jugaad_option_chain: no parseable expiry dates")
        return []
    current_expiry = min(dated, key=lambda pair: pair[1])[0]

    ce_strikes = ce_by_expiry.get(current_expiry, {})
    pe_strikes = pe_by_expiry.get(current_expiry, {})

    normalized = []
    underlying_value = None
    for row in data_rows:
        if isinstance(row, dict) and row.get("underlyingValue") is not None:
            try:
                underlying_value = float(row["underlyingValue"])
                break
            except (TypeError, ValueError):
                pass

    for strike in sorted(set(ce_strikes) | set(pe_strikes)):
        normalized.append({
            "source": "JUGAAD",
            "underlying_value": underlying_value,
            "expiry": current_expiry,
            "strike": strike,
            "ce": ce_strikes.get(strike),
            "pe": pe_strikes.get(strike),
        })

    logger.debug(
        "normalize_jugaad_option_chain: %d entries for expiry %s",
        len(normalized), current_expiry,
    )
    return normalized
