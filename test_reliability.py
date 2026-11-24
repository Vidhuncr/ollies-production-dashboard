import gzip
import http.client
import json
import sqlite3
import threading
from pathlib import Path

import app_sqlite_backup as app


app.USE_SUPABASE = False
app.SUPABASE_DATABASE_URL = ""
app.EMAIL_ENABLED = False
test_db = Path(__file__).with_name("test_reliability.db")
if test_db.exists():
    test_db.unlink()
app.DB = test_db
app.invalidate_dashboard_cache()
app.initialize()

server = app.BoundedThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()


def call(path, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    connection.request("GET", path, headers=headers or {})
    response = connection.getresponse()
    data = response.read()
    result = response.status, data, dict(response.getheaders())
    connection.close()
    return result


try:
    status, raw, _ = call("/health")
    assert status == 200 and json.loads(raw)["ok"] is True
    status, raw, _ = call("/ready")
    assert status == 200 and json.loads(raw)["ok"] is True

    status, runtime, headers = call("/app-runtime.js")
    assert status == 200
    assert b"resilientFetch" in runtime and b"attempts = retryable ? 6 : 1" in runtime
    assert b"scheduleInitialRecovery" in runtime and b"location.reload()" in runtime
    assert "max-age=300" in headers["Cache-Control"]
    etag = headers["ETag"]
    status, raw, headers = call("/app-runtime.js", {"If-None-Match": etag})
    assert status == 304 and raw == b""

    status, raw, headers = call("/api/dashboard", {"Accept-Encoding": "gzip"})
    assert status == 200 and headers.get("Content-Encoding") == "gzip"
    assert "performance" in json.loads(gzip.decompress(raw))

    # An expired dashboard can continue serving the last verified snapshot
    # during a brief database interruption instead of showing a blank screen.
    original_dashboard_data = app.dashboard_data
    app.DASHBOARD_CACHE["expires"] = 0
    app.dashboard_data = lambda: (_ for _ in ()).throw(sqlite3.OperationalError("temporary outage"))
    status, raw, _ = call("/api/dashboard")
    assert status == 200 and "performance" in json.loads(raw)
    app.dashboard_data = original_dashboard_data

    html_files = list(Path(__file__).parent.glob("*.html"))
    assert html_files and all('/app-runtime.js' in path.read_text(encoding="utf-8") for path in html_files)
    assert "goto run_dashboard" in Path("Start Dashboard.bat").read_text(encoding="utf-8")
    assert "healthCheckPath: /health" in Path("render.yaml").read_text(encoding="utf-8")

    print("PASS: liveness/readiness checks, compressed responses, static caching, safe read retries, stale dashboard recovery, and local restart loop")
finally:
    app.dashboard_data = globals().get("original_dashboard_data", app.dashboard_data)
    server.shutdown()
    server.server_close()
    if test_db.exists():
        test_db.unlink()
