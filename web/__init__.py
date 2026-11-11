"""The web app: review and publish drafts, look up items and totes, print the pick list.

Server-rendered Flask pages over the same code the CLI uses, all reading and writing the
database. Every change is recorded under the signed-in person. Run it with `cli.py serve`.
"""

import csv
import datetime
import functools
import io
import json
import os
import re
import secrets

import segno
from flask import Flask, abort, flash, g, redirect, render_template, request, send_from_directory, session, url_for
from markupsafe import Markup
from werkzeug.security import check_password_hash

from ebay import api, comps, config, db, drafts, listings, orders, sales

BATCH_RE = re.compile(r"^[\w.-]+$")

# Inventory API condition values offered in the editor. Which ones a category accepts varies;
# eBay rejects a bad one at publish time with a clear error.
CONDITIONS = [
    ("NEW", "New"),
    ("NEW_OTHER", "New other / open box"),
    ("NEW_WITH_DEFECTS", "New with defects"),
    ("LIKE_NEW", "Like new"),
    ("CERTIFIED_REFURBISHED", "Certified refurbished"),
    ("SELLER_REFURBISHED", "Seller refurbished"),
    ("USED_EXCELLENT", "Used / pre-owned"),
    ("USED_VERY_GOOD", "Used: very good"),
    ("USED_GOOD", "Used: good"),
    ("USED_ACCEPTABLE", "Used: acceptable"),
    ("FOR_PARTS_OR_NOT_WORKING", "For parts or not working"),
]

TITLE_MAX = 80
PAGE_SIZE = 200  # rows per page on the items list


def create_app(db_path=None, batches_dir=None):
    app = Flask(__name__)
    app.config.update(
        DB_PATH=db_path or config.DB_PATH,
        BATCHES_DIR=batches_dir or os.path.join(config.ROOT, "batches"),
        SECRET_KEY=_secret_key(os.path.dirname(db_path or config.DB_PATH)),
        PERMANENT_SESSION_LIFETIME=datetime.timedelta(days=30),
        SESSION_COOKIE_SAMESITE="Lax",
    )
    _routes(app)
    return app


def _secret_key(data_dir):
    """Random key for signing session cookies, made once and kept in data/ (which git ignores)."""
    path = os.path.join(data_dir, "web_secret_key")
    if not os.path.exists(path):
        os.makedirs(data_dir, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_hex(32))
    with open(path) as f:
        return f.read().strip()


# --- drafts ------------------------------------------------------------------


def _load_batch(batch):
    loaded = drafts.load(g.conn, batch)
    if loaded is None:
        abort(404)
    return loaded


def _draft(batch_drafts, sku):
    for item in batch_drafts["items"]:
        if item["sku"] == sku:
            return item
    abort(404)


def draft_state(item):
    if item.get("published", {}).get(config.ENV):
        return "published"
    if item.get("skip"):
        return "skipped"
    if problems(item):
        return "needs work"
    return "ready"


def problems(item):
    """Reasons a draft can't be published yet."""
    found = ["Open question: " + q for q in item.get("needs_check", [])]
    if not item.get("title"):
        found.append("No title")
    elif len(item["title"]) > TITLE_MAX:
        found.append("Title is %d characters (max %d)" % (len(item["title"]), TITLE_MAX))
    if not item.get("price") or item["price"] <= 0:
        found.append("No price")
    if not item.get("photos"):
        found.append("No photos")
    if not item.get("category_id"):
        found.append("No category")
    return found


def _aspects_text(aspects):
    return "\n".join("%s: %s" % (k, " | ".join(v)) for k, v in aspects.items())


def _parse_aspects(text):
    aspects = {}
    for line in text.splitlines():
        if ":" in line:
            name, values = line.split(":", 1)
            values = [v.strip() for v in values.split("|") if v.strip()]
            if name.strip() and values:
                aspects[name.strip()] = values
    return aspects


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _apply_form(item, form):
    """Copy the editor's fields into the draft. Returns the names of fields that changed."""
    new = {
        "title": form["title"].strip(),
        "price": _float(form["price"]),
        "condition": form["condition"],
        "condition_description": form["condition_description"].strip(),
        "category_id": form["category_id"].strip(),
        "description": form["description"].replace("\r\n", "\n").strip(),
        "aspects": _parse_aspects(form["aspects"]),
        "needs_check": [l.strip() for l in form["needs_check"].splitlines() if l.strip()],
        "comp_query": form["comp_query"].strip(),
        "package": {
            k: _float(form[k]) if _float(form[k]) is not None else item.get("package", {}).get(k)
            for k in ("weight_oz", "length_in", "width_in", "height_in")
        },
    }
    changed = []
    for key, value in new.items():
        if key == "comp_query" and not value:
            if item.pop("comp_query", None) is not None:
                changed.append(key)
            continue
        if item.get(key) != value:
            item[key] = value
            changed.append(key)
    skip = form.get("skip", "").strip()
    if skip != (item.get("skip") or ""):
        if skip:
            item["skip"] = skip
        else:
            item.pop("skip", None)
        changed.append("skip")
    return changed


# --- sorting ------------------------------------------------------------------


