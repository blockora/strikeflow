import time
from datetime import datetime

import pytz

IST = pytz.timezone("Asia/Kolkata")


def next_minute_boundary():
    now = datetime.now(IST)
    next_minute = now.replace(second=0, microsecond=0)
    next_minute = next_minute.timestamp() + 60
    return next_minute


def sleep_until_next_cycle():
    target = next_minute_boundary()
    while True:
        remaining = target - time.time()
        if remaining <= 0:
            break
        time.sleep(min(1, remaining))


def market_session_state(cfg):
    now = datetime.now(IST).strftime("%H:%M")
    if cfg.PRE_OPEN_START <= now < cfg.MARKET_OPEN:
        return "PRE_OPEN"
    if cfg.MARKET_OPEN <= now <= cfg.MARKET_CLOSE:
        return "MARKET_OPEN"
    if cfg.MARKET_CLOSE < now <= cfg.POST_CLOSE_END:
        return "POST_CLOSE"
    return "MARKET_CLOSED"
