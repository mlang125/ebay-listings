# Roadmap

## Goal

Get items out of the house and into storage as listings, and get sold items out the
door, at a pace that grows with the team. The same system should work whether
it's two people doing 4–5 sales a day or a warehouse crew doing hundreds.

That means:

- **Every step can be done by someone who isn't a developer.** A browser GUI sits on
  top of everything, and the CLI stays for automation and power use.
- **Every action is recorded.** Who did what, to which item, and when. Analytics
  answers questions from this record, not from people's memory.
- **Small-team shortcuts don't break the big-team model.** Two people can skip
  steps. The system still keeps the same statuses and records who did what.

## Guiding principles

1. **Build what the current team needs, using structures that scale.** A database
   table for users costs almost nothing today, while retrofitting one later costs
   a lot. A permissions system with 12 roles, on the other hand, can wait.
2. **One record per physical item.** The SKU (`MV-#####`) is the item's identity
   across listing, storage, sale, and shipping. It doesn't belong to eBay or any
   one channel.
3. **Append-only event log.** Status changes are events (`item MV-00042: LISTED → BOUGHT,
   by sync, 2026-11-09 10:14`). The current status is derived from events, so history
   is never lost and every report traces back to who did what.
4. **Unknown is not zero.** A missing cost, weight, or packer is recorded as *unknown*,
   and each report shows how complete its data is. A $3 box of tape that nobody
   logged shouldn't distort profit numbers or block anything.
5. **Don't rebuild what's solved.** Accounting (AP/AR, taxes, books) stays in
   QuickBooks or similar; we export to it. Label purchasing goes through a carrier
   or label API. Order and payment processing stays with eBay.

## Where we are now

- CLI tool: zip of tote photos → Claude drafts listings → review `drafts.json` → publish
  through the eBay Sell APIs. It also records SKU → tote in `inventory.csv`.
- Price estimate from active listings, plus an optional sold-price lookup in Chrome.
- Drafting happens in a Claude Code session on one laptop. That's the main
  bottleneck: nobody else can run a batch on their own.
- Hundreds of existing listings and sales were created before this tool, through
  Seller Hub and Floaro. The tool doesn't know about them yet.

## Item lifecycle

```
PENDING ─▶ LISTED ─▶ BOUGHT ─▶ LABEL_PRINTED ─▶ PACKAGED ─▶ SENT_TO_POST_OFFICE ─▶ DELIVERED
   │          │         │                                         ▲
   ▼          ▼         ▼                                         │
 SKIPPED    ENDED    CANCELLED                       carrier acceptance scan
                     RETURNED ─▶ (relist or write off)
```

- **PENDING**: drafted, not yet listed.
- **BOUGHT**: set automatically when the eBay order sync sees a sale.
- **LABEL_PRINTED → PACKAGED → SENT_TO_POST_OFFICE**: set by people (button or barcode scan).
  The carrier's first tracking scan confirms SENT_TO_POST_OFFICE independently.
- **DELIVERED**: set from tracking.
- **Household mode (override):** a setting that lets one action cover several steps.
  For example, "print label" can also mark the item PACKAGED. The events are still
  written, marked `auto`, so reports stay consistent when the team grows.

## Architecture

```
 phone / laptop browsers ──▶  web app (GUI)  ─┐
                              cli.py         ─┼─▶  core library (ebay/…)  ─▶  database
 scheduled jobs (order sync, tracking)       ─┘         │
                                                        ├─▶ eBay APIs (Sell, Browse, Finances)
                                                        ├─▶ Claude API (drafting from photos)
                                                        └─▶ label/tracking provider
```

- **One core library, two front ends.** The GUI and the CLI call the same functions,
  so features don't fork.
- **Web app, not a desktop app.** It runs on the current laptop for now. Anyone on
  the Wi-Fi uses it from a browser, including phones, which can upload photos
  straight from the camera. Moving it to a cloud server later doesn't change how
  people use it, so keep cloud-friendly habits from the start: settings in `.env`,
  file paths in config, no Mac-only calls in the web app.
- **Classic-style GUI:** server-rendered pages with plain forms, tables, and
  keyboard-friendly lists. Fast, predictable, and easy to learn. The CLI stays a
  full equal: anything the GUI does, `cli.py` can do too.
- **Database:** SQLite now, as a single file with no server to run. The schema is
  written so moving to PostgreSQL later is a swap, not a redesign. Switch once
  several people write at the same time or the app moves to the cloud.
- **Drafting is a separate, swappable step:** photos and tote go in, a draft comes out
  into the database. For now Claude Code fills this step in the terminal, which is
  covered by the subscription. Later the app fills it through the Claude API, so
  anyone can process a batch without a Claude Code session. Nothing else changes
  when the drafter does. Switch once a measured test batch shows the cost per item
  is worth it (rough estimates are under *Drafting cost* below).
- **Tooling change:** the stdlib-only rule worked for a CLI but not for a multi-user
  web app. Move to Python 3.12 (already installed through Homebrew) with a small set
  of dependencies: **Flask** with server-rendered pages (decided). Avoid a
  separate JavaScript front-end build until there's a real need for one.

