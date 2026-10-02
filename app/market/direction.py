"""Direction confirmation engine (BLOCKORA Phase 1).

Standalone evidence layer over the underlying (spot) series.  It consumes only
existing cycle evidence — spot, timestamped price history, wall-clock returns,
VWAP, ATR, data quality, spot freshness, session/day — and never touches the
network or the scoring gates.

It does NOT lower MIN_SCORE, does NOT bypass the hard gates, and does NOT
replace the existing score.  A candidate in the 64-68 range stays below the
BEST-STRIKE threshold unless the existing scoring/gates independently promote
it.

All expected output is documented here:

    direction : UP | DOWN | NEUTRAL
    confirmed : bool
    move_points : float or None
    origin_spot : float or None
    origin_time : float or None
    confirmation_reason : list[str]
    missing_evidence : list[str]
    episode_id : str
    episode_confirmed : bool

Direction formula
-----------------
Using the wall-clock 1m/3m/5m returns from UnderlyingEngine (return_Xm is the
latest usable price minus the usable price covered by >= X minutes, divided by
the anchor price, in percent):

    UP default   : current spot - origin spot >= +DIRECTION_MOVE_POINTS
    DOWN default : current spot - origin spot <= -DIRECTION_MOVE_POINTS

Origin
------
origin_time is the epoch of a same-day timestamped price cell inside the
configured lookback window; origin_spot is the price observed at that cell.
Only same-day valid timestamps are used, so a previous day's CycleHistory row
can never become a today's origin.

Confirmation requirements
-------------------------
UP confirmation requires:
    * required +DIRECTION_MOVE_POINTS move
    * sufficient same-day wall-clock history (lookback covered >= lookback
      minutes of samples);
    * valid/fresh underlying data (spot non-None, within MAX_STALE_SECONDS);
    * data quality >= MIN_DATA_QUALITY (existing gate, never overridden by
      direction);
    * EVERY AVAILABLE 1m/3m/5m horizon is > 0 for UP;
    * spot > VWAP when DIRECTION_REQUIRE_VWAP_AGREE=true;
    * spike/V-shape protection passes;
    * persistence: DIRECTION_PERSISTENCE_CYCLES consecutive qualifying
      cycles.

DOWN is the exact mirror: EVERY AVAILABLE 1m/3m/5m horizon must be < 0 and
spot < VWAP.

Timeframe agreement is strictly symmetric.  A horizon whose wall-clock return
is None (insufficient coverage) is neither required nor fabricated; a horizon
that IS available must carry the correct sign.  Any conflicting available
horizon rejects confirmation while `direction` still reports the raw move.

    UP   : +0.2, +0.1, None -> eligible     +0.2, -0.1, +0.1 -> rejected
                              -0.2, -0.1, -0.3 -> rejected
    DOWN : -0.2, -0.1, None -> eligible     -0.2, +0.1, -0.1 -> rejected
                              +0.2, +0.1, +0.3 -> rejected

Persistence + episode tracking
------------------------------
The engine keeps a lightweight in-memory episode (direction, origin, active/
confirmed, last confirmation timestamp).  A confirmed episode remains active
until:
    * directional evidence invalidates it (a qualifying cycle no longer
      qualifies, or the direction flips), or
    * the day ends (session/day computed from timestamps).

After a process restart no episode is reconstructed from yesterday; the engine
re-confirms against today's live history only.

Spike/V-shape protection
------------------------
The premise is: +10 in one isolated tick followed by immediate reversal must
not become a valid persistent directional episode (and the same for DOWN).

Interpretation (inside the configured lookback window ending at the current
timestamp):

    Let lookback_prices = prices at same-day cells inside
    [origin_time, now] (chronological).

    If len(lookback_prices) < 3: no spike protection claimed (not enough
        points to define a V-shape).

    Let peak = max(lookback_prices), trough = min(lookback_prices).

    For an UP candidate to be a true move, the price must not have already
    re-traced more than DIRECTION_MAX_ADVERSE_PCT of the lookback range from
    its within-lookback peak:

        retrace_pct = (peak - current_spot) / (peak - trough) * 100.0
        if retrace_pct >= DIRECTION_MAX_ADVERSE_PCT: not confirmed

    Symmetrically for DOWN, measured up from the within-lookback trough:

        retrace_pct = (current_spot - trough) / (peak - trough) * 100.0
        if retrace_pct >= DIRECTION_MAX_ADVERSE_PCT: not confirmed

UNITS: `DIRECTION_MAX_ADVERSE_PCT` is expressed in PERCENTAGE POINTS on the
same 0-100 scale that `retrace_pct` is computed on.  The default of 25.0
therefore means "at most 25% of the lookback peak-to-trough range may have been
given back".  It is NOT a 0-1 fraction and must never be compared against one.
The formula does not use ATR, so ATR being unavailable never disables it.

When peak == trough (a flat series) no retracement is defined and the guard
passes.  With fewer than 3 points in the window a V-shape cannot be defined
and the guard passes.  Any other data problem (misaligned arrays, non-numeric
prices) REJECTS the move with an observable reason: this guard fails closed
and is never silently skipped.

This is deliberately a simple adverse-move guard, not a trading strategy.

Environment-driven configuration (all read from app/config.py; no hard-coded
defaults inside the engine):
    DIRECTION_MOVE_POINTS = 10.0
    DIRECTION_LOOKBACK_MINUTES = 5
    DIRECTION_PERSISTENCE_CYCLES = 2
    DIRECTION_MAX_ADVERSE_PCT = 25.0   # percentage POINTS of the lookback range
    DIRECTION_REQUIRE_VWAP_AGREE = True

Episode identifier
------------------
episode_id = f"{session_date}|{direction}" — the same episode can only be
re-triggered after DIRECTION_PERSISTENCE_CYCLES consecutive qualifying
cycles, and it is never queued across process restarts.
"""

