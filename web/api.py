"""The JSON API for the phone apps (see docs/API.md for the contract).

It shares the website's database functions and queries so the two can't drift apart. Requests sign in
with a bearer token instead of the website's session cookie; browsers never send those on their own,
so the API needs no CSRF token.
"""

import functools
import hashlib
import os
import re
import secrets

from flask import Blueprint, g, jsonify, request, send_from_directory, url_for
from werkzeug.security import check_password_hash

from ebay import api as ebay_api, capture, db, orders, trading

from . import (ITEMS_SQL, ITEM_SORTS, MAX_FAILED_SIGN_INS, LOCKOUT_MINUTES, TOTE_SORTS, _float, _item_filters,
               _parse_json, _password_mark, _sku, count_tote, edit_changes, item_action, order_by, ship,
               sign_in_failures)

blueprint = Blueprint("api", __name__, url_prefix="/api/v1")

MAX_LIMIT = 500
ERROR_CODES = {400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "bad_request",
               409: "conflict", 413: "bad_request", 429: "too_many_attempts", 500: "server_error"}


def error(status, message, code=None):
    """The one error shape: {"error": {"code": ..., "message": ...}} with a plain-English message."""
    return jsonify({"error": {"code": code or ERROR_CODES.get(status, "error"), "message": message}}), status


def _hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def token_required(view):
    """Sign the request in from `Authorization: Bearer <token>`, or answer 401."""
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        header = request.headers.get("Authorization", "")
        token = header[7:].strip() if header.startswith("Bearer ") else ""
        row = g.conn.execute(
            "SELECT t.id AS token_id, t.password_mark, u.* FROM api_tokens t JOIN users u ON u.id = t.user_id"
            " WHERE t.token_hash = ? AND t.revoked_at IS NULL AND u.active = 1", (_hash(token),)
        ).fetchone() if token else None
        if row is None or row["password_mark"] != _password_mark(row["password_hash"]):
            return error(401, "Sign in again.")
        g.user, g.token_id = row, row["token_id"]
        with g.conn:
            g.conn.execute("UPDATE api_tokens SET last_used_at = ? WHERE id = ?", (db.now(), row["token_id"]))
        return view(*args, **kwargs)
    return wrapped


def _body():
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _page():
    """(limit, offset) from the query string, within bounds."""
    limit = min(max(request.args.get("limit", 100, type=int), 1), MAX_LIMIT)
    return limit, max(request.args.get("offset", 0, type=int), 0)


def _user_json(u):
    return {"id": u["id"], "name": u["name"], "role": u["role"]}


ITEM_FIELDS = ("sku", "title", "status", "tote", "price", "quantity", "condition", "category", "notes", "cost",
               "cost_note", "missing_since", "relisted_as", "listing_id", "days_listed", "last_change",
               "suggested_price", "created_at", "updated_at")


def _item_json(row):
    return {k: row[k] for k in ITEM_FIELDS if k in row.keys()}


# --- signing in ---

@blueprint.route("/auth/token", methods=["POST"])
def create_token():
    body = _body()
    name = str(body.get("name", "")).strip().lower()[:40]
    password = str(body.get("password", ""))
    device = str(body.get("device", "")).strip()[:80] or None
    if not name or not password:
        return error(400, "Enter your name and password.")
    if sign_in_failures(g.conn, name) >= MAX_FAILED_SIGN_INS:
        return error(429, "Too many wrong passwords for %s. Wait %d minutes and try again." % (name, LOCKOUT_MINUTES))
    user = g.conn.execute("SELECT * FROM users WHERE name = ? AND active = 1", (name,)).fetchone()
    if not (user and user["password_hash"] and check_password_hash(user["password_hash"], password)):
        with g.conn:
            db.log_event(g.conn, "user", name, "sign_in_failed", "api", data={"from": request.remote_addr, "device": device})
        return error(401, "Wrong name or password.")
    token = secrets.token_urlsafe(32)
    with g.conn:
        g.conn.execute("INSERT INTO api_tokens (user_id, token_hash, device, password_mark, created_at) VALUES (?, ?, ?, ?, ?)",
                       (user["id"], _hash(token), device, _password_mark(user["password_hash"]), db.now()))
        db.log_event(g.conn, "user", user["name"], "signed_in", "api", user["id"],
                     data={"from": request.remote_addr, "device": device})
    return jsonify({"token": token, "user": _user_json(user)}), 201