### Core data model (first cut)

| Table | Holds |
|---|---|
| `users` | name, role (admin / lister / shipper), active |
| `locations` | totes now; later shelf → rack → zone (a tote can sit inside a location) |
| `batches` | one photo session: tote, photographer, created_at |
| `items` | SKU, title, condition, current status, location, cost (nullable), batch |
| `photos` | item, file, taken_by, quality flags |
| `listings` | item, channel (eBay), listing/offer IDs, price, listed_by |
| `orders`, `order_lines` | eBay order, buyer-paid shipping, ship-by date, line → item |
| `shipments` | order, label cost, carrier, tracking, label_printed_by, packaged_by, package used |
| `events` | append-only log: who, what, item/order, from → to, when, `auto` flag |
| `expenses` | supply purchases as lump sums (boxes, tape, labels), date, amount, optional notes |
| `sourcing_lots` | where a tote or lot came from and what it cost (optional, per item or per lot) |

## Phases

### Phase 0: Foundations (do first, small)

The base that everything else builds on.

- [x] Move to Python 3.12 and a virtualenv; Flask installed.
- [x] SQLite database (`ebay/db.py`) with items, events, listings, batches, locations, users,
      orders and order_lines; `inventory.csv` and the drafts imported. Shipments, expenses and
      sourcing lots get added as migrations when their phase starts.
- [x] Import the 916 active listings with their totes from the tracking sheet (2026-11-08).
- [ ] **Import past sales.** This step makes the database the real
      source of truth. Floaro SKUs (`<tote>-#####`) contain the tote, so it can be filled in
      automatically. Anything without one gets tote *unknown* and is assigned when found.
- [x] `users` table and `CLI_USER`. Web login comes with the GUI in Phase 1.
- [x] Order sync (`cli.py sync`, Fulfillment API): sales → BOUGHT, shipped → SENT_TO_POST_OFFICE,
      cancelled before shipping → CANCELLED. Runs by hand for now; schedule it with the web app.
- [x] First pick list (`cli.py picklist [DAYS]`), pulled forward from Phase 2.

### Phase 1: Listing GUI (the first real milestone)

So someone other than me can take a tote from photos to live listings.

- [ ] **New batch:** choose a tote, then upload photos from a phone or laptop (or drop in a zip).
      The divider-photo convention stays the same.
- [x] **Web app** (`cli.py serve`): sign-in per person, drafts review queue with photos, editor,
      price estimate, publish (single or all ready), items search and history, totes with move/merge,
      printable pick list with *Label printed* / *Packed*, and a button for the order sync.
- [x] **Tote tools:** combine totes (into the next free ID, one of them, or a custom ID) and split a
      tote across several; only on-hand items move. All past orders imported (May 2026 on).
- [x] **Drafts live in the database** (`drafts` table, JSON per SKU, versioned). Claude still hands a batch
      over as `drafts.json`, loaded with `cli.py import-drafts`.
