"""SQLite persistence layer (BLOCKORA §59, §60, §74).

Creates the full §60 schema. Existing databases keep all legacy data: tables
are created with IF NOT EXISTS and no historical records are ever deleted or
rewritten. WAL mode keeps writes reliable on mobile.
"""

import logging
import os
import sqlite3
import threading

from app.config import config

logger = logging.getLogger(__name__)


def _project_base_path():
    """Resolve the project base path independent of CWD (Termux safety)."""
    try:
        import app
        app_dir = os.path.dirname(os.path.dirname(os.path.abspath(app.__file__)))
        if os.path.isdir(os.path.join(app_dir, "app")):
            return app_dir
    except Exception:
        pass
    return os.getcwd()


class Database:
    def __init__(self):
        base = _project_base_path()
        db_relative = config.DB_PATH or "data/history.db"
        db_path = os.path.join(base, db_relative)
        db_dir = os.path.dirname(db_path)
        os.makedirs(db_dir, exist_ok=True)
        self.db_path = db_path
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self._create_tables()

    def _create_tables(self):
        cur = self.conn.cursor()
        cur.execute("""
        CREATE TABLE IF NOT EXISTS market_snapshots (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle_id INTEGER,
            timestamp TEXT,
            underlying TEXT,
            spot REAL,
            source TEXT,
            vwap REAL,
            atr REAL,
            trend TEXT,
            regime TEXT,
            data_quality REAL
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS option_snapshots (
            snapshot_id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle_id INTEGER,
            timestamp TEXT,
            underlying TEXT,
            expiry TEXT,
            strike REAL,
            option_type TEXT,
            ltp REAL,
            bid REAL,
            ask REAL,
            volume REAL,
            oi REAL,
            oi_change REAL,
            iv REAL,
            source TEXT,
            data_timestamp TEXT
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS cycles (
            cycle_id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            underlying TEXT,
            spot REAL,
            expiry TEXT,
            regime TEXT,
            data_quality REAL,
            best_symbol TEXT,
            best_score REAL,
            confidence REAL,
            entry REAL,
            stop_loss REAL,
            target REAL,
            risk_reward REAL,
            signal_state TEXT
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS candidate_scores (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            cycle_id INTEGER,
            symbol TEXT,
            strike REAL,
            option_type TEXT,
            ltp REAL,
            bid REAL,
            ask REAL,
            volume REAL,
            oi REAL,
            oi_change REAL,
            iv REAL,
            delta REAL,
            gamma REAL,
            theta REAL,
            vega REAL,
            momentum_score REAL,
            oi_score REAL,
            volume_score REAL,
            liquidity_score REAL,
            greeks_score REAL,
            iv_score REAL,
            risk_reward_score REAL,
            total_score REAL,
            rank INTEGER
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS signals (
            signal_id TEXT PRIMARY KEY,
            created_at TEXT,
            underlying TEXT,
            expiry TEXT,
            strike REAL,
            option_type TEXT,
            entry REAL,
            stop_loss REAL,
            target REAL,
            score REAL,
            confidence REAL,
            regime TEXT,
            state TEXT
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS signal_state_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id TEXT,
            timestamp TEXT,
            state TEXT,
            score REAL,
            confidence REAL
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS signal_outcomes (
            signal_id TEXT PRIMARY KEY,
            entry REAL,
            highest_after_entry REAL,
            lowest_after_entry REAL,
            target REAL,
            sl REAL,
            result TEXT,
            time_to_result TEXT
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS system_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT,
            event TEXT,
            details TEXT
        )
        """)
        self.conn.commit()
        logger.info("SQLite schema ready at %s", self.db_path)

    # --- helpers (thread-safe) ---------------------------------------------

    def execute(self, query, params=()):
        with self._lock:
            cur = self.conn.cursor()
            cur.execute(query, params)
            self.conn.commit()
            return cur

    def fetchone(self, query, params=()):
        with self._lock:
            cur = self.conn.cursor()
            cur.execute(query, params)
            return cur.fetchone()

    def fetchall(self, query, params=()):
        with self._lock:
            cur = self.conn.cursor()
            cur.execute(query, params)
            return cur.fetchall()

    def log_event(self, event, details=None):
        try:
            self.execute(
                "INSERT INTO system_events (timestamp, event, details) VALUES (?, ?, ?)",
                (_ist_now_str(), event, details),
            )
        except Exception:
            logger.exception("system_events write failed")


def _ist_now_str():
    from datetime import datetime
    import pytz
    return datetime.now(pytz.timezone("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M:%S")


db = Database()
