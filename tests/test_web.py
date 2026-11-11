"""End-to-end test of the web app against a throwaway copy of the real database.

    .venv/bin/python tests/test_web.py

Copies data/ebay.db and batches/batch1 into a temp folder, signs in two test users and
goes through drafts, items, totes, relists, the pick list, sales and activity. eBay and
Claude are never called: publish and comps are replaced with fakes. Some checks assume
today's data (batch1's drafts, MV-01074 relisted as MV-00852, an item waiting to ship);
update them when the data moves on.
"""

import json
import os
import re
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S = tempfile.mkdtemp(prefix="ebay-webtest-")
shutil.copy(os.path.join(ROOT, "data", "ebay.db"), os.path.join(S, "ebay.db"))
shutil.copytree(os.path.join(ROOT, "batches", "batch1"), os.path.join(S, "batches", "batch1"))

from ebay import comps, db, drafts, listings, orders  # noqa: E402
from web import create_app  # noqa: E402
from werkzeug.datastructures import MultiDict  # noqa: E402
conn = db.connect(S + "/ebay.db"); db.set_password(conn, "matt", "testpass123"); db.add_user(conn, "sam", "lister"); db.set_password(conn, "sam", "testpass456"); conn.close()

calls = []
def fake_publish(conn, batch, skus, user=None, actor="cli", log=print):
    calls.append((skus, user, actor)); log("%s: published as listing 999 (fake)" % sorted(skus))
listings.publish = fake_publish
comps.refresh_active = lambda conn, batch, skus, log=print, user=None, actor="cli": calls.append(("comps", skus, user))
app = create_app(db_path=S + "/ebay.db", batches_dir=S + "/batches", inbox_dir=S + "/inbox")

def csrf(html): return re.search(r'name="csrf" value="([^"]+)"', html).group(1)
def check(resp, code=200, has=()):
    assert resp.status_code == code, (resp.status_code, resp.data[:500])
    if resp.mimetype != "text/html": return None
    body = resp.get_data(as_text=True)
    for h in has: assert h in body, (h, body[:3000])
    return body
def sign_in(name, pw):
    c = app.test_client()
    tok = csrf(check(c.get("/login")))
    r = c.post("/login", data={"csrf": tok, "name": name, "password": pw}); assert r.status_code == 302, r.status_code
    return c, csrf(c.get("/").get_data(as_text=True))
def fresh(): return db.connect(S + "/ebay.db")

# --- sign-in ---
START = fresh().execute("SELECT COALESCE(MAX(id), 0) FROM events").fetchone()[0]  # only count events from this run
c = app.test_client()
r = c.get("/"); assert r.status_code == 302 and "/login" in r.location
tok = csrf(check(c.get("/login")))
check(c.post("/login", data={"csrf": tok, "name": "matt", "password": "wrong"}), has=["Wrong name or password"])
assert c.post("/login", data={"csrf": "bad", "name": "matt", "password": "testpass123"}).status_code == 400
r = c.post("/login?next=//evil.com", data={"csrf": tok, "name": "Matt", "password": "testpass123"}); assert r.location == "/", r.location
c, tok = sign_in("matt", "testpass123")
sam, stok = sign_in("sam", "testpass456")
ev = [r["action"] for r in fresh().execute("select action from events where entity='user' and id > ? order by id", (START,))]
assert ev.count("signed_in") == 3 and "sign_in_failed" in ev, ev

# --- drafts from the database ---
check(c.get("/"), has=["Hi matt", "To ship", "batch1"])
check(c.get("/drafts"), has=["batch1"])
check(c.get("/drafts/batch1"), has=["MV-00005", "needs work", "published"])
check(c.get("/drafts/batch1/MV-00005"), has=["Before publishing", "eapot", "Estimate $12.80", 'name="version"'])
check(c.get("/drafts/batch1/MV-00001"), has=["Published as listing", "disabled"])
assert c.get("/drafts/nope").status_code == 404 and c.get("/drafts/batch1/MV-99999").status_code == 404
assert c.get("/photos/batch1/MV-00005-1.jpg").mimetype == "image/jpeg"
assert c.get("/photos/batch1/../drafts.json").status_code == 404

_, item = drafts.get(fresh(), "MV-00005")
form = {"csrf": tok, "version": str(item["_version"]), "title": item["title"], "price": "14.5", "condition": item["condition"],
        "condition_description": item["condition_description"], "category_id": item["category_id"],
        "description": item["description"], "aspects": "\n".join("%s: %s" % (k, " | ".join(v)) for k, v in item["aspects"].items()),
        "needs_check": "", "comp_query": item.get("comp_query", ""), "skip": "",
        **{k: str(v) for k, v in item["package"].items()}}
