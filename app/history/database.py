import os
import sqlite3
import sys

from app.config import config


def _project_base_path():
    """Resolve the project base path independent of CWD.

    Strategy:
    1. If running from the project directory, use that.
    2. Otherwise, derive from the location of the app package.
    3. Fall back to current directory with a 'data' subdirectory.
    """
    # Try to find the project root via the app module location
    try:
        import app
        # __file__ is app/__init__.py or similar; go up one level
        app_dir = os.path.dirname(os.path.dirname(os.path.abspath(app.__file__)))
        if os.path.isdir(os.path.join(app_dir, "data")):
            return app_dir
    except Exception:
        pass

    # Fallback: use the directory of the config module
    try:
        config_dir = os.path.dirname(os.path.abspath(config.__file__))
        parent = os.path.dirname(config_dir)
        if os.path.isdir(os.path.join(parent, "app")):
            return parent
    except Exception:
        pass

    # Last resort: use CWD
    return os.getcwd()


class Database:
    def __init__(self):
        base = _project_base_path()
        db_relative = config.DB_PATH if config.DB_PATH else "data/history.db"
        db_path = os.path.join(base, db_relative)
        db_dir = os.path.dirname(db_path)
        os.makedirs(db_dir, exist_ok=True)
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self):
        cur = self.conn.cursor()

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

    def execute(self, query, params=()):
        cur = self.conn.cursor()
        cur.execute(query, params)
        self.conn.commit()
        return cur

    def fetchone(self, query, params=()):
        cur = self.conn.cursor()
        cur.execute(query, params)
        return cur.fetchone()

    def fetchall(self, query, params=()):
        cur = self.conn.cursor()
        cur.execute(query, params)
        return cur.fetchall()


db = Database()
