import math
from datetime import datetime

import pytz

IST = pytz.timezone("Asia/Kolkata")

RISK_FREE_RATE = 0.06  # documented initial constant; configurable input required for production tuning


def norm_cdf(x):
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def norm_pdf(x):
    return math.exp(-0.5 * x * x) / math.sqrt(2 * math.pi)


def black_scholes_greeks(spot, strike, expiry_dt, option_type, iv, rate=RISK_FREE_RATE):
    """Calculate Greeks from valid inputs only (BLOCKORA §13, §22).

    IV is expected in percent (e.g. 13.82). If IV is missing the function
    returns None: Greeks are CALCULATED data, never fabricated (§7).
    """
    if spot is None or strike is None or iv is None or expiry_dt is None:
        return None
    if strike <= 0 or spot <= 0:
        return None

    now = datetime.now(IST)
    if expiry_dt.tzinfo is None:
        expiry_dt = IST.localize(expiry_dt)
    t = (expiry_dt - now).total_seconds() / (365.0 * 24 * 3600)
    if t <= 0:
        return None

    sigma = iv / 100.0
    if sigma <= 0:
        return None

    d1 = (math.log(spot / strike) + (rate + 0.5 * sigma ** 2) * t) / (sigma * math.sqrt(t))
    d2 = d1 - sigma * math.sqrt(t)

    if option_type == "CE":
        delta = norm_cdf(d1)
        theta = (
            -(spot * norm_pdf(d1) * sigma) / (2 * math.sqrt(t))
            - rate * strike * math.exp(-rate * t) * norm_cdf(d2)
        )
    elif option_type == "PE":
        delta = norm_cdf(d1) - 1
        theta = (
            -(spot * norm_pdf(d1) * sigma) / (2 * math.sqrt(t))
            + rate * strike * math.exp(-rate * t) * norm_cdf(-d2)
        )
    else:
        return None

    gamma = norm_pdf(d1) / (spot * sigma * math.sqrt(t))
    vega = spot * norm_pdf(d1) * math.sqrt(t) / 100.0

    return {
        "delta": round(delta, 6),
        "gamma": round(gamma, 8),
        "theta": round(theta, 4),
        "vega": round(vega, 4),
        "iv": iv,
        "greeks_method": "BLACK_SCHOLES",
        "greeks_status": "CALCULATED",
    }
