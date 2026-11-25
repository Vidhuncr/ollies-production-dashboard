import csv
import base64
import gzip
import hashlib
import hmac
import io
import json
import os
import re
import secrets
import socket
import sqlite3
import smtplib
import sys
import threading
import time
import traceback
import webbrowser
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from math import isfinite
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from email.message import EmailMessage
from queue import Empty, Full, LifoQueue
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlparse
from urllib.request import Request, urlopen

try:
    import psycopg
    from psycopg import Error as PsycopgError
    from psycopg.errors import IntegrityError as PsycopgIntegrityError
except ImportError:
    psycopg = None

    class PsycopgError(Exception):
        pass

    class PsycopgIntegrityError(Exception):
        pass

ROOT = Path(__file__).resolve().parent
DB = ROOT / "plant_production.db"


def load_local_env():
    env_file = ROOT / ".env"
    if env_file.exists():
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def bounded_env_number(name, default, minimum, maximum, as_float=False):
    try:
        value = float(os.environ.get(name, str(default)))
    except (TypeError, ValueError):
        value = float(default)
    value = max(float(minimum), min(value, float(maximum)))
    return value if as_float else int(value)


load_local_env()
SUPABASE_DATABASE_URL = (os.environ.get("SUPABASE_DATABASE_URL") or os.environ.get("DATABASE_URL") or "").strip()
USE_SUPABASE = bool(SUPABASE_DATABASE_URL)
APP_USERNAME = os.environ.get("APP_USERNAME", "admin")
APP_ADMIN_EMAIL = os.environ.get("APP_ADMIN_EMAIL", "admin@ollies-demo.com").strip().lower()
APP_PASSWORD = os.environ.get("APP_PASSWORD", "Ollies123!")
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "").strip()
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
EMAIL_ENABLED = os.environ.get("EMAIL_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
EMAIL_PROVIDER = os.environ.get("EMAIL_PROVIDER", "gmail_smtp").strip().lower()
GOOGLE_APPS_SCRIPT_URL = os.environ.get("GOOGLE_APPS_SCRIPT_URL", "").strip()
GOOGLE_APPS_SCRIPT_SECRET = os.environ.get("GOOGLE_APPS_SCRIPT_SECRET", "").strip()
SESSION_SECRET = os.environ.get("SESSION_SECRET", "").strip() or hashlib.sha256(
    (APP_PASSWORD + "|ollies-production-session").encode("utf-8")
).hexdigest()
SESSION_TTL_SECONDS = 12 * 60 * 60
SESSION_USER_CACHE_SECONDS = 60
SESSION_USER_CACHE = {}
REVOKED_SESSIONS = {}
SESSION_LOCK = threading.RLock()
DASHBOARD_CACHE_LOCK = threading.RLock()
DASHBOARD_CACHE = {"expires": 0.0, "value": None}
DASHBOARD_CACHE_TTL = bounded_env_number("DASHBOARD_CACHE_TTL", 1.5, 0.25, 10, as_float=True)
READ_CACHE_LOCK = threading.RLock()
READ_CACHE = {}
READ_CACHE_TTL = bounded_env_number("READ_CACHE_TTL", 3, 0.5, 30, as_float=True)
STATIC_CACHE_LOCK = threading.RLock()
STATIC_CACHE = {}
PROCESS_STARTED_AT = time.monotonic()
MAX_REQUEST_BYTES = bounded_env_number("MAX_REQUEST_BYTES", 1048576, 4096, 5 * 1024 * 1024)
DatabaseIntegrityError = (sqlite3.IntegrityError, PsycopgIntegrityError)
DatabaseOperationalError = (sqlite3.OperationalError, PsycopgError, RuntimeError)
PACKAGE_TYPES = {
    "5_gallon_pail": ("5 Gallon Pail", 5.0),
    "1_gallon_pail": ("1 Gallon Pail", 1.0),
    "quart": ("Quart", 0.25),
    "250_gallon_tote": ("250 Gallon Tote", 250.0),
    "case_pack": ("Case Pack", 1.0),
    "each": ("Individual Units", 1.0),
}
GALLON_PACKAGE_TYPES = {"5_gallon_pail", "1_gallon_pail", "quart", "250_gallon_tote"}
QTY_PACKAGE_TYPES = {"case_pack", "each"}


def package_type_data():
    return [{"key": key, "label": value[0], "size_gallons": value[1],
             "unit": "qty" if key in QTY_PACKAGE_TYPES else "gallons"} for key, value in PACKAGE_TYPES.items()]


def package_size_for(package_type, unit, raw_size=None):
    allowed = QTY_PACKAGE_TYPES if unit == "qty" else GALLON_PACKAGE_TYPES
    if package_type not in allowed: raise ValueError("Select a package type that matches the product unit.")
    if package_type == "case_pack":
        value = float(raw_size or 0); size = int(value)
        if value != size or size <= 0: raise ValueError("Enter a whole-number quantity per case.")
        return float(size)
    return float(PACKAGE_TYPES[package_type][1])


def hash_password(password, salt=None):
    salt = salt or os.urandom(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
    return base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password, stored):
    try:
        salt_text, digest_text = stored.split("$", 1); salt = base64.b64decode(salt_text)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, 200_000)
        return hmac.compare_digest(base64.b64encode(actual).decode(), digest_text)
    except Exception:
        return False


