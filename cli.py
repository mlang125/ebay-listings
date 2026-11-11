"""ebay-listings command line.

  .venv/bin/python cli.py login   one-time browser sign-in; stores the refresh token in the Keychain
  .venv/bin/python cli.py check   confirms the token works and shows the account's selling status
  .venv/bin/python cli.py setup   creates business policies and the ship-from location (safe to re-run)
  .venv/bin/python cli.py import-drafts [batch ...]   loads new drafts from batches/<batch>/drafts.json into the database
  .venv/bin/python cli.py export-drafts <batch> <file> writes a batch's drafts (as stored now) to a JSON file
  .venv/bin/python cli.py publish <batch> [SKU ...]   publishes reviewed drafts (all, or only the SKUs given)
  .venv/bin/python cli.py comps <batch> [SKU ...]     price estimate from active listings + sold-search links for unpublished drafts
  .venv/bin/python cli.py sold <SKU> <file>           saves sold comps (JSON from ebay/sold_extract.js) on the draft

  .venv/bin/python cli.py import                      loads new drafts from batches/*/drafts.json and the old inventory.csv
  .venv/bin/python cli.py import-tracking <sheet.csv> <active.csv> "<last binned title>"
                                             matches active listings to the tote-tracking sheet (new items get MV-#####)
  .venv/bin/python cli.py import-listings <report.csv>   condition, category, quantity, price from Seller Hub's All listings report
  .venv/bin/python cli.py import-orders <report.csv> [sheet.csv "<last binned title>"]
                                             orders and sold items from Seller Hub's Orders report (no buyer details kept)
  .venv/bin/python cli.py sync [DAYS]                pulls eBay orders from the last DAYS days (default 90; 365 or more for everything)
  .venv/bin/python cli.py picklist [DAYS]            sold, unshipped items by tote; DAYS: only those due within N days (0 = today)
  .venv/bin/python cli.py items [STATUS]              lists items (all, or one status such as PENDING or LISTED)
  .venv/bin/python cli.py history <SKU>               shows an item's event log
  .venv/bin/python cli.py tote <CODE>                lists what's in a tote or place (OT other, HC house closet, ST storage, UN not binned)
  .venv/bin/python cli.py move <TOTE> <SKU> [SKU ...] puts items in a tote and logs the move
  .venv/bin/python cli.py merge <FROM> <TO>          moves everything on hand in one tote into another (e.g. merge X2 A3)
  .venv/bin/python cli.py consolidate <TOTE> <TOTE> [...] [--into CODE]
                                             combines totes; without --into they go into the next free ID
  .venv/bin/python cli.py free-tote                   prints the next free tote ID (A1..Z9 with nothing on hand)
  .venv/bin/python cli.py missing <SKU> [...]         marks items missing from their tote / found <SKU> [...] clears it
  .venv/bin/python cli.py relists                     cancelled/returned items: suggested relist links and what's not relisted
  .venv/bin/python cli.py relists --link              saves the suggested links / relists <OLD> <NEW> links one by hand
  .venv/bin/python cli.py sales                       sales numbers by month
  .venv/bin/python cli.py draft-test <batch> [opus|sonnet|haiku ...]
                                             re-drafts a batch with the Claude API and compares (saves nothing to the database)
  .venv/bin/python cli.py next-sku                    prints the next free MV-##### number
  .venv/bin/python cli.py export <file.csv>           writes SKU, tote, status, title, price and listing ID to a CSV
  (Commands that change anything record CLI_USER from .env as the person who did it.)
  .venv/bin/python cli.py user add <name> [role]      adds a user (role: admin, lister or shipper; default admin)
  .venv/bin/python cli.py user list
  .venv/bin/python cli.py user password <name>        sets a user's web password (typed, not shown)
  .venv/bin/python cli.py serve [--lan]               starts the web app on http://127.0.0.1:5050
                                             (--lan: also reachable from phones and laptops on the home wifi)
"""

import csv
import getpass
import io
import json
import os
import sys
import webbrowser

from ebay import api, auth, comps, config, db, drafts, importer, listings, orders, sales, setup


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


def _user(conn):
    """The CLI's user (CLI_USER in .env). Commands that change anything need one, so every change says who made it."""
    if not config.CLI_USER:
        raise SystemExit("Set CLI_USER=<your name> in .env first, so changes are recorded under your name.")
    return db.user_id(conn, config.CLI_USER)


