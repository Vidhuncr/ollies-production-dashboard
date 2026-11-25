import http.client
import json
import threading
from pathlib import Path
from http.server import ThreadingHTTPServer

import app_sqlite_backup as app

# The full application test must always use its disposable SQLite database,
# even when a developer has a live Supabase URL in the local .env file.
app.USE_SUPABASE = False
app.SUPABASE_DATABASE_URL = ""
app.EMAIL_ENABLED = False


TEST_DB = Path(__file__).with_name("test_full_app.db")
if TEST_DB.exists():
    TEST_DB.unlink()
app.DB = TEST_DB
app.initialize()
server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()


def call(method, path, body=None, token=None):
    connection = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=10)
    headers = {"Accept": "application/json"}
    if token:
        headers["Cookie"] = f"ollies_session={token}"
    raw = None
    if body is not None:
        raw = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    connection.request(method, path, body=raw, headers=headers)
    response = connection.getresponse()
    content = response.read()
    content_type = response.getheader("Content-Type", "")
    result = json.loads(content) if content and "application/json" in content_type else content
    headers_out = dict(response.getheaders())
    status = response.status
    connection.close()
    return status, result, headers_out


def login(email, password):
    status, payload, headers = call("POST", "/api/login", {"email": email, "password": password})
    assert status == 200, payload
    cookie = headers["Set-Cookie"].split(";", 1)[0]
    return cookie.split("=", 1)[1]


def ok(method, path, body=None, token=None, expected=200):
    status, payload, headers = call(method, path, body, token)
    assert status == expected, (method, path, status, payload)
    return payload, headers


