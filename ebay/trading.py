"""Read-only calls to eBay's Trading API (XML), for what the REST APIs can't see.

Listings made in Seller Hub don't show up in the Inventory API, so the only way to ask which of them
are still live is the Trading API's GetMyeBaySelling (the "Active" tab). Trading API requests are always
POSTs, even when they only read, so they skip api.call's read-only guard; READ_CALLS is the list of
calls allowed here, and anything else is refused.
"""

import urllib.error
import urllib.request
import xml.etree.ElementTree as ET

from . import api, auth, config

NS = "{urn:ebay:apis:eBLBaseComponents}"
COMPATIBILITY_LEVEL = "1193"
READ_CALLS = {"GetMyeBaySelling"}  # only calls that change nothing on eBay
PAGE_SIZE = 200  # eBay's maximum for GetMyeBaySelling


def _call(name, body):
    if name not in READ_CALLS:
        raise api.ReadOnly("Trading API call %s" % name)  # never sent: only reads are allowed here
    xml = ('<?xml version="1.0" encoding="utf-8"?><%sRequest xmlns="urn:ebay:apis:eBLBaseComponents">%s</%sRequest>'
           % (name, body, name)).encode()
    req = urllib.request.Request(config.app()["api_base"] + "/ws/api.dll", data=xml, method="POST", headers={
        "X-EBAY-API-CALL-NAME": name,
        "X-EBAY-API-SITEID": "0",  # eBay US
        "X-EBAY-API-COMPATIBILITY-LEVEL": COMPATIBILITY_LEVEL,
        "X-EBAY-API-IAF-TOKEN": auth.access_token(),
        "Content-Type": "text/xml",
    })
    try:
        with urllib.request.urlopen(req) as resp:
            root = ET.fromstring(resp.read())
    except urllib.error.HTTPError as e:
        raise api.EbayError(e.code, e.read().decode())
    if root.findtext(NS + "Ack") not in ("Success", "Warning"):
        errors = "; ".join(e.findtext(NS + "LongMessage") or e.findtext(NS + "ShortMessage") or "?"
                           for e in root.iter(NS + "Errors"))
        raise api.EbayError("Trading " + name, errors or ET.tostring(root, encoding="unicode")[:500])
    return root


def parse_active(root):
    """(listings, total pages) from one GetMyeBaySelling reply. Each listing: id, sku, quantity left, title."""
    active = root.find(NS + "ActiveList")
    if active is None:
        return [], 0
    out = []
    for item in active.iter(NS + "Item"):
        quantity = item.findtext(NS + "QuantityAvailable")
        if quantity is None:  # older replies only give Quantity (listed) and SellingStatus/QuantitySold
            listed = int(item.findtext(NS + "Quantity") or 0)
            sold = int(item.findtext(NS + "SellingStatus/" + NS + "QuantitySold") or 0)
            quantity = listed - sold
        out.append({"listing_id": item.findtext(NS + "ItemID"), "sku": item.findtext(NS + "SKU"),
                    "quantity": int(quantity), "title": item.findtext(NS + "Title")})
    pages = int(active.findtext(NS + "PaginationResult/" + NS + "TotalNumberOfPages") or 1)
    return out, pages


def active_listings():
    """Every listing that's live on eBay right now, all pages (about one request per 200 listings)."""
    out, page, pages = [], 1, 1
    while page <= pages:
        root = _call("GetMyeBaySelling", "<ActiveList><Include>true</Include><Pagination>"
                     "<EntriesPerPage>%d</EntriesPerPage><PageNumber>%d</PageNumber></Pagination></ActiveList>"
                     "<HideVariations>true</HideVariations>" % (PAGE_SIZE, page))
        found, pages = parse_active(root)
        out += found
        page += 1
    return out


def compare(conn, active):
    """What differs between our LISTED items and eBay's live listings. Changes nothing.

    not_live: items we have as LISTED whose listing isn't live on eBay (ended, or sold and not synced yet).
    unknown:  live eBay listings we have no record of.
    quantity: live listings whose quantity left differs from ours.
    """
    live = {a["listing_id"]: a for a in active}
    rows = conn.execute(
        "SELECT i.sku, i.title, i.quantity, l.code AS tote,"
        " (SELECT listing_id FROM listings WHERE sku = i.sku ORDER BY listed_at DESC LIMIT 1) AS listing_id"
        " FROM items i LEFT JOIN locations l ON l.id = i.location_id WHERE i.status = 'LISTED' ORDER BY i.sku"
    ).fetchall()
    known = {r[0] for r in conn.execute("SELECT listing_id FROM listings")}
    return {
        "not_live": [r for r in rows if r["listing_id"] not in live],
        "unknown": [a for a in active if a["listing_id"] not in known],
        "quantity": [(r, live[r["listing_id"]]["quantity"]) for r in rows
                     if r["listing_id"] in live and live[r["listing_id"]]["quantity"] != r["quantity"]],
        "live": len(live),
        "listed": len(rows),
    }
