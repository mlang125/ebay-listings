"""The database: one record per physical item, plus an append-only event log.

SQLite for now (a single file, path in config.DB_PATH). Plain SQL with `?`
parameters and no SQLite-only types, so moving to PostgreSQL later is a swap.

Status changes go through set_status(), which writes an event every time; the
item's `status` column is just the latest one, kept for fast lookups.
"""

import datetime
import json
import os
import re
import sqlite3

from . import config

STATUSES = [
    "PENDING",  # drafted, not yet listed
    "LISTED",
    "BOUGHT",
    "LABEL_PRINTED",
    "PACKAGED",
    "SENT_TO_POST_OFFICE",
    "DELIVERED",
    "SKIPPED",  # drafted but never to be listed by us (e.g. already listed elsewhere)
    "ENDED",  # listing ended without a sale
    "CANCELLED",
    "RETURNED",
]

# Each entry upgrades the schema by one version (tracked in PRAGMA user_version).
# Never edit an entry that has shipped; add a new one.
MIGRATIONS = [
    """
    CREATE TABLE users (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        role TEXT NOT NULL DEFAULT 'admin',  -- admin / lister / shipper
        active INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    );

    -- Totes now; later shelves, racks and zones (parent_id nests them). Two-letter codes
    -- are places that aren't totes (see PLACES).
    CREATE TABLE locations (
        id INTEGER PRIMARY KEY,
        code TEXT NOT NULL UNIQUE,
        kind TEXT NOT NULL DEFAULT 'tote',
        parent_id INTEGER REFERENCES locations(id),
        notes TEXT
    );

    CREATE TABLE batches (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL UNIQUE,
        location_id INTEGER REFERENCES locations(id),
        photographer_id INTEGER REFERENCES users(id),
        source TEXT,
        created_at TEXT NOT NULL
    );

    CREATE TABLE items (
        sku TEXT PRIMARY KEY,
        title TEXT,
        condition TEXT,
        status TEXT NOT NULL,
        location_id INTEGER REFERENCES locations(id),
        batch_id INTEGER REFERENCES batches(id),
        price NUMERIC,
        cost NUMERIC,  -- NULL means unknown, not zero
        cost_note TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE listings (
        id INTEGER PRIMARY KEY,
        sku TEXT NOT NULL REFERENCES items(sku),
        channel TEXT NOT NULL DEFAULT 'ebay',
        env TEXT NOT NULL,
        listing_id TEXT NOT NULL,
        offer_id TEXT,
        price NUMERIC,
        listed_by INTEGER REFERENCES users(id),
        listed_at TEXT NOT NULL,
        ended_at TEXT,
        UNIQUE (channel, env, listing_id)
    );

    CREATE TABLE orders (
        order_id TEXT PRIMARY KEY,
        channel TEXT NOT NULL DEFAULT 'ebay',
        created_at TEXT NOT NULL,
        ship_by TEXT,
        buyer_paid_shipping NUMERIC,
        total NUMERIC,
        status TEXT
    );

    CREATE TABLE order_lines (
        line_item_id TEXT PRIMARY KEY,
        order_id TEXT NOT NULL REFERENCES orders(order_id),
        sku TEXT,  -- NULL when the sold listing isn't one of our items
        listing_id TEXT,
        title TEXT,
        price NUMERIC,
        quantity INTEGER NOT NULL DEFAULT 1
    );

    -- Append-only: never update or delete rows.
    CREATE TABLE events (
        id INTEGER PRIMARY KEY,
        at TEXT NOT NULL,
        user_id INTEGER REFERENCES users(id),
        actor TEXT NOT NULL,  -- user / cli / sync / auto / import
        entity TEXT NOT NULL,  -- item / order / batch / ...
        entity_id TEXT NOT NULL,
        action TEXT NOT NULL,
        from_status TEXT,
        to_status TEXT,
        data TEXT  -- JSON
    );
    CREATE INDEX events_entity ON events (entity, entity_id);
    CREATE INDEX events_user ON events (user_id, at);
    """,
    """
    ALTER TABLE items ADD COLUMN category TEXT;
    ALTER TABLE items ADD COLUMN notes TEXT;
    """,
    """
    ALTER TABLE items ADD COLUMN quantity INTEGER NOT NULL DEFAULT 1;
    ALTER TABLE listings ADD COLUMN condition TEXT;
    ALTER TABLE orders ADD COLUMN shipped_at TEXT;
    ALTER TABLE orders ADD COLUMN shipping_service TEXT;
    ALTER TABLE order_lines ADD COLUMN tracking TEXT;
    ALTER TABLE order_lines ADD COLUMN promoted INTEGER;
    """,
]


def now():
    return datetime.datetime.now().isoformat(timespec="seconds")


