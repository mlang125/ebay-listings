"""One-time seller setup: business policies and an inventory (ship-from) location.

Each step looks for an existing item by name first, so re-running is safe.
"""

from . import api, config

# Marked as the account default; eBay rejects updates that leave this unset.
CATEGORY_TYPES = [{"name": "ALL_EXCLUDING_MOTORS_VEHICLES", "default": True}]


def _policy_name(kind):
    return "%s %s" % (config.POLICY_PREFIX, kind)


def _ensure_policy(kind, list_key, id_key, body):
    path = "/sell/account/v1/%s_policy" % kind
    existing = api.call("GET", path + "?marketplace_id=" + config.MARKETPLACE_ID)
    for policy in existing.get(list_key, []):
        if policy["name"] == body["name"]:
            try:
                api.call("PUT", "%s/%s" % (path, policy[id_key]), body)
            except api.EbayError as e:
                # eBay answers an update with no changes as an error.
                if "same as in the system" not in e.body:
                    raise
            return policy[id_key], False
    created = api.call("POST", path, body)
    return created[id_key], True


def _fulfillment_body():
    return {
        "name": _policy_name("shipping"),
        "marketplaceId": config.MARKETPLACE_ID,
        "categoryTypes": CATEGORY_TYPES,
        "handlingTime": {"value": config.HANDLING_DAYS, "unit": "DAY"},
        "shippingOptions": [
            {
                "optionType": "DOMESTIC",
                "costType": config.SHIPPING_COST_TYPE,
                "shippingServices": [
                    {
                        "sortOrder": 1,
                        "shippingCarrierCode": config.SHIPPING_CARRIER,
                        "shippingServiceCode": config.SHIPPING_SERVICE,
                        "freeShipping": False,
                    }
                ],
            }
        ],
    }


def _payment_body():
    return {
        "name": _policy_name("payment"),
        "marketplaceId": config.MARKETPLACE_ID,
        "categoryTypes": CATEGORY_TYPES,
        "immediatePay": True,
    }


def _return_body():
    return {
        "name": _policy_name("returns"),
        "marketplaceId": config.MARKETPLACE_ID,
        "categoryTypes": CATEGORY_TYPES,
        "returnsAccepted": True,
        "returnPeriod": {"value": config.RETURN_DAYS, "unit": "DAY"},
        "returnShippingCostPayer": config.RETURN_SHIPPING_PAID_BY,
        "refundMethod": "MONEY_BACK",
    }


def _ensure_location():
    path = "/sell/inventory/v1/location/" + config.LOCATION_KEY
    address = {
        "addressLine1": config.SHIP_FROM_ADDRESS_LINE1,
        "city": config.SHIP_FROM_CITY,
        "stateOrProvince": config.SHIP_FROM_STATE,
        "postalCode": config.SHIP_FROM_POSTAL_CODE,
        "country": "US",
    }
    existing = api.call("GET", "/sell/inventory/v1/location")
    for loc in existing.get("locations", []):
        if loc["merchantLocationKey"] == config.LOCATION_KEY:
            if loc["location"]["address"] != address:
                api.call("POST", path + "/update_location_details", {"location": {"address": address}})
            return False
    api.call(
        "POST",
        path,
        {
            "name": config.SHIP_FROM_NAME,
            "location": {"address": address},
            "locationTypes": ["WAREHOUSE"],
            "merchantLocationStatus": "ENABLED",
        },
    )
    return True


def run():
    if config.ENV == "production":
        # The real account already has its policies (config.PRODUCTION_POLICY_IDS); never create or change them here.
        created = _ensure_location()
        print("location  %r (%s)" % (config.LOCATION_KEY, "created" if created else "up to date"))
        return
    programs = api.call("GET", "/sell/account/v1/program/get_opted_in_programs")
    if not any(p["programType"] == "SELLING_POLICY_MANAGEMENT" for p in programs.get("programs", [])):
        api.call("POST", "/sell/account/v1/program/opt_in", {"programType": "SELLING_POLICY_MANAGEMENT"})
        print("Opted in to business policies.")

    results = [
        ("shipping", _ensure_policy("fulfillment", "fulfillmentPolicies", "fulfillmentPolicyId", _fulfillment_body())),
        ("payment", _ensure_policy("payment", "paymentPolicies", "paymentPolicyId", _payment_body())),
        ("returns", _ensure_policy("return", "returnPolicies", "returnPolicyId", _return_body())),
    ]
    for label, (policy_id, created) in results:
        print("%-9s policy %s (%s)" % (label, policy_id, "created" if created else "up to date"))

    created = _ensure_location()
    print("location  %r (%s)" % (config.LOCATION_KEY, "created" if created else "up to date"))
