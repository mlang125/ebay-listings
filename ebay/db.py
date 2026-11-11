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
    """
    ALTER TABLE users ADD COLUMN password_hash TEXT;  -- for the web app; NULL means no web login
    """,
    """
    -- A listing being prepared. `data` is the draft as JSON (title, description, aspects,
    -- photos, package, comps, needs_check, ...); see ebay/drafts.py.
    CREATE TABLE drafts (
        sku TEXT PRIMARY KEY REFERENCES items(sku),
        batch_id INTEGER NOT NULL REFERENCES batches(id),
        position INTEGER NOT NULL DEFAULT 0,
        data TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1,  -- bumped on every save, so two editors can't overwrite each other
        updated_at TEXT NOT NULL,
        updated_by INTEGER REFERENCES users(id)
    );
    CREATE INDEX drafts_batch ON drafts (batch_id, position);
    ALTER TABLE items ADD COLUMN missing_since TEXT;  -- set when someone marks it missing from its tote
    ALTER TABLE items ADD COLUMN relisted_as TEXT REFERENCES items(sku);  -- the SKU it was relisted under
    ALTER TABLE locations ADD COLUMN checked_at TEXT;  -- last time someone checked the tote's contents
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


ROLES = ("admin", "lister", "shipper")


def add_user(conn, name, role="admin"):
    with conn:
        conn.execute("INSERT INTO users (name, role, created_at) VALUES (?, ?, ?)", (name, role, now()))


def set_password(conn, name, password):
    from werkzeug.security import generate_password_hash

    with conn:
        conn.execute("UPDATE users SET password_hash = ? WHERE name = ?", (generate_password_hash(password), name))


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


# Statuses of items that have left the building. Their location stays as history, but
# they don't count as being in a tote (and moves leave them alone).
GONE = ("SENT_TO_POST_OFFICE", "DELIVERED")

TOTE_CODE_RE = re.compile(r"^[A-Z]\d+$")


def check_code(code):
    """Normalise a tote or place code, or stop with a message if it isn't one."""
    code = (code or "").strip().upper()
    if not (TOTE_CODE_RE.match(code) or code in PLACES):
        raise ValueError("%r isn't a tote code (a letter then a number, like A3) or one of %s" % (code, ", ".join(PLACES)))
    return code


def on_hand(conn, code):
    """SKUs physically in a tote or place."""
    return [r["sku"] for r in conn.execute(
        "SELECT i.sku FROM items i JOIN locations l ON l.id = i.location_id"
        " WHERE l.code = ? AND i.status NOT IN (?, ?) ORDER BY i.sku", (code, *GONE))]


def totes_in_use(conn):
    return {r["code"] for r in conn.execute(
        "SELECT DISTINCT l.code FROM locations l JOIN items i ON i.location_id = l.id WHERE i.status NOT IN (?, ?)", GONE)}


def suggest_tote(conn, freeing=()):
    """The first free code from A1 to Z9, counting the totes in `freeing` as free (they're being emptied)."""
    used = totes_in_use(conn) - set(freeing)
    for letter in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
        for n in range(1, 10):
            if letter + str(n) not in used:
                return letter + str(n)
    return None


def merge(conn, from_code, to_code, actor, user=None):
    """Move everything on hand in one tote into another ("moved X2 into A3"). Returns the count."""
    return consolidate(conn, [from_code], to_code, actor, user)


def consolidate(conn, codes, into, actor, user=None):
    """Combine totes: move what's on hand in each of `codes` into `into`. Returns the count."""
    moved = 0
    for code in codes:
        if code == into:
            continue
        skus = on_hand(conn, code)
        for sku in skus:
            move(conn, sku, into, actor, user)
        moved += len(skus)
        log_event(conn, "location", code, "merged", actor, user, data={"into": into, "items": len(skus)})
    return moved


def split(conn, from_code, plan, actor, user=None):
    """Send items from one tote to several: plan is {sku: code}. Stops before moving anything if a SKU isn't in the tote."""
    here = set(on_hand(conn, from_code))
    strays = sorted(set(plan) - here)
    if strays:
        raise ValueError("Not in %s: %s" % (from_code, ", ".join(strays)))
    for sku, code in sorted(plan.items()):
        move(conn, sku, code, actor, user)
    log_event(conn, "location", from_code, "split", actor, user,
              data={"to": sorted(set(plan.values())), "items": len(plan)})


# Fields people can correct by hand in our records (never sent to eBay).
EDITABLE = ("title", "price", "notes", "cost", "cost_note")
# Statuses where the title and price are ours to change: not live on eBay, not sold.
OFF_EBAY = ("CANCELLED", "RETURNED", "ENDED", "SKIPPED")


def edit_item(conn, sku, changes, actor, user=None):
    """Update item fields in our records and log old and new values. Returns the changed field names."""
    row = conn.execute("SELECT * FROM items WHERE sku = ?", (sku,)).fetchone()
    if row is None:
        raise ValueError("Unknown SKU %s" % sku)
    changed = {k: v for k, v in changes.items() if k in EDITABLE and row[k] != v}
    if not changed:
        return []
    if ("title" in changed or "price" in changed) and row["status"] not in OFF_EBAY:
        raise ValueError("%s is %s; its title and price follow eBay. Suggest a price instead." % (sku, row["status"]))
    conn.execute("UPDATE items SET %s, updated_at = ? WHERE sku = ?" % ", ".join("%s = ?" % k for k in changed),
                 (*changed.values(), now(), sku))
    log_event(conn, "item", sku, "edited", actor, user,
              data={"fields": sorted(changed), "from": {k: row[k] for k in changed}, "to": changed})
    return sorted(changed)


