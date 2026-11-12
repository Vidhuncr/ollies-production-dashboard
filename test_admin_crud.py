import http.client
import json
import threading
from pathlib import Path
from http.server import ThreadingHTTPServer

import app_sqlite_backup as app


app.USE_SUPABASE = False
app.SUPABASE_DATABASE_URL = ""
app.EMAIL_ENABLED = False
TEST_DB = Path(__file__).with_name("test_admin_crud.db")
if TEST_DB.exists():
    TEST_DB.unlink()
app.DB = TEST_DB
app.initialize()
server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()


def call(method, path, body=None, token=None):
    con = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    headers = {"Accept": "application/json"}
    if token:
        headers["Cookie"] = f"ollies_session={token}"
    raw = None
    if body is not None:
        raw = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    con.request(method, path, raw, headers)
    response = con.getresponse()
    content = response.read()
    content_type = response.getheader("Content-Type", "")
    result = json.loads(content) if content and "application/json" in content_type else content
    status = response.status
    response_headers = dict(response.getheaders())
    con.close()
    return status, result, response_headers


def ok(method, path, body=None, token=None, expected=200):
    status, result, headers = call(method, path, body, token)
    assert status == expected, (method, path, status, result)
    return result, headers


def login(email, password):
    result, headers = ok("POST", "/api/login", {"email": email, "password": password})
    assert result["ok"]
    return headers["Set-Cookie"].split(";", 1)[0].split("=", 1)[1]


