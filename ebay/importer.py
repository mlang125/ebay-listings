"""Bring existing records into the database. Safe to re-run: rows are matched by SKU / listing ID."""

import csv
import datetime
import glob
import json
import os
import re

from . import config, db


def import_files(conn):
    """Load batches/*/drafts.json and the old inventory.csv."""
    counts = {"drafts": 0, "listings": 0}
    with conn:
        for path in sorted(glob.glob(os.path.join(config.ROOT, "batches", "*", "drafts.json"))):
            with open(path) as f:
                drafts = json.load(f)
            batch = db.batch_id(conn, drafts["batch"], drafts.get("tote"), drafts.get("source_zip"))
            for item in drafts["items"]:
                published = item.get("published", {}).get(config.ENV)
                status = "SKIPPED" if item.get("skip") else "LISTED" if published else "PENDING"
                db.upsert_item(
                    conn,
                    item["sku"],
                    status,
                    "import",
                    at=published["listed_at"] if published else None,
                    title=item["title"],
                    condition=item["condition"],
                    price=item["price"],
                    batch_id=batch,
                    location_id=db.location_id(conn, drafts.get("tote")),
                )
                counts["drafts"] += 1

        csv_path = os.path.join(config.ROOT, "inventory.csv")
        if os.path.exists(csv_path):
            with open(csv_path) as f:
                for row in csv.DictReader(f):
                    db.upsert_item(
                        conn,
                        row["sku"],
                        "LISTED",
                        "import",
                        at=row["listed_at"],
                        title=row["title"],
                        price=float(row["price"]),
                        batch_id=db.batch_id(conn, row["batch"], row["tote"]),
                        location_id=db.location_id(conn, row["tote"]),
                    )
                    db.record_listing(conn, row["sku"], row["env"], row["listing_id"], row["offer_id"],
                                      float(row["price"]), row["listed_at"], "import")
                    counts["listings"] += 1
    return counts



TOTE_RE = re.compile(r"^[A-Za-z]\d$")


def _norm(title):
    return re.sub(r"\s+", " ", title.strip().lower())


def _ebay_csv(path):
    """Rows of an eBay active-listings download as dicts (skips the #INFO line)."""
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        rows = list(csv.reader(f))
    header = rows[1]
    return [dict(zip(header, r), _cols=r) for r in rows[2:] if r and r[0] in ("Revise", "Comment")]


def _resolve_location(raw, binned):
    """Tote code from the tracking sheet's tote column, and a note when it needed guessing.

    Blank means OT (not in a tote: plush bin, house closet, storage...) for rows that
    have been binned, and UN (still needs a tote) for rows below the last binned one.
    """
    raw = raw.strip()
    if TOTE_RE.match(raw):
        return raw.upper(), None
    if not raw:
        return ("OT", None) if binned else ("UN", None)
    if raw.lower() == "storage":
        return "OT", "tracking sheet said storage"
    return "UN", "tracking sheet tote was %r" % raw


def plan_tracking_import(tracking_path, active_path, last_binned_title):
    """Match each active listing to its row in the tracking sheet.

    The tracking sheet is an eBay revise file with the tote typed into the Custom
    label column. Its item numbers were partly mangled by Excel (2.27E+11), so rows
    match by item number when it's intact, otherwise by title. Rows at or above
    `last_binned_title` with no tote are in "OT" (other); rows below it are "UN"
    (not binned yet).
    """
    tracking = [r for r in _ebay_csv(tracking_path) if r["Action"] == "Revise"]
    cut = next(i for i, r in enumerate(tracking) if _norm(r["Title"]) == _norm(last_binned_title))
    active = [r for r in _ebay_csv(active_path) if r["Action"] == "Revise"]

    by_id = {r["Item number"]: i for i, r in enumerate(tracking)}
    by_title = {}
    for i, r in enumerate(tracking):
        by_title.setdefault(_norm(r["Title"]), []).append(i)
    claimed = set()
    plan = []
    for a in active:
        i = by_id.get(a["Item number"])
        how = "item number"
        if i is None or i in claimed:
            free = [j for j in by_title.get(_norm(a["Title"]), []) if j not in claimed]
            # Same title listed more than once: take the latest unclaimed row.
            i, how = (free[-1], "title" if len(free) == 1 else "title, %d candidates" % len(free)) if free else (None, "no match")
        claimed.add(i)
        t = tracking[i] if i is not None else None
        cols = t["_cols"] if t else []
        code, note = _resolve_location(cols[11] if t else "", t is not None and i <= cut)
        notes = [n for n in [note, cols[13].strip() if t and cols[13].strip() not in ("", "-", "0") else None] if n]
        if t is None:
            notes.append("not in tracking sheet")
        if t and cols[12] == "TRUE":
            notes.append("checked (TRUE) in tracking sheet")
        plan.append({
            "listing_id": a["Item number"],
            "sku": a["Custom label (SKU)"] or None,
            "title": a["Title"],
            "category": a["Category name"],
            "price": float(a["Start price"]),
            "location": code,
            "notes": "; ".join(notes) or None,
            "match": how,
            "row": i,
        })
    unmatched_rows = len(tracking) - len(claimed - {None})
    return plan, unmatched_rows


