"""The phone app's camera flow: a batch is one tote, shot item by item.

Each item gets its SKU the moment it's started (so a sticker can be printed then and there) and a
row in `drafts` holding its photos and note. Photos belong to their item from the moment they're
taken, so nothing is split or guessed later; Claude drafts each item from its photos and note.

Any item in a batch can still be changed until it's drafted: more photos, a different order, a better
note. That's what lets someone swipe back to item 2 for the photo of the mug's bottom they forgot.

A photo is stored under the ID the phone gave it, so sending it again (an offline queue retrying)
finds the copy already there instead of adding a second one.
"""

import datetime
import json
import os
import re

from . import db, drafts

CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")
MAX_PHOTO_BYTES = 30 * 1024 * 1024
# JPEG or PNG: the drafter can't read HEIC, so the app converts (iOS does it in one call).
PHOTO_TYPES = {"image/jpeg": "jpg", "image/png": "png"}

# Quick choices for an item's note. Free text sits beside them, and Claude reads both, so a tag and
# text that say the same thing (or more) are fine.
NOTE_TAGS = [
    "Tested, works", "Untested", "Not working / for parts", "New, sealed", "New, open box", "Like new",
    "Missing parts", "Cosmetic wear", "Has original box", "No box", "Batteries not included", "Lot / bundle",
]


def _dir(batches_dir, name):
    """The batch's photos folder: under `batches_dir` when given (the web app's setting, a temp folder in tests)."""
    return os.path.join(batches_dir, name, "photos") if batches_dir else drafts.photos_dir(name)


class CaptureError(ValueError):
    """Something the person asked for can't be done; the message says why in plain English."""


def _now_name(tote, conn):
    """A batch name like A5-20261111, with -2, -3 ... when the tote is shot again the same day."""
    base = "%s-%s" % (tote, datetime.date.today().strftime("%Y%m%d"))
    name, n = base, 1
    while conn.execute("SELECT 1 FROM batches WHERE name = ?", (name,)).fetchone():
        n += 1
        name = "%s-%d" % (base, n)
    return name


def batch_row(conn, batch_id):
    row = conn.execute(
        "SELECT b.*, l.code AS tote FROM batches b LEFT JOIN locations l ON l.id = b.location_id WHERE b.id = ?",
        (batch_id,)).fetchone()
    if row is None:
        raise LookupError("No batch %s." % batch_id)
    return row


def start_batch(conn, tote, user, batches_dir=None):
    """Open a batch for a tote (the next free one when `tote` is empty). Call inside a transaction."""
    tote = db.check_code(tote) if tote else db.suggest_tote(conn)
    name = _now_name(tote, conn)
    batch_id = db.batch_id(conn, name, tote, "app")
    conn.execute("UPDATE batches SET photographer_id = ? WHERE id = ?", (user, batch_id))
    os.makedirs(_dir(batches_dir, name), exist_ok=True)
    db.log_event(conn, "batch", name, "started", "api", user, data={"tote": tote})
    return batch_id


def _open(conn, batch_id):
    batch = batch_row(conn, batch_id)
    if batch["source"] != "app":
        raise CaptureError("Batch %s wasn't shot in the app; edit it on the website." % batch["name"])
    return batch


def _draft(conn, batch, sku):
    row = conn.execute("SELECT * FROM drafts WHERE sku = ? AND batch_id = ?", (sku, batch["id"])).fetchone()
    if row is None:
        raise LookupError("%s isn't in batch %s." % (sku, batch["name"]))
    item = json.loads(row["data"])
    item["sku"], item["_version"] = sku, row["version"]
    if item.get("drafted_by"):
        raise CaptureError("%s already has its draft; change it on the review page." % sku)
    return item


def new_item(conn, batch_id, user):
    """Start the next item in the batch: a new SKU, in the batch's tote, with no photos yet."""
    batch = _open(conn, batch_id)
    if batch["finished_at"]:
        raise CaptureError("Tote %s is finished; start a new batch to add items." % batch["tote"])
    sku = db.next_sku(conn)
    db.upsert_item(conn, sku, "PENDING", "api", user)
    conn.execute("UPDATE items SET location_id = ?, batch_id = ?, updated_at = ? WHERE sku = ?",
                 (batch["location_id"], batch["id"], db.now(), sku))
    position = conn.execute("SELECT COALESCE(MAX(position), -1) + 1 FROM drafts WHERE batch_id = ?",
                            (batch["id"],)).fetchone()[0]
    data = {"captured": True, "photos": [], "note": {"tags": [], "text": ""}}
    conn.execute("INSERT INTO drafts (sku, batch_id, position, data, updated_at, updated_by) VALUES (?, ?, ?, ?, ?, ?)",
                 (sku, batch["id"], position, json.dumps(data), db.now(), user))
    return sku


def _save(conn, item, user):
    data = {k: v for k, v in item.items() if k not in ("sku", "_version")}
    conn.execute("UPDATE drafts SET data = ?, version = version + 1, updated_at = ?, updated_by = ? WHERE sku = ?",
                 (json.dumps(data), db.now(), user, item["sku"]))


