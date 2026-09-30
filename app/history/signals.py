from datetime import datetime

import pytz

from app.history.database import db

IST = pytz.timezone("Asia/Kolkata")


class SignalHistory:
    @staticmethod
    def create_signal_id(underlying, strike, option_type):
        ts = datetime.now(IST).strftime("%Y%m%d-%H%M%S")
        return f"SIG-{ts}-{underlying}-{strike}{option_type}"

    @staticmethod
    def save_signal(signal):
        db.execute("""
            INSERT INTO signals (
                signal_id, created_at, underlying, expiry, strike, option_type,
                entry, stop_loss, target, score, confidence, regime, state
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            signal.get("signal_id"),
            datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S"),
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

    @staticmethod
    def latest_signal_for_contract(underlying, strike, option_type):
        return db.fetchone("""
            SELECT * FROM signals
            WHERE underlying=? AND strike=? AND option_type=?
            ORDER BY created_at DESC LIMIT 1
        """, (underlying, strike, option_type))
