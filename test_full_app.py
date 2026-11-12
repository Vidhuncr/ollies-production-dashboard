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
    entry_page = call("GET", "/entry", token=team)
    assert entry_page[0] == 200
    assert b"What is this manufacturing for?" in entry_page[1]
    assert b"Select the exact S# or no-S# order" in entry_page[1]
    assert b"What is this tinting for?" in entry_page[1]
    for route in ("/", "/production", "/inventory", "/shipping", "/costing", "/reporting", "/admin"):
        team_page = call("GET", route, token=team)
        assert team_page[0] == 302 and team_page[2].get("Location") == "/entry", route
    assert call("GET", "/", token=manager)[0] == 200

    ok("POST", "/api/catalog", {"category": "Paint", "color": "Blue", "unit": "gallons"}, team, 201)
    ok("POST", "/api/catalog", {"category": "Paint", "color": "Red", "unit": "gallons"}, team, 201)
    ok("POST", "/api/catalog", {"category": "Parts", "color": "Red", "unit": "qty"}, team, 201)
    ok("POST", "/api/catalog", {"category": "Unused", "color": "Test", "unit": "qty"}, team, 201)
    assert call("POST", "/api/orders", {"s_number": "S99999", "brand": "Paint", "color": "Blue", "gallons": 1}, team)[0] == 400
    admin_data, _ = ok("GET", "/api/admin", token=admin)
    unused_id = next(x["id"] for x in admin_data["catalog"] if x["category"] == "Unused")
    assert call("PUT", f"/api/admin/catalog/{unused_id}", {"unit": "qty"}, team)[0] == 403
    assert call("DELETE", f"/api/admin/catalog/{unused_id}", token=manager)[0] == 403
    ok("DELETE", f"/api/admin/catalog/{unused_id}", token=admin)

    ok("POST", "/api/labor-rates", {"labor_name": "General", "hourly_rate": 20, "effective_from": "2026-01-01"}, manager, 201)
    costing, _ = ok("GET", "/api/costing", token=admin)
    rate_id = costing["current_rates"][0]["id"]

    ok("POST", "/api/orders", {"s_number": "S10001", "brand": "Paint", "color": "Blue", "gallons": 100, "ship_month": "2026-11"}, team, 201)
    dashboard, _ = ok("GET", "/api/dashboard")
    gallon_order = next(x for x in dashboard["orders"] if x["s_number"] == "S10001")
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
    ok("PUT", f"/api/admin/orders/{stock_order_admin['id']}", {"s_number": "S10003", "brand": "Paint", "color": "Blue", "gallons": 10, "ship_month": "2027-03", "created_at": stock_order_admin["created_at"]}, manager)
    admin_data, _ = ok("GET", "/api/admin", token=admin)
    assert next(x for x in admin_data["orders"] if x["id"] == stock_order_admin["id"])["ship_month"] == "2027-03"
    assert call("DELETE", f"/api/admin/orders/{stock_order_admin['id']}", token=manager)[0] == 403

    for route in ("/", "/entry", "/production", "/inventory", "/shipping", "/costing", "/reporting", "/admin", "/tv"):
        assert call("GET", route, token=admin)[0] == 200, route
    tv_page = call("GET", "/tv", token=admin)[1]
    production_page = call("GET", "/production", token=admin)[1]
    assert b"Changes every 15 seconds" in tv_page and b"Orders by Ship Month" in tv_page
    assert b"Orders by Ship Month" in production_page and b'type="month"' in production_page
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

    admin_data, _ = ok("GET", "/api/admin", token=admin)
    team_id = next(x["id"] for x in admin_data["users"] if x["email"] == "team@test.local")
    ok("DELETE", f"/api/admin/users/{team_id}", token=admin)
    assert call("POST", "/api/login", {"email": "team@test.local", "password": "TeamUser123!"})[0] == 401
    logout_status, _, logout_headers = call("GET", "/logout", token=admin)
    assert logout_status == 302 and "Max-Age=0" in logout_headers.get("Set-Cookie", "")
    print("PASS: login, role-specific team entry, S# and no-S# customer orders, mixed manufacture-and-tint fulfillment, shared remaining need, catalog, future-stock needs, labor, packaging, stock rework, case packs, assignment, shipping, edit, undo, reports, pages, CSV, and user deletion")
finally:
    server.shutdown()
    server.server_close()
    if TEST_DB.exists():
        TEST_DB.unlink()
