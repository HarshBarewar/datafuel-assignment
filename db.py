"""
db.py: Database schema, connection management, and helper functions.
Supports both SQLite file databases and in-memory databases for tests.
"""
from datetime import datetime, timedelta, timezone
import os
import sqlite3
from typing import Optional

DB_PATH = os.environ.get("DATAFUEL_DB_PATH", "datafuel.db")
IST = timezone(timedelta(hours=5, minutes=30))


def to_utc_iso(dt_or_str: str | datetime) -> str:
    """Normalize any timestamp string or datetime object to canonical UTC ISO string."""
    if isinstance(dt_or_str, str):
        # Python 3.10/3.11 compatibility: replace 'Z' with '+00:00'
        dt = datetime.fromisoformat(dt_or_str.replace("Z", "+00:00"))
    else:
        dt = dt_or_str
    if dt.tzinfo is None:
        raise ValueError(f"Timestamp must include a timezone: {dt_or_str}")
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def ist_calendar_date(dt_or_str: str | datetime) -> str:
    """Return the YYYY-MM-DD date in Indian Standard Time (IST) for a given UTC timestamp."""
    if isinstance(dt_or_str, str):
        dt = datetime.fromisoformat(dt_or_str.replace("Z", "+00:00"))
    else:
        dt = dt_or_str
    if dt.tzinfo is None:
        raise ValueError(f"Timestamp must include a timezone: {dt_or_str}")
    return dt.astimezone(IST).date().isoformat()


def yesterday_in_ist() -> str:
    """Return yesterday's calendar date in IST."""
    now_ist = datetime.now(timezone.utc).astimezone(IST)
    return (now_ist.date() - timedelta(days=1)).isoformat()


def get_connection(db_path: Optional[str] = None) -> sqlite3.Connection:
    """Open an SQLite database connection with row factory and foreign keys enabled."""
    path = db_path if db_path is not None else DB_PATH
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Initialize schema tables with primary keys and indexes."""
    with conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS stores (
                store_id TEXT PRIMARY KEY,
                city TEXT NOT NULL,
                name TEXT NOT NULL,
                is_active INTEGER NOT NULL,
                is_serviceable INTEGER NOT NULL
            );
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS sweeps (
                as_of TEXT PRIMARY KEY,
                ist_date TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT
            );
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS store_sweeps (
                as_of TEXT NOT NULL,
                store_id TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT,
                item_count INTEGER NOT NULL DEFAULT 0,
                fetched_at TEXT NOT NULL,
                PRIMARY KEY (as_of, store_id),
                FOREIGN KEY (as_of) REFERENCES sweeps(as_of) ON DELETE CASCADE,
                FOREIGN KEY (store_id) REFERENCES stores(store_id) ON DELETE CASCADE
            );
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS observations (
                as_of TEXT NOT NULL,
                store_id TEXT NOT NULL,
                sku_id TEXT NOT NULL,
                name TEXT NOT NULL,
                in_stock INTEGER NOT NULL,
                qty INTEGER NOT NULL,
                price REAL NOT NULL,
                observed_at TEXT NOT NULL,
                PRIMARY KEY (as_of, store_id, sku_id),
                FOREIGN KEY (as_of, store_id) REFERENCES store_sweeps(as_of, store_id) ON DELETE CASCADE
            );
        """)

        conn.execute("CREATE INDEX IF NOT EXISTS idx_sweeps_ist_date ON sweeps(ist_date);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_stores_city_active ON stores(city, is_active);")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_obs_sku ON observations(sku_id);")