@blueprint.route("/auth/token", methods=["DELETE"])
@token_required
def revoke_token():
    with g.conn:
        g.conn.execute("UPDATE api_tokens SET revoked_at = ? WHERE id = ?", (db.now(), g.token_id))
        db.log_event(g.conn, "user", g.user["name"], "signed_out", "api", g.user["id"])
    return "", 204


@blueprint.route("/me")
@token_required
def me():
    return jsonify({"user": _user_json(g.user)})


# --- inventory ---

@blueprint.route("/items")
@token_required
def items():
    sql, params, filters = _item_filters()
    limit, offset = _page()
    total = g.conn.execute("SELECT COUNT(*) FROM (%s)" % sql, params).fetchone()[0]
    rows = g.conn.execute(sql + order_by(ITEM_SORTS, "sku", default_desc=True) + " LIMIT ? OFFSET ?",
                          params + [limit, offset]).fetchall()
    return jsonify({"items": [_item_json(r) for r in rows], "total": total, "limit": limit, "offset": offset,
                    "filters": {k: v for k, v in filters.items() if v}})


@blueprint.route("/items/<sku>")
@token_required
def item(sku):
    sku = _sku(sku)
    row = g.conn.execute(ITEMS_SQL + " WHERE i.sku = ?", (sku,)).fetchone()
    if row is None:
        return error(404, "No item %s." % sku)
    events = g.conn.execute(
        "SELECT e.at, e.action, e.from_status, e.to_status, e.data, e.actor, u.name AS user FROM events e"
        " LEFT JOIN users u ON u.id = e.user_id WHERE e.entity = 'item' AND e.entity_id = ? ORDER BY e.id DESC LIMIT 200",
        (sku,)).fetchall()
    listings = g.conn.execute("SELECT listing_id, env, price, listed_at, ended_at FROM listings WHERE sku = ?"
                              " ORDER BY listed_at DESC", (sku,)).fetchall()
    sales = g.conn.execute(
        "SELECT o.order_id, o.created_at, o.status, o.ship_by, ol.price, ol.quantity FROM order_lines ol"
        " JOIN orders o ON o.order_id = ol.order_id WHERE ol.sku = ? ORDER BY o.created_at DESC", (sku,)).fetchall()
    return jsonify({
        "item": _item_json(row),
        "listings": [dict(r) for r in listings],
        "sales": [dict(r) for r in sales],
        "history": [{**{k: e[k] for k in ("at", "action", "from_status", "to_status", "actor", "user")},
                     "data": _parse_json(e["data"])} for e in events],
    })


TOTES_SQL = """
    SELECT l.code, l.kind, l.notes, l.checked_at,
           SUM(i.status NOT IN ('SENT_TO_POST_OFFICE', 'DELIVERED')) AS on_hand,
           SUM(i.status = 'LISTED') AS listed,
           SUM(i.missing_since IS NOT NULL AND i.status NOT IN ('SENT_TO_POST_OFFICE', 'DELIVERED')) AS missing,
           SUM(CASE WHEN i.status = 'LISTED' THEN i.price * MAX(i.quantity, 1) END) AS value
    FROM locations l LEFT JOIN items i ON i.location_id = l.id
"""


@blueprint.route("/totes")
@token_required
def totes():
    sorts = {k: v for k, v in TOTE_SORTS.items() if k in ("code", "on_hand", "listed", "missing", "checked", "value")}
    rows = g.conn.execute(TOTES_SQL + " GROUP BY l.id HAVING on_hand > 0 OR l.kind = 'place'"
                          + order_by(sorts, "code", then="l.code")).fetchall()
    return jsonify({"totes": [dict(r) for r in rows], "total": len(rows), "next_free": db.suggest_tote(g.conn)})


