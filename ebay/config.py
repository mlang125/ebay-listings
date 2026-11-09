"""Settings for talking to eBay.

Account-specific values (app keys, policy IDs, ship-from address) come from .env,
which git ignores; .env.example lists them. Secrets (the Cert ID and the OAuth
refresh token) live in the macOS Keychain; see keychain.py.
"""

import os

ENV_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")


def _load_env_file(path):
    values = {}
    if not os.path.exists(path):
        raise SystemExit("Missing %s. Copy .env.example to .env and fill it in." % path)
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


_ENV_VALUES = _load_env_file(ENV_FILE)


def _env(key):
    if not _ENV_VALUES.get(key):
        raise SystemExit("%s is not set in %s (see .env.example)." % (key, ENV_FILE))
    return _ENV_VALUES[key]

ROOT = os.path.dirname(ENV_FILE)
DB_PATH = os.path.join(ROOT, _ENV_VALUES.get("DB_PATH") or "data/ebay.db")
# Who `cli.py` records as doing things (a name from `cli.py user list`); optional.
CLI_USER = _ENV_VALUES.get("CLI_USER") or None


ENV = "production"  # or "sandbox" for testing

APPS = {
    "sandbox": {
        "client_id": _env("SANDBOX_CLIENT_ID"),
        "ru_name": _env("SANDBOX_RU_NAME"),
        "auth_url": "https://auth.sandbox.ebay.com/oauth2/authorize",
        "api_base": "https://api.sandbox.ebay.com",
    },
    "production": {
        "client_id": _env("PRODUCTION_CLIENT_ID"),
        "ru_name": _env("PRODUCTION_RU_NAME"),
        "auth_url": "https://auth.ebay.com/oauth2/authorize",
        "api_base": "https://api.ebay.com",
    },
}

SCOPES = [
    "https://api.ebay.com/oauth/api_scope",
    "https://api.ebay.com/oauth/api_scope/sell.inventory",
    "https://api.ebay.com/oauth/api_scope/sell.account",
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment",
]

MARKETPLACE_ID = "EBAY_US"


def app():
    return APPS[ENV]

# Existing policies on the real account. Production listings use these IDs
# directly; in production `cli.py setup` only manages the location.
PRODUCTION_POLICY_IDS = {
    "fulfillmentPolicyId": _env("FULFILLMENT_POLICY_ID"),
    "paymentPolicyId": _env("PAYMENT_POLICY_ID"),
    "returnPolicyId": _env("RETURN_POLICY_ID"),
}

# Seller defaults used by `cli.py setup`. Re-run setup after changing them.
POLICY_PREFIX = "ebay-listings"
HANDLING_DAYS = 2
RETURN_DAYS = 30
RETURN_SHIPPING_PAID_BY = "BUYER"  # or "SELLER"
SHIPPING_CARRIER = "USPS"
SHIPPING_SERVICE = "USPSParcel"  # eBay's code for USPS Ground Advantage
# CALCULATED: buyer pays real postage, from the estimated weight/size on each item.
SHIPPING_COST_TYPE = "CALCULATED"
LOCATION_KEY = "home"
SHIP_FROM_NAME = _env("SHIP_FROM_NAME")
SHIP_FROM_ADDRESS_LINE1 = _env("SHIP_FROM_ADDRESS_LINE1")
SHIP_FROM_CITY = _env("SHIP_FROM_CITY")
SHIP_FROM_STATE = _env("SHIP_FROM_STATE")
SHIP_FROM_POSTAL_CODE = _env("SHIP_FROM_POSTAL_CODE")
