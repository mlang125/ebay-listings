"""Draft a listing from an item's photos with the Claude API.

One request per item: the whiteboard (divider) photo plus the item's photos go to
Claude, which returns the draft as JSON (a fixed schema, so the reply always parses).
eBay's Taxonomy API then picks the category ID from Claude's description of the item,
and any item specifics that category requires but the draft lacks become open questions.

The API key lives in the Keychain (service ebay-listings, account anthropic-api-key).
"""

import base64
import io
import json
import time
import urllib.parse

import anthropic
from PIL import Image, ImageOps

from . import api, keychain

MODELS = {
    "opus": "claude-opus-5-5",
    "sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-5-5",
}

# USD per million tokens (input, output), for prompts under 100K tokens. Thinking counts as output.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}

CONDITIONS = [
    "NEW", "NEW_OTHER", "NEW_WITH_DEFECTS", "LIKE_NEW", "CERTIFIED_REFURBISHED", "SELLER_REFURBISHED",
    "USED_EXCELLENT", "USED_VERY_GOOD", "USED_GOOD", "USED_ACCEPTABLE", "FOR_PARTS_OR_NOT_WORKING",
]

# Claude looks at images at up to about this many pixels on the long edge; bigger only costs upload time.
MAX_EDGE = 1568

SYSTEM = """You write eBay listings for a small resale business that sells secondhand and \
open-box goods. You get photos of one item and return its draft listing. A person reviews \
every draft before it's published, so when you can't tell something from the photos, say so \
in needs_check rather than guessing: a wrong detail in a listing causes returns.

The first photo is a divider photo of a small whiteboard (or a screen). Whatever is written on \
it is the seller's note about this item: condition ("tested", "not working", "no lid", "open \
box"), or what it is. Copy that note into whiteboard_notes and let it override what the photos \
suggest. Short codes like "N1", "T3" or "MV-00012" are tote or item IDs: leave them out of \
whiteboard_notes and don't ask about them. The divider photo is never listed.

Items photographed in the phone app have no divider photo: the seller's note comes as text \
instead (quick tags like "Untested" plus anything they typed). Treat it exactly like the \
whiteboard: copy it into whiteboard_notes and let it override the photos. Tags and text may \
repeat each other.

Fields:
- title: at most 80 characters, and use the space. Lead with brand and model/name, then the \
item type and the details buyers search for (size, color, edition, year, quantity, "Set of 2", \
"CIB", "Open Box", "Untested", "For Parts"). No filler words, emoji or ALL CAPS.
- condition: one of the enum values. NEW only for sealed, unused items; NEW_OTHER for open box \
or unused without original packaging; USED_EXCELLENT is eBay's general "Pre-owned"; \
FOR_PARTS_OR_NOT_WORKING when it's broken or the whiteboard says so.
- condition_description: one or two plain sentences on the actual condition: wear, flaws, \
what's missing, whether it was tested. Only what the photos or whiteboard show.
- description: a short paragraph on what the item is (from labels, tags and packaging you can \
read), then a paragraph on condition ending with "See photos." Plain text, no headings, no \
marketing claims you can't see.
- aspects: eBay item specifics as name/values pairs, using eBay's usual names (Brand, Model, \
Type, Color, Material, Size, Character, Franchise, Platform, MPN, Country of Origin, ...). \
Include only values you can read or clearly see; put "Unbranded" for Brand only when there is \
clearly no brand.
- category_query: 2-5 words naming the item type for eBay's category search \
(e.g. "spice rack organizer", "hubcap wheel cover", "teapot").
- category_guess: the eBay category path you'd expect, for a person to sanity-check.
- comp_query: 2-6 words to search eBay for the same item when pricing: brand plus model or name, \
not the whole title.
- price_guess: a starting price in USD for a fixed-price listing with calculated shipping, \
based on what this sells for used on eBay. A person checks it against real sales.
- package: shipping weight in ounces (item plus packing) and box size in inches. Estimate \
from the item's apparent size; close is good enough.
- needs_check: short questions a person must answer before listing: things that affect value \
or accuracy that the photos don't settle (does it work, is a part missing, which edition, \
measurements you guessed). Empty if nothing is uncertain."""

SCHEMA = {
    "type": "object",
    "properties": {
        "whiteboard_notes": {"type": "string"},
        "title": {"type": "string"},
        "condition": {"type": "string", "enum": CONDITIONS},
        "condition_description": {"type": "string"},
        "description": {"type": "string"},
        "aspects": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string"}, "values": {"type": "array", "items": {"type": "string"}}},
                "required": ["name", "values"],
                "additionalProperties": False,
            },
        },
        "category_query": {"type": "string"},
        "category_guess": {"type": "string"},
        "comp_query": {"type": "string"},
        "price_guess": {"type": "number"},
        "package": {
            "type": "object",
            "properties": {k: {"type": "number"} for k in ("weight_oz", "length_in", "width_in", "height_in")},
            "required": ["weight_oz", "length_in", "width_in", "height_in"],
            "additionalProperties": False,
        },
        "needs_check": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["whiteboard_notes", "title", "condition", "condition_description", "description", "aspects",
                 "category_query", "category_guess", "comp_query", "price_guess", "package", "needs_check"],
    "additionalProperties": False,
}