def order_by(columns, default, default_desc=False, then="i.sku"):
    """ORDER BY for ?sort=<key>&dir=asc|desc. Only keys in `columns` (key -> SQL expression,
    or a tuple of them) are accepted, so the URL can't inject SQL. Remembers the choice in g.sort."""
    key = request.args.get("sort", default)
    if key not in columns:
        key = default
    desc = request.args.get("dir", "desc" if default_desc and key == default else "asc") == "desc"
    g.sort = (key, desc)
    exprs = columns[key] if isinstance(columns[key], tuple) else (columns[key],)
    # NULLs (unknown tote, no price) go last in either direction.
    parts = ["%s IS NULL, %s %s" % (e, e, "DESC" if desc else "ASC") for e in exprs]
    return " ORDER BY " + ", ".join(parts) + (", " + then if then else "")


def sort_url(key):
    """Link for a column header: sort by `key`, flipping the direction if it's already the sort."""
    current, desc = g.get("sort", (None, False))
    args = request.args.to_dict()
    args.pop("page", None)
    args["sort"] = key
    args["dir"] = "asc" if current == key and desc else "desc" if current == key else "asc"
    return url_for(request.endpoint, **(request.view_args or {}), **args)


def sort_mark(key):
    current, desc = g.get("sort", (None, False))
    return ("▼" if desc else "▲") if current == key else ""


# --- request plumbing ----------------------------------------------------------


def login_required(view):
    @functools.wraps(view)
    def wrapped(*args, **kwargs):
        if g.user is None:
            return redirect(url_for("login", next=request.full_path))
        return view(*args, **kwargs)

    return wrapped