def suggest_price(conn, sku, price, actor, user=None, note=None):
    """Record a suggested eBay price for a listed item. Nothing is sent to eBay; someone applies it there."""
    row = conn.execute("SELECT price FROM items WHERE sku = ?", (sku,)).fetchone()
    if row is None:
        raise ValueError("Unknown SKU %s" % sku)
    if price is None or price <= 0:
        raise ValueError("A suggested price must be more than $0")
    log_event(conn, "item", sku, "price_suggested", actor, user,
              data={"price": round(price, 2), "was": row["price"], "note": note})


def price_drop(price, percent):
    """`price` lowered by `percent`, ending in .99 (never above the lowered price, never below $0.99)."""
    lowered = price * (1 - percent / 100.0)
    ending = int(lowered) + 0.99
    if ending > lowered:
        ending -= 1
    return round(max(ending, 0.99), 2)


# The newest price suggestion for an item, if it came after the item's last price change or listing.
LATEST_SUGGESTION = """(SELECT json_extract(e.data, '$.price') FROM events e
    WHERE e.entity = 'item' AND e.entity_id = i.sku AND e.action = 'price_suggested'
      AND e.id > COALESCE((SELECT MAX(x.id) FROM events x WHERE x.entity = 'item' AND x.entity_id = i.sku
                           AND x.action IN ('listed', 'price_applied')), 0)
    ORDER BY e.id DESC LIMIT 1)"""


def set_missing(conn, sku, missing, actor, user=None, note=None):
    """Flag an item as missing from its tote (or found again). Its tote stays as it was."""
    row = conn.execute("SELECT missing_since FROM items WHERE sku = ?", (sku,)).fetchone()
    if row is None:
        raise ValueError("Unknown SKU %s" % sku)
    if bool(row["missing_since"]) == missing:
        return
    conn.execute("UPDATE items SET missing_since = ?, updated_at = ? WHERE sku = ?", (now() if missing else None, now(), sku))
    log_event(conn, "item", sku, "missing" if missing else "found", actor, user, data={"note": note} if note else None)


def mark_checked(conn, code, actor, user=None):
    """Record that someone went through a tote."""
    conn.execute("UPDATE locations SET checked_at = ? WHERE code = ?", (now(), code))
    log_event(conn, "location", code, "checked", actor, user, data={
        "on_hand": len(on_hand(conn, code)),
        "missing": conn.execute(
            "SELECT COUNT(*) FROM items i JOIN locations l ON l.id = i.location_id"
            " WHERE l.code = ? AND i.missing_since IS NOT NULL AND i.status NOT IN (?, ?)", (code, *GONE)).fetchone()[0],
    })


# Items that came back to us unsold and may need listing again.
RELISTABLE = ("CANCELLED", "RETURNED", "ENDED")


def link_relist(conn, old, new, actor, user=None):
    """Record that `old` (cancelled, returned or ended) was relisted as `new`."""
    for sku in (old, new):
        if not conn.execute("SELECT 1 FROM items WHERE sku = ?", (sku,)).fetchone():
            raise ValueError("Unknown SKU %s" % sku)
    if old == new:
        raise ValueError("An item can't be relisted as itself")
    conn.execute("UPDATE items SET relisted_as = ?, updated_at = ? WHERE sku = ?", (new, now(), old))
    log_event(conn, "item", old, "relisted_as", actor, user, data={"sku": new})
    log_event(conn, "item", new, "relist_of", actor, user, data={"sku": old})


def suggest_relists(conn):
    """(old, new) pairs: a relistable item and the one other item with the same title on a newer listing.

    eBay listing IDs only go up, so "newer" means a higher listing ID. When several old
    items point at the same new one (e.g. three cancellations, then one sale), it's
    left for a person to decide.
    """
    def latest_listing(sku):
        ids = [int(r[0]) for r in conn.execute("SELECT listing_id FROM listings WHERE sku = ?", (sku,)) if r[0].isdigit()]
        return max(ids, default=None)

    pairs = []
    olds = conn.execute(
        "SELECT sku, title FROM items WHERE status IN (?, ?, ?) AND relisted_as IS NULL AND title IS NOT NULL", RELISTABLE
    ).fetchall()
    for old in olds:
        old_listing = latest_listing(old["sku"])
        matches = [r["sku"] for r in conn.execute(
            "SELECT i.sku FROM items i WHERE lower(trim(i.title)) = lower(trim(?)) AND i.sku <> ?"
            " AND i.status NOT IN (?, ?, ?, 'PENDING', 'SKIPPED')"
            " AND NOT EXISTS (SELECT 1 FROM items o WHERE o.relisted_as = i.sku)",
            (old["title"], old["sku"], *RELISTABLE),
        ) if old_listing and (latest_listing(r["sku"]) or 0) > old_listing]
        if len(matches) == 1:
            pairs.append((old["sku"], matches[0]))
    targets = [new for _, new in pairs]
    return [(old, new) for old, new in pairs if targets.count(new) == 1]


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
