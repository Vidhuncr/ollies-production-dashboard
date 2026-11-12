from pathlib import Path

import app_sqlite_backup as backend


ROOT = Path(__file__).resolve().parent


row = backend.DatabaseRow(("id", "amount"), (7, 12.5))
assert row[0] == 7 and row["amount"] == 12.5
assert dict(row) == {"id": 7, "amount": 12.5}
assert backend.postgres_sql("SELECT * FROM orders WHERE id=?") == "SELECT * FROM orders WHERE id=%s"
assert "CREATE TABLE IF NOT EXISTS orders" in backend.SUPABASE_SCHEMA
assert "ship_month TEXT" in backend.SUPABASE_SCHEMA
assert "packaging_requirements" in backend.SUPABASE_SCHEMA
assert "labor_entries" in backend.SUPABASE_SCHEMA
assert "ENABLE ROW LEVEL SECURITY" in backend.SUPABASE_SCHEMA

requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
render = (ROOT / "render.yaml").read_text(encoding="utf-8")
gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
entrypoint = (ROOT / "app.py").read_text(encoding="utf-8")
migration = (ROOT / "migrate_sqlite_to_supabase.py").read_text(encoding="utf-8")
admin_page = (ROOT / "admin.html").read_text(encoding="utf-8")
assert "psycopg" in requirements
assert "SUPABASE_DATABASE_URL" in render
assert "EMAIL_ENABLED" in render and 'value: "false"' in render
assert "EMAIL_PROVIDER" in render and "GOOGLE_APPS_SCRIPT_URL" in render and "GOOGLE_APPS_SCRIPT_SECRET" in render
assert "DB_POOL_MAX" in render and "MAX_REQUEST_THREADS" in render and "DASHBOARD_CACHE_TTL" in render
assert "idx_production_order" in backend.SUPABASE_SCHEMA and "idx_packaging_order_type" in backend.SUPABASE_SCHEMA
assert hasattr(backend, "PostgresPool") and hasattr(backend, "BoundedThreadingHTTPServer")
assert hasattr(backend, "decode_session") and "hmac.new(SESSION_SECRET" in Path(backend.__file__).read_text(encoding="utf-8")
assert "saveContact" in admin_page and "contactActive" in admin_page and "Email recipient updated" in admin_page
assert "*.db" in gitignore and ".env" in gitignore and ".venv/" in gitignore
assert "backend.main()" in entrypoint
assert "SUPABASE_DATABASE_URL" in migration

print("PASS: Supabase PostgreSQL configuration, schema, migration, and Render settings")
