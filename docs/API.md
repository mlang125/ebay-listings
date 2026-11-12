# API (v1)

The JSON API the phone apps use (iOS first, Android later). It runs inside the same Flask app as the
website, on the same database functions, so the two can't drift apart. Everything the website can do
should be reachable here; the apps add the camera capture flow on top.

Status: **in progress.** Endpoints marked *planned* are the agreed shape but not built yet.

## Conventions

- Base path `/api/v1`. A breaking change gets `/api/v2`; adding fields is not breaking, so clients ignore
  fields they don't know.
- JSON in and out (`Content-Type: application/json`), except photo uploads (`multipart/form-data`).
- Times are local ISO 8601 without a zone (`2026-11-11T14:05:00`), as stored. Money is a number in USD.
- SKUs look like `MV-00920`; anywhere a SKU is accepted, `920`, `mv920` and a sticker's QR address work too.
- Errors: an HTTP status plus `{"error": {"code": "not_found", "message": "No item MV-99999."}}`.
  `message` is plain English, safe to show to the person. Codes: `bad_request`, `unauthorized`,
  `forbidden`, `not_found`, `conflict`, `read_only` (eBay read-only mode refused it), `ebay_error`, `server_error`.
- Lists: `?limit=` (default 100, max 500) and `?offset=`; replies carry `total`.
- Every change is recorded with the signed-in person, exactly as on the website.

## Signing in

Apps don't use the website's cookies. A device signs in once and keeps a token:

```
POST /api/v1/auth/token        {"name": "matt", "password": "...", "device": "Matt's iPhone"}
-> 201 {"token": "<secret>", "user": {"id": 1, "name": "matt", "role": "admin"}}
```

Then every request sends `Authorization: Bearer <token>`. Store the token in the Keychain (iOS) or
EncryptedSharedPreferences (Android). A token stops working when it's revoked, when that person's
password changes, or when their account is turned off; the reply is then `401 unauthorized` and the
app should show its sign-in screen. Failed sign-ins count toward the same lockout as the website.

| Method | Path | What |
|---|---|---|
| POST | /auth/token | Sign in, get a token |
| DELETE | /auth/token | Sign out (revokes this token) |
| GET | /me | Who this token belongs to |

## Inventory

| Method | Path | What |
|---|---|---|
| GET | /items | Search and filter: `q`, `status` (or `ON_HAND`, `MISSING`, `SUGGESTED`), `tote` (`?` = unknown), `min_price`, `max_price`, `min_days`, `sort`, `dir` |
| GET | /items/{sku} | One item with its listings, sales, history and photos |
| GET | /totes | Every tote and place with counts |
| GET | /totes/{code} | What's in a tote, plus its recent history |
| PATCH | /items/{sku} | Edit our records: any of `title`, `price`, `cost`, `cost_note`, `notes` (empty cost or price = unknown). Title and price only when the item isn't live on eBay. -> `{"item", "changed": [...]}` |
| POST | /items/{sku}/move | `{"tote": "A5"}` -> `{"item", "done"}` |
| POST | /items/{sku}/missing | `{"missing": true, "note": "..."}` (false = found) |
| POST | /items/{sku}/status | `{"status": "ENDED", "note": "..."}` |
| POST | /items/{sku}/price-suggestion | `{"price": 12.99, "note": "..."}` Record a price to set on eBay by hand (nothing is sent to eBay) |
| POST | /items/{sku}/price-applied | `{"price": 12.99}` The price was changed on eBay; clears the suggestion |
| POST | /items/{sku}/relisted-as | `{"sku": "MV-00852"}` This cancelled, returned or ended item was listed again |
| POST | /items/bulk | `{"skus": [...], "action": "move"/"missing"/"found"/"status"/"drop", ...}` with `tote`, `status`, `note` or `percent` (drop = suggest a lower price). All or nothing -> `{"done", "skus"}` |
| POST | /totes/{code}/count | `{"here": [SKUs found], "extra": [SKUs found here but recorded elsewhere]}`. On-hand items not in `here` become missing, extras move in, the tote is marked checked -> `{"here", "missing", "new_missing", "found", "moved_in", "message"}` |
| POST | /totes/{code}/checked | Record that someone went through the tote -> 204 |
| POST | /totes/consolidate | `{"codes": ["B4", "C6"], "into": "B4"}` (no `into` = next free tote). Refuses a tote that has items unless it's listed -> `{"into", "moved"}` |
| POST | /totes/{code}/split | `{"plan": {"MV-00012": "C3"}}` Send items from this tote to others |

## Shipping and eBay

| Method | Path | What |
|---|---|---|
| GET | /picklist | Sold items waiting to ship, in tote order (`?days=0` = due today) -> `{"items", "total", "last_sync"}` |
| POST | /pack | `{"skus": [...] or "sku", "step": "LABEL_PRINTED"/"PACKAGED"/"SENT_TO_POST_OFFICE"/"BOUGHT", "box": "..."}` Only items waiting to ship change -> `{"changed", "skipped"}` |
| POST | /sync/orders | Fetch new orders from eBay (read only) -> `{"orders", "new_sales", "changes": [{"sku", "from", "to", "title"}]}` |
| POST | /sync/listings | Compare live eBay listings (read only) -> `{"live", "listed", "not_live", "unknown", "quantity"}`. Changes nothing; end items with /items/bulk |
| POST | /listings/quantities | `{"quantities": {"MV-00123": 2}}` Take eBay's units left for listed items, all or nothing |