check(c.post("/drafts/batch1/MV-00005", data={**form, "price": "abc"}), has=["Price must be a number"])
assert drafts.get(fresh(), "MV-00005")[1]["price"] == item["price"], "bad price must not save"
# publish blocked while a question is open
check(c.post("/drafts/batch1/MV-00005", data={**form, "needs_check": "Is the lid there?", "then": "publish"}, follow_redirects=True), has=["not published: Open question"])
assert not calls
# sam saved in between -> matt's stale save is refused
v = drafts.get(fresh(), "MV-00005")[1]["_version"]
check(sam.post("/drafts/batch1/MV-00005", data={**form, "csrf": stok, "version": str(v), "needs_check": "Sam's question"}, follow_redirects=True), has=["Saved."])
check(c.post("/drafts/batch1/MV-00005", data={**form, "version": str(v), "then": "publish"}), has=["sam saved this draft", "Save again"])
assert drafts.get(fresh(), "MV-00005")[1]["needs_check"] == ["Sam's question"] and not calls
v = drafts.get(fresh(), "MV-00005")[1]["_version"]
r = c.post("/drafts/batch1/MV-00005", data={**form, "version": str(v), "then": "publish"}, follow_redirects=True)
check(r, has=["Saved.", "published as listing 999"])
d = drafts.get(fresh(), "MV-00005")[1]
assert d["price"] == 14.5 and d["needs_check"] == [] and d["aspects"] == item["aspects"] and d["description"] == item["description"]
assert fresh().execute("select price, status from items where sku='MV-00005'").fetchone()[0] == 14.5
assert calls[-1] == ({"MV-00005"}, 1, "web"), calls
check(c.post("/drafts/batch1/MV-00001", data={**form, "then": "save"}, follow_redirects=True), has=["Already published"])
check(c.post("/drafts/batch1/publish", data={"csrf": tok}, follow_redirects=True), has=["published as listing 999"])
check(c.post("/drafts/batch1/MV-00005/comps", data={"csrf": tok}, follow_redirects=True), has=["Price estimate updated"])
assert calls[-1] == ("comps", {"MV-00005"}, 1)
edits = fresh().execute("select u.name, e.data from events e join users u on u.id=e.user_id where e.entity_id='MV-00005' and e.action='edited' and e.id > ? order by e.id", (START,)).fetchall()
assert [e[0] for e in edits] == ["matt", "sam", "matt"], [tuple(e) for e in edits]

# --- items, missing, relists ---
to_ship = orders.pick_list(fresh())
assert to_ship, "these checks need at least one sold, unshipped item"
SHIP = to_ship[0]["sku"]
check(c.get("/items?q=teapot"), has=["MV-00005"])
check(c.get("/items?status=CANCELLED"), has=["MV-01385"])
check(c.get("/items/MV-01074"), has=["Relisted as", "MV-00852"])
check(c.get("/items/MV-00852"), has=["Relist of", "MV-01074"])
check(c.get("/items/MV-01385"), has=['name="relisted_as"'])
check(c.post("/items/MV-01385", data={"csrf": tok, "relisted_as": "MV-99999", "status": "CANCELLED"}, follow_redirects=True), has=["Unknown SKU MV-99999"])
check(c.get("/relists"), has=["Not relisted yet", "MV-01385"])
check(c.post("/items/%s" % SHIP, data={"csrf": tok, "tote": "e3", "status": "BOUGHT", "note": ""}, follow_redirects=True), has=["Moved to E3"])
check(c.post("/items/%s/missing" % SHIP, data={"csrf": tok, "missing": "1"}, follow_redirects=True), has=["missing since", "Found it"])
check(c.get("/picklist"), has=[SHIP, "missing"])
check(c.get("/items?status=MISSING"), has=[SHIP])
check(sam.post("/items/%s/missing" % SHIP, data={"csrf": stok, "missing": "0", "back": "/totes/E3"}), code=302)
assert fresh().execute("select missing_since from items where sku = ?", (SHIP,)).fetchone()[0] is None
check(c.get("/items/%s" % SHIP), has=["matt", "sam", "missing", "found", "moved"])