@blueprint.route("/totes/<code>")
@token_required
def tote(code):
    try:
        code = db.check_code(code)
    except ValueError as e:
        return error(404, str(e))
    place = g.conn.execute(TOTES_SQL + " WHERE l.code = ? GROUP BY l.id", (code,)).fetchone()
    if place is None:
        return error(404, "No tote %s." % code)
    rows = g.conn.execute(ITEMS_SQL + " WHERE l.code = ? AND i.status NOT IN (?, ?) ORDER BY i.sku",
                          (code, *db.GONE)).fetchall()
    return jsonify({"tote": dict(place), "items": [_item_json(r) for r in rows]})


# --- changing items (our records only; nothing here touches eBay) ---

def _item_reply(sku, **extra):
    row = g.conn.execute(ITEMS_SQL + " WHERE i.sku = ?", (sku,)).fetchone()
    return jsonify({"item": _item_json(row), **extra})


def _act(skus, action, args):
    """Run item_action in one transaction: all of it happens, or none of it and a 400 says why."""
    try:
        with g.conn:
            return item_action(g.conn, skus, action, args, "api", g.user["id"]), None
    except ValueError as e:
        return None, error(400, str(e).rstrip(".") + ".")


def _known(sku):
    sku = _sku(sku)
    return sku if g.conn.execute("SELECT 1 FROM items WHERE sku = ?", (sku,)).fetchone() else None


@blueprint.route("/items/<sku>", methods=["PATCH"])
@token_required
def item_edit(sku):
    """Correct our records: notes and cost always; title and price only when the item isn't live on eBay."""
    sku = _known(sku)
    if not sku:
        return error(404, "No such item.")
    try:
        changes = edit_changes(_body())
        with g.conn:
            changed = db.edit_item(g.conn, sku, changes, "api", g.user["id"])
    except ValueError as e:
        return error(400, str(e))
    return _item_reply(sku, changed=changed)


@blueprint.route("/items/<sku>/<action>", methods=["POST"])
@token_required
def item_do(sku, action):
    """move {"tote"}, missing {"missing": true/false, "note"}, status {"status", "note"}."""
    sku = _known(sku)
    if not sku:
        return error(404, "No such item.")
    body = _body()
    if action == "missing":
        action = "missing" if body.get("missing", True) else "found"
    elif action not in ("move", "status"):
        return error(404, "No such action.")
    done, err = _act([sku], action, body)
    return err or _item_reply(sku, done=done)


@blueprint.route("/items/<sku>/price-suggestion", methods=["POST"])
@token_required
def item_suggest(sku):
    """Record a lower (or higher) price to set on eBay by hand. Nothing is sent to eBay."""
    sku = _known(sku)
    if not sku:
        return error(404, "No such item.")
    body = _body()
    try:
        with g.conn:
            db.suggest_price(g.conn, sku, _float(body.get("price")), "api", g.user["id"],
                             str(body.get("note") or "").strip() or None)
    except ValueError as e:
        return error(400, str(e) + ".")
    return _item_reply(sku), 201


@blueprint.route("/items/<sku>/price-applied", methods=["POST"])
@token_required
def item_applied(sku):
    """Someone changed the price on eBay: record it, which clears the suggestion."""
    sku = _known(sku)
    if not sku:
        return error(404, "No such item.")
    price = _float(_body().get("price"))
    if price is None or price <= 0:
        return error(400, "Send the new eBay price.")
    with g.conn:
        old = g.conn.execute("SELECT price FROM items WHERE sku = ?", (sku,)).fetchone()[0]
        g.conn.execute("UPDATE items SET price = ?, updated_at = ? WHERE sku = ?", (price, db.now(), sku))
        db.log_event(g.conn, "item", sku, "price_applied", "api", g.user["id"], data={"from": old, "to": price})
    return _item_reply(sku)


@blueprint.route("/items/<sku>/relisted-as", methods=["POST"])
@token_required
def item_relisted(sku):
    """{"sku": "MV-00852"}: this cancelled, returned or ended item was listed again under that SKU."""
    sku = _known(sku)
    if not sku:
        return error(404, "No such item.")
    try:
        with g.conn:
            db.link_relist(g.conn, sku, _sku(str(_body().get("sku") or "")), "api", g.user["id"])
    except ValueError as e:
        return error(400, str(e) + ".")
    return _item_reply(sku)


