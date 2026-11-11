# ebay-listings

Turns a zip of item photos into live eBay listings through eBay's own Sell APIs,
and keeps track of which tote each item is stored in.

Python 3.12 with a virtualenv. One-time setup:

```sh
/opt/homebrew/bin/python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env    # then fill it in
```

Run commands with `.venv/bin/python cli.py ...` (or `source .venv/bin/activate` first, then `python cli.py ...`).

## The workflow

1. **Photograph a tote.** Before each item, take one *divider* photo of the whiteboard
   screen. Leave it blank, or write condition notes on it ("tested", "not working",
   "no lid"). Divider photos are never listed. Item or tote IDs written on the
   whiteboard are ignored.
2. **Zip the photos**, one zip per tote, and put the zip in `inbox/` (for example `inbox/batch1.zip`).
3. **Have Claude draft the batch.** Give Claude the zip and the tote ID (`<letter><number>`, e.g. `T1`).
   Claude splits the photos at each divider and assigns SKUs (`MV-#####`, from `cli.py next-sku`). It writes `batches/<batch>/drafts.json`, copies the
   listing photos into `batches/<batch>/photos/`, and loads the drafts into the database with `cli.py import-drafts <batch>`.
4. **Price check:** `cli.py comps <batch>`, or *Update estimate* on the website (see [Pricing](#pricing)).
5. **Review** in the web app: titles, condition, price, and anything listed under `needs_check`.
6. **Publish:** `cli.py publish <batch> [SKU ...]`. Each published item is
   recorded in the database with its tote and listing, and the event log says who listed it.

## Web app

```sh
.venv/bin/python cli.py user add <name> admin    # the first admin (after that, use the People page)
.venv/bin/python cli.py user password <name>     # sets their web password; type it in your own terminal
.venv/bin/python cli.py serve                    # http://127.0.0.1:5050 on this Mac
.venv/bin/python cli.py serve --lan              # also reachable from phones/laptops on the home wifi
```

The server restarts by itself when the code changes (after a pull or merge), so it never runs new pages
against old code. A server started before this change needs one manual restart.

Pages (every table sorts by clicking a column header; the sort stays in the URL):

- **Home:** what needs attention, each count linking to the list: items waiting in UN, missing items,
  cancellations not relisted, listings up 90+ days, price suggestions to apply, on-hand items with no
  known tote, totes never checked. Overdue and due-today shipments show at the top.
- **Find** (top bar): a SKU or just its number (`920`), a tote code, or words from a title.
- **Upload photos** (Drafts page): pick the tote and upload its photos (or a zip) from a phone or laptop.
  They're saved in upload order to `inbox/<tote>-<date>/` and listed as *waiting to be drafted*, with who took them.
- **Quick move** (top bar): pick a tote, then type SKU after SKU to bin items; the tote stays filled in.
- **Drafts:** each batch's drafts with their photos and state (*needs work*, *ready*, *published*,
  *skipped*). The editor covers title, price, condition, description, item specifics, package size,
  comp search words and open questions. A draft can't be published while it still has open questions
  (`needs_check`), so delete each question once it's settled. *Update estimate* re-runs `comps` for that draft.
  *Publish N ready* publishes every ready draft in the batch. Edits and publishes are logged under the signed-in person.
- **Items:** search by SKU or title and filter by status (plus *on hand*, *missing* and *price suggested*),
  tote, price range and days listed, together. 200 per page; *Download CSV* exports everything matching.
  - *Bulk actions:* tick rows (or tick all), then move them to a tote, mark them missing or found, set
    their status, or suggest a price N% lower (ending in .99). All or nothing.
  - *An item's page* shows its listings, sales, relist links and full history, and lets you move it or set
    its status by hand (with a note). *Our records* edits notes and cost (empty = unknown) and, for items
    not live on eBay, the title and price. For listed items, *Suggest* records a new eBay price; change it
    on eBay, then press *Applied*. Nothing on this page is sent to eBay.
- **Totes:** each tote's on-hand count, listed value and last check, plus the next free tote ID.
  The tote page has the same tick-and-act bar as the items list.
  - *Recent changes:* each tote page lists what moved in and out, combines, splits and checks, and who did them.
  - *Labels:* `/labels` prints one 4x6 label per tote: the code in big letters and a QR code that opens
    the tote's page. Set `LABEL_URL` in `.env` (e.g. `http://your-mac.local:5050`) so printed codes keep
    working if the Mac's IP address changes.
  - *Combine totes* (e.g. B4 + C6): everything goes into the next free ID (say A5), into one of
    the totes (keeping its label), or into an ID you type. Only on-hand items move; shipped items keep
    their old tote as history. A tote is free once nothing in it is on hand.
  - *Split a tote:* on its page, type where each item goes (MV-1 and MV-4 to T1, MV-3 to T2, ...)
    and press Move. Blank means it stays.
  - *Put items in*: type SKUs to move them into this tote.
- **Pick list:** sold, unshipped items grouped by tote, with a *Print* layout. *Get new orders from eBay*
  runs the order sync. *Label printed* and *Packed* mark progress for us; eBay only shows "shipped" to the buyer.
  Tick several items to mark them label printed, packed or dropped off together.
  *Pack by SKU* opens the packing station: pick a step, then type or scan SKU after SKU (a sticker's QR code
  works too, as it does on Quick move).

- **Relists:** cancelled and returned items, with suggested links to the SKU each was relisted under
  (same title on a newer listing). Tick and save, or link one by hand from the item's page.
- **Sales:** item sales, shipping paid by buyers, average sale, sell-through, cancellations, by month and by
  price, plus what's listed now and for how long.
- **Checks:** records that look wrong (listed with no price or listing ID, titles over 80 characters, the
  same title listed twice, sales with no order, cost at or above price), each linked to the item. The home
  page points here when anything turns up.
- **People** (click your name): who can sign in, their role and last sign-in. Admins add people, set
  passwords and roles, and turn accounts off. Roles: admins do everything; listers can also publish drafts;
  shippers do everything except publish and manage people.
- **Activity:** everything that happened, newest first, filterable by person, action and SKU/tote, plus a
  30-day count per person (listed, edited, moved, labels, packed).

**Missing items.** An item is where the records say until someone presses *Missing* (on the tote or item
page). It keeps its tote, gets a "missing" tag everywhere including the pick list, and *Found* clears it.
*I checked this tote* records who went through a tote and when; it's never required.
*Count items* on a tote's page is for going through a tote with a phone: tick what's there and save. Anything
not ticked is marked missing, ticked missing items are found again, SKUs typed in the box move into the tote,
and the tote is recorded as checked. A wrong move can be undone with *Move back* in the item's history.
*Stickers* (in the bar for ticked items, or *Print a SKU sticker* on an item's page) prints 2.25×1.25 in
SKU stickers with a QR code that opens the item's page.

**Who did what.** Every change is recorded in the event log with the person who made it: sign-ins, draft
edits, publishes, comps, moves, combines, splits, missing/found, tote checks, status changes, order syncs
and imports. On the command line that's `CLI_USER` from `.env`; commands that change anything refuse to run
without it. Two people editing the same draft can't overwrite each other: the second save is refused and shows who saved first.
The session-signing key is created in `data/web_secret_key` on first run.
`--lan` serves plain HTTP, so use it only on your own wifi. Port 5050 avoids macOS AirPlay, which uses 5000.
After 10 wrong passwords for a name in 15 minutes, that name can't sign in until the 15 minutes pass.
With `EBAY_READ_ONLY=1` in `.env`, nothing can be published or changed on eBay (an "eBay read-only" tag shows).

Phones get a layout of their own: the top bar stays put, item lists turn into cards, and buttons are bigger.
`tests/test_web.py` checks the whole site against a temp copy of the database (run it after any change).

## Commands

| Command | What it does |
|---|---|
| `cli.py login` | One-time browser sign-in. Stores the refresh token in the macOS Keychain (lasts about 18 months). |
| `cli.py check` | Confirms the token works and shows the account's selling status. |
| `cli.py setup` | Creates the ship-from location. In sandbox it also creates the business policies. Safe to re-run. |
| `cli.py comps <batch> [SKU ...]` | Price estimate from active listings plus a sold-search link for each unpublished draft. |
| `cli.py sold <SKU> <file.json>` | Saves sold prices read from eBay's website on the draft. |
| `cli.py publish <batch> [SKU ...]` | Publishes drafts: all of them, or only the SKUs given. |
| `cli.py import-drafts [batch ...]` | Loads new drafts from `batches/<batch>/drafts.json` into the database. Drafts already loaded are left alone, so edits are never overwritten. |
| `cli.py export-drafts <batch> <file>` | Writes a batch's drafts, as stored now, to a JSON file. |
| `cli.py import` | Loads new drafts from every batch and the old `inventory.csv`. Safe to re-run. |
| `cli.py sync [DAYS]` | Pulls eBay orders (default: last 90 days; the API goes back further, so `sync 365` gets everything) and updates statuses: sold, shipped, cancelled. An item sold more than once follows its latest order. |
| `cli.py picklist [DAYS]` | Sold, unshipped items grouped by tote. `DAYS` limits it to orders due within N days (0 = today). |
| `cli.py items [STATUS]` | Lists items, optionally only one status (`PENDING`, `LISTED`, ...). |
| `cli.py history <SKU>` | Shows an item's event log: what happened, when, and who did it. |
| `cli.py tote <CODE>` | Lists what's on hand in a tote or place. |
| `cli.py move <TOTE> <SKU> [SKU ...]` | Puts items in a tote and logs the move. |
| `cli.py merge <FROM> <TO>` | Moves everything on hand in one tote into another. |
| `cli.py consolidate <TOTE> <TOTE> [...] [--into CODE]` | Combines totes, into the next free ID unless `--into` says otherwise. |
| `cli.py free-tote` | Prints the next free tote ID (A1 to Z9). |
| `cli.py missing <SKU> ...` / `found <SKU> ...` | Marks items missing from their tote, or found again. |
| `cli.py relists` | Cancelled/returned items: suggested relist links and what's not relisted. `--link` saves the suggestions; `relists <OLD> <NEW>` links one. |
| `cli.py sales` | Sales numbers by month. |
| `cli.py import-tracking <sheet> <active> "<last binned title>"` | Matches active listings to the tote-tracking sheet. |
| `cli.py import-listings <report>` | Condition, category, quantity and price from Seller Hub's All listings report. |
| `cli.py import-orders <report> [sheet "<title>"]` | Orders and sold items from Seller Hub's Orders report. Buyer details aren't stored. Safe to re-run: orders already in the database keep their status. Run `sync 365` after it to pick up cancellations, which the report doesn't show. |
| `cli.py next-sku` | Prints the next free `MV-#####`. |
| `cli.py export <file.csv>` | Writes SKU, status, tote, price, title and listing ID to a CSV. |
| `cli.py user add <name> [role]` / `user list` | Manages users (roles: admin, lister, shipper). |
| `cli.py user password <name>` | Sets a user's web password. |
| `cli.py serve [--lan]` | Starts the web app (see [Web app](#web-app)). |

`publish` skips drafts that have a `skip` field or are already published in the
current environment, so re-running it is safe.

## drafts.json

How Claude hands a new batch over (then `cli.py import-drafts <batch>`). After that the draft lives in the
database's `drafts` table, one JSON document per SKU in the same shape. One file per batch: `{"batch", "tote", "source_zip", "items": [...]}`. Each item has:

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
2. drops your own listings (found through the database);
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

## Database

SQLite file at `data/ebay.db` (change with `DB_PATH` in `.env`). Code: `ebay/db.py`.

- **items**: one row per physical item (SKU), with its status, tote, price and cost.
  A cost of NULL means *unknown*, not zero.
- **events**: append-only log of everything that happens to an item: created, listed,
  status changes. Each event records the actor (`cli`, `sync`, `auto`, `import`) and the user.
  `CLI_USER` in `.env` sets who `cli.py` records as the user.
- **locations**: totes (`<letter><number>`) and two places that aren't totes: `OT` other
  (plush bin, house closet, storage, ...) and `UN` not binned yet. An empty location means unknown
  (e.g. sold before the tracking sheet existed). Totes get consolidated often, so an item's
  location is just its latest one; every move is in the event log (`move`, `merge`).
- **drafts**: unpublished (and published) drafts, one JSON document per SKU, with a version number
  so concurrent edits are caught. Code: `ebay/drafts.py`.
- **listings**, **batches**, **locations** (totes now; shelves and racks later),
  **users**, **orders** and **order_lines** (filled by the order sync).

Statuses: `PENDING → LISTED → BOUGHT → LABEL_PRINTED → PACKAGED → SENT_TO_POST_OFFICE → DELIVERED`,
plus `SKIPPED`, `ENDED`, `CANCELLED` and `RETURNED`. Status changes go through `db.set_status()`,
which always writes an event.

Schema changes are numbered migrations in `db.MIGRATIONS` and run automatically on connect.
Never edit a migration that has already run; add a new one.

`inventory.csv` is the old tote list from before the database. `cli.py import` reads it,
and nothing writes to it any more.

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
ebay/listings.py    inventory item -> offer -> publish, records the listing in the database
ebay/db.py          database schema, migrations, items, events, SKUs
ebay/importer.py    loads existing drafts and the old inventory.csv into the database
ebay/comps.py       price estimate and sold-comp storage
ebay/sold_extract.js  browser script that reads an eBay sold-search page
ebay/orders.py      order sync (Fulfillment API) and the pick list
ebay/drafts.py      drafts in the database: load, save (with edit conflicts), import/export drafts.json
ebay/sales.py       sales numbers
web/                Flask web app: routes in __init__.py, templates/, static/
```

## eBay quirks worth knowing

- USPS Ground Advantage's service code is `USPSParcel`.
- Motors parts offers need marketplace `EBAY_MOTORS`. `EBAY_US` gives error 25005 (invalid category).
- Sandbox publishing fails with 25002 whenever business policies are used. This is a
  known eBay sandbox outage, so test in production with one real item instead.
- When updating a policy, `categoryTypes` must include `default: true`, and an
  unchanged update returns a "same as in the system" error.