def connect(path=None):
    path = path or config.DB_PATH
    os.makedirs(os.path.dirname(path), exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    _migrate(conn)
    return conn


def _migrate(conn):
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
        with conn:
            conn.executescript(sql)
            conn.execute("PRAGMA user_version = %d" % i)


def user_id(conn, name):
    """ID of the named user, or None when no name is given."""
    if not name:
        return None
    row = conn.execute("SELECT id FROM users WHERE name = ?", (name,)).fetchone()
    if not row:
        raise SystemExit("Unknown user %r. Add one with: cli.py user add <name> [role]" % name)
    return row["id"]


def add_user(conn, name, role="admin"):
    with conn:
        conn.execute("INSERT INTO users (name, role, created_at) VALUES (?, ?, ?)", (name, role, now()))


# Places that aren't numbered totes (see the locations table).
PLACES = {
    "OT": "other: not in a tote (plush bin, house closet, storage, ...)",
    "UN": "unresolved: still needs a tote",
}


def location_id(conn, code):
    if not code:
        return None
    kind = "place" if code in PLACES else "tote"
    conn.execute("INSERT OR IGNORE INTO locations (code, kind, notes) VALUES (?, ?, ?)", (code, kind, PLACES.get(code)))
    return conn.execute("SELECT id FROM locations WHERE code = ?", (code,)).fetchone()["id"]


def batch_id(conn, name, tote=None, source=None):
    conn.execute(
        "INSERT OR IGNORE INTO batches (name, location_id, source, created_at) VALUES (?, ?, ?, ?)",
        (name, location_id(conn, tote), source, now()),
    )
    return conn.execute("SELECT id FROM batches WHERE name = ?", (name,)).fetchone()["id"]


def log_event(conn, entity, entity_id, action, actor, user=None, from_status=None, to_status=None, data=None, at=None):
    conn.execute(
        "INSERT INTO events (at, user_id, actor, entity, entity_id, action, from_status, to_status, data)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            at or now(),
            user,
            actor,
            entity,
            entity_id,
            action,
            from_status,
            to_status,
            json.dumps(data, sort_keys=True) if data else None,
        ),
    )


def set_status(conn, sku, status, actor, user=None, data=None, at=None):
    """Move an item to a new status and record the event. No-op if it's already there."""
    if status not in STATUSES:
        raise ValueError("unknown status %r" % status)
    row = conn.execute("SELECT status FROM items WHERE sku = ?", (sku,)).fetchone()
    if row["status"] == status:
        return
    conn.execute("UPDATE items SET status = ?, updated_at = ? WHERE sku = ?", (status, now(), sku))
    log_event(conn, "item", sku, "status", actor, user, row["status"], status, data, at)


def upsert_item(conn, sku, status, actor, user=None, at=None, **fields):
    """Create the item (logging its first status) or update the given fields."""
    exists = conn.execute("SELECT 1 FROM items WHERE sku = ?", (sku,)).fetchone()
    if not exists:
        conn.execute(
            "INSERT INTO items (sku, status, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (sku, status, at or now(), now()),
        )
        log_event(conn, "item", sku, "created", actor, user, None, status, at=at)
    if fields:
        cols = ", ".join("%s = ?" % k for k in fields)
        conn.execute("UPDATE items SET %s, updated_at = ? WHERE sku = ?" % cols, (*fields.values(), now(), sku))
    if exists:
        set_status(conn, sku, status, actor, user, at=at)


def record_listing(conn, sku, env, listing_id, offer_id, price, listed_at, actor, user=None):
    cur = conn.execute(
        "INSERT OR IGNORE INTO listings (sku, env, listing_id, offer_id, price, listed_by, listed_at)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (sku, env, listing_id, offer_id, price, user, listed_at),
    )
    if cur.rowcount:
        log_event(conn, "item", sku, "listed", actor, user, data={"env": env, "listing_id": listing_id}, at=listed_at)


def move(conn, sku, code, actor, user=None):
    """Put an item in a tote or place, recording where it came from."""
    row = conn.execute(
        "SELECT l.code FROM items i LEFT JOIN locations l ON l.id = i.location_id WHERE i.sku = ?", (sku,)
    ).fetchone()
    if row is None:
        raise SystemExit("Unknown SKU %s" % sku)
    conn.execute("UPDATE items SET location_id = ?, updated_at = ? WHERE sku = ?", (location_id(conn, code), now(), sku))
    log_event(conn, "item", sku, "moved", actor, user, data={"from": row["code"], "to": code})


def merge(conn, from_code, to_code, actor, user=None):
    """Move everything in one tote into another ("moved X2 into A3"). Returns the count."""
    skus = [r["sku"] for r in conn.execute(
        "SELECT i.sku FROM items i JOIN locations l ON l.id = i.location_id WHERE l.code = ?", (from_code,))]
    for sku in skus:
        move(conn, sku, to_code, actor, user)
    log_event(conn, "location", from_code, "merged", actor, user, data={"into": to_code, "items": len(skus)})
    return len(skus)


def own_listing_ids(conn):
    return {r["listing_id"] for r in conn.execute("SELECT listing_id FROM listings")}


def next_sku(conn, prefix="MV-"):
    """The next free MV-##### number."""
    numbers = [
        int(m.group(1))
        for (sku,) in conn.execute("SELECT sku FROM items WHERE sku LIKE ?", (prefix + "%",))
        for m in [re.match(re.escape(prefix) + r"(\d+)$", sku)]
        if m
    ]
    return "%s%05d" % (prefix, max(numbers, default=0) + 1)