@blueprint.route("/items/bulk", methods=["POST"])
@token_required
def items_bulk():
    """{"skus": [...], "action": "move" | "missing" | "found" | "status" | "drop", plus tote, status, note
    or percent}. All or nothing."""
    body = _body()
    skus = body.get("skus")
    if not isinstance(skus, list) or not all(isinstance(s, (str, int)) for s in skus):
        return error(400, "Send skus as a list.")
    skus = list(dict.fromkeys(_sku(str(s)) for s in skus))
    done, err = _act(skus, str(body.get("action") or ""), body)
    return err or jsonify({"done": done, "skus": skus})


# --- totes ---

def _code(code):
    try:
        return db.check_code(code), None
    except ValueError as e:
        return None, error(404 if code else 400, str(e))


def _sku_list(value):
    if not isinstance(value, list) or not all(isinstance(s, (str, int)) for s in value):
        raise ValueError("Send SKUs as a list.")
    return [_sku(str(s)) for s in value]


@blueprint.route("/totes/<code>/count", methods=["POST"])
@token_required
def tote_count(code):
    """{"here": [SKUs found], "extra": [SKUs found here that we had somewhere else]}. Items on hand
    that aren't in `here` become missing; the tote is recorded as checked."""
    code, err = _code(code)
    if err:
        return err
    body = _body()
    try:
        here, extra = _sku_list(body.get("here", [])), _sku_list(body.get("extra", []))
        with g.conn:
            result = count_tote(g.conn, code, here, extra, "api", g.user["id"])
    except ValueError as e:
        return error(400, str(e).rstrip(".") + ". Nothing was changed.")
    return jsonify(result)


@blueprint.route("/totes/<code>/checked", methods=["POST"])
@token_required
def tote_checked(code):
    code, err = _code(code)
    if err:
        return err
    with g.conn:
        db.mark_checked(g.conn, code, "api", g.user["id"])
    return "", 204


@blueprint.route("/totes/consolidate", methods=["POST"])
@token_required
def totes_consolidate():
    """{"codes": ["B4", "C6"], "into": "B4"} (no `into` = the next free tote). Into a tote that has
    items in it only when it's one of `codes`, so nobody adds to a full tote by mistake."""
    body = _body()
    try:
        codes = list(dict.fromkeys(db.check_code(str(c)) for c in body.get("codes") or []))
        if len(codes) < 2:
            raise ValueError("List at least two totes to combine.")
        into = db.check_code(body["into"]) if body.get("into") else db.suggest_tote(g.conn, freeing=codes)
        if into not in codes and into in db.totes_in_use(g.conn):
            raise ValueError("%s already has items in it; list it with the others to add to it." % into)
        with g.conn:
            moved = db.consolidate(g.conn, codes, into, "api", g.user["id"])
    except ValueError as e:
        return error(400, str(e))
    return jsonify({"into": into, "moved": moved})


@blueprint.route("/totes/<code>/split", methods=["POST"])
@token_required
def tote_split(code):
    """{"plan": {"MV-00012": "C3", "MV-00015": "D1"}}: send items from this tote to others."""
    code, err = _code(code)
    if err:
        return err
    plan = _body().get("plan")
    if not isinstance(plan, dict) or not plan:
        return error(400, "Send a plan: which SKU goes to which tote.")
    try:
        plan = {_sku(str(k)): db.check_code(str(v)) for k, v in plan.items()}
        with g.conn:
            db.split(g.conn, code, {k: v for k, v in plan.items() if v != code}, "api", g.user["id"])
    except ValueError as e:
        return error(400, str(e))
    return jsonify({"moved": sum(1 for v in plan.values() if v != code)})


# --- shipping ---

@blueprint.route("/picklist")
@token_required
def picklist():
    """Sold items waiting to ship, in tote order; `?days=0` for only those due today, 1 for tomorrow too."""
    days = request.args.get("days", "")
    rows = orders.pick_list(g.conn, int(days) if days.isdigit() else None)
    return jsonify({"items": [dict(r) for r in rows], "total": len(rows), "last_sync": _last_sync()})