# --- totes ---
conn = fresh()
def onhand(code): return db.on_hand(fresh(), code)
FREE = db.suggest_tote(conn)
check(c.get("/totes"), has=["Next free tote ID: <strong>%s</strong>" % FREE, "Combine totes", "Checked"])
check(c.get("/totes/consolidate?codes=G3", follow_redirects=True), has=["at least two totes"])
check(c.get("/totes/consolidate?codes=G3+zz", follow_redirects=True), has=["isn&#39;t a tote code"])
g3, c1 = onhand("G3"), onhand("C1")
check(c.get("/totes/consolidate?codes=g3 c1"), has=["Combine G3 + C1", "labelled <strong>%s</strong>" % FREE])
check(c.post("/totes/consolidate", data={"csrf": tok, "codes": "G3 C1", "into": "custom", "custom": "B2"}, follow_redirects=True), has=["B2 already has items"])
check(sam.post("/totes/consolidate", data={"csrf": stok, "codes": "G3 C1", "into": FREE}, follow_redirects=True), has=["Moved %d items into %s" % (len(g3) + len(c1), FREE)])
assert sorted(onhand(FREE)) == sorted(g3 + c1) and not onhand("G3") and not onhand("C1")
t = onhand(FREE)
check(c.get("/totes/" + FREE), has=["Move to", "to_" + t[0], "Never checked", ">Missing<"])
check(c.post("/totes/" + FREE, data={"csrf": tok, "to_" + t[0]: "t9", "to_" + t[1]: "t9", "to_" + t[2]: "u7"}, follow_redirects=True), has=["Moved 3 items: 2 to T9, 1 to U7"])
check(c.post("/totes/" + FREE, data={"csrf": tok, "to_" + t[3]: "bad code"}, follow_redirects=True), has=["isn&#39;t a tote code"])
check(c.post("/totes/" + FREE, data={"csrf": tok, "to_" + SHIP: "T9"}, follow_redirects=True), has=["Not in %s: %s" % (FREE, SHIP)])
check(c.post("/items/%s/missing" % t[3], data={"csrf": tok, "missing": "1", "back": "/totes/" + FREE}, follow_redirects=True), has=["Found"])
check(c.post("/totes/%s/checked" % FREE, data={"csrf": tok}, follow_redirects=True), has=["Last checked", "by matt"])
loc = fresh().execute("select e.action, u.name, e.data from events e join users u on u.id=e.user_id where entity='location' and entity_id in ('G3', 'C1', ?) and e.id > ? order by e.id", (FREE, START)).fetchall()
assert [(r[0], r[1]) for r in loc] == [("merged", "sam"), ("merged", "sam"), ("split", "matt"), ("checked", "matt")], [tuple(r) for r in loc]
assert json.loads(loc[-1][2]) == {"on_hand": len(t) - 3, "missing": 1}, loc[-1][2]
check(c.get("/totes"), has=[FREE])

# --- sorting ---
def prices(html): return [float(x.replace(",", "")) for x in re.findall(r'<td class="num">\$([\d,]+\.\d\d)</td>', html)]
def skus(html): return re.findall(r'href="/items/(MV-\d+)"', html)
p = prices(check(c.get("/items?status=LISTED&sort=price&dir=desc"), has=["▼"]))
assert len(p) > 10 and p == sorted(p, reverse=True), p[:10]
p = prices(check(c.get("/items?status=LISTED&sort=price&dir=asc"), has=["▲"]))
assert p == sorted(p), p[:10]
k = skus(check(c.get("/items?status=LISTED")))
assert k == sorted(k, reverse=True), "default is newest SKU first"
body = check(c.get("/items?status=LISTED&sort=price"))
assert "sort=price&amp;dir=desc" in body or "dir=desc&amp;sort=price" in body or "dir=desc" in body, "clicking the current column flips it"
assert "status=LISTED" in re.search(r'<a class="sort" href="([^"]+)"', body).group(1), "sort links keep the filters"
check(c.get("/items?sort=nonsense;drop table items&dir=sideways"))  # unknown key falls back, no SQL from the URL
assert fresh().execute("select count(*) from items").fetchone()[0] > 0
t = check(c.get("/totes?sort=on_hand&dir=desc"), has=["Listed value", 'class="total"'])
listed = [int(x) for x in re.findall(r'<a href="/totes/[A-Z0-9]+">[A-Z0-9]+</a>(?:[^<]|<span[^>]*>[^<]*</span>)*</td>\s*<td class="num">(\d+)</td>', t)]
assert len(listed) > 5 and listed == sorted(listed, reverse=True), listed[:10]
tt = check(c.get("/totes/OT?sort=title"))
titles = re.findall(r'<td class="title">([^<]*)</td>', tt)
assert len(titles) > 5 and [x.lower() for x in titles] == sorted(x.lower() for x in titles), titles[:5]

