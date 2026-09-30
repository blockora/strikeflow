from datetime import datetime

import pytz

from app.history.database import db

IST = pytz.timezone("Asia/Kolkata")


def _now_str():
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


class SignalHistory:
    """Signal persistence (BLOCKORA §63).

    Every BEST signal receives a unique signal_id and is stored exactly once;
    the record is never overwritten. Later cycle information goes to
    signal_state_history as new append-only rows.
    """

    @staticmethod
    def create_signal_id(underlying, strike, option_type):
        ts = datetime.now(IST).strftime("%Y%m%d-%H%M%S")
        return f"SIG-{ts}-{underlying}-{strike}{option_type}"

    @staticmethod
    def save_signal(signal):
        try:
            db.execute("""
                INSERT INTO signals (
                    signal_id, created_at, underlying, expiry, strike, option_type,
                    entry, stop_loss, target, score, confidence, regime, state
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                signal.get("signal_id"),
                _now_str(),
                signal.get("underlying"),
                signal.get("expiry"),
                signal.get("strike"),
                signal.get("option_type"),
                signal.get("entry"),
                signal.get("stop_loss"),
                signal.get("target"),
                signal.get("score"),
                signal.get("confidence"),
                signal.get("regime"),
                signal.get("state"),
            ))
            return True
        except Exception:
            # Duplicate signal_id or constraint failure: log, never overwrite.
            import logging
            logging.getLogger(__name__).exception("signal insert failed for %s", signal.get("signal_id"))
            return False

    @staticmethod
    def record_state(signal_id, state, score, confidence):
        """Append-only state history (§63: never modify the original record)."""
        db.execute("""
            INSERT INTO signal_state_history (signal_id, timestamp, state, score, confidence)
            VALUES (?, ?, ?, ?, ?)
        """, (signal_id, _now_str(), state, score, confidence))

    @staticmethod
    def latest_open_signal(underlying):
        """Most recent signal still awaiting an outcome (for the outcome engine)."""
        return db.fetchone("""
            SELECT * FROM signals
            WHERE underlying = ?
              AND signal_id NOT IN (SELECT signal_id FROM signal_outcomes)
            ORDER BY created_at DESC LIMIT 1
        """, (underlying,))

    @staticmethod
    def latest_signal_for_contract(underlying, strike, option_type):
        return db.fetchone("""
            SELECT * FROM signals
            WHERE underlying=? AND strike=? AND option_type=?
            ORDER BY created_at DESC LIMIT 1
        """, (underlying, strike, option_type))
