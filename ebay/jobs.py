"""Background work, so the phone doesn't wait: for now, drafting the items shot in the app.

A job drafts each item of one or more finished batches with Claude, one item at a time, and records
its progress in the `jobs` table so the app can show it. Items that already have a draft are skipped,
so a job that was interrupted (the server restarted, say) is finished by simply starting another.
Each item costs about $0.10 with Opus; nothing starts a job except a person pressing Create drafts.
"""

import datetime
import json
import os

from . import capture, db, drafter, drafts

STALE_SECONDS = 180  # a running job that hasn't reported for this long was interrupted
MAX_ITEMS = 300      # per job: a mistake shouldn't cost more than about $30
COST_PER_ITEM = {"claude-opus-5-5": 0.10, "claude-sonnet-5-5": 0.05, "claude-haiku-5-5": 0.005}


class JobError(ValueError):
    """The job can't be started; the message says why in plain English."""


def _undrafted(conn, batch_id):
    return [r for r in capture.items(conn, batch_id) if not r["drafted"]]


def get(conn, job_id):
    """The job as a dict, with status "interrupted" for a running job that stopped reporting."""
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise LookupError("No job %s." % job_id)
    job = dict(row, params=json.loads(row["params"]), errors=json.loads(row["errors"]))
    if job["status"] in ("queued", "running"):
        last = datetime.datetime.fromisoformat(job["heartbeat_at"] or job["created_at"])
        if (datetime.datetime.now() - last).total_seconds() > STALE_SECONDS:
            job["status"] = "interrupted"
    return job


def _live(conn):
    rows = conn.execute("SELECT id FROM jobs WHERE status IN ('queued', 'running')").fetchall()
    return [j for j in (get(conn, r["id"]) for r in rows) if j["status"] in ("queued", "running")]


def start_drafts(conn, batch_ids, user, model=None):
    """Queue drafting for every undrafted item with photos in these finished app batches. Call inside a
    transaction. Returns (job id, SKUs left out because they have no photos)."""
    model = drafter.MODELS.get(model or "opus", model)
    if model not in COST_PER_ITEM:
        raise JobError("Pick opus, sonnet or haiku.")
    if not batch_ids:
        raise JobError("Pick at least one batch.")
    skus, empty, names = [], [], []
    for batch_id in dict.fromkeys(batch_ids):
        batch = capture.batch_row(conn, batch_id)  # LookupError for an unknown one
        if batch["source"] != "app":
            raise JobError("Batch %s wasn't shot in the app." % batch["name"])
        if not batch["finished_at"]:
            raise JobError("Finish tote %s first, so nothing is added while it's being drafted." % batch["tote"])
        for item in _undrafted(conn, batch_id):
            (skus if item["photos"] else empty).append(item["sku"])
        names.append(batch["name"])
    if not skus:
        raise JobError("Nothing to draft: every item already has a draft or has no photos.")
    if len(skus) > MAX_ITEMS:
        raise JobError("That's %d items; draft at most %d at a time." % (len(skus), MAX_ITEMS))
    busy = {s for j in _live(conn) for s in j["params"]["skus"]} & set(skus)
    if busy:
        raise JobError("%d of these items are already being drafted." % len(busy))
    cur = conn.execute(
        "INSERT INTO jobs (kind, params, status, total, user_id, created_at) VALUES ('drafts', ?, 'queued', ?, ?, ?)",
        (json.dumps({"batches": names, "skus": skus, "model": model}), len(skus), user, db.now()))
    db.log_event(conn, "job", str(cur.lastrowid), "started", "api", user,
                 data={"kind": "drafts", "batches": names, "items": len(skus), "model": model})
    return cur.lastrowid, empty


def estimate(model, count):
    return round(COST_PER_ITEM[drafter.MODELS.get(model or "opus", model)] * count, 2)


def _draft_one(conn, claude, model, sku, batches_dir, user):
    """Draft one item and store it. Returns the run's cost; raises on failure."""
    batch_name, item = drafts.get(conn, sku)
    if item.get("drafted_by") or item.get("title"):
        return 0.0  # done already (an earlier, interrupted job)
    photos = [os.path.join(batches_dir, batch_name, "photos", p) for p in item.get("photos", [])]
    new, run = drafter.draft(claude, model, None, photos, note=capture.note_text(item.get("note")))
    # Keep what the phone recorded (photos, note); take everything Claude wrote. If the phone changed the
    # item while Claude was working, merge into that newer version rather than overwrite it.
    written = {k: v for k, v in new.items() if k not in ("photos", "note", "captured")}
    with conn:
        try:
            drafts.save(conn, {**item, **written}, "auto", user, version=item["_version"])
        except drafts.Conflict:
            _, fresh = drafts.get(conn, sku)
            drafts.save(conn, {**fresh, **written}, "auto", user)
        db.log_event(conn, "item", sku, "drafted", "auto", user,
                     data={"batch": batch_name, "model": run["model"], "cost_usd": run["cost_usd"]})
    return run["cost_usd"]


def run(db_path, job_id, batches_dir, claude=None):
    """Work through a queued job (in a background thread, or inline in tests)."""
    conn = db.connect(db_path)
    try:
        job = get(conn, job_id)
        with conn:
            conn.execute("UPDATE jobs SET status = 'running', heartbeat_at = ? WHERE id = ?", (db.now(), job_id))
        try:
            claude = claude or drafter.client()
        except SystemExit as e:  # no API key
            with conn:
                conn.execute("UPDATE jobs SET status = 'failed', errors = ?, finished_at = ? WHERE id = ?",
                             (json.dumps([{"sku": None, "error": str(e)}]), db.now(), job_id))
            return
        errors, cost = [], 0.0
        for n, sku in enumerate(job["params"]["skus"], start=1):
            try:
                cost += _draft_one(conn, claude, job["params"]["model"], sku, batches_dir, job["user_id"])
            except Exception as e:  # keep going; the person sees which items failed and why
                errors.append({"sku": sku, "error": str(e)[:300]})
            with conn:
                conn.execute("UPDATE jobs SET done = ?, errors = ?, cost_usd = ?, heartbeat_at = ? WHERE id = ?",
                             (n, json.dumps(errors), round(cost, 4), db.now(), job_id))
        with conn:
            status = "failed" if errors and len(errors) == len(job["params"]["skus"]) else "done"
            conn.execute("UPDATE jobs SET status = ?, finished_at = ? WHERE id = ?", (status, db.now(), job_id))
            db.log_event(conn, "job", str(job_id), "finished", "auto", job["user_id"],
                         data={"drafted": len(job["params"]["skus"]) - len(errors), "errors": len(errors),
                               "cost_usd": round(cost, 4)})
    finally:
        conn.close()
