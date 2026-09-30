import math


def safe_div(a, b):
    if a is None or b in (None, 0):
        return None
    return a / b


def pct_change(current, previous):
    if current is None or previous in (None, 0):
        return None
    return ((current - previous) / previous) * 100.0


def sma(values, period):
    vals = [v for v in values if v is not None]
    if len(vals) < period:
        return None
    return sum(vals[-period:]) / period


def ema(values, period):
    vals = [v for v in values if v is not None]
    if len(vals) < period:
        return None
    k = 2 / (period + 1)
    e = sum(vals[:period]) / period
    for v in vals[period:]:
        e = v * k + e * (1 - k)
    return e


def vwap(closes, highs, lows, volumes):
    num = 0.0
    den = 0.0
    for h, l, c, v in zip(highs, lows, closes, volumes):
        if None in (h, l, c, v):
            continue
        tp = (h + l + c) / 3.0
        num += tp * v
        den += v
    if den == 0:
        return None
    return num / den


def true_range(high, low, prev_close):
    if high is None or low is None:
        return None
    if prev_close is None:
        return high - low
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return None
    trs = []
    for i in range(1, len(closes)):
        trs.append(true_range(highs[i], lows[i], closes[i - 1]))
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period


def realized_volatility(returns):
    vals = [r for r in returns if r is not None]
    if len(vals) < 2:
        return None
    mean = sum(vals) / len(vals)
    var = sum((x - mean) ** 2 for x in vals) / (len(vals) - 1)
    return math.sqrt(var)
