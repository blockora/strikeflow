import time
from datetime import datetime

import pytz

IST = pytz.timezone("Asia/Kolkata")


def next_minute_boundary():
    """Return the epoch timestamp of the next minute boundary (IST wall clock)."""
    now = datetime.now(IST)
    next_minute = now.replace(second=0, microsecond=0)
    return next_minute.timestamp() + 60


def sleep_until_next_cycle():
    """Sleep until the next minute boundary (BLOCKORA §30).

    No blind sleep(60): sleeps in 1-second slices until the boundary so the
    analysis always fires on the synchronized minute and never drifts, while
    avoiding a busy CPU-spinning loop on mobile hardware.
    """
    target = next_minute_boundary()
    while True:
        remaining = target - time.time()
        if remaining <= 0:
            break
        time.sleep(min(1.0, remaining))


def market_session_state(cfg):
    """Classify the current IST time into session states (BLOCKORA §69)."""
    now = datetime.now(IST).strftime("%H:%M")
    if cfg.PRE_OPEN_START <= now < cfg.MARKET_OPEN:
        return "PRE_OPEN"
    if cfg.MARKET_OPEN <= now <= cfg.MARKET_CLOSE:
        return "MARKET_OPEN"
    if cfg.MARKET_CLOSE < now <= cfg.POST_CLOSE_END:
        return "POST_CLOSE"
    return "MARKET_CLOSED"
