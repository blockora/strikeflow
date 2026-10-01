"""SQLite persistence layer (BLOCKORA §59, §60, §74).

Creates the full §60 schema. Existing databases keep all legacy data: tables
are created with IF NOT EXISTS and no historical records are ever deleted or
rewritten. WAL mode keeps writes reliable on mobile.

Schema evolution (BLOCKORA §74: restarts must never lose history):
- Fresh databases are created directly at the current schema.
- Existing databases are upgraded additively at startup (ALTER TABLE ADD
  COLUMN only) BEFORE any persist path can run; historical rows are never
  altered or deleted.
- The applied schema revision is stamped in PRAGMA user_version, so repeated
  startups are idempotent no-ops.
- A mismatch that cannot be migrated safely raises SchemaMigrationError and
  blocks startup instead of failing every cycle at INSERT time.

Add a new step to _MIGRATIONS (and bump SCHEMA_VERSION) whenever a committed
schema change affects tables that may already exist on a production device.

The post-migration integrity check verifies exactly the columns the
application reads/writes (see _REQUIRED_COLUMNS). Synthetic AUTOINCREMENT
primary keys (snapshot_id / id) are fresh-DDL identity only: no query in
this codebase references them, so legacy tables created without them stay
valid — they are never force-added to historical rows.
"""

import logging
import os
import sqlite3
import threading

from app.config import config

logger = logging.getLogger(__name__)


