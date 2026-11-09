# ebay-listings

Turns a zip of item photos into live eBay listings through eBay's own Sell APIs,
and keeps track of which tote each item is stored in.

Python 3.9, standard library only. Nothing to install.

## The workflow

1. **Photograph a tote.** Before each item, take one *divider* photo of the whiteboard
   screen. Leave it blank, or write condition notes on it ("tested", "not working",
   "no lid"). Divider photos are never listed. Item or tote IDs written on the
   whiteboard are ignored.
2. **Zip the photos**, one zip per tote, and put the zip in `inbox/` (for example `inbox/batch1.zip`).
3. **Have Claude draft the batch.** Give Claude the zip and the tote ID (`<letter><number>`, e.g. `T1`).
   Claude splits the photos at each divider and assigns SKUs (`MV-#####`, the next free
   number in `inventory.csv`). It writes `batches/<batch>/drafts.json` and copies the
   listing photos into `batches/<batch>/photos/`.
4. **Price check:** `python3 cli.py comps <batch>` (see [Pricing](#pricing)).
5. **Review** `drafts.json`: titles, condition, price, and anything listed under `needs_check`.
6. **Publish:** `python3 cli.py publish <batch> [SKU ...]`. Each published item is
   added to `inventory.csv` with its tote.

## Commands

| Command | What it does |
|---|---|
| `python3 cli.py login` | One-time browser sign-in. Stores the refresh token in the macOS Keychain (lasts about 18 months). |
| `python3 cli.py check` | Confirms the token works and shows the account's selling status. |
| `python3 cli.py setup` | Creates the ship-from location. In sandbox it also creates the business policies. Safe to re-run. |
| `python3 cli.py comps <batch> [SKU ...]` | Price estimate from active listings plus a sold-search link for each unpublished draft. |
| `python3 cli.py sold <batch> <SKU> <file.json>` | Saves sold prices read from eBay's website into the draft. |
| `python3 cli.py publish <batch> [SKU ...]` | Publishes drafts: all of them, or only the SKUs given. |

`publish` skips drafts that have a `skip` field or are already published in the
current environment, so re-running it is safe.

## drafts.json

One file per batch: `{"batch", "tote", "source_zip", "items": [...]}`. Each item has:

| Field | Notes |
|---|---|
| `sku` | `MV-#####`. Becomes the eBay Custom label. |
| `photos` | Filenames in `photos/`, in listing order. |
| `title`, `description` | Title is 80 characters max. |
| `category_id` | eBay leaf category. Add `"category_tree": "100"` for eBay Motors parts (these list on the `EBAY_MOTORS` marketplace). |
| `condition`, `condition_description` | Inventory API enum, e.g. `USED_EXCELLENT` (Pre-owned), `NEW_OTHER` (Open box). |
| `aspects` | Item specifics, `{"Brand": ["Nike"], ...}`. |
| `price` | USD. |
| `package` | `weight_oz`, `length_in`, `width_in`, `height_in`. These are rough guesses from the photos; shipping is calculated, so close is good enough. |
| `needs_check` | Questions for you to settle before publishing. |
| `comp_query`, `comp_exclude` | Optional. Search words for pricing (see below). |
| `skip` | Optional reason not to publish (e.g. already listed elsewhere). |
| `comps`, `published` | Written by the tool. |

## Pricing

eBay doesn't give sold-price data to an app like this. Its Marketplace Insights
API is restricted, and our request for access was turned down. That leaves two sources.

**Estimate from active listings (automatic).** `cli.py comps` searches current
fixed-price listings in the same category and condition group (new or used), then:

1. keeps only titles that contain every word of `comp_query` that's 3+ letters long
   (eBay's search is loose) and none of the words in `comp_exclude`;
2. drops your own listings (found through `inventory.csv`);
3. drops outliers under ⅓ or over 3× the median;
4. reports the **30th percentile** as the estimate, with a 20th–50th percentile range.
   Asking prices run higher than sale prices, so the estimate sits below the median.

```
MV-00005  draft $12.99  | estimate: $11.32 ($10.80-$15.74, 4 listings, low confidence)  | active: 60, median $24.49 ($8.00-$125.00)
```

*Confidence* reflects how many comps there are and how closely they agree. It
doesn't say whether the search found the right kind of item, so a vague
`comp_query` can still give "high confidence" on the wrong items. Keep queries
short and specific: `"Nike Air Max Moto 2K"`, not the full title. Without a
`comp_query` the search uses the title, and the word filter is skipped.

**Sold prices (manual, uses Chrome).** `comps` also saves `comps.sold_url`, an eBay
sold-listings search. Claude opens it in Chrome and runs `ebay/sold_extract.js` on
the page, then removes results that don't match. Save the cleaned list with
`cli.py sold`. Use this for items where the estimate isn't reliable, such as
collectibles or anything whose price depends on the maker.

The price is always your call. The estimate is a starting point.

## inventory.csv

One row per published item: `sku, tote, title, price, listing_id, offer_id, env, batch, listed_at`.
To move or merge totes, edit the `tote` column.

## Configuration

Account-specific values are kept in **`.env`**, which git ignores:
app keys (client ID and RuName for each environment), the real account's policy
IDs, and the ship-from address. To set up a fresh copy:

```sh
cp .env.example .env    # then fill in your values
```

Write plain `KEY=value` lines. Comments must be on their own line, not at the end of a value.
If a value is missing, the tool stops and names the key it couldn't find.

**`ebay/config.py`** reads `.env` and also holds the settings that aren't personal:

- `ENV`: `"production"` or `"sandbox"`.
- Seller settings: 2-day handling, 30-day returns with the buyer paying return
  shipping, and calculated USPS Ground Advantage (`USPSParcel`) paid by the buyer.
- `PRODUCTION_POLICY_IDS` comes from `.env`. In production the tool uses these
  existing policies and never creates or changes policies.

`.gitignore` also excludes `inbox/`, `batches/` and `inventory.csv`, because they hold your photos, drafts and stock list.

### Secrets

The Cert ID and refresh token are stored in the macOS Keychain (service `ebay-listings`,
accounts `<env>-cert-id` and `<env>-refresh-token`), never in files. To store or
replace a Cert ID:

```sh
security add-generic-password -U -s ebay-listings -a production-cert-id -w
```

## Code layout

```
cli.py              command-line entry point
ebay/config.py      environment, app keys, seller settings
ebay/keychain.py    Keychain read/write
ebay/auth.py        OAuth sign-in and token refresh
ebay/api.py         REST helper (auth and marketplace headers, errors)
ebay/setup.py       ship-from location (+ policies in sandbox)
ebay/media.py       photo upload (Media API)
ebay/listings.py    inventory item -> offer -> publish, inventory.csv
ebay/comps.py       price estimate and sold-comp storage
ebay/sold_extract.js  browser script that reads an eBay sold-search page
```

## eBay quirks worth knowing

- USPS Ground Advantage's service code is `USPSParcel`.
- Motors parts offers need marketplace `EBAY_MOTORS`. `EBAY_US` gives error 25005 (invalid category).
- Sandbox publishing fails with 25002 whenever business policies are used. This is a
  known eBay sandbox outage, so test in production with one real item instead.
- When updating a policy, `categoryTypes` must include `default: true`, and an
  unchanged update returns a "same as in the system" error.