# --- item filters ---
def count(html): return int(re.search(r'<h1>Items <span class="muted">(\d+)', html).group(1))
db_ = fresh()
n_listed = db_.execute("select count(*) from items where status = 'LISTED'").fetchone()[0]
assert count(check(c.get("/items?status=LISTED"), has=["Days listed", "Last change", "Clear"])) == n_listed
assert count(check(c.get("/items"))) == db_.execute("select count(*) from items").fetchone()[0]
n = db_.execute("select count(*) from items i join locations l on l.id = i.location_id where l.code = 'OT' and i.status = 'LISTED' and i.price between 10 and 20").fetchone()[0]
html = check(c.get("/items?status=LISTED&tote=ot&min_price=10&max_price=20"))
assert count(html) == n and n > 0, (count(html), n)
assert all(10 <= x <= 20 for x in prices(html))
assert count(check(c.get("/items?tote=?"))) == db_.execute("select count(*) from items where location_id is null").fetchone()[0]
n90 = db_.execute("select count(*) from items i where status = 'LISTED' and julianday('now') - julianday((select max(listed_at) from listings where sku = i.sku)) >= 90").fetchone()[0]
html = check(c.get("/items?status=LISTED&min_days=90&sort=days&dir=asc"))
assert count(html) == n90 and n90 > 0
days = [int(x) for x in re.findall(r'<td class="num opt">(\d+)</td>\s*<td class="nowrap small opt">', html)]
assert days and min(days) >= 90 and days == sorted(days), days[:5]
assert count(check(c.get("/items?status=LISTED&min_price=abc"))) == n_listed, "a bad number is ignored, not an error"

# --- bulk actions ---
check(c.get("/items?tote=UN"), has=["Tick all", "Move to tote", "Set status"])
un = db.on_hand(fresh(), "UN")[:3]
assert len(un) == 3
def bulk(data, client=None, token=None):
    client, token = client or c, token or tok
    pairs = [("csrf", token), ("back", "/items?tote=UN")] + [("sku", x) for x in data.pop("skus", [])] + list(data.items())
    return check(client.post("/items/bulk", data=MultiDict(pairs), follow_redirects=True))
assert "Moved 2 items to W9" in bulk({"skus": un[:2], "action": "move", "tote": "w9"})
assert db.on_hand(fresh(), "W9") == sorted(un[:2])
assert "Unknown SKU: MV-99999. Nothing was changed." in bulk({"skus": [un[2], "MV-99999"], "action": "move", "tote": "W9"})
assert un[2] in db.on_hand(fresh(), "UN"), "all or nothing"
assert "Nothing was changed" in bulk({"skus": [un[2]], "action": "move", "tote": "not a tote"})
assert "Tick at least one item" in bulk({"action": "move", "tote": "W9"})
assert "Pick a status" in bulk({"skus": un, "action": "status", "status": ""})
assert "Marked 3 items missing" in bulk({"skus": un, "action": "missing"}, sam, stok)
assert fresh().execute("select count(*) from items where sku in (?, ?, ?) and missing_since is not null", un).fetchone()[0] == 3
assert "Marked 3 items found" in bulk({"skus": un, "action": "found"})
before = {r[0]: r[1] for r in fresh().execute("select sku, status from items where sku in (?, ?, ?)", un)}
assert "Set 3 items to ENDED" in bulk({"skus": un, "action": "status", "status": "ENDED", "note": "test bulk"})
ev = fresh().execute("select u.name, e.data from events e join users u on u.id = e.user_id where e.entity_id = ? and e.action = 'status' and e.id > ? order by e.id desc", (un[0], START)).fetchone()
assert ev[0] == "matt" and json.loads(ev[1]) == {"note": "test bulk", "manual": True, "bulk": 3}, tuple(ev)
missing_ev = fresh().execute("select u.name from events e join users u on u.id = e.user_id where e.entity_id = ? and e.action = 'missing' and e.id > ?", (un[0], START)).fetchone()
assert missing_ev[0] == "sam"
# the tote page has the same bar, posting the same form
on_w9 = db.on_hand(fresh(), "W9")
html = check(c.get("/totes/W9"), has=["Tick all", "Move to tote"])
assert 'name="sku" value="%s"' % on_w9[0] in html
r = c.post("/items/bulk", data=MultiDict([("csrf", tok), ("back", "/totes/W9"), ("sku", on_w9[0]), ("action", "move"), ("tote", "W8")]))
assert r.status_code == 302 and r.location.endswith("/totes/W9")
assert db.on_hand(fresh(), "W8") == [on_w9[0]]

# --- needs attention ---
home = check(c.get("/"), has=["Needs attention", "in UN, waiting for a tote", "listed 90+ days"])
links = re.findall(r'<li><a href="([^"]+)"><strong>(\d+)</strong> ([^<]+)</a></li>', home)
assert len(links) >= 4, links
for link, n, what in links:
    page = check(c.get(link.replace("&amp;", "&")))
    if "/items" in link:
        assert count(page) == int(n), (what, count(page), n)
    elif "/totes" in link:
        assert len(re.findall(r'<td><a href="/totes/', page)) == int(n), what
    elif "/relists" in link:
        assert re.search(r"Not relisted yet \((\d+)\)", page)
