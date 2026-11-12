import csv
import base64
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
import threading
import time
import webbrowser
from contextlib import closing
from datetime import date, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from email.message import EmailMessage
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

try:
    import psycopg
    from psycopg.errors import IntegrityError as PsycopgIntegrityError
except ImportError:
    psycopg = None

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
SESSIONS = {}
DatabaseIntegrityError = (sqlite3.IntegrityError, PsycopgIntegrityError)
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
    token = secrets.token_urlsafe(32)
    SESSIONS[token] = {"expires": time.time() + 12 * 60 * 60, "id": user["id"],
                       "username": user["username"], "email": user["email"] if "email" in user.keys() else "",
                       "display_name": user["display_name"], "role": user["role"]}
    return token


def valid_session(token):
    session = SESSIONS.get(token)
    if session and session["expires"] > time.time(): return True
    SESSIONS.pop(token, None); return False


def session_data(token):
    return SESSIONS.get(token) if valid_session(token) else None


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


class PostgresConnection:
    def __init__(self, database_url):
        if psycopg is None:
            raise RuntimeError("PostgreSQL support is not installed. Run: python -m pip install -r requirements.txt")
        self._connection = psycopg.connect(database_url, connect_timeout=10, prepare_threshold=None)

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
        self._connection.close()


def connect():
    if USE_SUPABASE:
        return PostgresConnection(SUPABASE_DATABASE_URL)
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys=ON")
    return con


SUPABASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
  id BIGSERIAL PRIMARY KEY,
  s_number TEXT NOT NULL DEFAULT '',
  brand TEXT NOT NULL,
  color TEXT NOT NULL,
  unit TEXT NOT NULL DEFAULT 'gallons',
  gallons NUMERIC NOT NULL CHECK(gallons > 0),
  ship_month TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS production (
  id BIGSERIAL PRIMARY KEY,
  order_id BIGINT REFERENCES orders(id),
  brand TEXT NOT NULL,
  color TEXT NOT NULL,
  unit TEXT NOT NULL DEFAULT 'gallons',
  batch_number TEXT NOT NULL UNIQUE,
  gallons NUMERIC NOT NULL CHECK(gallons > 0),
  produced_at TEXT NOT NULL
);
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
CREATE INDEX IF NOT EXISTS idx_orders_ship_month ON orders(ship_month);
CREATE INDEX IF NOT EXISTS idx_packaging_s_number ON packaging(s_number);
CREATE INDEX IF NOT EXISTS idx_packaging_order ON packaging(order_id);
CREATE INDEX IF NOT EXISTS idx_shipments_packaging ON shipments(packaging_id);
CREATE INDEX IF NOT EXISTS idx_shipments_s_number ON shipments(s_number);
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
"""


def initialize_supabase():
    with closing(connect()) as con:
        for statement in SUPABASE_SCHEMA.split(";"):
            if statement.strip():
                con.execute(statement)
        if not con.execute("SELECT 1 FROM users WHERE role='admin' AND active=1").fetchone():
            seed_email = APP_ADMIN_EMAIL if valid_email(APP_ADMIN_EMAIL) else (APP_USERNAME.lower() if valid_email(APP_USERNAME) else "")
            seed_username = seed_email or APP_USERNAME
            con.execute("INSERT INTO users(username,email,display_name,password_hash,role,active,created_at) VALUES(?,?,?,?,?,1,?)",
                        (seed_username, seed_email, "Administrator", hash_password(APP_PASSWORD), "admin", datetime.now().isoformat(timespec="seconds")))
        for category, color, unit in (("Default", "White", "gallons"), ("Default", "Black", "gallons")):
            if not con.execute("SELECT 1 FROM catalog WHERE category=? AND color=?", (category, color)).fetchone():
                con.execute("INSERT INTO catalog(category,color,unit) VALUES(?,?,?)", (category, color, unit))
        con.commit()


def initialize():
    if USE_SUPABASE:
        initialize_supabase()
        return
    with closing(connect()) as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS orders (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          s_number TEXT NOT NULL DEFAULT '',
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
          produced_at TEXT NOT NULL
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
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_packaging_s_number ON packaging(s_number)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_packaging_order ON packaging(order_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_shipments_packaging ON shipments(packaging_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_shipments_s_number ON shipments(s_number)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_stock_packaging_production ON stock_packaging(production_id)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_stock_needs_product ON stock_needs(brand,color,unit,created_at)")
        con.execute("CREATE INDEX IF NOT EXISTS idx_stock_need_fulfillments_need ON stock_need_fulfillments(need_id)")
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
              produced_at TEXT NOT NULL
            );
            INSERT INTO production(id,order_id,brand,color,unit,batch_number,gallons,produced_at)
              SELECT id,order_id,brand,color,unit,batch_number,gallons,produced_at FROM production_old;
            DROP TABLE production_old;
            """)
            con.execute("PRAGMA foreign_keys=ON")
        con.commit()


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


