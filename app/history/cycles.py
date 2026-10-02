import json
from datetime import datetime

import pytz

from app.history.database import db

IST = pytz.timezone("Asia/Kolkata")


def _now_str():
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def _origin_time_str(value):
    """Epoch seconds -> IST 'YYYY-MM-DD HH:MM:SS', matching the TEXT timestamp
    convention used by every other time column in this schema.  Keeping the
    stored type consistent with `cycles.timestamp` makes §59 reconstruction
    readable without conversion.  None stays None (nothing fabricated).
    """
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), IST).strftime("%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _compact_reason(reasons):
    """Serialize confirmation reasons as a compact JSON string for §59.

    Reasons are short human-readable strings, so a compact JSON array keeps
    them lossless (no delimiter collisions) while staying small.  None / empty
    become NULL rather than an empty blob.
    """
    if not reasons:
        return None
    try:
        return json.dumps(list(reasons), separators=(",", ":"))
    except (TypeError, ValueError):
        return None


class CycleHistory:
    @staticmethod
    def save_cycle(cycle):
        """Persist one analysis cycle (BLOCKORA §32) and return its cycle_id.

        This is the SINGLE authoritative cycle-write path.  Directional evidence
        is written on the SAME row as the cycle it belongs to — never as a
        second INSERT, which would violate the cycles.cycle_id primary key.

        Directional columns are additive (schema v2/v3) and are populated for
        every persisted cycle, including NO CLEAR STRIKE and data-quality
        failures, so §59 reconstruction works for rejections too.  Values are
        NULL when genuinely unavailable — nothing is fabricated.
        """
        cur = db.execute("""
            INSERT INTO cycles (
                timestamp, underlying, spot, expiry, regime, data_quality,
                best_symbol, best_score, confidence, entry, stop_loss,
                target, risk_reward, signal_state,
                direction, confirmed_move, direction_confirmed,
                direction_origin_time, direction_origin_spot,
                direction_confirm_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                      ?, ?, ?, ?, ?, ?)
        """, (
            _now_str(),
            cycle.get("underlying"),
            cycle.get("spot"),
            cycle.get("expiry"),
            cycle.get("regime"),
            cycle.get("data_quality"),
            cycle.get("best_symbol"),
            cycle.get("best_score"),
            cycle.get("confidence"),
            cycle.get("entry"),
            cycle.get("stop_loss"),
            cycle.get("target"),
            cycle.get("risk_reward"),
            cycle.get("signal_state"),
            cycle.get("direction"),
            cycle.get("confirmed_move"),
            cycle.get("direction_confirmed"),
            _origin_time_str(cycle.get("direction_origin_time")),
            cycle.get("direction_origin_spot"),
            _compact_reason(cycle.get("direction_confirm_reason")),
        ))
        return cur.lastrowid

    @staticmethod
    def latest_cycle():
        """Most recent cycle as a plain dict, or None.

        Every consumer (run_cycle's previous-cycle comparison and
        history.outcomes.detect_state) calls `.get()` on the result, but
        sqlite3.Row has no `.get()`.  Returning a Row therefore raised
        AttributeError from cycle 2 onwards.  A dict satisfies every call site
        with no other change.
        """
        row = db.fetchone("SELECT * FROM cycles ORDER BY cycle_id DESC LIMIT 1")
        return dict(row) if row is not None else None

    @staticmethod
    def save_market_snapshot(cycle_id, underlying_snapshot, regime, data_quality, source=None):
        db.execute("""
            INSERT INTO market_snapshots (
                cycle_id, timestamp, underlying, spot, source, vwap, atr,
                trend, regime, data_quality
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            cycle_id,
            _now_str(),
            underlying_snapshot.get("symbol"),
            underlying_snapshot.get("spot"),
            source or "UNKNOWN",
            underlying_snapshot.get("vwap"),
            underlying_snapshot.get("atr"),
            underlying_snapshot.get("trend"),
            regime,
            data_quality,
        ))

    @staticmethod
    def save_option_snapshot(cycle_id, underlying, candidates):
        """Persist per-contract chain observations for the cycle (§60)."""
        ts = _now_str()
        for c in candidates:
            db.execute("""
                INSERT INTO option_snapshots (
                    cycle_id, timestamp, underlying, expiry, strike, option_type,
                    ltp, bid, ask, volume, oi, oi_change, iv, source, data_timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                cycle_id,
                ts,
                c.get("underlying"),
                c.get("expiry"),
                c.get("strike"),
                c.get("option_type"),
                c.get("ltp"),
                c.get("bid"),
                c.get("ask"),
                c.get("volume"),
                c.get("oi"),
                c.get("oi_change"),
                c.get("iv"),
                c.get("source"),
                None,
            ))

    @staticmethod
    def save_candidate_scores(cycle_id, candidates):
        """Persist scored candidates (BLOCKORA §62) for the given cycle."""
        for c in candidates:
            scores = c.get("scores", {})
            db.execute("""
                INSERT INTO candidate_scores (
                    cycle_id, symbol, strike, option_type, ltp, bid, ask,
                    volume, oi, oi_change, iv, delta, gamma, theta, vega,
                    momentum_score, oi_score, volume_score, liquidity_score,
                    greeks_score, iv_score, risk_reward_score, total_score, rank
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                cycle_id,
                f"{c.get('underlying')} {c.get('strike')} {c.get('option_type')}",
                c.get("strike"),
                c.get("option_type"),
                c.get("ltp"),
                c.get("bid"),
                c.get("ask"),
                c.get("volume"),
                c.get("oi"),
                c.get("oi_change"),
                c.get("iv"),
                c.get("delta"),
                c.get("gamma"),
                c.get("theta"),
                c.get("vega"),
                scores.get("option_momentum"),
                scores.get("oi"),
                scores.get("volume"),
                scores.get("liquidity"),
                scores.get("greeks"),
                scores.get("iv"),
                scores.get("risk_reward"),
                c.get("total_score"),
                c.get("rank"),
            ))