## Capture (the camera flow)

A capture session is a batch: one tote, then items one after another, each with its photos. Photos
belong to their item from the moment they're taken, so nothing has to be split or guessed later.

| Method | Path | What |
|---|---|---|
| POST | /batches | `{"tote": "A5"}` (or no tote for the next free one) -> 201 `{"batch": ...}` |
| GET | /batches | Batches shot in the app, newest first; `?open=1` for ones not finished |
| GET | /batches/{id} | One batch with its items in shooting order |
| POST | /batches/{id}/items | Start the next item -> 201 `{"sku": "MV-00931", "batch": ...}`. The SKU is fixed now, so a sticker can be printed; a deleted item's SKU is never reused |
| PUT | /batches/{id}/items/{sku}/photos/{client_id} | Upload one photo: the body is the image (`Content-Type` `image/jpeg` or `image/png`; convert HEIC first, the drafter can't read it), or multipart with a `photo` field. `client_id` is 8-64 letters, digits or dashes (a UUID), so a retry from the offline queue never duplicates: 201 when added, 200 when it was already there. Max 30 MB |
| DELETE | /batches/{id}/items/{sku}/photos/{client_id} | Remove one photo |
| PATCH | /batches/{id}/items/{sku} | `{"note": {...}}` and/or `{"photos": [...]}` (all of the item's photo files, in the new order) |
| DELETE | /batches/{id}/items/{sku} | Drop an item started by mistake (kept as SKIPPED for the record) |
| POST | /batches/{id}/finish | The tote is done: no new items, but its items can still be fixed until they're drafted |
| GET | /note-tags | `{"recent": [...], "standard": [...]}`: this person's most used tags, then the standard list |
| GET | /photos/{batch}/{file} | A photo (the `url` given with each photo; needs the token) |
| POST | /batches/drafts | `{"batches": [41, 42], "model": "opus"}` Create drafts with Claude in the background, for finished batches; listers and admins only -> 202 `{"job", "estimate_usd", "no_photos": [SKUs left out]}`. About $0.10 an item with Opus; at most 300 items per job |
| GET | /jobs/{id} | Progress: `{"job": {"status", "done", "total", "errors": [{"sku", "error"}], "cost_usd", ...}}`. Status is `queued`, `running`, `done`, `failed` or `interrupted` (the server restarted: start the same batches again, drafted items are skipped) |
| GET | /jobs | The last 20 jobs |
| PATCH | /drafts/{sku} | *planned* Edit a draft (with `version`, like the website, so two editors can't overwrite each other) |

A batch looks like:

```
{"id": 41, "name": "A5-20261111", "tote": "A5", "source": "app", "created_at": "...", "finished_at": null,
 "items": [{"sku": "MV-00931", "position": 0, "drafted": false,
            "photos": [{"file": "MV-00931-3f2a....jpg", "url": "/api/v1/photos/A5-20261111/MV-00931-3f2a....jpg"}],
            "note": {"tags": ["Untested", "Has original box"], "text": "chip on the base"}}]}
```

The note replaces the whiteboard. Tags and text are both passed to Claude as written, so they may
overlap or say anything (up to 20 tags of 60 characters, and 4000 characters of text). Any item can be
changed (photos added, removed or reordered, note rewritten) at any time until its draft is created;
that's what lets the app swipe back to an earlier item.

Once drafted, an item's `draft` holds its title, price, condition and `needs_check` (questions a
person should answer before listing), and the item is reviewed and corrected on the website's
Drafts page like any other. Drafting also asks eBay (read only) for the item's category.

## Reports and people

| Method | Path | What |
|---|---|---|
| GET | /home | The home page: `attention` (each `{"key", "count", "what", "list", "filters"}`; `list` is `items`, `picklist`, `relists`, `totes` or `checks`, and for `items` the filters are /items query parameters), `counts` by status, `to_ship`, `overdue` and `due_today` SKUs, `today`'s work, `last_sync` |
| GET | /checks | Records that look wrong: `{"checks": [{"what", "why", "items": [{"sku", "title", "detail"}]}], "last_listing_check"}` |
| GET | /sales | `summary`, `months`, `bands` (sales by price), `active` (what's listed) |
| GET | /activity | Events newest first: `who` (a name, or `(none)` for unattended), `action`, `q` (a SKU or tote), `limit`/`offset` |
| GET | /users | Everyone, with role, on/off, last sign-in and how many changes they've made |
| POST | /users | Admins only: `{"action": "add", "name", "role", "password"}`, or `password`, `disable`, `enable`, `role`. A new password ends that person's other sign-ins; changing your own keeps this device signed in |
