"""Drafts: listings being prepared, one row per SKU in the `drafts` table.

Claude writes a new batch as batches/<batch>/drafts.json with the photos beside it in
batches/<batch>/photos/, and `cli.py import-drafts <batch>` loads it. From then on the
database copy is the one that counts: the web editor, comps and publish all read and
write it, and every change records who made it.

A draft is a JSON document (title, description, aspects, photos, package, comps,
needs_check, ...) because its shape follows eBay's listing fields. While it's
unpublished, its title, condition and price are mirrored onto the item's row.
"""

import json
import os

from . import config, db


class Conflict(Exception):
    """Someone else saved the draft after it was loaded."""


def photos_dir(batch):
    return os.path.join(config.ROOT, "batches", batch, "photos")


def _item(row):
    item = json.loads(row["data"])
    item["sku"] = row["sku"]
    item["_version"] = row["version"]
    return item


def batch_names(conn):
    return [r["name"] for r in conn.execute(
        "SELECT DISTINCT b.name FROM drafts d JOIN batches b ON b.id = d.batch_id ORDER BY b.name")]


def load(conn, batch):
    """{"batch", "tote", "source_zip", "items": [draft, ...]} for a batch, or None."""
    b = conn.execute(
        "SELECT b.id, b.name, b.source, l.code AS tote FROM batches b LEFT JOIN locations l ON l.id = b.location_id"
        " WHERE b.name = ?", (batch,)).fetchone()
    if b is None:
        return None
    rows = conn.execute("SELECT * FROM drafts WHERE batch_id = ? ORDER BY position, sku", (b["id"],)).fetchall()
    if not rows:
        return None
    return {"batch": b["name"], "tote": b["tote"], "source_zip": b["source"], "items": [_item(r) for r in rows]}


def get(conn, sku):
    """(batch name, draft) for a SKU, or (None, None)."""
    row = conn.execute(
        "SELECT d.*, b.name AS batch FROM drafts d JOIN batches b ON b.id = d.batch_id WHERE d.sku = ?", (sku,)
    ).fetchone()
    return (row["batch"], _item(row)) if row else (None, None)


def save(conn, item, actor, user=None, changed=None, version=None):
    """Store a draft (call inside a transaction).

    With `version`, refuses with Conflict if someone saved it since that version was loaded.
    `changed` names the edited fields for the event log; leave it out for bookkeeping
    saves (like recording the published listing) that are logged elsewhere.
    """
    data = {k: v for k, v in item.items() if k not in ("sku", "_version")}
    sql = "UPDATE drafts SET data = ?, version = version + 1, updated_at = ?, updated_by = ? WHERE sku = ?"
    params = [json.dumps(data), db.now(), user, item["sku"]]
    if version is not None:
        sql += " AND version = ?"
        params.append(version)
    if conn.execute(sql, params).rowcount == 0:
        raise Conflict(item["sku"])
    item["_version"] = conn.execute("SELECT version FROM drafts WHERE sku = ?", (item["sku"],)).fetchone()[0]
    status = conn.execute("SELECT status FROM items WHERE sku = ?", (item["sku"],)).fetchone()["status"]
    if status in ("PENDING", "SKIPPED"):
        conn.execute(
            "UPDATE items SET title = ?, condition = ?, price = ?, updated_at = ? WHERE sku = ?",
            (item.get("title"), item.get("condition"), item.get("price"), db.now(), item["sku"]),
        )
        db.set_status(conn, item["sku"], "SKIPPED" if item.get("skip") else "PENDING", actor, user)
    if changed:
        db.log_event(conn, "item", item["sku"], "edited", actor, user, data={"fields": changed})


def import_json(conn, path, actor, user=None):
    """Add the drafts in a drafts.json file. Returns (added, already there).

    Drafts already in the database are left alone, so edits made since the first
    import are never overwritten. An existing item keeps its status and fields.
    """
    with open(path) as f:
        batch = json.load(f)
    batch_id = db.batch_id(conn, batch["batch"], batch.get("tote"), batch.get("source_zip"))
    added = existing = 0
    for position, item in enumerate(batch["items"]):
        sku = item["sku"]
        if conn.execute("SELECT 1 FROM drafts WHERE sku = ?", (sku,)).fetchone():
            existing += 1
            continue
        published = item.get("published", {}).get(config.ENV)
        if not conn.execute("SELECT 1 FROM items WHERE sku = ?", (sku,)).fetchone():
            db.upsert_item(
                conn, sku, "SKIPPED" if item.get("skip") else "LISTED" if published else "PENDING", actor, user,
                at=published["listed_at"] if published else None,
                title=item.get("title"), condition=item.get("condition"), price=item.get("price"),
                batch_id=batch_id, location_id=db.location_id(conn, batch.get("tote")),
            )
        if published:
            db.record_listing(conn, sku, config.ENV, published["listing_id"], published.get("offer_id"),
                              item.get("price"), published["listed_at"], actor, user)
        data = {k: v for k, v in item.items() if k != "sku"}
        conn.execute(
            "INSERT INTO drafts (sku, batch_id, position, data, updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?)",
            (sku, batch_id, position, json.dumps(data), db.now(), user),
        )
        db.log_event(conn, "item", sku, "drafted", actor, user, data={"batch": batch["batch"]})
        added += 1
    return added, existing


def export_json(conn, batch, path):
    """Write a batch back out in the drafts.json format (for backups or hand edits by Claude)."""
    drafts = load(conn, batch)
    if drafts is None:
        raise ValueError("No drafts in batch %s" % batch)
    for item in drafts["items"]:
        item.pop("_version", None)
    with open(path, "w") as f:
        json.dump(drafts, f, indent=2)
    return len(drafts["items"])