html = check(c.get("/totes?unchecked=1"), has=["nobody has checked yet"])
assert "/totes/OT" not in html and "/totes/UN" not in html, "places aren't totes"
assert ("/totes/%s\"" % FREE) not in html, "the tote checked earlier in this run isn't listed"
assert count(check(c.get("/items?status=ON_HAND"))) == fresh().execute("select count(*) from items where status not in ('SENT_TO_POST_OFFICE', 'DELIVERED')").fetchone()[0]

# --- tote labels ---
html = check(c.get("/labels?codes=w9 ot"), has=['<div class="label-code">W9</div>', '<div class="label-code">OT</div>', 'class="qr"', "Print"])
import web as web_module  # noqa: E402
assert html.count('<svg') == 2
assert str(web_module._qr_svg("http://localhost/totes/W9")) in html, "the QR code opens the tote's page"
assert "isn&#39;t a tote code" in check(c.get("/labels?codes=zz9", follow_redirects=True))
in_use = len(db.totes_in_use(fresh()))
assert check(c.get("/labels")).count('class="label-code"') == in_use
check(c.get("/totes/W9"), has=["Print label"])
assert "4in 6in" not in open(os.path.join(ROOT, "web", "static", "style.css")).read(), "label page size must not leak into other printouts"

# --- editing our records, price suggestions ---
listed = fresh().execute("select sku, price from items where status = 'LISTED' and price is not null order by sku limit 1").fetchone()
L, old_price = listed[0], listed[1]
check(c.get("/items/" + L), has=["Our records", "Price on eBay", "follow the eBay listing"])
check(c.post("/items/%s/edit" % L, data={"csrf": tok, "notes": "box dented", "cost": "$3.50", "cost_note": "lot of 10"}, follow_redirects=True),
      has=["Saved: cost, cost_note, notes", "$3.50", "lot of 10", "notes: empty → box dented", "cost: empty → 3.50"])
check(c.post("/items/%s/edit" % L, data={"csrf": tok, "notes": "box dented", "cost": "abc"}, follow_redirects=True), has=["Cost must be a number"])
check(c.post("/items/%s/edit" % L, data={"csrf": tok, "notes": "box dented", "cost": "3.5", "cost_note": "lot of 10", "price": "1.00"}, follow_redirects=True),
      has=["its title and price follow eBay"])
assert fresh().execute("select price from items where sku = ?", (L,)).fetchone()[0] == old_price
check(c.post("/items/%s/edit" % L, data={"csrf": tok, "notes": "", "cost": "", "cost_note": "lot of 10"}, follow_redirects=True), has=["unknown"])
assert fresh().execute("select cost from items where sku = ?", (L,)).fetchone()[0] is None, "empty cost means unknown"
C = "MV-01385"  # cancelled, not relisted: its title and price are ours
check(c.get("/items/" + C), has=['name="title"', 'name="price"'])
check(c.post("/items/%s/edit" % C, data={"csrf": tok, "title": "NVIDIA SHIELD K1 tablet (2 units?)", "price": "45", "notes": "", "cost": "", "cost_note": ""}, follow_redirects=True),
      has=["Saved: price, title"])
check(c.post("/items/%s/edit" % C, data={"csrf": tok, "title": "x" * 81, "price": "45"}, follow_redirects=True), has=["1 to 80 characters"])
# suggestions
check(c.post("/items/%s/suggest" % L, data={"csrf": tok, "price": "0"}, follow_redirects=True), has=["more than $0"])
check(sam.post("/items/%s/suggest" % L, data={"csrf": stok, "price": "$7.99", "note": "120 days"}, follow_redirects=True),
      has=["Price suggestion recorded", "Suggested price", "$7.99", "Applied $7.99 on eBay"])
html = check(c.get("/items?status=SUGGESTED"))
assert count(html) == 1 and L in html and "→ $7.99" in html
assert "price suggestions to change on eBay" in check(c.get("/"))
check(c.post("/items/%s/applied" % L, data={"csrf": tok, "price": "7.99"}, follow_redirects=True), has=["Recorded the new eBay price of $7.99"])
assert fresh().execute("select price from items where sku = ?", (L,)).fetchone()[0] == 7.99
assert count(check(c.get("/items?status=SUGGESTED"))) == 0, "applying clears the suggestion"
who = [tuple(r) for r in fresh().execute("select e.action, u.name from events e join users u on u.id = e.user_id where e.entity_id = ? and e.id > ? order by e.id", (L, START))]
assert ("price_suggested", "sam") in who and ("price_applied", "matt") in who and ("edited", "matt") in who, who