def _routes(app):
    @app.before_request
    def open_db():
        g.conn = db.connect(app.config["DB_PATH"])
        g.batches_dir = app.config["BATCHES_DIR"]
        g.user = None
        if "user_id" in session:
            g.user = g.conn.execute(
                "SELECT * FROM users WHERE id = ? AND active = 1", (session["user_id"],)
            ).fetchone()
        if "csrf" not in session:
            session["csrf"] = secrets.token_hex(16)
        if request.method == "POST" and request.form.get("csrf") != session["csrf"]:
            abort(400, "Form expired. Go back, reload the page and try again.")

    @app.teardown_request
    def close_db(exc):
        conn = g.pop("conn", None)
        if conn is not None:
            conn.close()

    @app.context_processor
    def helpers():
        return {"csrf": session.get("csrf"), "env": config.ENV, "read_only": config.READ_ONLY, "money": _money_fmt,
                "sort_url": sort_url, "sort_mark": sort_mark, "event_details": event_details}

    # --- sign in ---

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            row = g.conn.execute(
                "SELECT * FROM users WHERE name = ? AND active = 1", (request.form["name"].strip().lower(),)
            ).fetchone()
            if row and row["password_hash"] and check_password_hash(row["password_hash"], request.form["password"]):
                with g.conn:
                    db.log_event(g.conn, "user", row["name"], "signed_in", "web", row["id"],
                                 data={"from": request.remote_addr})
                session.clear()
                session.permanent = True
                session["user_id"] = row["id"]
                nxt = request.args.get("next", "")
                return redirect(nxt if nxt.startswith("/") and not nxt.startswith("//") else url_for("home"))
            with g.conn:
                db.log_event(g.conn, "user", request.form["name"].strip().lower()[:40], "sign_in_failed", "web",
                             data={"from": request.remote_addr})
            flash("Wrong name or password.", "error")
        return render_template("login.html")

    @app.route("/logout", methods=["POST"])
    def logout():
        if g.user is not None:
            with g.conn:
                db.log_event(g.conn, "user", g.user["name"], "signed_out", "web", g.user["id"])
        session.clear()
        return redirect(url_for("login"))

    # --- home ---

    @app.route("/")
    @login_required
    def home():
        counts = dict(g.conn.execute("SELECT status, COUNT(*) FROM items GROUP BY status").fetchall())
        to_ship = orders.pick_list(g.conn)
        today = datetime.date.today().isoformat()
        batches = _batch_summaries()
        return render_template(
            "home.html",
            counts=[(s, counts.get(s, 0)) for s in db.STATUSES if counts.get(s)],
            to_ship=to_ship,
            overdue=[r for r in to_ship if r["ship_by"] and r["ship_by"] < today],
            due_today=[r for r in to_ship if r["ship_by"] == today],
            batches=[b for b in batches if b["open"]],
            attention=_attention(g.conn),
        )

    def _attention(conn):
        """Things someone should look at, each with a count and a link to the list. Zero counts are left out."""
        def n(sql, *params):
            return conn.execute(sql, params).fetchone()[0]
        on_hand = "i.status NOT IN ('SENT_TO_POST_OFFICE', 'DELIVERED')"
        found = [
            (n("SELECT COUNT(*) FROM items i JOIN locations l ON l.id = i.location_id WHERE l.code = 'UN' AND " + on_hand),
             "in UN, waiting for a tote", url_for("items", tote="UN", status="ON_HAND")),
            (n("SELECT COUNT(*) FROM items i WHERE i.missing_since IS NOT NULL AND " + on_hand),
             "marked missing", url_for("items", status="MISSING")),
            (n("SELECT COUNT(*) FROM items WHERE status IN (?, ?, ?) AND relisted_as IS NULL", *db.RELISTABLE),
             "cancelled or returned and not relisted", url_for("relists")),
            (n("SELECT COUNT(*) FROM items i WHERE i.status = 'LISTED' AND " + DAYS_LISTED + " >= 90"),
             "listed 90+ days (consider a price drop)", url_for("items", status="LISTED", min_days=90, sort="days", dir="desc")),
            (n("SELECT COUNT(*) FROM items i WHERE i.status = 'LISTED' AND " + db.LATEST_SUGGESTION + " IS NOT NULL"),
             "price suggestions to change on eBay", url_for("items", status="SUGGESTED")),
            (n("SELECT COUNT(*) FROM items i WHERE i.location_id IS NULL AND " + on_hand),
             "on hand with no known tote", url_for("items", tote="?", status="ON_HAND")),
            (n("SELECT COUNT(DISTINCT l.id) FROM locations l JOIN items i ON i.location_id = l.id"
               " WHERE l.kind = 'tote' AND l.checked_at IS NULL AND " + on_hand),
             "totes never checked", url_for("totes", unchecked=1)),
        ]
        return [f for f in found if f[0]]

    # --- drafts ---

    def _batch_summaries():
        out = []
        for name in drafts.batch_names(g.conn):
            batch_drafts = drafts.load(g.conn, name)
            states = [draft_state(i) for i in batch_drafts["items"]]
            out.append({
                "name": name,
                "tote": batch_drafts["tote"],
                "total": len(states),
                "ready": states.count("ready"),
                "needs_work": states.count("needs work"),
                "published": states.count("published"),
                "open": states.count("ready") + states.count("needs work"),
            })
        return out

    @app.route("/drafts")
    @login_required
    def drafts_index():
        return render_template("drafts.html", batches=_batch_summaries())

    @app.route("/drafts/<batch>")
    @login_required
    def batch(batch):
        batch_drafts = _load_batch(batch)
        rows = [dict(item=i, state=draft_state(i), problems=problems(i)) for i in batch_drafts["items"]]
        return render_template("batch.html", batch=batch, drafts=batch_drafts, rows=rows)

    @app.route("/drafts/<batch>/<sku>", methods=["GET", "POST"])
    @login_required
    def draft(batch, sku):
        batch_drafts = _load_batch(batch)
        item = _draft(batch_drafts, sku)
        published = item.get("published", {}).get(config.ENV)
        if request.method == "POST":
            if published:
                flash("Already published; change it on eBay instead.", "error")
                return redirect(request.url)
            loaded_version = request.form.get("version", type=int)
            changed = _apply_form(item, request.form)
            if item["price"] is None:
                flash("Price must be a number.", "error")
                return render_template("draft.html", **_draft_context(batch, batch_drafts, item))
            if changed:
                try:
                    with g.conn:
                        drafts.save(g.conn, item, "web", g.user["id"], changed=changed, version=loaded_version)
                except drafts.Conflict:
                    who = g.conn.execute(
                        "SELECT u.name, d.updated_at FROM drafts d LEFT JOIN users u ON u.id = d.updated_by WHERE d.sku = ?",
                        (sku,)).fetchone()
                    flash("%s saved this draft at %s while you were editing it, so your changes weren't saved. "
                          "Your version is below: Save again to replace theirs, or reload to see theirs."
                          % (who["name"] or "Someone", who["updated_at"][11:16]), "error")
                    item["_version"] = drafts.get(g.conn, sku)[1]["_version"]
                    return render_template("draft.html", **_draft_context(batch, batch_drafts, item))
                flash("Saved.", "ok")
            if request.form.get("then") == "publish":
                return _publish(batch, {sku}, back=url_for("draft", batch=batch, sku=sku))
            return redirect(request.url)
        return render_template("draft.html", **_draft_context(batch, batch_drafts, item))

    def _draft_context(batch, batch_drafts, item):
        skus = [i["sku"] for i in batch_drafts["items"]]
        pos = skus.index(item["sku"])
        conditions = list(CONDITIONS)
        if item.get("condition") not in dict(conditions):
            conditions.append((item.get("condition"), item.get("condition")))
        return dict(
            batch=batch,
            drafts=batch_drafts,
            item=item,
            state=draft_state(item),
            problems=problems(item),
            published=item.get("published", {}).get(config.ENV),
            conditions=conditions,
            aspects_text=_aspects_text(item.get("aspects", {})),
            prev_sku=skus[pos - 1] if pos > 0 else None,
            next_sku=skus[pos + 1] if pos + 1 < len(skus) else None,
            title_max=TITLE_MAX,
        )

    @app.route("/drafts/<batch>/<sku>/comps", methods=["POST"])
    @login_required
    def draft_comps(batch, sku):
        messages = []
        try:
            comps.refresh_active(g.conn, batch, {sku}, log=messages.append, user=g.user["id"], actor="web")
            flash("Price estimate updated.", "ok")
        except (api.EbayError, SystemExit) as e:
            flash("Couldn't get comps: %s" % e, "error")
        return redirect(url_for("draft", batch=batch, sku=sku))

    @app.route("/drafts/<batch>/publish", methods=["POST"])
    @login_required
    def batch_publish(batch):
        ready = {i["sku"] for i in _load_batch(batch)["items"] if draft_state(i) == "ready"}
        if not ready:
            flash("Nothing is ready to publish.", "error")
            return redirect(url_for("batch", batch=batch))
        return _publish(batch, ready, back=url_for("batch", batch=batch))

    def _publish(batch, skus, back):
        batch_drafts = _load_batch(batch)
        blocked = {i["sku"]: problems(i) for i in batch_drafts["items"] if i["sku"] in skus and draft_state(i) != "ready"}
        for sku, found in blocked.items():
            flash("%s not published: %s" % (sku, "; ".join(found) or draft_state(_draft(batch_drafts, sku))), "error")
        skus = skus - set(blocked)
        if skus:
            messages = []
            try:
                listings.publish(g.conn, batch, skus, user=g.user["id"], actor="web", log=messages.append)
            except (api.EbayError, SystemExit) as e:
                messages.append("Stopped: %s" % e)
                flash("Publishing stopped with an error from eBay (details below).", "error")
            for m in messages:
                flash(m, "error" if m.startswith("Stopped") else "ok")
        return redirect(back)

    @app.route("/photos/<batch>/<path:filename>")
    @login_required
    def photo(batch, filename):
        if not BATCH_RE.match(batch):
            abort(404)
        return send_from_directory(os.path.join(g.batches_dir, batch, "photos"), filename)

    # --- items and totes ---

    def _item_filters():
        """The items page's filters from the URL, as (SQL, params, cleaned-up values)."""
        f = {k: request.args.get(k, "").strip() for k in ("q", "status", "tote", "min_price", "max_price", "min_days")}
        where, params = ["1 = 1"], []
        if f["q"]:
            where.append("(i.sku LIKE ? OR i.title LIKE ?)")
            params += ["%" + f["q"] + "%"] * 2
        if f["status"] == "MISSING":
            where.append("i.missing_since IS NOT NULL")
        elif f["status"] == "SUGGESTED":
            where.append("i.status = 'LISTED' AND " + db.LATEST_SUGGESTION + " IS NOT NULL")
        elif f["status"] == "ON_HAND":
            where.append("i.status NOT IN (?, ?)")
            params += list(db.GONE)
        elif f["status"]:
            where.append("i.status = ?")
            params.append(f["status"])
        if f["tote"] == "?":
            where.append("i.location_id IS NULL")
        elif f["tote"]:
            where.append("l.code = ?")
            params.append(f["tote"].upper())
        for key, test in (("min_price", "i.price >= ?"), ("max_price", "i.price <= ?"), ("min_days", DAYS_LISTED + " >= ?")):
            value = _float(f[key])
            if value is not None:
                where.append(test)
                params.append(value)
            else:
                f[key] = ""
        return ITEMS_SQL + " WHERE " + " AND ".join(where), params, f

    @app.route("/items")
    @login_required
    def items():
        sql, params, f = _item_filters()
        total = g.conn.execute("SELECT COUNT(*) FROM (%s)" % sql, params).fetchone()[0]
        pages = max(1, -(-total // PAGE_SIZE))
        page = min(max(request.args.get("page", 1, type=int), 1), pages)
        rows = g.conn.execute(sql + order_by(ITEM_SORTS, "sku", default_desc=True) + " LIMIT ? OFFSET ?",
                              params + [PAGE_SIZE, (page - 1) * PAGE_SIZE]).fetchall()
        totes = [r["code"] for r in g.conn.execute(
            "SELECT DISTINCT l.code FROM locations l JOIN items i ON i.location_id = l.id"
            " ORDER BY l.kind = 'place', substr(l.code, 1, 1), CAST(substr(l.code, 2) AS INTEGER)")]
        args = request.args.to_dict()
        args.pop("page", None)
        return render_template("items.html", rows=rows, total=total, f=f, statuses=db.STATUSES, totes=totes,
                               filtered=any(f.values()), suggestion=db.suggest_tote(g.conn),
                               page=page, pages=pages, first=(page - 1) * PAGE_SIZE + 1, args=args)

    @app.route("/items.csv")
    @login_required
    def items_csv():
        """Everything the current filters match, as a spreadsheet."""
        sql, params, _ = _item_filters()
        rows = g.conn.execute(sql + order_by(ITEM_SORTS, "sku", default_desc=True), params).fetchall()
        out = io.StringIO()
        writer = csv.writer(out)
        columns = ["sku", "status", "tote", "price", "suggested_price", "cost", "title", "days_listed", "last_change",
                   "listing_id", "missing_since", "notes"]
        writer.writerow(columns)
        writer.writerows([r[c] for c in columns] for r in rows)
        name = "items-%s.csv" % datetime.date.today().isoformat()
        return app.response_class(out.getvalue(), mimetype="text/csv",
                                  headers={"Content-Disposition": "attachment; filename=" + name})

    @app.route("/items/<sku>", methods=["GET", "POST"])
    @login_required
    def item(sku):
        row = g.conn.execute(ITEMS_SQL + " WHERE i.sku = ?", (sku,)).fetchone()
        if row is None:
            abort(404)
        if request.method == "POST":
            with g.conn:
                if request.form.get("tote"):
                    try:
                        code = db.check_code(request.form["tote"])
                    except ValueError as e:
                        flash(str(e), "error")
                        return redirect(url_for("item", sku=sku))
                    db.move(g.conn, sku, code, "web", g.user["id"])
                    flash("Moved to %s." % code, "ok")
                if request.form.get("relisted_as", "").strip():
                    try:
                        db.link_relist(g.conn, sku, request.form["relisted_as"].strip().upper(), "web", g.user["id"])
                        flash("Linked: relisted as %s." % request.form["relisted_as"].strip().upper(), "ok")
                    except ValueError as e:
                        flash(str(e), "error")
                if request.form.get("status") and request.form["status"] != row["status"]:
                    db.set_status(g.conn, sku, request.form["status"], "web", g.user["id"],
                                  data={"note": request.form.get("note", "").strip() or None, "manual": True})
                    flash("Status set to %s." % request.form["status"], "ok")
            return redirect(url_for("item", sku=sku))
        events = g.conn.execute(
            "SELECT e.*, u.name AS user FROM events e LEFT JOIN users u ON u.id = e.user_id"
            " WHERE e.entity = 'item' AND e.entity_id = ? ORDER BY e.at DESC, e.id DESC",
            (sku,),
        ).fetchall()
        item_listings = g.conn.execute("SELECT * FROM listings WHERE sku = ? ORDER BY listed_at DESC", (sku,)).fetchall()
        item_sales = g.conn.execute(
            "SELECT o.*, ol.price AS line_price FROM order_lines ol JOIN orders o ON o.order_id = ol.order_id"
            " WHERE ol.sku = ? ORDER BY o.created_at DESC",
            (sku,),
        ).fetchall()
        relist_of = g.conn.execute("SELECT sku FROM items WHERE relisted_as = ?", (sku,)).fetchall()
        suggestion = g.conn.execute("SELECT " + db.LATEST_SUGGESTION + " FROM items i WHERE i.sku = ?", (sku,)).fetchone()[0]
        draft_batch = drafts.get(g.conn, sku)[0] if row["status"] == "PENDING" else None
        return render_template("item.html", row=row, events=events, listings=item_listings, sales=item_sales,
                               statuses=db.STATUSES, parse_json=_parse_json, relist_of=relist_of,
                               relistable=db.RELISTABLE, off_ebay=row["status"] in db.OFF_EBAY,
                               suggestion=suggestion, draft_batch=draft_batch, title_max=TITLE_MAX)

    @app.route("/items/<sku>/edit", methods=["POST"])
    @login_required
    def item_edit(sku):
        """Correct our records: notes and cost always; title and price only when it's not live on eBay."""
        form = request.form
        changes = {"notes": form.get("notes", "").strip() or None, "cost_note": form.get("cost_note", "").strip() or None}
        for key in ("cost", "price"):
            if key in form:
                text = form[key].strip().lstrip("$")
                value = _float(text)
                if text and value is None:
                    flash("%s must be a number." % key.capitalize(), "error")
                    return redirect(url_for("item", sku=sku))
                changes[key] = round(value, 2) if value is not None else None  # empty cost = unknown
        if "title" in form:
            changes["title"] = form["title"].strip()
            if not changes["title"] or len(changes["title"]) > TITLE_MAX:
                flash("The title must be 1 to %d characters." % TITLE_MAX, "error")
                return redirect(url_for("item", sku=sku))
        try:
            with g.conn:
                changed = db.edit_item(g.conn, sku, changes, "web", g.user["id"])
            flash("Saved: %s." % ", ".join(changed) if changed else "Nothing changed.", "ok")
        except ValueError as e:
            flash(str(e), "error")
        return redirect(url_for("item", sku=sku))

    @app.route("/items/<sku>/suggest", methods=["POST"])
    @login_required
    def item_suggest(sku):
        try:
            with g.conn:
                db.suggest_price(g.conn, sku, _float(request.form.get("price", "").strip().lstrip("$")), "web",
                                 g.user["id"], request.form.get("note", "").strip() or None)
            flash("Price suggestion recorded. Change it on eBay, then press Applied.", "ok")
        except ValueError as e:
            flash(str(e), "error")
        return redirect(url_for("item", sku=sku))

    @app.route("/items/<sku>/applied", methods=["POST"])
    @login_required
    def item_applied(sku):
        """Someone changed the price on eBay: record it and clear the suggestion."""
        price = _float(request.form.get("price"))
        row = g.conn.execute("SELECT price FROM items WHERE sku = ?", (sku,)).fetchone()
        if row is None or price is None:
            abort(400)
        with g.conn:
            g.conn.execute("UPDATE items SET price = ?, updated_at = ? WHERE sku = ?", (price, db.now(), sku))
            db.log_event(g.conn, "item", sku, "price_applied", "web", g.user["id"], data={"from": row["price"], "to": price})
        flash("Recorded the new eBay price of %s." % _money_fmt(price), "ok")
        return redirect(request.form.get("back") if (request.form.get("back") or "").startswith("/") else url_for("item", sku=sku))

    @app.route("/items/bulk", methods=["POST"])
    @login_required
    def items_bulk():
        """One action on every ticked item: move, missing, found or set status. All or nothing."""
        skus = list(dict.fromkeys(s.upper() for s in request.form.getlist("sku")))
        action = request.form.get("action", "")
        back = request.form.get("back", "")
        back = back if back.startswith("/") and not back.startswith("//") else url_for("items")
        if not skus:
            flash("Tick at least one item first.", "error")
            return redirect(back)
        user = g.user["id"]
        try:
            with g.conn:
                unknown = [s for s in skus if not g.conn.execute("SELECT 1 FROM items WHERE sku = ?", (s,)).fetchone()]
                if unknown:
                    raise ValueError("Unknown SKU: %s" % ", ".join(unknown))
                if action == "move":
                    code = db.check_code(request.form.get("tote"))
                    for sku in skus:
                        db.move(g.conn, sku, code, "web", user)
                    done = "Moved %d items to %s." % (len(skus), code)
                elif action in ("missing", "found"):
                    for sku in skus:
                        db.set_missing(g.conn, sku, action == "missing", "web", user)
                    done = "Marked %d items %s." % (len(skus), action)
                elif action == "status":
                    status = request.form.get("status", "")
                    if status not in db.STATUSES:
                        raise ValueError("Pick a status.")
                    note = request.form.get("note", "").strip() or None
                    for sku in skus:
                        db.set_status(g.conn, sku, status, "web", user, data={"note": note, "manual": True, "bulk": len(skus)})
                    done = "Set %d items to %s." % (len(skus), status)
                elif action == "drop":
                    percent = _float(request.form.get("percent"))
                    if percent is None or not 0 < percent < 90:
                        raise ValueError("Enter a price drop between 1 and 89 percent.")
                    rows = g.conn.execute("SELECT sku, status, price FROM items WHERE sku IN (%s)" % ",".join("?" * len(skus)), skus).fetchall()
                    not_listed = [r["sku"] for r in rows if r["status"] != "LISTED" or not r["price"]]
                    if not_listed:
                        raise ValueError("Only listed items with a price can get a price suggestion: %s" % ", ".join(not_listed))
                    for r in rows:
                        db.suggest_price(g.conn, r["sku"], db.price_drop(r["price"], percent), "web", user,
                                         note="%g%% drop" % percent)
                    done = "Suggested a %g%% lower price for %d items. Change them on eBay, then press Applied on each." % (percent, len(rows))
                else:
                    raise ValueError("Pick an action.")
            flash(done, "ok")
        except ValueError as e:
            flash("%s. Nothing was changed." % str(e).rstrip("."), "error")
        return redirect(back)

    @app.route("/items/<sku>/missing", methods=["POST"])
    @login_required
    def item_missing(sku):
        try:
            with g.conn:
                db.set_missing(g.conn, sku, request.form.get("missing") == "1", "web", g.user["id"])
        except ValueError as e:
            flash(str(e), "error")
        back = request.form.get("back", "")
        return redirect(back if back.startswith("/") and not back.startswith("//") else url_for("item", sku=sku))

    @app.route("/relists", methods=["GET", "POST"])
    @login_required
    def relists():
        if request.method == "POST":
            pairs = [p.split(":") for p in request.form.getlist("pair")]
            with g.conn:
                for old, new in pairs:
                    db.link_relist(g.conn, old, new, "web", g.user["id"])
            flash("Linked %d relisted items." % len(pairs), "ok")
            return redirect(url_for("relists"))
        pairs = db.suggest_relists(g.conn)
        suggested = {old for old, _ in pairs}
        titles = dict(g.conn.execute("SELECT sku, title FROM items WHERE sku IN (%s)" % ",".join("?" * len(pairs) * 2),
                                     [s for p in pairs for s in p]).fetchall()) if pairs else {}
        rows = [r for r in g.conn.execute(
            ITEMS_SQL + " WHERE i.status IN (?, ?, ?) AND i.relisted_as IS NULL ORDER BY l.code IS NULL, l.code, i.sku",
            db.RELISTABLE) if r["sku"] not in suggested]
        return render_template("relists.html", pairs=pairs, titles=titles, rows=rows)

    @app.route("/totes")
    @login_required
    def totes():
        rows = g.conn.execute(
            "SELECT l.code, l.kind, l.notes, l.checked_at, SUM(i.missing_since IS NOT NULL AND i.status NOT IN ('SENT_TO_POST_OFFICE', 'DELIVERED')) AS missing,"
            " SUM(i.status = 'LISTED') AS listed, SUM(i.status IN ('PENDING', 'SKIPPED', 'ENDED', 'CANCELLED', 'RETURNED')) AS unlisted,"
            " SUM(i.status IN ('BOUGHT', 'LABEL_PRINTED', 'PACKAGED')) AS to_ship"
            " FROM locations l LEFT JOIN items i ON i.location_id = l.id"
            + (" WHERE l.kind = 'tote' AND l.checked_at IS NULL" if request.args.get("unchecked") else "")
            + " GROUP BY l.id"
            + (" HAVING SUM(i.status NOT IN ('SENT_TO_POST_OFFICE', 'DELIVERED')) > 0" if request.args.get("unchecked") else "")
            + order_by(TOTE_SORTS, "code", then="l.code")
        ).fetchall()
        return render_template("totes.html", rows=rows, suggestion=db.suggest_tote(g.conn),
                               unchecked=bool(request.args.get("unchecked")))

    def _codes(text):
        return [db.check_code(c) for c in re.split(r"[\s,+]+", text or "") if c.strip()]

    @app.route("/totes/consolidate", methods=["GET", "POST"])
    @login_required
    def consolidate():
        try:
            codes = list(dict.fromkeys(_codes(request.values.get("codes"))))
        except ValueError as e:
            flash(str(e), "error")
            return redirect(url_for("totes"))
        if len(codes) < 2:
            flash("Enter at least two totes to combine, e.g. B4 C6.", "error")
            return redirect(url_for("totes"))
        suggestion = db.suggest_tote(g.conn, freeing=codes)
        if request.method == "POST":
            choice = request.form.get("into")
            try:
                into = db.check_code(request.form.get("custom") if choice == "custom" else choice)
            except ValueError as e:
                flash(str(e), "error")
                return redirect(url_for("consolidate", codes=" ".join(codes)))
            if into not in codes and into in db.totes_in_use(g.conn):
                flash("%s already has items in it. Pick it only if you mean to add to it: list it with the others." % into, "error")
                return redirect(url_for("consolidate", codes=" ".join(codes)))
            with g.conn:
                n = db.consolidate(g.conn, codes, into, "web", g.user["id"])
            flash("Moved %d items into %s. Relabel the tote as %s." % (n, into, into), "ok")
            return redirect(url_for("tote", code=into))
        totes = [(c, len(db.on_hand(g.conn, c))) for c in codes]
        return render_template("consolidate.html", codes=codes, totes=totes, suggestion=suggestion)

    @app.route("/totes/<code>", methods=["GET", "POST"])
    @login_required
    def tote(code):
        code = code.upper()
        if request.method == "POST":
            try:
                with g.conn:
                    if request.form.get("skus"):
                        skus = [s.upper() for s in re.split(r"[\s,]+", request.form["skus"]) if s]
                        unknown = [s for s in skus if not g.conn.execute("SELECT 1 FROM items WHERE sku = ?", (s,)).fetchone()]
                        if unknown:
                            raise ValueError("Unknown SKU: %s. Nothing moved." % ", ".join(unknown))
                        for s in skus:
                            db.move(g.conn, s, code, "web", g.user["id"])
                        flash("Moved %d items into %s." % (len(skus), code), "ok")
                    plan = {k[len("to_"):]: db.check_code(v) for k, v in request.form.items()
                            if k.startswith("to_") and v.strip() and db.check_code(v) != code}
                    if plan:
                        db.split(g.conn, code, plan, "web", g.user["id"])
                        flash("Moved %d items: %s." % (len(plan), ", ".join(
                            "%d to %s" % (list(plan.values()).count(t), t) for t in sorted(set(plan.values())))), "ok")
            except ValueError as e:
                flash(str(e), "error")
            return redirect(url_for("tote", code=code))
        rows = g.conn.execute(ITEMS_SQL + " WHERE l.code = ?" + order_by(ITEM_SORTS, "status"), (code,)).fetchall()
        here = [r for r in rows if r["status"] not in db.GONE]
        gone = [r for r in rows if r["status"] in db.GONE]
        place = g.conn.execute("SELECT * FROM locations WHERE code = ?", (code,)).fetchone()
        codes = sorted(db.totes_in_use(g.conn) | set(db.PLACES))
        checked = g.conn.execute(
            "SELECT e.at, u.name FROM events e LEFT JOIN users u ON u.id = e.user_id"
            " WHERE e.entity = 'location' AND e.entity_id = ? AND e.action = 'checked' ORDER BY e.id DESC LIMIT 1", (code,)
        ).fetchone()
        return render_template("tote.html", code=code, place=place, rows=here, gone=gone, codes=codes,
                               suggestion=db.suggest_tote(g.conn), checked=checked, statuses=db.STATUSES)

    @app.route("/labels")
    @login_required
    def labels():
        """Printable 4x6 tote labels: the code in big letters and a QR code that opens the tote's page."""
        try:
            codes = list(dict.fromkeys(_codes(request.args.get("codes", ""))))
        except ValueError as e:
            flash(str(e), "error")
            return redirect(url_for("totes"))
        if not codes:
            codes = sorted(db.totes_in_use(g.conn), key=lambda c: (c in db.PLACES, c[0], int(c[1:]) if c[1:].isdigit() else 0))
        base = config.LABEL_URL or request.host_url.rstrip("/")
        made = [{"code": c, "url": base + url_for("tote", code=c), "qr": _qr_svg(base + url_for("tote", code=c)),
                 "count": len(db.on_hand(g.conn, c))} for c in codes]
        return render_template("labels.html", labels=made, base=base, label_url_set=bool(config.LABEL_URL))

    @app.route("/totes/<code>/checked", methods=["POST"])
    @login_required
    def tote_checked(code):
        code = code.upper()
        with g.conn:
            db.mark_checked(g.conn, code, "web", g.user["id"])
        flash("Recorded that %s was checked." % code, "ok")
        return redirect(url_for("tote", code=code))

    # --- shipping ---

    @app.route("/picklist")
    @login_required
    def picklist():
        days = request.args.get("days", "")
        rows = orders.pick_list(g.conn, int(days) if days.isdigit() else None)
        groups = []
        for r in rows:
            if not groups or groups[-1][0] != r["tote"]:
                groups.append((r["tote"], []))
            groups[-1][1].append(r)
        last_sync = g.conn.execute(
            "SELECT MAX(at) FROM events WHERE actor = 'sync'"
        ).fetchone()[0]
        return render_template("picklist.html", groups=groups, count=len(rows), days=days,
                               today=datetime.date.today().isoformat(), printed=db.now()[:16].replace("T", " "),
                               last_sync=last_sync)

    @app.route("/picklist/status", methods=["POST"])
    @login_required
    def picklist_status():
        status = request.form["status"]
        if status not in ("BOUGHT", "LABEL_PRINTED", "PACKAGED", "SENT_TO_POST_OFFICE"):
            abort(400)
        with g.conn:
            db.set_status(g.conn, request.form["sku"], status, "web", g.user["id"])
        return redirect(url_for("picklist", days=request.form.get("days", "")))

    @app.route("/sync", methods=["POST"])
    @login_required
    def sync():
        try:
            counts = orders.sync(g.conn, user=g.user["id"])
            flash("%d orders checked, %d new sales." % (counts["orders"], counts["new_lines"]), "ok")
            for sku, before, after, title in counts["changes"]:
                flash("%s: %s → %s  %s" % (sku, before, after, title), "ok")
        except (api.EbayError, SystemExit) as e:
            flash("Order sync failed: %s" % e, "error")
        return redirect(request.form.get("back") or url_for("picklist"))

    # --- activity and numbers ---

    @app.route("/activity")
    @login_required
    def activity():
        who = request.args.get("who", "")
        action = request.args.get("action", "")
        q = request.args.get("q", "").strip().upper()
        page = max(request.args.get("page", 1, type=int), 1)
        sql = ("SELECT e.*, u.name AS user FROM events e LEFT JOIN users u ON u.id = e.user_id WHERE 1 = 1")
        params = []
        if who == "(none)":
            sql += " AND e.user_id IS NULL"
        elif who:
            sql += " AND u.name = ?"
            params.append(who)
        if action:
            sql += " AND e.action = ?"
            params.append(action)
        if q:
            sql += " AND e.entity_id = ?"
            params.append(q)
        sql += " ORDER BY e.id DESC LIMIT 101 OFFSET ?"
        params.append((page - 1) * 100)
        rows = g.conn.execute(sql, params).fetchall()
        users = [r["name"] for r in g.conn.execute("SELECT name FROM users ORDER BY name")]
        actions = [r["action"] for r in g.conn.execute("SELECT DISTINCT action FROM events ORDER BY action")]
        return render_template("activity.html", rows=rows[:100], more=len(rows) > 100, page=page, who=who,
                               action=action, q=q, users=users, actions=actions, parse_json=_parse_json,
                               people=sales.by_user(g.conn))

    @app.route("/sales")
    @login_required
    def sales_page():
        return render_template("sales.html", s=sales.summary(g.conn), months=sales.by_month(g.conn),
                               bands=sales.price_bands(g.conn), active=sales.active(g.conn))


# Days since the item's latest listing went up, and when anything last happened to it.
DAYS_LISTED = "CAST(julianday('now') - julianday((SELECT MAX(listed_at) FROM listings WHERE sku = i.sku)) AS INTEGER)"
LAST_CHANGE = "(SELECT MAX(at) FROM events WHERE entity = 'item' AND entity_id = i.sku)"

# Sortable columns: URL key -> SQL.
ITEM_SORTS = {
    "sku": "i.sku",
    "status": "i.status",
    "tote": ("substr(l.code, 1, 1)", "CAST(substr(l.code, 2) AS INTEGER)"),
    "price": "i.price",
    "title": "lower(i.title)",
    "days": DAYS_LISTED,
    "changed": LAST_CHANGE,
}
TOTE_SORTS = {
    "code": ("l.kind = 'place'", "substr(l.code, 1, 1)", "CAST(substr(l.code, 2) AS INTEGER)"),
    "listed": "listed",
    "unlisted": "unlisted",
    "to_ship": "to_ship",
    "missing": "missing",
    "checked": "l.checked_at",
}

ITEMS_SQL = """
    SELECT i.*, l.code AS tote,
           (SELECT listing_id FROM listings WHERE sku = i.sku ORDER BY listed_at DESC LIMIT 1) AS listing_id,
           %s AS days_listed, %s AS last_change, %s AS suggested_price
    FROM items i LEFT JOIN locations l ON l.id = i.location_id
""" % (DAYS_LISTED, LAST_CHANGE, db.LATEST_SUGGESTION)


def _qr_svg(text):
    """A QR code as inline SVG markup (sized by CSS)."""
    out = io.BytesIO()
    segno.make(text, error="m").save(out, kind="svg", border=2, xmldecl=False, svgns=True, omitsize=True,
                                     svgclass="qr", nl=False)
    return Markup(out.getvalue().decode())


def _money_fmt(value):
    return "" if value is None else "${:,.2f}".format(value)


def _parse_json(text):
    return json.loads(text) if text else None


def _show(value):
    if value is None:
        return "empty"
    if isinstance(value, float):
        return "%.2f" % value
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v) for v in value)
    return str(value)


def event_details(data_json):
    """An event's data as one readable line, e.g. "price: 12.99 → 14.00; notes: empty → box is dented"."""
    data = _parse_json(data_json)
    if not data:
        return ""
    if isinstance(data.get("from"), dict) and isinstance(data.get("to"), dict):
        return "; ".join("%s: %s → %s" % (k, _show(data["from"].get(k)), _show(data["to"].get(k))) for k in data["to"])
    return "; ".join("%s: %s" % (k, _show(v)) for k, v in data.items() if v is not None)
