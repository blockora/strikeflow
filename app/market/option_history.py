"""Option Engine — bounded per-contract history (BLOCKORA §13, §17, §18,
§19, §23, Phase 5).

The UnderlyingEngine keeps a bounded history of spot quotes; nothing in the
codebase kept one for option contracts, so §17 momentum, §19 relative volume
and §23 IV change had no real data to work from. This module supplies that
missing layer: one short, bounded, per-contract time series fed from the
already-normalized option chain once per cycle.

Design constraints (BLOCKORA §49, §75 — Android 14 / Termux):
  * every series is a fixed-length deque, so memory is bounded by
    MAX_CONTRACTS x MAX_POINTS regardless of how many strikes the chain has;
  * no unbounded growth, no pandas/dataframe, no ML, nothing heavy.

Data rules (BLOCKORA §7, §8, §46):
  * ONLY values that are actually present in the normalized chain are
    recorded. A missing field is stored as None and stays None downstream.
  * provenance is preserved: the value ingested is the chain's own `oi`
    (JUGAAD/NSE per BLOCKORA §5). Angel's `angel_oi` is recorded separately
    and is never substituted for a valid chain OI.
  * nothing is estimated, interpolated or carried forward.
  * every derived metric returns None when its inputs are unavailable, so a
    caller can degrade that factor safely instead of scoring a false positive.
"""

from __future__ import annotations

import math
import time
from collections import deque

from app.config import config
from app.market.indicators import pct_change

# Contract-history bounds. 48 contracts x 90 one-per-cycle samples is a few
# hundred kilobytes worst case: trivial for a phone.
MAX_CONTRACTS = config.OPTION_HISTORY_MAX_CONTRACTS
MAX_POINTS = config.OPTION_HISTORY_MAX_POINTS

_VOLUME_AVG_POINTS = config.OPTION_VOLUME_AVG_POINTS
_IV_HISTORY_POINTS = config.OPTION_IV_HISTORY_POINTS


def _finite(value):
    """float(value) only when it is a real finite number, else None."""
    if value is None:
        return None
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(v) or math.isinf(v):
        return None
    return v


def contract_key(expiry, strike, option_type):
    """Stable identity for one contract: expiry + strike + option type."""
    return "{}|{}|{}".format(expiry, strike, option_type)


