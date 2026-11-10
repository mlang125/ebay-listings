"""Publish reviewed drafts: photos -> inventory item -> offer -> live listing."""

import os

from . import api, config, db, drafts, media


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


def _record(conn, drafts_, item, listing_id, offer_id, listed_at, user, actor):
    with conn:
        item["published"] = dict(item.get("published", {}), **{config.ENV: {"listing_id": listing_id, "offer_id": offer_id, "listed_at": listed_at}})
        drafts.save(conn, item, actor, user)
        db.upsert_item(
            conn,
            item["sku"],
            "LISTED",
            actor,
            user,
            title=item["title"],
            condition=item["condition"],
            price=item["price"],
            batch_id=db.batch_id(conn, drafts_["batch"], drafts_["tote"], drafts_.get("source_zip")),
            location_id=db.location_id(conn, drafts_["tote"]),
        )
        db.record_listing(conn, item["sku"], config.ENV, listing_id, offer_id, item["price"], listed_at, actor, user)


def publish(conn, batch, skus=None, user=None, actor="cli", log=print):
    """Publish a batch's drafts (all, or only `skus`). `user` is who's publishing; messages go to `log`."""
    drafts_ = drafts.load(conn, batch)
    if drafts_ is None:
        raise SystemExit("No drafts in batch %s. Load them with: cli.py import-drafts %s" % (batch, batch))
    policies = policy_ids()
    photos = drafts.photos_dir(batch)

    for item in drafts_["items"]:
        if skus and item["sku"] not in skus:
            continue
        if item.get("skip"):
            log("%s skipped: %s" % (item["sku"], item["skip"]))
            continue
        published = item.get("published", {})
        if published.get(config.ENV):
            log("%s already published in %s (listing %s); skipping" % (item["sku"], config.ENV, published[config.ENV]["listing_id"]))
            continue

        log("%s: uploading %d photos" % (item["sku"], len(item["photos"])))
        image_urls = [media.upload_image(os.path.join(photos, p)) for p in item["photos"]]

        api.call("PUT", "/sell/inventory/v1/inventory_item/" + item["sku"], _inventory_item(item, image_urls))
        offer_id = _upsert_offer(item, policies)
        listing_id = api.call("POST", "/sell/inventory/v1/offer/%s/publish" % offer_id)["listingId"]
        log("%s: published as listing %s" % (item["sku"], listing_id))
        _record(conn, drafts_, item, listing_id, offer_id, db.now(), user, actor)