def cmd_publish(batch, *skus):
    conn = db.connect()
    listings.publish(conn, batch, set(skus) or None, user=_user(conn))


def cmd_comps(batch, *skus):
    conn = db.connect()
    comps.refresh_active(conn, batch, set(skus) or None, user=_user(conn))


def cmd_sold(sku, path):
    with open(path) as f:
        sales = json.load(f)
    conn = db.connect()
    summary = comps.record_sold(conn, sku, sales, user=_user(conn))
    print("%s sold comps: %s" % (sku, comps._format(summary)))


def cmd_import():
    conn = db.connect()
    counts = importer.import_files(conn, _user(conn))
    print("Imported %(drafts)d new drafts and %(listings)d listings." % counts)


def cmd_import_drafts(*batches):
    conn = db.connect()
    user = _user(conn)
    root = os.path.join(config.ROOT, "batches")
    for batch in batches or sorted(os.listdir(root)):
        path = os.path.join(root, batch, "drafts.json")
        if not os.path.exists(path):
            if batches:
                raise SystemExit("No %s" % path)
            continue
        with conn:
            added, existing = drafts.import_json(conn, path, "cli", user)
        print("%s: %d new drafts%s" % (batch, added, ", %d already loaded (left as they are)" % existing if existing else ""))


def cmd_export_drafts(batch, path):
    print("Wrote %d drafts to %s" % (drafts.export_json(db.connect(), batch, path), path))


def cmd_import_tracking(sheet, active, last_binned_title):
    plan, old_rows = importer.plan_tracking_import(sheet, active, last_binned_title)
    conn = db.connect()
    created = importer.apply_tracking_import(conn, plan, _user(conn))
    print("%d active listings, %d new items; %d sheet rows aren't active." % (len(plan), created, old_rows))


def cmd_import_listings(path):
    conn = db.connect()
    print("Updated %d of %d listings." % importer.import_listings_report(conn, path, _user(conn)))


def cmd_import_orders(path, sheet=None, last_binned_title=None):
    conn = db.connect()
    counts = importer.import_orders_report(conn, path, sheet, last_binned_title, user=_user(conn))
    print("%(orders)d orders, %(lines)d lines, %(new_items)d items created from older listings." % counts)
    for sku, title, ship_by, sold in counts["unshipped"]:
        print("  not shipped: %s  sold %s, ship by %s  %s" % (sku, sold, ship_by, title))
    if counts["unshipped"]:
        print("Run `cli.py sync 365` next: the report can't show cancellations, the order sync can.")


def cmd_sync(days="90"):
    conn = db.connect()
    counts = orders.sync(conn, int(days), _user(conn))
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
    SELECT i.sku, i.status, l.code AS tote, i.price, i.title, i.missing_since, i.relisted_as,
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
    code = db.check_code(code)
    rows = conn.execute(ITEMS_SQL + " WHERE l.code = ? AND i.status NOT IN (?, ?) ORDER BY i.sku", (code, *db.GONE)).fetchall()
    for r in rows:
        print("%-9s %-10s %8s  %s" % (r["sku"], r["status"], "$%.2f" % r["price"] if r["price"] else "", r["title"] or ""))
    print("%d items on hand in %s" % (len(rows), code))


def cmd_move(code, *skus):
    conn = db.connect()
    code = db.check_code(code)
    user = _user(conn)
    with conn:
        for sku in skus:
            db.move(conn, sku.upper(), code, "cli", user)
    print("Moved %d items to %s" % (len(skus), code))


def cmd_merge(from_code, to_code):
    conn = db.connect()
    with conn:
        n = db.merge(conn, db.check_code(from_code), db.check_code(to_code), "cli", _user(conn))
    print("Moved %d items from %s to %s" % (n, from_code.upper(), to_code.upper()))


def cmd_consolidate(*args):
    args = list(args)
    into = None
    if "--into" in args:
        i = args.index("--into")
        into = db.check_code(args[i + 1])
        del args[i:i + 2]
    codes = list(dict.fromkeys(db.check_code(c) for c in args))
    if len(codes) < 2:
        raise SystemExit("Give at least two totes, e.g. consolidate B4 C6")
    conn = db.connect()
    into = into or db.suggest_tote(conn, freeing=codes)
    if into not in codes and into in db.totes_in_use(conn):
        raise SystemExit("%s already has items in it; list it with the others to add to it." % into)
    with conn:
        n = db.consolidate(conn, codes, into, "cli", _user(conn))
    print("Moved %d items from %s into %s. Label the tote %s." % (n, " + ".join(codes), into, into))


