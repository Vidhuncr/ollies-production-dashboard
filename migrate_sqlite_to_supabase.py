"""One-time migration from the local SQLite database to Supabase PostgreSQL.

Set SUPABASE_DATABASE_URL in .env, then run:
    python migrate_sqlite_to_supabase.py

The target may contain only the administrator and default catalog rows created
by the application. The script refuses to merge into a Supabase database that
already has production records, which prevents accidental overwrites.
"""

import os
import sqlite3
from pathlib import Path

import psycopg

import app_sqlite_backup as backend


ROOT = Path(__file__).resolve().parent
SOURCE_DB = ROOT / "plant_production.db"
BUSINESS_TABLES = (
    "orders", "production", "contacts", "order_recipients", "s_number_status",
    "packaging", "shipments", "stock_packaging", "stock_needs",
    "stock_need_fulfillments", "packaging_requirements", "labor_rates",
    "labor_entries",
)
COPY_ORDER = (
    "contacts", "orders", "production", "order_recipients", "s_number_status",
    "packaging", "shipments", "stock_packaging", "stock_needs",
    "stock_need_fulfillments", "packaging_requirements", "labor_rates",
    "labor_entries",
)
ID_TABLES = tuple(table for table in COPY_ORDER if table not in ("order_recipients", "s_number_status"))


def sqlite_rows(connection, table):
    columns = [row[1] for row in connection.execute(f"PRAGMA table_info([{table}])")]
    rows = connection.execute(f"SELECT * FROM [{table}]").fetchall()
    return columns, rows


def migrate():
    database_url = os.environ.get("SUPABASE_DATABASE_URL", "").strip()
    if not database_url:
        raise SystemExit("SUPABASE_DATABASE_URL is missing. Add the Supabase Session Pooler connection string to .env first.")
    if not SOURCE_DB.exists():
        raise SystemExit(f"Local database was not found: {SOURCE_DB}")

    backend.initialize_supabase()
    source = sqlite3.connect(SOURCE_DB)
    source.row_factory = sqlite3.Row
    target = psycopg.connect(database_url, connect_timeout=10, prepare_threshold=None)
    try:
        populated = []
        with target.cursor() as cursor:
            for table in BUSINESS_TABLES:
                cursor.execute(f"SELECT COUNT(*) FROM {table}")
                count = cursor.fetchone()[0]
                if count:
                    populated.append(f"{table}={count}")
        if populated:
            raise SystemExit("Supabase already contains production data (" + ", ".join(populated) + "). Migration stopped without changing it.")

        with target.transaction():
            with target.cursor() as cursor:
                catalog_columns, catalog_rows = sqlite_rows(source, "catalog")
                for row in catalog_rows:
                    data = dict(zip(catalog_columns, row))
                    cursor.execute("""INSERT INTO catalog(category,color,unit) VALUES(%s,%s,%s)
                      ON CONFLICT(category,color) DO UPDATE SET unit=EXCLUDED.unit""",
                                   (data["category"], data["color"], data.get("unit", "gallons")))

                user_columns, user_rows = sqlite_rows(source, "users")
                for row in user_rows:
                    data = dict(zip(user_columns, row))
                    email = str(data.get("email") or "").lower()
                    cursor.execute("SELECT id FROM users WHERE lower(username)=lower(%s) OR (%s<>'' AND lower(email)=%s)",
                                   (data["username"], email, email))
                    existing = cursor.fetchone()
                    values = (data["username"], email, data["display_name"], data["password_hash"],
                              data["role"], data.get("active", 1), data["created_at"])
                    if existing:
                        cursor.execute("""UPDATE users SET username=%s,email=%s,display_name=%s,password_hash=%s,
                          role=%s,active=%s,created_at=%s WHERE id=%s""", values + (existing[0],))
                    else:
                        cursor.execute("""INSERT INTO users(username,email,display_name,password_hash,role,active,created_at)
                          VALUES(%s,%s,%s,%s,%s,%s,%s)""", values)

                for table in COPY_ORDER:
                    columns, rows = sqlite_rows(source, table)
                    if not rows:
                        continue
                    column_sql = ",".join(columns)
                    placeholders = ",".join(["%s"] * len(columns))
                    statement = f"INSERT INTO {table}({column_sql}) VALUES({placeholders}) ON CONFLICT DO NOTHING"
                    cursor.executemany(statement, [tuple(row[column] for column in columns) for row in rows])

                for table in ID_TABLES:
                    cursor.execute(f"SELECT setval(pg_get_serial_sequence('{table}','id'), COALESCE(MAX(id),1), MAX(id) IS NOT NULL) FROM {table}")

        print("Migration completed successfully.")
        with target.cursor() as cursor:
            for table in ("orders", "production", "packaging", "shipments", "labor_entries", "users"):
                cursor.execute(f"SELECT COUNT(*) FROM {table}")
                print(f"{table}: {cursor.fetchone()[0]}")
    finally:
        source.close()
        target.close()


if __name__ == "__main__":
    migrate()
