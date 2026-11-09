"""ebay-listings command line.

  .venv/bin/python cli.py login   one-time browser sign-in; stores the refresh token in the Keychain
  .venv/bin/python cli.py check   confirms the token works and shows the account's selling status
  .venv/bin/python cli.py setup   creates business policies and the ship-from location (safe to re-run)
  .venv/bin/python cli.py publish <batch> [SKU ...]   publishes reviewed drafts (all, or only the SKUs given)
  .venv/bin/python cli.py comps <batch> [SKU ...]     price estimate from active listings + sold-search links for unpublished drafts
  .venv/bin/python cli.py sold <batch> <SKU> <file>   saves sold comps (JSON from ebay/sold_extract.js) into the draft

  .venv/bin/python cli.py import                      loads batches/*/drafts.json and the old inventory.csv into the database
  .venv/bin/python cli.py import-tracking <sheet.csv> <active.csv> "<last binned title>"
                                             matches active listings to the tote-tracking sheet (new items get MV-#####)
  .venv/bin/python cli.py import-listings <report.csv>   condition, category, quantity, price from Seller Hub's All listings report
  .venv/bin/python cli.py import-orders <report.csv> [sheet.csv "<last binned title>"]
                                             orders and sold items from Seller Hub's Orders report (no buyer details kept)
  .venv/bin/python cli.py sync [DAYS]                pulls eBay orders from the last DAYS days (default 90) and updates statuses
  .venv/bin/python cli.py picklist [DAYS]            sold, unshipped items by tote; DAYS: only those due within N days (0 = today)
  .venv/bin/python cli.py items [STATUS]              lists items (all, or one status such as PENDING or LISTED)
  .venv/bin/python cli.py history <SKU>               shows an item's event log
  .venv/bin/python cli.py tote <CODE>                lists what's in a tote or place (OT other, HC house closet, ST storage, UN not binned)
  .venv/bin/python cli.py move <TOTE> <SKU> [SKU ...] puts items in a tote and logs the move
  .venv/bin/python cli.py merge <FROM> <TO>          moves everything in one tote into another (e.g. merge X2 A3)
  .venv/bin/python cli.py next-sku                    prints the next free MV-##### number
  .venv/bin/python cli.py export <file.csv>           writes SKU, tote, status, title, price and listing ID to a CSV
  .venv/bin/python cli.py user add <name> [role]      adds a user (role: admin, lister or shipper; default admin)
  .venv/bin/python cli.py user list
"""

import csv
import json
import os
import sys
import webbrowser

from ebay import api, auth, comps, config, db, importer, listings, orders, setup


def cmd_login():
    url = auth.consent_url()
    print("Opening eBay sign-in (%s). Sign in as your %s user and click Agree." % (config.ENV, config.ENV))
    print("If the browser doesn't open, visit:\n\n%s\n" % url)
    webbrowser.open(url)
    pasted = input("Paste the full URL of the page you land on afterwards:\n> ")
    days = auth.login(pasted)
    print("Logged in. Refresh token saved to the Keychain (valid about %d days)." % days)


def cmd_check():
    privileges = api.call("GET", "/sell/account/v1/privilege")
    print(json.dumps(privileges, indent=2))


def cmd_setup():
    setup.run()


def _batch_dir(batch):
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "batches", batch)


def cmd_publish(batch, *skus):
    listings.publish(_batch_dir(batch), set(skus) or None)


def cmd_comps(batch, *skus):
    comps.refresh_active(_batch_dir(batch), set(skus) or None)


def cmd_sold(batch, sku, path):
    with open(path) as f:
        sales = json.load(f)
    summary = comps.record_sold(_batch_dir(batch), sku, sales)
    print("%s sold comps: %s" % (sku, comps._format(summary)))


def cmd_import():
    counts = importer.import_files(db.connect())
    print("Imported %(drafts)d drafts and %(listings)d listings." % counts)


def cmd_import_tracking(sheet, active, last_binned_title):
    plan, old_rows = importer.plan_tracking_import(sheet, active, last_binned_title)
    created = importer.apply_tracking_import(db.connect(), plan)
    print("%d active listings, %d new items; %d sheet rows aren't active." % (len(plan), created, old_rows))


def cmd_import_listings(path):
    print("Updated %d of %d listings." % importer.import_listings_report(db.connect(), path))


def cmd_import_orders(path, sheet=None, last_binned_title=None):
    counts = importer.import_orders_report(db.connect(), path, sheet, last_binned_title)
    print("%(orders)d orders, %(lines)d lines, %(new_items)d items created from older listings." % counts)
    for sku, title, ship_by, sold in counts["unshipped"]:
        print("  not shipped: %s  sold %s, ship by %s  %s" % (sku, sold, ship_by, title))