@blueprint.route("/pack", methods=["POST"])
@token_required
def pack():
    """{"skus": [...] (or "sku"), "step": "LABEL_PRINTED" | "PACKAGED" | "SENT_TO_POST_OFFICE" | "BOUGHT",
    "box": "..."}. Only items waiting to ship change; the rest come back in `skipped`."""
    body = _body()
    try:
        skus = _sku_list(body["skus"]) if "skus" in body else [_sku(str(body.get("sku") or ""))]
        box = str(body.get("box") or "").strip()[:40] if body.get("step") == "PACKAGED" else None
        with g.conn:
            changed, skipped = ship(g.conn, skus, str(body.get("step") or ""), "api", g.user["id"], box=box or None)
    except ValueError as e:
        return error(400, str(e).rstrip(".") + ".")
    return jsonify({"changed": changed, "skipped": skipped})


def _last_sync():
    return g.conn.execute("SELECT MAX(at) FROM events WHERE actor = 'sync'").fetchone()[0]


# --- eBay (read only: these fetch from eBay and change only our records) ---

@blueprint.route("/sync/orders", methods=["POST"])
@token_required
def sync_orders():
    """Fetch new orders from eBay. New sales lower an item's units left; at 0 it's BOUGHT."""
    try:
        counts = orders.sync(g.conn, user=g.user["id"])
    except (ebay_api.EbayError, SystemExit) as e:
        return error(502, "Couldn't get orders from eBay: %s" % e, "ebay_error")
    return jsonify({"orders": counts["orders"], "new_sales": counts["new_lines"], "changes": [
        {"sku": sku, "from": before, "to": after, "title": title} for sku, before, after, title in counts["changes"]]})


@blueprint.route("/sync/listings", methods=["POST"])
@token_required
def sync_listings():
    """Compare our LISTED items with eBay's live listings. Changes nothing: to act on it, end items
    with /items/bulk (status ENDED) and fix units left with /listings/quantities."""
    try:
        active = trading.active_listings()
    except (ebay_api.EbayError, SystemExit) as e:
        return error(502, "Couldn't get listings from eBay: %s" % e, "ebay_error")
    d = trading.compare(g.conn, active)
    with g.conn:
        db.log_event(g.conn, "listings", "check", "checked", "api", g.user["id"], data={
            "live": d["live"], "not_live": len(d["not_live"]), "unknown": len(d["unknown"]), "quantity": len(d["quantity"])})
    if not d["live"] and d["listed"]:
        return error(502, "eBay returned no live listings at all, which can't be right. Try again later.", "ebay_error")
    return jsonify({"live": d["live"], "listed": d["listed"], "not_live": [dict(r) for r in d["not_live"]],
                    "unknown": [dict(a) for a in d["unknown"]],
                    "quantity": [{**dict(r), "ebay_quantity": q} for r, q in d["quantity"]]})


@blueprint.route("/listings/quantities", methods=["POST"])
@token_required
def listings_quantities():
    """{"quantities": {"MV-00123": 2}}: take eBay's units left for listed items. Our records only; all or nothing."""
    quantities = _body().get("quantities")
    if not isinstance(quantities, dict) or not quantities:
        return error(400, "Send quantities: units left per SKU.")
    changed = 0
    try:
        with g.conn:
            for sku, qty in quantities.items():
                sku = _sku(str(sku))
                if not isinstance(qty, int) or isinstance(qty, bool) or qty < 0:
                    raise ValueError("Units left for %s must be a whole number." % sku)
                row = g.conn.execute("SELECT status FROM items WHERE sku = ?", (sku,)).fetchone()
                if row is None or row["status"] != "LISTED":
                    raise ValueError("%s isn't listed." % sku)
                changed += db.set_quantity(g.conn, sku, qty, "api", g.user["id"], note="eBay's count (listing check)")
    except ValueError as e:
        return error(400, "%s Nothing was changed." % e)
    return jsonify({"changed": changed})


# --- capture (the camera flow) ---

def _batch_json(conn, batch_id):
    b = capture.batch_row(conn, batch_id)
    return {"id": b["id"], "name": b["name"], "tote": b["tote"], "source": b["source"], "created_at": b["created_at"],
            "finished_at": b["finished_at"], "items": [
                {**i, "photos": [{"file": p, "url": url_for("api.photo", batch=b["name"], filename=p)} for p in i["photos"]]}
                for i in capture.items(conn, batch_id)]}


