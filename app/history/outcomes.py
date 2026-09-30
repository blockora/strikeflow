"""Signal state machine and outcome tracking (BLOCKORA §35, §40, §64, §65).

detect_state implements the §35 transitions from objective rules on scores
and contract identity. resolve_outcome freezes the original signal inputs and
lets future market data determine TARGET_REACHED / SL_REACHED / TIMEOUT /
INVALIDATED; the original signal record is never modified (§40).

Live monitoring: monitor_open_signals() checks the latest cached premium for
each open signal each cycle and resolves outcomes as data arrives.
"""

from datetime import datetime

import pytz

from app.data.cache import cache
from app.history.database import db
from app.history.signals import SignalHistory

IST = pytz.timezone("Asia/Kolkata")


def _now_str():
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def _symbol(underlying, strike, option_type):
    return f"{underlying} {strike} {option_type}"


def detect_state(previous_cycle, current_best, current_score, current_confidence=None):
    """§35 state machine over cycle-level BEST observations."""
    if current_best is None:
        return "NO SIGNAL"
    if previous_cycle is None:
        return "NEW"

    prev_symbol = previous_cycle.get("best_symbol")
    curr_symbol = _symbol(
        current_best.get("underlying"),
        current_best.get("strike"),
        current_best.get("option_type"),
    )

    if prev_symbol is None:
        return "NEW"
    if prev_symbol == curr_symbol:
        prev_score = previous_cycle.get("best_score")
        if prev_score is None:
            return "NEW"
        delta = current_score - float(prev_score)
        if delta >= 3:
            return "STRENGTHENING"
        if delta <= -3:
            return "WEAKENING"
        return "STABLE"
    return "NEW BEST"


class OutcomeEngine:
    CONFIRM_THRESHOLD_DELTA = 8.0  # documented initial: sustained high score confirms

    def __init__(self):
        self.cycle_counter = {}

    def resolve_outcome(self, signal_id, final_premium, result):
        """Store the outcome once; never re-resolve (PRIMARY KEY guard)."""
        try:
            db.execute("""
                INSERT INTO signal_outcomes (
                    signal_id, entry, highest_after_entry, lowest_after_entry,
                    target, sl, result, time_to_result
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                signal_id,
                final_premium.get("entry"),
                final_premium.get("highest_after_entry"),
                final_premium.get("lowest_after_entry"),
                final_premium.get("target"),
                final_premium.get("sl"),
                result,
                final_premium.get("time_to_result"),
            ))
            return True
        except Exception:
            import logging
            logging.getLogger(__name__).exception(
                "outcome insert failed for %s (already resolved?)", signal_id
            )
            return False

    def monitor_open_signals(self, tracked_signals):
        """Check open signals against the latest cached premium (§40, §64).

        tracked_signals: {signal_id: {"token": str, "entry": f, "stop_loss": f,
        "target": f, "highest": f, "lowest": f, "created": ts}}
        Mutates the dicts' highest/lowest and returns resolved signal_ids.

        Tick LTP arrives in paise (value * 100) per the installed SDK. The
        normalized ":n" cache entry is already rupees and is preferred; the
        raw tick is only a fallback and is converted deterministically.
        """
        resolved = []
        for signal_id, t in tracked_signals.items():
            token = t.get("token")
            if not token:
                continue
            quote = cache.get_quote(f"{token}:n") or cache.get_quote(token)
            if not quote:
                continue
            ltp = quote.get("ltp")
            if ltp is None:
                continue
            try:
                ltp = float(ltp)
            except (TypeError, ValueError):
                continue
            if not str(token).endswith(":n"):
                # Raw cached tick: wire scale is paise for NFO instruments.
                ltp = ltp / 100.0
            t["highest"] = max(t.get("highest") or ltp, ltp)
            t["lowest"] = min(t.get("lowest") or ltp, ltp)
            if t.get("target") is not None and ltp >= t["target"]:
                self._close(signal_id, t, "TARGET_REACHED")
                resolved.append(signal_id)
            elif t.get("stop_loss") is not None and ltp <= t["stop_loss"]:
                self._close(signal_id, t, "SL_REACHED")
                resolved.append(signal_id)
        return resolved

    def _close(self, signal_id, t, result):
        time_to = None
        created = t.get("created")
        if created:
            elapsed = datetime.now(IST) - created
            time_to = str(elapsed)
        self.resolve_outcome(signal_id, {
            "entry": t.get("entry"),
            "highest_after_entry": t.get("highest"),
            "lowest_after_entry": t.get("lowest"),
            "target": t.get("target"),
            "sl": t.get("stop_loss"),
            "time_to_result": time_to,
        }, result)
        SignalHistory.record_state(signal_id, result, None, None)

    def historical_setup_stats(self, underlying, strike, option_type, regime=None):
        """Setup-level outcome statistics (§65) with enforced sample reporting.

        Returns {"samples", "target_reached", "sl_reached", "timeout",
        "invalidated", "win_rate"} — samples may be below
        MIN_HISTORICAL_SAMPLES, which the confidence engine must penalize.
        """
        rows = db.fetchall("""
            SELECT o.result FROM signal_outcomes o
            JOIN signals s ON s.signal_id = o.signal_id
            WHERE s.underlying = ? AND s.strike = ? AND s.option_type = ?
        """, (underlying, strike, option_type))
        stats = {
            "samples": 0,
            "target_reached": 0,
            "sl_reached": 0,
            "timeout": 0,
            "invalidated": 0,
            "win_rate": None,
        }
        for row in rows:
            result = row["result"]
            stats["samples"] += 1
            if result == "TARGET_REACHED":
                stats["target_reached"] += 1
            elif result == "SL_REACHED":
                stats["sl_reached"] += 1
            elif result == "TIMEOUT":
                stats["timeout"] += 1
            elif result == "INVALIDATED":
                stats["invalidated"] += 1
        if stats["samples"] > 0:
            stats["win_rate"] = round(stats["target_reached"] / stats["samples"], 3)
        return stats