# --- bulk price drop suggestions ---
stale = [r[0] for r in fresh().execute("select i.sku from items i where i.status = 'LISTED' and i.price >= 10 and i.sku <> ? order by i.sku limit 3", (L,))]
assert "Suggested a 20% lower price for 3 items" in bulk({"skus": stale, "action": "drop", "percent": "20"})
for sku in stale:
    price = fresh().execute("select price from items where sku = ?", (sku,)).fetchone()[0]
    sug = fresh().execute("select json_extract(data, '$.price') from events where entity_id = ? and action = 'price_suggested' order by id desc", (sku,)).fetchone()[0]
    assert sug == db.price_drop(price, 20) and str(sug).endswith(".99") and sug <= price * 0.8, (sku, price, sug)
assert "between 1 and 89" in bulk({"skus": stale, "action": "drop", "percent": "95"})
assert "Only listed items" in bulk({"skus": [C], "action": "drop", "percent": "10"})
assert count(check(c.get("/items?status=SUGGESTED"))) == 3

# --- paging and CSV ---
import csv as csv_module, io as io_module  # noqa: E402
p1 = check(c.get("/items?status=LISTED&sort=price&dir=desc"), has=["Next →", "Download CSV"])
p2 = check(c.get("/items?status=LISTED&sort=price&dir=desc&page=2"), has=["← Previous", "201–"])
assert set(skus(p1)).isdisjoint(skus(p2)) and prices(p1)[-1] >= prices(p2)[0], "page 2 continues page 1"
assert "status=LISTED" in re.search(r'href="([^"]*page=2[^"]*)"', p1).group(1).replace("&amp;", "&")
assert "page 1 of" in check(c.get("/items?status=LISTED&page=-4")) and "Next" not in check(c.get("/items?status=LISTED&page=999")).split("Download CSV")[0][-200:]
r = c.get("/items.csv?status=LISTED&tote=OT")
assert r.status_code == 200 and r.mimetype == "text/csv" and "attachment" in r.headers["Content-Disposition"]
rows = list(csv_module.DictReader(io_module.StringIO(r.get_data(as_text=True))))
assert len(rows) == count(check(c.get("/items?status=LISTED&tote=OT"))) and all(x["tote"] == "OT" and x["status"] == "LISTED" for x in rows)

# --- quick find ---
assert c.get("/find?q=1385").location.endswith("/items/MV-01385")
assert c.get("/find?q=mv-1385").location.endswith("/items/MV-01385")
assert c.get("/find?q=MV-01385").location.endswith("/items/MV-01385")
assert c.get("/find?q=ot").location.endswith("/totes/OT")
assert c.get("/find?q=e2").location.endswith("/totes/E2")
assert "/items?q=teapot" in c.get("/find?q=teapot").location
assert "/items?q=Z99" in c.get("/find?q=Z99").location, "a tote code with nothing in it falls back to search"
check(c.get("/"), has=['action="/find"'])

# --- sign-in lockout ---
x = app.test_client()
xt = csrf(check(x.get("/login")))
for _ in range(10):
    check(x.post("/login", data={"csrf": xt, "name": "sam", "password": "nope"}), has=["Wrong name or password"])
check(x.post("/login", data={"csrf": xt, "name": "sam", "password": "testpass456"}), code=429, has=["Too many wrong passwords"])
check(x.post("/login", data={"csrf": xt, "name": "matt", "password": "testpass123"}), code=302)  # other names aren't affected

# --- photos on the item page ---
html = check(c.get("/items/MV-00001"), has=['class="photos small-photos"', "/photos/batch1/MV-00001-1.jpg"])
no_draft = fresh().execute("select sku from items where sku not in (select sku from drafts) limit 1").fetchone()[0]
assert 'small-photos' not in check(c.get("/items/" + no_draft)), "items without a draft have no photo strip"

# --- tote history ---
html = check(c.get("/totes/" + FREE), has=["Recent changes", "split", "checked", "merged"])
assert "to: T9" in html or "T9" in html
html = check(c.get("/totes/W9"), has=["Recent changes", "moved", "to: W9", "to: W8"])

# --- quick move ---
check(c.get("/move"), has=["Quick move", 'name="tote"'])
un_item = db.on_hand(fresh(), "UN")[0]
num = str(int(un_item[3:]))
check(c.post("/move", data={"csrf": tok, "tote": "q3", "sku": num}, follow_redirects=True),
      has=["%s → Q3 (was UN)" % un_item, "You moved into Q3", 'value="Q3"'])