def allocate_stock_need_sql(con, production_id, brand, color, unit, amount):
    available=float(amount); allocated=0.0; now=datetime.now().isoformat(timespec="seconds")
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


def dashboard_data():
    today = date.today().isoformat()
    with closing(connect()) as con:
        orders = con.execute("""
          SELECT o.id, o.s_number, o.brand, o.color, o.unit, o.gallons, o.ship_month, o.created_at,
                 ROUND(o.gallons - COALESCE(SUM(p.gallons),0),2) remaining
          FROM orders o LEFT JOIN production p ON p.order_id=o.id
          GROUP BY o.id HAVING o.gallons - COALESCE(SUM(p.gallons),0) > 0.0001
          ORDER BY o.created_at, o.id
        """).fetchall()
        need = con.execute("""
          SELECT brand, color, unit, ROUND(SUM(remaining),2) gallons FROM (
            SELECT o.brand, o.color, o.unit, o.gallons-COALESCE(SUM(p.gallons),0) remaining
            FROM orders o LEFT JOIN production p ON p.order_id=o.id GROUP BY o.id
          ) WHERE remaining > 0.0001 GROUP BY brand,color,unit ORDER BY gallons DESC
        """).fetchall()
        stock_needs = stock_need_rows_sql(con)
        daily = con.execute("""
          SELECT brand,color,unit,ROUND(SUM(gallons),2) gallons FROM production
          WHERE substr(produced_at,1,10)=? GROUP BY brand,color,unit ORDER BY gallons DESC
        """, (today,)).fetchall()
        total = con.execute("""
          SELECT brand,color,unit,ROUND(SUM(gallons),2) gallons FROM production
          GROUP BY brand,color,unit ORDER BY gallons DESC
        """).fetchall()
        recent = con.execute("""
          SELECT p.batch_number,p.brand,p.color,p.unit,p.gallons,p.produced_at,
                 COALESCE(o.s_number,'') s_number
          FROM production p LEFT JOIN orders o ON o.id=p.order_id
          ORDER BY p.produced_at DESC,p.id DESC LIMIT 12
        """).fetchall()
        order_rows = [dict(x) for x in orders]
        for order in order_rows:
            order["recipients"] = [dict(x) for x in con.execute("""SELECT c.id,c.name,c.email FROM contacts c
              JOIN order_recipients r ON r.contact_id=c.id WHERE r.order_id=? ORDER BY c.name""", (order["id"],)).fetchall()]
        requirement_orders = con.execute("""SELECT o.id,o.s_number,o.brand,o.color,o.gallons,o.unit,o.ship_month
          FROM orders o LEFT JOIN s_number_status s ON s.s_number=o.s_number
          WHERE s.shipped_at IS NULL ORDER BY o.s_number,o.brand,o.color,o.id""").fetchall()
        active_orders = con.execute("""SELECT o.id,o.s_number,o.brand,o.color,o.gallons,o.unit,o.ship_month,o.created_at
          FROM orders o LEFT JOIN s_number_status s ON s.s_number=o.s_number
          WHERE s.shipped_at IS NULL ORDER BY o.s_number,o.brand,o.color,o.id""").fetchall()
        schedule_raw = con.execute("""SELECT o.id,o.s_number,o.brand,o.color,o.unit,o.gallons,o.ship_month,o.created_at,
          ROUND(COALESCE(SUM(p.gallons),0),2) produced,
          ROUND(CASE WHEN o.gallons-COALESCE(SUM(p.gallons),0)>0
                THEN o.gallons-COALESCE(SUM(p.gallons),0) ELSE 0 END,2) remaining,
          s.ready_at,s.shipped_at FROM orders o LEFT JOIN production p ON p.order_id=o.id
          LEFT JOIN s_number_status s ON s.s_number=o.s_number GROUP BY o.id,s.ready_at,s.shipped_at
          ORDER BY CASE WHEN o.ship_month='' THEN 1 ELSE 0 END,o.ship_month,o.s_number,o.id""").fetchall()
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
        stock_packageable = con.execute("""SELECT * FROM (SELECT p.id,p.brand,p.color,p.unit,p.batch_number,
          ROUND(p.gallons,2) produced,ROUND(COALESCE((SELECT SUM(k.gallons) FROM stock_packaging k WHERE k.production_id=p.id),0),2) packaged,
          ROUND(p.gallons-COALESCE((SELECT SUM(k.gallons) FROM stock_packaging k WHERE k.production_id=p.id),0),2) available
          FROM production p WHERE p.order_id IS NULL) WHERE available>0.0001 ORDER BY brand,color,id""").fetchall()
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
          FROM shipments h JOIN packaging k ON k.id=h.packaging_id JOIN orders o ON o.id=k.order_id ORDER BY h.shipped_at DESC,h.id DESC
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
        s_numbers = []
        for status in statuses:
            remaining = {x["unit"]: round(float(x["remaining"]),2) for x in con.execute("""
              SELECT unit,SUM(CASE WHEN remaining>0 THEN remaining ELSE 0 END) remaining FROM (
                SELECT o.unit,o.gallons-COALESCE((SELECT SUM(p.gallons) FROM production p WHERE p.order_id=o.id),0) remaining
                FROM orders o WHERE o.s_number=?
              ) GROUP BY unit""", (status["s_number"],)).fetchall()}
            unpackaged = {x["unit"]: round(float(x["available"]),2) for x in con.execute("""SELECT unit,
              SUM(CASE WHEN available>0 THEN available ELSE 0 END) available FROM (
                SELECT o.unit,COALESCE((SELECT SUM(p.gallons) FROM production p WHERE p.order_id=o.id),0)-
                       COALESCE((SELECT SUM(k.gallons) FROM packaging k WHERE k.order_id=o.id),0) available
                FROM orders o WHERE o.s_number=?) GROUP BY unit""", (status["s_number"],)).fetchall()}
            packed = con.execute("""SELECT COALESCE(SUM(k.package_count-COALESCE((SELECT SUM(h.package_count) FROM shipments h WHERE h.packaging_id=k.id),0)),0),
              COALESCE(SUM((k.package_count-COALESCE((SELECT SUM(h.package_count) FROM shipments h WHERE h.packaging_id=k.id),0))*k.package_size),0)
              FROM packaging k WHERE k.s_number=?""", (status["s_number"],)).fetchone()
            s_numbers.append({**dict(status), "remaining_gallons": remaining.get("gallons",0),
                              "remaining_qty": remaining.get("qty",0), "unpackaged_gallons": unpackaged.get("gallons",0),
                              "unpackaged_qty": unpackaged.get("qty",0),
                              "package_count": int(packed[0]), "packaged_gallons": round(float(packed[1]),2)})
    need_rows=[dict(x) for x in need]
    for stock_need in stock_needs:
        if float(stock_need["remaining"])<=.0001: continue
        existing=next((x for x in need_rows if x["brand"]==stock_need["brand"] and x["color"]==stock_need["color"] and x["unit"]==stock_need["unit"]),None)
        if existing: existing["gallons"]=round(float(existing["gallons"])+float(stock_need["remaining"]),2)
        else: need_rows.append({"brand":stock_need["brand"],"color":stock_need["color"],"unit":stock_need["unit"],"gallons":stock_need["remaining"]})
    need_rows.sort(key=lambda x:float(x["gallons"]),reverse=True)
    return {"today": today, "orders": order_rows, "active_orders": [dict(x) for x in active_orders], "order_schedule": order_schedule,
            "requirement_orders": [dict(x) for x in requirement_orders],
            "need": need_rows, "daily": [dict(x) for x in daily],
            "total": [dict(x) for x in total], "recent": [dict(x) for x in recent],
            "package_types": package_type_data(), "packageable": packageable_rows,
            "inventory": [dict(x) for x in inventory], "shipped": [dict(x) for x in shipped],
            "shipping_inventory": [dict(x) for x in shipping_inventory], "stock_inventory": [dict(x) for x in stock_inventory],
            "packaging_requirements": [dict(x) for x in requirements], "s_numbers": s_numbers,"stock_needs":stock_needs}