def add_photo(conn, batch_id, sku, client_id, content_type, data, user, batches_dir=None):
    """Store one photo for an item. Returns (filename, added); the same client_id again changes nothing."""
    batch = _open(conn, batch_id)
    item = _draft(conn, batch, sku)
    if not CLIENT_ID_RE.match(client_id or ""):
        raise CaptureError("A photo needs an ID of 8 to 64 letters, digits or dashes (a UUID works).")
    ext = PHOTO_TYPES.get((content_type or "").split(";")[0].strip().lower())
    if not ext:
        raise CaptureError("Photos must be JPEG or PNG.")
    if not data:
        raise CaptureError("That photo was empty.")
    if len(data) > MAX_PHOTO_BYTES:
        raise CaptureError("That photo is over %d MB." % (MAX_PHOTO_BYTES // (1024 * 1024)))
    filename = "%s-%s.%s" % (sku, client_id, ext)
    if filename in item["photos"]:
        return filename, False
    path = os.path.join(_dir(batches_dir, batch["name"]), filename)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".part", "wb") as f:
        f.write(data)
    os.replace(path + ".part", path)  # never leave half a photo behind
    item["photos"].append(filename)
    _save(conn, item, user)
    return filename, True


def update_item(conn, batch_id, sku, user, note=None, photos=None):
    """Change an item's note ({"tags": [...], "text": "..."}) and/or photo order (a reordering of its photos)."""
    batch = _open(conn, batch_id)
    item = _draft(conn, batch, sku)
    if note is not None:
        tags = note.get("tags", [])
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise CaptureError("Note tags must be a list of words.")
        item["note"] = {"tags": [t.strip()[:60] for t in tags if t.strip()][:20],
                        "text": str(note.get("text") or "")[:4000]}
    if photos is not None:
        if sorted(photos) != sorted(item["photos"]):
            raise CaptureError("To reorder photos, send all of this item's photos (use delete to remove one).")
        item["photos"] = list(photos)
    _save(conn, item, user)
    return item


def delete_photo(conn, batch_id, sku, client_id, user, batches_dir=None):
    batch = _open(conn, batch_id)
    item = _draft(conn, batch, sku)
    match = [p for p in item["photos"] if p.rsplit(".", 1)[0] == "%s-%s" % (sku, client_id)]
    if not match:
        return False
    item["photos"].remove(match[0])
    _save(conn, item, user)
    path = os.path.join(_dir(batches_dir, batch["name"]), match[0])
    if os.path.exists(path):
        os.remove(path)
    return True


def delete_item(conn, batch_id, sku, user, batches_dir=None):
    """Drop an item started by mistake. Its SKU isn't reused; the item is kept as SKIPPED for the record."""
    batch = _open(conn, batch_id)
    item = _draft(conn, batch, sku)
    for photo in item["photos"]:
        path = os.path.join(_dir(batches_dir, batch["name"]), photo)
        if os.path.exists(path):
            os.remove(path)
    conn.execute("DELETE FROM drafts WHERE sku = ?", (sku,))
    db.set_status(conn, sku, "SKIPPED", "api", user, data={"note": "deleted while shooting %s" % batch["name"]})


def finish_batch(conn, batch_id, user):
    """The tote is done. Items can still get photos and notes until they're drafted, but no new items."""
    batch = _open(conn, batch_id)
    if not batch["finished_at"]:
        conn.execute("UPDATE batches SET finished_at = ? WHERE id = ?", (db.now(), batch["id"]))
        count = conn.execute("SELECT COUNT(*) FROM drafts WHERE batch_id = ?", (batch["id"],)).fetchone()[0]
        db.log_event(conn, "batch", batch["name"], "finished", "api", user, data={"items": count})


def items(conn, batch_id):
    """The batch's items in shooting order, each {"sku", "position", "photos", "note", "drafted", "draft"}
    (draft: its title, price, condition and open questions once Claude has written it)."""
    rows = conn.execute("SELECT sku, position, data FROM drafts WHERE batch_id = ? ORDER BY position, sku",
                        (batch_id,)).fetchall()
    out = []
    for r in rows:
        data = json.loads(r["data"])
        drafted = bool(data.get("drafted_by") or data.get("title"))
        out.append({"sku": r["sku"], "position": r["position"], "photos": data.get("photos", []),
                    "note": data.get("note") or {"tags": [], "text": data.get("whiteboard_notes", "")},
                    "drafted": drafted,
                    "draft": {k: data.get(k) for k in ("title", "price", "condition", "needs_check")} if drafted else None})
    return out


def note_text(note):
    """An item's note as one piece of text for the drafter: the tags, then what was typed."""
    note = note or {}
    return "\n".join(p for p in ("; ".join(note.get("tags") or []), (note.get("text") or "").strip()) if p)


def recent_tags(conn, user, limit=6):
    """The note tags this person used most in their last 200 captured items, most used first."""
    counts = {}
    for (data,) in conn.execute(
            "SELECT d.data FROM drafts d JOIN batches b ON b.id = d.batch_id WHERE b.source = 'app'"
            " AND d.updated_by = ? ORDER BY d.updated_at DESC LIMIT 200", (user,)):
        for tag in (json.loads(data).get("note") or {}).get("tags", []):
            counts[tag] = counts.get(tag, 0) + 1
    return [t for t, _ in sorted(counts.items(), key=lambda kv: -kv[1])][:limit]
