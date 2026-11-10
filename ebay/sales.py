"""Sales numbers from the orders in the database (run the order sync first for fresh ones).

Sales are item prices (what buyers paid for the items, before eBay fees, without tax);
shipping is what buyers paid for shipping. Cancelled orders are left out of both. eBay
fees and item costs aren't recorded yet, so there's no profit figure.
"""

import datetime
import statistics

# Order lines that count as sales: their order wasn't cancelled.
SOLD = """
    FROM order_lines ol JOIN orders o ON o.order_id = ol.order_id
    WHERE o.status <> 'cancelled'
"""

PRICE_BANDS = [(0, 10), (10, 20), (20, 50), (50, 100), (100, None)]
AGE_BANDS = [(0, 30), (30, 90), (90, 180), (180, None)]


def _since(days, today):
    return (today - datetime.timedelta(days=days)).isoformat()


def summary(conn, today=None):
    today = today or datetime.date.today()
    prices = [r[0] for r in conn.execute("SELECT ol.price " + SOLD + " AND ol.price IS NOT NULL")]
    shipping = conn.execute("SELECT SUM(buyer_paid_shipping), COUNT(*) FROM orders WHERE status <> 'cancelled'").fetchone()
    cancelled = conn.execute("SELECT COUNT(*) FROM orders WHERE status = 'cancelled'").fetchone()[0]
    first = conn.execute("SELECT MIN(created_at) FROM orders").fetchone()[0]

    def window(days):
        r = conn.execute("SELECT COUNT(*), SUM(ol.price) " + SOLD + " AND o.created_at >= ?", (_since(days, today),)).fetchone()
        return {"items": r[0], "sales": r[1] or 0}

    listed = conn.execute("SELECT COUNT(*) FROM items WHERE status = 'LISTED'").fetchone()[0]
    last30 = window(30)
    return {
        "since": first[:10] if first else None,
        "orders": shipping[1],
        "items": len(prices),
        "sales": sum(prices),
        "shipping": shipping[0] or 0,
        "average": sum(prices) / len(prices) if prices else None,
        "median": statistics.median(prices) if prices else None,
        "cancelled": cancelled,
        "cancel_rate": cancelled / (shipping[1] + cancelled) if shipping[1] + cancelled else None,
        "last7": window(7),
        "last30": last30,
        # Share of what was for sale in the last 30 days that sold.
        "sell_through_30": last30["items"] / (listed + last30["items"]) if listed + last30["items"] else None,
    }


def by_month(conn):
    rows = conn.execute(
        "SELECT substr(o.created_at, 1, 7) AS month, COUNT(DISTINCT o.order_id) AS orders, COUNT(*) AS items,"
        " SUM(ol.price) AS sales " + SOLD + " GROUP BY month ORDER BY month DESC"
    ).fetchall()
    shipping = dict(conn.execute(
        "SELECT substr(created_at, 1, 7), SUM(buyer_paid_shipping) FROM orders WHERE status <> 'cancelled' GROUP BY 1"))
    cancelled = dict(conn.execute(
        "SELECT substr(created_at, 1, 7), COUNT(*) FROM orders WHERE status = 'cancelled' GROUP BY 1"))
    return [dict(r, shipping=shipping.get(r["month"]) or 0, cancelled=cancelled.get(r["month"], 0),
                 average=r["sales"] / r["items"] if r["items"] else None) for r in rows]


def _band_label(lo, hi, unit):
    return "%s%d+" % (unit, lo) if hi is None else "%s%d–%s%d" % (unit, lo, unit, hi)


def price_bands(conn):
    prices = [r[0] for r in conn.execute("SELECT ol.price " + SOLD + " AND ol.price IS NOT NULL")]
    out = []
    for lo, hi in PRICE_BANDS:
        band = [p for p in prices if p >= lo and (hi is None or p < hi)]
        out.append({"band": _band_label(lo, hi, "$"), "items": len(band), "sales": sum(band),
                    "share": sum(band) / sum(prices) if prices else 0})
    return out


def active(conn, today=None):
    """What's listed now: count, asking value, and how long it's been up."""
    today = today or datetime.date.today()
    rows = conn.execute(
        "SELECT i.price, (SELECT MAX(listed_at) FROM listings WHERE sku = i.sku) AS listed_at"
        " FROM items i WHERE i.status = 'LISTED'"
    ).fetchall()
    ages = [(today - datetime.date.fromisoformat(r["listed_at"][:10])).days for r in rows if r["listed_at"]]
    bands = []
    for lo, hi in AGE_BANDS:
        n = sum(1 for a in ages if a >= lo and (hi is None or a < hi))
        bands.append({"band": "%d+ days" % lo if hi is None else "%d–%d days" % (lo, hi), "items": n})
    return {
        "items": len(rows),
        "asking": sum(r["price"] or 0 for r in rows),
        "average_asking": sum(r["price"] or 0 for r in rows) / len(rows) if rows else None,
        "median_age": statistics.median(ages) if ages else None,
        "age_unknown": len(rows) - len(ages),
        "ages": bands,
    }


def by_user(conn, days=30, today=None):
    """What each person did in the last `days` days, from the event log."""
    today = today or datetime.date.today()
    rows = conn.execute(
        "SELECT COALESCE(u.name, '(no user)') AS name, e.action, e.to_status, COUNT(*) AS n"
        " FROM events e LEFT JOIN users u ON u.id = e.user_id WHERE e.at >= ?"
        " GROUP BY 1, 2, 3", (_since(days, today),)
    ).fetchall()
    people = {}
    for r in rows:
        counts = people.setdefault(r["name"], {"listed": 0, "edited": 0, "moved": 0, "packed": 0, "labels": 0, "other": 0})
        if r["action"] == "listed":
            counts["listed"] += r["n"]
        elif r["action"] in ("edited", "drafted"):
            counts["edited"] += r["n"]
        elif r["action"] == "moved":
            counts["moved"] += r["n"]
        elif r["action"] == "status" and r["to_status"] == "PACKAGED":
            counts["packed"] += r["n"]
        elif r["action"] == "status" and r["to_status"] == "LABEL_PRINTED":
            counts["labels"] += r["n"]
        else:
            counts["other"] += r["n"]
    return sorted(people.items())