try:
    admin = login(app.APP_ADMIN_EMAIL, app.APP_PASSWORD)
    ok("POST", "/api/users", {"display_name": "Manager", "email": "manager@test.local", "password": "Manager123!", "role": "manager"}, admin, 201)
    ok("POST", "/api/users", {"display_name": "Team", "email": "team@test.local", "password": "TeamUser123!", "role": "team"}, admin, 201)
    manager = login("manager@test.local", "Manager123!")
    team = login("team@test.local", "TeamUser123!")
    # Signed cookies must survive loss of all process-local session cache, as
    # happens when Render sleeps, restarts, or deploys a new instance.
    with app.SESSION_LOCK:
        app.SESSION_USER_CACHE.clear()
        app.REVOKED_SESSIONS.clear()
    session, _ = ok("GET", "/api/session", token=admin)
    assert session["user"]["email"] == app.APP_ADMIN_EMAIL
    assert call("GET", "/api/session", token=admin + "tampered")[0] == 401
    entry_page = call("GET", "/entry", token=team)
    assert entry_page[0] == 200
    assert b"What is this manufacturing for?" in entry_page[1]
    assert b"Do not select an individual S#" in entry_page[1]
    assert b"What is this tinting for?" in entry_page[1]
    for route in ("/", "/production", "/inventory", "/shipping", "/costing", "/reporting", "/admin"):
        team_page = call("GET", route, token=team)
        assert team_page[0] == 302 and team_page[2].get("Location") == "/entry", route
    assert call("GET", "/", token=manager)[0] == 200

    ok("POST", "/api/contacts", {"name": "Shipping Lead", "email": "shipping@test.local"}, team, 201)
    admin_data, _ = ok("GET", "/api/admin", token=admin)
    contact_id = next(x["id"] for x in admin_data["contacts"] if x["email"] == "shipping@test.local")
    ok("PUT", f"/api/admin/contacts/{contact_id}", {
        "name": "Shipping Manager", "email": "shipping-manager@test.local", "active": False
    }, manager)
    contacts, _ = ok("GET", "/api/contacts", token=admin)
    assert not any(x["id"] == contact_id for x in contacts)
    ok("PUT", f"/api/admin/contacts/{contact_id}", {
        "name": "Shipping Manager", "email": "shipping-manager@test.local", "active": True
    }, manager)
    contacts, _ = ok("GET", "/api/contacts", token=admin)
    assert any(x["id"] == contact_id for x in contacts)
    assert call("DELETE", f"/api/admin/contacts/{contact_id}", token=manager)[0] == 403
    ok("DELETE", f"/api/admin/contacts/{contact_id}", token=admin)
    admin_data, _ = ok("GET", "/api/admin", token=admin)
    assert not any(x["id"] == contact_id for x in admin_data["contacts"])

    ok("POST", "/api/catalog", {"category": "Paint", "color": "Blue", "unit": "gallons"}, team, 201)
    ok("POST", "/api/catalog", {"category": "Paint", "color": "Red", "unit": "gallons"}, team, 201)
    ok("POST", "/api/catalog", {"category": "Parts", "color": "Red", "unit": "qty"}, team, 201)
    ok("POST", "/api/catalog", {"category": "Bulk Test", "color": "Green", "unit": "gallons"}, team, 201)
    ok("POST", "/api/catalog", {"category": "Unused", "color": "Test", "unit": "qty"}, team, 201)
    ok("POST", "/api/contacts", {"name": "Order Watcher", "email": "orders@test.local"}, team, 201)
    order_contact = next(x for x in ok("GET", "/api/contacts", token=admin)[0] if x["email"] == "orders@test.local")
    assert call("POST", "/api/orders", {"s_number": "S99999", "brand": "Paint", "color": "Blue", "gallons": 1}, team)[0] == 400
    admin_data, _ = ok("GET", "/api/admin", token=admin)
    unused_id = next(x["id"] for x in admin_data["catalog"] if x["category"] == "Unused")
    assert call("PUT", f"/api/admin/catalog/{unused_id}", {"unit": "qty"}, team)[0] == 403
    assert call("DELETE", f"/api/admin/catalog/{unused_id}", token=manager)[0] == 403
    ok("DELETE", f"/api/admin/catalog/{unused_id}", token=admin)

    ok("POST", "/api/labor-rates", {"labor_name": "General", "hourly_rate": 20, "effective_from": "2026-01-01"}, manager, 201)
    costing, _ = ok("GET", "/api/costing", token=admin)
    rate_id = costing["current_rates"][0]["id"]

    # New manufacturing flow is category-level: a bulk batch reserves combined
    # order demand, incubates, and is unavailable to packaging until QC releases it.
    bulk_order_result, _ = ok("POST", "/api/orders", {"s_number": "S10998", "brand": "Bulk Test", "color": "Green",
        "gallons": 10, "ship_month": "2027-04"}, team, 201)
    bulk_order_id = bulk_order_result["id"]
    bulk_result, _ = ok("POST", "/api/bulk-production", {"purpose": "orders", "brand": "Bulk Test", "color": "Green",
        "batch_number": "BULK-QC-1", "gallons": 10, "incubation_hours": 1, "rate_id": rate_id, "labor_hours": 0.5}, team, 201)
    dashboard, _ = ok("GET", "/api/dashboard", token=manager)
    pending = next(x for x in dashboard["bulk_demand"] if x["brand"] == "Bulk Test")
    assert pending["remaining"] == 10 and pending["available_to_start"] == 0
    assert not any(x.get("batch_number") == "BULK-QC-1" for x in dashboard["packageable"])
    assert call("POST", "/api/bulk-production", {"purpose": "orders", "brand": "Bulk Test", "color": "Green",
        "batch_number": "BULK-TOO-MUCH", "gallons": 1, "incubation_hours": 0, "rate_id": rate_id, "labor_hours": 0.1}, team)[0] == 400
    assert call("POST", f"/api/bulk-production/{bulk_result['id']}/quality", {"decision": "ready"}, team)[0] == 403
    assert call("POST", f"/api/bulk-production/{bulk_result['id']}/quality", {"decision": "ready"}, manager)[0] == 400
    with app.closing(app.connect()) as con:
        con.execute("UPDATE production SET incubation_due_at='2000-01-01T00:00:00' WHERE id=?", (bulk_result["id"],)); con.commit()
    ok("POST", f"/api/bulk-production/{bulk_result['id']}/quality", {"decision": "ready", "notes": "Passed QC"}, manager)
    dashboard, _ = ok("GET", "/api/dashboard", token=manager)
    assert next(x for x in dashboard["bulk_demand"] if x["brand"] == "Bulk Test")["remaining"] == 0
    bulk_source = next(x for x in dashboard["packageable"] if x.get("batch_number") == "BULK-QC-1")
    ok("POST", "/api/packaging-requirements", {"order_id": bulk_order_id, "package_type": "5_gallon_pail", "required_count": 2}, team, 201)
    ok("POST", "/api/packaging", {"source_type": "stock", "source_id": bulk_source["source_id"], "package_type": "5_gallon_pail",
        "package_count": 2, "rate_id": rate_id, "labor_hours": 0.2}, team, 201)
    dashboard, _ = ok("GET", "/api/dashboard", token=manager)
    packaged_bulk = next(x for x in dashboard["stock_inventory"] if x["batch_number"] == "BULK-QC-1")
    ok("POST", "/api/inventory/assign", {"stock_packaging_id": packaged_bulk["id"], "order_id": bulk_order_id, "package_count": 2}, manager, 201)
    dashboard, _ = ok("GET", "/api/dashboard", token=manager)
    assert next(x for x in dashboard["incubation_batches"] if x["id"] == bulk_result["id"])["bulk_allocated"] == 10
    assert next(x for x in dashboard["total"] if x["brand"] == "Bulk Test")["gallons"] == 10
    assert call("POST", f"/api/bulk-production/{bulk_result['id']}/quality", {"decision": "trash"}, manager)[0] == 400

    trash_order, _ = ok("POST", "/api/orders", {"s_number": "S10997", "brand": "Bulk Test", "color": "Green",
        "gallons": 5, "ship_month": "2027-04"}, team, 201)
    trash_batch, _ = ok("POST", "/api/bulk-production", {"purpose": "orders", "brand": "Bulk Test", "color": "Green",
        "batch_number": "BULK-TRASH-1", "gallons": 5, "incubation_hours": 0, "rate_id": rate_id, "labor_hours": 0.2}, team, 201)
    ok("POST", f"/api/bulk-production/{trash_batch['id']}/quality", {"decision": "trash", "notes": "Failed test",
       "rate_id":rate_id,"labor_hours":0.25}, manager)
    dashboard, _ = ok("GET", "/api/dashboard", token=manager)
    assert next(x for x in dashboard["bulk_demand"] if x["brand"] == "Bulk Test")["remaining"] == 5
    assert not any(x.get("batch_number") == "BULK-TRASH-1" for x in dashboard["packageable"])
    rework_source, _ = ok("POST", "/api/bulk-production", {"purpose": "orders", "brand": "Bulk Test", "color": "Green",
        "batch_number": "BULK-REWORK-1", "gallons": 5, "incubation_hours": 0, "rate_id": rate_id, "labor_hours": 0.2}, team, 201)
    ok("POST", f"/api/bulk-production/{rework_source['id']}/quality", {"decision": "rework", "notes": "Adjust formula"}, manager)
    reworked, _ = ok("POST", "/api/bulk-production", {"purpose": "orders", "brand": "Bulk Test", "color": "Green",
        "batch_number": "BULK-REWORK-2", "gallons": 5, "incubation_hours": 0, "rate_id": rate_id, "labor_hours": 0.2,
        "rework_of_id": rework_source["id"]}, team, 201)
    ok("POST", f"/api/bulk-production/{reworked['id']}/quality", {"decision": "ready", "notes": "Rework passed"}, manager)
    dashboard, _ = ok("GET", "/api/dashboard", token=manager)
    assert any(x.get("batch_number") == "BULK-REWORK-2" for x in dashboard["packageable"])
    costs = {x["batch_number"]:x for x in ok("GET", "/api/costing", token=manager)[0]["stock_summary"]}
    assert costs["BULK-TRASH-1"]["quality_status"] == "trash" and costs["BULK-TRASH-1"]["labor_cost"] == 9
    assert costs["BULK-REWORK-1"]["labor_cost"] == 4 and costs["BULK-REWORK-2"]["labor_cost"] == 4
    assert costs["BULK-REWORK-2"]["rework_of_batch"] == "BULK-REWORK-1"
    assert call("POST", f"/api/bulk-production/{rework_source['id']}/quality", {"decision":"ready"}, manager)[0] == 400

    ok("POST", "/api/stock-needs", {"brand": "Bulk Test", "color": "Green", "amount": 3, "note": "QC stock test"}, team, 201)
    stock_qc, _ = ok("POST", "/api/bulk-production", {"purpose": "stock", "brand": "Bulk Test", "color": "Green",
        "batch_number": "BULK-STOCK-QC", "gallons": 3, "incubation_hours": 0, "rate_id": rate_id, "labor_hours": 0.1}, team, 201)
    dashboard, _ = ok("GET", "/api/dashboard", token=manager)
    assert next(x for x in dashboard["stock_needs"] if x["note"] == "QC stock test")["remaining"] == 3
    ok("POST", f"/api/bulk-production/{stock_qc['id']}/quality", {"decision": "ready"}, manager)
    dashboard, _ = ok("GET", "/api/dashboard", token=manager)
    assert next(x for x in dashboard["stock_needs"] if x["note"] == "QC stock test")["remaining"] == 0
    ok("POST", f"/api/bulk-production/{stock_qc['id']}/quality", {"decision": "trash", "notes": "Damaged"}, manager)
    dashboard, _ = ok("GET", "/api/dashboard", token=manager)
    assert next(x for x in dashboard["stock_needs"] if x["note"] == "QC stock test")["remaining"] == 3

    legacy_order = ok("POST", "/api/orders", {"s_number":"S10996","brand":"Bulk Test","color":"Green",
        "gallons":8,"ship_month":"2027-04"}, team, 201)[0]
    ok("POST", "/api/production", {"order_id":legacy_order["id"],"batch_number":"LEGACY-QC-1",
       "gallons":2,"rate_id":rate_id,"labor_hours":.5}, team, 201)
    legacy = ok("GET", "/api/bulk-production/legacy?q=LEGACY-QC", token=manager)[0][0]
    assert legacy["s_number"] == "S10996"
    assert call("POST", f"/api/bulk-production/{legacy['id']}/incubate", {"incubation_hours":0}, team)[0] == 403
    ok("POST", f"/api/bulk-production/{legacy['id']}/incubate", {"incubation_hours":0}, manager)
    def bulk_need():
        return next(x for x in ok("GET", "/api/dashboard", token=manager)[0]["bulk_demand"] if x["brand"]=="Bulk Test")["remaining"]
    assert bulk_need() == 8
    ok("POST", f"/api/bulk-production/{legacy['id']}/quality", {"decision":"ready"}, manager)
    assert bulk_need() == 6
    ok("POST", f"/api/bulk-production/{legacy['id']}/quality", {"decision":"incubating","incubation_hours":0}, manager)
    assert bulk_need() == 8
    assert next(x for x in ok("GET","/api/costing",token=manager)[0]["stock_summary"] if x["batch_number"]=="LEGACY-QC-1")["labor_cost"] == 10

    multi_gal, _ = ok("POST", "/api/orders", {"s_number": "S10999", "po_number": "PO-TEST-10999", "brand": "Paint", "color": "Blue", "gallons": 2, "ship_month": "2027-04", "recipient_ids": [order_contact["id"]]}, team, 201)
    assert multi_gal["po_number"] == "PO-TEST-10999" and "disabled" in multi_gal["email_status"].lower()
    multi_qty, _ = ok("POST", "/api/orders", {"s_number": "S10999", "brand": "Parts", "color": "Red", "gallons": 30, "ship_month": "2027-04"}, team, 201)
    multi_detail, _ = ok("GET", "/api/s-numbers/S10999/detail", token=admin)
    assert multi_detail["status"] == "Not Started" and len(multi_detail["orders"]) == 2
    assert multi_detail["po_numbers"] == ["PO-TEST-10999"] and multi_detail["orders"][0]["po_number"] == "PO-TEST-10999"
    assert multi_detail["totals"]["ordered_gallons"] == 2 and multi_detail["totals"]["ordered_qty"] == 30
    assert {x["brand"] for x in multi_detail["orders"]} == {"Paint", "Parts"}
    ok("DELETE", f"/api/admin/orders/{multi_gal['id']}", token=admin)
    ok("DELETE", f"/api/admin/orders/{multi_qty['id']}", token=admin)

    ok("POST", "/api/orders", {"s_number": "S10001", "brand": "Paint", "color": "Blue", "gallons": 100, "ship_month": "2026-11"}, team, 201)
    dashboard, _ = ok("GET", "/api/dashboard")
    gallon_order = next(x for x in dashboard["orders"] if x["s_number"] == "S10001")
    assert [x["key"] for x in dashboard["performance"]["periods"]] == ["today", "week", "month", "lifetime"]
    assert len(dashboard["performance"]["daily_history"]) == 14
    assert gallon_order["ship_month"] == "2026-11"
    assert next(x for x in dashboard["order_schedule"] if x["s_number"] == "S10001")["status"] == "Not Started"
    production, _ = ok("POST", "/api/production", {"order_id": gallon_order["id"], "batch_number": "G-BATCH-1", "gallons": 100, "rate_id": rate_id, "labor_hours": 2}, team, 201)
    assert production["labor_cost"] == 40
    ok("POST", "/api/packaging-requirements", {"order_id": gallon_order["id"], "package_type": "5_gallon_pail", "required_count": 20}, team, 201)
    packaged, _ = ok("POST", "/api/packaging", {"source_type": "order", "source_id": gallon_order["id"], "package_type": "5_gallon_pail", "package_count": 20, "rate_id": rate_id, "labor_hours": 1}, team, 201)
    assert packaged["labor_cost"] == 20

    dashboard, _ = ok("GET", "/api/dashboard")
    shipping_row = next(x for x in dashboard["shipping_inventory"] if x["s_number"] == "S10001")
    ok("POST", "/api/shipments", {"packaging_id": shipping_row["packaging_id"], "package_count": 8}, manager, 201)
    dashboard, _ = ok("GET", "/api/dashboard")
    assert next(x for x in dashboard["shipping_inventory"] if x["s_number"] == "S10001")["available_count"] == 12
    assert next(x for x in dashboard["inventory"] if x["s_number"] == "S10001")["package_count"] == 12
    report, _ = ok("GET", "/api/reporting", token=admin)
    gallon_report = next(x for x in report["product_lines"] if x["s_number"] == "S10001")
    package_report = next(x for x in report["package_rows"] if x["s_number"] == "S10001")
    assert (gallon_report["required_amount"], gallon_report["shipped_amount"], gallon_report["inventory_amount"]) == (100, 40, 60)
    assert (package_report["required_count"], package_report["shipped_count"], package_report["balance_count"]) == (20, 8, 12)
    shipment_id = dashboard["shipped"][0]["id"]
    ok("POST", f"/api/shipments/{shipment_id}/undo", {}, manager)
    dashboard, _ = ok("GET", "/api/dashboard")
    assert next(x for x in dashboard["shipping_inventory"] if x["s_number"] == "S10001")["available_count"] == 20
    ok("POST", "/api/shipments", {"packaging_id": shipping_row["packaging_id"], "package_count": 8}, manager, 201)
    dashboard, _ = ok("GET", "/api/dashboard")
    shipment_id = dashboard["shipped"][0]["id"]
    ok("PUT", f"/api/admin/shipments/{shipment_id}", {"package_count": 10}, manager)
    dashboard, _ = ok("GET", "/api/dashboard")
    assert next(x for x in dashboard["shipping_inventory"] if x["s_number"] == "S10001")["available_count"] == 10
    assert call("POST", "/api/s-numbers/S10001/ship", {}, manager)[0] == 400
    ok("POST", "/api/shipments", {"packaging_id": shipping_row["packaging_id"], "package_count": 10}, manager, 201)
    ok("POST", "/api/s-numbers/S10001/ship", {}, manager)

    ok("POST", "/api/orders", {"s_number": "S10002", "brand": "Parts", "color": "Red", "gallons": 50, "ship_month": "2026-12"}, team, 201)
    dashboard, _ = ok("GET", "/api/dashboard")
    qty_order = next(x for x in dashboard["orders"] if x["s_number"] == "S10002")
    ok("POST", "/api/production", {"order_id": qty_order["id"], "batch_number": "Q-BATCH-1", "gallons": 50, "rate_id": rate_id, "labor_hours": 1.5}, team, 201)
    ok("POST", "/api/packaging-requirements", {"order_id": qty_order["id"], "package_type": "case_pack", "package_size": 10, "required_count": 5}, team, 201)
    ok("POST", "/api/packaging", {"source_type": "order", "source_id": qty_order["id"], "package_type": "case_pack", "package_size": 10, "package_count": 5, "rate_id": rate_id, "labor_hours": 0.5}, team, 201)
    dashboard, _ = ok("GET", "/api/dashboard")
    assert next(x for x in dashboard["s_numbers"] if x["s_number"] == "S10002")["ready_at"]
    assert call("GET", "/api/s-numbers/S10002/detail")[0] == 401
    detail, _ = ok("GET", "/api/s-numbers/S10002/detail", token=admin)
    assert detail["status"] == "Ready" and detail["totals"]["ordered_qty"] == 50
    assert detail["totals"]["produced_qty"] == 50 and detail["totals"]["remaining_qty"] == 0
    assert detail["orders"][0]["brand"] == "Parts" and detail["orders"][0]["color"] == "Red"
    assert detail["requirements"][0]["package_type"] == "case_pack"
    assert detail["requirements"][0]["package_size"] == 10 and detail["requirements"][0]["required_count"] == 5
    assert detail["requirements"][0]["completed_count"] == 5
    assert detail["packages"][0]["package_count"] == 5 and detail["packages"][0]["available_count"] == 5
    assert detail["batches"][0]["batch_number"] == "Q-BATCH-1" and detail["labor"]["cost"] > 0
    assert call("GET", "/api/s-numbers/S00000/detail", token=admin)[0] == 404

    need_result, _ = ok("POST", "/api/stock-needs", {"brand": "Paint", "color": "Blue", "amount": 15, "note": "Order pending S#"}, team, 201)
    need_id = need_result["id"]
    dashboard, _ = ok("GET", "/api/dashboard")
    assert next(x for x in dashboard["stock_needs"] if x["id"] == need_id)["remaining"] == 15
    stock_result, _ = ok("POST", "/api/stock-production", {"brand": "Paint", "color": "Blue", "batch_number": "STOCK-1", "gallons": 10, "rate_id": rate_id, "labor_hours": 1}, team, 201)
    assert stock_result["need_fulfilled"] == 10
    dashboard, _ = ok("GET", "/api/dashboard")
    assert next(x for x in dashboard["stock_needs"] if x["id"] == need_id)["remaining"] == 5
    dashboard, _ = ok("GET", "/api/dashboard")
    stock_source = next(x for x in dashboard["packageable"] if x["source_type"] == "stock" and x["batch_number"] == "STOCK-1")
    ok("POST", "/api/packaging", {"source_type": "stock", "source_id": stock_source["source_id"], "package_type": "5_gallon_pail", "package_count": 2, "rate_id": rate_id, "labor_hours": 0.5}, team, 201)
    ok("POST", "/api/orders", {"s_number": "S10003", "brand": "Paint", "color": "Blue", "gallons": 5, "ship_month": "2026-12"}, team, 201)
    dashboard, _ = ok("GET", "/api/dashboard")
    stock_package = next(x for x in dashboard["stock_inventory"] if x["batch_number"] == "STOCK-1")
    stock_order = next(x for x in dashboard["orders"] if x["s_number"] == "S10003")
    ok("POST", "/api/inventory/assign", {"stock_packaging_id": stock_package["id"], "order_id": stock_order["id"], "package_count": 1}, manager, 201)
    dashboard, _ = ok("GET", "/api/dashboard")
    assert next(x for x in dashboard["stock_inventory"] if x["batch_number"] == "STOCK-1")["package_count"] == 1
    reassigned_need = next(x for x in dashboard["stock_needs"] if x["id"] == need_id)
    assert reassigned_need["fulfilled_amount"] == 5 and reassigned_need["remaining"] == 10
    ok("PUT", f"/api/admin/stock-needs/{need_id}", {"brand": "Paint", "color": "Blue", "amount": 12, "note": "Revised pending order"}, manager)
    dashboard, _ = ok("GET", "/api/dashboard")
    assert next(x for x in dashboard["stock_needs"] if x["id"] == need_id)["remaining"] == 7
    assert call("DELETE", f"/api/admin/stock-needs/{need_id}", token=admin)[0] == 400
    report, _ = ok("GET", "/api/reporting", token=admin)
    assert next(x for x in report["product_lines"] if x["s_number"] == "S10003")["produced_amount"] == 5

    on_hand, _ = ok("POST", "/api/existing-production", {"brand": "Paint", "color": "Blue", "batch_number": "ON-HAND-1", "gallons": 5, "produced_at": "2026-11-05T10:00:00", "rate_id": rate_id, "labor_hours": 0.25}, team, 201)
    assert on_hand["need_fulfilled"] == 5
    dashboard, _ = ok("GET", "/api/dashboard")
    assert next(x for x in dashboard["stock_needs"] if x["id"] == need_id)["remaining"] == 2
    ok("POST", "/api/stock-production", {"brand": "Paint", "color": "Blue", "batch_number": "BASE-STOCK-1", "gallons": 5, "rate_id": rate_id, "labor_hours": 0.2}, team, 201)
    dashboard, _ = ok("GET", "/api/dashboard")
    base_source = next(x for x in dashboard["packageable"] if x["source_type"] == "stock" and x["batch_number"] == "BASE-STOCK-1")
    ok("POST", "/api/packaging", {"source_type": "stock", "source_id": base_source["source_id"], "package_type": "5_gallon_pail", "package_count": 1, "rate_id": rate_id, "labor_hours": 0.1}, team, 201)
    ok("POST", "/api/orders", {"s_number": "S10004", "brand": "Paint", "color": "Red", "gallons": 5, "ship_month": "2027-01"}, team, 201)
    dashboard, _ = ok("GET", "/api/dashboard")
    tint_order = next(x for x in dashboard["orders"] if x["s_number"] == "S10004")
    ok("POST", "/api/packaging-requirements", {"order_id": tint_order["id"], "package_type": "5_gallon_pail", "required_count": 1}, team, 201)
    dashboard, _ = ok("GET", "/api/dashboard")
    base_package = next(x for x in dashboard["stock_inventory"] if x["batch_number"] == "BASE-STOCK-1")
    reworked, _ = ok("POST", "/api/inventory/rework", {"stock_packaging_id": base_package["id"], "order_id": tint_order["id"],
      "batch_number": "TINT-RED-1", "package_count": 1, "rate_id": rate_id, "labor_hours": 0.3, "labor_notes": "Red tint"}, team, 201)
    assert reworked["converted_amount"] == 5 and reworked["labor_cost"] == 6
    dashboard, _ = ok("GET", "/api/dashboard")
    assert not any(x["batch_number"] == "BASE-STOCK-1" for x in dashboard["stock_inventory"])
    assert next(x for x in dashboard["s_numbers"] if x["s_number"] == "S10004")["ready_at"]
    tint_inventory = next(x for x in dashboard["shipping_inventory"] if x["s_number"] == "S10004")
    ok("POST", "/api/shipments", {"packaging_id": tint_inventory["packaging_id"], "package_count": 1}, manager, 201)
    ok("POST", "/api/s-numbers/S10004/ship", {}, manager)

    ok("POST", "/api/orders", {"s_number": "S10005", "brand": "Paint", "color": "Red", "gallons": 5, "ship_month": "2027-01"}, team, 201)
    dashboard, _ = ok("GET", "/api/dashboard")
    feed_order = next(x for x in dashboard["orders"] if x["s_number"] == "S10005")
    ok("POST", "/api/packaging-requirements", {"order_id": feed_order["id"], "package_type": "5_gallon_pail", "required_count": 1}, team, 201)
    dashboard, _ = ok("GET", "/api/dashboard")
    feed_requirement = next(x for x in dashboard["packaging_requirements"] if x["order_id"] == feed_order["id"])
    feed_result, _ = ok("POST", "/api/feed-rework", {
        "target_type": "order", "requirement_id": feed_requirement["id"], "feed_lot": "SUPPLIED-FEED-01",
        "batch_number": "FEED-TINT-RED-1", "package_count": 1, "rate_id": rate_id,
        "labor_hours": 0.25, "labor_notes": "Customer-supplied pail"
    }, team, 201)
    assert feed_result["converted_amount"] == 5 and feed_result["labor_cost"] == 5
    dashboard, _ = ok("GET", "/api/dashboard")
    assert next(x for x in dashboard["s_numbers"] if x["s_number"] == "S10005")["ready_at"]
    feed_inventory = next(x for x in dashboard["shipping_inventory"] if x["s_number"] == "S10005")
    ok("POST", "/api/shipments", {"packaging_id": feed_inventory["packaging_id"], "package_count": 1}, manager, 201)
    ok("POST", "/api/s-numbers/S10005/ship", {}, manager)

    no_s_order, _ = ok("POST", "/api/orders", {"s_number": "", "brand": "Paint", "color": "Red", "gallons": 3, "ship_month": "2027-02"}, team, 201)
    assert no_s_order["s_number"].startswith("N") and no_s_order["has_s_number"] is False
    dashboard, _ = ok("GET", "/api/dashboard")
    no_s_line = next(x for x in dashboard["orders"] if x["id"] == no_s_order["id"])
    ok("POST", "/api/packaging-requirements", {"order_id": no_s_line["id"], "package_type": "1_gallon_pail", "required_count": 3}, team, 201)
    no_s_made, _ = ok("POST", "/api/production", {"order_id": no_s_line["id"], "batch_number": "NO-S-MADE-1",
        "gallons": 1, "rate_id": rate_id, "labor_hours": 0.1}, team, 201)
    assert no_s_made["remaining"] == 2
    ok("POST", "/api/packaging", {"source_type": "order", "source_id": no_s_line["id"], "package_type": "1_gallon_pail",
        "package_count": 1, "rate_id": rate_id, "labor_hours": 0.1}, team, 201)
    dashboard, _ = ok("GET", "/api/dashboard")
    no_s_requirement = next(x for x in dashboard["packaging_requirements"] if x["order_id"] == no_s_line["id"])
    no_s_feed, _ = ok("POST", "/api/feed-rework", {
        "target_type": "order", "requirement_id": no_s_requirement["id"], "feed_lot": "SUPPLIED-NO-S",
        "batch_number": "FEED-NO-S-1", "package_count": 2, "rate_id": rate_id, "labor_hours": 0.1
    }, team, 201)
    assert no_s_feed["converted_amount"] == 2 and no_s_feed["remaining"] == 0
    dashboard, _ = ok("GET", "/api/dashboard")
    no_s_inventory = [x for x in dashboard["shipping_inventory"] if x["s_number"] == no_s_order["s_number"]]
    assert sum(x["available_count"] for x in no_s_inventory) == 3
    for package in no_s_inventory:
        ok("POST", "/api/shipments", {"packaging_id": package["packaging_id"], "package_count": package["available_count"]}, manager, 201)
    ok("POST", f"/api/s-numbers/{no_s_order['s_number']}/ship", {}, manager)

    ok("POST", "/api/orders", {"s_number": "S10006", "brand": "Paint", "color": "Blue", "gallons": 6, "ship_month": "2027-02"}, team, 201)
    dashboard, _ = ok("GET", "/api/dashboard")
    mixed_order = next(x for x in dashboard["orders"] if x["s_number"] == "S10006")
    first_mix, _ = ok("POST", "/api/production", {"order_id": mixed_order["id"], "batch_number": "MIX-MADE-1",
        "gallons": 2, "rate_id": rate_id, "labor_hours": 0.2}, team, 201)
    assert first_mix["remaining"] == 4
    ok("POST", "/api/packaging-requirements", {"order_id": mixed_order["id"], "package_type": "1_gallon_pail", "required_count": 6}, team, 201)
    dashboard, _ = ok("GET", "/api/dashboard")
    mixed_requirement = next(x for x in dashboard["packaging_requirements"] if x["order_id"] == mixed_order["id"])
    mixed_tint, _ = ok("POST", "/api/feed-rework", {"target_type": "order", "requirement_id": mixed_requirement["id"],
        "feed_lot": "MIX-FEED-1", "batch_number": "MIX-TINT-1", "package_count": 2,
        "rate_id": rate_id, "labor_hours": 0.2}, team, 201)
    assert mixed_tint["remaining"] == 2
    excess_production = call("POST", "/api/production", {"order_id": mixed_order["id"], "batch_number": "TOO-MUCH",
        "gallons": 3, "rate_id": rate_id, "labor_hours": 0.1}, team)
    assert excess_production[0] == 400 and "cannot enter more" in excess_production[1]["error"]
    excess_tint = call("POST", "/api/feed-rework", {"target_type": "order", "requirement_id": mixed_requirement["id"],
        "feed_lot": "TOO-MUCH-FEED", "batch_number": "TOO-MUCH-TINT", "package_count": 3,
        "rate_id": rate_id, "labor_hours": 0.1}, team)
    assert excess_tint[0] == 400 and "needs only 2.00" in excess_tint[1]["error"]
    final_mix, _ = ok("POST", "/api/production", {"order_id": mixed_order["id"], "batch_number": "MIX-MADE-2",
        "gallons": 2, "rate_id": rate_id, "labor_hours": 0.2}, team, 201)
    assert final_mix["remaining"] == 0
    ok("POST", "/api/packaging", {"source_type": "order", "source_id": mixed_order["id"], "package_type": "1_gallon_pail",
        "package_count": 4, "rate_id": rate_id, "labor_hours": 0.2}, team, 201)
    dashboard, _ = ok("GET", "/api/dashboard")
    assert next(x for x in dashboard["s_numbers"] if x["s_number"] == "S10006")["ready_at"]

    red_need, _ = ok("POST", "/api/stock-needs", {"brand": "Paint", "color": "Red", "amount": 1, "note": "S# pending"}, team, 201)
    stock_feed, _ = ok("POST", "/api/feed-rework", {
        "target_type": "stock", "brand": "Paint", "color": "Red", "package_type": "1_gallon_pail",
        "feed_lot": "SUPPLIED-FEED-02", "batch_number": "FEED-STOCK-RED-1", "package_count": 1,
        "rate_id": rate_id, "labor_hours": 0.1
    }, team, 201)
    assert stock_feed["converted_amount"] == 1 and stock_feed["need_fulfilled"] == 1
    dashboard, _ = ok("GET", "/api/dashboard")
    assert next(x for x in dashboard["stock_needs"] if x["id"] == red_need["id"])["remaining"] == 0
    assert next(x for x in dashboard["stock_inventory"] if x["batch_number"] == "FEED-STOCK-RED-1")["package_count"] == 1
    disposable_need, _ = ok("POST", "/api/stock-needs", {"brand": "Parts", "color": "Red", "amount": 7, "note": "Temporary"}, team, 201)
    ok("DELETE", f"/api/admin/stock-needs/{disposable_need['id']}", token=admin)
    ok("POST", "/api/stock-production", {
        "brand": "Paint", "color": "Blue", "batch_number": "DELETE-STOCK-1",
        "gallons": 1, "rate_id": rate_id, "labor_hours": 0.1
    }, team, 201)
    dashboard, _ = ok("GET", "/api/dashboard")
    disposable_source = next(x for x in dashboard["packageable"] if x["batch_number"] == "DELETE-STOCK-1")
    ok("POST", "/api/packaging", {
        "source_type": "stock", "source_id": disposable_source["source_id"],
        "package_type": "1_gallon_pail", "package_count": 1,
        "rate_id": rate_id, "labor_hours": 0.1
    }, team, 201)
    ok("DELETE", f"/api/admin/batches/{disposable_source['source_id']}", token=admin)
    dashboard, _ = ok("GET", "/api/dashboard")
    assert not any(x["batch_number"] == "DELETE-STOCK-1" for x in dashboard["stock_inventory"])
    assert not any(x["batch_number"] == "DELETE-STOCK-1" for x in dashboard["packageable"])
    assert next(x for x in dashboard["stock_needs"] if x["id"] == need_id)["remaining"] == 2
    ok("POST", "/api/labor-entries", {"order_id": qty_order["id"], "process_name": "Quality Control", "rate_id": rate_id, "hours": 0.25, "worked_at": "2026-11-05T10:00:00", "notes": "Inspection"}, team, 201)
    costing, _ = ok("GET", "/api/costing", token=admin)
    assert all(isinstance(costing[key], list) for key in ("rates", "entries", "summary", "stock_summary", "s_summary"))
    assert next(x for x in costing["s_summary"] if x["s_number"] == "S10002")["labor_cost"] > 0
    assert any(x["batch_number"] == "ON-HAND-1" for x in costing["stock_summary"])
    assert any(x["process_name"] == "Tint / Relabel / New Lid" and x["s_number"] == "S10004" for x in costing["entries"])
    assert any(x["process_name"] == "Packaged Feed Tint / Relabel / New Lid" and x["s_number"] == "S10005" for x in costing["entries"])
    assert any(x["process_name"] == "Packaged Feed Tint / Relabel / New Lid" and x["s_number"] == "STOCK" for x in costing["entries"])

    admin_data, _ = ok("GET", "/api/admin", token=admin)
    stock_order_admin = next(x for x in admin_data["orders"] if x["s_number"] == "S10003")
    ok("PUT", f"/api/admin/orders/{stock_order_admin['id']}", {"s_number": "S10003", "po_number": "PO-EDIT-10003", "brand": "Paint", "color": "Blue", "gallons": 10, "ship_month": "2027-03", "created_at": stock_order_admin["created_at"]}, manager)
    admin_data, _ = ok("GET", "/api/admin", token=admin)
    assert next(x for x in admin_data["orders"] if x["id"] == stock_order_admin["id"])["ship_month"] == "2027-03"
    assert next(x for x in admin_data["orders"] if x["id"] == stock_order_admin["id"])["po_number"] == "PO-EDIT-10003"
    assert call("DELETE", f"/api/admin/orders/{stock_order_admin['id']}", token=manager)[0] == 403

    assert call("POST", "/api/targets", {"daily_gallons": 4, "daily_qty": 6}, manager)[0] == 403
    targets, _ = ok("POST", "/api/targets", {"daily_gallons": 4, "daily_qty": 6}, admin)
    assert targets["daily_gallons"] == 4 and targets["daily_qty"] == 6
    dashboard, _ = ok("GET", "/api/dashboard")
    assert dashboard["targets"]["daily_gallons"] == 4 and dashboard["targets"]["daily_qty"] == 6
    performance_targets = {x["key"]: x["target"] for x in dashboard["performance"]["periods"]}
    assert performance_targets["today"]["gallons"] == 4 and performance_targets["week"]["gallons"] == 28
    assert performance_targets["month"]["qty"] == 180 and performance_targets["lifetime"]["gallons"] == 0
    target_day = app.date.today().isoformat()
    assert call("POST", "/api/daily-targets", {"target_date": target_day, "scope": "combined", "unit": "gallons", "amount": 10}, team)[0] == 403
    ok("POST", "/api/daily-targets", {"target_date": target_day, "scope": "combined", "unit": "gallons", "amount": 10}, manager)
    ok("POST", "/api/daily-targets", {"target_date": target_day, "scope": "product", "brand": "Paint", "color": "Red", "amount": 3}, manager)
    ok("POST", "/api/daily-targets", {"target_date": target_day, "scope": "product", "brand": "Parts", "color": "Red", "amount": 20}, admin)
    dashboard, _ = ok("GET", "/api/dashboard")
    today_period = next(x for x in dashboard["performance"]["periods"] if x["key"] == "today")
    assert today_period["target"]["gallons"] == 10 and today_period["target"]["qty"] == 20
    assert any(not x["brand"] and x["unit"] == "gallons" and x["amount"] == 10 for x in dashboard["performance"]["target_breakdown"])
    assert any(x["brand"] == "Parts" and x["unit"] == "qty" and x["amount"] == 20 for x in dashboard["performance"]["target_breakdown"])
    tv_fast, _ = ok("GET", "/api/tv-dashboard")
    summary_fast, _ = ok("GET", "/api/dashboard-summary", token=admin)
    assert set(tv_fast) == {"performance", "order_schedule"}
    assert "active_inventory_packages" in summary_fast and "inventory" not in summary_fast
    active_lines = summary_fast["active_orders"]
    assert [x["ship_month"] or "9999-99" for x in active_lines] == sorted(x["ship_month"] or "9999-99" for x in active_lines)
    bulk_line = next(x for x in active_lines if x["id"] == bulk_order_id)
    assert bulk_line["produced"] == 10 and bulk_line["package_count"] == 2
    assert not any(x["s_number"] == "S10001" for x in active_lines)
    assert call("GET", "/api/dashboard-summary")[0] == 401

    for route in ("/", "/entry", "/production", "/inventory", "/shipping", "/costing", "/reporting", "/admin", "/tv"):
        assert call("GET", route, token=admin)[0] == 200, route
    tv_page = call("GET", "/tv", token=admin)[1]
    production_page = call("GET", "/production", token=admin)[1]
    dashboard_page = call("GET", "/", token=admin)[1]
    reporting_page = call("GET", "/reporting", token=admin)[1]
    assert b"Changes every 15 seconds" in tv_page and b"14-Day Production Trend" in tv_page
    assert b"DAILY TARGET CRUSHED" in tv_page and b"targetState" in tv_page and b"/api/tv-dashboard" in tv_page
    assert b"labor cost" not in tv_page.lower() and b"Production Performance by Time Period" in dashboard_page
    assert b"Orders by Ship Month" in production_page and b'type="month"' in production_page
    assert b"PO# optional" in production_page and b"showOrderDetail" in dashboard_page and b"Active Orders by PO#" in dashboard_page
    assert b"buildPOGroups" in dashboard_page and b"showPOGroup" in dashboard_page and b"/api/dashboard-summary" in dashboard_page and b"Packaging Requirements" in dashboard_page
    assert b'orderSummaryMonth' in dashboard_page and b'orderSummaryProduced' in dashboard_page
    assert b'<th>Category</th><th>Color</th><th>Batch #' in call("GET", "/admin", token=admin)[1]
    assert b"Grouped by PO#" in reporting_page and b"This Month" in reporting_page and b'<option value="category">Category</option>' in reporting_page
    for route in ("/api/reports/category.csv", "/api/reports/labor.csv"):
        assert call("GET", route, token=admin)[0] == 200, route
    report, _ = ok("GET", "/api/reporting", token=admin)
    assert report["snapshot"]["labor_cost"] > 0
    assert report["snapshot"]["produced_gallons"] > 0 and report["snapshot"]["produced_qty"] == 50
    assert next(x for x in report["stock_needs"] if x["id"] == need_id)["remaining"] == 2
    assert next(x for x in report["product_lines"] if x["s_number"] == "S10001")["status"] == "Shipped"
    tint_report = next(x for x in report["product_lines"] if x["s_number"] == "S10004")
    assert tint_report["produced_amount"] == 5 and tint_report["shipped_amount"] == 5 and tint_report["status"] == "Shipped"
    feed_report = next(x for x in report["product_lines"] if x["s_number"] == "S10005")
    assert feed_report["produced_amount"] == 5 and feed_report["shipped_amount"] == 5 and feed_report["status"] == "Shipped"
    no_s_report = next(x for x in report["product_lines"] if x["s_number"] == no_s_order["s_number"])
    assert no_s_report["produced_amount"] == 3 and no_s_report["shipped_amount"] == 3 and no_s_report["status"] == "Shipped"
    assert any(x["brand"] == "Paint" for x in report["category_summary"])
    assert any(x["po_number"] == "PO-EDIT-10003" for x in report["po_summary"])
    assert any(x["po_number"] == "PO-EDIT-10003" and x["brand"] == "Paint" for x in report["category_po_summary"])
    ranged, _ = ok("GET", "/api/reporting?from=2026-11-01&to=2026-11-30", token=admin)
    assert ranged["snapshot"]["date_from"] == "2026-11-01" and ranged["snapshot"]["date_to"] == "2026-11-30"
    assert ranged["snapshot"]["batches"] > 0 and ranged["snapshot"]["labor_cost"] > 0
    assert call("GET", "/api/reporting?from=2026-12-01&to=2026-11-01", token=admin)[0] == 400

    admin_data, _ = ok("GET", "/api/admin", token=admin)
    team_id = next(x["id"] for x in admin_data["users"] if x["email"] == "team@test.local")
    ok("DELETE", f"/api/admin/users/{team_id}", token=admin)
    assert call("GET", "/api/session", token=team)[0] == 401
    assert call("POST", "/api/login", {"email": "team@test.local", "password": "TeamUser123!"})[0] == 401
    logout_status, _, logout_headers = call("GET", "/logout", token=admin)
    assert logout_status == 302 and "Max-Age=0" in logout_headers.get("Set-Cookie", "")
    print("PASS: restart-safe login, recipients, role entry, S#/PO# orders, manufacture/tint, future stock, labor, packaging, shipping, TV performance, date-range snapshots, category/PO grouping, edit/delete, CSV, and user deletion")
finally:
    server.shutdown()
    server.server_close()
    if TEST_DB.exists():
        TEST_DB.unlink()