def create_session(user):
    now = time.time()
    payload = {
        "exp": int(now + SESSION_TTL_SECONDS),
        "id": user["id"],
        "username": user["username"],
        "email": user["email"] if "email" in user.keys() else "",
        "display_name": user["display_name"],
        "role": user["role"],
        "nonce": secrets.token_urlsafe(12),
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ).decode("ascii").rstrip("=")
    signature = base64.urlsafe_b64encode(
        hmac.new(SESSION_SECRET.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
    ).decode("ascii").rstrip("=")
    with SESSION_LOCK:
        expired = [key for key, value in REVOKED_SESSIONS.items() if value <= now]
        for key in expired:
            REVOKED_SESSIONS.pop(key, None)
        SESSION_USER_CACHE[user["id"]] = (now + SESSION_USER_CACHE_SECONDS, dict(payload))
    return encoded + "." + signature


def decode_session(token):
    try:
        encoded, signature = token.split(".", 1)
        expected = base64.urlsafe_b64encode(
            hmac.new(SESSION_SECRET.encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
        ).decode("ascii").rstrip("=")
        if not hmac.compare_digest(signature, expected):
            return None
        padding = "=" * (-len(encoded) % 4)
        payload = json.loads(base64.urlsafe_b64decode(encoded + padding).decode("utf-8"))
        if int(payload.get("exp", 0)) <= time.time() or not int(payload.get("id", 0)):
            return None
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with SESSION_LOCK:
            if REVOKED_SESSIONS.get(token_hash, 0) > time.time():
                return None
        return payload
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return None


def valid_session(token):
    return session_data(token) is not None


def session_data(token):
    payload = decode_session(token)
    if not payload:
        return None
    user_id = int(payload["id"])
    now = time.time()
    with SESSION_LOCK:
        cached = SESSION_USER_CACHE.get(user_id)
        if cached and cached[0] > now:
            return dict(cached[1])
    with closing(connect()) as con:
        user = con.execute("SELECT id,username,email,display_name,role FROM users WHERE id=? AND active=1", (user_id,)).fetchone()
    if not user:
        return None
    result = dict(user)
    result["exp"] = payload["exp"]
    with SESSION_LOCK:
        SESSION_USER_CACHE[user_id] = (now + SESSION_USER_CACHE_SECONDS, result)
    return dict(result)


def clear_session(token):
    payload = decode_session(token)
    with SESSION_LOCK:
        if payload:
            REVOKED_SESSIONS[hashlib.sha256(token.encode("utf-8")).hexdigest()] = int(payload["exp"])
            SESSION_USER_CACHE.pop(int(payload["id"]), None)


def invalidate_session_user(user_id):
    with SESSION_LOCK:
        SESSION_USER_CACHE.pop(int(user_id), None)


def invalidate_dashboard_cache():
    with DASHBOARD_CACHE_LOCK:
        DASHBOARD_CACHE["expires"] = 0.0
        # Keep the prior value as a short-lived emergency fallback if Supabase
        # has a transient interruption while the next refresh is attempted.
    with READ_CACHE_LOCK:
        READ_CACHE.clear()


def cached_read(key, loader, ttl=None):
    """Cache expensive read-only payloads briefly without caching writes."""
    now = time.monotonic()
    with READ_CACHE_LOCK:
        cached = READ_CACHE.get(key)
        if cached and cached[0] > now:
            return cached[1]
    value = loader()
    with READ_CACHE_LOCK:
        if len(READ_CACHE) >= 128:
            expired = [name for name, item in READ_CACHE.items() if item[0] <= now]
            for name in expired:
                READ_CACHE.pop(name, None)
            if len(READ_CACHE) >= 128:
                READ_CACHE.pop(next(iter(READ_CACHE)), None)
        READ_CACHE[key] = (time.monotonic() + (ttl or READ_CACHE_TTL), value)
    return value


def cached_static_file(filename):
    path = ROOT / filename
    stamp = path.stat().st_mtime_ns
    with STATIC_CACHE_LOCK:
        cached = STATIC_CACHE.get(filename)
        if cached and cached[0] == stamp:
            return cached[1], cached[2]
    data = path.read_bytes()
    etag = 'W/"' + hashlib.sha256(data).hexdigest()[:20] + '"'
    with STATIC_CACHE_LOCK:
        STATIC_CACHE[filename] = (stamp, data, etag)
    return data, etag


def valid_email(value):
    return bool(re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", str(value or "").strip()))


def normalize_s_number(value):
    result = str(value or "").strip().upper()
    if not re.fullmatch(r"S\d{5}", result):
        raise ValueError("S# must be S followed by exactly 5 digits, for example S12345.")
    return result


def normalize_order_reference(value, current=""):
    result = str(value or "").strip().upper()
    if not result:
        return ""
    if re.fullmatch(r"S\d{5}", result):
        return result
    if result == str(current or "").strip().upper() and re.fullmatch(r"N\d{5}", result):
        return result
    raise ValueError("S# is optional. When entered, it must be S followed by exactly 5 digits, for example S12345.")


def normalize_ship_month(value):
    result = str(value or "").strip()
    if not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", result):
        raise ValueError("Select a valid ship month.")
    return result


def normalize_po_number(value):
    result = re.sub(r"[\r\n\t]+", " ", str(value or "")).strip()
    if len(result) > 80:
        raise ValueError("PO# must be 80 characters or fewer.")
    return result


def next_no_s_reference_sql(con):
    values = []
    for row in con.execute("SELECT s_number FROM orders WHERE s_number LIKE 'N%'").fetchall():
        reference = str(row[0] or "").upper()
        if re.fullmatch(r"N\d{5}", reference):
            values.append(int(reference[1:]))
    value = max(values, default=0) + 1
    if value > 99999:
        raise ValueError("No-S# order reference limit was reached.")
    return f"N{value:05d}"


def _plain_value(value):
    if isinstance(value, Decimal):
        return float(value)
    return value


class DatabaseRow:
    """Row object compatible with both sqlite3.Row and PostgreSQL tuples."""

    def __init__(self, columns, values):
        self._columns = tuple(columns)
        self._values = tuple(_plain_value(value) for value in values)
        self._mapping = dict(zip(self._columns, self._values))

    def __getitem__(self, key):
        return self._values[key] if isinstance(key, int) else self._mapping[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def keys(self):
        return self._columns


POSTGRES_ID_TABLES = {
    "orders", "production", "catalog", "contacts", "users", "packaging",
    "shipments", "stock_packaging", "stock_needs", "stock_need_fulfillments",
    "packaging_requirements", "labor_rates", "labor_entries",
}


def postgres_sql(sql):
    """Translate the small SQLite placeholder differences used by this app."""
    translated = sql.replace("?", "%s")
    translated = re.sub(r"\bINSERT\s+OR\s+IGNORE\s+INTO\b", "INSERT INTO", translated, flags=re.I)
    return translated


class PostgresCursor:
    def __init__(self, cursor, rows=None, lastrowid=None):
        self._cursor = cursor
        self._rows = rows
        self._offset = 0
        self.lastrowid = lastrowid
        self.rowcount = cursor.rowcount

    def _convert(self, values):
        if values is None:
            return None
        columns = [item.name for item in self._cursor.description]
        return DatabaseRow(columns, values)

    def fetchone(self):
        if self._rows is not None:
            if self._offset >= len(self._rows):
                return None
            value = self._rows[self._offset]
            self._offset += 1
            return value
        return self._convert(self._cursor.fetchone())

    def fetchall(self):
        if self._rows is not None:
            values = self._rows[self._offset:]
            self._offset = len(self._rows)
            return values
        return [self._convert(value) for value in self._cursor.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


def new_postgres_connection(database_url):
    """Create a resilient pooled connection with bounded database waits."""
    if psycopg is None:
        raise RuntimeError("PostgreSQL support is not installed. Run: python -m pip install -r requirements.txt")
    connection = psycopg.connect(
        database_url,
        connect_timeout=10,
        prepare_threshold=None,
        application_name="ollies-production-dashboard",
        keepalives=1,
        keepalives_idle=30,
        keepalives_interval=10,
        keepalives_count=3,
    )
    connection.execute("SET statement_timeout TO '25s'")
    connection.execute("SET lock_timeout TO '8s'")
    connection.execute("SET idle_in_transaction_session_timeout TO '30s'")
    connection.commit()
    return connection


class PostgresConnection:
    def __init__(self, database_url=None, connection=None, pool=None):
        if psycopg is None:
            raise RuntimeError("PostgreSQL support is not installed. Run: python -m pip install -r requirements.txt")
        self._pool = pool
        self._connection = connection or new_postgres_connection(database_url)

    def execute(self, sql, params=()):
        statement = postgres_sql(sql).strip()
        insert_match = re.match(r"INSERT\s+INTO\s+([a-z_][a-z0-9_]*)", statement, flags=re.I)
        returning_id = bool(insert_match and insert_match.group(1).lower() in POSTGRES_ID_TABLES
                            and not re.search(r"\bRETURNING\b", statement, flags=re.I))
        if returning_id:
            statement = statement.rstrip("; \t\r\n") + " RETURNING id"
        cursor = self._connection.cursor()
        cursor.execute(statement, tuple(params or ()))
        if returning_id:
            inserted = cursor.fetchone()
            return PostgresCursor(cursor, rows=[], lastrowid=inserted[0] if inserted else None)
        return PostgresCursor(cursor)

    def commit(self):
        self._connection.commit()

    def rollback(self):
        self._connection.rollback()

    def close(self):
        connection, self._connection = self._connection, None
        if connection is None:
            return
        if self._pool is not None:
            self._pool.put(connection)
        else:
            connection.close()


class PostgresPool:
    """Small bounded pool that works with the app's existing connection wrapper."""

    def __init__(self, database_url, max_size=8, timeout=10, max_idle=300):
        self.database_url = database_url
        self.max_size = max(1, min(int(max_size), 20))
        self.timeout = max(1, int(timeout))
        self.max_idle = max(30, int(max_idle))
        self.available = LifoQueue(maxsize=self.max_size)
        self.lock = threading.Lock()
        self.total = 0

    def _decrement(self):
        with self.lock:
            self.total = max(0, self.total - 1)

    def _create(self):
        try:
            return new_postgres_connection(self.database_url)
        except Exception:
            self._decrement()
            raise

    def _validate(self, connection):
        try:
            connection.execute("SELECT 1")
            connection.rollback()
            return True
        except Exception:
            try:
                connection.close()
            finally:
                self._decrement()
            return False

    def get(self):
        while True:
            try:
                connection, returned_at = self.available.get_nowait()
                if connection.closed or time.monotonic() - returned_at > self.max_idle:
                    try:
                        connection.close()
                    finally:
                        self._decrement()
                    continue
                if self._validate(connection):
                    return connection
                continue
            except Empty:
                with self.lock:
                    create_new = self.total < self.max_size
                    if create_new:
                        self.total += 1
                if create_new:
                    return self._create()
                try:
                    connection, returned_at = self.available.get(timeout=self.timeout)
                except Empty as exc:
                    raise RuntimeError("The database is busy. Please try again in a moment.") from exc
                if connection.closed or time.monotonic() - returned_at > self.max_idle:
                    try:
                        connection.close()
                    finally:
                        self._decrement()
                    continue
                if self._validate(connection):
                    return connection

    def put(self, connection):
        try:
            connection.rollback()
        except Exception:
            try:
                connection.close()
            finally:
                self._decrement()
            return
        try:
            self.available.put_nowait((connection, time.monotonic()))
        except Full:
            try:
                connection.close()
            finally:
                self._decrement()


POSTGRES_POOL = None
POSTGRES_POOL_LOCK = threading.Lock()


def get_postgres_pool():
    global POSTGRES_POOL
    if POSTGRES_POOL is None:
        with POSTGRES_POOL_LOCK:
            if POSTGRES_POOL is None:
                POSTGRES_POOL = PostgresPool(
                    SUPABASE_DATABASE_URL,
                    max_size=bounded_env_number("DB_POOL_MAX", 8, 1, 20),
                    timeout=bounded_env_number("DB_POOL_TIMEOUT", 10, 1, 60),
                    max_idle=bounded_env_number("DB_POOL_MAX_IDLE", 300, 30, 3600),
                )
    return POSTGRES_POOL


def connect():
    if USE_SUPABASE:
        pool = get_postgres_pool()
        return PostgresConnection(connection=pool.get(), pool=pool)
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    con.execute("PRAGMA busy_timeout=30000")
    con.execute("PRAGMA synchronous=FULL")
    con.execute("PRAGMA wal_autocheckpoint=1000")
    con.execute("PRAGMA journal_size_limit=67108864")
    return con


def lock_order_sql(con, order_id):
    """Serialize quantity-changing work for one order across concurrent users."""
    if USE_SUPABASE:
        con.execute("SELECT id FROM orders WHERE id=? FOR UPDATE", (order_id,)).fetchone()
    else:
        con.execute("BEGIN IMMEDIATE")


def lock_packaging_sql(con, packaging_id):
    """Prevent two users from shipping or moving the same packages at once."""
    if USE_SUPABASE:
        con.execute("SELECT id FROM packaging WHERE id=? FOR UPDATE", (packaging_id,)).fetchone()
    else:
        con.execute("BEGIN IMMEDIATE")


def lock_production_sql(con, production_id):
    if USE_SUPABASE:
        con.execute("SELECT id FROM production WHERE id=? FOR UPDATE", (production_id,)).fetchone()
    else:
        con.execute("BEGIN IMMEDIATE")


def lock_s_number_sql(con, s_number):
    if USE_SUPABASE:
        con.execute("SELECT id FROM orders WHERE s_number=? ORDER BY id FOR UPDATE", (s_number,)).fetchall()
    else:
        con.execute("BEGIN IMMEDIATE")


def lock_order_sequence_sql(con):
    if USE_SUPABASE:
        con.execute("SELECT pg_advisory_xact_lock(714003)").fetchone()
    else:
        con.execute("BEGIN IMMEDIATE")


def lock_bulk_production_sql(con):
    if USE_SUPABASE:
        con.execute("SELECT pg_advisory_xact_lock(714004)").fetchone()
    else:
        con.execute("BEGIN IMMEDIATE")


SUPABASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
  id BIGSERIAL PRIMARY KEY,
  s_number TEXT NOT NULL DEFAULT '',
  po_number TEXT NOT NULL DEFAULT '',
  brand TEXT NOT NULL,
  color TEXT NOT NULL,
  unit TEXT NOT NULL DEFAULT 'gallons',
  gallons NUMERIC NOT NULL CHECK(gallons > 0),
  ship_month TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
ALTER TABLE orders ADD COLUMN IF NOT EXISTS po_number TEXT NOT NULL DEFAULT '';
CREATE TABLE IF NOT EXISTS production (
  id BIGSERIAL PRIMARY KEY,
  order_id BIGINT REFERENCES orders(id),
  brand TEXT NOT NULL,
  color TEXT NOT NULL,
  unit TEXT NOT NULL DEFAULT 'gallons',
  batch_number TEXT NOT NULL UNIQUE,
  gallons NUMERIC NOT NULL CHECK(gallons > 0),
  produced_at TEXT NOT NULL,
  quality_status TEXT NOT NULL DEFAULT 'ready',
  incubation_started_at TEXT NOT NULL DEFAULT '',
  incubation_due_at TEXT NOT NULL DEFAULT '',
  quality_decided_at TEXT NOT NULL DEFAULT '',
  quality_decided_by TEXT NOT NULL DEFAULT '',
  quality_notes TEXT NOT NULL DEFAULT '',
  bulk_purpose TEXT NOT NULL DEFAULT '',
  bulk_allocated NUMERIC NOT NULL DEFAULT 0,
  rework_of_id BIGINT REFERENCES production(id)
);
ALTER TABLE production ADD COLUMN IF NOT EXISTS quality_status TEXT NOT NULL DEFAULT 'ready';
ALTER TABLE production ADD COLUMN IF NOT EXISTS incubation_started_at TEXT NOT NULL DEFAULT '';
ALTER TABLE production ADD COLUMN IF NOT EXISTS incubation_due_at TEXT NOT NULL DEFAULT '';
ALTER TABLE production ADD COLUMN IF NOT EXISTS quality_decided_at TEXT NOT NULL DEFAULT '';
ALTER TABLE production ADD COLUMN IF NOT EXISTS quality_decided_by TEXT NOT NULL DEFAULT '';
ALTER TABLE production ADD COLUMN IF NOT EXISTS quality_notes TEXT NOT NULL DEFAULT '';
ALTER TABLE production ADD COLUMN IF NOT EXISTS bulk_purpose TEXT NOT NULL DEFAULT '';
ALTER TABLE production ADD COLUMN IF NOT EXISTS bulk_allocated NUMERIC NOT NULL DEFAULT 0;
ALTER TABLE production ADD COLUMN IF NOT EXISTS rework_of_id BIGINT REFERENCES production(id);
CREATE TABLE IF NOT EXISTS production_qc_overrides (
  id BIGSERIAL PRIMARY KEY,
  production_id BIGINT NOT NULL REFERENCES production(id),
  previous_status TEXT NOT NULL,
  new_status TEXT NOT NULL,
  scheduled_due_at TEXT NOT NULL,
  reason TEXT NOT NULL,
  decided_by TEXT NOT NULL,
  decided_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_production_qc_overrides_batch ON production_qc_overrides(production_id,id);
ALTER TABLE production_qc_overrides ENABLE ROW LEVEL SECURITY;
CREATE TABLE IF NOT EXISTS catalog (
  id BIGSERIAL PRIMARY KEY,
  category TEXT NOT NULL,
  color TEXT NOT NULL,
  unit TEXT NOT NULL DEFAULT 'gallons' CHECK(unit IN ('gallons','qty')),
  UNIQUE(category,color)
);
CREATE TABLE IF NOT EXISTS contacts (
  id BIGSERIAL PRIMARY KEY,
  name TEXT NOT NULL,
  email TEXT NOT NULL UNIQUE,
  active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS order_recipients (
  order_id BIGINT NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
  contact_id BIGINT NOT NULL REFERENCES contacts(id),
  PRIMARY KEY(order_id,contact_id)
);
CREATE TABLE IF NOT EXISTS users (
  id BIGSERIAL PRIMARY KEY,
  username TEXT NOT NULL UNIQUE,
  email TEXT,
  display_name TEXT NOT NULL,
  password_hash TEXT NOT NULL,
  role TEXT NOT NULL CHECK(role IN ('admin','manager','team')),
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS s_number_status (
  s_number TEXT PRIMARY KEY,
  done_at TEXT,
  done_by TEXT,
  ready_at TEXT,
  ready_by TEXT,
  shipped_at TEXT,
  shipped_by TEXT
);
CREATE TABLE IF NOT EXISTS packaging (
  id BIGSERIAL PRIMARY KEY,
  order_id BIGINT NOT NULL REFERENCES orders(id),
  s_number TEXT NOT NULL,
  brand TEXT NOT NULL,
  color TEXT NOT NULL,
  package_type TEXT NOT NULL,
  package_size NUMERIC NOT NULL CHECK(package_size > 0),
  package_count INTEGER NOT NULL CHECK(package_count > 0),
  gallons NUMERIC NOT NULL CHECK(gallons > 0),
  packaged_at TEXT NOT NULL,
  created_by TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS shipments (
  id BIGSERIAL PRIMARY KEY,
  packaging_id BIGINT NOT NULL REFERENCES packaging(id),
  s_number TEXT NOT NULL,
  brand TEXT NOT NULL,
  color TEXT NOT NULL,
  package_type TEXT NOT NULL,
  package_size NUMERIC NOT NULL CHECK(package_size > 0),
  package_count INTEGER NOT NULL CHECK(package_count > 0),
  gallons NUMERIC NOT NULL CHECK(gallons > 0),
  shipped_at TEXT NOT NULL,
  shipped_by TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS stock_packaging (
  id BIGSERIAL PRIMARY KEY,
  production_id BIGINT NOT NULL REFERENCES production(id),
  brand TEXT NOT NULL,
  color TEXT NOT NULL,
  package_type TEXT NOT NULL,
  package_size NUMERIC NOT NULL CHECK(package_size > 0),
  package_count INTEGER NOT NULL CHECK(package_count > 0),
  gallons NUMERIC NOT NULL CHECK(gallons > 0),
  packaged_at TEXT NOT NULL,
  created_by TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS stock_needs (
  id BIGSERIAL PRIMARY KEY,
  brand TEXT NOT NULL,
  color TEXT NOT NULL,
  unit TEXT NOT NULL CHECK(unit IN ('gallons','qty')),
  amount NUMERIC NOT NULL CHECK(amount > 0),
  note TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  created_by TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS stock_need_fulfillments (
  id BIGSERIAL PRIMARY KEY,
  need_id BIGINT NOT NULL REFERENCES stock_needs(id) ON DELETE CASCADE,
  production_id BIGINT NOT NULL REFERENCES production(id) ON DELETE CASCADE,
  amount NUMERIC NOT NULL CHECK(amount > 0),
  created_at TEXT NOT NULL,
  UNIQUE(need_id,production_id)
);
CREATE TABLE IF NOT EXISTS packaging_requirements (
  id BIGSERIAL PRIMARY KEY,
  order_id BIGINT NOT NULL REFERENCES orders(id),
  s_number TEXT NOT NULL,
  brand TEXT NOT NULL,
  color TEXT NOT NULL,
  package_type TEXT NOT NULL,
  package_size NUMERIC NOT NULL CHECK(package_size > 0),
  required_count INTEGER NOT NULL CHECK(required_count > 0),
  required_gallons NUMERIC NOT NULL CHECK(required_gallons > 0),
  created_at TEXT NOT NULL,
  created_by TEXT NOT NULL DEFAULT '',
  UNIQUE(order_id,package_type)
);
CREATE TABLE IF NOT EXISTS labor_rates (
  id BIGSERIAL PRIMARY KEY,
  labor_name TEXT NOT NULL,
  hourly_rate NUMERIC NOT NULL CHECK(hourly_rate >= 0),
  effective_from TEXT NOT NULL,
  active INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  created_by TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS labor_entries (
  id BIGSERIAL PRIMARY KEY,
  order_id BIGINT REFERENCES orders(id),
  production_id BIGINT REFERENCES production(id),
  s_number TEXT NOT NULL,
  brand TEXT NOT NULL,
  color TEXT NOT NULL,
  unit TEXT NOT NULL CHECK(unit IN ('gallons','qty')),
  process_name TEXT NOT NULL,
  labor_name TEXT NOT NULL,
  hours NUMERIC NOT NULL CHECK(hours > 0),
  hourly_rate NUMERIC NOT NULL CHECK(hourly_rate >= 0),
  labor_cost NUMERIC NOT NULL CHECK(labor_cost >= 0),
  worked_at TEXT NOT NULL,
  notes TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  created_by TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS production_targets (
  id INTEGER PRIMARY KEY CHECK(id = 1),
  daily_gallons NUMERIC NOT NULL DEFAULT 0 CHECK(daily_gallons >= 0),
  daily_qty NUMERIC NOT NULL DEFAULT 0 CHECK(daily_qty >= 0),
  updated_at TEXT NOT NULL DEFAULT '',
  updated_by TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS daily_production_targets (
  id BIGSERIAL PRIMARY KEY,
  target_date TEXT NOT NULL,
  brand TEXT NOT NULL DEFAULT '',
  color TEXT NOT NULL DEFAULT '',
  unit TEXT NOT NULL CHECK(unit IN ('gallons','qty')),
  amount NUMERIC NOT NULL CHECK(amount >= 0),
  updated_at TEXT NOT NULL,
  updated_by TEXT NOT NULL DEFAULT '',
  UNIQUE(target_date,brand,color,unit)
);
CREATE INDEX IF NOT EXISTS idx_daily_targets_date ON daily_production_targets(target_date);
CREATE INDEX IF NOT EXISTS idx_orders_ship_month ON orders(ship_month);
CREATE INDEX IF NOT EXISTS idx_orders_s_number ON orders(s_number);
CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at);
CREATE INDEX IF NOT EXISTS idx_production_order ON production(order_id);
CREATE INDEX IF NOT EXISTS idx_production_date ON production(produced_at);
CREATE INDEX IF NOT EXISTS idx_production_quality ON production(quality_status,incubation_due_at);
CREATE INDEX IF NOT EXISTS idx_packaging_s_number ON packaging(s_number);
CREATE INDEX IF NOT EXISTS idx_packaging_order ON packaging(order_id);
CREATE INDEX IF NOT EXISTS idx_packaging_order_type ON packaging(order_id,package_type);
CREATE INDEX IF NOT EXISTS idx_shipments_packaging ON shipments(packaging_id);
CREATE INDEX IF NOT EXISTS idx_shipments_s_number ON shipments(s_number);
CREATE INDEX IF NOT EXISTS idx_shipments_date ON shipments(shipped_at);
CREATE INDEX IF NOT EXISTS idx_order_recipients_order ON order_recipients(order_id);
CREATE INDEX IF NOT EXISTS idx_stock_packaging_production ON stock_packaging(production_id);
CREATE INDEX IF NOT EXISTS idx_stock_needs_product ON stock_needs(brand,color,unit,created_at);
CREATE INDEX IF NOT EXISTS idx_stock_need_fulfillments_need ON stock_need_fulfillments(need_id);
CREATE INDEX IF NOT EXISTS idx_stock_need_fulfillments_production ON stock_need_fulfillments(production_id);
CREATE INDEX IF NOT EXISTS idx_packaging_requirements_s ON packaging_requirements(s_number);
CREATE INDEX IF NOT EXISTS idx_labor_rates_name ON labor_rates(labor_name,effective_from);
CREATE INDEX IF NOT EXISTS idx_labor_entries_order ON labor_entries(order_id,worked_at);
CREATE INDEX IF NOT EXISTS idx_labor_entries_s ON labor_entries(s_number,worked_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email) WHERE email IS NOT NULL AND email<>'';
ALTER TABLE orders ENABLE ROW LEVEL SECURITY;
ALTER TABLE production ENABLE ROW LEVEL SECURITY;
ALTER TABLE catalog ENABLE ROW LEVEL SECURITY;
ALTER TABLE contacts ENABLE ROW LEVEL SECURITY;
ALTER TABLE order_recipients ENABLE ROW LEVEL SECURITY;
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE s_number_status ENABLE ROW LEVEL SECURITY;
ALTER TABLE packaging ENABLE ROW LEVEL SECURITY;
ALTER TABLE shipments ENABLE ROW LEVEL SECURITY;
ALTER TABLE stock_packaging ENABLE ROW LEVEL SECURITY;
ALTER TABLE stock_needs ENABLE ROW LEVEL SECURITY;
ALTER TABLE stock_need_fulfillments ENABLE ROW LEVEL SECURITY;
ALTER TABLE packaging_requirements ENABLE ROW LEVEL SECURITY;
ALTER TABLE labor_rates ENABLE ROW LEVEL SECURITY;
ALTER TABLE labor_entries ENABLE ROW LEVEL SECURITY;
ALTER TABLE production_targets ENABLE ROW LEVEL SECURITY;
ALTER TABLE daily_production_targets ENABLE ROW LEVEL SECURITY;
"""


def initialize_supabase():
    with closing(connect()) as con:
        for statement in SUPABASE_SCHEMA.split(";"):
            if statement.strip():
                con.execute(statement)
        remove_provisional_stock_credit_sql(con)
        if not con.execute("SELECT 1 FROM users WHERE role='admin' AND active=1").fetchone():
            seed_email = APP_ADMIN_EMAIL if valid_email(APP_ADMIN_EMAIL) else (APP_USERNAME.lower() if valid_email(APP_USERNAME) else "")
            seed_username = seed_email or APP_USERNAME
            con.execute("INSERT INTO users(username,email,display_name,password_hash,role,active,created_at) VALUES(?,?,?,?,?,1,?)",
                        (seed_username, seed_email, "Administrator", hash_password(APP_PASSWORD), "admin", datetime.now().isoformat(timespec="seconds")))
        for category, color, unit in (("Default", "White", "gallons"), ("Default", "Black", "gallons")):
            if not con.execute("SELECT 1 FROM catalog WHERE category=? AND color=?", (category, color)).fetchone():
                con.execute("INSERT INTO catalog(category,color,unit) VALUES(?,?,?)", (category, color, unit))
        if not con.execute("SELECT 1 FROM production_targets WHERE id=1").fetchone():
            con.execute("INSERT INTO production_targets(id,daily_gallons,daily_qty,updated_at,updated_by) VALUES(1,0,0,?,?)",
                        (datetime.now().isoformat(timespec="seconds"), "System"))
        con.commit()


def initialize():
    if USE_SUPABASE:
        initialize_supabase()
        return
    with closing(connect()) as con:
        con.execute("PRAGMA journal_mode=WAL")
        con.executescript("""
        CREATE TABLE IF NOT EXISTS orders (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          s_number TEXT NOT NULL DEFAULT '',
          po_number TEXT NOT NULL DEFAULT '',
          brand TEXT NOT NULL,
          color TEXT NOT NULL,
          unit TEXT NOT NULL DEFAULT 'gallons',
          gallons REAL NOT NULL CHECK(gallons > 0),
          ship_month TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS production (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          order_id INTEGER REFERENCES orders(id),
          brand TEXT NOT NULL,
          color TEXT NOT NULL,
          unit TEXT NOT NULL DEFAULT 'gallons',
          batch_number TEXT NOT NULL UNIQUE,
          gallons REAL NOT NULL CHECK(gallons > 0),
          produced_at TEXT NOT NULL,
          quality_status TEXT NOT NULL DEFAULT 'ready',
          incubation_started_at TEXT NOT NULL DEFAULT '',
          incubation_due_at TEXT NOT NULL DEFAULT '',
          quality_decided_at TEXT NOT NULL DEFAULT '',
          quality_decided_by TEXT NOT NULL DEFAULT '',
          quality_notes TEXT NOT NULL DEFAULT '',
          bulk_purpose TEXT NOT NULL DEFAULT '',
          rework_of_id INTEGER REFERENCES production(id)
        );
        CREATE TABLE IF NOT EXISTS catalog (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          category TEXT NOT NULL,
          color TEXT NOT NULL,
          unit TEXT NOT NULL DEFAULT 'gallons' CHECK(unit IN ('gallons','qty')),
          UNIQUE(category,color)
        );
        CREATE TABLE IF NOT EXISTS contacts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          name TEXT NOT NULL,
          email TEXT NOT NULL UNIQUE,
          active INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS order_recipients (
          order_id INTEGER NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
          contact_id INTEGER NOT NULL REFERENCES contacts(id),
          PRIMARY KEY(order_id,contact_id)
        );
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          username TEXT NOT NULL UNIQUE,
          email TEXT,
          display_name TEXT NOT NULL,
          password_hash TEXT NOT NULL,
          role TEXT NOT NULL CHECK(role IN ('admin','manager','team')),
          active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS s_number_status (
          s_number TEXT PRIMARY KEY,
          done_at TEXT,
          done_by TEXT,
          ready_at TEXT,
          ready_by TEXT,
          shipped_at TEXT,
          shipped_by TEXT
        );
        CREATE TABLE IF NOT EXISTS packaging (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          order_id INTEGER NOT NULL REFERENCES orders(id),
          s_number TEXT NOT NULL,
          brand TEXT NOT NULL,
          color TEXT NOT NULL,
          package_type TEXT NOT NULL,
          package_size REAL NOT NULL CHECK(package_size > 0),
          package_count INTEGER NOT NULL CHECK(package_count > 0),
          gallons REAL NOT NULL CHECK(gallons > 0),
          packaged_at TEXT NOT NULL,
          created_by TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS shipments (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          packaging_id INTEGER NOT NULL REFERENCES packaging(id),
          s_number TEXT NOT NULL,
          brand TEXT NOT NULL,
          color TEXT NOT NULL,
          package_type TEXT NOT NULL,
          package_size REAL NOT NULL CHECK(package_size > 0),
          package_count INTEGER NOT NULL CHECK(package_count > 0),
          gallons REAL NOT NULL CHECK(gallons > 0),
          shipped_at TEXT NOT NULL,
          shipped_by TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS stock_packaging (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          production_id INTEGER NOT NULL REFERENCES production(id),
          brand TEXT NOT NULL,
          color TEXT NOT NULL,
          package_type TEXT NOT NULL,
          package_size REAL NOT NULL CHECK(package_size > 0),
          package_count INTEGER NOT NULL CHECK(package_count > 0),
          gallons REAL NOT NULL CHECK(gallons > 0),
          packaged_at TEXT NOT NULL,
          created_by TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS stock_needs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          brand TEXT NOT NULL,
          color TEXT NOT NULL,
          unit TEXT NOT NULL CHECK(unit IN ('gallons','qty')),
          amount REAL NOT NULL CHECK(amount > 0),
          note TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          created_by TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS stock_need_fulfillments (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          need_id INTEGER NOT NULL REFERENCES stock_needs(id) ON DELETE CASCADE,
          production_id INTEGER NOT NULL REFERENCES production(id) ON DELETE CASCADE,
          amount REAL NOT NULL CHECK(amount > 0),
          created_at TEXT NOT NULL,
          UNIQUE(need_id,production_id)
        );
        CREATE TABLE IF NOT EXISTS packaging_requirements (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          order_id INTEGER NOT NULL REFERENCES orders(id),
          s_number TEXT NOT NULL,
          brand TEXT NOT NULL,
          color TEXT NOT NULL,
          package_type TEXT NOT NULL,
          package_size REAL NOT NULL CHECK(package_size > 0),
          required_count INTEGER NOT NULL CHECK(required_count > 0),
          required_gallons REAL NOT NULL CHECK(required_gallons > 0),
          created_at TEXT NOT NULL,
          created_by TEXT NOT NULL DEFAULT '',
          UNIQUE(order_id,package_type)
        );
        CREATE TABLE IF NOT EXISTS labor_rates (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          labor_name TEXT NOT NULL,
          hourly_rate REAL NOT NULL CHECK(hourly_rate >= 0),
          effective_from TEXT NOT NULL,
          active INTEGER NOT NULL DEFAULT 1,
          created_at TEXT NOT NULL,
          created_by TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS labor_entries (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          order_id INTEGER REFERENCES orders(id),
          production_id INTEGER REFERENCES production(id),
          s_number TEXT NOT NULL,
          brand TEXT NOT NULL,
          color TEXT NOT NULL,
          unit TEXT NOT NULL CHECK(unit IN ('gallons','qty')),
          process_name TEXT NOT NULL,
          labor_name TEXT NOT NULL,
          hours REAL NOT NULL CHECK(hours > 0),
          hourly_rate REAL NOT NULL CHECK(hourly_rate >= 0),
          labor_cost REAL NOT NULL CHECK(labor_cost >= 0),
          worked_at TEXT NOT NULL,
          notes TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          created_by TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS production_targets (
          id INTEGER PRIMARY KEY CHECK(id = 1),
          daily_gallons REAL NOT NULL DEFAULT 0 CHECK(daily_gallons >= 0),
          daily_qty REAL NOT NULL DEFAULT 0 CHECK(daily_qty >= 0),
          updated_at TEXT NOT NULL DEFAULT '',
          updated_by TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS daily_production_targets (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          target_date TEXT NOT NULL,
          brand TEXT NOT NULL DEFAULT '',
          color TEXT NOT NULL DEFAULT '',
          unit TEXT NOT NULL CHECK(unit IN ('gallons','qty')),
          amount REAL NOT NULL CHECK(amount >= 0),
          updated_at TEXT NOT NULL,
          updated_by TEXT NOT NULL DEFAULT '',
          UNIQUE(target_date,brand,color,unit)
        );
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_packaging_s_number ON packaging(s_number)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_packaging_order ON packaging(order_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_packaging_order_type ON packaging(order_id,package_type)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_shipments_packaging ON shipments(packaging_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_shipments_s_number ON shipments(s_number)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_shipments_date ON shipments(shipped_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_order_recipients_order ON order_recipients(order_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_orders_s_number ON orders(s_number)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_orders_created ON orders(created_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_production_order ON production(order_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_production_date ON production(produced_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_stock_packaging_production ON stock_packaging(production_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_stock_needs_product ON stock_needs(brand,color,unit,created_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_stock_need_fulfillments_need ON stock_need_fulfillments(need_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_daily_targets_date ON daily_production_targets(target_date)")
        if not con.execute("SELECT 1 FROM production_targets WHERE id=1").fetchone():
            con.execute("INSERT INTO production_targets(id,daily_gallons,daily_qty,updated_at,updated_by) VALUES(1,0,0,?,?)",
                        (datetime.now().isoformat(timespec="seconds"), "System"))
        con.execute("CREATE INDEX IF NOT EXISTS idx_stock_need_fulfillments_production ON stock_need_fulfillments(production_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_packaging_requirements_s ON packaging_requirements(s_number)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_labor_rates_name ON labor_rates(labor_name,effective_from)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_labor_entries_order ON labor_entries(order_id,worked_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_labor_entries_s ON labor_entries(s_number,worked_at)")
        labor_columns = {x[1]: x for x in con.execute("PRAGMA table_info(labor_entries)")}
        if "production_id" not in labor_columns or labor_columns.get("order_id",(None,None,None,1))[3]:
            con.execute("PRAGMA foreign_keys=OFF")
            con.executescript("""
            ALTER TABLE labor_entries RENAME TO labor_entries_old;
            CREATE TABLE labor_entries (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              order_id INTEGER REFERENCES orders(id),
              production_id INTEGER REFERENCES production(id),
              s_number TEXT NOT NULL, brand TEXT NOT NULL, color TEXT NOT NULL,
              unit TEXT NOT NULL CHECK(unit IN ('gallons','qty')), process_name TEXT NOT NULL,
              labor_name TEXT NOT NULL, hours REAL NOT NULL CHECK(hours > 0),
              hourly_rate REAL NOT NULL CHECK(hourly_rate >= 0), labor_cost REAL NOT NULL CHECK(labor_cost >= 0),
              worked_at TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, created_by TEXT NOT NULL DEFAULT ''
            );
            INSERT INTO labor_entries(id,order_id,production_id,s_number,brand,color,unit,process_name,labor_name,hours,hourly_rate,labor_cost,worked_at,notes,created_at,created_by)
              SELECT id,order_id,NULL,s_number,brand,color,unit,process_name,labor_name,hours,hourly_rate,labor_cost,worked_at,notes,created_at,created_by FROM labor_entries_old;
            DROP TABLE labor_entries_old;
            CREATE INDEX idx_labor_entries_order ON labor_entries(order_id,worked_at);
            CREATE INDEX idx_labor_entries_s ON labor_entries(s_number,worked_at);
            """)
            con.execute("PRAGMA foreign_keys=ON")
        status_columns = {x[1] for x in con.execute("PRAGMA table_info(s_number_status)")}
        if "ready_at" not in status_columns: con.execute("ALTER TABLE s_number_status ADD COLUMN ready_at TEXT")
        if "ready_by" not in status_columns: con.execute("ALTER TABLE s_number_status ADD COLUMN ready_by TEXT")
        con.execute("""INSERT INTO shipments(packaging_id,s_number,brand,color,package_type,package_size,package_count,gallons,shipped_at,shipped_by)
          SELECT k.id,k.s_number,k.brand,k.color,k.package_type,k.package_size,k.package_count,k.gallons,s.shipped_at,COALESCE(s.shipped_by,'')
          FROM packaging k JOIN s_number_status s ON s.s_number=k.s_number
          WHERE s.shipped_at IS NOT NULL AND NOT EXISTS(SELECT 1 FROM shipments h WHERE h.packaging_id=k.id)""")
        user_columns = {x[1] for x in con.execute("PRAGMA table_info(users)")}
        if "email" not in user_columns:
            con.execute("ALTER TABLE users ADD COLUMN email TEXT")
        con.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email) WHERE email IS NOT NULL AND email<>''")
        con.execute("UPDATE users SET email=lower(username) WHERE (email IS NULL OR email='') AND username LIKE '%@%'")
        if not con.execute("SELECT 1 FROM users WHERE role='admin' AND active=1").fetchone():
            seed_email = APP_ADMIN_EMAIL if valid_email(APP_ADMIN_EMAIL) else (APP_USERNAME.lower() if valid_email(APP_USERNAME) else "")
            seed_username = seed_email or APP_USERNAME
            con.execute("INSERT INTO users(username,email,display_name,password_hash,role,active,created_at) VALUES(?,?,?,?,?,1,?)",
                        (seed_username, seed_email, "Administrator", hash_password(APP_PASSWORD), "admin", datetime.now().isoformat(timespec="seconds")))
        order_columns = {x[1] for x in con.execute("PRAGMA table_info(orders)")}
        if "s_number" not in order_columns:
            con.execute("ALTER TABLE orders ADD COLUMN s_number TEXT NOT NULL DEFAULT ''")
        if "unit" not in order_columns:
            con.execute("ALTER TABLE orders ADD COLUMN unit TEXT NOT NULL DEFAULT 'gallons'")
        if "ship_month" not in order_columns:
            con.execute("ALTER TABLE orders ADD COLUMN ship_month TEXT NOT NULL DEFAULT ''")
        if "po_number" not in order_columns:
            con.execute("ALTER TABLE orders ADD COLUMN po_number TEXT NOT NULL DEFAULT ''")
        con.execute("CREATE INDEX IF NOT EXISTS idx_orders_ship_month ON orders(ship_month)")
        production_columns = {x[1] for x in con.execute("PRAGMA table_info(production)")}
        if "unit" not in production_columns:
            con.execute("ALTER TABLE production ADD COLUMN unit TEXT NOT NULL DEFAULT 'gallons'")
        catalog_columns = {x[1] for x in con.execute("PRAGMA table_info(catalog)")}
        if "unit" not in catalog_columns:
            con.execute("ALTER TABLE catalog ADD COLUMN unit TEXT NOT NULL DEFAULT 'gallons'")
        con.execute("""INSERT OR IGNORE INTO catalog(category,color,unit)
          SELECT brand,color,unit FROM orders WHERE trim(brand)<>'' AND trim(color)<>''""")
        con.execute("""INSERT OR IGNORE INTO catalog(category,color,unit)
          SELECT brand,color,unit FROM production WHERE trim(brand)<>'' AND trim(color)<>''""")
        order_id_column = next(x for x in con.execute("PRAGMA table_info(production)") if x[1] == "order_id")
        if order_id_column[3]:
            con.execute("PRAGMA foreign_keys=OFF")
            con.executescript("""
            ALTER TABLE production RENAME TO production_old;
            CREATE TABLE production (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              order_id INTEGER REFERENCES orders(id),
              brand TEXT NOT NULL,
              color TEXT NOT NULL,
              unit TEXT NOT NULL DEFAULT 'gallons',
              batch_number TEXT NOT NULL UNIQUE,
              gallons REAL NOT NULL CHECK(gallons > 0),
              produced_at TEXT NOT NULL,
              quality_status TEXT NOT NULL DEFAULT 'ready',
              incubation_started_at TEXT NOT NULL DEFAULT '',
              incubation_due_at TEXT NOT NULL DEFAULT '',
              quality_decided_at TEXT NOT NULL DEFAULT '',
              quality_decided_by TEXT NOT NULL DEFAULT '',
              quality_notes TEXT NOT NULL DEFAULT '',
              bulk_purpose TEXT NOT NULL DEFAULT '',
              rework_of_id INTEGER REFERENCES production(id)
            );
            INSERT INTO production(id,order_id,brand,color,unit,batch_number,gallons,produced_at)
              SELECT id,order_id,brand,color,unit,batch_number,gallons,produced_at FROM production_old;
            DROP TABLE production_old;
            """)
            con.execute("PRAGMA foreign_keys=ON")
        production_columns = {x[1] for x in con.execute("PRAGMA table_info(production)")}
        for column, definition in (
            ("quality_status", "TEXT NOT NULL DEFAULT 'ready'"),
            ("incubation_started_at", "TEXT NOT NULL DEFAULT ''"),
            ("incubation_due_at", "TEXT NOT NULL DEFAULT ''"),
            ("quality_decided_at", "TEXT NOT NULL DEFAULT ''"),
            ("quality_decided_by", "TEXT NOT NULL DEFAULT ''"),
            ("quality_notes", "TEXT NOT NULL DEFAULT ''"),
            ("bulk_purpose", "TEXT NOT NULL DEFAULT ''"),
            ("bulk_allocated", "REAL NOT NULL DEFAULT 0"),
            ("rework_of_id", "INTEGER REFERENCES production(id)"),
        ):
            if column not in production_columns:
                con.execute(f"ALTER TABLE production ADD COLUMN {column} {definition}")
        con.execute("CREATE INDEX IF NOT EXISTS idx_production_quality ON production(quality_status,incubation_due_at)")
        con.execute("""CREATE TABLE IF NOT EXISTS production_qc_overrides (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          production_id INTEGER NOT NULL REFERENCES production(id),
          previous_status TEXT NOT NULL, new_status TEXT NOT NULL, scheduled_due_at TEXT NOT NULL,
          reason TEXT NOT NULL, decided_by TEXT NOT NULL, decided_at TEXT NOT NULL
        )""")
        con.execute("CREATE INDEX IF NOT EXISTS idx_production_qc_overrides_batch ON production_qc_overrides(production_id,id)")
        remove_provisional_stock_credit_sql(con)
        con.commit()


def remove_provisional_stock_credit_sql(con):
    # Earlier versions recorded credits when incubation started. Clear only
    # those derived credits; production, QC, and labor records stay in place.
    con.execute("""DELETE FROM stock_need_fulfillments WHERE production_id IN
      (SELECT id FROM production WHERE bulk_purpose='stock' AND quality_status='incubating')""")


def refresh_s_number_shipped(con, s_number):
    totals = con.execute("""SELECT COALESCE(SUM(k.package_count),0) total,
      COALESCE((SELECT SUM(h.package_count) FROM shipments h WHERE h.s_number=?),0) shipped
      FROM packaging k WHERE k.s_number=?""", (s_number,s_number)).fetchone()
    if totals["total"] <= 0 or totals["shipped"] < totals["total"]:
        con.execute("UPDATE s_number_status SET shipped_at=NULL,shipped_by=NULL WHERE s_number=?", (s_number,))


def refresh_s_number_ready(con, s_number, actor="System"):
    orders = con.execute("SELECT id,unit,gallons FROM orders WHERE s_number=?", (s_number,)).fetchall()
    ready = bool(orders)
    for order in orders:
        produced = con.execute("SELECT COALESCE(SUM(gallons),0) FROM production WHERE order_id=?", (order["id"],)).fetchone()[0]
        if float(order["gallons"]) - float(produced) > .0001:
            ready = False; break
        requirements = con.execute("SELECT package_type,required_count,required_gallons FROM packaging_requirements WHERE order_id=?", (order["id"],)).fetchall()
        if order["unit"] == "gallons" or requirements:
            if not requirements or abs(sum(float(x["required_gallons"]) for x in requirements)-float(order["gallons"])) > .0001:
                ready = False; break
            for requirement in requirements:
                completed = con.execute("SELECT COALESCE(SUM(package_count),0) FROM packaging WHERE order_id=? AND package_type=?",
                                        (order["id"],requirement["package_type"])).fetchone()[0]
                if completed < requirement["required_count"]:
                    ready = False; break
            if not ready: break
    now = datetime.now().isoformat(timespec="seconds")
    if ready:
        con.execute("""INSERT INTO s_number_status(s_number,done_at,done_by,ready_at,ready_by) VALUES(?,?,?,?,?)
          ON CONFLICT(s_number) DO UPDATE SET done_at=COALESCE(s_number_status.done_at,excluded.done_at),
          done_by=COALESCE(s_number_status.done_by,excluded.done_by),ready_at=COALESCE(s_number_status.ready_at,excluded.ready_at),
          ready_by=COALESCE(s_number_status.ready_by,excluded.ready_by)""", (s_number,now,actor,now,actor))
    else:
        con.execute("UPDATE s_number_status SET done_at=NULL,done_by=NULL,ready_at=NULL,ready_by=NULL WHERE s_number=? AND shipped_at IS NULL", (s_number,))
    return ready


def stock_need_rows_sql(con):
    return [dict(x) for x in con.execute("""SELECT n.id,n.brand,n.color,n.unit,n.amount,n.note,n.created_at,n.created_by,
      ROUND(COALESCE((SELECT SUM(f.amount) FROM stock_need_fulfillments f WHERE f.need_id=n.id),0),2) fulfilled_amount,
      ROUND(CASE WHEN n.amount-COALESCE((SELECT SUM(f.amount) FROM stock_need_fulfillments f WHERE f.need_id=n.id),0)>0
        THEN n.amount-COALESCE((SELECT SUM(f.amount) FROM stock_need_fulfillments f WHERE f.need_id=n.id),0) ELSE 0 END,2) remaining
      FROM stock_needs n ORDER BY CASE WHEN n.amount-COALESCE((SELECT SUM(f.amount) FROM stock_need_fulfillments f WHERE f.need_id=n.id),0)>.0001 THEN 0 ELSE 1 END,
      n.created_at,n.id""").fetchall()]


def bulk_demand_rows_sql(con, purpose="orders"):
    """Report unmet need separately from the balance available to start mixing."""
    order_need = {
        (row["brand"], row["color"], row["unit"]): float(row["amount"])
        for row in con.execute("""SELECT brand,color,unit,ROUND(SUM(remaining),2) amount FROM (
          SELECT o.id,o.brand,o.color,o.unit,
            CASE WHEN o.gallons-COALESCE(SUM(p.gallons),0)>0
                 THEN o.gallons-COALESCE(SUM(p.gallons),0) ELSE 0 END remaining
          FROM orders o LEFT JOIN production p ON p.order_id=o.id
          LEFT JOIN s_number_status s ON s.s_number=o.s_number
          WHERE s.shipped_at IS NULL GROUP BY o.id
        ) GROUP BY brand,color,unit""").fetchall()
    }
    if purpose == "stock":
        order_need = {}
        for row in stock_need_rows_sql(con):
            key = (row["brand"], row["color"], row["unit"])
            order_need[key] = order_need.get(key, 0.0) + float(row["remaining"])
    pipeline = {}
    for row in con.execute("""SELECT brand,color,unit,quality_status,
        ROUND(SUM(gallons-bulk_allocated),2) amount FROM production
        WHERE order_id IS NULL AND bulk_purpose=? AND quality_status IN ('incubating','ready')
        GROUP BY brand,color,unit,quality_status""", (purpose,)).fetchall():
        pipeline.setdefault((row["brand"], row["color"], row["unit"]), {})[row["quality_status"]] = float(row["amount"])
    rows = []
    for key, required in order_need.items():
        states = pipeline.get(key, {})
        held = states.get("incubating", 0.0)
        # Released future stock is already reflected in stock_need_fulfillments.
        released = states.get("ready", 0.0) if purpose == "orders" else 0.0
        remaining = round(max(required-released, 0.0), 2)
        rows.append({"brand": key[0], "color": key[1], "unit": key[2],
                     "order_need": round(required, 2), "bulk_reserved": round(held, 2), "qc_released": released,
                     "available_to_start": round(max(remaining-held, 0.0), 2),
                     "gallons": remaining, "remaining": remaining})
    rows.sort(key=lambda row: float(row["remaining"]), reverse=True)
    return rows


def incubation_batches_sql(con):
    rows = [dict(row) for row in con.execute("""SELECT p.id,p.batch_number,p.brand,p.color,p.unit,p.gallons,
      p.produced_at,p.quality_status,p.incubation_started_at,p.incubation_due_at,p.quality_decided_at,
      p.quality_decided_by,p.quality_notes,p.bulk_purpose,p.rework_of_id,
      p.bulk_allocated,
      ROUND(p.gallons-p.bulk_allocated-COALESCE((SELECT SUM(k.gallons) FROM stock_packaging k WHERE k.production_id=p.id),0),2) unpackaged,
      (SELECT MIN(c.id) FROM production c WHERE c.rework_of_id=p.id) rework_child_id
      FROM production p WHERE p.order_id IS NULL AND p.incubation_started_at<>''
      ORDER BY CASE p.quality_status WHEN 'incubating' THEN 0 WHEN 'rework' THEN 1 WHEN 'ready' THEN 2 ELSE 3 END,
      p.produced_at DESC,p.id DESC LIMIT 250""").fetchall()]
    for row in rows:
        started = incubation_datetime(row["incubation_started_at"])
        due = incubation_datetime(row["incubation_due_at"])
        row["incubation_hours"] = round(max((due-started).total_seconds()/3600,0),4) if started and due else None
        row["incubation_due_utc"] = due.isoformat() if due else ""
    return rows


def eligible_legacy_batches_sql(con, search=""):
    return [dict(row) for row in con.execute("""SELECT p.id,p.batch_number,p.brand,p.color,p.unit,p.gallons,o.s_number
      FROM production p LEFT JOIN orders o ON o.id=p.order_id LEFT JOIN s_number_status s ON s.s_number=o.s_number
      WHERE p.incubation_started_at='' AND lower(p.batch_number) LIKE ?
        AND s.ready_at IS NULL AND s.done_at IS NULL AND s.shipped_at IS NULL
        AND NOT EXISTS (SELECT 1 FROM stock_packaging k WHERE k.production_id=p.id)
        AND NOT EXISTS (SELECT 1 FROM packaging k WHERE k.order_id=p.order_id)
      ORDER BY p.produced_at DESC,p.id DESC LIMIT 100""", ("%"+search.lower()+"%",)).fetchall()]


def incubation_datetime(value):
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc) if value else None
    except ValueError:
        return None


def incubation_times(payload, started=None):
    hours = float(payload.get("incubation_hours", 24))
    if not isfinite(hours) or not 0 <= hours <= 720:
        raise ValueError("Incubation must be between 0 and 720 hours.")
    started = started if started is not None else datetime.now(timezone.utc)
    return started.isoformat(timespec="seconds"), (started+timedelta(hours=hours)).isoformat(timespec="seconds")


def allocate_stock_need_sql(con, production_id, brand, color, unit, amount):
    available=float(amount); allocated=0.0; now=datetime.now().isoformat(timespec="seconds")
    if USE_SUPABASE:
        con.execute("SELECT id FROM stock_needs WHERE brand=? AND color=? AND unit=? ORDER BY id FOR UPDATE",
                    (brand,color,unit)).fetchall()
    needs=con.execute("""SELECT n.id,n.amount-COALESCE((SELECT SUM(f.amount) FROM stock_need_fulfillments f WHERE f.need_id=n.id),0) remaining
      FROM stock_needs n WHERE n.brand=? AND n.color=? AND n.unit=? AND
      n.amount-COALESCE((SELECT SUM(f.amount) FROM stock_need_fulfillments f WHERE f.need_id=n.id),0)>.0001
      ORDER BY n.created_at,n.id""",(brand,color,unit)).fetchall()
    for need in needs:
        moved=round(min(available,float(need["remaining"])),2)
        if moved<=0: continue
        con.execute("""INSERT INTO stock_need_fulfillments(need_id,production_id,amount,created_at) VALUES(?,?,?,?)
          ON CONFLICT(need_id,production_id) DO UPDATE SET amount=stock_need_fulfillments.amount+excluded.amount""",
          (need["id"],production_id,moved,now))
        available=round(available-moved,2); allocated=round(allocated+moved,2)
        if available<=.0001: break
    return allocated


def release_stock_need_sql(con, production_id, amount):
    remaining=float(amount); released=0.0
    rows=con.execute("SELECT id,amount FROM stock_need_fulfillments WHERE production_id=? ORDER BY id DESC",(production_id,)).fetchall()
    for row in rows:
        moved=round(min(remaining,float(row["amount"])),2)
        if moved<=0: continue
        balance=round(float(row["amount"])-moved,2)
        if balance<=.0001: con.execute("DELETE FROM stock_need_fulfillments WHERE id=?",(row["id"],))
        else: con.execute("UPDATE stock_need_fulfillments SET amount=? WHERE id=?",(balance,row["id"]))
        remaining=round(remaining-moved,2); released=round(released+moved,2)
        if remaining<=.0001: break
    return released


def _date_range_sql(column, start="", end=""):
    clauses, params = [], []
    if start:
        clauses.append(f"{column}>=?"); params.append(start)
    if end:
        clauses.append(f"{column}<?"); params.append(end)
    return (" WHERE " + " AND ".join(clauses)) if clauses else "", params


def _period_metrics_sql(con, start="", end=""):
    production_where, production_params = _date_range_sql("produced_at", start, end)
    order_where, order_params = _date_range_sql("created_at", start, end)
    labor_where, labor_params = _date_range_sql("worked_at", start, end)
    package_where, package_params = _date_range_sql("k.packaged_at", start, end)
    shipment_where, shipment_params = _date_range_sql("h.shipped_at", start, end)
    result = {"produced_gallons": 0.0, "produced_qty": 0.0, "required_gallons": 0.0,
      "required_qty": 0.0, "packaged_gallons": 0.0, "packaged_qty": 0.0,
      "shipped_gallons": 0.0, "shipped_qty": 0.0, "labor_hours": 0.0,
      "labor_cost": 0.0, "batches": 0, "orders": 0, "packages": 0, "shipped_packages": 0}
    for row in con.execute(f"SELECT unit,COALESCE(SUM(gallons-bulk_allocated),0) amount,COUNT(*) batches FROM production{production_where} GROUP BY unit", production_params).fetchall():
        suffix = "qty" if row["unit"] == "qty" else "gallons"
        result[f"produced_{suffix}"] = round(float(row["amount"]), 2); result["batches"] += int(row["batches"])
    for row in con.execute(f"SELECT unit,COALESCE(SUM(gallons),0) amount,COUNT(*) orders FROM orders{order_where} GROUP BY unit", order_params).fetchall():
        suffix = "qty" if row["unit"] == "qty" else "gallons"
        result[f"required_{suffix}"] = round(float(row["amount"]), 2); result["orders"] += int(row["orders"])
    labor = con.execute(f"SELECT COALESCE(SUM(hours),0) hours,COALESCE(SUM(labor_cost),0) cost FROM labor_entries{labor_where}", labor_params).fetchone()
    result["labor_hours"] = round(float(labor["hours"]), 2); result["labor_cost"] = round(float(labor["cost"]), 2)
    for row in con.execute(f"""SELECT o.unit,COALESCE(SUM(k.gallons),0) amount,COALESCE(SUM(k.package_count),0) packages
      FROM packaging k JOIN orders o ON o.id=k.order_id{package_where} GROUP BY o.unit""", package_params).fetchall():
        suffix = "qty" if row["unit"] == "qty" else "gallons"
        result[f"packaged_{suffix}"] = round(float(row["amount"]), 2); result["packages"] += int(row["packages"])
    stock_package_where, stock_package_params = _date_range_sql("k.packaged_at", start, end)
    for row in con.execute(f"""SELECT p.unit,COALESCE(SUM(k.gallons),0) amount,COALESCE(SUM(k.package_count),0) packages
      FROM stock_packaging k JOIN production p ON p.id=k.production_id{stock_package_where} GROUP BY p.unit""", stock_package_params).fetchall():
        suffix = "qty" if row["unit"] == "qty" else "gallons"
        result[f"packaged_{suffix}"] = round(result[f"packaged_{suffix}"]+float(row["amount"]), 2)
        result["packages"] += int(row["packages"])
    for row in con.execute(f"""SELECT o.unit,COALESCE(SUM(h.gallons),0) amount,COALESCE(SUM(h.package_count),0) packages
      FROM shipments h JOIN packaging k ON k.id=h.packaging_id JOIN orders o ON o.id=k.order_id{shipment_where} GROUP BY o.unit""", shipment_params).fetchall():
        suffix = "qty" if row["unit"] == "qty" else "gallons"
        result[f"shipped_{suffix}"] = round(float(row["amount"]), 2); result["shipped_packages"] += int(row["packages"])
    return result


def _target_plan_sql(con, start, end, defaults):
    """Return effective targets, preferring a combined goal over per-product goals for each day/unit."""
    if not start or not end or end <= start:
        return {"gallons": 0.0, "qty": 0.0}
    rows = [dict(x) for x in con.execute("""SELECT target_date,brand,color,unit,amount
      FROM daily_production_targets WHERE target_date>=? AND target_date<? ORDER BY target_date,id""",
      (start.isoformat(), end.isoformat())).fetchall()]
    by_day = {}
    for row in rows:
        by_day.setdefault((row["target_date"], row["unit"]), []).append(row)
    result = {"gallons": 0.0, "qty": 0.0}
    for offset in range((end-start).days):
        day = (start+timedelta(days=offset)).isoformat()
        for unit in ("gallons", "qty"):
            unit_rows = by_day.get((day, unit), [])
            combined = [x for x in unit_rows if not x["brand"] and not x["color"]]
            if combined:
                amount = sum(float(x["amount"]) for x in combined)
            elif unit_rows:
                amount = sum(float(x["amount"]) for x in unit_rows)
            else:
                amount = float(defaults.get(f"daily_{unit}", 0) or 0)
            result[unit] += amount
    return {key: round(value, 2) for key, value in result.items()}


def _today_target_breakdown_sql(con, target_date, defaults):
    rows = [dict(x) for x in con.execute("""SELECT id,target_date,brand,color,unit,amount,updated_at,updated_by
      FROM daily_production_targets WHERE target_date=? AND amount>0 ORDER BY unit,brand,color,id""", (target_date,)).fetchall()]
    actual_rows = con.execute("""SELECT brand,color,unit,ROUND(SUM(gallons-bulk_allocated),2) amount FROM production
      WHERE produced_at>=? AND produced_at<? GROUP BY brand,color,unit""",
      (target_date, (date.fromisoformat(target_date)+timedelta(days=1)).isoformat())).fetchall()
    actual = {(x["brand"],x["color"],x["unit"]): float(x["amount"]) for x in actual_rows}
    result = []
    for unit in ("gallons", "qty"):
        unit_rows = [x for x in rows if x["unit"] == unit]
        combined = [x for x in unit_rows if not x["brand"] and not x["color"]]
        if combined:
            selected = combined
        elif unit_rows:
            selected = unit_rows
        elif float(defaults.get(f"daily_{unit}", 0) or 0) > 0:
            selected = [{"id": 0, "target_date": target_date, "brand": "", "color": "", "unit": unit,
                         "amount": defaults[f"daily_{unit}"], "updated_at": defaults.get("updated_at", ""),
                         "updated_by": defaults.get("updated_by", "")}]
        else:
            selected = []
        for row in selected:
            produced = (sum(value for (brand,color,row_unit),value in actual.items() if row_unit == unit)
                        if not row["brand"] else actual.get((row["brand"],row["color"],unit), 0.0))
            target = float(row["amount"])
            result.append({**row, "amount": target, "produced": round(produced,2),
                           "percent": round(produced/target*100,1) if target else 0,
                           "achieved": bool(target and produced >= target)})
    return result


def production_performance_sql(con):
    today_value = date.today(); tomorrow_value = today_value + timedelta(days=1)
    target_row = con.execute("SELECT daily_gallons,daily_qty,updated_at,updated_by FROM production_targets WHERE id=1").fetchone()
    defaults = {
      "daily_gallons": float(target_row["daily_gallons"]) if target_row else 0.0,
      "daily_qty": float(target_row["daily_qty"]) if target_row else 0.0,
      "updated_at": target_row["updated_at"] if target_row else "",
      "updated_by": target_row["updated_by"] if target_row else "",
    }
    definitions = [
      ("today", "Today", today_value, tomorrow_value, today_value-timedelta(days=1), today_value, 1),
      ("week", "Last 7 Days", today_value-timedelta(days=6), tomorrow_value, today_value-timedelta(days=13), today_value-timedelta(days=6), 7),
      ("month", "Last 30 Days", today_value-timedelta(days=29), tomorrow_value, today_value-timedelta(days=59), today_value-timedelta(days=29), 30),
      ("lifetime", "Lifetime", None, None, None, None, 0),
    ]
    periods = []
    for key, label, start, end, previous_start, previous_end, target_days in definitions:
        current = _period_metrics_sql(con, start.isoformat() if start else "", end.isoformat() if end else "")
        previous = _period_metrics_sql(con, previous_start.isoformat(), previous_end.isoformat()) if previous_start else None
        periods.append({"key": key, "label": label, "start": start.isoformat() if start else "",
          "end": (end-timedelta(days=1)).isoformat() if end else "", "current": current, "previous": previous,
          "target": {**(_target_plan_sql(con,start,end,defaults) if target_days else {"gallons": 0.0,"qty": 0.0}),
                     "days": target_days}})
    history_start = today_value - timedelta(days=13)
    history_rows = con.execute("""SELECT substr(produced_at,1,10) AS production_day,unit,ROUND(SUM(gallons-bulk_allocated),2) amount
      FROM production WHERE produced_at>=? AND produced_at<? GROUP BY substr(produced_at,1,10),unit ORDER BY production_day""",
      (history_start.isoformat(), tomorrow_value.isoformat())).fetchall()
    by_day = {str(history_start+timedelta(days=offset)): {"day": str(history_start+timedelta(days=offset)), "gallons": 0.0, "qty": 0.0}
              for offset in range(14)}
    for row in history_rows:
        by_day[row["production_day"]]["qty" if row["unit"] == "qty" else "gallons"] = float(row["amount"])
    mix = [dict(x) for x in con.execute("""SELECT brand,unit,ROUND(SUM(gallons-bulk_allocated),2) amount FROM production
      WHERE produced_at>=? AND produced_at<? GROUP BY brand,unit ORDER BY amount DESC""",
      ((today_value-timedelta(days=29)).isoformat(), tomorrow_value.isoformat())).fetchall()]
    today_targets = _target_plan_sql(con,today_value,tomorrow_value,defaults)
    targets = {**defaults, "daily_gallons": today_targets["gallons"], "daily_qty": today_targets["qty"]}
    return {"periods": periods, "daily_history": list(by_day.values()), "category_mix": mix, "targets": targets,
            "target_breakdown": _today_target_breakdown_sql(con,today_value.isoformat(),defaults)}


def dashboard_data():
    today = date.today().isoformat()
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    current_month = today[:7]
    with closing(connect()) as con:
        orders = con.execute("""
          SELECT o.id, o.s_number, o.po_number, o.brand, o.color, o.unit, o.gallons, o.ship_month, o.created_at,
                 ROUND(o.gallons - COALESCE(SUM(p.gallons),0),2) remaining
          FROM orders o LEFT JOIN production p ON p.order_id=o.id
          GROUP BY o.id HAVING o.gallons - COALESCE(SUM(p.gallons),0) > 0.0001
          ORDER BY o.created_at, o.id
        """).fetchall()
        need = bulk_demand_rows_sql(con)
        stock_needs = stock_need_rows_sql(con)
        daily = con.execute("""
          SELECT brand,color,unit,ROUND(SUM(gallons-bulk_allocated),2) gallons FROM production
          WHERE produced_at>=? AND produced_at<? GROUP BY brand,color,unit ORDER BY gallons DESC
        """, (today,tomorrow)).fetchall()
        total = con.execute("""
          SELECT brand,color,unit,ROUND(SUM(gallons-bulk_allocated),2) gallons FROM production
          GROUP BY brand,color,unit ORDER BY gallons DESC
        """).fetchall()
        recent = con.execute("""
          SELECT p.batch_number,p.brand,p.color,p.unit,p.gallons,p.produced_at,p.quality_status,p.incubation_due_at,
                 COALESCE(o.s_number,'') s_number
          FROM production p LEFT JOIN orders o ON o.id=p.order_id
          ORDER BY p.produced_at DESC,p.id DESC LIMIT 12
        """).fetchall()
        order_rows = [dict(x) for x in orders]
        recipients_by_order = {}
        recipient_rows = con.execute("""SELECT r.order_id,c.id,c.name,c.email FROM order_recipients r
          JOIN contacts c ON c.id=r.contact_id WHERE c.active=1 ORDER BY r.order_id,c.name""").fetchall()
        for recipient in recipient_rows:
            recipients_by_order.setdefault(recipient["order_id"], []).append({
                "id": recipient["id"], "name": recipient["name"], "email": recipient["email"]
            })
        for order in order_rows:
            order["recipients"] = recipients_by_order.get(order["id"], [])
        requirement_orders = con.execute("""SELECT o.id,o.s_number,o.po_number,o.brand,o.color,o.gallons,o.unit,o.ship_month
          FROM orders o LEFT JOIN s_number_status s ON s.s_number=o.s_number
          WHERE s.shipped_at IS NULL ORDER BY o.s_number,o.brand,o.color,o.id""").fetchall()
        active_orders = con.execute("""SELECT o.id,o.s_number,o.po_number,o.brand,o.color,o.gallons,o.unit,o.ship_month,o.created_at
          FROM orders o LEFT JOIN s_number_status s ON s.s_number=o.s_number
          WHERE s.shipped_at IS NULL ORDER BY o.s_number,o.brand,o.color,o.id""").fetchall()
        schedule_raw = con.execute("""SELECT o.id,o.s_number,o.po_number,o.brand,o.color,o.unit,o.gallons,o.ship_month,o.created_at,
          ROUND(COALESCE(SUM(p.gallons),0),2) produced,
          ROUND(CASE WHEN o.gallons-COALESCE(SUM(p.gallons),0)>0
                THEN o.gallons-COALESCE(SUM(p.gallons),0) ELSE 0 END,2) remaining,
          s.ready_at,s.shipped_at FROM orders o LEFT JOIN production p ON p.order_id=o.id
          LEFT JOIN s_number_status s ON s.s_number=o.s_number
          WHERE s.shipped_at IS NULL OR o.ship_month>=?
          GROUP BY o.id,s.ready_at,s.shipped_at
          ORDER BY CASE WHEN o.ship_month='' THEN 1 ELSE 0 END,o.ship_month,o.s_number,o.id""", (current_month,)).fetchall()
        order_schedule = []
        for raw in schedule_raw:
            row = dict(raw)
            row["status"] = "Shipped" if row["shipped_at"] else "Ready" if row["ready_at"] else "Packaging" if float(row["remaining"]) <= .0001 else "In Production" if float(row["produced"]) > .0001 else "Not Started"
            order_schedule.append(row)
        packageable = con.execute("""
          SELECT * FROM (
            SELECT o.id,o.s_number,o.brand,o.color,o.unit,
              ROUND(COALESCE((SELECT SUM(p.gallons) FROM production p WHERE p.order_id=o.id),0),2) produced,
              ROUND(COALESCE((SELECT SUM(k.gallons) FROM packaging k WHERE k.order_id=o.id),0),2) packaged,
              ROUND(COALESCE((SELECT SUM(p.gallons) FROM production p WHERE p.order_id=o.id),0)-
                    COALESCE((SELECT SUM(k.gallons) FROM packaging k WHERE k.order_id=o.id),0),2) available
            FROM orders o LEFT JOIN s_number_status s ON s.s_number=o.s_number
            WHERE s.done_at IS NULL AND s.shipped_at IS NULL
          ) WHERE available > 0.0001 ORDER BY s_number,brand,color,id
        """).fetchall()
        packageable_rows = [{**dict(x), "source_type": "order", "source_id": x["id"]} for x in packageable]
        stock_packageable = con.execute("""SELECT * FROM (SELECT p.id,p.brand,p.color,p.unit,p.batch_number,p.bulk_purpose,p.quality_status,
          ROUND(p.gallons,2) produced,ROUND(COALESCE((SELECT SUM(k.gallons) FROM stock_packaging k WHERE k.production_id=p.id),0),2) packaged,
          ROUND(p.gallons-p.bulk_allocated-COALESCE((SELECT SUM(k.gallons) FROM stock_packaging k WHERE k.production_id=p.id),0),2) available
           FROM production p WHERE p.order_id IS NULL AND (p.incubation_started_at='' OR p.quality_status='ready'))
           WHERE available>0.0001 ORDER BY brand,color,id""").fetchall()
        packageable_rows.extend([{**dict(x), "s_number": "STOCK", "source_type": "stock", "source_id": x["id"]} for x in stock_packageable])
        stock_inventory = con.execute("""SELECT k.id,k.production_id,k.brand,k.color,k.package_type,k.package_size,k.package_count,
          k.gallons,k.packaged_at,k.created_by,p.batch_number,p.unit FROM stock_packaging k JOIN production p ON p.id=k.production_id
          ORDER BY k.packaged_at DESC,k.id DESC""").fetchall()
        inventory = con.execute("""
          SELECT s_number,brand,color,unit,package_type,package_size,SUM(available_count) package_count,
            ROUND(SUM(available_count*package_size),2) gallons,MAX(packaged_at) packaged_at,done_at,done_by,ready_at,ready_by
          FROM (SELECT k.*,o.unit,s.done_at,s.done_by,s.ready_at,s.ready_by,k.package_count-
            COALESCE((SELECT SUM(h.package_count) FROM shipments h WHERE h.packaging_id=k.id),0) available_count
            FROM packaging k JOIN orders o ON o.id=k.order_id LEFT JOIN s_number_status s ON s.s_number=k.s_number)
          WHERE available_count>0 GROUP BY s_number,brand,color,unit,package_type,package_size,done_at,done_by,ready_at,ready_by
          ORDER BY s_number,brand,color,package_size DESC
        """).fetchall()
        shipped = con.execute("""
          SELECT h.id,h.packaging_id,h.s_number,h.brand,h.color,h.package_type,h.package_size,h.package_count,h.gallons,h.shipped_at,h.shipped_by,o.unit
          FROM shipments h JOIN packaging k ON k.id=h.packaging_id JOIN orders o ON o.id=k.order_id
          ORDER BY h.shipped_at DESC,h.id DESC LIMIT 250
        """).fetchall()
        shipping_inventory = con.execute("""
          SELECT * FROM (SELECT k.id packaging_id,k.s_number,k.brand,k.color,o.unit,k.package_type,k.package_size,
            k.package_count-COALESCE((SELECT SUM(h.package_count) FROM shipments h WHERE h.packaging_id=k.id),0) available_count,
            s.ready_at,s.ready_by FROM packaging k JOIN orders o ON o.id=k.order_id JOIN s_number_status s ON s.s_number=k.s_number WHERE s.ready_at IS NOT NULL)
          WHERE available_count>0 ORDER BY s_number,brand,color,package_size DESC
        """).fetchall()
        requirements = con.execute("""SELECT r.id,r.order_id,r.s_number,r.brand,r.color,o.unit,r.package_type,r.package_size,
          r.required_count,r.required_gallons,COALESCE((SELECT SUM(k.package_count) FROM packaging k
          WHERE k.order_id=r.order_id AND k.package_type=r.package_type),0) completed_count
          FROM packaging_requirements r JOIN orders o ON o.id=r.order_id ORDER BY r.s_number,r.brand,r.color,r.package_size DESC""").fetchall()
        statuses = con.execute("""
          SELECT DISTINCT o.s_number,s.done_at,s.done_by,s.ready_at,s.ready_by,s.shipped_at,s.shipped_by
          FROM orders o LEFT JOIN s_number_status s ON s.s_number=o.s_number
          WHERE s.shipped_at IS NULL ORDER BY o.s_number
        """).fetchall()
        remaining_by_s = {}
        for row in con.execute("""SELECT s_number,unit,ROUND(SUM(CASE WHEN remaining>0 THEN remaining ELSE 0 END),2) remaining FROM (
          SELECT o.id,o.s_number,o.unit,o.gallons-COALESCE(SUM(p.gallons),0) remaining
          FROM orders o LEFT JOIN production p ON p.order_id=o.id GROUP BY o.id
        ) GROUP BY s_number,unit""").fetchall():
            remaining_by_s.setdefault(row["s_number"], {})[row["unit"]] = float(row["remaining"])
        unpackaged_by_s = {}
        for row in con.execute("""SELECT s_number,unit,ROUND(SUM(CASE WHEN available>0 THEN available ELSE 0 END),2) available FROM (
          SELECT o.id,o.s_number,o.unit,COALESCE(SUM(p.gallons),0)-
                 COALESCE((SELECT SUM(k.gallons) FROM packaging k WHERE k.order_id=o.id),0) available
          FROM orders o LEFT JOIN production p ON p.order_id=o.id GROUP BY o.id
        ) GROUP BY s_number,unit""").fetchall():
            unpackaged_by_s.setdefault(row["s_number"], {})[row["unit"]] = float(row["available"])
        packed_by_s = {row["s_number"]: row for row in con.execute("""SELECT k.s_number,
          COALESCE(SUM(k.package_count-COALESCE((SELECT SUM(h.package_count) FROM shipments h WHERE h.packaging_id=k.id),0)),0) package_count,
          COALESCE(SUM((k.package_count-COALESCE((SELECT SUM(h.package_count) FROM shipments h WHERE h.packaging_id=k.id),0))*k.package_size),0) packaged_gallons
          FROM packaging k GROUP BY k.s_number""").fetchall()}
        s_numbers = []
        po_numbers_by_s = {}
        for row in con.execute("SELECT s_number,po_number FROM orders WHERE trim(po_number)<>'' ORDER BY id").fetchall():
            values = po_numbers_by_s.setdefault(row["s_number"], [])
            if row["po_number"] not in values:
                values.append(row["po_number"])
        for status in statuses:
            remaining = remaining_by_s.get(status["s_number"], {})
            unpackaged = unpackaged_by_s.get(status["s_number"], {})
            packed = packed_by_s.get(status["s_number"])
            po_numbers = po_numbers_by_s.get(status["s_number"], [])
            s_numbers.append({**dict(status), "po_number": ", ".join(po_numbers), "po_numbers": po_numbers,
                              "remaining_gallons": remaining.get("gallons",0),
                              "remaining_qty": remaining.get("qty",0), "unpackaged_gallons": unpackaged.get("gallons",0),
                              "unpackaged_qty": unpackaged.get("qty",0),
                              "package_count": int(packed["package_count"]) if packed else 0,
                              "packaged_gallons": round(float(packed["packaged_gallons"]),2) if packed else 0})
        bulk_demand = [dict(x) for x in need]
        bulk_stock_demand = bulk_demand_rows_sql(con, "stock")
        incubation_batches = incubation_batches_sql(con)
    performance = {"periods": [], "daily_history": [], "category_mix": [],
                   "targets": {"daily_gallons": 0, "daily_qty": 0, "updated_at": "", "updated_by": ""}}
    try:
        with closing(connect()) as performance_connection:
            performance = production_performance_sql(performance_connection)
    except DatabaseOperationalError as exc:
        # The core production/order dashboard must remain available even if an
        # optional trend query is temporarily unavailable.
        print(f"Performance visualization temporarily unavailable: {exc}", file=sys.stderr, flush=True)
    need_rows=[dict(x) for x in need]
    for stock_need in stock_needs:
        if float(stock_need["remaining"])<=.0001: continue
        existing=next((x for x in need_rows if x["brand"]==stock_need["brand"] and x["color"]==stock_need["color"] and x["unit"]==stock_need["unit"]),None)
        if existing: existing["gallons"]=round(float(existing["gallons"])+float(stock_need["remaining"]),2)
        else: need_rows.append({"brand":stock_need["brand"],"color":stock_need["color"],"unit":stock_need["unit"],"gallons":stock_need["remaining"]})
    need_rows.sort(key=lambda x:float(x["gallons"]),reverse=True)
    return {"today": today, "performance": performance, "orders": order_rows, "active_orders": [dict(x) for x in active_orders], "order_schedule": order_schedule,
            "requirement_orders": [dict(x) for x in requirement_orders],
            "need": need_rows, "daily": [dict(x) for x in daily],
            "total": [dict(x) for x in total], "recent": [dict(x) for x in recent],
            "package_types": package_type_data(), "packageable": packageable_rows,
            "inventory": [dict(x) for x in inventory], "shipped": [dict(x) for x in shipped],
            "shipping_inventory": [dict(x) for x in shipping_inventory], "stock_inventory": [dict(x) for x in stock_inventory],
            "packaging_requirements": [dict(x) for x in requirements], "s_numbers": s_numbers,"stock_needs":stock_needs,
            "bulk_demand": bulk_demand, "bulk_stock_demand": bulk_stock_demand,
            "incubation_batches": incubation_batches,
            "targets": performance["targets"]}


def _order_schedule_sql(con):
    current_month = date.today().isoformat()[:7]
    rows = con.execute("""SELECT o.id,o.s_number,o.po_number,o.brand,o.color,o.unit,o.gallons,o.ship_month,o.created_at,
      ROUND(COALESCE(SUM(p.gallons),0),2) produced,
      ROUND(CASE WHEN o.gallons-COALESCE(SUM(p.gallons),0)>0 THEN o.gallons-COALESCE(SUM(p.gallons),0) ELSE 0 END,2) remaining,
      s.ready_at,s.shipped_at FROM orders o LEFT JOIN production p ON p.order_id=o.id
      LEFT JOIN s_number_status s ON s.s_number=o.s_number WHERE s.shipped_at IS NULL OR o.ship_month>=?
      GROUP BY o.id,s.ready_at,s.shipped_at
      ORDER BY CASE WHEN o.ship_month='' THEN 1 ELSE 0 END,o.ship_month,o.s_number,o.id""", (current_month,)).fetchall()
    result = []
    for raw in rows:
        row = dict(raw)
        row["status"] = "Shipped" if row["shipped_at"] else "Ready" if row["ready_at"] else "Packaging" if float(row["remaining"]) <= .0001 else "In Production" if float(row["produced"]) > .0001 else "Not Started"
        result.append(row)
    return result


def tv_dashboard_data():
    """Small public payload for the floor TV; avoids loading inventory, shipping, and costing records."""
    with closing(connect()) as con:
        return {"performance": production_performance_sql(con), "order_schedule": _order_schedule_sql(con)}


def dashboard_summary_data():
    """Fast management landing-page payload with drill-down data loaded only after a click."""
    today = date.today().isoformat(); tomorrow = (date.today()+timedelta(days=1)).isoformat()
    with closing(connect()) as con:
        need = bulk_demand_rows_sql(con)
        stock_needs = stock_need_rows_sql(con)
        for item in stock_needs:
            if float(item["remaining"]) <= .0001: continue
            existing = next((x for x in need if x["brand"]==item["brand"] and x["color"]==item["color"] and x["unit"]==item["unit"]),None)
            if existing: existing["gallons"] = round(float(existing["gallons"])+float(item["remaining"]),2)
            else: need.append({"brand":item["brand"],"color":item["color"],"unit":item["unit"],"gallons":item["remaining"]})
        need.sort(key=lambda x:float(x["gallons"]),reverse=True)
        daily = [dict(x) for x in con.execute("""SELECT brand,color,unit,ROUND(SUM(gallons-bulk_allocated),2) gallons FROM production
          WHERE produced_at>=? AND produced_at<? GROUP BY brand,color,unit ORDER BY gallons DESC""", (today,tomorrow)).fetchall()]
        total = [dict(x) for x in con.execute("""SELECT brand,color,unit,ROUND(SUM(gallons-bulk_allocated),2) gallons FROM production
          GROUP BY brand,color,unit ORDER BY gallons DESC""").fetchall()]
        incubating = [dict(x) for x in con.execute("""SELECT brand,color,unit,ROUND(SUM(gallons-bulk_allocated),2) gallons
          FROM production WHERE unit='gallons' AND quality_status='incubating' AND order_id IS NULL
          GROUP BY brand,color,unit HAVING SUM(gallons-bulk_allocated)>0 ORDER BY gallons DESC""").fetchall()]
        active_orders = [dict(x) for x in con.execute("""SELECT o.id,o.s_number,o.po_number,o.brand,o.color,o.gallons,o.unit,o.ship_month,o.created_at,
          ROUND(COALESCE((SELECT SUM(p.gallons) FROM production p WHERE p.order_id=o.id),0),2) produced,
          COALESCE((SELECT SUM(k.package_count-COALESCE((SELECT SUM(h.package_count) FROM shipments h WHERE h.packaging_id=k.id),0))
            FROM packaging k WHERE k.order_id=o.id),0) package_count
          FROM orders o LEFT JOIN s_number_status s ON s.s_number=o.s_number
          WHERE s.shipped_at IS NULL ORDER BY CASE WHEN o.ship_month='' THEN 1 ELSE 0 END,o.ship_month,o.s_number,o.id""").fetchall()]
        statuses = con.execute("""SELECT DISTINCT o.s_number,s.done_at,s.done_by,s.ready_at,s.ready_by,s.shipped_at,s.shipped_by
          FROM orders o LEFT JOIN s_number_status s ON s.s_number=o.s_number WHERE s.shipped_at IS NULL ORDER BY o.s_number""").fetchall()
        remaining_by_s = {}
        for row in con.execute("""SELECT s_number,unit,ROUND(SUM(CASE WHEN remaining>0 THEN remaining ELSE 0 END),2) remaining FROM (
          SELECT o.id,o.s_number,o.unit,o.gallons-COALESCE(SUM(p.gallons),0) remaining
          FROM orders o LEFT JOIN production p ON p.order_id=o.id GROUP BY o.id) GROUP BY s_number,unit""").fetchall():
            remaining_by_s.setdefault(row["s_number"],{})[row["unit"]] = float(row["remaining"])
        unpackaged_by_s = {}
        for row in con.execute("""SELECT s_number,unit,ROUND(SUM(CASE WHEN available>0 THEN available ELSE 0 END),2) available FROM (
          SELECT o.id,o.s_number,o.unit,COALESCE(SUM(p.gallons),0)-COALESCE((SELECT SUM(k.gallons) FROM packaging k WHERE k.order_id=o.id),0) available
          FROM orders o LEFT JOIN production p ON p.order_id=o.id GROUP BY o.id) GROUP BY s_number,unit""").fetchall():
            unpackaged_by_s.setdefault(row["s_number"],{})[row["unit"]] = float(row["available"])
        packed_by_s = {row["s_number"]:row for row in con.execute("""SELECT k.s_number,
          COALESCE(SUM(k.package_count-COALESCE((SELECT SUM(h.package_count) FROM shipments h WHERE h.packaging_id=k.id),0)),0) package_count,
          COALESCE(SUM((k.package_count-COALESCE((SELECT SUM(h.package_count) FROM shipments h WHERE h.packaging_id=k.id),0))*k.package_size),0) packaged_gallons
          FROM packaging k GROUP BY k.s_number""").fetchall()}
        po_numbers_by_s = {}
        for row in con.execute("SELECT s_number,po_number FROM orders WHERE trim(po_number)<>'' ORDER BY id").fetchall():
            values=po_numbers_by_s.setdefault(row["s_number"],[])
            if row["po_number"] not in values: values.append(row["po_number"])
        s_numbers=[]
        for status in statuses:
            remaining=remaining_by_s.get(status["s_number"],{}); unpackaged=unpackaged_by_s.get(status["s_number"],{}); packed=packed_by_s.get(status["s_number"])
            po_numbers=po_numbers_by_s.get(status["s_number"],[])
            s_numbers.append({**dict(status),"po_number":", ".join(po_numbers),"po_numbers":po_numbers,
              "remaining_gallons":remaining.get("gallons",0),"remaining_qty":remaining.get("qty",0),
              "unpackaged_gallons":unpackaged.get("gallons",0),"unpackaged_qty":unpackaged.get("qty",0),
              "package_count":int(packed["package_count"]) if packed else 0,
              "packaged_gallons":round(float(packed["packaged_gallons"]),2) if packed else 0})
        active_inventory_packages = con.execute("""SELECT COALESCE(SUM(available_count),0) FROM (
          SELECT k.package_count-COALESCE((SELECT SUM(h.package_count) FROM shipments h WHERE h.packaging_id=k.id),0) available_count FROM packaging k
          UNION ALL SELECT package_count FROM stock_packaging) WHERE available_count>0""").fetchone()[0]
        performance = production_performance_sql(con)
    return {"performance":performance,"targets":performance["targets"],"need":need,"daily":daily,"total":total,"incubating":incubating,
            "active_orders":active_orders,"s_numbers":s_numbers,"active_inventory_packages":int(active_inventory_packages or 0),
            "package_types":package_type_data()}


def s_number_detail(s_number):
    """Return the complete, read-only production story for one order reference."""
    reference = str(s_number or "").strip().upper()
    if not reference:
        return None
    with closing(connect()) as con:
        orders = [dict(x) for x in con.execute("""SELECT o.id,o.s_number,o.po_number,o.brand,o.color,o.unit,o.gallons,
          o.ship_month,o.created_at,
          ROUND(COALESCE((SELECT SUM(p.gallons) FROM production p WHERE p.order_id=o.id),0),2) produced,
          ROUND(COALESCE((SELECT SUM(k.gallons) FROM packaging k WHERE k.order_id=o.id),0),2) packaged,
          ROUND(COALESCE((SELECT SUM(h.gallons) FROM shipments h JOIN packaging k ON k.id=h.packaging_id
            WHERE k.order_id=o.id),0),2) shipped
          FROM orders o WHERE o.s_number=? ORDER BY o.brand,o.color,o.id""", (reference,)).fetchall()]
        if not orders:
            return None
        status_row = con.execute("SELECT * FROM s_number_status WHERE s_number=?", (reference,)).fetchone()
        status = dict(status_row) if status_row else {"s_number": reference, "done_at": None, "done_by": None,
          "ready_at": None, "ready_by": None, "shipped_at": None, "shipped_by": None}
        batches = [dict(x) for x in con.execute("""SELECT p.id,p.order_id,p.batch_number,p.brand,p.color,p.unit,
          p.gallons,p.produced_at FROM production p JOIN orders o ON o.id=p.order_id
          WHERE o.s_number=? ORDER BY p.produced_at,p.id""", (reference,)).fetchall()]
        requirements = [dict(x) for x in con.execute("""SELECT r.id,r.order_id,r.brand,r.color,o.unit,
          r.package_type,r.package_size,r.required_count,r.required_gallons,
          COALESCE((SELECT SUM(k.package_count) FROM packaging k WHERE k.order_id=r.order_id
            AND k.package_type=r.package_type AND ABS(k.package_size-r.package_size)<.0001),0) completed_count,
          COALESCE((SELECT SUM(h.package_count) FROM shipments h JOIN packaging k ON k.id=h.packaging_id
            WHERE k.order_id=r.order_id AND k.package_type=r.package_type
            AND ABS(k.package_size-r.package_size)<.0001),0) shipped_count
          FROM packaging_requirements r JOIN orders o ON o.id=r.order_id
          WHERE r.s_number=? ORDER BY r.brand,r.color,r.package_size DESC""", (reference,)).fetchall()]
        packages = [dict(x) for x in con.execute("""SELECT k.id,k.order_id,k.brand,k.color,o.unit,k.package_type,
          k.package_size,k.package_count,k.gallons,k.packaged_at,k.created_by,
          COALESCE((SELECT SUM(h.package_count) FROM shipments h WHERE h.packaging_id=k.id),0) shipped_count,
          k.package_count-COALESCE((SELECT SUM(h.package_count) FROM shipments h WHERE h.packaging_id=k.id),0) available_count
          FROM packaging k JOIN orders o ON o.id=k.order_id WHERE k.s_number=?
          ORDER BY k.brand,k.color,k.package_size DESC,k.id""", (reference,)).fetchall()]
        recipients = [dict(x) for x in con.execute("""SELECT DISTINCT c.id,c.name,c.email,c.active
          FROM order_recipients r JOIN contacts c ON c.id=r.contact_id JOIN orders o ON o.id=r.order_id
          WHERE o.s_number=? ORDER BY c.name,c.email""", (reference,)).fetchall()]
        labor = con.execute("""SELECT ROUND(COALESCE(SUM(hours),0),2) hours,
          ROUND(COALESCE(SUM(labor_cost),0),2) cost FROM labor_entries WHERE s_number=?""", (reference,)).fetchone()

    totals = {"ordered_gallons": 0.0, "ordered_qty": 0.0, "produced_gallons": 0.0,
      "produced_qty": 0.0, "remaining_gallons": 0.0, "remaining_qty": 0.0,
      "unpackaged_gallons": 0.0, "unpackaged_qty": 0.0, "inventory_gallons": 0.0,
      "inventory_qty": 0.0, "shipped_gallons": 0.0, "shipped_qty": 0.0}
    for order in orders:
        ordered = float(order["gallons"])
        produced = float(order["produced"])
        packaged = float(order["packaged"])
        shipped = float(order["shipped"])
        remaining = round(max(ordered - produced, 0), 2)
        unpackaged = round(max(produced - packaged, 0), 2)
        inventory = round(max(packaged - shipped, 0), 2)
        order.update({"remaining": remaining, "unpackaged": unpackaged, "inventory": inventory})
        if remaining > .0001:
            order["status"] = "In Production" if produced > .0001 else "Not Started"
        elif unpackaged > .0001 or packaged + .0001 < ordered:
            order["status"] = "Packaging"
        elif status.get("shipped_at"):
            order["status"] = "Shipped"
        elif status.get("ready_at"):
            order["status"] = "Ready"
        else:
            order["status"] = "Packaging"
        suffix = "qty" if order["unit"] == "qty" else "gallons"
        totals[f"ordered_{suffix}"] += ordered
        totals[f"produced_{suffix}"] += produced
        totals[f"remaining_{suffix}"] += remaining
        totals[f"unpackaged_{suffix}"] += unpackaged
        totals[f"inventory_{suffix}"] += inventory
        totals[f"shipped_{suffix}"] += shipped
    totals = {key: round(value, 2) for key, value in totals.items()}
    has_remaining = totals["remaining_gallons"] > .0001 or totals["remaining_qty"] > .0001
    has_production = totals["produced_gallons"] > .0001 or totals["produced_qty"] > .0001
    overall_status = "Shipped" if status.get("shipped_at") else "Ready" if status.get("ready_at") else (
      ("In Production" if has_production else "Not Started") if has_remaining else "Packaging")
    return {"s_number": reference, "po_numbers": sorted({x["po_number"] for x in orders if x["po_number"]}),
      "status": overall_status, "status_history": status,
      "ship_months": sorted({x["ship_month"] for x in orders if x["ship_month"]}), "totals": totals,
      "orders": orders, "batches": batches, "requirements": requirements, "packages": packages,
      "recipients": recipients, "labor": {"hours": float(labor["hours"]), "cost": float(labor["cost"])} if labor else {"hours": 0, "cost": 0}}


def cached_dashboard_data():
    now = time.monotonic()
    with DASHBOARD_CACHE_LOCK:
        if DASHBOARD_CACHE["value"] is not None and DASHBOARD_CACHE["expires"] > now:
            return DASHBOARD_CACHE["value"]
        stale = DASHBOARD_CACHE["value"]
        try:
            value = dashboard_data()
        except DatabaseOperationalError:
            if stale is not None:
                return stale
            raise
        DASHBOARD_CACHE["value"] = value
        DASHBOARD_CACHE["expires"] = time.monotonic() + DASHBOARD_CACHE_TTL
        return value


def admin_data():
    with closing(connect()) as con:
        orders = con.execute("""
          SELECT o.id,o.s_number,o.po_number,o.brand,o.color,o.unit,o.gallons,o.ship_month,o.created_at,
                 ROUND(COALESCE(SUM(p.gallons),0),2) produced,
                 ROUND(o.gallons-COALESCE(SUM(p.gallons),0),2) remaining
          FROM orders o LEFT JOIN production p ON p.order_id=o.id
          GROUP BY o.id ORDER BY o.created_at DESC,o.id DESC
        """).fetchall()
        batches = con.execute("""
          SELECT p.id,p.order_id,p.brand,p.color,p.unit,p.batch_number,p.gallons,p.produced_at,
                 COALESCE(o.s_number,'') s_number
          FROM production p LEFT JOIN orders o ON o.id=p.order_id
          ORDER BY p.produced_at DESC,p.id DESC
        """).fetchall()
        catalog = con.execute("SELECT id,category,color,unit FROM catalog ORDER BY category,color").fetchall()
        contacts = con.execute("SELECT id,name,email,active FROM contacts ORDER BY active DESC,name,email").fetchall()
        packaging = con.execute("""
          SELECT k.id,k.order_id,k.s_number,k.brand,k.color,k.package_type,k.package_size,k.package_count,
                 k.gallons,k.packaged_at,k.created_by,s.done_at,s.ready_at,s.shipped_at,s.shipped_by,
                 COALESCE((SELECT SUM(h.package_count) FROM shipments h WHERE h.packaging_id=k.id),0) shipped_count,
                 k.package_count-COALESCE((SELECT SUM(h.package_count) FROM shipments h WHERE h.packaging_id=k.id),0) remaining_count
          FROM packaging k LEFT JOIN s_number_status s ON s.s_number=k.s_number
          ORDER BY k.packaged_at DESC,k.id DESC
        """).fetchall()
        shipments = con.execute("SELECT * FROM shipments ORDER BY shipped_at DESC,id DESC").fetchall()
        stock_packaging = con.execute("""SELECT k.*,p.batch_number FROM stock_packaging k JOIN production p ON p.id=k.production_id
          ORDER BY k.packaged_at DESC,k.id DESC""").fetchall()
        requirements = con.execute("""SELECT r.*,o.unit,COALESCE((SELECT SUM(k.package_count) FROM packaging k
          WHERE k.order_id=r.order_id AND k.package_type=r.package_type),0) completed_count
          FROM packaging_requirements r JOIN orders o ON o.id=r.order_id ORDER BY r.s_number,r.brand,r.color""").fetchall()
        target = con.execute("SELECT daily_gallons,daily_qty,updated_at,updated_by FROM production_targets WHERE id=1").fetchone()
        daily_targets = con.execute("""SELECT id,target_date,brand,color,unit,amount,updated_at,updated_by
          FROM daily_production_targets WHERE target_date>=? ORDER BY target_date DESC,unit,brand,color,id""",
          ((date.today()-timedelta(days=7)).isoformat(),)).fetchall()
        order_rows = [dict(x) for x in orders]
        for order in order_rows:
            order["recipient_ids"] = [x[0] for x in con.execute("SELECT contact_id FROM order_recipients WHERE order_id=?", (order["id"],)).fetchall()]
    return {"orders": order_rows, "batches": [dict(x) for x in batches],
            "catalog": [dict(x) for x in catalog], "contacts": [dict(x) for x in contacts],
            "packaging": [dict(x) for x in packaging], "shipments": [dict(x) for x in shipments],
            "stock_packaging": [dict(x) for x in stock_packaging], "packaging_requirements": [dict(x) for x in requirements],
            "package_types": package_type_data(), "targets": dict(target) if target else {"daily_gallons": 0, "daily_qty": 0},
            "daily_targets": [dict(x) for x in daily_targets]}


def contact_data():
    with closing(connect()) as con:
        return [dict(x) for x in con.execute("SELECT id,name,email FROM contacts WHERE active=1 ORDER BY name,email").fetchall()]


def user_data():
    with closing(connect()) as con:
        return [dict(x) for x in con.execute("SELECT id,username,email,display_name,role,active,created_at FROM users ORDER BY active DESC,display_name,email,username").fetchall()]


def deliver_notification_email(recipient_emails, subject, body, saved_label="Production saved"):
    recipients = [str(email or "").strip() for email in recipient_emails if valid_email(email)]
    if not recipients:
        return None
    if not EMAIL_ENABLED:
        return f"{saved_label}; email notifications are disabled for this environment."
    if EMAIL_PROVIDER == "google_apps_script":
        if not GOOGLE_APPS_SCRIPT_URL.startswith("https://script.google.com/") or not GOOGLE_APPS_SCRIPT_SECRET:
            return f"{saved_label}, but Google Apps Script email is not configured."
        try:
            request = Request(
                GOOGLE_APPS_SCRIPT_URL,
                data=json.dumps({
                    "secret": GOOGLE_APPS_SCRIPT_SECRET,
                    "to": recipients,
                    "subject": subject,
                    "body": body,
                }).encode("utf-8"),
                method="POST",
                headers={"content-type": "application/json"},
            )
            with urlopen(request, timeout=20) as response:
                result = json.loads(response.read().decode("utf-8"))
            if not result.get("ok"):
                raise RuntimeError(str(result.get("error") or "Apps Script rejected the request"))
            return f"Email sent to {len(recipients)} recipient(s)."
        except HTTPError as exc:
            return f"{saved_label}, but Google Apps Script email failed with HTTP {exc.code}."
        except (URLError, TimeoutError, OSError) as exc:
            return f"{saved_label}, but Google Apps Script email failed: {exc}."
        except (ValueError, RuntimeError) as exc:
            return f"{saved_label}, but Google Apps Script email failed: {exc}."
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        return f"{saved_label}, but Gmail is not configured."
    message = EmailMessage(); message["From"] = GMAIL_ADDRESS
    message["To"] = ", ".join(recipients); message["Subject"] = subject; message.set_content(body)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as smtp:
            smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD); smtp.send_message(message)
        return f"Email sent to {len(recipients)} recipient(s)."
    except Exception as exc:
        return f"{saved_label}, but email failed: {exc}"


def send_order_created_email(order_id):
    with closing(connect()) as con:
        order = con.execute("""SELECT s_number,po_number,brand,color,unit,gallons,ship_month,created_at
          FROM orders WHERE id=?""", (order_id,)).fetchone()
        recipients = con.execute("""SELECT c.email FROM contacts c JOIN order_recipients r ON r.contact_id=c.id
          WHERE r.order_id=? AND c.active=1 ORDER BY c.email""", (order_id,)).fetchall()
    if not order or not recipients:
        return None
    unit = "qty" if order["unit"] == "qty" else "gallons"
    po_text = order["po_number"] or "Not provided"
    subject = f"New order {order['s_number']}" + (f" — PO {order['po_number']}" if order["po_number"] else "")
    body = (f"A new production order was added.\n\nS#/Order: {order['s_number']}\nPO#: {po_text}\n"
            f"Category: {order['brand']}\nColor: {order['color']}\nRequired: {float(order['gallons']):g} {unit}\n"
            f"Ship month: {order['ship_month']}\nOrder entered: {order['created_at']}\n")
    return deliver_notification_email([x["email"] for x in recipients], subject, body, "Order saved")


def send_production_email(order_id, batch_number, produced_amount, remaining):
    with closing(connect()) as con:
        order = con.execute("SELECT s_number,po_number,brand,color,unit FROM orders WHERE id=?", (order_id,)).fetchone()
        recipients = con.execute("""SELECT c.email FROM contacts c JOIN order_recipients r ON r.contact_id=c.id
          WHERE r.order_id=? AND c.active=1 ORDER BY c.email""", (order_id,)).fetchall()
    if not order or not recipients: return None
    unit = "qty" if order["unit"] == "qty" else "gallons"; complete = remaining <= .0001
    subject = f"{order['s_number']} - {'Production complete' if complete else 'Partial production update'}"
    status = "COMPLETE" if complete else "PARTIALLY PRODUCED"
    body = (f"S#: {order['s_number']}\nPO#: {order['po_number'] or 'Not provided'}\nCategory: {order['brand']}\nColor: {order['color']}\n"
            f"Batch #: {batch_number}\nStatus: {status}\nProduced this batch: {produced_amount:g} {unit}\n"
            f"Need to produce: {max(remaining,0):g} {unit}\n")
    return deliver_notification_email([x["email"] for x in recipients], subject, body)


def catalog_data():
    with closing(connect()) as con:
        rows = con.execute("SELECT id,category,color,unit FROM catalog ORDER BY category,color").fetchall()
    groups = {}
    for row in rows:
        groups.setdefault(row["category"], {"unit": row["unit"], "colors": []})["colors"].append({"id": row["id"], "name": row["color"]})
    return [{"category": key, **value} for key, value in groups.items()]


def category_report_rows():
    totals = {}
    def row(category, unit):
        return totals.setdefault((category, unit), {"category": category, "unit": unit,
          "ordered": 0.0, "produced": 0.0, "need": 0.0})
    with closing(connect()) as con:
        for item in con.execute("SELECT DISTINCT category,unit FROM catalog").fetchall(): row(item["category"], item["unit"])
        for item in con.execute("SELECT brand,unit,SUM(gallons) amount FROM orders GROUP BY brand,unit").fetchall():
            row(item["brand"],item["unit"])["ordered"] += float(item["amount"])
        for item in bulk_demand_rows_sql(con):
            row(item["brand"],item["unit"])["need"] += item["remaining"]
        for need in stock_need_rows_sql(con):
            item = row(need["brand"],need["unit"])
            item["ordered"] += float(need["amount"]); item["need"] += float(need["remaining"])
        for item in con.execute("SELECT brand,unit,SUM(gallons-bulk_allocated) amount FROM production GROUP BY brand,unit").fetchall():
            row(item["brand"],item["unit"])["produced"] += float(item["amount"])
    return [{**x, "ordered": round(x["ordered"],2), "produced": round(x["produced"],2), "need": round(x["need"],2)}
            for x in sorted(totals.values(), key=lambda x: (x["category"].lower(), x["unit"]))]


def category_report_csv():
    output = io.StringIO(newline=""); writer = csv.writer(output)
    writer.writerow(["Category", "Unit", "Total Required (S# + No S#)", "Total Produced", "Need to Produce"])
    for row in category_report_rows():
        writer.writerow([row["category"], "Qty" if row["unit"] == "qty" else "Gallons",
                         row["ordered"], row["produced"], row["need"]])
    return output.getvalue().encode("utf-8-sig")


def costing_data():
    today = date.today().isoformat()
    with closing(connect()) as con:
        orders = [dict(x) for x in con.execute("SELECT id,s_number,po_number,brand,color,unit,gallons,created_at FROM orders ORDER BY s_number,brand,color,id").fetchall()]
        rates = [dict(x) for x in con.execute("SELECT * FROM labor_rates ORDER BY labor_name,effective_from DESC,id DESC").fetchall()]
        current_rates = []
        for rate in rates:
            if rate["active"] and rate["effective_from"][:10] <= today and not any(x["labor_name"].lower()==rate["labor_name"].lower() for x in current_rates):
                current_rates.append(rate)
        entries = [dict(x) for x in con.execute("SELECT * FROM labor_entries ORDER BY worked_at DESC,id DESC LIMIT 250").fetchall()]
        summary = [dict(x) for x in con.execute("""SELECT o.id order_id,o.s_number,o.brand,o.color,o.unit,o.gallons ordered_amount,
          ROUND(COALESCE((SELECT SUM(p.gallons) FROM production p WHERE p.order_id=o.id),0),2) produced_amount,
          ROUND(COALESCE((SELECT SUM(e.hours) FROM labor_entries e WHERE e.order_id=o.id),0),2) labor_hours,
          ROUND(COALESCE((SELECT SUM(e.labor_cost) FROM labor_entries e WHERE e.order_id=o.id),0),2) labor_cost,
          ROUND(CASE WHEN COALESCE((SELECT SUM(p.gallons) FROM production p WHERE p.order_id=o.id),0)>0
            THEN COALESCE((SELECT SUM(e.labor_cost) FROM labor_entries e WHERE e.order_id=o.id),0)/
                 (SELECT SUM(p.gallons) FROM production p WHERE p.order_id=o.id) ELSE 0 END,4) cost_per_unit
          FROM orders o ORDER BY o.s_number,o.brand,o.color""").fetchall()]
        stock_summary = [dict(x) for x in con.execute("""SELECT p.id production_id,p.batch_number,p.brand,p.color,p.unit,
          p.gallons-p.bulk_allocated produced_amount,p.quality_status,p.rework_of_id,parent.batch_number rework_of_batch,
          ROUND(COALESCE((SELECT SUM(e.hours) FROM labor_entries e WHERE e.order_id IS NULL AND e.production_id=p.id),0),2) labor_hours,
          ROUND(COALESCE((SELECT SUM(e.labor_cost) FROM labor_entries e WHERE e.order_id IS NULL AND e.production_id=p.id),0),2) labor_cost,
          ROUND(CASE WHEN p.gallons>p.bulk_allocated THEN COALESCE((SELECT SUM(e.labor_cost) FROM labor_entries e WHERE e.order_id IS NULL AND e.production_id=p.id),0)/(p.gallons-p.bulk_allocated) ELSE 0 END,4) cost_per_unit
          FROM production p LEFT JOIN production parent ON parent.id=p.rework_of_id
          WHERE p.order_id IS NULL ORDER BY p.produced_at DESC,p.id DESC""").fetchall()]
    combined = {}
    for row in summary:
        item=combined.setdefault(row["s_number"],{"s_number":row["s_number"],"labor_cost":0.0,"labor_hours":0.0,"produced_gallons":0.0,"produced_qty":0.0,"gallon_cost":0.0,"qty_cost":0.0})
        item["labor_cost"]+=float(row["labor_cost"]); item["labor_hours"]+=float(row["labor_hours"])
        if row["unit"]=="qty": item["produced_qty"]+=float(row["produced_amount"]); item["qty_cost"]+=float(row["labor_cost"])
        else: item["produced_gallons"]+=float(row["produced_amount"]); item["gallon_cost"]+=float(row["labor_cost"])
    s_summary=[]
    for item in combined.values():
        item["cost_per_gallon"]=round(item["gallon_cost"]/item["produced_gallons"],4) if item["produced_gallons"] else 0
        item["cost_per_piece"]=round(item["qty_cost"]/item["produced_qty"],4) if item["produced_qty"] else 0
        item["labor_cost"]=round(item["labor_cost"],2); item["labor_hours"]=round(item["labor_hours"],2); s_summary.append(item)
    return {"orders": orders, "rates": rates, "current_rates": current_rates, "entries": entries, "summary": summary,
            "stock_summary":stock_summary,"s_summary":sorted(s_summary,key=lambda x:x["s_number"]),
            "processes": ["Mixing / Production", "Bulk Mixing / Production", "Batch Rework", "Quality Control", "Filling / Packaging", "Material Handling", "Shipping / Loading", "Other"]}


def normalize_report_date(value):
    result = str(value or "").strip()
    if not result:
        return ""
    try:
        return date.fromisoformat(result).isoformat()
    except ValueError:
        raise ValueError("Report dates must use YYYY-MM-DD.")


def reporting_data(date_from="", date_to=""):
    date_from = normalize_report_date(date_from); date_to = normalize_report_date(date_to)
    if date_from and date_to and date_from > date_to:
        raise ValueError("The report start date cannot be after the end date.")
    end_exclusive = (date.fromisoformat(date_to)+timedelta(days=1)).isoformat() if date_to else ""
    generated_at = datetime.now().isoformat(timespec="seconds")
    with closing(connect()) as con:
        period_metrics = _period_metrics_sql(con, date_from, end_exclusive)
        order_rows = [dict(x) for x in con.execute("""SELECT o.id order_id,o.s_number,o.po_number,o.brand,o.color,o.unit,o.created_at,
          ROUND(o.gallons,2) required_amount,
          ROUND(COALESCE((SELECT SUM(p.gallons) FROM production p WHERE p.order_id=o.id),0),2) produced_amount,
          ROUND(CASE WHEN o.gallons-COALESCE((SELECT SUM(p.gallons) FROM production p WHERE p.order_id=o.id),0)>0
            THEN o.gallons-COALESCE((SELECT SUM(p.gallons) FROM production p WHERE p.order_id=o.id),0) ELSE 0 END,2) need_amount,
          ROUND(COALESCE((SELECT SUM(k.gallons) FROM packaging k WHERE k.order_id=o.id),0),2) packaged_amount,
          ROUND(COALESCE((SELECT SUM(h.gallons) FROM shipments h JOIN packaging k ON k.id=h.packaging_id WHERE k.order_id=o.id),0),2) shipped_amount,
          ROUND(CASE WHEN COALESCE((SELECT SUM(k.gallons) FROM packaging k WHERE k.order_id=o.id),0)-
            COALESCE((SELECT SUM(h.gallons) FROM shipments h JOIN packaging k ON k.id=h.packaging_id WHERE k.order_id=o.id),0)>0
            THEN COALESCE((SELECT SUM(k.gallons) FROM packaging k WHERE k.order_id=o.id),0)-
            COALESCE((SELECT SUM(h.gallons) FROM shipments h JOIN packaging k ON k.id=h.packaging_id WHERE k.order_id=o.id),0)
            ELSE 0 END,2) inventory_amount,
          ROUND(COALESCE((SELECT SUM(e.hours) FROM labor_entries e WHERE e.order_id=o.id),0),2) labor_hours,
          ROUND(COALESCE((SELECT SUM(e.labor_cost) FROM labor_entries e WHERE e.order_id=o.id),0),2) labor_cost,
          ROUND(CASE WHEN COALESCE((SELECT SUM(p.gallons) FROM production p WHERE p.order_id=o.id),0)>0 THEN
            COALESCE((SELECT SUM(e.labor_cost) FROM labor_entries e WHERE e.order_id=o.id),0)/
            (SELECT SUM(p.gallons) FROM production p WHERE p.order_id=o.id) ELSE 0 END,4) cost_per_unit,
          s.done_at,s.ready_at,s.shipped_at,
          (SELECT MAX(h.shipped_at) FROM shipments h JOIN packaging k ON k.id=h.packaging_id WHERE k.order_id=o.id) last_shipped_at
          FROM orders o LEFT JOIN s_number_status s ON s.s_number=o.s_number ORDER BY o.s_number,o.brand,o.color,o.id""").fetchall()]
        package_rows = [dict(x) for x in con.execute("""SELECT r.id,r.order_id,r.s_number,o.po_number,r.brand,r.color,o.unit,o.created_at,
          r.package_type,r.package_size,r.required_count,
          COALESCE((SELECT SUM(k.package_count) FROM packaging k WHERE k.order_id=r.order_id AND k.package_type=r.package_type),0) packaged_count,
          COALESCE((SELECT SUM(h.package_count) FROM shipments h JOIN packaging k ON k.id=h.packaging_id
            WHERE k.order_id=r.order_id AND k.package_type=r.package_type),0) shipped_count,
          CASE WHEN COALESCE((SELECT SUM(k.package_count) FROM packaging k WHERE k.order_id=r.order_id AND k.package_type=r.package_type),0)-
            COALESCE((SELECT SUM(h.package_count) FROM shipments h JOIN packaging k ON k.id=h.packaging_id
            WHERE k.order_id=r.order_id AND k.package_type=r.package_type),0)>0
          THEN COALESCE((SELECT SUM(k.package_count) FROM packaging k WHERE k.order_id=r.order_id AND k.package_type=r.package_type),0)-
            COALESCE((SELECT SUM(h.package_count) FROM shipments h JOIN packaging k ON k.id=h.packaging_id
            WHERE k.order_id=r.order_id AND k.package_type=r.package_type),0) ELSE 0 END balance_count,
          s.ready_at,s.shipped_at FROM packaging_requirements r JOIN orders o ON o.id=r.order_id
          LEFT JOIN s_number_status s ON s.s_number=r.s_number ORDER BY r.s_number,r.brand,r.color,r.package_size DESC""").fetchall()]
        produced_totals = {x["unit"]: float(x["amount"]) for x in con.execute(
          "SELECT unit,COALESCE(SUM(gallons-bulk_allocated),0) amount FROM production GROUP BY unit").fetchall()}
        stock_inventory = {x["unit"]: float(x["amount"]) for x in con.execute("""SELECT p.unit,COALESCE(SUM(k.gallons),0) amount
          FROM stock_packaging k JOIN production p ON p.id=k.production_id GROUP BY p.unit""").fetchall()}
        total_labor_cost = float(con.execute("SELECT COALESCE(SUM(labor_cost),0) FROM labor_entries").fetchone()[0])
        stock_needs = stock_need_rows_sql(con)
        order_range_where, order_range_params = _date_range_sql("o.created_at", date_from, end_exclusive)
        need_rows = con.execute(f"""SELECT unit,ROUND(SUM(CASE WHEN remaining>0 THEN remaining ELSE 0 END),2) amount FROM (
          SELECT o.id,o.unit,o.gallons-COALESCE((SELECT SUM(p.gallons) FROM production p WHERE p.order_id=o.id),0) remaining
          FROM orders o{order_range_where}) ranged GROUP BY unit""", order_range_params).fetchall()
        range_need = {x["unit"]: float(x["amount"]) for x in need_rows}
        count_row = con.execute(f"""SELECT COUNT(DISTINCT o.s_number) s_numbers,
          COUNT(DISTINCT CASE WHEN s.shipped_at IS NULL THEN o.s_number END) active_s_numbers
          FROM orders o LEFT JOIN s_number_status s ON s.s_number=o.s_number{order_range_where}""", order_range_params).fetchone()
    for row in order_rows:
        row["status"] = "Shipped" if row["shipped_at"] else "Ready" if row["ready_at"] else "Packaging" if row["need_amount"] <= .0001 else "In Production"
    for row in package_rows:
        row["status"] = "Shipped" if row["shipped_at"] else "Ready" if row["ready_at"] and row["balance_count"] > 0 else "Packaging"
    s_map = {}; category_map = {}; po_map = {}; category_po_map = {}
    for row in order_rows:
        po_number = row["po_number"] or "No PO#"
        for target, key, base in ((s_map,(row["s_number"],row["unit"]),{"s_number":row["s_number"],"unit":row["unit"]}),
                                  (category_map,(row["brand"],row["unit"]),{"brand":row["brand"],"unit":row["unit"]}),
                                  (po_map,(po_number,row["unit"]),{"po_number":po_number,"unit":row["unit"]}),
                                  (category_po_map,(po_number,row["brand"],row["unit"]),{"po_number":po_number,"brand":row["brand"],"unit":row["unit"]})):
            item = target.setdefault(key,{**base,"required_amount":0.0,"produced_amount":0.0,"need_amount":0.0,"packaged_amount":0.0,
              "shipped_amount":0.0,"inventory_amount":0.0,"labor_hours":0.0,"labor_cost":0.0,"created_at":row["created_at"],"status":"Shipped"})
            for name in ("required_amount","produced_amount","need_amount","packaged_amount","shipped_amount","inventory_amount","labor_hours","labor_cost"):
                item[name] += float(row[name])
            rank={"Shipped":0,"Ready":1,"Packaging":2,"In Production":3}
            if rank[row["status"]] > rank[item["status"]]: item["status"] = row["status"]
    def finish(items):
        result=[]
        for item in items:
            for name in ("required_amount","produced_amount","need_amount","packaged_amount","shipped_amount","inventory_amount","labor_hours","labor_cost"):
                item[name]=round(item[name],2)
            item["cost_per_unit"]=round(item["labor_cost"]/item["produced_amount"],4) if item["produced_amount"] else 0
            result.append(item)
        return result
    s_summary = finish(sorted(s_map.values(),key=lambda x:(x["s_number"],x["unit"])))
    category_summary = finish(sorted(category_map.values(),key=lambda x:(x["brand"].lower(),x["unit"])))
    po_summary = finish(sorted(po_map.values(),key=lambda x:(x["po_number"].lower(),x["unit"])))
    category_po_summary = finish(sorted(category_po_map.values(),key=lambda x:(x["po_number"].lower(),x["brand"].lower(),x["unit"])))
    selected_stock_needs = [x for x in stock_needs if (not date_from or x["created_at"][:10]>=date_from) and (not date_to or x["created_at"][:10]<=date_to)]
    for row in selected_stock_needs:
        period_metrics[f"required_{'qty' if row['unit']=='qty' else 'gallons'}"] += float(row["amount"])
        range_need[row["unit"]] = range_need.get(row["unit"], 0)+float(row["remaining"])
    snapshot={**period_metrics,"date_from":date_from,"date_to":date_to,
      "need_gallons":round(range_need.get("gallons",0),2),"need_qty":round(range_need.get("qty",0),2),
      "inventory_gallons":round(max(period_metrics["packaged_gallons"]-period_metrics["shipped_gallons"],0),2),
      "inventory_qty":round(max(period_metrics["packaged_qty"]-period_metrics["shipped_qty"],0),2),
      "s_numbers":int(count_row["s_numbers"] or 0),"active_s_numbers":int(count_row["active_s_numbers"] or 0)}
    return {"generated_at":generated_at,"snapshot":snapshot,"product_lines":order_rows,"s_summary":s_summary,
            "category_summary":category_summary,"po_summary":po_summary,"category_po_summary":category_po_summary,
            "package_rows":package_rows,"stock_needs":stock_needs,"package_types":package_type_data()}


def record_labor_sql(con, payload, order=None, production=None, process_name="Production", actor="System"):
    hours = float(payload.get("labor_hours",0) or 0); rate_id = int(payload.get("rate_id",0) or 0)
    if hours == 0 and not rate_id: return None
    if not isfinite(hours) or hours <= 0 or not rate_id: raise ValueError("Select a labor rate and enter labor hours greater than zero.")
    rate = con.execute("SELECT * FROM labor_rates WHERE id=? AND active=1",(rate_id,)).fetchone()
    if not rate: raise ValueError("The selected labor rate is unavailable.")
    source = order or production; cost = round(hours*float(rate["hourly_rate"]),2); worked_at = datetime.now().isoformat(timespec="seconds")
    con.execute("""INSERT INTO labor_entries(order_id,production_id,s_number,brand,color,unit,process_name,labor_name,hours,hourly_rate,labor_cost,worked_at,notes,created_at,created_by)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(order["id"] if order else None,production["id"] if production else None,
      order["s_number"] if order else "STOCK",source["brand"],source["color"],source["unit"],process_name,rate["labor_name"],hours,
      rate["hourly_rate"],cost,worked_at,str(payload.get("labor_notes","")).strip(),worked_at,actor))
    return cost


def allocate_stock_labor_sql(con, production_id, order, assigned_production_id, ratio, actor):
    for entry in con.execute("SELECT * FROM labor_entries WHERE order_id IS NULL AND production_id=?",(production_id,)).fetchall():
        moved_hours=round(float(entry["hours"])*ratio,4); moved_cost=round(float(entry["labor_cost"])*ratio,2)
        if moved_hours<=0: continue
        now=datetime.now().isoformat(timespec="seconds")
        con.execute("""INSERT INTO labor_entries(order_id,production_id,s_number,brand,color,unit,process_name,labor_name,hours,hourly_rate,labor_cost,worked_at,notes,created_at,created_by)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(order["id"],assigned_production_id,order["s_number"],order["brand"],order["color"],order["unit"],entry["process_name"],
          entry["labor_name"],moved_hours,entry["hourly_rate"],moved_cost,entry["worked_at"],(entry["notes"]+" | Allocated from future stock").strip(" |"),now,actor))
        remaining_hours=round(float(entry["hours"])-moved_hours,4); remaining_cost=round(float(entry["labor_cost"])-moved_cost,2)
        if remaining_hours<=0.0001: con.execute("DELETE FROM labor_entries WHERE id=?",(entry["id"],))
        else: con.execute("UPDATE labor_entries SET hours=?,labor_cost=? WHERE id=?",(remaining_hours,remaining_cost,entry["id"]))


def labor_report_csv():
    data = costing_data(); output = io.StringIO(newline=""); writer = csv.writer(output)
    writer.writerow(["COMBINED S# COST"])
    writer.writerow(["S#", "Labor Hours", "Produced Gallons", "Cost Per Gallon", "Produced Pieces", "Cost Per Piece", "Total Labor Cost"])
    for row in data["s_summary"]:
        writer.writerow([row["s_number"],row["labor_hours"],row["produced_gallons"],row["cost_per_gallon"],row["produced_qty"],row["cost_per_piece"],row["labor_cost"]])
    writer.writerow([]); writer.writerow(["S# PRODUCT LINE COST"])
    writer.writerow(["S#", "Category", "Color", "Unit", "Ordered Amount", "Produced Amount", "Labor Hours", "Labor Cost", "Labor Cost Per Produced Unit"])
    for row in data["summary"]:
        writer.writerow([row["s_number"],row["brand"],row["color"],"piece" if row["unit"]=="qty" else "gallon",
                         row["ordered_amount"],row["produced_amount"],row["labor_hours"],row["labor_cost"],row["cost_per_unit"]])
    writer.writerow([]); writer.writerow(["BULK AND STOCK BATCH COST"])
    writer.writerow(["Batch", "Category", "Color", "Unit", "Unallocated Amount", "Labor Hours", "Labor Cost", "Labor Cost Per Unit", "QC Status", "Rework Of"])
    for row in data["stock_summary"]:
        writer.writerow([row["batch_number"],row["brand"],row["color"],"piece" if row["unit"]=="qty" else "gallon",
                         row["produced_amount"],row["labor_hours"],row["labor_cost"],row["cost_per_unit"],
                         row["quality_status"],row["rework_of_batch"] or ""])
    return output.getvalue().encode("utf-8-sig")


class Handler(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(30)

    def _send_bytes(self, data, status=200, content_type="application/octet-stream", cache_control="no-store", extra_headers=None):
        payload = data
        compressed = False
        if len(data) >= 1024 and "gzip" in self.headers.get("Accept-Encoding", "").lower():
            payload = gzip.compress(data, compresslevel=5)
            compressed = True
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", cache_control)
        if compressed:
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Vary", "Accept-Encoding")
        for name, value in (extra_headers or {}).items():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _internal_error(self, exc):
        print(f"Unhandled request error: {self.command} {self.path}: {exc}", file=sys.stderr, flush=True)
        traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)
        try:
            return self.send_json({"error": "The service had a temporary problem. Please try again."}, 500)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return None

    def send_json(self, obj, status=200):
        if status < 400 and self.command in ("POST", "PUT", "DELETE"):
            invalidate_dashboard_cache()
        data = json.dumps(obj, separators=(",", ":")).encode()
        return self._send_bytes(data, status, "application/json; charset=utf-8")

    def body(self):
        size = int(self.headers.get("Content-Length", 0))
        if size < 0 or size > MAX_REQUEST_BYTES:
            raise ValueError("Request is too large.")
        return json.loads(self.rfile.read(size) or b"{}")

    def send_csv(self, data, filename):
        return self._send_bytes(data, 200, "text/csv; charset=utf-8", "no-store", {
            "Content-Disposition": f'attachment; filename="{filename}"'
        })

    def send_file(self, filename):
        data, etag = cached_static_file(filename)
        is_asset = filename.endswith((".css", ".js"))
        cache_control = "public, max-age=300, must-revalidate" if is_asset else "no-cache"
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("Cache-Control", cache_control)
            self.send_header("ETag", etag)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        content_type = (
            "text/css; charset=utf-8" if filename.endswith(".css")
            else "application/javascript; charset=utf-8" if filename.endswith(".js")
            else "text/html; charset=utf-8"
        )
        return self._send_bytes(data, 200, content_type, cache_control, {"ETag": etag})

    def send_redirect(self, location):
        self.send_response(302); self.send_header("Location", location)
        self.send_header("Content-Length", "0"); self.end_headers()

    def session_cookie(self, token="", max_age=0):
        secure = "; Secure" if (
            os.environ.get("RENDER", "").strip().lower() == "true"
            or self.headers.get("X-Forwarded-Proto", "").strip().lower() == "https"
        ) else ""
        return (
            f"ollies_session={token}; Path=/; HttpOnly; SameSite=Lax; "
            f"Max-Age={int(max_age)}{secure}"
        )

    def full_ui_authorized(self):
        if not self.authorized(): return False
        if self.current_user().get("role") == "team":
            self.send_redirect("/entry"); return False
        return True

    def session_token(self):
        for item in self.headers.get("Cookie", "").split(";"):
            key, _, value = item.strip().partition("=")
            if key == "ollies_session": return value
        return ""

    def current_user(self):
        return session_data(self.session_token())

    def authorized(self):
        if self.current_user(): return True
        if urlparse(self.path).path.startswith("/api/"):
            self.send_json({"error": "Login required."}, 401)
        else:
            self.send_response(302); self.send_header("Location", "/login"); self.end_headers()
        return False

    def require_role(self, action):
        user = self.current_user()
        if not user:
            self.send_json({"error": "Login required."}, 401); return None
        allowed = {"add": {"admin","manager","team"}, "edit": {"admin","manager"}, "delete": {"admin"}, "admin": {"admin"}}
        if user["role"] not in allowed[action]:
            self.send_json({"error": f"Your {user['role']} role cannot perform this action."}, 403); return None
        return user

    def do_GET(self):
        try:
            return self._do_GET()
        except DatabaseOperationalError:
            return self.send_json({"error": "The database is busy. Please try again in a moment."}, 503)
        except Exception as exc:
            return self._internal_error(exc)

    def _do_GET(self):
        path = urlparse(self.path).path
        if path == "/health":
            return self.send_json({
                "ok": True,
                "uptime_seconds": int(time.monotonic() - PROCESS_STARTED_AT),
                "database": "supabase" if USE_SUPABASE else "sqlite",
            })
        if path == "/ready":
            try:
                with closing(connect()) as con:
                    con.execute("SELECT 1").fetchone()
                return self.send_json({"ok": True, "database": "supabase" if USE_SUPABASE else "sqlite"})
            except Exception:
                return self.send_json({"ok": False}, 503)
        if path == "/api/tv-dashboard": return self.send_json(cached_read("tv-dashboard", tv_dashboard_data, 5))
        if path == "/api/dashboard-summary":
            if not self.authorized(): return
            return self.send_json(cached_read("dashboard-summary", dashboard_summary_data, 5))
        if path == "/api/dashboard": return self.send_json(cached_dashboard_data())
        if path == "/api/bulk-production/legacy":
            if not self.require_role("edit"): return
            search = parse_qs(urlparse(self.path).query).get("q", [""])[0][:100]
            with closing(connect()) as con:
                return self.send_json(eligible_legacy_batches_sql(con, search))
        override_match = re.fullmatch(r"/api/bulk-production/(\d+)/overrides", path)
        if override_match:
            if not self.require_role("edit"): return
            with closing(connect()) as con:
                rows = con.execute("""SELECT previous_status,new_status,scheduled_due_at,reason,decided_by,decided_at
                  FROM production_qc_overrides WHERE production_id=? ORDER BY id DESC LIMIT 20""",
                  (int(override_match.group(1)),)).fetchall()
                return self.send_json([dict(row) for row in rows])
        detail_match = re.fullmatch(r"/api/s-numbers/([^/]+)/detail", path)
        if detail_match:
            if not self.authorized(): return
            detail = s_number_detail(unquote(detail_match.group(1)))
            return self.send_json(detail, 200) if detail else self.send_json({"error": "Order / S# was not found."}, 404)
        if path == "/app.css": return self.send_file("app.css")
        if path == "/app-runtime.js": return self.send_file("app-runtime.js")
        if path in ("/login", "/login.html"): return self.send_file("login.html")
        if path == "/logout":
            clear_session(self.session_token())
            self.send_response(302); self.send_header("Location", "/login")
            self.send_header("Set-Cookie", self.session_cookie())
            self.end_headers(); return
        if path == "/api/session":
            user = self.current_user()
            return self.send_json({"user": user}, 200 if user else 401)
        if path == "/api/reports/category.csv":
            if not self.authorized(): return
            return self.send_csv(category_report_csv(), f"ollies-category-production-{date.today().isoformat()}.csv")
        if path == "/api/costing":
            if not self.authorized(): return
            return self.send_json(cached_read("costing", costing_data))
        if path == "/api/reporting":
            if not self.authorized(): return
            query = parse_qs(urlparse(self.path).query)
            try:
                date_from = query.get("from", [""])[0]
                date_to = query.get("to", [""])[0]
                return self.send_json(cached_read(
                    ("reporting", date_from, date_to),
                    lambda: reporting_data(date_from, date_to),
                ))
            except ValueError as exc:
                return self.send_json({"error": str(exc)}, 400)
        if path == "/api/reports/labor.csv":
            if not self.authorized(): return
            return self.send_csv(labor_report_csv(), f"ollies-labor-cost-{date.today().isoformat()}.csv")
        if path == "/api/catalog":
            if not self.authorized(): return
            return self.send_json(cached_read("catalog", catalog_data, 10))
        if path == "/api/contacts":
            if not self.authorized(): return
            return self.send_json(cached_read("contacts", contact_data, 5))
        if path == "/api/admin":
            if not self.authorized(): return
            result = admin_data(); user = self.current_user(); result["current_user"] = user
            result["users"] = user_data() if user["role"] == "admin" else []
            return self.send_json(result)
        if path in ("/entry", "/entry.html"):
            if not self.authorized(): return
            return self.send_file("entry.html")
        if path in ("/", "/index.html"):
            if not self.full_ui_authorized(): return
            return self.send_file("index.html")
        if path in ("/production", "/production.html"):
            if not self.full_ui_authorized(): return
            return self.send_file("production.html")
        if path in ("/inventory", "/inventory.html"):
            if not self.full_ui_authorized(): return
            return self.send_file("inventory.html")
        if path in ("/shipping", "/shipping.html"):
            if not self.full_ui_authorized(): return
            return self.send_file("shipping.html")
        if path in ("/costing", "/costing.html"):
            if not self.full_ui_authorized(): return
            return self.send_file("costing.html")
        if path in ("/reporting", "/reporting.html"):
            if not self.full_ui_authorized(): return
            return self.send_file("reporting.html")
        if path in ("/tv", "/tv.html"):
            return self.send_file("tv.html")
        if path in ("/admin", "/admin.html"):
            if not self.full_ui_authorized(): return
            return self.send_file("admin.html")
        self.send_error(404)

    def do_POST(self):
        try:
            return self._do_POST()
        except DatabaseOperationalError:
            return self.send_json({"error": "The database is busy. Please try again in a moment."}, 503)
        except Exception as exc:
            return self._internal_error(exc)

    def _do_POST(self):
        try:
            payload = self.body(); path = urlparse(self.path).path
            if path == "/api/login":
                login = str(payload.get("email", payload.get("username", ""))).strip().lower(); password = str(payload.get("password", ""))
                with closing(connect()) as con:
                    user = con.execute("SELECT id,username,email,display_name,password_hash,role FROM users WHERE active=1 AND (lower(email)=? OR lower(username)=?)", (login,login)).fetchone()
                if not user or not verify_password(password, user["password_hash"]):
                    return self.send_json({"error": "Incorrect username or password."}, 401)
                token = create_session(user); data = json.dumps({"ok": True, "role": user["role"]}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie", self.session_cookie(token, SESSION_TTL_SECONDS))
                self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
            if path == "/api/logout":
                clear_session(self.session_token()); data = json.dumps({"ok": True}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie", self.session_cookie())
                self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
            if path == "/api/daily-targets":
                user = self.require_role("edit")
                if not user: return
                target_date = str(payload.get("target_date", "")).strip()
                if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", target_date): raise ValueError("Select a target date.")
                try: date.fromisoformat(target_date)
                except ValueError: raise ValueError("Select a valid target date.")
                scope = str(payload.get("scope", "combined")).strip().lower()
                brand = str(payload.get("brand", "")).strip() if scope == "product" else ""
                color = str(payload.get("color", "")).strip() if scope == "product" else ""
                unit = str(payload.get("unit", "")).strip().lower()
                amount = float(payload.get("amount", 0))
                if amount < 0 or amount > 1000000000: raise ValueError("Target must be zero or a positive number below one billion.")
                actor = user.get("display_name") or user.get("email") or user.get("username", "")
                with closing(connect()) as con:
                    if scope == "product":
                        item = con.execute("SELECT unit FROM catalog WHERE category=? AND color=?", (brand,color)).fetchone()
                        if not item: raise ValueError("Select a category and color from the catalog.")
                        unit = item["unit"]
                    elif scope != "combined" or unit not in ("gallons","qty"):
                        raise ValueError("Select a combined unit or a category target.")
                    now = datetime.now().isoformat(timespec="seconds")
                    con.execute("""INSERT INTO daily_production_targets(target_date,brand,color,unit,amount,updated_at,updated_by)
                      VALUES(?,?,?,?,?,?,?) ON CONFLICT(target_date,brand,color,unit) DO UPDATE SET
                      amount=excluded.amount,updated_at=excluded.updated_at,updated_by=excluded.updated_by""",
                      (target_date,brand,color,unit,round(amount,2),now,actor)); con.commit()
                return self.send_json({"ok":True,"target_date":target_date,"brand":brand,"color":color,"unit":unit,"amount":round(amount,2)})
            if path == "/api/targets":
                user = self.require_role("admin")
                if not user: return
                daily_gallons = float(payload.get("daily_gallons", 0)); daily_qty = float(payload.get("daily_qty", 0))
                if daily_gallons < 0 or daily_qty < 0 or daily_gallons > 1000000000 or daily_qty > 1000000000:
                    raise ValueError("Targets must be zero or a positive number below one billion.")
                actor = user.get("display_name") or user.get("email") or user.get("username", "")
                now = datetime.now().isoformat(timespec="seconds")
                with closing(connect()) as con:
                    con.execute("""INSERT INTO production_targets(id,daily_gallons,daily_qty,updated_at,updated_by)
                      VALUES(1,?,?,?,?) ON CONFLICT(id) DO UPDATE SET daily_gallons=excluded.daily_gallons,
                      daily_qty=excluded.daily_qty,updated_at=excluded.updated_at,updated_by=excluded.updated_by""",
                      (round(daily_gallons,2),round(daily_qty,2),now,actor))
                    con.commit()
                return self.send_json({"ok": True, "daily_gallons": round(daily_gallons,2), "daily_qty": round(daily_qty,2)})
            if path == "/api/users":
                if not self.require_role("admin"): return
                email = str(payload.get("email", payload.get("username", ""))).strip().lower(); display_name = str(payload.get("display_name", "")).strip()
                password = str(payload.get("password", "")); role = str(payload.get("role", "")).strip()
                if not valid_email(email) or not display_name or len(password) < 8 or role not in ("admin","manager","team"):
                    raise ValueError("Enter a valid email, display name, role, and a password of at least 8 characters.")
                with closing(connect()) as con:
                    con.execute("INSERT INTO users(username,email,display_name,password_hash,role,active,created_at) VALUES(?,?,?,?,?,1,?)",
                                (email,email,display_name,hash_password(password),role,datetime.now().isoformat(timespec="seconds"))); con.commit()
                return self.send_json({"ok": True}, 201)
            undo_match = re.fullmatch(r"/api/shipments/(\d+)/undo", path)
            if undo_match:
                if not self.require_role("edit"): return
                shipment_id = int(undo_match.group(1))
                with closing(connect()) as con:
                    shipment = con.execute("SELECT packaging_id,s_number FROM shipments WHERE id=?", (shipment_id,)).fetchone()
                    if not shipment: raise ValueError("Shipment record was not found.")
                    lock_packaging_sql(con, shipment["packaging_id"])
                    con.execute("DELETE FROM shipments WHERE id=?", (shipment_id,)); refresh_s_number_shipped(con,shipment["s_number"]); con.commit()
                return self.send_json({"ok": True})
            if path == "/api/shipments":
                user = self.require_role("edit")
                if not user: return
                packaging_id = int(payload.get("packaging_id", 0)); raw_count = float(payload.get("package_count", 0)); package_count = int(raw_count)
                if raw_count != package_count or package_count <= 0: raise ValueError("Ship quantity must be a whole number greater than zero.")
                actor = user.get("display_name") or user.get("email") or user.get("username", "")
                with closing(connect()) as con:
                    lock_packaging_sql(con, packaging_id)
                    item = con.execute("""SELECT k.*,s.ready_at,k.package_count-
                      COALESCE((SELECT SUM(h.package_count) FROM shipments h WHERE h.packaging_id=k.id),0) available
                      FROM packaging k LEFT JOIN s_number_status s ON s.s_number=k.s_number WHERE k.id=?""", (packaging_id,)).fetchone()
                    if not item: raise ValueError("Package inventory record was not found.")
                    if not item["ready_at"]: raise ValueError("The S# must be Ready before shipping packages.")
                    if package_count > item["available"]: raise ValueError(f"Only {item['available']} package(s) remain in inventory.")
                    con.execute("""INSERT INTO shipments(packaging_id,s_number,brand,color,package_type,package_size,package_count,gallons,shipped_at,shipped_by)
                      VALUES(?,?,?,?,?,?,?,?,?,?)""", (packaging_id,item["s_number"],item["brand"],item["color"],item["package_type"],
                      item["package_size"],package_count,round(item["package_size"]*package_count,2),datetime.now().isoformat(timespec="seconds"),actor))
                    refresh_s_number_shipped(con,item["s_number"]); con.commit()
                return self.send_json({"ok": True}, 201)
            status_match = re.fullmatch(r"/api/s-numbers/([SN]\d{5})/(done|ship)", path)
            if status_match:
                user = self.require_role("edit")
                if not user: return
                s_number, action = status_match.groups(); now = datetime.now().isoformat(timespec="seconds")
                actor = user.get("display_name") or user.get("email") or user.get("username", "")
                with closing(connect()) as con:
                    lock_s_number_sql(con, s_number)
                    if not con.execute("SELECT 1 FROM orders WHERE s_number=?", (s_number,)).fetchone():
                        raise ValueError("S# was not found.")
                    status = con.execute("SELECT done_at,ready_at,shipped_at FROM s_number_status WHERE s_number=?", (s_number,)).fetchone()
                    if status and status["shipped_at"]: raise ValueError("This S# has already shipped.")
                    if action == "done":
                        incomplete = con.execute("""SELECT COUNT(*) FROM orders o WHERE o.s_number=? AND
                          o.gallons-COALESCE((SELECT SUM(p.gallons) FROM production p WHERE p.order_id=o.id),0) > 0.0001""", (s_number,)).fetchone()[0]
                        if incomplete: raise ValueError("Finish producing every order line before marking this S# done.")
                        unpackaged = con.execute("""SELECT COALESCE(SUM(available),0) FROM (
                          SELECT COALESCE((SELECT SUM(p.gallons) FROM production p WHERE p.order_id=o.id),0)-
                                 COALESCE((SELECT SUM(k.gallons) FROM packaging k WHERE k.order_id=o.id),0) available
                          FROM orders o WHERE o.s_number=? AND o.unit='gallons')""", (s_number,)).fetchone()[0]
                        if unpackaged > .0001: raise ValueError(f"Package the remaining {unpackaged:.2f} produced gallons before marking this S# done.")
                        gallon_lines = con.execute("SELECT COUNT(*) FROM orders WHERE s_number=? AND unit='gallons'", (s_number,)).fetchone()[0]
                        if gallon_lines and not con.execute("SELECT 1 FROM packaging WHERE s_number=?", (s_number,)).fetchone():
                            raise ValueError("Add the package inventory before marking this S# done.")
                        if not refresh_s_number_ready(con,s_number,actor):
                            raise ValueError("Enter and complete all packaging requirements before marking this S# ready.")
                    else:
                        if not status or not status["ready_at"]: raise ValueError("The S# must be Ready before it can be marked Shipped.")
                        if not con.execute("SELECT 1 FROM packaging WHERE s_number=?", (s_number,)).fetchone():
                            raise ValueError("This S# has no packaged inventory to ship.")
                        remaining_packages = con.execute("""SELECT COALESCE(SUM(k.package_count-
                          COALESCE((SELECT SUM(h.package_count) FROM shipments h WHERE h.packaging_id=k.id),0)),0)
                          FROM packaging k WHERE k.s_number=?""", (s_number,)).fetchone()[0]
                        if remaining_packages: raise ValueError(f"{remaining_packages} package(s) are still in inventory. Ship them before changing the S# status.")
                        con.execute("UPDATE s_number_status SET shipped_at=?,shipped_by=? WHERE s_number=?", (now,actor,s_number))
                    con.commit()
                return self.send_json({"ok": True, "s_number": s_number, "status": action})
            if path == "/api/inventory/assign":
                user = self.require_role("edit")
                if not user: return
                stock_packaging_id = int(payload.get("stock_packaging_id", 0)); order_id = int(payload.get("order_id", 0))
                raw_count = float(payload.get("package_count", 0)); package_count = int(raw_count)
                if raw_count != package_count or package_count <= 0: raise ValueError("Transfer quantity must be a whole number greater than zero.")
                actor = user.get("display_name") or user.get("email") or user.get("username", "")
                with closing(connect()) as con:
                    lock_bulk_production_sql(con)
                    if USE_SUPABASE: lock_order_sql(con,order_id)
                    stock = con.execute("""SELECT k.*,p.batch_number,p.gallons-p.bulk_allocated production_gallons,p.unit,
                      p.bulk_purpose,p.produced_at,p.quality_status FROM stock_packaging k
                      JOIN production p ON p.id=k.production_id WHERE k.id=?""", (stock_packaging_id,)).fetchone()
                    if not stock: raise ValueError("Stock package record was not found.")
                    if stock["quality_status"] != "ready": raise ValueError("Only QC-released stock can be allocated.")
                    if package_count > stock["package_count"]: raise ValueError(f"Only {stock['package_count']} package(s) are available in stock.")
                    order = con.execute("""SELECT o.*,s.done_at,s.shipped_at,o.gallons-
                      COALESCE((SELECT SUM(p.gallons) FROM production p WHERE p.order_id=o.id),0) remaining
                      FROM orders o LEFT JOIN s_number_status s ON s.s_number=o.s_number WHERE o.id=?""", (order_id,)).fetchone()
                    if not order: raise ValueError("Target order line was not found.")
                    if order["done_at"] or order["shipped_at"]: raise ValueError("The target S# is already done or shipped.")
                    if order["unit"] != stock["unit"] or order["brand"] != stock["brand"] or order["color"] != stock["color"]:
                        raise ValueError("Stock can only move to an S# with the same category, color, and unit.")
                    assigned_gallons = round(stock["package_size"]*package_count,2)
                    if assigned_gallons > order["remaining"] + .0001:
                        raise ValueError(f"The target order needs only {order['remaining']:.2f} gallons; this transfer is {assigned_gallons:.2f} gallons.")
                    allocation_batch = f"{stock['batch_number']}-S{order_id}-A{stock_packaging_id}-{time.time_ns()}"
                    cur=con.execute("INSERT INTO production(order_id,brand,color,unit,batch_number,gallons,produced_at) VALUES(?,?,?,?,?,?,?)",
                                 (order_id,stock["brand"],stock["color"],stock["unit"],allocation_batch,assigned_gallons,
                                  stock["produced_at"] if stock["bulk_purpose"] else datetime.now().isoformat(timespec="seconds")))
                    assigned_production_id=cur.lastrowid
                    con.execute("""INSERT INTO packaging(order_id,s_number,brand,color,package_type,package_size,package_count,gallons,packaged_at,created_by)
                      VALUES(?,?,?,?,?,?,?,?,?,?)""", (order_id,order["s_number"],stock["brand"],stock["color"],stock["package_type"],
                      stock["package_size"],package_count,assigned_gallons,stock["packaged_at"],actor))
                    remaining_packages = stock["package_count"]-package_count; remaining_gallons = round(stock["production_gallons"]-assigned_gallons,2)
                    if remaining_packages:
                        con.execute("UPDATE stock_packaging SET package_count=?,gallons=? WHERE id=?",
                                    (remaining_packages,round(stock["package_size"]*remaining_packages,2),stock_packaging_id))
                    else: con.execute("DELETE FROM stock_packaging WHERE id=?", (stock_packaging_id,))
                    release_stock_need_sql(con,stock["production_id"],assigned_gallons)
                    allocate_stock_labor_sql(con,stock["production_id"],order,assigned_production_id,assigned_gallons/float(stock["production_gallons"]),actor)
                    if stock["bulk_purpose"]:
                        con.execute("UPDATE production SET bulk_allocated=bulk_allocated+? WHERE id=?", (assigned_gallons,stock["production_id"]))
                    elif remaining_gallons > .0001: con.execute("UPDATE production SET gallons=? WHERE id=?", (remaining_gallons,stock["production_id"]))
                    else: con.execute("DELETE FROM production WHERE id=?", (stock["production_id"],))
                    refresh_s_number_ready(con,order["s_number"],actor)
                    con.commit()
                return self.send_json({"ok": True, "assigned_gallons": assigned_gallons}, 201)
            if path == "/api/inventory/rework":
                user = self.require_role("add")
                if not user: return
                stock_packaging_id = int(payload.get("stock_packaging_id", 0)); order_id = int(payload.get("order_id", 0))
                raw_count = float(payload.get("package_count", 0)); package_count = int(raw_count)
                batch = str(payload.get("batch_number", "")).strip()
                if raw_count != package_count or package_count <= 0: raise ValueError("Package count must be a whole number greater than zero.")
                if not batch: raise ValueError("Enter a batch number for the tinted and relabeled product.")
                actor = user.get("display_name") or user.get("email") or user.get("username", "")
                with closing(connect()) as con:
                    lock_bulk_production_sql(con)
                    if USE_SUPABASE: lock_order_sql(con,order_id)
                    stock = con.execute("""SELECT k.*,p.batch_number source_batch,p.gallons-p.bulk_allocated production_gallons,p.unit,
                      p.bulk_purpose,p.quality_status
                      FROM stock_packaging k JOIN production p ON p.id=k.production_id WHERE k.id=?""", (stock_packaging_id,)).fetchone()
                    if not stock: raise ValueError("Packaged source material was not found.")
                    if stock["quality_status"] != "ready": raise ValueError("Only QC-released stock can be used.")
                    if package_count > stock["package_count"]: raise ValueError(f"Only {stock['package_count']} package(s) are available in this source stock.")
                    order = con.execute("""SELECT o.*,s.done_at,s.shipped_at,o.gallons-
                      COALESCE((SELECT SUM(p.gallons) FROM production p WHERE p.order_id=o.id),0) remaining
                      FROM orders o LEFT JOIN s_number_status s ON s.s_number=o.s_number WHERE o.id=?""", (order_id,)).fetchone()
                    if not order: raise ValueError("Target S# order line was not found.")
                    if order["done_at"] or order["shipped_at"]: raise ValueError("The target S# is already ready or shipped.")
                    if order["unit"] != stock["unit"]: raise ValueError("Source stock and the target S# must use the same unit.")
                    requirement = con.execute("SELECT * FROM packaging_requirements WHERE order_id=? AND package_type=?",
                                              (order_id,stock["package_type"])).fetchone()
                    if not requirement or abs(float(requirement["package_size"])-float(stock["package_size"])) > .0001:
                        raise ValueError("First add a matching packaging requirement for this S# and package size.")
                    completed = con.execute("SELECT COALESCE(SUM(package_count),0) FROM packaging WHERE order_id=? AND package_type=?",
                                            (order_id,stock["package_type"])).fetchone()[0]
                    packages_needed = int(requirement["required_count"])-int(completed)
                    if package_count > packages_needed: raise ValueError(f"This S# needs only {packages_needed} more package(s) of this type.")
                    converted = round(float(stock["package_size"])*package_count,2)
                    if converted > float(order["remaining"]) + .0001:
                        raise ValueError(f"The target order needs only {order['remaining']:.2f} {order['unit']}; this conversion is {converted:.2f}.")
                    now = datetime.now().isoformat(timespec="seconds")
                    cur = con.execute("INSERT INTO production(order_id,brand,color,unit,batch_number,gallons,produced_at) VALUES(?,?,?,?,?,?,?)",
                                      (order_id,order["brand"],order["color"],order["unit"],batch,converted,now))
                    production_id = cur.lastrowid
                    con.execute("""INSERT INTO packaging(order_id,s_number,brand,color,package_type,package_size,package_count,gallons,packaged_at,created_by)
                      VALUES(?,?,?,?,?,?,?,?,?,?)""", (order_id,order["s_number"],order["brand"],order["color"],stock["package_type"],
                      stock["package_size"],package_count,converted,now,actor))
                    remaining_packages = int(stock["package_count"])-package_count
                    remaining_source = round(float(stock["production_gallons"])-converted,2)
                    if remaining_packages:
                        con.execute("UPDATE stock_packaging SET package_count=?,gallons=? WHERE id=?",
                                    (remaining_packages,round(float(stock["package_size"])*remaining_packages,2),stock_packaging_id))
                    else: con.execute("DELETE FROM stock_packaging WHERE id=?",(stock_packaging_id,))
                    release_stock_need_sql(con,stock["production_id"],converted)
                    allocate_stock_labor_sql(con,stock["production_id"],order,production_id,converted/float(stock["production_gallons"]),actor)
                    labor_payload = dict(payload); source_note = f"Tinted, relabeled, and fitted with a new lid from source batch {stock['source_batch']}"
                    labor_payload["labor_notes"] = (source_note + (" | " + str(payload.get("labor_notes","")).strip() if str(payload.get("labor_notes","")).strip() else ""))
                    labor_cost = record_labor_sql(con,labor_payload,order=order,production={"id":production_id},
                                                  process_name="Tint / Relabel / New Lid",actor=actor)
                    if stock["bulk_purpose"]:
                        con.execute("UPDATE production SET bulk_allocated=bulk_allocated+? WHERE id=?", (converted,stock["production_id"]))
                    elif remaining_source > .0001: con.execute("UPDATE production SET gallons=? WHERE id=?",(remaining_source,stock["production_id"]))
                    else: con.execute("DELETE FROM production WHERE id=?",(stock["production_id"],))
                    refresh_s_number_ready(con,order["s_number"],actor); con.commit()
                    remaining_after = round(float(order["remaining"])-converted,2)
                email_status = send_production_email(order_id,batch,converted,remaining_after)
                return self.send_json({"ok":True,"converted_amount":converted,"labor_cost":labor_cost,"email_status":email_status},201)
            if path == "/api/feed-rework":
                user = self.require_role("add")
                if not user: return
                target_type = str(payload.get("target_type","order")).strip(); batch = str(payload.get("batch_number","")).strip()
                feed_lot = str(payload.get("feed_lot","")).strip(); raw_count = float(payload.get("package_count",0)); package_count = int(raw_count)
                if target_type not in ("order","stock"): raise ValueError("Select whether this feed is for an S# or a no-S# need.")
                if not batch or not feed_lot: raise ValueError("Enter the supplied feed lot and the finished batch number.")
                if raw_count != package_count or package_count <= 0: raise ValueError("Package count must be a whole number greater than zero.")
                actor = user.get("display_name") or user.get("email") or user.get("username",""); now = datetime.now().isoformat(timespec="seconds")
                email_order_id = None; remaining_after = None
                with closing(connect()) as con:
                    if target_type == "order":
                        requirement_id = int(payload.get("requirement_id",0))
                        row = con.execute("""SELECT r.*,o.id target_order_id,o.s_number target_s_number,o.brand target_brand,o.color target_color,
                          o.unit target_unit,o.gallons order_amount,s.done_at,s.shipped_at,o.gallons-
                          COALESCE((SELECT SUM(p.gallons) FROM production p WHERE p.order_id=o.id),0) remaining
                          FROM packaging_requirements r JOIN orders o ON o.id=r.order_id
                          LEFT JOIN s_number_status s ON s.s_number=o.s_number WHERE r.id=?""",(requirement_id,)).fetchone()
                        if not row: raise ValueError("Select an S# packaging requirement.")
                        if row["done_at"] or row["shipped_at"]: raise ValueError("The target S# is already ready or shipped.")
                        completed = con.execute("SELECT COALESCE(SUM(package_count),0) FROM packaging WHERE order_id=? AND package_type=?",
                                                (row["target_order_id"],row["package_type"])).fetchone()[0]
                        packages_needed = int(row["required_count"])-int(completed)
                        if package_count > packages_needed: raise ValueError(f"This requirement needs only {packages_needed} more package(s).")
                        amount = round(float(row["package_size"])*package_count,2)
                        if amount > float(row["remaining"]) + .0001:
                            raise ValueError(f"The order needs only {row['remaining']:.2f} {row['target_unit']}; this feed entry is {amount:.2f}.")
                        cur = con.execute("INSERT INTO production(order_id,brand,color,unit,batch_number,gallons,produced_at) VALUES(?,?,?,?,?,?,?)",
                          (row["target_order_id"],row["target_brand"],row["target_color"],row["target_unit"],batch,amount,now))
                        production_id = cur.lastrowid
                        con.execute("""INSERT INTO packaging(order_id,s_number,brand,color,package_type,package_size,package_count,gallons,packaged_at,created_by)
                          VALUES(?,?,?,?,?,?,?,?,?,?)""",(row["target_order_id"],row["target_s_number"],row["target_brand"],row["target_color"],
                          row["package_type"],row["package_size"],package_count,amount,now,actor))
                        order = {"id":row["target_order_id"],"s_number":row["target_s_number"],"brand":row["target_brand"],
                                 "color":row["target_color"],"unit":row["target_unit"]}
                        note = f"Used supplied packaged feed lot {feed_lot}; tinted, relabeled, and fitted with new lids"
                        labor_payload = dict(payload); extra = str(payload.get("labor_notes","")).strip(); labor_payload["labor_notes"] = note + (" | "+extra if extra else "")
                        labor_cost = record_labor_sql(con,labor_payload,order=order,production={"id":production_id},
                                                      process_name="Packaged Feed Tint / Relabel / New Lid",actor=actor)
                        refresh_s_number_ready(con,row["target_s_number"],actor)
                        email_order_id = row["target_order_id"]; remaining_after = round(float(row["remaining"])-amount,2)
                        fulfilled = 0
                    else:
                        brand = str(payload.get("brand","")).strip(); color = str(payload.get("color","")).strip()
                        package_type = str(payload.get("package_type","")).strip()
                        item = con.execute("SELECT unit FROM catalog WHERE category=? AND color=?",(brand,color)).fetchone()
                        if not item: raise ValueError("Select the target category and color.")
                        package_size = package_size_for(package_type,item["unit"],payload.get("package_size")); amount = round(package_size*package_count,2)
                        open_need = sum(float(x["remaining"]) for x in stock_need_rows_sql(con)
                                        if x["brand"]==brand and x["color"]==color and x["unit"]==item["unit"])
                        if open_need <= .0001: raise ValueError("Add a no-S# need for this category and color first.")
                        if amount > open_need + .0001: raise ValueError(f"The no-S# need has only {open_need:.2f} {item['unit']} remaining; this feed entry is {amount:.2f}.")
                        cur = con.execute("INSERT INTO production(order_id,brand,color,unit,batch_number,gallons,produced_at) VALUES(NULL,?,?,?,?,?,?)",
                                          (brand,color,item["unit"],batch,amount,now)); production_id = cur.lastrowid
                        con.execute("""INSERT INTO stock_packaging(production_id,brand,color,package_type,package_size,package_count,gallons,packaged_at,created_by)
                          VALUES(?,?,?,?,?,?,?,?,?)""",(production_id,brand,color,package_type,package_size,package_count,amount,now,actor))
                        fulfilled = allocate_stock_need_sql(con,production_id,brand,color,item["unit"],amount)
                        production = {"id":production_id,"brand":brand,"color":color,"unit":item["unit"]}
                        note = f"Used supplied packaged feed lot {feed_lot}; tinted, relabeled, and fitted with new lids"
                        labor_payload = dict(payload); extra = str(payload.get("labor_notes","")).strip(); labor_payload["labor_notes"] = note + (" | "+extra if extra else "")
                        labor_cost = record_labor_sql(con,labor_payload,production=production,
                                                      process_name="Packaged Feed Tint / Relabel / New Lid",actor=actor)
                    con.commit()
                email_status = send_production_email(email_order_id,batch,amount,remaining_after) if email_order_id else None
                return self.send_json({"ok":True,"converted_amount":amount,"need_fulfilled":fulfilled,"remaining":remaining_after,
                                       "labor_cost":labor_cost,"email_status":email_status},201)
            if path == "/api/labor-rates":
                user = self.require_role("edit")
                if not user: return
                labor_name = str(payload.get("labor_name","")).strip(); hourly_rate = float(payload.get("hourly_rate",-1))
                effective_from = str(payload.get("effective_from","")).strip()
                if not labor_name or hourly_rate < 0 or not effective_from: raise ValueError("Enter labor name, hourly rate, and effective date.")
                actor = user.get("display_name") or user.get("email") or user.get("username","")
                with closing(connect()) as con:
                    con.execute("INSERT INTO labor_rates(labor_name,hourly_rate,effective_from,active,created_at,created_by) VALUES(?,?,?,1,?,?)",
                                (labor_name,round(hourly_rate,2),effective_from,datetime.now().isoformat(timespec="seconds"),actor)); con.commit()
                return self.send_json({"ok":True},201)
            if path == "/api/labor-entries":
                user = self.require_role("add")
                if not user: return
                order_id = int(payload.get("order_id",0)); rate_id = int(payload.get("rate_id",0)); hours = float(payload.get("hours",0))
                process_name = str(payload.get("process_name","")).strip(); worked_at = str(payload.get("worked_at","")).strip()
                notes = str(payload.get("notes","")).strip()
                if hours <= 0 or not process_name or not worked_at: raise ValueError("Select an S#, process, labor rate, date, and enter hours greater than zero.")
                actor = user.get("display_name") or user.get("email") or user.get("username","")
                with closing(connect()) as con:
                    order = con.execute("SELECT * FROM orders WHERE id=?",(order_id,)).fetchone(); rate = con.execute("SELECT * FROM labor_rates WHERE id=? AND active=1",(rate_id,)).fetchone()
                    if not order: raise ValueError("Order line was not found.")
                    if not rate: raise ValueError("Labor rate was not found or is inactive.")
                    cost = round(hours*float(rate["hourly_rate"]),2)
                    con.execute("""INSERT INTO labor_entries(order_id,s_number,brand,color,unit,process_name,labor_name,hours,hourly_rate,labor_cost,worked_at,notes,created_at,created_by)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",(order_id,order["s_number"],order["brand"],order["color"],order["unit"],process_name,rate["labor_name"],
                      hours,rate["hourly_rate"],cost,worked_at,notes,datetime.now().isoformat(timespec="seconds"),actor)); con.commit()
                return self.send_json({"ok":True,"labor_cost":cost},201)
            legacy_match = re.fullmatch(r"/api/bulk-production/(\d+)/incubate", path)
            if legacy_match:
                user = self.require_role("edit")
                if not user: return
                production_id = int(legacy_match.group(1))
                started, due = incubation_times(payload)
                with closing(connect()) as con:
                    lock_bulk_production_sql(con)
                    old = con.execute("SELECT * FROM production WHERE id=?", (production_id,)).fetchone()
                    if not old or old["incubation_started_at"]:
                        raise ValueError("Select an older batch that has not already entered QC.")
                    if USE_SUPABASE:
                        if old["order_id"] is not None: lock_order_sql(con, old["order_id"])
                        lock_production_sql(con, production_id)
                    if con.execute("SELECT 1 FROM stock_packaging WHERE production_id=?", (production_id,)).fetchone():
                        raise ValueError("An already packaged batch cannot be moved to incubation.")
                    if old["order_id"] is not None:
                        order = con.execute("""SELECT o.s_number,s.done_at,s.ready_at,s.shipped_at FROM orders o
                          LEFT JOIN s_number_status s ON s.s_number=o.s_number WHERE o.id=?""", (old["order_id"],)).fetchone()
                        if not order or any(order[k] for k in ("done_at","ready_at","shipped_at")) or con.execute(
                            "SELECT 1 FROM packaging WHERE order_id=?", (old["order_id"],)).fetchone():
                            raise ValueError("This order already has packaging or is closed; its batch cannot be moved.")
                        con.execute("UPDATE labor_entries SET order_id=NULL,s_number='STOCK' WHERE production_id=?", (production_id,))
                    release_stock_need_sql(con, production_id, float(old["gallons"]))
                    con.execute("""UPDATE production SET order_id=NULL,bulk_purpose='orders',quality_status='incubating',
                        incubation_started_at=?,incubation_due_at=? WHERE id=?""", (started,due,production_id))
                    con.commit()
                return self.send_json({"ok":True,"quality_status":"incubating","incubation_due_at":due})
            quality_match = re.fullmatch(r"/api/bulk-production/(\d+)/quality", path)
            if quality_match:
                user = self.require_role("edit")
                if not user: return
                production_id = int(quality_match.group(1))
                decision = str(payload.get("decision", "")).strip().lower()
                notes = str(payload.get("notes", "")).strip()
                override = payload.get("override_incubation") is True
                reason = str(payload.get("override_reason") or "").strip()
                edit_timer = payload.get("update_incubation") is True
                if decision not in ("incubating", "ready", "rework", "trash"):
                    raise ValueError("Select a valid QC status.")
                actor = user.get("display_name") or user.get("email") or user.get("username", "")
                with closing(connect()) as con:
                    lock_bulk_production_sql(con)
                    if USE_SUPABASE: lock_production_sql(con, production_id)
                    batch = con.execute("""SELECT * FROM production WHERE id=? AND order_id IS NULL
                      AND incubation_started_at<>''""", (production_id,)).fetchone()
                    if not batch: raise ValueError("Bulk production batch was not found.")
                    previous = batch["quality_status"]
                    started, due = batch["incubation_started_at"], batch["incubation_due_at"]
                    if payload.get("expected_status", previous) != previous or payload.get("expected_due_at", due) != due:
                        raise ValueError("Another person changed this batch's status or timer. Close and refresh before editing.")
                    if edit_timer and (previous != "incubating" or decision != "incubating"):
                        raise ValueError("To edit the timer, keep the status set to Incubating.")
                    if (decision != previous or edit_timer) and (float(batch["bulk_allocated"]) > 0 or con.execute(
                        "SELECT 1 FROM stock_packaging WHERE production_id=?", (production_id,)).fetchone() or con.execute(
                        "SELECT 1 FROM production WHERE rework_of_id=?", (production_id,)).fetchone()):
                        raise ValueError("Status is locked after packaging, allocation, or the start of a linked rework batch.")
                    early = False
                    if decision == "incubating" and previous != decision:
                        started, due = incubation_times(payload)
                    elif edit_timer:
                        original_start = incubation_datetime(started)
                        if not original_start: raise ValueError("This batch has no valid incubation start.")
                        _, due = incubation_times(payload, original_start)
                    elif decision != previous:
                        deadline = incubation_datetime(due)
                        if not deadline: raise ValueError("Set a valid incubation timer before recording a QC decision.")
                        early = bool(deadline and datetime.now(timezone.utc) < deadline)
                        if early and (not override or not reason):
                            raise ValueError("Incubation is not finished. Select Override incubation and enter a reason to change the status early.")
                        if early and len(reason) > 500:
                            raise ValueError("Keep the override reason to 500 characters or fewer.")
                    if decision != "ready" and previous == "ready" and batch["bulk_purpose"] == "stock":
                        release_stock_need_sql(con, production_id, float(batch["gallons"]))
                    elif decision == "ready" and previous != "ready" and batch["bulk_purpose"] == "stock":
                        allocate_stock_need_sql(con, production_id, batch["brand"],batch["color"],batch["unit"],batch["gallons"])
                    cost = record_labor_sql(con,payload,production=batch,process_name="Quality Control",actor=actor)
                    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    if early:
                        con.execute("""INSERT INTO production_qc_overrides
                          (production_id,previous_status,new_status,scheduled_due_at,reason,decided_by,decided_at)
                          VALUES(?,?,?,?,?,?,?)""", (production_id,previous,decision,due,reason,actor,now))
                    con.execute("""UPDATE production SET quality_status=?,quality_decided_at=?,quality_decided_by=?,quality_notes=?,
                        incubation_started_at=?,incubation_due_at=? WHERE id=?""",
                        (decision, now if decision != previous else batch["quality_decided_at"],
                         actor if decision != previous else batch["quality_decided_by"], notes, started, due, production_id))
                    con.commit()
                return self.send_json({"ok": True, "quality_status": decision, "incubation_due_at":due,
                                       "override_recorded":early, "labor_cost":cost}, 200)
            if not self.require_role("add"): return
            if path == "/api/stock-needs":
                brand=str(payload.get("brand","")).strip(); color=str(payload.get("color","")).strip()
                amount=float(payload.get("amount",payload.get("gallons",0))); note=str(payload.get("note","")).strip()
                if not brand or not color or amount<=0: raise ValueError("Select a category and color, then enter an amount greater than zero.")
                user=self.current_user(); actor=user.get("display_name") or user.get("email") or user.get("username","")
                with closing(connect()) as con:
                    item=con.execute("SELECT unit FROM catalog WHERE category=? AND color=?",(brand,color)).fetchone()
                    if not item: raise ValueError("Select a catalog category and color.")
                    cur=con.execute("INSERT INTO stock_needs(brand,color,unit,amount,note,created_at,created_by) VALUES(?,?,?,?,?,?,?)",
                      (brand,color,item["unit"],amount,note,datetime.now().isoformat(timespec="seconds"),actor)); con.commit()
                return self.send_json({"ok":True,"id":cur.lastrowid},201)
            if path == "/api/orders":
                s_number = normalize_order_reference(payload.get("s_number"))
                po_number = normalize_po_number(payload.get("po_number"))
                brand = str(payload.get("brand", "")).strip()
                color = str(payload.get("color", "")).strip()
                gallons = float(payload.get("gallons", 0)); ship_month = normalize_ship_month(payload.get("ship_month"))
                if not brand or not color or gallons <= 0: raise ValueError("Enter category, color, and an amount greater than zero.")
                with closing(connect()) as con:
                    if not s_number:
                        lock_order_sequence_sql(con)
                        s_number = next_no_s_reference_sql(con)
                    else:
                        lock_s_number_sql(con, s_number)
                    closed = con.execute("SELECT done_at,shipped_at FROM s_number_status WHERE s_number=?", (s_number,)).fetchone()
                    if closed and (closed["done_at"] or closed["shipped_at"]): raise ValueError("This order is already ready or shipped and cannot receive another line.")
                    item = con.execute("SELECT unit FROM catalog WHERE category=? AND color=?", (brand,color)).fetchone()
                    if not item: raise ValueError("Select a color from the chosen category.")
                    cur = con.execute("INSERT INTO orders(s_number,po_number,brand,color,unit,gallons,ship_month,created_at) VALUES(?,?,?,?,?,?,?,?)",
                                (s_number, po_number, brand, color, item["unit"], gallons, ship_month, datetime.now().isoformat(timespec="seconds")))
                    order_id = cur.lastrowid
                    recipient_ids = payload.get("recipient_ids", [])
                    if not isinstance(recipient_ids, list): raise ValueError("Email recipients are invalid.")
                    for contact_id in {int(x) for x in recipient_ids}:
                        active = con.execute("SELECT 1 FROM contacts WHERE id=? AND active=1", (contact_id,)).fetchone()
                        if active: con.execute("INSERT INTO order_recipients(order_id,contact_id) VALUES(?,?)", (order_id,contact_id))
                    con.commit()
                email_status = send_order_created_email(order_id)
                return self.send_json({"ok": True,"id":order_id,"s_number":s_number,"po_number":po_number,
                  "has_s_number":s_number.startswith("S"),"email_status":email_status}, 201)
            if path == "/api/contacts":
                name = str(payload.get("name", "")).strip(); email = str(payload.get("email", "")).strip().lower()
                if not name or not valid_email(email): raise ValueError("Enter a name and valid email address.")
                with closing(connect()) as con:
                    con.execute("INSERT INTO contacts(name,email,active) VALUES(?,?,1)", (name,email)); con.commit()
                return self.send_json({"ok": True}, 201)
            if path == "/api/catalog":
                category = str(payload.get("category", "")).strip()
                color = str(payload.get("color", "")).strip()
                unit = str(payload.get("unit", "")).strip().lower()
                if not category or not color or unit not in ("gallons", "qty"): raise ValueError("Enter category, color, and select Gallons or Qty.")
                with closing(connect()) as con:
                    existing = con.execute("SELECT DISTINCT unit FROM catalog WHERE category=?", (category,)).fetchone()
                    if existing and existing["unit"] != unit: raise ValueError(f"{category} is already measured in {existing['unit']}.")
                    con.execute("INSERT INTO catalog(category,color,unit) VALUES(?,?,?)", (category,color,unit)); con.commit()
                return self.send_json({"ok": True}, 201)
            if path == "/api/packaging-requirements":
                order_id = int(payload.get("order_id", 0)); package_type = str(payload.get("package_type", "")).strip()
                raw_count = float(payload.get("required_count", 0)); required_count = int(raw_count)
                if package_type not in PACKAGE_TYPES or raw_count != required_count or required_count <= 0:
                    raise ValueError("Select a package type and enter a whole-number requirement.")
                user = self.current_user(); actor = user.get("display_name") or user.get("email") or user.get("username", "")
                with closing(connect()) as con:
                    lock_order_sql(con, order_id)
                    order = con.execute("""SELECT o.*,s.shipped_at FROM orders o LEFT JOIN s_number_status s
                      ON s.s_number=o.s_number WHERE o.id=?""", (order_id,)).fetchone()
                    if not order: raise ValueError("Order line was not found.")
                    if order["shipped_at"]: raise ValueError("This S# has already shipped.")
                    package_size = package_size_for(package_type,order["unit"],payload.get("package_size"))
                    required_gallons = round(package_size*required_count,2)
                    planned = con.execute("SELECT COALESCE(SUM(required_gallons),0) FROM packaging_requirements WHERE order_id=?", (order_id,)).fetchone()[0]
                    if planned + required_gallons > order["gallons"] + .0001:
                        raise ValueError(f"Requirements would exceed the order by {planned+required_gallons-order['gallons']:.2f} {order['unit']}.")
                    con.execute("""INSERT INTO packaging_requirements(order_id,s_number,brand,color,package_type,package_size,required_count,required_gallons,created_at,created_by)
                      VALUES(?,?,?,?,?,?,?,?,?,?)""", (order_id,order["s_number"],order["brand"],order["color"],package_type,package_size,
                      required_count,required_gallons,datetime.now().isoformat(timespec="seconds"),actor))
                    refresh_s_number_ready(con,order["s_number"],actor); con.commit()
                return self.send_json({"ok": True}, 201)
            if path == "/api/bulk-production":
                brand = str(payload.get("brand", "")).strip(); color = str(payload.get("color", "")).strip()
                batch = str(payload.get("batch_number", "")).strip(); amount = float(payload.get("gallons", payload.get("amount", 0)))
                started_text, due_text = incubation_times(payload)
                rework_of_id = int(payload.get("rework_of_id", 0) or 0)
                purpose = str(payload.get("purpose", "orders")).strip().lower()
                if not brand or not color or not batch or not isfinite(amount) or amount <= 0:
                    raise ValueError("Select category and color, then enter a batch number and amount.")
                if purpose not in ("orders", "stock"):
                    raise ValueError("Choose active-order demand or future stock.")
                user = self.current_user(); actor = user.get("display_name") or user.get("email") or user.get("username", "")
                with closing(connect()) as con:
                    lock_bulk_production_sql(con)
                    item = con.execute("SELECT unit FROM catalog WHERE category=? AND color=?", (brand, color)).fetchone()
                    if not item: raise ValueError("Select a catalog category and color.")
                    if rework_of_id:
                        prior = con.execute("""SELECT * FROM production WHERE id=? AND order_id IS NULL
                          AND incubation_started_at<>'' AND quality_status='rework'""", (rework_of_id,)).fetchone()
                        if not prior: raise ValueError("The original batch is not waiting for rework.")
                        if con.execute("SELECT 1 FROM production WHERE rework_of_id=?", (rework_of_id,)).fetchone():
                            raise ValueError("A linked rework batch already exists.")
                        if prior["brand"] != brand or prior["color"] != color or prior["unit"] != item["unit"]:
                            raise ValueError("Rework must keep the original category, color, and unit.")
                        if amount > float(prior["gallons"]) + .0001:
                            raise ValueError("Rework amount cannot exceed the original batch amount.")
                        purpose = prior["bulk_purpose"] or purpose
                    demand = next((row for row in bulk_demand_rows_sql(con, purpose)
                                   if row["brand"] == brand and row["color"] == color and row["unit"] == item["unit"]), None)
                    available = float(demand["available_to_start"]) if demand else 0.0
                    if not rework_of_id and amount > available + .0001:
                        label = "pieces" if item["unit"] == "qty" else "gallons"
                        raise ValueError(f"Only {available:.2f} {label} remain in the combined category demand.")
                    cur = con.execute("""INSERT INTO production(order_id,brand,color,unit,batch_number,gallons,produced_at,
                      quality_status,incubation_started_at,incubation_due_at,bulk_purpose,rework_of_id)
                      VALUES(NULL,?,?,?,?,?,?,'incubating',?,?,?,?)""",
                      (brand,color,item["unit"],batch,amount,datetime.now().isoformat(timespec="seconds"),
                       started_text,due_text,purpose,rework_of_id or None))
                    production = {"id": cur.lastrowid, "brand": brand, "color": color, "unit": item["unit"]}
                    labor_cost = record_labor_sql(con,payload,production=production,
                        process_name="Batch Rework" if rework_of_id else "Bulk Mixing / Production",actor=actor)
                    con.commit()
                return self.send_json({"ok": True, "id": cur.lastrowid, "quality_status": "incubating",
                                       "incubation_due_at": due_text, "labor_cost": labor_cost}, 201)
            if path == "/api/stock-production":
                brand = str(payload.get("brand", "")).strip(); color = str(payload.get("color", "")).strip()
                batch = str(payload.get("batch_number", "")).strip(); gallons = float(payload.get("gallons", 0))
                if not brand or not color or not batch or gallons <= 0: raise ValueError("Enter category, color, batch number, and gallons.")
                with closing(connect()) as con:
                    item = con.execute("SELECT unit FROM catalog WHERE category=? AND color=?", (brand,color)).fetchone()
                    if not item: raise ValueError("Select a catalog category and color.")
                    now=datetime.now().isoformat(timespec="seconds"); cur=con.execute("INSERT INTO production(order_id,brand,color,unit,batch_number,gallons,produced_at) VALUES(NULL,?,?,?,?,?,?)",
                                (brand,color,item["unit"],batch,gallons,now)); production={"id":cur.lastrowid,"brand":brand,"color":color,"unit":item["unit"]}
                    need_fulfilled=allocate_stock_need_sql(con,cur.lastrowid,brand,color,item["unit"],gallons)
                    user=self.current_user(); labor_cost=record_labor_sql(con,payload,production=production,process_name="Mixing / Production",actor=user.get("display_name","System")); con.commit()
                return self.send_json({"ok": True,"labor_cost":labor_cost,"need_fulfilled":need_fulfilled}, 201)
            if path == "/api/packaging":
                source_type = str(payload.get("source_type", "order")).strip(); source_id = int(payload.get("source_id", payload.get("order_id", 0)))
                package_type = str(payload.get("package_type", "")).strip()
                raw_count = float(payload.get("package_count", 0)); package_count = int(raw_count)
                if package_type not in PACKAGE_TYPES: raise ValueError("Select a valid package type.")
                if raw_count != package_count or package_count <= 0: raise ValueError("Package count must be a whole number greater than zero.")
                label = PACKAGE_TYPES[package_type][0]
                user = self.current_user(); actor = user.get("display_name") or user.get("email") or user.get("username", "")
                with closing(connect()) as con:
                    if source_type == "stock":
                        lock_production_sql(con, source_id)
                        stock = con.execute("""SELECT p.*,p.gallons-p.bulk_allocated-COALESCE((SELECT SUM(k.gallons) FROM stock_packaging k WHERE k.production_id=p.id),0) available
                          FROM production p WHERE p.id=? AND p.order_id IS NULL""", (source_id,)).fetchone()
                        if not stock: raise ValueError("Stock production batch was not found.")
                        if stock["incubation_started_at"] and stock["quality_status"] != "ready":
                            raise ValueError("This bulk batch must be marked Ready for Packaging after incubation.")
                        package_size = package_size_for(package_type,stock["unit"],payload.get("package_size")); gallons = round(package_size * package_count, 2)
                        if gallons > stock["available"] + .0001:
                            raise ValueError(f"Only {stock['available']:.2f} stock {stock['unit']} remain to package; {package_count} {label}(s) require {gallons:.2f} {stock['unit']}.")
                        con.execute("""INSERT INTO stock_packaging(production_id,brand,color,package_type,package_size,package_count,gallons,packaged_at,created_by)
                          VALUES(?,?,?,?,?,?,?,?,?)""", (source_id,stock["brand"],stock["color"],package_type,package_size,package_count,gallons,
                          datetime.now().isoformat(timespec="seconds"),actor))
                        labor_cost=record_labor_sql(con,payload,production=stock,process_name="Filling / Packaging",actor=actor)
                    else:
                        lock_order_sql(con, source_id)
                        order = con.execute("""SELECT o.id,o.s_number,o.brand,o.color,o.unit,s.done_at,s.shipped_at,
                          COALESCE((SELECT SUM(p.gallons) FROM production p WHERE p.order_id=o.id),0)-
                          COALESCE((SELECT SUM(k.gallons) FROM packaging k WHERE k.order_id=o.id),0) available
                          FROM orders o LEFT JOIN s_number_status s ON s.s_number=o.s_number WHERE o.id=?""", (source_id,)).fetchone()
                        if not order: raise ValueError("Order line was not found.")
                        package_size = package_size_for(package_type,order["unit"],payload.get("package_size")); gallons = round(package_size * package_count, 2)
                        planned = con.execute("SELECT package_size FROM packaging_requirements WHERE order_id=? AND package_type=?", (source_id,package_type)).fetchone()
                        if planned and abs(float(planned["package_size"])-package_size) > .0001:
                            raise ValueError(f"This order requires {planned['package_size']:.0f} qty per case.")
                        if order["done_at"] or order["shipped_at"]: raise ValueError("This S# is already closed for packaging.")
                        if gallons > order["available"] + .0001:
                            raise ValueError(f"Only {order['available']:.2f} produced {order['unit']} remain to package; {package_count} {label}(s) require {gallons:.2f} {order['unit']}.")
                        con.execute("""INSERT INTO packaging(order_id,s_number,brand,color,package_type,package_size,package_count,gallons,packaged_at,created_by)
                          VALUES(?,?,?,?,?,?,?,?,?,?)""", (source_id,order["s_number"],order["brand"],order["color"],package_type,
                          package_size,package_count,gallons,datetime.now().isoformat(timespec="seconds"),actor))
                        labor_cost=record_labor_sql(con,payload,order=order,process_name="Filling / Packaging",actor=actor)
                        refresh_s_number_ready(con,order["s_number"],actor)
                    con.commit()
                return self.send_json({"ok": True, "gallons": gallons,"labor_cost":labor_cost}, 201)
            if path == "/api/production":
                oid = int(payload.get("order_id", 0)); batch = str(payload.get("batch_number", "")).strip()
                gallons = float(payload.get("gallons", 0))
                with closing(connect()) as con:
                    lock_order_sql(con, oid)
                    row = con.execute("""SELECT o.id,o.s_number,o.brand,o.color,o.unit,o.gallons-COALESCE(SUM(p.gallons),0) remaining,
                      s.done_at,s.shipped_at FROM orders o LEFT JOIN production p ON p.order_id=o.id
                      LEFT JOIN s_number_status s ON s.s_number=o.s_number WHERE o.id=?
                      GROUP BY o.id,s.done_at,s.shipped_at""", (oid,)).fetchone()
                    if not row: raise ValueError("Order was not found.")
                    if row["done_at"] or row["shipped_at"]: raise ValueError("This S# is already done or shipped.")
                    if not batch or gallons <= 0: raise ValueError("Enter a batch number and gallons greater than zero.")
                    unit_label = "pieces" if row["unit"] == "qty" else "gallons"
                    if gallons > row["remaining"] + .0001: raise ValueError(f"Only {row['remaining']:.2f} {unit_label} remain on this order. You cannot enter more than the order needs.")
                    now=datetime.now().isoformat(timespec="seconds"); cur=con.execute("INSERT INTO production(order_id,brand,color,unit,batch_number,gallons,produced_at) VALUES(?,?,?,?,?,?,?)",
                                (oid,row["brand"],row["color"],row["unit"],batch,gallons,now))
                    labor_cost=record_labor_sql(con,payload,order=row,production={"id":cur.lastrowid},process_name="Mixing / Production",actor=self.current_user().get("display_name","System"))
                    refresh_s_number_ready(con,con.execute("SELECT s_number FROM orders WHERE id=?", (oid,)).fetchone()[0],self.current_user().get("display_name","System"))
                    con.commit(); remaining_after = float(row["remaining"]) - gallons
                email_status = send_production_email(oid, batch, gallons, remaining_after)
                return self.send_json({"ok": True, "remaining":round(remaining_after,2),
                                       "email_status": email_status,"labor_cost":labor_cost}, 201)
            if path == "/api/existing-production":
                raw_order_id = payload.get("order_id")
                order_id = int(raw_order_id) if raw_order_id not in (None, "") else None
                brand = str(payload.get("brand", "")).strip(); color = str(payload.get("color", "")).strip()
                batch = str(payload.get("batch_number", "")).strip(); gallons = float(payload.get("gallons", 0))
                produced_at = str(payload.get("produced_at", "")).strip()
                if not batch or gallons <= 0 or not produced_at: raise ValueError("Enter batch number, gallons, and production date.")
                with closing(connect()) as con:
                    if order_id is not None:
                        lock_order_sql(con, order_id)
                        order = con.execute("""SELECT o.id,o.s_number,o.brand,o.color,o.unit,o.gallons-COALESCE(SUM(p.gallons),0) remaining,
                          s.done_at,s.shipped_at FROM orders o LEFT JOIN production p ON p.order_id=o.id
                          LEFT JOIN s_number_status s ON s.s_number=o.s_number WHERE o.id=?
                          GROUP BY o.id,s.done_at,s.shipped_at""", (order_id,)).fetchone()
                        if not order: raise ValueError("Selected order was not found.")
                        if order["done_at"] or order["shipped_at"]: raise ValueError("This S# is already done or shipped.")
                        if gallons > order["remaining"] + .0001: raise ValueError(f"Only {order['remaining']:.2f} gallons remain on this order.")
                        brand, color, unit = order["brand"], order["color"], order["unit"]
                    elif not brand or not color:
                        raise ValueError("Enter brand and color for on-hand production.")
                    else:
                        item = con.execute("SELECT unit FROM catalog WHERE category=? AND color=?", (brand,color)).fetchone()
                        if not item: raise ValueError("Select a catalog category and color.")
                        unit = item["unit"]
                    cur=con.execute("INSERT INTO production(order_id,brand,color,unit,batch_number,gallons,produced_at) VALUES(?,?,?,?,?,?,?)",
                                (order_id,brand,color,unit,batch,gallons,produced_at))
                    production={"id":cur.lastrowid,"brand":brand,"color":color,"unit":unit}
                    labor_cost=record_labor_sql(con,payload,order=order if order_id is not None else None,production=production,
                      process_name="Mixing / Production",actor=self.current_user().get("display_name","System"))
                    need_fulfilled=allocate_stock_need_sql(con,cur.lastrowid,brand,color,unit,gallons) if order_id is None else 0
                    if order_id is not None:
                        refresh_s_number_ready(con,con.execute("SELECT s_number FROM orders WHERE id=?", (order_id,)).fetchone()[0],self.current_user().get("display_name","System"))
                    con.commit()
                    remaining_after = float(order["remaining"]) - gallons if order_id is not None else None
                email_status = send_production_email(order_id, batch, gallons, remaining_after) if order_id is not None else None
                return self.send_json({"ok": True, "email_status": email_status,"labor_cost":labor_cost,"need_fulfilled":need_fulfilled}, 201)
            return self.send_error(404)
        except DatabaseIntegrityError:
            if path == "/api/contacts": message = "That email recipient already exists."
            elif path == "/api/users": message = "That login email already exists."
            else: message = "That batch number or category/color entry already exists."
            self.send_json({"error": message}, 400)
        except (ValueError, TypeError, json.JSONDecodeError) as e:
            self.send_json({"error": str(e)}, 400)

    def do_PUT(self):
        try:
            return self._do_PUT()
        except DatabaseOperationalError:
            return self.send_json({"error": "The database is busy. Please try again in a moment."}, 503)
        except Exception as exc:
            return self._internal_error(exc)

    def _do_PUT(self):
        try:
            payload = self.body()
            if not self.require_role("edit"): return
            parts = urlparse(self.path).path.strip("/").split("/")
            if len(parts) != 4 or parts[:2] != ["api", "admin"]: return self.send_error(404)
            kind, record_id = parts[2], int(parts[3])
            if kind == "stock-needs":
                brand=str(payload.get("brand","")).strip(); color=str(payload.get("color","")).strip()
                amount=float(payload.get("amount",0)); note=str(payload.get("note","")).strip()
                if not brand or not color or amount<=0: raise ValueError("Select a category and color, then enter an amount greater than zero.")
                with closing(connect()) as con:
                    old=con.execute("SELECT * FROM stock_needs WHERE id=?",(record_id,)).fetchone()
                    if not old: raise ValueError("Production need was not found.")
                    fulfilled=float(con.execute("SELECT COALESCE(SUM(amount),0) FROM stock_need_fulfillments WHERE need_id=?",(record_id,)).fetchone()[0])
                    if amount+.0001<fulfilled: raise ValueError(f"Amount cannot be below {fulfilled:.2f} already produced.")
                    item=con.execute("SELECT unit FROM catalog WHERE category=? AND color=?",(brand,color)).fetchone()
                    if not item: raise ValueError("Select a catalog category and color.")
                    if fulfilled>.0001 and (brand!=old["brand"] or color!=old["color"] or item["unit"]!=old["unit"]):
                        raise ValueError("Category or color cannot change after production has fulfilled part of this need.")
                    con.execute("UPDATE stock_needs SET brand=?,color=?,unit=?,amount=?,note=? WHERE id=?",(brand,color,item["unit"],amount,note,record_id)); con.commit()
                return self.send_json({"ok":True})
            if kind == "labor-entries":
                rate_id=int(payload.get("rate_id",0)); hours=float(payload.get("hours",0))
                process_name=str(payload.get("process_name","")).strip(); worked_at=str(payload.get("worked_at","")).strip(); notes=str(payload.get("notes","")).strip()
                if hours<=0 or not process_name or not worked_at: raise ValueError("Complete the labor entry and enter hours greater than zero.")
                with closing(connect()) as con:
                    entry=con.execute("SELECT * FROM labor_entries WHERE id=?",(record_id,)).fetchone(); rate=con.execute("SELECT * FROM labor_rates WHERE id=? AND active=1",(rate_id,)).fetchone()
                    if not entry or not rate: raise ValueError("The labor entry or selected labor rate was not found.")
                    cost=round(hours*float(rate["hourly_rate"]),2)
                    if entry["order_id"] is None:
                        cur=con.execute("""UPDATE labor_entries SET process_name=?,labor_name=?,hours=?,hourly_rate=?,labor_cost=?,worked_at=?,notes=? WHERE id=?""",
                          (process_name,rate["labor_name"],hours,rate["hourly_rate"],cost,worked_at,notes,record_id))
                    else:
                        order_id=int(payload.get("order_id",0)); order=con.execute("SELECT * FROM orders WHERE id=?",(order_id,)).fetchone()
                        if not order: raise ValueError("The selected order was not found.")
                        cur=con.execute("""UPDATE labor_entries SET order_id=?,s_number=?,brand=?,color=?,unit=?,process_name=?,labor_name=?,hours=?,hourly_rate=?,labor_cost=?,worked_at=?,notes=? WHERE id=?""",
                          (order_id,order["s_number"],order["brand"],order["color"],order["unit"],process_name,rate["labor_name"],hours,rate["hourly_rate"],cost,worked_at,notes,record_id))
                    if not cur.rowcount: raise ValueError("Labor entry was not found.")
                    con.commit()
                return self.send_json({"ok":True,"labor_cost":cost})
            if kind == "contacts":
                name = str(payload.get("name", "")).strip()
                email = str(payload.get("email", "")).strip().lower()
                active_value = payload.get("active", True)
                active = 1 if active_value in (True, 1, "1", "true", "on") else 0
                if not name or not valid_email(email):
                    raise ValueError("Enter a recipient name and valid email address.")
                with closing(connect()) as con:
                    cur = con.execute(
                        "UPDATE contacts SET name=?,email=?,active=? WHERE id=?",
                        (name, email, active, record_id),
                    )
                    if not cur.rowcount:
                        raise ValueError("Email recipient was not found.")
                    con.commit()
                return self.send_json({"ok": True})
            if kind == "users":
                if not self.require_role("admin"): return
                display_name = str(payload.get("display_name", "")).strip(); email = str(payload.get("email", "")).strip().lower()
                role = str(payload.get("role", "")).strip()
                password = str(payload.get("password", "")); active = 1 if payload.get("active", True) else 0
                if not display_name or not valid_email(email) or role not in ("admin","manager","team"): raise ValueError("Enter display name, a valid email, and a valid role.")
                with closing(connect()) as con:
                    if active == 0 and record_id == self.current_user()["id"]: raise ValueError("You cannot disable your own account.")
                    if record_id == self.current_user()["id"] and role != self.current_user()["role"]:
                        raise ValueError("You cannot change your own role. Ask another administrator.")
                    values = {"display_name": display_name, "role": role, "active": active}
                    if password:
                        if len(password) < 8: raise ValueError("Password must have at least 8 characters.")
                        cur = con.execute("UPDATE users SET username=?,email=?,display_name=?,role=?,active=?,password_hash=? WHERE id=?",
                                          (email,email,display_name,role,active,hash_password(password),record_id))
                    else:
                        cur = con.execute("UPDATE users SET username=?,email=?,display_name=?,role=?,active=? WHERE id=?", (email,email,display_name,role,active,record_id))
                    if not cur.rowcount: raise ValueError("User was not found.")
                    con.commit()
                invalidate_session_user(record_id)
                return self.send_json({"ok": True})
            if kind == "packaging":
                package_type = str(payload.get("package_type", "")).strip(); raw_count = float(payload.get("package_count", 0))
                package_count = int(raw_count); packaged_at = str(payload.get("packaged_at", "")).strip()
                if package_type not in PACKAGE_TYPES or raw_count != package_count or package_count <= 0 or not packaged_at:
                    raise ValueError("Select a package type, enter a whole-number count, and provide the packaged date.")
                with closing(connect()) as con:
                    lock_packaging_sql(con, record_id)
                    item = con.execute("""SELECT k.order_id,k.s_number,k.package_type,k.package_size,o.unit,s.shipped_at,
                      COALESCE((SELECT SUM(h.package_count) FROM shipments h WHERE h.packaging_id=k.id),0) shipped_count,
                      COALESCE((SELECT SUM(p.gallons) FROM production p WHERE p.order_id=k.order_id),0)-
                      COALESCE((SELECT SUM(x.gallons) FROM packaging x WHERE x.order_id=k.order_id AND x.id<>k.id),0) available
                      FROM packaging k JOIN orders o ON o.id=k.order_id LEFT JOIN s_number_status s ON s.s_number=k.s_number WHERE k.id=?""", (record_id,)).fetchone()
                    if not item: raise ValueError("Packaging record was not found.")
                    size_input = payload.get("package_size",item["package_size"] if package_type==item["package_type"] else None)
                    package_size = package_size_for(package_type,item["unit"],size_input); gallons = round(package_size * package_count, 2)
                    if package_count < item["shipped_count"]: raise ValueError(f"This record already has {item['shipped_count']} shipped package(s).")
                    if gallons > item["available"] + .0001: raise ValueError(f"Only {item['available']:.2f} produced gallons are available for this record.")
                    con.execute("UPDATE packaging SET package_type=?,package_size=?,package_count=?,gallons=?,packaged_at=? WHERE id=?",
                                (package_type,package_size,package_count,gallons,packaged_at,record_id))
                    refresh_s_number_ready(con,item["s_number"],self.current_user().get("display_name","System")); con.commit()
                return self.send_json({"ok": True})
            if kind == "shipments":
                raw_count = float(payload.get("package_count", 0)); package_count = int(raw_count)
                if raw_count != package_count or package_count <= 0: raise ValueError("Shipped quantity must be a whole number greater than zero.")
                with closing(connect()) as con:
                    shipment_ref = con.execute("SELECT packaging_id FROM shipments WHERE id=?", (record_id,)).fetchone()
                    if not shipment_ref: raise ValueError("Shipment record was not found.")
                    lock_packaging_sql(con, shipment_ref["packaging_id"])
                    item = con.execute("""SELECT h.packaging_id,h.s_number,k.package_count,k.package_size,
                      COALESCE((SELECT SUM(x.package_count) FROM shipments x WHERE x.packaging_id=h.packaging_id AND x.id<>h.id),0) other_shipped
                      FROM shipments h JOIN packaging k ON k.id=h.packaging_id WHERE h.id=?""", (record_id,)).fetchone()
                    if not item: raise ValueError("Shipment record was not found.")
                    if item["other_shipped"] + package_count > item["package_count"]:
                        raise ValueError(f"Only {item['package_count']-item['other_shipped']} package(s) are available for this shipment.")
                    con.execute("UPDATE shipments SET package_count=?,gallons=? WHERE id=?", (package_count,round(package_count*item["package_size"],2),record_id))
                    refresh_s_number_shipped(con,item["s_number"]); con.commit()
                return self.send_json({"ok": True})
            if kind == "requirements":
                raw_count = float(payload.get("required_count", 0)); required_count = int(raw_count)
                if raw_count != required_count or required_count <= 0: raise ValueError("Required quantity must be a whole number greater than zero.")
                with closing(connect()) as con:
                    requirement_ref = con.execute("SELECT order_id FROM packaging_requirements WHERE id=?", (record_id,)).fetchone()
                    if not requirement_ref: raise ValueError("Packaging requirement was not found.")
                    lock_order_sql(con, requirement_ref["order_id"])
                    item = con.execute("""SELECT r.*,o.gallons order_gallons FROM packaging_requirements r
                      JOIN orders o ON o.id=r.order_id WHERE r.id=?""", (record_id,)).fetchone()
                    if not item: raise ValueError("Packaging requirement was not found.")
                    other = con.execute("SELECT COALESCE(SUM(required_gallons),0) FROM packaging_requirements WHERE order_id=? AND id<>?",
                                        (item["order_id"],record_id)).fetchone()[0]
                    required_gallons = round(item["package_size"]*required_count,2)
                    if other + required_gallons > item["order_gallons"] + .0001: raise ValueError("Requirements exceed the order gallons.")
                    con.execute("UPDATE packaging_requirements SET required_count=?,required_gallons=? WHERE id=?", (required_count,required_gallons,record_id))
                    refresh_s_number_ready(con,item["s_number"],self.current_user().get("display_name","System")); con.commit()
                return self.send_json({"ok": True})
            if kind == "orders":
                brand = str(payload.get("brand", "")).strip(); color = str(payload.get("color", "")).strip()
                po_number = normalize_po_number(payload.get("po_number"))
                gallons = float(payload.get("gallons", 0)); created_at = str(payload.get("created_at", "")).strip(); ship_month = normalize_ship_month(payload.get("ship_month"))
                if not brand or not color or gallons <= 0 or not created_at: raise ValueError("Complete every order field.")
                with closing(connect()) as con:
                    lock_order_sql(con, record_id)
                    old = con.execute("""SELECT o.*,s.shipped_at FROM orders o LEFT JOIN s_number_status s
                      ON s.s_number=o.s_number WHERE o.id=?""", (record_id,)).fetchone()
                    if not old: raise ValueError("Order was not found.")
                    if old["shipped_at"]: raise ValueError("A shipped order cannot be changed.")
                    s_number = normalize_order_reference(payload.get("s_number"),old["s_number"]) or old["s_number"]
                    catalog_item = con.execute("SELECT unit FROM catalog WHERE category=? AND color=?", (brand,color)).fetchone()
                    if not catalog_item: raise ValueError("Select a catalog category and color.")
                    unit = catalog_item["unit"]
                    row = con.execute("SELECT COALESCE(SUM(gallons),0) produced FROM production WHERE order_id=?", (record_id,)).fetchone()
                    if gallons + .0001 < row["produced"]: raise ValueError(f"Order cannot be below {row['produced']:.2f} gallons already produced.")
                    cur = con.execute("UPDATE orders SET s_number=?,po_number=?,brand=?,color=?,unit=?,gallons=?,ship_month=?,created_at=? WHERE id=?", (s_number,po_number,brand,color,unit,gallons,ship_month,created_at,record_id))
                    if not cur.rowcount: raise ValueError("Order was not found.")
                    con.execute("UPDATE production SET brand=?,color=?,unit=? WHERE order_id=?", (brand,color,unit,record_id))
                    con.execute("UPDATE packaging SET s_number=?,brand=?,color=? WHERE order_id=?", (s_number,brand,color,record_id))
                    con.execute("UPDATE packaging_requirements SET s_number=?,brand=?,color=? WHERE order_id=?", (s_number,brand,color,record_id))
                    con.execute("UPDATE labor_entries SET s_number=?,brand=?,color=?,unit=? WHERE order_id=?", (s_number,brand,color,unit,record_id))
                    actor = self.current_user().get("display_name","System")
                    refresh_s_number_ready(con,old["s_number"],actor)
                    if s_number != old["s_number"]: refresh_s_number_ready(con,s_number,actor)
                    con.commit()
                return self.send_json({"ok": True})
            if kind == "batches":
                raw_order_id = payload.get("order_id")
                order_id = int(raw_order_id) if raw_order_id not in (None, "") else None
                brand = str(payload.get("brand", "")).strip(); color = str(payload.get("color", "")).strip()
                batch = str(payload.get("batch_number", "")).strip()
                gallons = float(payload.get("gallons", 0)); produced_at = str(payload.get("produced_at", "")).strip()
                if not batch or gallons <= 0 or not produced_at: raise ValueError("Complete every batch field.")
                with closing(connect()) as con:
                    lock_production_sql(con, record_id)
                    old=con.execute("SELECT * FROM production WHERE id=?",(record_id,)).fetchone()
                    if not old: raise ValueError("Batch was not found.")
                    if old["incubation_started_at"]:
                        raise ValueError("Manage QC batches on the Production page to preserve inventory and costing history.")
                    allocated=float(con.execute("SELECT COALESCE(SUM(amount),0) FROM stock_need_fulfillments WHERE production_id=?",(record_id,)).fetchone()[0])
                    stock_packaged=float(con.execute("SELECT COALESCE(SUM(gallons),0) FROM stock_packaging WHERE production_id=?",(record_id,)).fetchone()[0])
                    if gallons+.0001<stock_packaged: raise ValueError(f"Batch amount cannot be below {stock_packaged:.2f} already packaged.")
                    if order_id is not None:
                        if allocated>.0001: raise ValueError("This stock batch fulfilled a no-S# production need and cannot be moved directly to an S# batch. Assign its packaged inventory instead.")
                        if stock_packaged>.0001: raise ValueError("This stock batch is packaged. Assign its packaged inventory to an S# instead.")
                        order = con.execute("SELECT brand,color,unit,gallons FROM orders WHERE id=?", (order_id,)).fetchone()
                        if not order: raise ValueError("Selected order was not found.")
                        other = con.execute("SELECT COALESCE(SUM(gallons),0) total FROM production WHERE order_id=? AND id<>?", (order_id,record_id)).fetchone()["total"]
                        if other + gallons > order["gallons"] + .0001: raise ValueError(f"This would exceed the order by {other + gallons - order['gallons']:.2f} gallons.")
                        brand, color, unit = order["brand"], order["color"], order["unit"]
                    elif not brand or not color:
                        raise ValueError("Enter brand and color for on-hand production.")
                    else:
                        item = con.execute("SELECT unit FROM catalog WHERE category=? AND color=?", (brand,color)).fetchone()
                        if not item: raise ValueError("Select a catalog category and color.")
                        unit = item["unit"]
                        if allocated>.0001 and (brand!=old["brand"] or color!=old["color"] or unit!=old["unit"]):
                            raise ValueError("Category or color cannot change because this batch fulfilled a no-S# production need.")
                        if stock_packaged>.0001 and (brand!=old["brand"] or color!=old["color"] or unit!=old["unit"]):
                            raise ValueError("Category or color cannot change after this stock batch is packaged.")
                        if gallons+.0001<allocated: release_stock_need_sql(con,record_id,allocated-gallons)
                    cur = con.execute("""UPDATE production SET order_id=?,brand=?,color=?,unit=?,batch_number=?,gallons=?,produced_at=? WHERE id=?""",
                                      (order_id,brand,color,unit,batch,gallons,produced_at,record_id))
                    if not cur.rowcount: raise ValueError("Batch was not found.")
                    if order_id is None:
                        extra=gallons-float(old["gallons"]) if old["order_id"] is None else gallons
                        if extra>.0001: allocate_stock_need_sql(con,record_id,brand,color,unit,extra)
                    con.commit()
                return self.send_json({"ok": True})
            if kind == "catalog":
                unit = str(payload.get("unit", "")).strip().lower()
                if unit not in ("gallons", "qty"): raise ValueError("Select Gallons or Qty.")
                with closing(connect()) as con:
                    item = con.execute("SELECT category FROM catalog WHERE id=?", (record_id,)).fetchone()
                    if not item: raise ValueError("Catalog item was not found.")
                    category = item["category"]
                    con.execute("UPDATE catalog SET unit=? WHERE category=?", (unit,category))
                    con.execute("UPDATE orders SET unit=? WHERE brand=?", (unit,category))
                    con.execute("UPDATE production SET unit=? WHERE brand=?", (unit,category))
                    con.execute("UPDATE labor_entries SET unit=? WHERE brand=?", (unit,category))
                    con.execute("UPDATE stock_needs SET unit=? WHERE brand=?", (unit,category)); con.commit()
                return self.send_json({"ok": True})
            return self.send_error(404)
        except DatabaseIntegrityError:
            message = (
                "That login email already exists." if kind == "users"
                else "That recipient email already exists." if kind == "contacts"
                else "That batch number already exists."
            )
            self.send_json({"error": message}, 400)
        except (ValueError, TypeError, json.JSONDecodeError) as e:
            self.send_json({"error": str(e)}, 400)

    def do_DELETE(self):
        try:
            return self._do_DELETE()
        except DatabaseIntegrityError:
            return self.send_json({"error": "This record is connected to other data. Delete the related records first."}, 400)
        except DatabaseOperationalError:
            return self.send_json({"error": "The database is busy. Please try again in a moment."}, 503)
        except Exception as exc:
            return self._internal_error(exc)

    def _do_DELETE(self):
        if not self.require_role("delete"): return
        try:
            parts = urlparse(self.path).path.strip("/").split("/")
            if len(parts) != 4 or parts[:2] != ["api", "admin"]: return self.send_error(404)
            kind, record_id = parts[2], int(parts[3])
            with closing(connect()) as con:
                if kind == "batches":
                    lock_production_sql(con, record_id)
                    item = con.execute("""SELECT p.id,p.order_id,p.gallons,p.incubation_started_at,o.s_number
                      FROM production p LEFT JOIN orders o ON o.id=p.order_id WHERE p.id=?""", (record_id,)).fetchone()
                    if not item: raise ValueError("Batch was not found.")
                    if item["incubation_started_at"]:
                        raise ValueError("Keep the QC history and cost: mark an unused batch Trash on the Production page instead.")
                    if item["order_id"] is None:
                        con.execute("DELETE FROM stock_packaging WHERE production_id=?", (record_id,))
                    else:
                        remaining_produced = float(con.execute(
                            "SELECT COALESCE(SUM(gallons),0) FROM production WHERE order_id=? AND id<>?",
                            (item["order_id"], record_id),
                        ).fetchone()[0])
                        packaged = float(con.execute(
                            "SELECT COALESCE(SUM(gallons),0) FROM packaging WHERE order_id=?",
                            (item["order_id"],),
                        ).fetchone()[0])
                        if packaged > remaining_produced + .0001:
                            raise ValueError("Delete or reduce this order's packaging before deleting this batch.")
                    con.execute("DELETE FROM labor_entries WHERE production_id=?", (record_id,))
                    cur = con.execute("DELETE FROM production WHERE id=?", (record_id,))
                    if not cur.rowcount: raise ValueError("Batch was not found.")
                    if item["order_id"] is not None:
                        refresh_s_number_ready(con,item["s_number"],self.current_user().get("display_name","System"))
                elif kind == "packaging":
                    lock_packaging_sql(con, record_id)
                    item = con.execute("""SELECT k.s_number,s.shipped_at FROM packaging k LEFT JOIN s_number_status s
                      ON s.s_number=k.s_number WHERE k.id=?""", (record_id,)).fetchone()
                    if not item: raise ValueError("Packaging record was not found.")
                    if con.execute("SELECT 1 FROM shipments WHERE packaging_id=?", (record_id,)).fetchone():
                        raise ValueError("Undo this record's shipments before deleting its package inventory.")
                    con.execute("DELETE FROM packaging WHERE id=?", (record_id,))
                    refresh_s_number_ready(con,item["s_number"],self.current_user().get("display_name","System"))
                elif kind == "requirements":
                    requirement_ref = con.execute("SELECT order_id FROM packaging_requirements WHERE id=?", (record_id,)).fetchone()
                    if not requirement_ref: raise ValueError("Packaging requirement was not found.")
                    lock_order_sql(con, requirement_ref["order_id"])
                    item = con.execute("SELECT s_number FROM packaging_requirements WHERE id=?", (record_id,)).fetchone()
                    if not item: raise ValueError("Packaging requirement was not found.")
                    con.execute("DELETE FROM packaging_requirements WHERE id=?", (record_id,))
                    refresh_s_number_ready(con,item["s_number"],self.current_user().get("display_name","System"))
                elif kind == "labor-entries":
                    cur=con.execute("DELETE FROM labor_entries WHERE id=?",(record_id,))
                    if not cur.rowcount: raise ValueError("Labor entry was not found.")
                elif kind == "labor-rates":
                    cur=con.execute("DELETE FROM labor_rates WHERE id=?",(record_id,))
                    if not cur.rowcount: raise ValueError("Labor rate was not found.")
                elif kind == "stock-needs":
                    fulfilled=float(con.execute("SELECT COALESCE(SUM(amount),0) FROM stock_need_fulfillments WHERE need_id=?",(record_id,)).fetchone()[0])
                    if fulfilled>.0001: raise ValueError("A production need with completed production cannot be deleted. Edit its total amount instead.")
                    cur=con.execute("DELETE FROM stock_needs WHERE id=?",(record_id,))
                    if not cur.rowcount: raise ValueError("Production need was not found.")
                elif kind == "daily-targets":
                    cur=con.execute("DELETE FROM daily_production_targets WHERE id=?",(record_id,))
                    if not cur.rowcount: raise ValueError("Daily production target was not found.")
                elif kind == "orders":
                    lock_order_sql(con, record_id)
                    count = con.execute("SELECT COUNT(*) FROM production WHERE order_id=?", (record_id,)).fetchone()[0]
                    if count: raise ValueError("Delete this order's batches first.")
                    if con.execute("SELECT 1 FROM labor_entries WHERE order_id=?",(record_id,)).fetchone(): raise ValueError("Delete this order's labor entries first.")
                    cur = con.execute("DELETE FROM orders WHERE id=?", (record_id,))
                    if not cur.rowcount: raise ValueError("Order was not found.")
                elif kind == "catalog":
                    item = con.execute("SELECT category,color FROM catalog WHERE id=?", (record_id,)).fetchone()
                    if not item: raise ValueError("Catalog item was not found.")
                    used = con.execute("SELECT (SELECT COUNT(*) FROM orders WHERE brand=? AND color=?)+(SELECT COUNT(*) FROM stock_needs WHERE brand=? AND color=?)",
                      (item["category"],item["color"],item["category"],item["color"])).fetchone()[0]
                    if used: raise ValueError("This category/color is used by an order and cannot be deleted.")
                    con.execute("DELETE FROM catalog WHERE id=?", (record_id,))
                elif kind == "contacts":
                    con.execute("DELETE FROM order_recipients WHERE contact_id=?", (record_id,))
                    cur = con.execute("DELETE FROM contacts WHERE id=?", (record_id,))
                    if not cur.rowcount: raise ValueError("Email contact was not found.")
                elif kind == "users":
                    if record_id == self.current_user()["id"]: raise ValueError("You cannot delete your own account.")
                    cur = con.execute("DELETE FROM users WHERE id=?", (record_id,))
                    if not cur.rowcount: raise ValueError("User was not found.")
                    invalidate_session_user(record_id)
                else: return self.send_error(404)
                con.commit()
            return self.send_json({"ok": True})
        except (ValueError, TypeError) as e:
            self.send_json({"error": str(e)}, 400)

    def log_message(self, fmt, *args):
        pass


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(self, server_address, handler_class):
        self.request_slots = threading.BoundedSemaphore(
            bounded_env_number("MAX_REQUEST_THREADS", 32, 4, 64)
        )
        super().__init__(server_address, handler_class)

    def process_request(self, request, client_address):
        self.request_slots.acquire()
        try:
            super().process_request(request, client_address)
        except Exception:
            self.request_slots.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.request_slots.release()

    def handle_error(self, request, client_address):
        print(f"Request worker recovered after an error from {client_address[0]}.", file=sys.stderr, flush=True)
        traceback.print_exc(file=sys.stderr)


def initialize_with_retry():
    """Wait through short Supabase wakeups/outages before letting Render restart us."""
    attempts = bounded_env_number("STARTUP_DB_RETRIES", 6, 1, 12)
    delay = 1.0
    for attempt in range(1, attempts + 1):
        try:
            initialize()
            return
        except DatabaseOperationalError as exc:
            if attempt >= attempts:
                raise
            print(
                f"Database startup attempt {attempt}/{attempts} failed; retrying in {delay:.0f}s: {exc}",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(delay)
            delay = min(delay * 2, 15)


def main():
    initialize_with_retry()
    port = int(os.environ.get("PORT", "8765"))
    is_render = os.environ.get("RENDER", "").lower() == "true"
    if is_render and not USE_SUPABASE:
        raise RuntimeError("SUPABASE_DATABASE_URL must be configured on Render.")
    if is_render and (not APP_PASSWORD or APP_PASSWORD == "Ollies123!"):
        raise RuntimeError("Set a private APP_PASSWORD on Render.")
    local_url = f"http://127.0.0.1:{port}"
    print("\nOllies Production Dashboard is running.")
    print("Database: " + ("Supabase PostgreSQL" if USE_SUPABASE else "local SQLite"))
    if is_render:
        print(f"Render web service is listening on port {port}.")
    else:
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect(("8.8.8.8", 80))
            lan_ip = probe.getsockname()[0]
            probe.close()
        except OSError:
            lan_ip = socket.gethostbyname(socket.gethostname())
        print(f"This computer: {local_url}")
        print(f"Other devices: http://{lan_ip}:{port}")
        print(f"TV dashboard: http://{lan_ip}:{port}/tv")
        threading.Timer(0.8, lambda: webbrowser.open(local_url)).start()
    print("Keep this process running. Press Ctrl+C to stop.\n")
    BoundedThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