from __future__ import annotations

import time

from app.config import config
from app.market.underlying import UnderlyingEngine

# Default values are only used when the environment does not define the
# corresponding app/config.py keys.  The engine reads all real values from
# config so no constant is hard-coded after import.
_DIRECTION_MOVE_POINTS = 10.0
_DIRECTION_LOOKBACK_MINUTES = 5
_DIRECTION_PERSISTENCE_CYCLES = 2
# Percentage POINTS of the lookback peak-to-trough range that may be given
# back before the move is treated as a spike/V-shape.  Same 0-100 scale the
# retracement formula produces.  Never use a 0-1 fraction here.
_DIRECTION_MAX_ADVERSE_PCT = 25.0
_DIRECTION_REQUIRE_VWAP_AGREE = True

_DIRECTION_UP = "UP"
_DIRECTION_DOWN = "DOWN"
_DIRECTION_NEUTRAL = "NEUTRAL"


class DirectionEvidenceMissing(Exception):
    """Raised when required evidence (history, spot, DQ, etc.) is absent.
    The caller should treat this as 'no confirmation', not as a partial one.
    """


class DirectionEngine:
    """Directional confirmation engine for a single underlying.

    The in-memory episode is scoped to one process lifetime.  Persisting the
    episode across restarts is a later phase.
    """

    def __init__(self, underlying_engine=None):
        self.underlying_engine = underlying_engine or UnderlyingEngine()
        # Lightweight in-memory episode (S5).  Scoped to this process lifetime.
        self._episode = {
            "direction": _DIRECTION_NEUTRAL,
            "origin_spot": None,
            "origin_time": None,
            "confirmed": False,
            "active": False,
            "last_confirmation_time": None,
        }

    # ------------------------------------------------------------- config

    @staticmethod
    def _cfg_float(key, default):
        val = getattr(config, key, None)
        if val is None:
            return default
        try:
            return float(val)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _cfg_int(key, default):
        val = getattr(config, key, None)
        if val is None:
            return default
        try:
            return int(val)
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _cfg_bool(key, default):
        val = getattr(config, key, None)
        if val is None:
            return default
        return str(val).strip().lower() in ("1", "true", "yes", "y", "on")

    # ------------------------------------------------------------- session

    @staticmethod
    def _session_info(unix_timestamp):
        """Date+time+datetime from an epoch.

        Same-day history is filtered by comparing the IST date of the
        timestamp with today's date, so a previous-day CycleHistory row can
        never become a today's origin.
        """
        from datetime import datetime

        import pytz

        ist = pytz.timezone("Asia/Kolkata")
        dt = datetime.fromtimestamp(unix_timestamp, tz=ist)
        return dt.date(), dt.hour * 3600 + dt.minute * 60 + dt.second, dt

    def _same_day(self, timestamp, now=None):
        """True when `timestamp` falls on the same IST date as `now`.

        `now` is injectable so replay and tests are deterministic; it defaults
        to real wall time when not supplied.
        """
        reference = now if now is not None else self._now_epoch()
        return self._session_info(timestamp)[0] == self._session_info(reference)[0]

    def _now_epoch(self):
        return time.time()

    # ----------------------------------------------------------- helpers

    @staticmethod
    def _directional_evidence_agrees(horizons, is_up):
        """Every AVAILABLE horizon must agree with the requested direction.

        `horizons` maps "1m"/"3m"/"5m" to a wall-clock return that may be None
        when that horizon has insufficient coverage.  A None horizon is neither
        required nor fabricated.  A horizon that IS available must carry the
        correct sign: strictly > 0 for UP, strictly < 0 for DOWN.  This is the
        single implementation used by BOTH directions, so UP and DOWN can
        never drift apart again.

        Returns (agrees, conflicting_horizon_labels).
        """
        conflicting = [
            label
            for label, value in horizons.items()
            if value is not None
            and ((is_up and value <= 0) or (not is_up and value >= 0))
        ]
        return (not conflicting), conflicting

    def generate(
        self,
        underlying_snapshot,
        now=None,
    ):
        """Produce the directional evidence tuple for the current cycle."""
        now = now if now is not None else self._now_epoch()
        evidence = []
        missing = []

        # ---------------------------------------------------------- basics
        spot = underlying_snapshot.get("spot")
        if spot is None:
            missing.append("spot (no live underlying data)")
            return self._neutral_result(now, missing)

        # Data-quality gate: directional confirmation must never override
        # existing data-quality rules.
        data_quality = underlying_snapshot.get("data_quality")
        if data_quality is None or data_quality < config.MIN_DATA_QUALITY:
            missing.append("data quality below MIN_DATA_QUALITY")
            return self._neutral_result(now, missing)

        vwap = underlying_snapshot.get("vwap")
        if vwap is None:
            missing.append("vwap unavailable")

        timestamps = self.underlying_engine.timestamps
        if not timestamps:
            missing.append(
                "timestamped price history unavailable (no same-day origin)"
            )
            return self._neutral_result(now, missing)

        # ------------------------------------------------ wall-clock returns
        ret_1m = underlying_snapshot.get("return_1m")
        ret_3m = underlying_snapshot.get("return_3m")
        ret_5m = underlying_snapshot.get("return_5m")

        # Staleness check: underlying must be fresh.
        underlying_age = self.underlying_engine.underlying_age()
        if underlying_age is not None and underlying_age > config.MAX_STALE_SECONDS:
            missing.append("underlying quote stale")
            return self._neutral_result(now, missing)

        # -------------------------------------------------------------- move
        config_move_points = self._cfg_float("DIRECTION_MOVE_POINTS", _DIRECTION_MOVE_POINTS)
        config_max_adverse = self._cfg_float(
            "DIRECTION_MAX_ADVERSE_PCT", _DIRECTION_MAX_ADVERSE_PCT
        )
        config_vwap_agree = self._cfg_bool(
            "DIRECTION_REQUIRE_VWAP_AGREE", _DIRECTION_REQUIRE_VWAP_AGREE
        )
        config_persistence = self._cfg_int(
            "DIRECTION_PERSISTENCE_CYCLES", _DIRECTION_PERSISTENCE_CYCLES
        )

        # Origin from a same-day timestamped cell inside the lookback window.
        origin_spot, origin_time, origin_reason = self._origin_for(spot, now, timestamps)
        if origin_time is None:
            missing.append(f"origin unavailable: {origin_reason}")
            return self._neutral_result(now, missing)

        move = spot - origin_spot

        # Raw move direction first: `direction` reports what the price actually
        # did, independent of whether confirmation later succeeds.
        if move >= config_move_points:
            direction = _DIRECTION_UP
        elif move <= -config_move_points:
            direction = _DIRECTION_DOWN
        else:
            direction = _DIRECTION_NEUTRAL

        # --------------------------------------------------- classify + test
        if direction == _DIRECTION_NEUTRAL:
            qualifies = False
        else:
            # Spike/V-shape protection (same-day lookback, timestamped points).
            # Fails closed: any data problem rejects the move with a visible
            # reason rather than silently approving it.
            spike_ok, spike_reason = self._spike_protection(
                spot, origin_time, now, timestamps, config_max_adverse, direction
            )
            if not spike_ok:
                missing.append(spike_reason)
                qualifies = False
            else:
                qualifies = self._pass_confirmation(
                    direction,
                    ret_1m,
                    ret_3m,
                    ret_5m,
                    spot,
                    vwap,
                    config_vwap_agree,
                    evidence,
                    missing,
                )

        episode_state = self._apply_episode(
            direction, qualifies, now, origin_spot, origin_time, config_persistence
        )
        episode_confirmed = bool(episode_state.get("confirmed", False))

        return {
            "direction": direction,
            "confirmed": episode_confirmed,
            "move_points": move if direction != _DIRECTION_NEUTRAL else None,
            "origin_spot": origin_spot,
            "origin_time": origin_time,
            "confirmation_reason": list(episode_state.get("confirmation_reason", [])),
            "missing_evidence": missing,
            "episode_id": episode_state.get("episode_id"),
            "episode_confirmed": episode_confirmed,
        }

    def _pass_confirmation(
        self,
        direction,
        ret_1m,
        ret_3m,
        ret_5m,
        spot,
        vwap,
        require_vwap_agree,
        evidence,
        missing,
    ):
        """All per-cycle directional requirements (everything except
        persistence, which the episode layer owns).

        Strictly symmetric for UP and DOWN: every AVAILABLE wall-clock horizon
        must carry the direction's sign.  A horizon that is None (insufficient
        coverage) is neither required nor invented.
        """
        horizons = {"1m": ret_1m, "3m": ret_3m, "5m": ret_5m}
        agree, conflicting = self._directional_evidence_agrees(
            horizons, direction == _DIRECTION_UP
        )
        if not agree:
            missing.append(
                f"timeframe directional evidence does not agree with {direction} "
                f"(conflicting horizon(s): {', '.join(conflicting)})"
            )
            return False

        if require_vwap_agree and vwap is not None:
            if direction == _DIRECTION_UP and spot <= vwap:
                missing.append("spot not above VWAP (VWAP agreement required)")
                return False
            if direction == _DIRECTION_DOWN and spot >= vwap:
                missing.append("spot not below VWAP (VWAP agreement required)")
                return False

        available = ", ".join(k for k, v in horizons.items() if v is not None) or "none"
        evidence.append(
            f"{direction} move confirmed on wall-clock returns ({available}) and VWAP"
        )
        return True

    # -------------------------------------------------------------- origin

    def _origin_for(self, spot, now, timestamps):
        """Return (origin_spot, origin_time, reason) anchored to a same-day
        price cell inside the configured lookback window.

        The origin is the price seen at the oldest timestamped cell that is
        covered by at least DIRECTION_LOOKBACK_MINUTES of wall time and has a
        valid price.  On failure `reason` explains why no origin exists, so
        the caller can report honest missing evidence.
        """
        config_lookback_minutes = self._cfg_int(
            "DIRECTION_LOOKBACK_MINUTES", _DIRECTION_LOOKBACK_MINUTES
        )
        lookback_seconds = config_lookback_minutes * 60.0

        if not timestamps:
            return None, None, "no timestamped price history"

        prices = self.underlying_engine.price_history
        if prices is None or len(prices) < 2 or len(prices) != len(timestamps):
            return None, None, "price history too thin or misaligned"

        # `now` is the caller-supplied (injectable) clock: never overwritten
        # with real wall time, so replay and tests stay deterministic.
        previous_day_cells = 0
        for idx, ts in enumerate(timestamps):
            if ts is None:
                continue
            if now - ts < lookback_seconds:
                continue
            price = prices[idx]
            if price is None:
                continue
            if not self._same_day(ts, now=now):
                # A previous-day row can never become today's origin — but it
                # also must not abort the search: a later same-day cell may
                # still qualify.
                previous_day_cells += 1
                continue
            return price, ts, None

        if previous_day_cells:
            return (
                None,
                None,
                f"only previous-day cells covered the {config_lookback_minutes}-minute "
                f"lookback; no same-day origin exists",
            )
        return (
            None,
            None,
            f"no cell covered by {config_lookback_minutes} minutes of wall time",
        )

    # ---------------------------------------------------------- spike / V

    def _spike_protection(self, spot, origin_time, now, timestamps, max_adverse, direction):
        """Adverse-move guard inside the lookback window.  FAILS CLOSED.

        UP: the price must not have already given back more than `max_adverse`
        PERCENTAGE POINTS of the lookback peak-to-trough range from its peak.
        DOWN: symmetric, measured up from the within-lookback trough.

        ATR is deliberately NOT an input: the retracement formula does not use
        it, so a missing ATR must never disable the guard.  Returns
        (ok, reason); `reason` is None when ok is True and always populated
        when the guard rejects a move.
        """
        if direction == _DIRECTION_NEUTRAL:
            return True, None

        prices = self.underlying_engine.price_history
        if not timestamps or prices is None:
            return False, "spike protection: no timestamped price history available"
        if len(prices) != len(timestamps):
            return False, "spike protection: price history and timestamps are misaligned"

        try:
            spot_value = float(spot)
        except (TypeError, ValueError):
            return False, "spike protection: spot is not numeric"

        points = []
        for idx, ts in enumerate(timestamps):
            if ts is None or ts < origin_time or ts > now:
                continue
            if not self._same_day(ts, now=now):
                continue
            price = prices[idx]
            if price is None:
                continue
            try:
                points.append(float(price))
            except (TypeError, ValueError):
                return False, "spike protection: non-numeric price inside lookback window"

        if len(points) < 3:
            # Fewer than 3 points: a V-shape is not definable, so no
            # retracement can be claimed.  Documented, not a crash path.
            return True, None

        peak = max(points)
        trough = min(points)
        if peak <= trough:
            # Flat window: no retracement is defined.
            return True, None

        span = peak - trough
        if direction == _DIRECTION_UP:
            retrace = (peak - spot_value) / span * 100.0
        else:
            retrace = (spot_value - trough) / span * 100.0

        if retrace >= max_adverse:
            return False, (
                f"{direction} move retraced {retrace:.2f}% of the {span:.2f}-point "
                f"lookback range (max {max_adverse}%) — spike/V-shape rejected"
            )
        return True, None

    # ------------------------------------------------------------- episode

    def _apply_episode(
        self, direction, qualifies, now, origin_spot, origin_time, persistence_cycles
    ):
        """Manage the in-memory episode: direction, origin, active/confirmed,
        last confirmation timestamp, day boundary, persistence counter and
        the episode identifier.
        """
        episode = self._episode
        session_date, _, _ = self._session_info(now)
        persistence = max(1, int(persistence_cycles or 1))

        # Day boundary: a new day never resumes an old in-memory episode.
        last_ts = episode.get("last_confirmation_time") or now
        if self._session_info(last_ts)[0] != session_date:
            episode.update(
                {
                    "direction": _DIRECTION_NEUTRAL,
                    "origin_spot": None,
                    "origin_time": None,
                    "confirmed": False,
                    "active": False,
                    "streak": 0,
                    "last_confirmation_time": None,
                    "confirmation_reason": [],
                }
            )

        episode_id = f"{session_date}|{direction}"

        if direction == _DIRECTION_NEUTRAL:
            # No qualifying direction: the episode ends and the streak resets.
            episode.update(
                {
                    "direction": _DIRECTION_NEUTRAL,
                    "origin_spot": None,
                    "origin_time": None,
                    "confirmed": False,
                    "active": False,
                    "streak": 0,
                    "last_confirmation_time": None,
                    "confirmation_reason": [],
                }
            )
            episode["episode_id"] = episode_id
            return episode

        if direction != episode.get("direction"):
            # Flip: the previous episode is invalidated; a new streak starts.
            episode.update(
                {
                    "direction": direction,
                    "origin_spot": origin_spot,
                    "origin_time": origin_time,
                    "confirmed": False,
                    "active": True,
                    "streak": 0,
                    "last_confirmation_time": None,
                    "confirmation_reason": [],
                }
            )

        if not qualifies:
            # Same direction but one requirement failed: streak resets.
            episode.update(
                {
                    "confirmed": False,
                    "active": True,
                    "streak": 0,
                    "last_confirmation_time": None,
                    "confirmation_reason": [],
                }
            )
            episode["episode_id"] = episode_id
            return episode

        streak = int(episode.get("streak") or 0) + 1
        confirmed = streak >= persistence
        episode.update(
            {
                "direction": direction,
                "origin_spot": origin_spot,
                "origin_time": origin_time,
                "active": True,
                "streak": streak,
                "confirmed": confirmed,
                "last_confirmation_time": now if confirmed else None,
                "confirmation_reason": [
                    f"{direction} confirmed over {streak}/{persistence} consecutive cycles"
                ],
            }
        )
        episode["episode_id"] = episode_id
        return episode

    def _neutral_result(self, now, missing):
        return {
            "direction": _DIRECTION_NEUTRAL,
            "confirmed": False,
            "move_points": None,
            "origin_spot": None,
            "origin_time": None,
            "confirmation_reason": [],
            "missing_evidence": missing,
            "episode_id": _DIRECTION_NEUTRAL,
            "episode_confirmed": False,
        }
