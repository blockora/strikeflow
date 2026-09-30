from datetime import datetime

import logging
import pytz

logger = logging.getLogger(__name__)

IST = pytz.timezone("Asia/Kolkata")


def now_ist():
    return datetime.now(IST)


def normalize_angel_quote(raw_quote):
    if not raw_quote:
        return None

    ltp = raw_quote.get("ltp")
    if ltp is not None:
        ltp = ltp / 100.0

    return {
        "source": "ANGEL",
        "token": str(raw_quote.get("token")),
        "ltp": ltp,
        "open": _safe_div100(raw_quote.get("open")),
        "high": _safe_div100(raw_quote.get("high")),
        "low": _safe_div100(raw_quote.get("low")),
        "close": _safe_div100(raw_quote.get("close")),
        "timestamp": raw_quote.get("exchange_timestamp"),
        "local_timestamp": raw_quote.get("local_timestamp"),
    }


def normalize_jugaad_option_chain(raw_chain):
    """Normalize NSE option chain data from the Jugaad/NSE NextApi endpoint.

    Expected raw_chain format (from app/data/jugaad.py option_chain_index):
    {
        "source": "JUGAAD",
        "symbol": "NIFTY",
        "raw": {
            "data": [...option rows...],
            "timestamp": "...",
            ...
        },
        "error": None,
        "status": "success",
    }

    Each option row has fields like:
        identifier, instrumentType, optionType, strikePrice, expiryDate,
        lastPrice, pchange, totalTradedVolume, openInterest,
        changeinOpenInterest, underlyingValue, etc.

    Returns a list of normalized dicts with keys:
        source, underlying_value, expiry, strike, ce, pe
    """
    if not raw_chain or not raw_chain.get("raw"):
        return []

    raw = raw_chain["raw"]

    # Validate that raw is a dict
    if not isinstance(raw, dict):
        logger.warning("normalize_jugaad_option_chain: raw is not a dict")
        return []

    # Extract the data rows
    data_rows = raw.get("data")
    if data_rows is None:
        logger.warning("normalize_jugaad_option_chain: raw missing 'data' field")
        return []

    if not isinstance(data_rows, list):
        logger.warning("normalize_jugaad_option_chain: raw['data'] is not a list")
        return []

    if len(data_rows) == 0:
        logger.debug("normalize_jugaad_option_chain: no data rows in option chain")
        return []

# Separate CE and PE rows, grouped by expiry date
    ce_by_expiry = {}  # expiryDate -> list of CE rows
    pe_by_expiry = {}  # expiryDate -> list of PE rows
    all_expiry_dates = set()

    for row in data_rows:
        if not isinstance(row, dict):
            logger.debug("normalize_jugaad_option_chain: skipping row, not a dict")
            continue

        expiry = row.get("expiryDate")
        strike = row.get("strikePrice")  # string from API like "23500.00"
        option_type = row.get("optionType")  # "CE" or "PE"

        if not expiry or not strike:
            continue

        # Convert strike to numeric value
        try:
            strike_numeric = float(strike)
        except (TypeError, ValueError):
            logger.warning(f"normalize_jugaad_option_chain: cannot convert strike '{strike}' to float")
            strike_numeric = None

        all_expiry_dates.add(expiry)

        # Extract price data from the row
        price_data = {
            "lastPrice": row.get("lastPrice"),
            "pchange": row.get("pchange"),
            "totalTradedVolume": row.get("totalTradedVolume"),
            "openInterest": row.get("openInterest"),
            "changeinOpenInterest": row.get("changeinOpenInterest"),
        }

        if option_type == "CE":
            if expiry not in ce_by_expiry:
                ce_by_expiry[expiry] = []
            ce_by_expiry[expiry].append({"strike": strike_numeric, **price_data})
        elif option_type == "PE":
            if expiry not in pe_by_expiry:
                pe_by_expiry[expiry] = []
            pe_by_expiry[expiry].append({"strike": strike_numeric, **price_data})

    # Current expiry is the first one found
    current_expiry = None
    for expiry in sorted(all_expiry_dates):
        current_expiry = expiry
        break

    if current_expiry is None:
        logger.warning("normalize_jugaad_option_chain: no expiry dates found")
        return []

    # Get CE and PE rows for the current expiry
    ce_rows = ce_by_expiry.get(current_expiry, [])
    pe_rows = pe_by_expiry.get(current_expiry, [])

    # Build a lookup of PE data by strike price (numeric)
    pe_by_strike = {}
    for pe_row in pe_rows:
        pe_by_strike[pe_row["strike"]] = pe_row

    # Create normalized entries: for each CE, find matching PE by strike
    normalized = []
    for ce_row in ce_rows:
        strike = ce_row["strike"]  # already numeric (float)

        # Find matching PE
        pe_data = pe_by_strike.get(strike)

        normalized_ce = {
            "ltp": ce_row.get("lastPrice"),
            "bid": ce_row.get("bidprice"),
            "ask": ce_row.get("askPrice"),
            "bid_qty": ce_row.get("bidQty"),
            "ask_qty": ce_row.get("askQty"),
            "volume": ce_row.get("totalTradedVolume"),
            "oi": ce_row.get("openInterest"),
            "oi_change": ce_row.get("changeinOpenInterest"),
            "iv": ce_row.get("impliedVolatility"),
            "prev_close": ce_row.get("prevClose"),
            "change": ce_row.get("change"),
            "pchange": ce_row.get("pchange"),
        }

        normalized_pe = {}
        if pe_data:
            normalized_pe = {
                "ltp": pe_data.get("lastPrice"),
                "bid": pe_data.get("bidprice"),
                "ask": pe_data.get("askPrice"),
                "bid_qty": pe_data.get("bidQty"),
                "ask_qty": pe_data.get("askQty"),
                "volume": pe_data.get("totalTradedVolume"),
                "oi": pe_data.get("openInterest"),
                "oi_change": pe_data.get("changeinOpenInterest"),
                "iv": pe_data.get("impliedVolatility"),
                "prev_close": pe_data.get("prevClose"),
                "change": pe_data.get("change"),
                "pchange": pe_data.get("pchange"),
            }

        normalized.append({
            "source": "JUGAAD",
            "underlying_value": raw.get("underlyingValue"),
            "expiry": current_expiry,
            "strike": strike,
            "ce": normalized_ce,
            "pe": normalized_pe,
        })

    logger.debug(
        f"normalize_jugaad_option_chain: {len(normalized)} entries "
        f"normalized for expiry {current_expiry}"
    )
    return normalized


def _normalize_oc_side(side):
    return {
        "ltp": side.get("lastPrice"),
        "bid": side.get("bidprice"),
        "ask": side.get("askPrice"),
        "bid_qty": side.get("bidQty"),
        "ask_qty": side.get("askQty"),
        "volume": side.get("totalTradedVolume"),
        "oi": side.get("openInterest"),
        "oi_change": side.get("changeinOpenInterest"),
        "iv": side.get("impliedVolatility"),
        "prev_close": side.get("prevClose"),
        "change": side.get("change"),
        "pchange": side.get("pChange"),
    }


def _safe_div100(value):
    if value is None:
        return None
    try:
        return float(value) / 100.0
    except Exception:
        return None