def _capture_call(fn, *args, **kwargs):
    """Run a capture change in a transaction; turn its refusals into API errors."""
    try:
        with g.conn:
            return fn(g.conn, *args, **kwargs), None
    except LookupError as e:
        return None, error(404, str(e.args[0]))
    except ValueError as e:  # CaptureError, and a bad tote code
        return None, error(400, str(e))


@blueprint.route("/batches", methods=["POST"])
@token_required
def batch_start():
    batch_id, err = _capture_call(capture.start_batch, _body().get("tote"), g.user["id"], batches_dir=g.batches_dir)
    return err or (jsonify({"batch": _batch_json(g.conn, batch_id)}), 201)


@blueprint.route("/batches")
@token_required
def batch_list():
    """Batches shot in the app, newest first; `?open=1` for ones still being shot."""
    sql = ("SELECT b.id FROM batches b WHERE b.source = 'app'"
           + (" AND b.finished_at IS NULL" if request.args.get("open") else "") + " ORDER BY b.id DESC LIMIT 100")
    return jsonify({"batches": [_batch_json(g.conn, r["id"]) for r in g.conn.execute(sql)]})


@blueprint.route("/batches/<int:batch_id>")
@token_required
def batch_get(batch_id):
    try:
        return jsonify({"batch": _batch_json(g.conn, batch_id)})
    except LookupError as e:
        return error(404, str(e))


@blueprint.route("/batches/<int:batch_id>/items", methods=["POST"])
@token_required
def capture_new_item(batch_id):
    sku, err = _capture_call(capture.new_item, batch_id, g.user["id"])
    return err or (jsonify({"sku": sku, "batch": _batch_json(g.conn, batch_id)}), 201)


@blueprint.route("/batches/<int:batch_id>/items/<sku>", methods=["PATCH"])
@token_required
def capture_update_item(batch_id, sku):
    body = _body()
    _, err = _capture_call(capture.update_item, batch_id, _sku(sku), g.user["id"],
                           note=body.get("note"), photos=body.get("photos"))
    return err or jsonify({"batch": _batch_json(g.conn, batch_id)})


@blueprint.route("/batches/<int:batch_id>/items/<sku>", methods=["DELETE"])
@token_required
def capture_delete_item(batch_id, sku):
    _, err = _capture_call(capture.delete_item, batch_id, _sku(sku), g.user["id"], batches_dir=g.batches_dir)
    return err or ("", 204)


@blueprint.route("/batches/<int:batch_id>/items/<sku>/photos/<client_id>", methods=["PUT"])
@token_required
def capture_photo(batch_id, sku, client_id):
    """The photo is the request body (Content-Type image/jpeg, image/png or image/heic), or a
    multipart field named `photo`. Sending the same client_id again is harmless."""
    upload = request.files.get("photo")
    content_type, data = (upload.mimetype, upload.read()) if upload else (request.content_type, request.get_data())
    result, err = _capture_call(capture.add_photo, batch_id, _sku(sku), client_id, content_type, data, g.user["id"],
                                 batches_dir=g.batches_dir)
    if err:
        return err
    filename, added = result
    return jsonify({"file": filename, "added": added}), 201 if added else 200


@blueprint.route("/batches/<int:batch_id>/items/<sku>/photos/<client_id>", methods=["DELETE"])
@token_required
def capture_delete_photo(batch_id, sku, client_id):
    _, err = _capture_call(capture.delete_photo, batch_id, _sku(sku), client_id, g.user["id"],
                           batches_dir=g.batches_dir)
    return err or ("", 204)


@blueprint.route("/batches/<int:batch_id>/finish", methods=["POST"])
@token_required
def capture_finish(batch_id):
    _, err = _capture_call(capture.finish_batch, batch_id, g.user["id"])
    return err or jsonify({"batch": _batch_json(g.conn, batch_id)})


@blueprint.route("/note-tags")
@token_required
def note_tags():
    """Quick choices for an item's note: this person's most used first, then the standard list."""
    return jsonify({"recent": capture.recent_tags(g.conn, g.user["id"]), "standard": capture.NOTE_TAGS})


@blueprint.route("/photos/<batch>/<filename>")
@token_required
def photo(batch, filename):
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", batch):
        return error(404, "No such photo.")
    return send_from_directory(os.path.join(g.batches_dir, batch, "photos"), filename)