def admin_data():
    with closing(connect()) as con:
        orders = con.execute("""
          SELECT o.id,o.s_number,o.brand,o.color,o.unit,o.gallons,o.ship_month,o.created_at,
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
        order_rows = [dict(x) for x in orders]
        for order in order_rows:
            order["recipient_ids"] = [x[0] for x in con.execute("SELECT contact_id FROM order_recipients WHERE order_id=?", (order["id"],)).fetchall()]
    return {"orders": order_rows, "batches": [dict(x) for x in batches],
            "catalog": [dict(x) for x in catalog], "contacts": [dict(x) for x in contacts],
            "packaging": [dict(x) for x in packaging], "shipments": [dict(x) for x in shipments],
            "stock_packaging": [dict(x) for x in stock_packaging], "packaging_requirements": [dict(x) for x in requirements],
            "package_types": package_type_data()}


def contact_data():
    with closing(connect()) as con:
        return [dict(x) for x in con.execute("SELECT id,name,email FROM contacts WHERE active=1 ORDER BY name,email").fetchall()]


def user_data():
    with closing(connect()) as con:
        return [dict(x) for x in con.execute("SELECT id,username,email,display_name,role,active,created_at FROM users ORDER BY active DESC,display_name,email,username").fetchall()]


def deliver_notification_email(recipient_emails, subject, body):
    recipients = [str(email or "").strip() for email in recipient_emails if valid_email(email)]
    if not recipients:
        return None
    if not EMAIL_ENABLED:
        return "Production saved; email notifications are disabled for this environment."
    if EMAIL_PROVIDER == "google_apps_script":
        if not GOOGLE_APPS_SCRIPT_URL.startswith("https://script.google.com/") or not GOOGLE_APPS_SCRIPT_SECRET:
            return "Production saved, but Google Apps Script email is not configured."
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
            return f"Production saved, but Google Apps Script email failed with HTTP {exc.code}."
        except (URLError, TimeoutError, OSError) as exc:
            return f"Production saved, but Google Apps Script email failed: {exc}."
        except (ValueError, RuntimeError) as exc:
            return f"Production saved, but Google Apps Script email failed: {exc}."
    if not GMAIL_ADDRESS or not GMAIL_APP_PASSWORD:
        return "Production saved, but Gmail is not configured."
    message = EmailMessage(); message["From"] = GMAIL_ADDRESS
    message["To"] = ", ".join(recipients); message["Subject"] = subject; message.set_content(body)
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as smtp:
            smtp.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD); smtp.send_message(message)
        return f"Email sent to {len(recipients)} recipient(s)."
    except Exception as exc:
        return f"Production saved, but email failed: {exc}"


def send_production_email(order_id, batch_number, produced_amount, remaining):
    with closing(connect()) as con:
        order = con.execute("SELECT s_number,brand,color,unit FROM orders WHERE id=?", (order_id,)).fetchone()
        recipients = con.execute("""SELECT c.email FROM contacts c JOIN order_recipients r ON r.contact_id=c.id
          WHERE r.order_id=? AND c.active=1 ORDER BY c.email""", (order_id,)).fetchall()
    if not order or not recipients: return None
    unit = "qty" if order["unit"] == "qty" else "gallons"; complete = remaining <= .0001
    subject = f"{order['s_number']} - {'Production complete' if complete else 'Partial production update'}"
    status = "COMPLETE" if complete else "PARTIALLY PRODUCED"
    body = (f"S#: {order['s_number']}\nCategory: {order['brand']}\nColor: {order['color']}\n"
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
    data = admin_data(); stock_needs = dashboard_data().get("stock_needs", []); totals = {}
    def row(category, unit):
        return totals.setdefault((category, unit), {"category": category, "unit": unit,
          "ordered": 0.0, "produced": 0.0, "need": 0.0})
    for item in data["catalog"]: row(item["category"], item.get("unit", "gallons"))
    for order in data["orders"]:
        item = row(order["brand"], order.get("unit", "gallons"))
        item["ordered"] += float(order["gallons"]); item["need"] += max(float(order["remaining"]), 0)
    for need in stock_needs:
        item = row(need["brand"], need.get("unit", "gallons"))
        item["ordered"] += float(need["amount"]); item["need"] += max(float(need["remaining"]), 0)
    for batch in data["batches"]:
        row(batch["brand"], batch.get("unit", "gallons"))["produced"] += float(batch["gallons"])
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
        orders = [dict(x) for x in con.execute("SELECT id,s_number,brand,color,unit,gallons,created_at FROM orders ORDER BY s_number,brand,color,id").fetchall()]
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
        stock_summary = [dict(x) for x in con.execute("""SELECT p.id production_id,p.batch_number,p.brand,p.color,p.unit,p.gallons produced_amount,
          ROUND(COALESCE((SELECT SUM(e.hours) FROM labor_entries e WHERE e.order_id IS NULL AND e.production_id=p.id),0),2) labor_hours,
          ROUND(COALESCE((SELECT SUM(e.labor_cost) FROM labor_entries e WHERE e.order_id IS NULL AND e.production_id=p.id),0),2) labor_cost,
          ROUND(CASE WHEN p.gallons>0 THEN COALESCE((SELECT SUM(e.labor_cost) FROM labor_entries e WHERE e.order_id IS NULL AND e.production_id=p.id),0)/p.gallons ELSE 0 END,4) cost_per_unit
          FROM production p WHERE p.order_id IS NULL ORDER BY p.produced_at DESC,p.id DESC""").fetchall()]
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
            "processes": ["Mixing / Production", "Quality Control", "Filling / Packaging", "Material Handling", "Shipping / Loading", "Other"]}


def reporting_data():
    generated_at = datetime.now().isoformat(timespec="seconds")
    with closing(connect()) as con:
        order_rows = [dict(x) for x in con.execute("""SELECT o.id order_id,o.s_number,o.brand,o.color,o.unit,o.created_at,
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
        package_rows = [dict(x) for x in con.execute("""SELECT r.id,r.order_id,r.s_number,r.brand,r.color,o.unit,o.created_at,
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
          "SELECT unit,COALESCE(SUM(gallons),0) amount FROM production GROUP BY unit").fetchall()}
        stock_inventory = {x["unit"]: float(x["amount"]) for x in con.execute("""SELECT p.unit,COALESCE(SUM(k.gallons),0) amount
          FROM stock_packaging k JOIN production p ON p.id=k.production_id GROUP BY p.unit""").fetchall()}
        total_labor_cost = float(con.execute("SELECT COALESCE(SUM(labor_cost),0) FROM labor_entries").fetchone()[0])
        stock_needs = stock_need_rows_sql(con)
    for row in order_rows:
        row["status"] = "Shipped" if row["shipped_at"] else "Ready" if row["ready_at"] else "Packaging" if row["need_amount"] <= .0001 else "In Production"
    for row in package_rows:
        row["status"] = "Shipped" if row["shipped_at"] else "Ready" if row["ready_at"] and row["balance_count"] > 0 else "Packaging"
    s_map = {}; category_map = {}
    for row in order_rows:
        for target, key, base in ((s_map,(row["s_number"],row["unit"]),{"s_number":row["s_number"],"unit":row["unit"]}),
                                  (category_map,(row["brand"],row["unit"]),{"brand":row["brand"],"unit":row["unit"]})):
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
    required = {"gallons":0.0,"qty":0.0}; need={"gallons":0.0,"qty":0.0}; shipped={"gallons":0.0,"qty":0.0}; inventory={"gallons":0.0,"qty":0.0}
    for row in order_rows:
        unit=row["unit"]; required[unit]+=float(row["required_amount"]); need[unit]+=float(row["need_amount"])
        shipped[unit]+=float(row["shipped_amount"]); inventory[unit]+=float(row["inventory_amount"])
    for row in stock_needs:
        required[row["unit"]]+=float(row["amount"]); need[row["unit"]]+=float(row["remaining"])
    for unit,amount in stock_inventory.items(): inventory[unit]+=amount
    snapshot={"labor_cost":round(total_labor_cost,2),"required_gallons":round(required["gallons"],2),"required_qty":round(required["qty"],2),
      "produced_gallons":round(produced_totals.get("gallons",0),2),"produced_qty":round(produced_totals.get("qty",0),2),
      "need_gallons":round(need["gallons"],2),"need_qty":round(need["qty"],2),"shipped_gallons":round(shipped["gallons"],2),
      "shipped_qty":round(shipped["qty"],2),"inventory_gallons":round(inventory["gallons"],2),"inventory_qty":round(inventory["qty"],2),
      "s_numbers":len({x["s_number"] for x in order_rows}),"active_s_numbers":len({x["s_number"] for x in order_rows if x["status"]!="Shipped"})}
    return {"generated_at":generated_at,"snapshot":snapshot,"product_lines":order_rows,"s_summary":s_summary,
            "category_summary":category_summary,"package_rows":package_rows,"stock_needs":stock_needs,"package_types":package_type_data()}


def record_labor_sql(con, payload, order=None, production=None, process_name="Production", actor="System"):
    hours = float(payload.get("labor_hours",0) or 0); rate_id = int(payload.get("rate_id",0) or 0)
    if hours <= 0 and not rate_id: return None
    if hours <= 0 or not rate_id: raise ValueError("Select a labor rate and enter labor hours greater than zero.")
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
    writer.writerow([]); writer.writerow(["FUTURE STOCK COST"])
    writer.writerow(["Batch", "Category", "Color", "Unit", "Produced Amount", "Labor Hours", "Labor Cost", "Labor Cost Per Produced Unit"])
    for row in data["stock_summary"]:
        writer.writerow([row["batch_number"],row["brand"],row["color"],"piece" if row["unit"]=="qty" else "gallon",
                         row["produced_amount"],row["labor_hours"],row["labor_cost"],row["cost_per_unit"]])
    return output.getvalue().encode("utf-8-sig")


class Handler(BaseHTTPRequestHandler):
    def send_json(self, obj, status=200):
        data = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers(); self.wfile.write(data)

    def body(self):
        size = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(size) or b"{}")

    def send_csv(self, data, filename):
        self.send_response(200); self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)

    def send_file(self, filename):
        data = (ROOT / filename).read_bytes(); self.send_response(200)
        content_type = "text/css; charset=utf-8" if filename.endswith(".css") else "text/html; charset=utf-8"
        self.send_header("Content-Type", content_type); self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers(); self.wfile.write(data)

    def send_redirect(self, location):
        self.send_response(302); self.send_header("Location", location); self.end_headers()

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
        path = urlparse(self.path).path
        if path == "/health":
            try:
                with closing(connect()) as con:
                    con.execute("SELECT 1").fetchone()
                return self.send_json({"ok": True, "database": "supabase" if USE_SUPABASE else "sqlite"})
            except Exception:
                return self.send_json({"ok": False}, 503)
        if path == "/api/dashboard": return self.send_json(dashboard_data())
        if path == "/app.css": return self.send_file("app.css")
        if path in ("/login", "/login.html"): return self.send_file("login.html")
        if path == "/logout":
            SESSIONS.pop(self.session_token(), None)
            self.send_response(302); self.send_header("Location", "/login")
            self.send_header("Set-Cookie", "ollies_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")
            self.end_headers(); return
        if path == "/api/session":
            user = self.current_user()
            return self.send_json({"user": user}, 200 if user else 401)
        if path == "/api/reports/category.csv":
            if not self.authorized(): return
            return self.send_csv(category_report_csv(), f"ollies-category-production-{date.today().isoformat()}.csv")
        if path == "/api/costing":
            if not self.authorized(): return
            return self.send_json(costing_data())
        if path == "/api/reporting":
            if not self.authorized(): return
            return self.send_json(reporting_data())
        if path == "/api/reports/labor.csv":
            if not self.authorized(): return
            return self.send_csv(labor_report_csv(), f"ollies-labor-cost-{date.today().isoformat()}.csv")
        if path == "/api/catalog":
            if not self.authorized(): return
            return self.send_json(catalog_data())
        if path == "/api/contacts":
            if not self.authorized(): return
            return self.send_json(contact_data())
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
            payload = self.body(); path = urlparse(self.path).path
            if path == "/api/login":
                login = str(payload.get("email", payload.get("username", ""))).strip().lower(); password = str(payload.get("password", ""))
                with closing(connect()) as con:
                    user = con.execute("SELECT id,username,email,display_name,password_hash,role FROM users WHERE active=1 AND (lower(email)=? OR lower(username)=?)", (login,login)).fetchone()
                if not user or not verify_password(password, user["password_hash"]):
                    return self.send_json({"error": "Incorrect username or password."}, 401)
                token = create_session(user); data = json.dumps({"ok": True, "role": user["role"]}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie", f"ollies_session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age=43200")
                self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
            if path == "/api/logout":
                SESSIONS.pop(self.session_token(), None); data = json.dumps({"ok": True}).encode()
                self.send_response(200); self.send_header("Content-Type", "application/json")
                self.send_header("Set-Cookie", "ollies_session=; Path=/; HttpOnly; SameSite=Lax; Max-Age=0")
                self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data); return
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
                    shipment = con.execute("SELECT s_number FROM shipments WHERE id=?", (shipment_id,)).fetchone()
                    if not shipment: raise ValueError("Shipment record was not found.")
                    con.execute("DELETE FROM shipments WHERE id=?", (shipment_id,)); refresh_s_number_shipped(con,shipment["s_number"]); con.commit()
                return self.send_json({"ok": True})
            if path == "/api/shipments":
                user = self.require_role("edit")
                if not user: return
                packaging_id = int(payload.get("packaging_id", 0)); raw_count = float(payload.get("package_count", 0)); package_count = int(raw_count)
                if raw_count != package_count or package_count <= 0: raise ValueError("Ship quantity must be a whole number greater than zero.")
                actor = user.get("display_name") or user.get("email") or user.get("username", "")
                with closing(connect()) as con:
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
                    stock = con.execute("""SELECT k.*,p.batch_number,p.gallons production_gallons,p.unit FROM stock_packaging k
                      JOIN production p ON p.id=k.production_id WHERE k.id=?""", (stock_packaging_id,)).fetchone()
                    if not stock: raise ValueError("Stock package record was not found.")
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
                    allocation_batch = f"{stock['batch_number']}-S{order_id}-A{stock_packaging_id}-{int(time.time())}"
                    cur=con.execute("INSERT INTO production(order_id,brand,color,unit,batch_number,gallons,produced_at) VALUES(?,?,?,?,?,?,?)",
                                (order_id,stock["brand"],stock["color"],stock["unit"],allocation_batch,assigned_gallons,datetime.now().isoformat(timespec="seconds")))
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
                    if remaining_gallons > .0001: con.execute("UPDATE production SET gallons=? WHERE id=?", (remaining_gallons,stock["production_id"]))
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
                    stock = con.execute("""SELECT k.*,p.batch_number source_batch,p.gallons production_gallons,p.unit
                      FROM stock_packaging k JOIN production p ON p.id=k.production_id WHERE k.id=?""", (stock_packaging_id,)).fetchone()
                    if not stock: raise ValueError("Packaged source material was not found.")
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
                    if remaining_source > .0001: con.execute("UPDATE production SET gallons=? WHERE id=?",(remaining_source,stock["production_id"]))
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
                brand = str(payload.get("brand", "")).strip()
                color = str(payload.get("color", "")).strip()
                gallons = float(payload.get("gallons", 0)); ship_month = normalize_ship_month(payload.get("ship_month"))
                if not brand or not color or gallons <= 0: raise ValueError("Enter category, color, and an amount greater than zero.")
                with closing(connect()) as con:
                    if not s_number: s_number = next_no_s_reference_sql(con)
                    closed = con.execute("SELECT done_at,shipped_at FROM s_number_status WHERE s_number=?", (s_number,)).fetchone()
                    if closed and (closed["done_at"] or closed["shipped_at"]): raise ValueError("This order is already ready or shipped and cannot receive another line.")
                    item = con.execute("SELECT unit FROM catalog WHERE category=? AND color=?", (brand,color)).fetchone()
                    if not item: raise ValueError("Select a color from the chosen category.")
                    cur = con.execute("INSERT INTO orders(s_number,brand,color,unit,gallons,ship_month,created_at) VALUES(?,?,?,?,?,?,?)",
                                (s_number, brand, color, item["unit"], gallons, ship_month, datetime.now().isoformat(timespec="seconds")))
                    order_id = cur.lastrowid
                    recipient_ids = payload.get("recipient_ids", [])
                    if not isinstance(recipient_ids, list): raise ValueError("Email recipients are invalid.")
                    for contact_id in {int(x) for x in recipient_ids}:
                        active = con.execute("SELECT 1 FROM contacts WHERE id=? AND active=1", (contact_id,)).fetchone()
                        if active: con.execute("INSERT INTO order_recipients(order_id,contact_id) VALUES(?,?)", (order_id,contact_id))
                    con.commit()
                return self.send_json({"ok": True,"id":order_id,"s_number":s_number,"has_s_number":s_number.startswith("S")}, 201)
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
                        stock = con.execute("""SELECT p.*,p.gallons-COALESCE((SELECT SUM(k.gallons) FROM stock_packaging k WHERE k.production_id=p.id),0) available
                          FROM production p WHERE p.id=? AND p.order_id IS NULL""", (source_id,)).fetchone()
                        if not stock: raise ValueError("Stock production batch was not found.")
                        package_size = package_size_for(package_type,stock["unit"],payload.get("package_size")); gallons = round(package_size * package_count, 2)
                        if gallons > stock["available"] + .0001:
                            raise ValueError(f"Only {stock['available']:.2f} stock {stock['unit']} remain to package; {package_count} {label}(s) require {gallons:.2f} {stock['unit']}.")
                        con.execute("""INSERT INTO stock_packaging(production_id,brand,color,package_type,package_size,package_count,gallons,packaged_at,created_by)
                          VALUES(?,?,?,?,?,?,?,?,?)""", (source_id,stock["brand"],stock["color"],package_type,package_size,package_count,gallons,
                          datetime.now().isoformat(timespec="seconds"),actor))
                        labor_cost=record_labor_sql(con,payload,production=stock,process_name="Filling / Packaging",actor=actor)
                    else:
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
                if record_id == self.current_user()["id"]:
                    self.current_user().update({"username": email, "email": email, "display_name": display_name})
                return self.send_json({"ok": True})
            if kind == "packaging":
                package_type = str(payload.get("package_type", "")).strip(); raw_count = float(payload.get("package_count", 0))
                package_count = int(raw_count); packaged_at = str(payload.get("packaged_at", "")).strip()
                if package_type not in PACKAGE_TYPES or raw_count != package_count or package_count <= 0 or not packaged_at:
                    raise ValueError("Select a package type, enter a whole-number count, and provide the packaged date.")
                with closing(connect()) as con:
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
                gallons = float(payload.get("gallons", 0)); created_at = str(payload.get("created_at", "")).strip(); ship_month = normalize_ship_month(payload.get("ship_month"))
                if not brand or not color or gallons <= 0 or not created_at: raise ValueError("Complete every order field.")
                with closing(connect()) as con:
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
                    cur = con.execute("UPDATE orders SET s_number=?,brand=?,color=?,unit=?,gallons=?,ship_month=?,created_at=? WHERE id=?", (s_number,brand,color,unit,gallons,ship_month,created_at,record_id))
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
                    old=con.execute("SELECT * FROM production WHERE id=?",(record_id,)).fetchone()
                    if not old: raise ValueError("Batch was not found.")
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
            self.send_json({"error": "That login email already exists." if kind == "users" else "That batch number already exists."}, 400)
        except (ValueError, TypeError, json.JSONDecodeError) as e:
            self.send_json({"error": str(e)}, 400)

    def do_DELETE(self):
        if not self.require_role("delete"): return
        try:
            parts = urlparse(self.path).path.strip("/").split("/")
            if len(parts) != 4 or parts[:2] != ["api", "admin"]: return self.send_error(404)
            kind, record_id = parts[2], int(parts[3])
            with closing(connect()) as con:
                if kind == "batches":
                    if con.execute("SELECT 1 FROM labor_entries WHERE production_id=?", (record_id,)).fetchone():
                        raise ValueError("Delete this batch's labor entries in Labor Costing before deleting the batch.")
                    cur = con.execute("DELETE FROM production WHERE id=?", (record_id,))
                    if not cur.rowcount: raise ValueError("Batch was not found.")
                elif kind == "packaging":
                    item = con.execute("""SELECT k.s_number,s.shipped_at FROM packaging k LEFT JOIN s_number_status s
                      ON s.s_number=k.s_number WHERE k.id=?""", (record_id,)).fetchone()
                    if not item: raise ValueError("Packaging record was not found.")
                    if con.execute("SELECT 1 FROM shipments WHERE packaging_id=?", (record_id,)).fetchone():
                        raise ValueError("Undo this record's shipments before deleting its package inventory.")
                    con.execute("DELETE FROM packaging WHERE id=?", (record_id,))
                    refresh_s_number_ready(con,item["s_number"],self.current_user().get("display_name","System"))
                elif kind == "requirements":
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
                elif kind == "orders":
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
                    cur = con.execute("UPDATE contacts SET active=0 WHERE id=?", (record_id,))
                    if not cur.rowcount: raise ValueError("Email contact was not found.")
                elif kind == "users":
                    if record_id == self.current_user()["id"]: raise ValueError("You cannot delete your own account.")
                    cur = con.execute("DELETE FROM users WHERE id=?", (record_id,))
                    if not cur.rowcount: raise ValueError("User was not found.")
                else: return self.send_error(404)
                con.commit()
            return self.send_json({"ok": True})
        except (ValueError, TypeError) as e:
            self.send_json({"error": str(e)}, 400)

    def log_message(self, fmt, *args):
        pass


def main():
    initialize()
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
    ThreadingHTTPServer(("0.0.0.0", port), Handler).serve_forever()


if __name__ == "__main__":
    main()
