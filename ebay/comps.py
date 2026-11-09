"""Price comparisons ("comps") for drafts.

Two sources:
  - active listings, fetched here through the Browse API (asking prices only),
    turned into a price estimate by estimate();
  - sold listings, which eBay only shows on its website (the sold-items API is
    restricted). Claude reads those pages in Chrome and saves them with record_sold().
"""

import datetime
import json
import os
import re
import statistics
import urllib.parse

from . import api, db

# Inventory API condition -> whether comps should look at new or used items.
NEW_CONDITIONS = {"NEW", "LIKE_NEW", "NEW_OTHER", "NEW_WITH_DEFECTS"}
NEW_CONDITION_IDS = "1000|1500|1750"
USED_CONDITION_IDS = "2750|2990|3000|3010|4000|5000|6000"


def _condition_ids(item):
    return NEW_CONDITION_IDS if item["condition"] in NEW_CONDITIONS else USED_CONDITION_IDS


def _query(item):
    return item.get("comp_query") or item["title"]


def _summary(prices):
    if not prices:
        return {"count": 0}
    prices = sorted(prices)
    return {
        "count": len(prices),
        "median": round(statistics.median(prices), 2),
        "low": prices[0],
        "high": prices[-1],
    }


def _percentile(sorted_prices, pct):
    """Linear-interpolated percentile of an already sorted list."""
    pos = (len(sorted_prices) - 1) * pct / 100.0
    lo = int(pos)
    hi = min(lo + 1, len(sorted_prices) - 1)
    return sorted_prices[lo] + (sorted_prices[hi] - sorted_prices[lo]) * (pos - lo)


# Asking prices run above what things actually sell for, and the listings that sell
# are the cheaper ones, so the estimate sits below the active median.
ESTIMATE_PERCENTILE = 30


def _has_word(title, word):
    return re.search(r"\b%s\b" % re.escape(word.lower()), title.lower()) is not None


def estimate(item, listings):
    """Price estimate from active listings, without sold data.

    Browse search matches loosely, so first keeps only listings whose title has
    every word of comp_query (words of 3+ letters), none of the draft's
    comp_exclude words (e.g. "lot", "lid"), and isn't one of our own listings.
    Then drops outliers (under 1/3 or over 3x the median: usually accessories,
    lots or wishful pricing). The estimate is the 30th percentile of what's left;
    the range is the 20th-50th. Confidence only says how many comps there are and
    how much they agree, not whether the query found the right kind of item.
    """
    # Only for a hand-written comp_query; the full title is too specific to require.
    required = [w for w in re.findall(r"[\w'-]+", item.get("comp_query", "")) if len(w) >= 3]
    exclude = item.get("comp_exclude", [])
    own = db.own_listing_ids(db.connect())
    prices = sorted(
        l["price"]
        for l in listings
        if l.get("listing_id") not in own
        and all(_has_word(l["title"], w) for w in required)
        and not any(_has_word(l["title"], w) for w in exclude)
    )
    if not prices:
        return {"count": 0, "confidence": "none"}
    median = statistics.median(prices)
    kept = [p for p in prices if median / 3 <= p <= median * 3]
    spread = (_percentile(kept, 75) - _percentile(kept, 25)) / statistics.median(kept)
    if len(kept) >= 20 and spread <= 0.6:
        confidence = "high"
    elif len(kept) >= 8 and spread <= 1.2:
        confidence = "medium"
    else:
        confidence = "low"
    return {
        "price": round(_percentile(kept, ESTIMATE_PERCENTILE), 2),
        "range": [round(_percentile(kept, 20), 2), round(_percentile(kept, 50), 2)],
        "count": len(kept),
        "dropped": len(listings) - len(kept),
        "confidence": confidence,
    }


def sold_url(item):
    """eBay sold-listings search for this item, filtered to its category and new/used."""
    params = {
        "_nkw": _query(item),
        "LH_Sold": "1",
        "LH_Complete": "1",
        "LH_ItemCondition": _condition_ids(item),
        "_sop": "13",  # most recently ended first
        "_ipg": "120",
    }
    if item.get("category_tree") != "100":
        params["_sacat"] = item["category_id"]
    return "https://www.ebay.com/sch/i.html?" + urllib.parse.urlencode(params)


def active_comps(item):
    """Current fixed-price listings like this one, with total price (item + shipping)."""
    filters = "conditionIds:{%s},buyingOptions:{FIXED_PRICE}" % _condition_ids(item)
    path = "/buy/browse/v1/item_summary/search?limit=100&q=%s&filter=%s" % (
        urllib.parse.quote(_query(item)),
        urllib.parse.quote(filters),
    )
    if item.get("category_tree") != "100":
        path += "&category_ids=" + item["category_id"]
    results = api.call("GET", path).get("itemSummaries", [])
    listings = []
    for r in results:
        price = float(r["price"]["value"])
        shipping = r.get("shippingOptions", [{}])[0].get("shippingCost", {}).get("value")
        listings.append(
            {
                "listing_id": r.get("legacyItemId"),
                "price": price,
                "shipping": float(shipping) if shipping is not None else None,
                "condition": r.get("condition"),
                "title": r["title"],
                "url": r.get("itemWebUrl"),
            }
        )
    return listings


def _load(batch_dir):
    with open(os.path.join(batch_dir, "drafts.json")) as f:
        return json.load(f)


def _save(batch_dir, drafts):
    with open(os.path.join(batch_dir, "drafts.json"), "w") as f:
        json.dump(drafts, f, indent=2)


def _today():
    return datetime.date.today().isoformat()


def refresh_active(batch_dir, skus=None):
    """Fetch active comps for each unpublished draft and store a summary in drafts.json."""
    drafts = _load(batch_dir)
    for item in drafts["items"]:
        if item.get("published") or item.get("skip") or (skus and item["sku"] not in skus):
            continue
        listings = active_comps(item)
        comps = item.setdefault("comps", {})
        comps["query"] = _query(item)
        comps["sold_url"] = sold_url(item)
        comps["active"] = dict(_summary([l["price"] for l in listings]), checked=_today())
        comps["estimate"] = dict(estimate(item, listings), checked=_today())
        print(
            "%s  draft $%.2f  | estimate: %s  | active: %s\n          sold search: %s"
            % (
                item["sku"],
                item["price"],
                _format_estimate(comps["estimate"]),
                _format(comps["active"]),
                comps["sold_url"],
            )
        )
    _save(batch_dir, drafts)


def record_sold(batch_dir, sku, sales):
    """Save sold comps read from eBay's website.

    sales: list of {"price": float, "shipping": float|None, "date": "YYYY-MM-DD",
                    "condition": str, "title": str}
    """
    drafts = _load(batch_dir)
    item = next(i for i in drafts["items"] if i["sku"] == sku)
    comps = item.setdefault("comps", {})
    comps["sold"] = dict(_summary([s["price"] for s in sales]), checked=_today(), sales=sales)
    _save(batch_dir, drafts)
    return comps["sold"]


def _format_estimate(est):
    if not est.get("count"):
        return "no comps"
    return "$%.2f ($%.2f-$%.2f, %d listings, %s confidence)" % (
        est["price"], est["range"][0], est["range"][1], est["count"], est["confidence"]
    )


def _format(summary):
    if not summary.get("count"):
        return "none found"
    return "%d, median $%.2f ($%.2f-$%.2f)" % (summary["count"], summary["median"], summary["low"], summary["high"])