def apply_tracking_import(conn, plan):
    """Create an item (new MV-##### SKU) for each planned listing we don't have yet."""
    created = 0
    with conn:
        for p in plan:
            row = conn.execute("SELECT sku FROM listings WHERE listing_id = ?", (p["listing_id"],)).fetchone()
            sku = row["sku"] if row else p["sku"] if p["sku"] and conn.execute(
                "SELECT 1 FROM items WHERE sku = ?", (p["sku"],)).fetchone() else None
            if sku:
                # Already ours (listed by this tool): refresh price/title, keep its tote.
                db.upsert_item(conn, sku, "LISTED", "import", title=p["title"], price=p["price"], category=p["category"])
            else:
                sku = db.next_sku(conn)
                db.upsert_item(
                    conn, sku, "LISTED", "import",
                    title=p["title"], price=p["price"], category=p["category"],
                    location_id=db.location_id(conn, p["location"]), notes=p["notes"],
                )
                created += 1
            db.record_listing(conn, sku, "production", p["listing_id"], None, p["price"], db.now(), "import")
    return created


def _date(text):
    """eBay report dates ("May-23-26 03:54:10 PDT" or "Nov-05-26") as ISO strings; None if blank."""
    if not text:
        return None
    parts = text.split()
    if len(parts) == 1:
        return datetime.datetime.strptime(parts[0], "%b-%d-%y").date().isoformat()
    return datetime.datetime.strptime(" ".join(parts[:2]), "%b-%d-%y %H:%M:%S").isoformat()


def _money(text):
    return float(text.replace("$", "").replace(",", "")) if text else None