def client():
    key = keychain.get("anthropic-api-key")
    if not key:
        raise SystemExit("No Claude API key. Store one with:\n"
                         "  security add-generic-password -U -s ebay-listings -a anthropic-api-key -w")
    return anthropic.Anthropic(api_key=key)


def _image_block(source):
    """A JPEG image block, shrunk to MAX_EDGE. `source` is a path or an open file."""
    with Image.open(source) as img:
        img = ImageOps.exif_transpose(img).convert("RGB")
        img.thumbnail((MAX_EDGE, MAX_EDGE))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=85)
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                         "data": base64.standard_b64encode(buf.getvalue()).decode()}}


def cost(model, usage):
    price_in, price_out = PRICES[model]
    return (usage.input_tokens * price_in + usage.output_tokens * price_out) / 1_000_000


def _category(query):
    """eBay's top category suggestion for the words, plus the runners-up."""
    found = api.call("GET", "/commerce/taxonomy/v1/category_tree/0/get_category_suggestions?q=" + urllib.parse.quote(query))
    suggestions = [(s["category"]["categoryId"], " > ".join(
        [a["categoryName"] for a in reversed(s.get("categoryTreeNodeAncestors", []))] + [s["category"]["categoryName"]]))
        for s in found.get("categorySuggestions", [])]
    return suggestions[0] if suggestions else (None, None), suggestions[1:4]


def _required_aspects(category_id):
    found = api.call("GET", "/commerce/taxonomy/v1/category_tree/0/get_item_aspects_for_category?category_id=" + category_id)
    return [a["localizedAspectName"] for a in found.get("aspects", []) if a.get("aspectConstraint", {}).get("aspectRequired")]


def draft(claude, model, divider, photos, tote=None, effort="medium", note=None):
    """Draft one item. `divider` and each photo are paths or open files; `note` is the seller's typed
    note (the phone app's replacement for the divider photo). Returns (draft, run info)."""
    content = [{"type": "text", "text": "Divider photo (the whiteboard note for this item):"}]
    if divider is not None:
        content.append(_image_block(divider))
    elif note:
        content[0]["text"] = "The seller's note about this item:\n" + note
    else:
        content[0]["text"] = "There is no divider photo for this item."
    content.append({"type": "text", "text": "Item photos, in listing order:"})
    content += [_image_block(p) for p in photos]
    content.append({"type": "text", "text": "Draft the listing."})

    started = time.monotonic()
    with claude.messages.stream(
        model=model,
        max_tokens=16000,
        system=SYSTEM,
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": SCHEMA}},
        messages=[{"role": "user", "content": content}],
    ) as stream:
        response = stream.get_final_message()
    run = {
        "model": model,
        "effort": effort,
        "seconds": round(time.monotonic() - started, 1),
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
        "cost_usd": round(cost(model, response.usage), 4),
        "stop_reason": response.stop_reason,
    }
    if response.stop_reason != "end_turn":
        raise RuntimeError("Claude stopped early (%s)" % response.stop_reason)
    out = json.loads(next(b.text for b in response.content if b.type == "text"))

    item = {
        "whiteboard_notes": out["whiteboard_notes"],
        "title": out["title"],
        "condition": out["condition"],
        "condition_description": out["condition_description"],
        "description": out["description"],
        "aspects": {a["name"]: a["values"] for a in out["aspects"] if a["values"]},
        "comp_query": out["comp_query"],
        "price": round(out["price_guess"], 2),
        "price_note": "Claude's guess; check comps",
        "package": out["package"],
        "needs_check": list(out["needs_check"]),
        "category_guess": out["category_guess"],
    }
    if len(item["title"]) > 80:
        item["needs_check"].append("Title is %d characters; eBay allows 80" % len(item["title"]))
    (category_id, category), others = _category(out["category_query"])
    item["category_id"], item["category"] = category_id, category
    if others:
        item["category_alternatives"] = [{"id": i, "path": p} for i, p in others]
    if category_id:
        missing = [a for a in _required_aspects(category_id) if a not in item["aspects"]]
        if missing:
            item["needs_check"].append("eBay requires: %s" % ", ".join(missing))
    else:
        item["needs_check"].append("No eBay category found for %r" % out["category_query"])
    item["drafted_by"] = run
    return item, run