assert un_item in db.on_hand(fresh(), "Q3")
check(c.post("/move", data={"csrf": tok, "tote": "Q3", "sku": un_item.lower()}, follow_redirects=True), has=["already in Q3"])
check(c.post("/move", data={"csrf": tok, "tote": "Q3", "sku": "99999"}, follow_redirects=True), has=["No item MV-99999"])
check(c.post("/move", data={"csrf": tok, "tote": "nope", "sku": num}, follow_redirects=True), has=["isn&#39;t a tote code"])
assert c.get("/find?q=mv920").location.endswith("/items/MV-00920")

# --- photo upload ---
from io import BytesIO  # noqa: E402
check(c.get("/drafts/upload"), has=["Upload a tote", 'type="file"'])
photo = open(os.path.join(S, "batches", "batch1", "photos", "MV-00005-1.jpg"), "rb").read()
r = c.post("/drafts/upload", data={"csrf": tok, "tote": "t5", "photos": [(BytesIO(photo), "IMG_1.JPG"), (BytesIO(photo), "../../evil name.jpg")]},
           content_type="multipart/form-data", follow_redirects=True)
check(r, has=["Uploaded 2 files as batch t5-", "Waiting to be drafted"])
up = [d for d in os.listdir(os.path.join(S, "inbox")) if d.startswith("t5-")]
assert len(up) == 1 and sorted(os.listdir(os.path.join(S, "inbox", up[0]))) == ["001-IMG_1.JPG", "002-evil_name.jpg"]
b = fresh().execute("select b.source, u.name from batches b join users u on u.id = b.photographer_id where b.name = ?", (up[0],)).fetchone()
assert tuple(b) == ("upload", "matt")
check(c.post("/drafts/upload", data={"csrf": tok, "tote": "T5", "photos": [(BytesIO(b"x"), "notes.txt")]}, content_type="multipart/form-data", follow_redirects=True),
      has=["not notes.txt"])
check(c.post("/drafts/upload", data={"csrf": tok, "tote": "bad tote", "photos": [(BytesIO(photo), "a.jpg")]}, content_type="multipart/form-data", follow_redirects=True),
      has=["isn&#39;t a tote code"])
assert len([d for d in os.listdir(os.path.join(S, "inbox")) if d.startswith("t5-")]) == 1

# --- counting a tote ---
T = next(r[0] for r in fresh().execute("select l.code from locations l join items i on i.location_id = l.id"
                                       " where l.kind = 'tote' and i.status not in (?, ?) and i.missing_since is null"
                                       " group by l.code having count(*) >= 3 order by l.code", db.GONE))
check(c.get("/totes/%s/count" % T), has=["Count " + T, 'name="here"', "Save count"])
in_t = db.on_hand(fresh(), T)
outside = next(r[0] for r in fresh().execute("select i.sku from items i join locations l on l.id = i.location_id"
                                            " where l.code <> ?"
                                            " and i.status = 'LISTED' limit 1", (T,)))
check(c.post("/totes/%s/count" % T, data=MultiDict([("csrf", tok), ("extra", "MV-99999")]), follow_redirects=True), has=["Unknown SKU: MV-99999"])
assert db.on_hand(fresh(), T) == in_t, "a bad SKU changes nothing"
check(c.post("/totes/%s/count" % T, data=MultiDict([("csrf", tok), ("extra", outside.lower())] + [("here", x) for x in in_t[1:]]),
             follow_redirects=True), has=[T + " counted: %d here, 1 missing, 1 moved in." % (len(in_t) - 1)])
assert fresh().execute("select missing_since from items where sku=?", (in_t[0],)).fetchone()[0], "unticked is missing"
assert outside in db.on_hand(fresh(), T)
assert fresh().execute("select checked_at from locations where code=?", (T,)).fetchone()[0]
check(c.post("/totes/%s/count" % T, data=MultiDict([("csrf", tok)] + [("here", x) for x in db.on_hand(fresh(), T)]),
             follow_redirects=True), has=["1 found again"])
assert not fresh().execute("select missing_since from items where sku=?", (in_t[0],)).fetchone()[0]
check(c.get("/totes/" + T), has=["Count items", "Last checked"])

# --- nav shows where you are ---
for page, label in [("/items", "Items"), ("/items/MV-00005", "Items"), ("/totes", "Totes"), ("/picklist", "Pick list"), ("/checks", "Checks")]:
    current = re.findall(r'aria-current="page">([^<]+)<', c.get(page).get_data(as_text=True))
    assert current == [label], (page, current)

