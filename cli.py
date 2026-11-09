"""ebay-listings command line.

  python3 cli.py login   one-time browser sign-in; stores the refresh token in the Keychain
  python3 cli.py check   confirms the token works and shows the account's selling status
  python3 cli.py setup   creates business policies and the ship-from location (safe to re-run)
  python3 cli.py publish <batch> [SKU ...]   publishes reviewed drafts (all, or only the SKUs given)
  python3 cli.py comps <batch> [SKU ...]     price estimate from active listings + sold-search links for unpublished drafts
  python3 cli.py sold <batch> <SKU> <file>   saves sold comps (JSON from ebay/sold_extract.js) into the draft
"""

import json
import sys
import webbrowser

import os

from ebay import api, auth, comps, config, listings, setup


def cmd_login():
    url = auth.consent_url()
    print("Opening eBay sign-in (%s). Sign in as your %s user and click Agree." % (config.ENV, config.ENV))
    print("If the browser doesn't open, visit:\n\n%s\n" % url)
    webbrowser.open(url)
    pasted = input("Paste the full URL of the page you land on afterwards:\n> ")
    days = auth.login(pasted)
    print("Logged in. Refresh token saved to the Keychain (valid about %d days)." % days)


def cmd_check():
    privileges = api.call("GET", "/sell/account/v1/privilege")
    print(json.dumps(privileges, indent=2))


def cmd_setup():
    setup.run()


def _batch_dir(batch):
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "batches", batch)


def cmd_publish(batch, *skus):
    listings.publish(_batch_dir(batch), set(skus) or None)


def cmd_comps(batch, *skus):
    comps.refresh_active(_batch_dir(batch), set(skus) or None)


def cmd_sold(batch, sku, path):
    with open(path) as f:
        sales = json.load(f)
    summary = comps.record_sold(_batch_dir(batch), sku, sales)
    print("%s sold comps: %s" % (sku, comps._format(summary)))


COMMANDS = {
    "login": cmd_login,
    "check": cmd_check,
    "setup": cmd_setup,
    "publish": cmd_publish,
    "comps": cmd_comps,
    "sold": cmd_sold,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
        print(__doc__)
        sys.exit(1)
    COMMANDS[sys.argv[1]](*sys.argv[2:])