def cmd_missing(*skus):
    conn = db.connect()
    user = _user(conn)
    with conn:
        for sku in skus:
            db.set_missing(conn, sku.upper(), True, "cli", user)
    print("Marked %d items missing (their tote is unchanged)." % len(skus))


def cmd_found(*skus):
    conn = db.connect()
    user = _user(conn)
    with conn:
        for sku in skus:
            db.set_missing(conn, sku.upper(), False, "cli", user)
    print("Marked %d items found." % len(skus))


def cmd_relists(*args):
    conn = db.connect()
    if args and args[0] != "--link":
        old, new = (a.upper() for a in args[:2])
        with conn:
            db.link_relist(conn, old, new, "cli", _user(conn))
        print("%s relisted as %s" % (old, new))
        return
    pairs = db.suggest_relists(conn)
    if args == ("--link",):
        user = _user(conn)
        with conn:
            for old, new in pairs:
                db.link_relist(conn, old, new, "cli", user)
        print("Linked %d relisted items." % len(pairs))
        return
    for old, new in pairs:
        print("  %s looks relisted as %s (same title)" % (old, new))
    print("%d suggested links; save them with: cli.py relists --link" % len(pairs))
    rows = conn.execute(ITEMS_SQL + " WHERE i.status IN (?, ?, ?) AND i.relisted_as IS NULL ORDER BY l.code, i.sku",
                        db.RELISTABLE).fetchall()
    suggested = {old for old, _ in pairs}
    print("\nNot relisted yet:")
    for r in rows:
        if r["sku"] not in suggested:
            print("  %-9s %-10s %-4s %s%s" % (r["sku"], r["status"], r["tote"] or "?", r["title"] or "",
                                           "  (missing)" if r["missing_since"] else ""))


def cmd_sales():
    s = sales.summary(db.connect())
    print("Since %(since)s: %(orders)d orders, %(items)d items, $%(sales).2f in item sales, $%(shipping).2f shipping paid by buyers" % s)
    print("Average sale $%.2f, median $%.2f; %d cancelled orders (%.0f%%)" % (s["average"], s["median"], s["cancelled"], 100 * s["cancel_rate"]))
    print("Last 7 days: %d items, $%.2f.  Last 30 days: %d items, $%.2f (sell-through %.0f%%)\n" % (
        s["last7"]["items"], s["last7"]["sales"], s["last30"]["items"], s["last30"]["sales"], 100 * s["sell_through_30"]))
    print("%-8s %7s %6s %10s %10s %8s %9s" % ("month", "orders", "items", "sales", "shipping", "avg", "cancelled"))
    for m in sales.by_month(db.connect()):
        print("%-8s %7d %6d %10.2f %10.2f %8.2f %9d" % (m["month"], m["orders"], m["items"], m["sales"], m["shipping"], m["average"], m["cancelled"]))


def cmd_draft_test(batch, *models):
    """Re-draft an existing batch with the Claude API and compare with the drafts on file. Saves nothing to the database."""
    import datetime
    import zipfile

    from ebay import drafter

    conn = db.connect()
    on_file = drafts.load(conn, batch)
    if on_file is None:
        raise SystemExit("No drafts in batch %s" % batch)
    zip_path = os.path.join(config.ROOT, on_file["source_zip"] or "")
    archive = zipfile.ZipFile(zip_path) if os.path.isfile(zip_path) else None
    claude = drafter.client()
    photos = drafts.photos_dir(batch)
    results = {}
    for name in models or ("sonnet",):
        model = drafter.MODELS.get(name, name)
        results[model] = []
        for item in on_file["items"]:
            divider = None
            if archive and item.get("divider_photo"):
                divider = io.BytesIO(archive.read(item["divider_photo"]))
            print("%s  %s ..." % (model, item["sku"]), end=" ", flush=True)
            try:
                new, run = drafter.draft(claude, model, divider, [os.path.join(photos, p) for p in item["photos"]])
            except Exception as e:  # keep going; the error goes in the report
                print("failed: %s" % e)
                results[model].append({"sku": item["sku"], "error": str(e)})
                continue
            print("%.1fs, %d in / %d out tokens, $%.4f" % (run["seconds"], run["input_tokens"], run["output_tokens"], run["cost_usd"]))
            results[model].append(dict(new, sku=item["sku"]))
    out = os.path.join(config.ROOT, "batches", batch, "api-test-%s.json" % datetime.datetime.now().strftime("%Y%m%d-%H%M"))
    with open(out, "w") as f:
        json.dump({"batch": batch, "on_file": on_file["items"], "results": results}, f, indent=2, default=str)
    report = _draft_test_report(on_file["items"], results)
    with open(out.replace(".json", ".txt"), "w") as f:
        f.write(report)
    print("\n" + report + "\nSaved %s (and .txt)" % out)