class SchemaMigrationError(RuntimeError):
    """Raised when an existing database cannot be safely migrated."""


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
        self._migrate_schema()

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

    # --- schema migration (additive-only, idempotent; BLOCKORA §74) --------

    # Current schema revision. Bump and append a matching step in _MIGRATIONS
    # whenever a committed schema change affects tables that may already exist
    # on a production device.
    SCHEMA_VERSION = 1

    # Additive-only migration steps, keyed by the schema version they upgrade
    # TO. Each step is (table, column, ddl); ddl must be an ALTER TABLE ADD
    # COLUMN. No historical row is ever altered or deleted. Columns already
    # present are skipped, so a fresh database (created at the current schema
    # but still stamped user_version=0) and a partially-migrated database are
    # both handled safely.
    _MIGRATIONS = {
        1: (
            (
                "option_snapshots",
                "data_timestamp",
                "ALTER TABLE option_snapshots ADD COLUMN data_timestamp TEXT",
            ),
        ),
    }

    # Columns the application actually reads/writes per table (INSERT
    # contracts in app/history/*.py plus PKs used by SELECT/ORDER/JOIN).
    # Mirrors what real queries require — deliberately NOT the full CREATE
    # TABLE DDL, because synthetic AUTOINCREMENT PKs (snapshot_id / id) are
    # never referenced by application code and must not be forced onto legacy
    # production tables. Columns used by app code: cycles.cycle_id
    # (latest_cycle ORDER BY), signals.signal_id / signal_outcomes.signal_id
    # (PK lookups/JOIN); snapshot_id / id are used nowhere.
    _REQUIRED_COLUMNS = {
        "market_snapshots": (
            "cycle_id", "timestamp", "underlying", "spot", "source",
            "vwap", "atr", "trend", "regime", "data_quality",
        ),
        "option_snapshots": (
            "cycle_id", "timestamp", "underlying", "expiry", "strike",
            "option_type", "ltp", "bid", "ask", "volume", "oi",
            "oi_change", "iv", "source", "data_timestamp",
        ),
        "cycles": (
            "cycle_id", "timestamp", "underlying", "spot", "expiry", "regime",
            "data_quality", "best_symbol", "best_score", "confidence",
            "entry", "stop_loss", "target", "risk_reward", "signal_state",
        ),
        "candidate_scores": (
            "cycle_id", "symbol", "strike", "option_type", "ltp", "bid",
            "ask", "volume", "oi", "oi_change", "iv", "delta", "gamma",
            "theta", "vega", "momentum_score", "oi_score", "volume_score",
            "liquidity_score", "greeks_score", "iv_score",
            "risk_reward_score", "total_score", "rank",
        ),
        "signals": (
            "signal_id", "created_at", "underlying", "expiry", "strike",
            "option_type", "entry", "stop_loss", "target", "score",
            "confidence", "regime", "state",
        ),
        "signal_state_history": (
            "signal_id", "timestamp", "state", "score", "confidence",
        ),
        "signal_outcomes": (
            "signal_id", "entry", "highest_after_entry", "lowest_after_entry",
            "target", "sl", "result", "time_to_result",
        ),
        "system_events": (
            "timestamp", "event", "details",
        ),
    }

    def _existing_columns(self, table):
        """Column names of an existing table (table names are internal constants)."""
        cur = self.conn.execute(
            "SELECT name FROM pragma_table_info(?)", (table,)
        )
        return {row[0] for row in cur.fetchall()}

    def _migrate_schema(self):
        """Upgrade an existing database to the current schema; no-op when current.

        Runs at Database.__init__ (module import time), i.e. strictly before
        any persist path can run. Idempotent: columns are added only when
        actually missing and the applied revision is stamped in
        PRAGMA user_version, so repeated startups never fail or duplicate work.
        """
        try:
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        except sqlite3.DatabaseError as exc:
            raise SchemaMigrationError(
                f"Cannot read schema version from {self.db_path}: {exc}"
            ) from exc

        try:
            existing_tables = {
                row[0]
                for row in self.conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        except sqlite3.DatabaseError as exc:
            raise SchemaMigrationError(
                f"Cannot inspect schema of {self.db_path}: {exc}"
            ) from exc

        if version > self.SCHEMA_VERSION:
            raise SchemaMigrationError(
                f"Database {self.db_path} has schema version {version}, newer "
                f"than supported version {self.SCHEMA_VERSION}; refusing to "
                f"start rather than risk modifying unknown history"
            )

        if version < self.SCHEMA_VERSION:
            logger.info(
                "Database schema upgrade: v%s -> v%s at %s",
                version, self.SCHEMA_VERSION, self.db_path,
            )

            # Safety gate: every required table must exist (created above by
            # CREATE TABLE IF NOT EXISTS).
            missing_tables = [t for t in self._REQUIRED_COLUMNS if t not in existing_tables]
            if missing_tables:
                raise SchemaMigrationError(
                    f"Migration aborted: required tables missing from "
                    f"{self.db_path}: {missing_tables}; refusing to continue"
                )

            try:
                for step_version in sorted(self._MIGRATIONS):
                    if step_version <= version:
                        continue
                    for table, column, ddl in self._MIGRATIONS[step_version]:
                        if not ddl.lstrip().upper().startswith("ALTER TABLE"):
                            raise SchemaMigrationError(
                                "Non-additive migration step rejected: " + ddl
                            )
                        if column in self._existing_columns(table):
                            continue  # already applied; keeps migration idempotent
                        self.conn.execute(ddl)
                        logger.info(
                            "Schema migration applied: %s", ddl
                        )
                    self.conn.execute(f"PRAGMA user_version = {step_version}")
                    self.conn.commit()
            except sqlite3.Error as exc:
                self.conn.rollback()
                raise SchemaMigrationError(
                    f"Schema migration to v{self.SCHEMA_VERSION} failed for "
                    f"{self.db_path}: {exc}"
                ) from exc

            logger.info("Database schema migrated to v%s", self.SCHEMA_VERSION)

        # Final integrity check: every column the application actually reads
        # or writes must exist after migration, so the INSERT/SELECT contracts
        # in app/history/*.py can never hit a missing column. Legacy tables
        # lacking synthetic PKs (snapshot_id/id) remain valid by design.
        for table, columns in self._REQUIRED_COLUMNS.items():
            if table not in existing_tables:
                raise SchemaMigrationError(
                    f"Schema integrity check failed: required table "
                    f"'{table}' missing from {self.db_path}"
                )
            missing = set(columns) - self._existing_columns(table)
            if missing:
                raise SchemaMigrationError(
                    f"Schema integrity check failed for table '{table}': "
                    f"missing columns {sorted(missing)} after migration"
                )

        logger.info("SQLite schema verified at v%s", self.SCHEMA_VERSION)

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