try:
    admin = login(app.APP_ADMIN_EMAIL, app.APP_PASSWORD)
    ok("POST", "/api/users", {
        "display_name": "CRUD Manager", "email": "crud.manager@test.local",
        "password": "Manager123!", "role": "manager"
    }, admin, 201)
    manager = login("crud.manager@test.local", "Manager123!")

    admin_data, _ = ok("GET", "/api/admin", token=admin)
    manager_id = next(x["id"] for x in admin_data["users"] if x["email"] == "crud.manager@test.local")
    ok("PUT", f"/api/admin/users/{manager_id}", {
        "display_name": "Updated Manager", "email": "updated.manager@test.local",
        "password": "Updated123!", "role": "manager", "active": True
    }, admin)
    assert call("POST", "/api/login", {"email": "crud.manager@test.local", "password": "Manager123!"})[0] == 401
    manager = login("updated.manager@test.local", "Updated123!")

    ok("POST", "/api/catalog", {"category": "CRUD Paint", "color": "Blue", "unit": "gallons"}, manager, 201)
    ok("POST", "/api/catalog", {"category": "CRUD Spare", "color": "Unused", "unit": "qty"}, manager, 201)
    admin_data, _ = ok("GET", "/api/admin", token=admin)
    spare_id = next(x["id"] for x in admin_data["catalog"] if x["category"] == "CRUD Spare")
    ok("PUT", f"/api/admin/catalog/{spare_id}", {"unit": "gallons"}, manager)
    ok("DELETE", f"/api/admin/catalog/{spare_id}", token=admin)

    ok("POST", "/api/contacts", {"name": "CRUD Recipient", "email": "crud.recipient@test.local"}, manager, 201)
    admin_data, _ = ok("GET", "/api/admin", token=admin)
    contact_id = next(x["id"] for x in admin_data["contacts"] if x["email"] == "crud.recipient@test.local")
    ok("PUT", f"/api/admin/contacts/{contact_id}", {
        "name": "Updated Recipient", "email": "updated.recipient@test.local", "active": True
    }, manager)
    assert call("POST", "/api/contacts", {"name": "Duplicate", "email": "updated.recipient@test.local"}, manager)[0] == 400

    ok("POST", "/api/labor-rates", {
        "labor_name": "CRUD Labor", "hourly_rate": 25, "effective_from": "2026-01-01"
    }, manager, 201)
    costing, _ = ok("GET", "/api/costing", token=admin)
    rate_id = next(x["id"] for x in costing["current_rates"] if x["labor_name"] == "CRUD Labor")

    order, _ = ok("POST", "/api/orders", {
        "s_number": "S90901", "brand": "CRUD Paint", "color": "Blue", "gallons": 6,
        "ship_month": "2026-12", "recipient_ids": [contact_id]
    }, manager, 201)
    admin_data, _ = ok("GET", "/api/admin", token=admin)
    order_row = next(x for x in admin_data["orders"] if x["id"] == order["id"])
    ok("PUT", f"/api/admin/orders/{order['id']}", {
        "s_number": "S90901", "brand": "CRUD Paint", "color": "Blue", "gallons": 7,
        "ship_month": "2027-01", "created_at": order_row["created_at"]
    }, manager)

    ok("POST", "/api/production", {
        "order_id": order["id"], "batch_number": "CRUD-BATCH-1", "gallons": 7,
        "rate_id": rate_id, "labor_hours": 1
    }, manager, 201)
    admin_data, _ = ok("GET", "/api/admin", token=admin)
    batch = next(x for x in admin_data["batches"] if x["batch_number"] == "CRUD-BATCH-1")
    ok("PUT", f"/api/admin/batches/{batch['id']}", {
        "order_id": order["id"], "brand": "CRUD Paint", "color": "Blue",
        "batch_number": "CRUD-BATCH-EDITED", "gallons": 7, "produced_at": batch["produced_at"]
    }, manager)

    ok("POST", "/api/packaging-requirements", {
        "order_id": order["id"], "package_type": "1_gallon_pail", "required_count": 7
    }, manager, 201)
    admin_data, _ = ok("GET", "/api/admin", token=admin)
    requirement = next(x for x in admin_data["packaging_requirements"] if x["order_id"] == order["id"])
    ok("PUT", f"/api/admin/requirements/{requirement['id']}", {"required_count": 6}, manager)
    ok("PUT", f"/api/admin/requirements/{requirement['id']}", {"required_count": 7}, manager)

    ok("POST", "/api/packaging", {
        "source_type": "order", "source_id": order["id"], "package_type": "1_gallon_pail",
        "package_count": 7, "rate_id": rate_id, "labor_hours": 0.5
    }, manager, 201)
    admin_data, _ = ok("GET", "/api/admin", token=admin)
    package = next(x for x in admin_data["packaging"] if x["order_id"] == order["id"])
    ok("PUT", f"/api/admin/packaging/{package['id']}", {
        "package_type": "1_gallon_pail", "package_count": 6, "packaged_at": package["packaged_at"]
    }, manager)
    ok("PUT", f"/api/admin/packaging/{package['id']}", {
        "package_type": "1_gallon_pail", "package_count": 7, "packaged_at": package["packaged_at"]
    }, manager)

    dashboard, _ = ok("GET", "/api/dashboard")
    shipping = next(x for x in dashboard["shipping_inventory"] if x["s_number"] == "S90901")
    ok("POST", "/api/shipments", {"packaging_id": shipping["packaging_id"], "package_count": 2}, manager, 201)
    dashboard, _ = ok("GET", "/api/dashboard")
    shipment = next(x for x in dashboard["shipped"] if x["s_number"] == "S90901")
    ok("PUT", f"/api/admin/shipments/{shipment['id']}", {"package_count": 1}, manager)
    ok("POST", f"/api/shipments/{shipment['id']}/undo", {}, manager)

    ok("DELETE", f"/api/admin/contacts/{contact_id}", token=admin)
    ok("DELETE", f"/api/admin/packaging/{package['id']}", token=admin)
    ok("DELETE", f"/api/admin/requirements/{requirement['id']}", token=admin)
    ok("DELETE", f"/api/admin/batches/{batch['id']}", token=admin)
    costing, _ = ok("GET", "/api/costing", token=admin)
    for entry in [x for x in costing["entries"] if x["order_id"] == order["id"]]:
        ok("PUT", f"/api/admin/labor-entries/{entry['id']}", {
            "order_id": order["id"], "process_name": entry["process_name"], "rate_id": rate_id,
            "hours": float(entry["hours"]) + 0.1, "worked_at": entry["worked_at"], "notes": "CRUD verified"
        }, manager)
        ok("DELETE", f"/api/admin/labor-entries/{entry['id']}", token=admin)
    ok("DELETE", f"/api/admin/orders/{order['id']}", token=admin)

    need, _ = ok("POST", "/api/stock-needs", {
        "brand": "CRUD Paint", "color": "Blue", "amount": 5, "note": "CRUD need"
    }, manager, 201)
    ok("PUT", f"/api/admin/stock-needs/{need['id']}", {
        "brand": "CRUD Paint", "color": "Blue", "amount": 8, "note": "Updated CRUD need"
    }, manager)
    ok("DELETE", f"/api/admin/stock-needs/{need['id']}", token=admin)
    ok("DELETE", f"/api/admin/labor-rates/{rate_id}", token=admin)

    assert call("DELETE", f"/api/admin/users/{manager_id}", token=manager)[0] == 403
    ok("DELETE", f"/api/admin/users/{manager_id}", token=admin)
    assert call("GET", "/api/session", token=manager)[0] == 401

    admin_data, _ = ok("GET", "/api/admin", token=admin)
    assert not any(x["s_number"] == "S90901" for x in admin_data["orders"])
    assert not any(x["batch_number"] == "CRUD-BATCH-EDITED" for x in admin_data["batches"])
    assert not any(x["email"] == "updated.recipient@test.local" for x in admin_data["contacts"])
    print("PASS: every backend add, edit, disable, undo, and delete control completed with role enforcement")
finally:
    server.shutdown()
    server.server_close()
    if TEST_DB.exists():
        TEST_DB.unlink()
