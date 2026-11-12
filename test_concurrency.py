import http.client
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import app_sqlite_backup as app


app.USE_SUPABASE = False
app.SUPABASE_DATABASE_URL = ""
app.EMAIL_ENABLED = False
app.DASHBOARD_CACHE_TTL = 1.5
TEST_DB = Path(__file__).with_name("test_concurrency.db")
if TEST_DB.exists():
    TEST_DB.unlink()
app.DB = TEST_DB
app.invalidate_dashboard_cache()
app.initialize()

server = app.BoundedThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()


def call(method, path, body=None, token=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=15)
    headers = {"Accept": "application/json"}
    if token:
        headers["Cookie"] = f"ollies_session={token}"
    raw = None
    if body is not None:
        raw = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    connection.request(method, path, raw, headers)
    response = connection.getresponse()
    content = response.read()
    payload = json.loads(content) if content else None
    result = (response.status, payload, dict(response.getheaders()))
    connection.close()
    return result


try:
    status, _, headers = call("POST", "/api/login", {
        "email": app.APP_ADMIN_EMAIL,
        "password": app.APP_PASSWORD,
    })
    assert status == 200
    token = headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]

    status, catalog, _ = call("POST", "/api/catalog", {
        "category": "Load Test Paint",
        "color": "White",
        "unit": "gallons",
    }, token)
    assert status == 201, catalog

    status, order, _ = call("POST", "/api/orders", {
        "s_number": "S90001",
        "brand": "Load Test Paint",
        "color": "White",
        "gallons": 100,
        "ship_month": "2026-12",
    }, token)
    assert status == 201, order
    order_id = order["id"]

    def produce(batch):
        return call("POST", "/api/production", {
            "order_id": order_id,
            "batch_number": batch,
            "gallons": 60,
        }, token)[0]

    with ThreadPoolExecutor(max_workers=2) as executor:
        statuses = sorted(executor.map(produce, ("CONCURRENT-A", "CONCURRENT-B")))
    assert statuses == [201, 400], statuses

    with app.closing(app.connect()) as connection:
        produced = float(connection.execute(
            "SELECT COALESCE(SUM(gallons),0) FROM production WHERE order_id=?", (order_id,)
        ).fetchone()[0])
    assert produced == 60

    status, _, _ = call("GET", "/api/dashboard")
    assert status == 200
    status, production, _ = call("POST", "/api/production", {
        "order_id": order_id,
        "batch_number": "CONCURRENT-FINAL",
        "gallons": 40,
    }, token)
    assert status == 201, production
    status, dashboard, _ = call("GET", "/api/dashboard")
    assert status == 200
    assert not any(row["id"] == order_id for row in dashboard["orders"])

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=16) as executor:
        results = list(executor.map(lambda _: call("GET", "/api/dashboard")[0], range(64)))
    elapsed = time.monotonic() - started
    assert all(status == 200 for status in results)
    assert elapsed < 10, elapsed

    print(f"PASS: concurrent write protection, cache invalidation, and 64 parallel dashboard reads ({elapsed:.2f}s)")
finally:
    server.shutdown()
    server.server_close()
    if TEST_DB.exists():
        TEST_DB.unlink()