def _draft_test_report(on_file, results):
    lines = []
    for model, items in results.items():
        runs = [i["drafted_by"] for i in items if "drafted_by" in i]
        if runs:
            lines.append("%s: %d items, $%.4f total, $%.4f per item, %.1fs per item, %d input / %d output tokens per item" % (
                model, len(runs), sum(r["cost_usd"] for r in runs), sum(r["cost_usd"] for r in runs) / len(runs),
                sum(r["seconds"] for r in runs) / len(runs), sum(r["input_tokens"] for r in runs) // len(runs),
                sum(r["output_tokens"] for r in runs) // len(runs)))
    lines.append("")
    for hand in on_file:
        lines.append("=" * 100)
        lines.append("%s  (on file: %s)" % (hand["sku"], "skipped: " + hand["skip"] if hand.get("skip") else "published" if hand.get("published") else "draft"))
        lines.append("  on file   %-12s $%-7s cat %-7s %s" % (hand.get("condition"), hand.get("price"), hand.get("category_id"), hand.get("title")))
        for model, items in results.items():
            new = next(i for i in items if i["sku"] == hand["sku"])
            short = model.replace("claude-", "")
            if "error" in new:
                lines.append("  %-9s ERROR %s" % (short, new["error"]))
                continue
            lines.append("  %-9s %-12s $%-7s cat %-7s %s" % (short, new["condition"], new["price"], new["category_id"], new["title"]))
            lines.append("  %-9s whiteboard %r; category %s; comps %r" % ("", new["whiteboard_notes"], new["category"], new["comp_query"]))
            for q in new["needs_check"]:
                lines.append("  %-9s ? %s" % ("", q))
    return "\n".join(lines) + "\n"


def cmd_free_tote():
    print(db.suggest_tote(db.connect()))


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
        db.add_user(conn, args[0].lower(), *args[1:])
    if action == "password":
        db.user_id(conn, args[0])  # stops if there's no such user
        password = getpass.getpass("New web password for %s: " % args[0])
        if len(password) < 8 or password != getpass.getpass("Again: "):
            raise SystemExit("Passwords didn't match or were shorter than 8 characters; nothing changed.")
        db.set_password(conn, args[0], password)
        print("Password set for %s." % args[0])
        return
    for r in conn.execute("SELECT name, role, active, password_hash IS NOT NULL AS web FROM users ORDER BY name"):
        print("%-12s %-8s %-10s %s" % (r["name"], r["role"], "web login" if r["web"] else "no web login", "" if r["active"] else "(inactive)"))


def cmd_serve(*flags):
    import socket

    from web import create_app

    host = "0.0.0.0" if "--lan" in flags else "127.0.0.1"
    if host == "0.0.0.0":
        # The address this Mac uses on the local network (nothing is sent).
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        print("On the home wifi, open http://%s:5050" % s.getsockname()[0])
        s.close()
    create_app().run(host=host, port=5050, threaded=True)


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
    "consolidate": cmd_consolidate,
    "free-tote": cmd_free_tote,
    "import-drafts": cmd_import_drafts,
    "export-drafts": cmd_export_drafts,
    "missing": cmd_missing,
    "found": cmd_found,
    "relists": cmd_relists,
    "sales": cmd_sales,
    "draft-test": cmd_draft_test,
    "next-sku": cmd_next_sku,
    "export": cmd_export,
    "user": cmd_user,
    "serve": cmd_serve,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    try:
        COMMANDS[sys.argv[1]](*sys.argv[2:])
    except ValueError as e:  # e.g. a bad tote code
        raise SystemExit(str(e))