class OptionHistoryEngine:
    """Bounded, per-contract history of real observed option fields."""

    def __init__(self, max_contracts=None, max_points=None):
        self._max_contracts = max_contracts or MAX_CONTRACTS
        self._max_points = max_points or MAX_POINTS
        # key -> deque[(ts, ltp, volume, oi, iv, oi_source)]
        self._series = {}
        # key -> last ingest wall-clock, for bounded eviction (LRU)
        self._last_seen = {}

    # ------------------------------------------------------------- ingestion

    def ingest(self, chain, now=None):
        """Record one observation per contract from a normalized chain.

        `chain` is the output of OptionChainEngine.get_normalized_chain(): a
        list of {"expiry", "strike", "ce": {...}, "pe": {...}} rows. Missing
        sides are skipped; a side with no LTP is recorded with None fields so
        the contract's later observations are still timestamped consistently.

        `now` defaults to real wall time and may be injected by replay/tests.
        """
        if not chain:
            return 0
        now = now if now is not None else time.time()

        recorded = 0
        for row in chain:
            if not isinstance(row, dict):
                continue
            expiry = row.get("expiry")
            strike = row.get("strike")
            if expiry is None or strike is None:
                continue
            for side in ("CE", "PE"):
                data = row.get(side.lower())
                if not isinstance(data, dict):
                    continue
                key = contract_key(expiry, strike, side)
                series = self._series.get(key)
                if series is None:
                    if len(self._series) >= self._max_contracts:
                        if not self._evict_one():
                            continue
                    series = deque(maxlen=self._max_points)
                    self._series[key] = series
                # Chain `oi` is the primary (Jugaad/NSE) value. `angel_oi` is
                # deliberately NOT read here: BLOCKORA §8 forbids silently
                # replacing one source with another, and the candidate's `oi`
                # field already carries the chain value with its provenance.
                series.append((
                    now,
                    _finite(data.get("ltp")),
                    _finite(data.get("volume")),
                    _finite(data.get("oi")),
                    _finite(data.get("iv")),
                    data.get("source"),
                ))
                self._last_seen[key] = now
                recorded += 1
        return recorded

    def _evict_one(self):
        """Drop the least-recently-ingested contract. Keeps memory bounded."""
        if not self._last_seen:
            return False
        oldest = min(self._last_seen, key=lambda k: self._last_seen[k])
        self._series.pop(oldest, None)
        self._last_seen.pop(oldest, None)
        return True

    # -------------------------------------------------------------- queries

    def tracked_contracts(self):
        return len(self._series)

    def has_history(self, key):
        return key in self._series

    def _anchor(self, series, now, seconds):
        """Latest past sample covered by >= `seconds` of wall time.

        Mirrors UnderlyingEngine._lookback: the anchor must be a real earlier
        observation, never the current one, and never an observation stamped at
        or after the decision time (`now`), so no look-ahead can enter a
        decision. Returns None when coverage is insufficient rather than
        borrowing a sample by index.
        """
        if not series:
            return None
        newest = series[-1][0]
        if newest is None:
            return None
        anchor = None
        for point in series:
            ts = point[0]
            if ts is None or ts >= newest:
                continue
            if now is not None and ts >= now:
                continue
            if newest - ts >= seconds:
                anchor = point
        return anchor

    def metrics(self, key, now=None):
        """All derivable evidence for one contract.

        Returns a dict whose keys are None whenever the underlying history is
        insufficient — never a zero-substitute that would read as real data.
        """
        series = self._series.get(key)
        empty = {
            "opt_return_1m": None,
            "opt_return_3m": None,
            "opt_return_5m": None,
            "opt_acceleration": None,
            "option_samples": 0,
            "volume_avg": None,
            "relative_volume": None,
            "volume_acceleration": None,
            "prev_volume": None,
            "oi_history": None,
            "prev_oi": None,
            "oi_change_pct": None,
            "iv_history": None,
            "iv_change": None,
            "iv_percentile": None,
            "iv_samples": 0,
            "oi_source": None,
        }
        if not series or len(series) < 2:
            empty["option_samples"] = len(series) if series else 0
            if series:
                empty["oi_source"] = series[-1][5]
            return empty

        newest = series[-1]
        ts_now, ltp, volume, oi, iv, oi_source = newest

        out = dict(empty)
        out["option_samples"] = len(series)
        out["oi_source"] = oi_source

        # ---- §17 option premium returns (wall-clock, no look-ahead) ------
        for label, seconds in (("1m", 60.0), ("3m", 180.0), ("5m", 300.0)):
            point = self._anchor(series, now, seconds)
            if point is not None and point[1] not in (None, 0) and ltp is not None:
                out["opt_return_{}".format(label)] = pct_change(ltp, point[1])

        # Momentum acceleration: the short-horizon rate of change versus the
        # longer-horizon rate. Positive => premium is speeding up.
        r1 = out["opt_return_1m"]
        r3 = out["opt_return_3m"]
        if r1 is not None and r3 is not None:
            out["opt_acceleration"] = r1 - (r3 / 3.0)

        # ---- §19 relative volume -----------------------------------------
        prior_volumes = [
            p[2] for p in list(series)[:-1]
            if p[2] is not None and p[2] >= 0
        ][-_VOLUME_AVG_POINTS:]
        if len(prior_volumes) >= config.OPTION_MIN_AVG_SAMPLES and volume is not None and volume >= 0:
            average = sum(prior_volumes) / len(prior_volumes)
            out["volume_avg"] = average
            # Zero denominator must not become an infinite relative volume.
            if average > 0:
                out["relative_volume"] = volume / average
            prev = prior_volumes[-1] if prior_volumes else None
            out["prev_volume"] = prev
            if prev is not None and prev > 0:
                out["volume_acceleration"] = volume / prev

        # ---- §18 previous OI / OI change % -------------------------------
        prior_oi = [p[3] for p in list(series)[:-1] if p[3] is not None and p[3] >= 0]
        if prior_oi:
            out["oi_history"] = prior_oi[-1]
            previous = prior_oi[-1]
            out["prev_oi"] = previous
            if oi is not None and previous > 0:
                out["oi_change_pct"] = ((oi - previous) / previous) * 100.0

        # ---- §23 IV change and percentile --------------------------------
        prior_ivs = [p[4] for p in list(series)[:-1] if p[4] is not None and p[4] > 0]
        if prior_ivs:
            out["iv_history"] = prior_ivs[-1]
            if iv is not None and prior_ivs[-1] > 0:
                out["iv_change"] = iv - prior_ivs[-1]
            window = prior_ivs[-_IV_HISTORY_POINTS:]
            out["iv_samples"] = len(window)
            # A percentile may only be stated once enough REAL observations
            # exist (BLOCKORA §23, §65). Below the minimum it stays None so no
            # rank can be invented from a handful of samples.
            if iv is not None and len(window) >= config.IV_PERCENTILE_MIN_SAMPLES:
                below = sum(1 for v in window if v < iv)
                equal = sum(1 for v in window if v == iv)
                out["iv_percentile"] = round(((below + 0.5 * equal) / len(window)) * 100.0, 2)

        return out

    def snapshot_for(self, candidate, now=None):
        """metrics() keyed off a candidate dict's expiry/strike/type."""
        key = contract_key(
            candidate.get("expiry"),
            candidate.get("strike"),
            candidate.get("option_type"),
        )
        out = self.metrics(key, now=now)
        out["contract_key"] = key
        return out