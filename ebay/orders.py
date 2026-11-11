"""Order sync (Fulfillment API) and the pick list.

Keeps only order data, never buyer names or addresses. Statuses only move forward:
a sale makes an item BOUGHT, eBay marking it shipped makes it SENT_TO_POST_OFFICE,
and a cancellation before shipping makes it CANCELLED (the item is still on hand).
A cancellation after shipping (a refund) leaves the item's status alone. Steps people do in between (LABEL_PRINTED,
PACKAGED) are never undone by a sync.
"""

import datetime
import urllib.parse
import zoneinfo

from . import api, db

EBAY_TZ = zoneinfo.ZoneInfo("America/Los_Angeles")  # eBay dates its days in Pacific time

# Order of the main path, for "only move forward".
FLOW = ["PENDING", "LISTED", "BOUGHT", "LABEL_PRINTED", "PACKAGED", "SENT_TO_POST_OFFICE", "DELIVERED"]


def _advance(conn, sku, status, data, user=None):
    current = conn.execute("SELECT status FROM items WHERE sku = ?", (sku,)).fetchone()["status"]
    if current in FLOW and FLOW.index(current) < FLOW.index(status):
        db.set_status(conn, sku, status, "sync", user, data=data)


def _fetch_orders(days):
    since = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    path = "/sell/fulfillment/v1/order?limit=200&filter=" + urllib.parse.quote("creationdate:[%s..]" % since)
    while path:
        page = api.call("GET", path)
        yield from page.get("orders", [])
        nxt = page.get("next")
        path = nxt[nxt.index("/sell/"):] if nxt else None


def _local_date(utc):
    """eBay's ship-by is the end of a day in Pacific time (e.g. ...T07:59:59Z); return that day."""
    return datetime.datetime.fromisoformat(utc.replace("Z", "+00:00")).astimezone(EBAY_TZ).date().isoformat()


def _money(amount):
    return float(amount["value"]) if amount else None


def sync(conn, days=90, user=None):
    """Pull the last `days` of orders and update orders, lines and item statuses. `user`: who ran it."""
    counts = {"orders": 0, "new_lines": 0, "new_items": 0, "changes": []}
    with conn:
        for o in _fetch_orders(days):
            cancelled = o.get("cancelStatus", {}).get("cancelState") == "CANCELED"
            shipped = o["orderFulfillmentStatus"] == "FULFILLED"
            ship_by = min(
                (l["lineItemFulfillmentInstructions"]["shipByDate"] for l in o["lineItems"]
                 if l.get("lineItemFulfillmentInstructions", {}).get("shipByDate")),
                default=None,
            )
            status = "cancelled" if cancelled else "shipped" if shipped else "unshipped"
            prev = conn.execute("SELECT status FROM orders WHERE order_id = ?", (o["orderId"],)).fetchone()
            if prev and prev["status"] == "returned" and not cancelled:
                status = prev["status"]  # set by hand; eBay still calls these fulfilled
            conn.execute(
                "INSERT INTO orders (order_id, created_at, ship_by, buyer_paid_shipping, total, status)"
                " VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT (order_id) DO UPDATE SET ship_by = excluded.ship_by, status = excluded.status,"
                " total = excluded.total, buyer_paid_shipping = excluded.buyer_paid_shipping",
                (o["orderId"], o["creationDate"][:19], _local_date(ship_by) if ship_by else None,
                 _money(o["pricingSummary"].get("deliveryCost")), _money(o["pricingSummary"].get("total")), status),
            )
            counts["orders"] += 1
            for line in o["lineItems"]:
                listing = conn.execute("SELECT sku FROM listings WHERE listing_id = ?", (line["legacyItemId"],)).fetchone()
                sku = listing["sku"] if listing else None
                if not sku:
                    sku = db.next_sku(conn)
                    db.upsert_item(conn, sku, "LISTED", "sync", user, at=o["creationDate"][:19], title=line["title"],
                                   price=_money(line["lineItemCost"]), quantity=0)
                    db.record_listing(conn, sku, "production", line["legacyItemId"], None, None,
                                      o["creationDate"][:19], "sync", user)
                    counts["new_items"] += 1
                # The Orders report gave some lines a made-up ID; switch them to eBay's.
                conn.execute(
                    "UPDATE order_lines SET line_item_id = ? WHERE order_id = ? AND listing_id = ? AND line_item_id <> ?",
                    (line["lineItemId"], o["orderId"], line["legacyItemId"], line["lineItemId"]),
                )
                new = conn.execute(
                    "INSERT OR IGNORE INTO order_lines (line_item_id, order_id, sku, listing_id, title, price, quantity)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (line["lineItemId"], o["orderId"], sku, line["legacyItemId"], line["title"],
                     _money(line["lineItemCost"]), line["quantity"]),
                ).rowcount
                counts["new_lines"] += new
                remaining = conn.execute("SELECT quantity, status FROM items WHERE sku = ?", (sku,)).fetchone()
                if remaining["quantity"] > 0 or remaining["status"] == "RETURNED":
                    continue  # more units still listed, or settled by hand
                latest = conn.execute(
                    "SELECT o.order_id FROM order_lines ol JOIN orders o ON o.order_id = ol.order_id"
                    " WHERE ol.sku = ? ORDER BY o.created_at DESC, o.order_id DESC LIMIT 1", (sku,)
                ).fetchone()["order_id"]
                if latest != o["orderId"]:
                    continue  # an item sold more than once (e.g. resold after a cancellation) follows its latest order
                before = remaining["status"]
                data = {"order_id": o["orderId"]}
                if cancelled and not shipped:
                    db.set_status(conn, sku, "CANCELLED", "sync", user, data=data)
                elif cancelled:
                    counts["changes"].append((sku, before, before + " (order cancelled after shipping: refund?)", line["title"]))
                else:
                    if before == "CANCELLED":
                        db.set_status(conn, sku, "BOUGHT", "sync", user, data=data)  # sold again after a cancellation
                    _advance(conn, sku, "BOUGHT", data, user)
                    if shipped:
                        _advance(conn, sku, "SENT_TO_POST_OFFICE", data, user)
                after = conn.execute("SELECT status FROM items WHERE sku = ?", (sku,)).fetchone()["status"]
                if after != before:
                    counts["changes"].append((sku, before, after, line["title"]))
        # Every sync is recorded, even one that finds nothing new, so "orders last checked" is true.
        db.log_event(conn, "orders", "sync", "synced", "sync", user, data={
            "days": days, "orders": counts["orders"], "new_lines": counts["new_lines"], "changes": len(counts["changes"])})
    return counts


def pick_list(conn, within_days=None, today=None):
    """Items sold and not yet shipped, by tote; optionally only those due within N days (0 = today)."""
    today = today or datetime.date.today()
    sql = """
        SELECT i.sku, i.status, l.code AS tote, o.ship_by, o.order_id, i.title, i.missing_since
        FROM items i
        JOIN order_lines ol ON ol.sku = i.sku
        JOIN orders o ON o.order_id = ol.order_id
        LEFT JOIN locations l ON l.id = i.location_id
        WHERE o.status = 'unshipped' AND i.status IN ('BOUGHT', 'LABEL_PRINTED', 'PACKAGED')
    """
    params = []
    if within_days is not None:
        sql += " AND o.ship_by <= ?"
        params.append((today + datetime.timedelta(days=within_days)).isoformat())
    sql += " ORDER BY l.code IS NULL, l.code, o.ship_by, i.sku"
    return conn.execute(sql, params).fetchall()