def cmd_sync(days="90"):
    counts = orders.sync(db.connect(), int(days))
    print("%(orders)d orders checked, %(new_lines)d new sales, %(new_items)d new items." % counts)
    for sku, before, after, title in counts["changes"]:
        print("  %s  %s -> %s  %s" % (sku, before, after, title))


def cmd_picklist(days=None):
    rows = orders.pick_list(db.connect(), int(days) if days is not None else None)
    tote = object()
    for r in rows:
        if r["tote"] != tote:
            tote = r["tote"]
            print("\n%s" % (tote or "(location unknown)"))
        print("  [ ] %-9s ship by %s  %s" % (r["sku"], r["ship_by"], r["title"]))
    print("\n%d items to ship" % len(rows))


ITEMS_SQL = """
    SELECT i.sku, i.status, l.code AS tote, i.price, i.title,
           (SELECT listing_id FROM listings WHERE sku = i.sku ORDER BY listed_at DESC LIMIT 1) AS listing_id
    FROM items i LEFT JOIN locations l ON l.id = i.location_id
"""


def cmd_items(status=None):
    conn = db.connect()
    sql = ITEMS_SQL + (" WHERE i.status = ?" if status else "") + " ORDER BY i.sku"
    for r in conn.execute(sql, (status.upper(),) if status else ()):
        print("%-9s %-10s %-4s %8s  %s" % (r["sku"], r["status"], r["tote"] or "?", "$%.2f" % r["price"] if r["price"] else "", r["title"] or ""))


def cmd_history(sku):
    conn = db.connect()
    rows = conn.execute(
        "SELECT e.*, u.name AS user FROM events e LEFT JOIN users u ON u.id = e.user_id"
        " WHERE e.entity = 'item' AND e.entity_id = ? ORDER BY e.at, e.id",
        (sku,),
    )
    for r in rows:
        change = "%s -> %s" % (r["from_status"] or "", r["to_status"]) if r["to_status"] else ""
        print("%s  %-8s %-7s %-6s %s %s" % (r["at"], r["action"], r["actor"], r["user"] or "", change, r["data"] or ""))


def cmd_tote(code):
    conn = db.connect()
    rows = conn.execute(ITEMS_SQL + " WHERE l.code = ? ORDER BY i.sku", (code.upper(),)).fetchall()
    for r in rows:
        print("%-9s %-10s %8s  %s" % (r["sku"], r["status"], "$%.2f" % r["price"] if r["price"] else "", r["title"] or ""))
    print("%d items in %s" % (len(rows), code.upper()))


def cmd_move(code, *skus):
    conn = db.connect()
    user = db.user_id(conn, config.CLI_USER)
    with conn:
        for sku in skus:
            db.move(conn, sku.upper(), code.upper(), "cli", user)
    print("Moved %d items to %s" % (len(skus), code.upper()))


def cmd_merge(from_code, to_code):
    conn = db.connect()
    with conn:
        n = db.merge(conn, from_code.upper(), to_code.upper(), "cli", db.user_id(conn, config.CLI_USER))
    print("Moved %d items from %s to %s" % (n, from_code.upper(), to_code.upper()))


def cmd_next_sku():
    print(db.next_sku(db.connect()))


def cmd_export(path):
    conn = db.connect()
    rows = conn.execute(ITEMS_SQL + " ORDER BY i.sku").fetchall()
    with open(path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["sku", "status", "tote", "price", "title", "listing_id"])
        writer.writerows([r["sku"], r["status"], r["tote"], r["price"], r["title"], r["listing_id"]] for r in rows)
    print("Wrote %d items to %s" % (len(rows), path))


def cmd_user(action, *args):
    conn = db.connect()
    if action == "add":
        db.add_user(conn, *args)
    for r in conn.execute("SELECT name, role, active FROM users ORDER BY name"):
        print("%-12s %-8s %s" % (r["name"], r["role"], "" if r["active"] else "(inactive)"))


COMMANDS = {
    "login": cmd_login,
    "check": cmd_check,
    "setup": cmd_setup,
    "publish": cmd_publish,
    "comps": cmd_comps,
    "sold": cmd_sold,
    "import": cmd_import,
    "import-tracking": cmd_import_tracking,
    "import-listings": cmd_import_listings,
    "import-orders": cmd_import_orders,
    "sync": cmd_sync,
    "picklist": cmd_picklist,
    "items": cmd_items,
    "history": cmd_history,
    "tote": cmd_tote,
    "move": cmd_move,
    "merge": cmd_merge,
    "next-sku": cmd_next_sku,
    "export": cmd_export,
    "user": cmd_user,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    COMMANDS[sys.argv[1]](*sys.argv[2:])
