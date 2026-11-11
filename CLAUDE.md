# ebay-listings: notes for Claude

A resale business's eBay tool: drafts listings from tote photos, publishes through eBay's
Sell APIs, tracks every item's tote and status in SQLite, and has a Flask web app for the
owner's small team (more workers later). README.md explains the commands and pages;
docs/ROADMAP.md has the plan.

## Hard rules

- **The GitHub repo is public.** Never commit business data: CSVs, the database, photos,
  drafts, `.env`, buyer details, sales figures, addresses, policy IDs, keys. `.gitignore`
  covers `data/`, `batches/`, `inbox/`, `*.csv`, `.env`. Check `git diff --cached` before committing.
- **Never change anything on eBay** (publish, revise or end listings, offers, photo uploads,
  policies) and never message buyers. `.env` has `EBAY_READ_ONLY=1`, which makes the code refuse;
  leave it on. Read-only calls (order sync, comps, categories) are fine but keep them rare.
- **Keep Claude API calls rare and small.** The key is for the drafter; don't run
  `draft-test` or other paid calls unless the owner asks.
- **The real database is `data/ebay.db`.** Change its schema only with a new numbered migration in
  `ebay/db.py` (never edit an old one). Back it up first (`cp data/ebay.db data/ebay-before-<what>.db`).
  Experiment on copies.
- **Every change to an item records who did it** (`db.log_event`, `db.set_status`, `db.move` with a user).
  Keep it that way for anything new.
- **Commits list only the owner as author**: no Co-Authored-By or "Generated with" lines.
- Don't touch other repositories, especially valify-all and valify-data-pipeline.
- No buyer names, emails or addresses are stored anywhere; keep it that way.

## Working on it

- Python 3.12 venv: `.venv/bin/python cli.py ...`. Web app: `.venv/bin/python cli.py serve`
  (port 5050; `--lan` for phones on the home wifi). Server-rendered Flask + Jinja, one stylesheet
  (`web/static/style.css`, light and dark), no JS framework. Keep it that way: small, plain, works on a phone.
- Test: `.venv/bin/python tests/test_web.py` runs the whole web app against a temp copy of the
  real database with eBay and Claude faked. Run it after every change; extend it for new features.
- Shell quirks on this Mac: `cat` is aliased to bat and `cp` is aliased; use `/bin/cat`, `/bin/cp`, `/bin/ls`.
- Match the existing code: short functions, plain SQL with `?` parameters (so a move to Postgres
  stays easy), docstrings that say why, user-facing text in plain English.

## Current state

Real inventory and order history are loaded (see `cli.py items`, `cli.py sales`). Totes are
`<letter><number>`; `OT` = other place, `UN` = needs a tote. Drafts live in the `drafts` table
(JSON per SKU). The Claude API drafter (`ebay/drafter.py`) is tested (Opus 5.5 is best, about
$0.10/item) but not wired into a real workflow yet.

## Unattended work (owner away, 2026-11-10)

Improve the website. Focus: inventory management, a better UI, sorting and filtering. Ideas, roughly in order:

1. Sortable columns on the items, tote and totes tables (click a header), with the sort kept in the URL.
2. Items page: filter by tote, status and price range together; show age (days listed) and last change.
3. Bulk actions on items and tote pages: select rows, then move to a tote, mark missing, or set status.
4. A "needs attention" home page: items in UN, missing items, cancelled and not relisted, stale listings
   (listed 90+ days), totes never checked.
5. Tote labels: a printable page per tote (big code and a QR code linking to its page; the QR as inline SVG,
   no external service).
6. Phone polish: tables that read well at phone width, bigger tap targets, sticky header.
7. Item page: edit title, price and notes for items in the database (not on eBay), with history.
8. Price changes on eBay are out of scope while read-only is on: record suggestions only.

Work on the branch `auto/improvements`, commit each finished improvement separately with a clear
message, run the test before each commit, and don't push. The owner reviews and merges.
