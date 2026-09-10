"""
db.py - PostgreSQL logging layer for the CPU Monitor app.

Handles connecting to PostgreSQL, creating the alerts table if it doesn't
already exist, and inserting/reading alert records (date/time, cpu%, level).

Connection settings are read from environment variables, with sensible
local defaults. Copy .env.example to .env and edit it, or export the
variables in your shell before running the app:

    DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD
"""

import os
import logging
from datetime import datetime

try:
    from dotenv import load_dotenv
    load_dotenv()  # reads .env in the current working directory, if present
except ImportError:
    pass  # python-dotenv not installed - fall back to real environment variables only

try:
    import psycopg2
    import psycopg2.pool
    PSYCOPG2_AVAILABLE = True
except ImportError:
    PSYCOPG2_AVAILABLE = False

logger = logging.getLogger("cpu_monitor.db")

DB_CONFIG = {
    "host": os.environ.get("DB_HOST", "localhost"),
    "port": os.environ.get("DB_PORT", "5432"),
    "dbname": os.environ.get("DB_NAME", "cpu_monitor"),
    "user": os.environ.get("DB_USER", "postgres"),
    "password": os.environ.get("DB_PASSWORD", "postgres"),
}

CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS cpu_alerts (
    id SERIAL PRIMARY KEY,
    alert_time TIMESTAMP NOT NULL DEFAULT NOW(),
    cpu_percent REAL NOT NULL,
    level VARCHAR(20) NOT NULL,
    is_simulated BOOLEAN NOT NULL DEFAULT FALSE
);
"""

INSERT_ALERT_SQL = """
INSERT INTO cpu_alerts (alert_time, cpu_percent, level, is_simulated)
VALUES (%s, %s, %s, %s)
RETURNING id;
"""

FETCH_RECENT_SQL = """
SELECT id, alert_time, cpu_percent, level, is_simulated
FROM cpu_alerts
ORDER BY alert_time DESC
LIMIT %s;
"""


class DBLogger:
    """Thin wrapper around a pooled psycopg2 connection for alert logging.

    Designed to fail soft: if PostgreSQL isn't installed/reachable, the
    app keeps running and simply reports the DB as disconnected instead
    of crashing.
    """

    def __init__(self, config=None, min_conn=1, max_conn=3):
        self.config = config or DB_CONFIG
        self.connected = False
        self._pool = None
        self._last_error = None

        if not PSYCOPG2_AVAILABLE:
            self._last_error = "psycopg2 is not installed."
            logger.warning(self._last_error)
            return

        self.connect(min_conn, max_conn)

    def connect(self, min_conn=1, max_conn=3):
        """Attempt to (re)connect and ensure the alerts table exists."""
        if not PSYCOPG2_AVAILABLE:
            self._last_error = "psycopg2 is not installed."
            self.connected = False
            return False

        try:
            self._pool = psycopg2.pool.SimpleConnectionPool(
                min_conn, max_conn, **self.config
            )
            self._ensure_table()
            self.connected = True
            self._last_error = None
            logger.info(
                "Connected to PostgreSQL at %s:%s/%s",
                self.config["host"], self.config["port"], self.config["dbname"],
            )
            return True
        except Exception as exc:  # noqa: BLE001 - want to surface any DB error to the UI
            self.connected = False
            self._last_error = str(exc)
            logger.error("Could not connect to PostgreSQL: %s", exc)
            return False

    def _ensure_table(self):
        conn = self._pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(CREATE_TABLE_SQL)
            conn.commit()
        finally:
            self._pool.putconn(conn)

    def last_error(self):
        return self._last_error

    def log_alert(self, cpu_percent, level, is_simulated=False, when=None):
        """Insert one alert row (date/time + cpu% + level). Returns the new row id, or None."""
        if not self.connected or self._pool is None:
            return None

        when = when or datetime.now()
        conn = None
        try:
            conn = self._pool.getconn()
            with conn.cursor() as cur:
                cur.execute(INSERT_ALERT_SQL, (when, cpu_percent, level, is_simulated))
                new_id = cur.fetchone()[0]
            conn.commit()
            return new_id
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            logger.error("Failed to insert alert: %s", exc)
            self.connected = False
            return None
        finally:
            if conn is not None:
                self._pool.putconn(conn)

    def fetch_recent(self, limit=100):
        """Return the most recent alert rows as a list of tuples."""
        if not self.connected or self._pool is None:
            return []

        conn = None
        try:
            conn = self._pool.getconn()
            with conn.cursor() as cur:
                cur.execute(FETCH_RECENT_SQL, (limit,))
                rows = cur.fetchall()
            return rows
        except Exception as exc:  # noqa: BLE001
            self._last_error = str(exc)
            logger.error("Failed to fetch alerts: %s", exc)
            return []
        finally:
            if conn is not None:
                self._pool.putconn(conn)

    def close(self):
        if self._pool is not None:
            self._pool.closeall()
            self.connected = False
