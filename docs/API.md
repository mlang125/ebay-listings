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
| PATCH | /items/{sku} | *planned* Edit our records (title, price, notes, cost) |
| POST | /items/{sku}/move | *planned* `{"tote": "A5"}` |
| POST | /items/{sku}/missing | *planned* `{"missing": true}` |
| POST | /items/{sku}/status | *planned* `{"status": "ENDED", "note": "..."}` |
| POST | /items/bulk | *planned* Same actions for many SKUs, all or nothing |
| POST | /totes/{code}/count | *planned* `{"here": [...], "extra": [...]}` |
| POST | /totes/consolidate, /totes/{code}/split | *planned* |

## Shipping and eBay

| Method | Path | What |
|---|---|---|
| GET | /picklist | *planned* Sold, unshipped items by tote |
| POST | /pack | *planned* `{"sku": "...", "step": "PACKAGED", "box": "..."}` |
| POST | /sync/orders | *planned* Fetch new orders from eBay (read only) |
| POST | /sync/listings | *planned* Compare live eBay listings (read only); returns the differences |

## Capture (the camera flow)

A capture session is a batch: one tote, then items one after another, each with its photos. Photos
belong to their item from the moment they're taken, so nothing has to be split or guessed later.

| Method | Path | What |
|---|---|---|
| POST | /batches | *planned* `{"tote": "A5"}` (or `{"tote": null}` for the next free tote) -> batch |
| POST | /batches/{id}/items | *planned* Start the next item; it gets its SKU now, so a sticker can be printed |
| PUT | /batches/{id}/items/{sku}/photos/{client_id} | *planned* Upload one photo. `client_id` is a UUID the phone makes, so a retry from the offline queue never duplicates |
| PATCH | /batches/{id}/items/{sku} | *planned* `{"note": "not working"}` (replaces the whiteboard), photo order, delete |
| POST | /batches/{id}/finish | *planned* The tote is done |
| POST | /batches/drafts | *planned* `{"batches": [3, 4]}` Create drafts with Claude in the background -> job |
| GET | /jobs/{id} | *planned* Progress: `{"done": 12, "total": 40, "errors": [...]}` |
| GET | /batches, /batches/{id} | *planned* Batches and their drafts, for review |
| PATCH | /drafts/{sku} | *planned* Edit a draft (with `version`, like the website, so two editors can't overwrite each other) |

## Reports and people

| Method | Path | What |
|---|---|---|
| GET | /home | *planned* The home page's attention list, today's counts, what's due |
| GET | /checks | *planned* |
| GET | /sales | *planned* |
| GET | /activity | *planned* |
| GET, POST, PATCH | /users | *planned* Admins only |