def import_listings_report(conn, path):
    """Fill in condition, category, quantity, price and start date from Seller Hub's All listings report."""
    with open(path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    updated = 0
    with conn:
        for r in rows:
            listing = conn.execute("SELECT sku FROM listings WHERE listing_id = ?", (r["Item number"],)).fetchone()
            if not listing:
                continue
            listed_at = _date(r["Start date"])
            conn.execute(
                "UPDATE items SET condition = ?, category = ?, quantity = ?, price = ?, title = ?, updated_at = ? WHERE sku = ?",
                (r["Condition"], "%s (%s)" % (r["eBay category 1 name"], r["eBay category 1 number"]),
                 int(r["Available quantity"]), _money(r["Current price"]), r["Title"], db.now(), listing["sku"]),
            )
            conn.execute(
                "UPDATE listings SET listed_at = ?, price = ?, condition = ? WHERE listing_id = ?",
                (listed_at, _money(r["Current price"]), r["Condition"], r["Item number"]),
            )
            updated += 1
    return updated, len(rows)


def _tracking_totes(tracking_path, last_binned_title):
    """Tote for any row of the tracking sheet, looked up by item number or title."""
    tracking = [r for r in _ebay_csv(tracking_path) if r["Action"] == "Revise"]
    cut = next(i for i, r in enumerate(tracking) if _norm(r["Title"]) == _norm(last_binned_title))
    by_id, by_title = {}, {}
    for i, r in enumerate(tracking):
        found = _resolve_location(r["_cols"][11], i <= cut)
        by_id[r["Item number"]] = found
        by_title[_norm(r["Title"])] = found  # later rows win, like the listing match
    return lambda listing_id, title: by_id.get(listing_id) or by_title.get(_norm(title))


def import_orders_report(conn, path, tracking_path=None, last_binned_title=None, today=None):
    """Load Seller Hub's Orders report: orders, their lines, and the sold items' statuses.

    Keeps only order data (no buyer names or addresses). A line for a listing we
    know marks that item sold; a line for an older listing creates the item, with
    its tote from the tracking sheet when there is one. Shipped lines end at
    SENT_TO_POST_OFFICE; unshipped ones stay BOUGHT.
    """
    with open(path, encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    header = rows[1]
    rows = [dict(zip(header, r)) for r in rows[2:] if len(r) == len(header) and r[1]]
    tote_of = _tracking_totes(tracking_path, last_binned_title) if tracking_path else lambda *a: None
    today = today or datetime.date.today().isoformat()

    orders = {}
    for r in rows:
        o = orders.setdefault(r["Order Number"], {"header": None, "lines": []})
        # Multi-item orders have a header row (no item number) with the totals, then one row per item.
        if r["Item Number"]:
            o["lines"].append(r)
        if not r["Item Number"] or o["header"] is None:
            o["header"] = r if not r["Item Number"] else o["header"] or r

    counts = {"orders": 0, "lines": 0, "new_items": 0, "unshipped": []}
    with conn:
        for order_id, o in orders.items():
            h = o["header"]
            shipped = _date(h["Shipped On Date"])
            conn.execute(
                "INSERT OR REPLACE INTO orders (order_id, created_at, ship_by, buyer_paid_shipping, total, status,"
                " shipped_at, shipping_service) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (order_id, _date(h["Sale Date"]), _date(h["Ship By Date"]), _money(h["Shipping And Handling"]),
                 _money(h["Total Price"]), "shipped" if shipped else "unshipped", shipped, h["Shipping Service"] or None),
            )
            counts["orders"] += 1
            for line in o["lines"]:
                listing = conn.execute("SELECT sku FROM listings WHERE listing_id = ?", (line["Item Number"],)).fetchone()
                sku = listing["sku"] if listing else None
                if not sku:
                    sku = db.next_sku(conn)
                    found = tote_of(line["Item Number"], line["Item Title"])
                    db.upsert_item(
                        conn, sku, "LISTED", "import", at=_date(line["Sale Date"]),
                        title=line["Item Title"], price=_money(line["Sold For"]), quantity=0,
                        location_id=db.location_id(conn, found[0] if found else None),
                        notes=found[1] if found and found[1] else None,
                    )
                    db.record_listing(conn, sku, "production", line["Item Number"], None, None,
                                      _date(line["Sale Date"]), "import")
                    counts["new_items"] += 1
                line_id = line["Transaction ID"] or "%s-%s" % (order_id, line["Item Number"])
                new_line = conn.execute(
                    "INSERT OR IGNORE INTO order_lines (line_item_id, order_id, sku, listing_id, title, price, quantity,"
                    " tracking, promoted) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (line_id, order_id, sku, line["Item Number"], line["Item Title"], _money(line["Sold For"]),
                     int(line["Quantity"]), line["Tracking Number"] or None, line["Sold Via Promoted Listings"] == "Yes"),
                ).rowcount
                counts["lines"] += 1
                remaining = conn.execute("SELECT quantity FROM items WHERE sku = ?", (sku,)).fetchone()["quantity"]
                if not new_line or remaining > 0:
                    continue  # already imported, or more units still listed (multi-quantity listing)
                db.set_status(conn, sku, "BOUGHT", "import", data={"order_id": order_id}, at=_date(line["Sale Date"]))
                if shipped:
                    db.set_status(conn, sku, "SENT_TO_POST_OFFICE", "import",
                                  data={"tracking": line["Tracking Number"] or None}, at=shipped)
                else:
                    counts["unshipped"].append((sku, line["Item Title"], _date(h["Ship By Date"]) or "", _date(h["Sale Date"])))
    return counts
