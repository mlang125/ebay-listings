"""Publish reviewed drafts: photos -> inventory item -> offer -> live listing."""

import json
import os

from . import api, config, db, media


def policy_ids():
    if config.ENV == "production":
        return dict(config.PRODUCTION_POLICY_IDS)
    ids = {}
    for kind, list_key, id_key in [
        ("fulfillment", "fulfillmentPolicies", "fulfillmentPolicyId"),
        ("payment", "paymentPolicies", "paymentPolicyId"),
        ("return", "returnPolicies", "returnPolicyId"),
    ]:
        policies = api.call("GET", "/sell/account/v1/%s_policy?marketplace_id=%s" % (kind, config.MARKETPLACE_ID))
        match = [p for p in policies.get(list_key, []) if p["name"].startswith(config.POLICY_PREFIX)]
        if not match:
            raise SystemExit("No %s policy found. Run: python3 cli.py setup" % kind)
        ids[id_key] = match[0][id_key]
    return ids


def _inventory_item(item, image_urls):
    pkg = item["package"]
    return {
        "availability": {"shipToLocationAvailability": {"quantity": 1}},
        "condition": item["condition"],
        "conditionDescription": item.get("condition_description", ""),
        "product": {
            "title": item["title"],
            "description": item["description"],
            "aspects": item["aspects"],
            "imageUrls": image_urls,
        },
        "packageWeightAndSize": {
            "weight": {"value": pkg["weight_oz"], "unit": "OUNCE"},
            "dimensions": {
                "length": pkg["length_in"],
                "width": pkg["width_in"],
                "height": pkg["height_in"],
                "unit": "INCH",
            },
        },
    }


def _marketplace(item):
    # Category tree 100 is eBay Motors (parts & accessories), a separate marketplace.
    return "EBAY_MOTORS" if item.get("category_tree") == "100" else config.MARKETPLACE_ID


def _offer(item, policies):
    return {
        "sku": item["sku"],
        "marketplaceId": _marketplace(item),
        "format": "FIXED_PRICE",
        "availableQuantity": 1,
        "categoryId": item["category_id"],
        "listingDescription": item["description"].replace("\n", "<br>"),
        "listingPolicies": policies,
        "pricingSummary": {"price": {"value": "%.2f" % item["price"], "currency": "USD"}},
        "merchantLocationKey": config.LOCATION_KEY,
    }


def _upsert_offer(item, policies):
    body = _offer(item, policies)
    existing = []
    try:
        existing = api.call("GET", "/sell/inventory/v1/offer?sku=" + item["sku"]).get("offers", [])
    except api.EbayError as e:
        if e.status != 404:
            raise
    for offer in existing:
        # Drop unpublished offers left on the wrong marketplace (e.g. a Motors item first tried on EBAY_US).
        if offer["marketplaceId"] != body["marketplaceId"] and offer["status"] == "UNPUBLISHED":
            api.call("DELETE", "/sell/inventory/v1/offer/" + offer["offerId"])
    existing = [o for o in existing if o["marketplaceId"] == body["marketplaceId"]]
    if existing:
        offer_id = existing[0]["offerId"]
        api.call("PUT", "/sell/inventory/v1/offer/" + offer_id, body)
        return offer_id
    return api.call("POST", "/sell/inventory/v1/offer", body)["offerId"]


def _record(conn, drafts, item, listing_id, offer_id, listed_at, user):
    with conn:
        db.upsert_item(
            conn,
            item["sku"],
            "LISTED",
            "cli",
            user,
            title=item["title"],
            condition=item["condition"],
            price=item["price"],
            batch_id=db.batch_id(conn, drafts["batch"], drafts["tote"], drafts.get("source_zip")),
            location_id=db.location_id(conn, drafts["tote"]),
        )
        db.record_listing(conn, item["sku"], config.ENV, listing_id, offer_id, item["price"], listed_at, "cli", user)


def publish(batch_dir, skus=None):
    drafts_path = os.path.join(batch_dir, "drafts.json")
    with open(drafts_path) as f:
        drafts = json.load(f)
    policies = policy_ids()
    conn = db.connect()
    user = db.user_id(conn, config.CLI_USER)

    for item in drafts["items"]:
        if skus and item["sku"] not in skus:
            continue
        if item.get("skip"):
            print("%s skipped: %s" % (item["sku"], item["skip"]))
            continue
        published = item.get("published", {})
        if published.get(config.ENV):
            print("%s already published in %s (listing %s); skipping" % (item["sku"], config.ENV, published[config.ENV]["listing_id"]))
            continue

        print("%s: uploading %d photos" % (item["sku"], len(item["photos"])))
        image_urls = [media.upload_image(os.path.join(batch_dir, "photos", p)) for p in item["photos"]]

        api.call("PUT", "/sell/inventory/v1/inventory_item/" + item["sku"], _inventory_item(item, image_urls))
        offer_id = _upsert_offer(item, policies)
        listing_id = api.call("POST", "/sell/inventory/v1/offer/%s/publish" % offer_id)["listingId"]
        print("%s: published as listing %s" % (item["sku"], listing_id))

        now = db.now()
        published[config.ENV] = {"listing_id": listing_id, "offer_id": offer_id, "listed_at": now}
        item["published"] = published
        with open(drafts_path, "w") as f:
            json.dump(drafts, f, indent=2)
        _record(conn, drafts, item, listing_id, offer_id, now, user)