- [x] **Every change records who made it** (web sign-in or `CLI_USER`), with an Activity page.
- [x] Relist links for cancelled/returned items; *Missing*/*Found* flags and optional tote checks.
- [x] Sales numbers page (`/sales`, `cli.py sales`). Days-to-sell waits for reliable listing dates. Claude Code (terminal) writes them there for now;
      the GUI never depends on how a draft was produced.
- [ ] **API drafter (when it's worth it):** the app splits the photos, sends each item to the
      Claude API, and stores the draft, including its `needs_check` questions, comp query, and
      package guess. Run it first on one test batch and record the real cost per item.
- [ ] **Review queue:** a list of drafts showing what needs attention first: open
      questions, low-confidence prices, missing required item specifics. Edit fields
      inline, see the photos and the price estimate, then approve.
- [ ] **Publish** the approved items. Each listing records who listed it.
- [ ] **Photo quality checks** at upload: blur and darkness detection, plus too few photos.
      These are flagged before drafting, not after a buyer complains.
- [ ] Measure the Claude API cost per item and the time from photo to listing.

### Phase 2: Shipping

**When to start:** once shipping prep starts to take real time. The pick list comes
first, since it's cheap and needs no label automation.

Labels go through three stages. Each stage only replaces the label step, so the
pick list, statuses, and analytics stay the same throughout.

| Stage | When | How labels are bought | What our app does |
|---|---|---|---|
| **A. Now** | 2 people, ~5 sales/day | eBay's bulk labels in Seller Hub (select orders → buy all → 4×6 PDF). Free, with eBay's USPS rates; tracking is attached to the order automatically. | Pick list and statuses. It sees the tracking appear on the order and marks it LABEL_PRINTED. |
| **B. Packing station** | a few workers, or labels printed in our app | **EasyPost** API (usage-based, small per-label fee, commercial and cubic USPS rates). Shippo is the backup option. | Buys labels, prints 4×6 straight to the thermal printer, and uploads tracking to eBay (Fulfillment API). |
| **C. Volume** | hundreds/day | Negotiated carrier contracts (USPS/UPS) connected through EasyPost ("bring your own carrier account") | Same as B, picking the cheapest rate per package. |

- **Rate check for Stage A (no code):** for the next ~10–20 orders, compare eBay's label
  price with Pirate Ship's (free; it has cubic pricing and its own eBay import). Use
  whichever is cheaper overall for our package sizes. Either works with the pick list.
- **eBay Logistics API** (buying eBay labels in-app): restricted to approved partners.
  Applying is free but likely to be refused, like Marketplace Insights. Don't plan around it.

Work items:

- [ ] **Pick list:** sold items grouped by tote, with filters for *due today*,
      *today + tomorrow*, and *within 2 days*, based on eBay's ship-by date. Printable.
- [ ] **Label detection (Stage A):** order sync notices new tracking numbers and marks
      LABEL_PRINTED (by "eBay labels" when we don't know who printed it).
- [ ] **Packing station:** scan or click to mark items PACKAGED and SENT_TO_POST_OFFICE.
      Record who did it and which package type was used.
- [ ] **SKU stickers:** a small barcode label printed at listing time for each item or
      tote, so scanning replaces typing.
- [ ] **Tracking sync:** carrier scans and deliveries update statuses.
- [ ] **Label cost capture:** from eBay's Finances API (Stage A) or the label provider (B/C),
      so shipping-margin reports work in every stage.
- [ ] *(Stage B)* EasyPost integration: bulk purchase, direct 4×6 printing, tracking upload.

### Phase 3: Reporting & analytics

All of these come from the event log and order data. No extra data entry is needed.

- [ ] **Throughput:** items photographed, listed, and shipped per day and week, per person.
- [ ] **Shipping margin:** label cost compared with what the buyer paid for shipping,
      per order and in total. Weight guesses that are too low show up here.
- [ ] **Profit per item and order:** sale price − eBay fees (Finances API) − label cost
      + buyer-paid shipping − item cost *(when known)*. Supplies are counted as an
      overhead rate per order, spread across orders from the lump-sum `expenses`,
      not tracked per box.
- [ ] **Exceptions:** items with a label printed but no carrier scan after N days
      ("probably still in the warehouse"), orders past their ship-by date, and stale
      listings with no sale after X days.
- [ ] **Quality:** returns and "not as described" cases (eBay Post-Order API) grouped by
      lister, photographer, packer, packaging type, and category. These show whether a
      problem is concentrated in one area or spread evenly. Photo-quality flags are
      grouped the same way.
- [ ] Export to the accounting system (sales, fees, shipping, expenses).

### Phase 4: Scale-out (when the team grows)

Build these when there's a real need, not before.

- [ ] Host in the cloud and move to PostgreSQL; add backups and monitoring.
- [ ] Roles and permissions beyond admin, lister, and shipper; crews/teams for reporting.
- [ ] Warehouse locations (zone → rack → shelf → tote) and a sorted pick path.
- [ ] Returns workflow: receive the item, inspect it, then relist or write it off.
- [ ] Sourcing and purchasing: record lots and pallets, then report return on investment per source.
- [ ] Other sales channels (Mercari, Poshmark, Facebook Marketplace): the SKU and item
      model already allow this, and selling one copy delists the others.

## Drafting cost (rough, to be measured)

Estimates for one item with about 6 photos (roughly 10–15K input tokens, mostly the
images) and a draft of about 1–4K output tokens. Prices are list prices for each
model, per million tokens (input / output), as of 2026-10; check current pricing
before relying on them.

| Model | $/M tokens (in / out) | ≈ per item | ≈ per 100 items |
|---|---|---|---|
| Claude Opus 5.5 | $4 / $20 | $0.10–0.15 | $10–15 |
| Claude Sonnet 5.5 | $2 / $10 | $0.05–0.07 | $5–7 |
| Claude Haiku 5.5 | $0.10 / $0.50 | under $0.01 | under $1 |

- Drafting isn't urgent, so the **Batch API** (half price, results within hours) fits it well.
- **Resizing photos** before sending cuts image tokens. eBay keeps the full-size copies.
- The fixed instructions can be **cached**, so only the photos are full price on each call.
- Test the same batch on two models and compare the draft quality, not just the price.

## What we're deliberately not building

- **Accounting (AP/AR, general ledger, payroll):** use existing software and export to it.
- **Our own payment, checkout, or storefront:** eBay handles these.
- **Exact per-box supply costing:** a rough overhead rate answers the real question with far less effort.

## Decisions

| Decision | Choice |
|---|---|
| Where the app runs | This laptop for now; cloud later (keep it cloud-friendly) |
| Web framework | Flask, server-rendered, classic-style pages; CLI kept as an equal |
| Labels | Stage A: eBay bulk labels (or Pirate Ship if the rate check favors it). Stage B: EasyPost |
| Public repo | Code public, business data kept out (`.gitignore`) |

## Open questions

| Question | Needed by |
|---|---|
| eBay vs. Pirate Ship rates on our real package sizes (rate check above) | Phase 2 |
| Thermal printer model (4×6 PDF works with most: Rollo, Zebra, Dymo 4XL) | Phase 2 |
| Claude API cost per listing (measured in Phase 1) | end of Phase 1 |
| Always-on machine: does the laptop stay awake and on the network enough? | Phase 1 |