# --- data checks ---
conn = fresh()
saved = [tuple(r) for r in conn.execute("SELECT sku, title, status, price FROM items WHERE sku IN ('MV-00005', 'MV-01385')")]
with conn:
    conn.execute("UPDATE items SET title = ? WHERE sku = 'MV-00005'", ("x" * 85,))
    conn.execute("UPDATE items SET status = 'LISTED', price = NULL WHERE sku = 'MV-01385'")
check(c.get("/checks"), has=["Title over 80 characters", "85 characters", "MV-00005", "Listed with no price", "MV-01385", "No problems:"])
check(c.get("/"), has=["record problem", "/checks"])
with conn:
    conn.executemany("UPDATE items SET title = ?, status = ?, price = ? WHERE sku = ?", [(t, st, p, sku) for sku, t, st, p in saved])
assert "85 characters" not in c.get("/checks").get_data(as_text=True)

# --- people and roles ---
check(c.get("/users"), has=["People", "Add someone", "sam", "lister"])
check(sam.get("/users"), has=["People"])
assert "Add someone" not in sam.get("/users").get_data(as_text=True), "only admins see the controls"
assert sam.post("/users", data={"csrf": stok, "action": "add", "name": "eve", "role": "admin", "password": "x" * 10}).status_code == 403
check(c.post("/users", data={"csrf": tok, "action": "add", "name": "Kim", "role": "shipper", "password": "short"}, follow_redirects=True), has=["at least 8"])
check(c.post("/users", data={"csrf": tok, "action": "add", "name": "Kim", "role": "shipper", "password": "kimpass123"}, follow_redirects=True), has=["Added kim (shipper)"])
check(c.post("/users", data={"csrf": tok, "action": "add", "name": "kim", "role": "shipper", "password": "kimpass123"}, follow_redirects=True), has=["already a kim"])
kim, ktok = sign_in("kim", "kimpass123")
# shippers can't publish
assert kim.post("/drafts/batch1/publish", data={"csrf": ktok}).status_code == 403
check(c.post("/users", data={"csrf": tok, "action": "disable", "name": "kim"}, follow_redirects=True), has=["turned off"])
assert kim.get("/").status_code == 302, "a turned-off account is signed out"
check(c.post("/users", data={"csrf": tok, "action": "disable", "name": "matt"}, follow_redirects=True), has=["your own account"])
check(c.post("/users", data={"csrf": tok, "action": "role", "name": "matt", "role": "lister"}, follow_redirects=True), has=["your own admin role"])
check(c.post("/users", data={"csrf": tok, "action": "enable", "name": "kim"}, follow_redirects=True), has=["back on"])
check(c.post("/users", data={"csrf": tok, "action": "password", "name": "kim", "password": "newkimpass1"}, follow_redirects=True), has=["New password set"])
sign_in("kim", "newkimpass1")
assert [r[0] for r in fresh().execute("select action from events where entity = 'user' and entity_id = 'kim' and action like 'user_%' order by id")] == ["user_add", "user_disable", "user_enable", "user_password"]

# --- shipping, numbers, activity ---
check(c.post("/picklist/status", data={"csrf": tok, "sku": SHIP, "status": "PACKAGED", "days": ""}, follow_redirects=True), has=["packaged"])
assert c.post("/picklist/status", data={"csrf": tok, "sku": SHIP, "status": "DELIVERED"}).status_code == 400
check(c.get("/picklist"), has=['form="pickbulk"', "Dropped off"])
check(c.post("/picklist/status", data={"csrf": tok, "status": "PACKAGED"}, follow_redirects=True), has=["Tick the items first"])
waiting = [r["sku"] for r in orders.pick_list(fresh())][:2]
check(c.post("/picklist/status", data=MultiDict([("csrf", tok), ("status", "LABEL_PRINTED")] + [("sku", x) for x in waiting + ["MV-00005"]]), follow_redirects=True),
      has=["%d marked label printed. 1 skipped" % len(waiting)])
assert all(fresh().execute("select status from items where sku=?", (s,)).fetchone()[0] == "LABEL_PRINTED" for s in waiting)
assert fresh().execute("select status from items where sku='MV-00005'").fetchone()[0] != "LABEL_PRINTED"
check(c.get("/sales"), has=["Item sales", "By month", "By sale price", "Listed now"])
check(c.get("/activity"), has=["Last 30 days by person", "matt", "sam", "signed in"])
check(c.get("/activity?who=sam"), has=["merged"])
check(c.get("/activity?q=" + SHIP), has=["missing", "found"])
nouser = fresh().execute("select count(*) from events where user_id is null and actor = 'web' and action <> 'sign_in_failed' and id > ?", (START,)).fetchone()[0]
assert nouser == 0, nouser
c.post("/logout", data={"csrf": tok}); assert c.get("/").status_code == 302
print("all checks passed")
shutil.rmtree(S)
