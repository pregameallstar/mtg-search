"""Self-hosted MTG card search — Scryfall replica.

ponytail: single-file Flask app, SQLite, no ORM, no JS, no build step.
ponytail: global lock via SQLite single-writer (we're read-only, irrelevant).
"""

import os
import json
import sqlite3
import re
import uuid
import threading
from datetime import datetime, timezone
from collections import OrderedDict
from urllib.parse import quote
from flask import Flask, g, render_template, request, abort, redirect, url_for, make_response, session, jsonify, send_file

from shared import db_path, color_identity_subset, resolve_bind_path

# ponytail: optional imports — only needed for commander eval / db ingest
try:
    from llm import generate as llm_generate, fetch_models
except ImportError:
    llm_generate = None
import embed
from prompts import COMMANDER_SYSTEM_PROMPT, DEEPDIVE_SYSTEM_PROMPT, VERIFY_SYSTEM_PROMPT
from websearch import web_search, SEARXNG_URL
from mcp_control import MCP_SSE_PORT, MCPO_PORT, MCP_HOST, MCP_DISPLAY_HOST, port_alive, restart_mcp
from images import card_image_url, mana_symbols, fetch_scryfall_image
from similarity import (
    similarity_label, get_idf, extract_terms, split_csv, gate, score_similarity,
    wubrg_sort, normalize_dash,
)
from eval_helpers import (
    is_placeholder_name, get_commander_filter_sets,
    eval_similar_embed, eval_similar_legacy,
    auto_save_deepdives, load_auto_deepdives,
)

app = Flask(__name__)

# ponytail: persist secret key so sessions survive restarts
_secret_key = os.environ.get("SECRET_KEY")
if not _secret_key:
    _key_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".secret_key")
    # ponytail: Docker creates a directory when the bind-mount source file
    # doesn't exist on the host. resolve_bind_path handles this transparently.
    _key_path = resolve_bind_path(_key_path)
    try:
        with open(_key_path, "rb") as f:
            _secret_key = f.read()
    except (FileNotFoundError, IsADirectoryError):
        _secret_key = os.urandom(24)
        _wrote = False
        try:
            with open(_key_path, "wb") as f:
                f.write(_secret_key)
            _wrote = True
        except (OSError, IsADirectoryError):
            pass
        if not _wrote:
            import warnings
            warnings.warn("Could not persist secret key — sessions will not survive restarts.")
app.secret_key = _secret_key

# ponytail: keep session cookie alive for 30 days so config survives browser restarts.
# Flask default is None (browser-session cookie — gone when tab closes).
app.config["PERMANENT_SESSION_LIFETIME"] = 30 * 24 * 3600  # 30 days

@app.before_request
def _mark_session_permanent():
    session.permanent = True

@app.after_request
def _add_security_headers(response):
    # Allow iframe embedding for the deck builder inline eval tool
    if request.args.get("embed") == "1":
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
    else:
        response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' https: data:; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; script-src 'self' 'unsafe-inline'; font-src 'self' https://cdn.jsdelivr.net; frame-ancestors 'self'"
    return response

# ponytail: server-side analysis cache — Flask cookie sessions cap at ~4KB,
# expanded bracket+kill-on-sight output exceeds that.
# ponytail: FIFO-capped at 100 entries, add LRU if eviction order matters.
# ponytail: _cache_lock guards mutations; dev server is single-thread but any
# threaded WSGI deployment needs it.  Reads (get) are atomic dict lookups, ok unlocked.
_eval_cache = OrderedDict()  # {id: {"analysis": {...}, "error": "..."}}
_cache_lock = threading.Lock()

# ponytail: progress entries are tiny (~3 fields) and transient — capped at 50 to
# prevent unbounded growth from abandoned/refreshed eval pages.
_progress_cache = OrderedDict()  # {id: {"step": N, "total": 6, "label": "..."}}

def _cache_put(key, entry):
    """Store in eval cache, evicting oldest if over cap."""
    with _cache_lock:
        _eval_cache[key] = entry
        while len(_eval_cache) > 100:
            _eval_cache.popitem(last=False)


def _cache_update(key, **kwargs):
    """Update sub-keys of an existing cache entry, lock-guarded.

    Call _cache_update_locked(key, ...) when you already hold _cache_lock."""
    with _cache_lock:
        _cache_update_locked(key, **kwargs)


def _cache_update_locked(key, **kwargs):
    """Update sub-keys of an existing cache entry. Caller must hold _cache_lock."""
    entry = _eval_cache.get(key)
    if entry is not None:
        entry.update(kwargs)

DATABASE = "AllPrintings.sqlite"
REPORTS_DIR = "eval_reports"
IMAGE_DIR = "images"
DECKS_DIR = "decks"
os.makedirs(DECKS_DIR, exist_ok=True)


# --- DB helpers ---


# ponytail: _db_path() lives in shared.db_path — imported above.
# Call as db_path(DATABASE) to resolve the actual database path.


def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(db_path(DATABASE))
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA query_only = ON")
    return g.db


def _db_ready():
    """Return True if the cards table exists with at least one row."""
    try:
        db = get_db()
        row = db.execute("SELECT COUNT(*) FROM cards WHERE language='English' LIMIT 1").fetchone()
        return row is not None and row[0] > 0
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        return False

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()

# --- Image URL ---

# ponytail: alias for backward compat — Jinja globals and deck routes use this name
card_image = card_image_url


@app.route("/img/<size>/<face>/<c1>/<c2>/<scryfall_id>.jpg")
def serve_image(size, face, c1, c2, scryfall_id):
    """Lazy cache-through proxy. First hit fetches from Scryfall and writes to disk."""
    image_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), IMAGE_DIR,
    )
    full_path = fetch_scryfall_image(image_dir, scryfall_id, size, face)
    if full_path:
        return send_file(full_path)
    app.logger.warning(
        "serve_image failed after 3 retries: %s/%s/%s/%s/%s.jpg",
        size, face, scryfall_id[0], scryfall_id[1], scryfall_id,
    )
    abort(404)


# --- Mana symbol helpers ---

def render_mana(cost):
    """Render mana cost to HTML with mana symbol classes."""
    return mana_symbols(cost)

def pagination_url(args, page):
    """Build URL with existing query params, replacing page."""
    parts = []
    for k, vals in args.lists():
        if k == "page":
            continue
        for v in vals:
            parts.append(f"{quote(k)}={quote(v)}")
    parts.append(f"page={page}")
    # ponytail: preserve per_page so pagination links don't reset it to default
    if "per_page" not in args:
        parts.append("per_page=30")
    return "/search?" + "&".join(parts) if parts else "/search"

app.jinja_env.globals.update(card_image=card_image, render_mana=render_mana, pagination_url=pagination_url, similarity_label=similarity_label)


@app.context_processor
def _inject_db_state():
    """ponytail: db_unseeded=False by default. Routes that detect a missing DB set it True."""
    return {"db_unseeded": False}

# --- Routes ---

@app.route("/")
def index():
    # Quick counts for homepage
    if not _db_ready():
        return render_template("index.html", total_cards=0, db_unseeded=True)
    db = get_db()
    total = db.execute(
        "SELECT COUNT(DISTINCT name) FROM cards WHERE language='English' AND (side IS NULL OR side='a')"
    ).fetchone()[0]
    return render_template("index.html", total_cards=total)

# noqa: WUBRG order moved to similarity.py, normalize_dash moved to similarity.py

def _n(op, field, cast=False):
    """Return SQL clause for a numeric operator + field. Handles =, <, >, <=, >=.
    If cast=True, the field is text — use CAST AS REAL for comparison, which
    handles integer and fractional values. '*' and non-numeric values cast to NULL,
    which fails any comparison → excluded."""
    ops = {"=": "=", "<": "<", ">": ">", "<=": "<=", ">=": ">="}
    if op in ops:
        col = f"CAST(c.{field} AS REAL)" if cast else f"c.{field}"
        return f"{col} {ops[op]} ?"

@app.route("/search")
def search():
    q = request.args.get("q", "").strip()                      # free-text name/type/text
    name = request.args.get("name", "").strip()                 # card name search (autocomplete-backed)
    oracle = request.args.get("oracle", "").strip()             # oracle text search
    type_line = request.args.get("type_line", "").strip()       # full type line
    mana_cost = request.args.get("mana_cost", "").strip()       # exact mana cost e.g. "{1}{R}"
    keywords = request.args.get("keywords", "").strip()         # comma-separated keywords, AND logic
    # Colors
    color = request.args.get("color", "").strip()               # WUBRGC string
    color_rule = request.args.get("color_rule", "at_least")     # exact / at_most / at_least
    ci = request.args.get("ci", "").strip()                     # color identity
    ci_rule = request.args.get("ci_rule", "at_least")           # exact / at_most / at_least
    # Stats
    mv = request.args.get("mv", "").strip()
    mv_op = request.args.get("mv_op", "=")
    pow_val = request.args.get("pow", "").strip()
    pow_op = request.args.get("pow_op", "=")
    tou_val = request.args.get("tou", "").strip()
    tou_op = request.args.get("tou_op", "=")
    loy_val = request.args.get("loy", "").strip()
    loy_op = request.args.get("loy_op", "=")
    # Rarity (multi-select)
    rarities = request.args.getlist("rarity")                   # e.g. ["rare","mythic"]
    # Legality
    fmt = request.args.get("format", "").strip()
    legality = request.args.get("legality", "legal")            # legal / banned / restricted
    # Set
    set_code = request.args.get("set", "").strip()
    # Booleans / Flags
    is_reprint = request.args.get("is_reprint")
    is_reserved = request.args.get("is_reserved")
    is_funny = request.args.get("is_funny")
    is_oversized = request.args.get("is_oversized")
    is_fullart = request.args.get("is_fullart")
    is_textless = request.args.get("is_textless")
    is_promo = request.args.get("is_promo")
    is_rebalanced = request.args.get("is_rebalanced")
    border = request.args.get("border", "").strip()             # black / white / silver / borderless
    layout = request.args.get("layout", "").strip()
    frame = request.args.get("frame", "").strip()               # frameVersion
    unique = request.args.get("unique", "cards")                # cards (dedupe) / prints (all)

    page = request.args.get("page", 1, type=int)
    if page < 1:
        page = 1
    per_page_raw = request.args.get("per_page", "30")
    per_page = int(per_page_raw) if per_page_raw.isdigit() and int(per_page_raw) in (10, 25, 50) else 30

    if not _db_ready():
        return render_template(
            "search.html",
            query=q, name=name, oracle=oracle, type_line=type_line, mana_cost=mana_cost, keywords=keywords,
            color=color, color_rule=color_rule, ci=ci, ci_rule=ci_rule,
            mv=mv, mv_op=mv_op, pow_val=pow_val, pow_op=pow_op,
            tou_val=tou_val, tou_op=tou_op, loy_val=loy_val, loy_op=loy_op,
            rarities=rarities, fmt=fmt, legality=legality, set_code=set_code,
            is_reprint=is_reprint, is_reserved=is_reserved, is_funny=is_funny,
            is_oversized=is_oversized, is_fullart=is_fullart, is_textless=is_textless,
            is_promo=is_promo, is_rebalanced=is_rebalanced,
            border=border, layout=layout, frame=frame, unique=unique,
            results=[], page=page, total=0, total_pages=0, per_page=per_page,
            db_unseeded=True, has_filters=False,
        )

    db = get_db()

    where = ["c.language = 'English'", "(c.side IS NULL OR c.side = 'a')"]
    params = []

    # Free-text: search name + type + oracle text (comma-separated, AND logic)
    if q:
        for term in q.split(","):
            term = term.strip().strip("\"'")
            if term:
                escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                where.append(
                    "(c.name LIKE ? ESCAPE '\\' OR c.type LIKE ? ESCAPE '\\' "
                    "OR c.text LIKE ? ESCAPE '\\')"
                )
                params.extend([f"%{escaped}%"] * 3)

    # Name search (autocomplete-backed, exact substring match)
    # ponytail: match only front-face name so DFCs with different back-face
    # names don't pollute results.  "Emeritus of Conflict // Lightning Bolt"
    # should not match a search for "Lightning Bolt".
    if name:
        escaped = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        front_only = "CASE WHEN instr(c.name, ' // ') > 0 THEN substr(c.name, 1, instr(c.name, ' // ') - 1) ELSE c.name END"
        where.append(f"{front_only} LIKE ? ESCAPE '\\'")
        params.append(f"%{escaped}%")

    # Oracle text only (comma-separated, AND logic)
    if oracle:
        for term in oracle.split(","):
            term = term.strip().strip("\"'")
            if term:
                escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                where.append("c.text LIKE ? ESCAPE '\\'")
                params.append(f"%{escaped}%")

    # Full type line
    if type_line:
        # ponytail: normalize dashes — DB stores U+2014 em-dash but users type '-'.
        # REPLACE on the DB column side so we don't fight LIKE escaping.
        normalized = normalize_dash(type_line)
        escaped = normalized.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("REPLACE(c.type, '—', '-') LIKE ? ESCAPE '\\'")
        params.append(f"%{escaped}%")

    # Exact mana cost
    if mana_cost:
        where.append("c.manaCost = ?")
        params.append(mana_cost)

    # Keywords (comma-separated, AND logic)
    if keywords:
        for kw in keywords.split(","):
            kw = kw.strip()
            if kw:
                where.append("c.keywords LIKE ?")
                params.append(f"%{kw}%")

    # Colors (WUBRGC)
    if color:
        color_chars = [ch for ch in color.upper() if ch in "WUBRGC"]
        has_c = "C" in color_chars
        color_chars = [ch for ch in color_chars if ch != "C"]
        if has_c and color_rule != "at_most":
            if not color_chars:
                # Only C selected: colorless cards have colors = ''
                where.append("c.colors = ''")
            elif color_rule == "exact":
                # C + other colors: contradiction (can't be colorless AND colored)
                where.append("1 = 0")
            # at_least with C + others: suppress C, match remaining colors only
        if color_chars:
            if color_rule == "exact":
                where.append("c.colors = ?")
                params.append(", ".join(wubrg_sort(color_chars)))
                where.append(f"LENGTH(c.colors) - LENGTH(REPLACE(c.colors, ',', '')) + (CASE WHEN c.colors = '' THEN 0 ELSE 1 END) = ?")
                params.append(len(color_chars))
            elif color_rule == "at_most":
                # Subset: card colors ⊆ selected colors. Exclude cards containing any non-selected color.
                for ch in "WUBRG":
                    if ch not in color_chars:
                        where.append("c.colors NOT LIKE ?")
                        params.append(f"%{ch}%")
            else:  # at_least
                for ch in color_chars:
                    where.append("c.colors LIKE ?")
                    params.append(f"%{ch}%")

    # Color identity
    if ci:
        ci_chars = [ch for ch in ci.upper() if ch in "WUBRGC"]
        has_c = "C" in ci_chars
        ci_chars = [ch for ch in ci_chars if ch != "C"]
        if has_c and ci_rule != "at_most":
            if not ci_chars:
                # Only C selected: colorless identity cards have colorIdentity = ''
                where.append("c.colorIdentity = ''")
            elif ci_rule == "exact":
                # C + other colors: contradiction
                where.append("1 = 0")
        if ci_chars:
            if ci_rule == "exact":
                # ponytail: colorIdentity is stored alphabetically in MTGJSON, unlike colors
                # which is in WUBRG order.
                where.append("c.colorIdentity = ?")
                params.append(", ".join(sorted(ci_chars)))
            elif ci_rule == "at_most":
                # Subset: color identity ⊆ selected colors
                for ch in "WUBRG":
                    if ch not in ci_chars:
                        where.append("c.colorIdentity NOT LIKE ?")
                        params.append(f"%{ch}%")
            else:  # at_least
                for ch in ci_chars:
                    where.append("c.colorIdentity LIKE ?")
                    params.append(f"%{ch}%")

    # Mana value
    if mv:
        try:
            if mv_op in ("<", ">", "<=", ">="):
                val = float(mv)
                where.append(f"c.manaValue {mv_op} ?")
                params.append(val)
            elif re.match(r"^\s*\d+(\.\d+)?\s*-\s*\d+(\.\d+)?\s*$", mv):
                lo, hi = mv.split("-", 1)
                lo_val, hi_val = float(lo.strip()), float(hi.strip())
                if lo_val > hi_val:
                    lo_val, hi_val = hi_val, lo_val  # ponytail: auto-swap reversed ranges
                where.append("c.manaValue BETWEEN ? AND ?")
                params.extend([lo_val, hi_val])
            elif mv_op == "=":
                val = float(mv)
                where.append("c.manaValue = ?")
                params.append(val)
            else:
                # ponytail: unrecognized mv format → no valid filter, show nothing
                import sys
                print(f"search: invalid mv format '{mv}', showing empty results", file=sys.stderr)
                where.append("1 = 0")
        except (ValueError, TypeError):
            # ponytail: garbage input → no valid filter, show nothing
            import sys
            print(f"search: unparseable mv '{mv}', showing empty results", file=sys.stderr)
            where.append("1 = 0")

    # Power (text column, needs CAST for comparisons)
    if pow_val:
        if pow_val == '*':
            # ponytail: literal '*' — match cards with variable power
            where.append("c.power = '*'")
        else:
            clause = _n(pow_op, "power", cast=True)
            if clause:
                where.append(clause)
                params.append(pow_val)

    # Toughness
    if tou_val:
        if tou_val == '*':
            where.append("c.toughness = '*'")
        else:
            clause = _n(tou_op, "toughness", cast=True)
            if clause:
                where.append(clause)
                params.append(tou_val)

    # Loyalty
    if loy_val:
        clause = _n(loy_op, "loyalty", cast=True)
        if clause:
            where.append(clause)
            params.append(loy_val)

    # Rarity (multi-select)
    if rarities:
        placeholders = ",".join("?" * len(rarities))
        where.append(f"LOWER(c.rarity) IN ({placeholders})")
        params.extend([r.lower() for r in rarities])

    # Set
    if set_code:
        where.append("c.setCode = ?")
        params.append(set_code.upper())

    # Format legality
    join_clause = ""
    if fmt:
        valid_formats = {"standard","commander","modern","legacy","pioneer","vintage",
                         "pauper","brawl","historic","oathbreaker","penny","duel",
                         "alchemy","gladiator","oldschool","premodern","predh",
                         "paupercommander","timeless","standardbrawl","competitivebrawl"}
        fmt_lower = fmt.lower()
        if fmt_lower in valid_formats:
            join_clause = "JOIN cardLegalities cl ON c.uuid = cl.uuid"
            if legality == "banned":
                where.append(f"cl.{fmt_lower} = 'Banned'")
            elif legality == "restricted":
                where.append(f"cl.{fmt_lower} = 'Restricted'")
            else:
                where.append(f"cl.{fmt_lower} = 'Legal'")

    # Boolean flags
    if is_reprint:
        where.append("c.isReprint = 1")
    if is_reserved:
        where.append("c.isReserved = 1")
    if is_funny:
        where.append("c.isFunny = 1")
    if is_oversized:
        where.append("c.isOversized = 1")
    if is_fullart:
        where.append("c.isFullArt = 1")
    if is_textless:
        where.append("c.isTextless = 1")
    if is_promo:
        where.append("c.isPromo = 1")
    if is_rebalanced:
        where.append("c.isRebalanced = 1")

    # Border
    if border:
        border_map = {"black": "black", "white": "white", "silver": "silver", "borderless": "borderless",
                       "gold": "gold", "yellow": "yellow"}
        if border in border_map:
            where.append("c.borderColor = ?")
            params.append(border_map[border])

    # Layout
    if layout:
        where.append("c.layout = ?")
        params.append(layout)

    # Frame version
    if frame:
        where.append("c.frameVersion = ?")
        params.append(frame)

    where_clause = "WHERE " + " AND ".join(where)

    # Count
    # ponytail: strip DFC suffix " // X" from names for dedup — "Sol Ring // Sol Ring"
    # is the same card as "Sol Ring".
    group_col = "c.name" if unique == "cards" else "c.uuid"
    group_expr = f"CASE WHEN instr({group_col}, ' // ') > 0 THEN substr({group_col}, 1, instr({group_col}, ' // ') - 1) ELSE {group_col} END"
    count_sql = f"""
        SELECT COUNT(DISTINCT {group_expr})
        FROM cards c
        {join_clause}
        {where_clause}
    """
    total = db.execute(count_sql, params).fetchone()[0]

    # Fetch
    name_expr = "CASE WHEN instr(c.name, ' // ') > 0 THEN substr(c.name, 1, instr(c.name, ' // ') - 1) ELSE c.name END"
    data_sql = f"""
        SELECT {name_expr} as name, c.manaCost, c.type, c.rarity, c.setCode, c.colors,
               ci.scryfallId, s.name as setName, s.releaseDate, c.number
        FROM cards c
        JOIN cardIdentifiers ci ON c.uuid = ci.uuid
        JOIN sets s ON c.setCode = s.code
        {join_clause}
        {where_clause}
        GROUP BY {group_expr}
        ORDER BY c.name
        LIMIT ? OFFSET ?
    """
    results = db.execute(data_sql, params + [per_page, (page - 1) * per_page]).fetchall()

    total_pages = max(1, (total + per_page - 1) // per_page)

    # No filters provided — show empty state, don't dump all cards
    has_filters = any([
        q, name, oracle, type_line, mana_cost, keywords, color, ci, mv, pow_val, tou_val,
        loy_val, rarities, fmt, set_code, is_reprint, is_reserved, is_funny,
        is_oversized, is_fullart, is_textless, is_promo, is_rebalanced,
        border, layout, frame,
    ])
    if not has_filters:
        results = []
        total = 0
        total_pages = 0


    return render_template(
        "search.html",
        query=q, name=name, oracle=oracle, type_line=type_line, mana_cost=mana_cost, keywords=keywords,
        color=color, color_rule=color_rule, ci=ci, ci_rule=ci_rule,
        mv=mv, mv_op=mv_op, pow_val=pow_val, pow_op=pow_op,
        tou_val=tou_val, tou_op=tou_op, loy_val=loy_val, loy_op=loy_op,
        rarities=rarities, fmt=fmt, legality=legality, set_code=set_code,
        is_reprint=is_reprint, is_reserved=is_reserved, is_funny=is_funny,
        is_oversized=is_oversized, is_fullart=is_fullart, is_textless=is_textless,
        is_promo=is_promo, is_rebalanced=is_rebalanced,
        border=border, layout=layout, frame=frame, unique=unique,
        results=results, page=page, total=total, total_pages=total_pages, per_page=per_page,
        has_filters=has_filters,
    )

@app.route("/card/<set_code>/<number>")
def card_detail(set_code, number):
    db = get_db()

    # Get the primary card (English, side 'a' or single-faced)
    card = db.execute("""
        SELECT c.*, ci.scryfallId, s.name as setName, s.releaseDate
        FROM cards c
        JOIN cardIdentifiers ci ON c.uuid = ci.uuid
        JOIN sets s ON c.setCode = s.code
        WHERE c.setCode = ? AND c.number = ? AND c.language = 'English'
          AND (c.side IS NULL OR c.side = 'a')
    """, [set_code, number]).fetchone()

    if not card:
        abort(404)

    # Rulings
    rulings = db.execute("""
        SELECT date, text FROM cardRulings WHERE uuid = ? ORDER BY date DESC
    """, [card["uuid"]]).fetchall()

    # Legalities
    legalities = db.execute("""
        SELECT * FROM cardLegalities WHERE uuid = ?
    """, [card["uuid"]]).fetchone()

    # All printings (from printings field — comma-separated set codes)
    printing_codes = card["printings"].split(",") if card["printings"] else []
    printings = []
    if printing_codes:
        placeholders = ",".join("?" * len(printing_codes))
        printings = db.execute(
            f"""SELECT code, name, releaseDate FROM sets
                WHERE code IN ({placeholders})
                ORDER BY releaseDate""",
            printing_codes,
        ).fetchall()

    # Other faces (for multi-faced cards)
    other_faces = []
    if card["otherFaceIds"]:
        other_uuids = [u.strip() for u in card["otherFaceIds"].split(",") if u.strip()]
        if other_uuids:
            placeholders = ",".join("?" * len(other_uuids))
            other_faces = db.execute(
                f"""SELECT c.*, ci.scryfallId
                    FROM cards c
                    JOIN cardIdentifiers ci ON c.uuid = ci.uuid
                    WHERE c.uuid IN ({placeholders})
                    ORDER BY c.side""",
                other_uuids,
            ).fetchall()

    template = "card_panel.html" if request.args.get("fragment") else "card.html"
    return render_template(
        template,
        card=card, rulings=rulings, legalities=legalities,
        printings=printings, other_faces=other_faces,
    )


@app.route("/similar", methods=["GET", "POST"])
@app.route("/cards/<set_code>/similar", methods=["GET", "POST"])
def similar_landing(set_code=None):
    """Landing page for the similarity tool — search by card name or drop image."""
    db = get_db()

    # --- POST: drag-and-drop image lookup ---
    if request.method == "POST":
        scryfall_id = (request.form.get("scryfall_id") or "").strip()
        if not scryfall_id:
            return jsonify({"error": "No card image data received."}), 400
        found = db.execute("""
            SELECT c.setCode, c.number FROM cards c
            JOIN cardIdentifiers ci ON c.uuid = ci.uuid
            WHERE ci.scryfallId = ? AND c.language = 'English'
              AND (c.side IS NULL OR c.side = 'a')
            LIMIT 1
        """, [scryfall_id]).fetchone()
        if found:
            return jsonify({
                "redirect": url_for('similar_cards',
                                    set_code=found['setCode'],
                                    number=found['number'])
            })
        return jsonify({"error": "Card not found. Try searching by name."}), 404

    # --- GET: name search ---
    by_name = request.args.get("by_name", "").strip()
    if by_name:
        found = db.execute("""
            SELECT c.setCode, c.number FROM cards c
            JOIN sets s ON c.setCode = s.code
            WHERE c.name = ? AND c.language = 'English' AND (c.side IS NULL OR c.side = 'a')
            ORDER BY s.releaseDate DESC LIMIT 1
        """, [by_name]).fetchone()
        if found:
            keep = {}
            for k in ("use_types", "use_keywords", "use_subtypes", "use_supertypes",
                      "use_mv", "use_color",
                      "s_types", "s_keywords", "s_subtypes", "s_supertypes",
                      "s_mv", "s_color", "s_oracle",
                      "w_types", "w_keywords", "w_subtypes", "w_supertypes",
                      "w_mv", "w_color", "use_weights",
                      "method",
                      "mtg_filter", "top_n", "tuned", "per_page"):
                v = request.args.get(k)
                if v:
                    keep[k] = v
            return redirect(url_for('similar_cards',
                                    set_code=found['setCode'],
                                    number=found['number'],
                                    **keep))

    return render_template("similar_landing.html",
                           set_code=set_code,
                           by_name=by_name,
                           by_name_error=bool(by_name))

@app.route("/card/<set_code>/<number>/similar")
def similar_cards(set_code, number):
    db = get_db()

    # --- First visit: default all factors ON with strictness=strict ---
    is_tuned = request.args.get("tuned") == "1"
    use_weights = request.args.get("use_weights") == "1"

    if not is_tuned:
        # Default every factor enabled with strictness=strict (3.0)
        # ponytail: URL params are the source of truth after first submit; no session state.
        default_on = ("use_types", "use_keywords", "use_subtypes", "use_supertypes",
                      "use_mv", "use_color")
        default_strict = ("s_types", "s_keywords", "s_subtypes", "s_supertypes",
                          "s_mv", "s_color", "s_oracle")
        args_dict = dict(request.args)
        args_dict["tuned"] = "1"
        for k in default_on:
            args_dict[k] = "1"
        for k in default_strict:
            args_dict[k] = "strict"
        args_dict["mtg_filter"] = "1"
        return redirect(url_for('similar_cards', set_code=set_code, number=number, **args_dict))

    # --- Parse factor toggles, weights, and strictness ---
    # ponytail: strictness values calibrated so strict/loose produce meaningful
    # spreads across real cards. Gates use the formula 1/(1+strictness*mismatch);
    # oracle uses exponentiation (overlap ** s_oracle).
    STRICT_VALUES = {"strict": 2.0, "moderate": 0.5, "loose": 0.15}
    COLOR_STRICT = {"strict": 200.0, "moderate": 2.0, "loose": 0.5}
    ORACLE_STRICT = {"strict": 1.0, "moderate": 0.5, "loose": 0.3}

    factors = {}
    for key in ("use_types", "use_keywords", "use_subtypes", "use_supertypes",
                "use_mv", "use_color"):
        if request.args.get(key) == "1":
            factors[key] = True

    # Strictness: map label → float, default "strict" (3.0)
    for key in ("s_types", "s_keywords", "s_subtypes", "s_supertypes",
                "s_mv"):
        label = request.args.get(key, "strict")
        factors[key] = STRICT_VALUES.get(label, 3.0)
    # Color identity: separate tuning, lower ceiling for moderate/loose
    factors["s_color"] = COLOR_STRICT.get(
        request.args.get("s_color", "strict"), 200.0)

    # Oracle strictness: uses exponent, not gate formula
    factors["s_oracle"] = ORACLE_STRICT.get(
        request.args.get("s_oracle", "strict"), 5.0
    )

    # Weights: only parse if advanced weights are toggled on
    for key in ("w_types", "w_keywords", "w_subtypes", "w_supertypes",
                "w_mv", "w_color"):
        if use_weights and request.args.get(key):
            try:
                factors[key] = float(request.args.get(key))
            except ValueError:
                pass  # ignore bad float values
        # ponytail: when weights hidden, defaults to 1.0 (already in score_similarity)

    mtg_filter = request.args.get("mtg_filter") == "1"

    top_n = request.args.get("top_n", "5")
    if top_n not in ("5", "10", "15", "20"):
        top_n = "5"
    top_n = int(top_n)

    # --- Name-based lookup ---
    by_name = request.args.get("by_name", "").strip()
    if by_name:
        found = db.execute("""
            SELECT c.setCode, c.number FROM cards c
            JOIN sets s ON c.setCode = s.code
            WHERE c.name = ? AND c.language = 'English' AND (c.side IS NULL OR c.side = 'a')
            ORDER BY s.releaseDate DESC LIMIT 1
        """, [by_name]).fetchone()
        if found:
            # Preserve all factor params in the redirect
            keep = {}
            for k in ("method",
                      "use_types", "use_keywords", "use_subtypes", "use_supertypes",
                      "use_mv", "use_color",
                      "s_types", "s_keywords", "s_subtypes", "s_supertypes",
                      "s_mv", "s_color", "s_oracle",
                      "w_types", "w_keywords", "w_subtypes", "w_supertypes",
                      "w_mv", "w_color", "use_weights",
                      "mtg_filter", "top_n", "page", "tuned", "per_page"):
                v = request.args.get(k)
                if v:
                    keep[k] = v
            return redirect(url_for('similar_cards',
                                    set_code=found['setCode'],
                                    number=found['number'],
                                    **keep))

    # --- Load base card ---
    base_card = db.execute("""
        SELECT c.*, ci.scryfallId, s.name as setName, s.releaseDate
        FROM cards c
        JOIN cardIdentifiers ci ON c.uuid = ci.uuid
        JOIN sets s ON c.setCode = s.code
        WHERE c.setCode = ? AND c.number = ? AND c.language = 'English'
          AND (c.side IS NULL OR c.side = 'a')
    """, [set_code, number]).fetchone()

    if not base_card:
        abort(404)

    # Pre-compute base card fields (used in every scoring call)
    base_card = dict(base_card)
    base_card["_terms"] = extract_terms(base_card["text"], mtg_filter=mtg_filter)
    base_card["_t"] = split_csv(base_card["types"])
    base_card["_kw"] = split_csv(base_card["keywords"])
    base_card["_sub"] = split_csv(base_card["subtypes"])
    base_card["_st"] = split_csv(base_card["supertypes"])
    base_card["_ci"] = split_csv(base_card["colorIdentity"])
    # ponytail: pre-compute IDF sum once, not per-candidate.
    idf = get_idf(db_path(DATABASE))
    base_card["_idf_sum"] = sum(idf.get(t, 0) for t in base_card["_terms"])

    # --- Method: embed (semantic) or legacy (TF-IDF with tuning) ---
    # ponytail: read from session (set in config page), fall back to query param, then legacy.
    method = request.args.get("method") or session.get("similar_method", "legacy")
    score_method = method  # ponytail: may fall back to legacy for scoring

    # --- Candidate query ---
    mv = base_card["manaValue"] or 0
    mv_min = max(0, mv - 3)
    mv_max = mv + 3

    scored = []

    if score_method == "embed":
        # ponytail: semantic similarity via embedding index.
        # Factor toggles are applied as hard post-filters (not scoring gates).
        try:
            candidates = embed.find(db_path(DATABASE), base_card.get("text") or "", top_k=200)
            candidates = [c for c in candidates if c["name"] != base_card["name"]]
            for c in candidates:
                c_mv = c.get("manaValue")
                if c_mv is not None and (c_mv < mv_min or c_mv > mv_max):
                    continue
                # --- Factor-based post-filters ---
                if factors.get("use_color"):
                    cand_ci = (c.get("colorIdentity") or "").strip()
                    # ponytail: use subset matching instead of exact — so a
                    # mono-white card also matches colorless artifacts that
                    # are mechanically similar.  This mirrors the legacy
                    # similarity's graduated penalty rather than a hard gate.
                    if not color_identity_subset(cand_ci, base_card["_ci"]):
                        continue
                if factors.get("use_types"):
                    cand_t = split_csv(c.get("types") or "")
                    base_t = base_card.get("_t")
                    if base_t and not (base_t & cand_t):
                        continue
                # Look up setCode/number for URL
                row = db.execute(
                    "SELECT c.setCode, c.number, c.manaCost, c.manaValue, c.type, c.types, c.supertypes, "
                    "c.subtypes, c.keywords, c.colors, c.colorIdentity, c.rarity, c.text, "
                    "ci.scryfallId, s.name as setName, s.releaseDate "
                    "FROM cards c "
                    "JOIN cardIdentifiers ci ON c.uuid = ci.uuid "
                    "JOIN sets s ON c.setCode = s.code "
                    "WHERE c.uuid = ? AND c.language = 'English'",
                    (c["uuid"],)
                ).fetchone()
                if row:
                    scored.append((c["score"], row))
            scored.sort(key=lambda x: x[0], reverse=True)

            # Convert scores to 0-100 scale (cosine similarity is already 0-1)
            scored = [(round(s * 100, 1), c) for s, c in scored]
        except Exception:
            import sys, traceback
            traceback.print_exc(file=sys.stderr)
            print("similar: embed failed, falling back to legacy", file=sys.stderr)
            score_method = "legacy"  # ponytail: fall back to legacy scoring only

    if score_method == "legacy":
        candidates = db.execute("""
            SELECT c.name, c.manaCost, c.manaValue, c.type, c.types, c.supertypes,
                   c.subtypes, c.keywords, c.text, c.colors, c.colorIdentity,
                   c.setCode, c.number, c.rarity,
                   ci.scryfallId, s.name as setName, s.releaseDate
            FROM cards c
            JOIN cardIdentifiers ci ON c.uuid = ci.uuid
            JOIN sets s ON c.setCode = s.code
            JOIN (
                SELECT c2.name, MAX(s2.releaseDate) as maxDate
                FROM cards c2
                JOIN sets s2 ON c2.setCode = s2.code
                WHERE c2.language = 'English'
                  AND (c2.side IS NULL OR c2.side = 'a')
                GROUP BY c2.name
            ) latest ON c.name = latest.name AND s.releaseDate = latest.maxDate
            WHERE c.language = 'English'
              AND (c.side IS NULL OR c.side = 'a')
              AND c.uuid != ?
              AND c.name != ?
              AND (c.manaValue BETWEEN ? AND ? OR c.manaValue IS NULL)
            GROUP BY c.name
        """, [base_card["uuid"], base_card["name"], mv_min, mv_max]).fetchall()

        # --- Score and sort ---
        scored = []
        for c in candidates:
            scored.append((score_similarity(base_card, c, factors, idf, mtg_filter=mtg_filter), c))
        scored.sort(key=lambda x: x[0], reverse=True)

        # Convert to 0-100 scale
        scored = [(round(s * 100, 1), c) for s, c in scored]

    # --- Split top N from remaining ---
    best_score = scored[0][0] if scored else 0
    top_results = scored[:top_n]
    remaining = scored[top_n:]

    # --- Export (self-contained HTML download) ---
    if request.args.get("export") == "1":
        rendered = render_template(
            "similar_export.html",
            card=base_card,
            top_results=top_results,
            top_n=top_n,
            best_score=best_score,
        )
        filename = f"similar-{base_card['name'].replace(' ', '-')}-top{top_n}.html"
        response = make_response(rendered)
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    # --- Paginate remaining ---
    page = request.args.get("page", 1, type=int)
    per_page_raw = request.args.get("per_page", "30")
    per_page = int(per_page_raw) if per_page_raw.isdigit() and int(per_page_raw) in (10, 25, 50) else 30
    total = len(remaining)
    total_pages = max(1, (total + per_page - 1) // per_page)
    start = (page - 1) * per_page
    page_results = remaining[start:start + per_page]

    # Build query string for pagination links (preserves tuning state)
    tuning_params = ""
    for k in ("use_types", "use_keywords", "use_subtypes", "use_supertypes",
              "use_mv", "use_color",
              "s_types", "s_keywords", "s_subtypes", "s_supertypes",
              "s_mv", "s_color", "s_oracle",
              "w_types", "w_keywords", "w_subtypes", "w_supertypes",
              "w_mv", "w_color", "use_weights",
              "mtg_filter", "top_n", "tuned", "per_page"):
        v = request.args.get(k)
        if v:
            tuning_params += f"&{quote(k)}={quote(v)}"

    return render_template(
        "similar.html",
        card=base_card,
        top_results=top_results,
        results=page_results,
        page=page,
        total=total,
        total_pages=total_pages,
        per_page=per_page,
        factors=factors,
        use_weights=use_weights,
        tuned=True,
        by_name=by_name,
        by_name_error=bool(by_name),
        mtg_filter=mtg_filter,
        tuning_params=tuning_params,
        top_n=top_n,
        method=method,
        best_score=best_score,
    )



# --- Commander Eval ---

# ponytail: color_identity_subset is provided by shared (imported above)



@app.route("/config", methods=["GET", "POST"])
def config_page():
    """LLM configuration — shared across tools that use the LLM."""
    if request.method == "POST":
        api_key = request.form.get("llm_api_key", "").strip()
        if api_key:
            session["llm_api_key"] = api_key
        elif api_key == "" and "llm_api_key" in session:
            session.pop("llm_api_key", None)
        session["llm_backend"] = request.form.get("llm_backend", "").strip()
        session["llm_base_url"] = request.form.get("llm_base_url", "").strip()
        session["llm_model"] = request.form.get("llm_model", "").strip()
        if request.form.get("save_prompt") == "1":
            eval_prompt = request.form.get("eval_prompt", "").strip()
            if eval_prompt:
                session["eval_prompt"] = eval_prompt
            elif "eval_prompt" in session:
                session.pop("eval_prompt", None)
        if request.form.get("save_deepdive_prompt") == "1":
            deepdive_prompt = request.form.get("deepdive_prompt", "").strip()
            if deepdive_prompt:
                session["deepdive_prompt"] = deepdive_prompt
            elif "deepdive_prompt" in session:
                session.pop("deepdive_prompt", None)
        if request.form.get("save_verify_prompt") == "1":
            verify_prompt = request.form.get("verify_prompt", "").strip()
            if verify_prompt:
                session["verify_prompt"] = verify_prompt
            elif "verify_prompt" in session:
                session.pop("verify_prompt", None)
        # Search Logic config
        if request.form.get("save_search_logic") == "1":
            similar_method = request.form.get("similar_method", "").strip()
            if similar_method in ("embed", "legacy"):
                session["similar_method"] = similar_method
            elif "similar_method" in session:
                session.pop("similar_method", None)
            eval_similar_method = request.form.get("eval_similar_method", "").strip()
            if eval_similar_method in ("embed", "legacy"):
                session["eval_similar_method"] = eval_similar_method
            elif "eval_similar_method" in session:
                session.pop("eval_similar_method", None)
        return redirect(url_for("config_page"))

    llm_backend = (session.get("llm_backend") or os.environ.get("LLM_BACKEND", "openai"))
    llm_base_url = (session.get("llm_base_url") or os.environ.get("LLM_BASE_URL", ""))
    llm_model = (session.get("llm_model") or os.environ.get("LLM_MODEL", ""))
    llm_has_key = bool(session.get("llm_api_key") or os.environ.get("LLM_API_KEY"))
    eval_prompt = session.get("eval_prompt", "")
    deepdive_prompt = session.get("deepdive_prompt", "")
    verify_prompt = session.get("verify_prompt", "")
    similar_method = session.get("similar_method", "embed")
    eval_similar_method = session.get("eval_similar_method", "embed")

    last_ingest = None
    _ingest_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".last_ingest.json")
    # ponytail: Docker creates a directory when bind-mount source is missing.
    # resolve_bind_path finds the file inside the directory if one exists.
    _actual = resolve_bind_path(_ingest_path)
    if not os.path.isfile(_actual):
        _actual = None
    if _actual:
        try:
            with open(_actual) as f:
                last_ingest = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    db_card_count = None
    try:
        db = get_db()
        row = db.execute("SELECT COUNT(*) AS cnt FROM (SELECT DISTINCT name FROM cards WHERE language = 'English' AND (side IS NULL OR side = 'a'))").fetchone()
        db_card_count = row["cnt"]
    except Exception:
        pass

    embed_status = embed.status()

    return render_template("config.html",
                           llm_backend=llm_backend,
                           llm_base_url=llm_base_url,
                           llm_model=llm_model,
                           llm_has_key=llm_has_key,
                           eval_prompt=eval_prompt,
                           deepdive_prompt=deepdive_prompt,
                           verify_prompt=verify_prompt,
                           similar_method=similar_method,
                           eval_similar_method=eval_similar_method,
                           default_prompt=COMMANDER_SYSTEM_PROMPT,
                           default_deepdive_prompt=DEEPDIVE_SYSTEM_PROMPT,
                           default_verify_prompt=VERIFY_SYSTEM_PROMPT,
                           last_ingest=last_ingest,
                           db_card_count=db_card_count,
                           embed_status=embed_status,
                           mcp_sse_port=MCP_SSE_PORT,
                           mcpo_port=MCPO_PORT,
                           mcp_host=MCP_HOST,
                           mcp_display_host=MCP_DISPLAY_HOST)


@app.route("/config/embed-status")
def embed_status():
    """Return current embedding index build status as JSON."""
    return jsonify(embed.status())


@app.route("/config/embed-build", methods=["POST"])
def embed_build():
    """Trigger an embedding index rebuild. Returns immediately; build runs async."""
    status_info = embed.status()
    if status_info["building"]:
        return jsonify({"success": False, "error": "Build already in progress."}), 409

    def _build():
        try:
            embed.build(db_path(DATABASE))
        except Exception:
            pass
    threading.Thread(target=_build, daemon=True).start()
    return jsonify({"success": True})


# ── MCP server status ─────────────────────────────────────────────────────


@app.route("/config/mcp-status")
def mcp_status():
    """Return MCP server status — SSE backend + MCPO proxy."""
    sse_alive = port_alive(MCP_SSE_PORT)
    mcpo_alive = port_alive(MCPO_PORT)
    return jsonify({
        "running": sse_alive and mcpo_alive,
        "host": MCP_DISPLAY_HOST,
        "sse": {
            "alive": sse_alive,
            "port": MCP_SSE_PORT,
        },
        "mcpo": {
            "alive": mcpo_alive,
            "port": MCPO_PORT,
            "url": f"http://{MCP_DISPLAY_HOST}:{MCPO_PORT}/openapi.json",
        },
    })


@app.route("/config/mcp-restart", methods=["POST"])
def mcp_restart():
    """Restart MCP SSE server + MCPO proxy."""
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        sse_pid = restart_mcp(
            "sse", MCP_SSE_PORT,
            os.path.join(script_dir, ".mcp_server.pid"),
            os.path.join(script_dir, ".mcp_server.log"),
        )
        # Restart MCPO by killing it and letting run.sh re-spawn it
        import signal, subprocess, time
        mcpo_pid_file = os.path.join(script_dir, ".mcpo.pid")
        try:
            with open(mcpo_pid_file) as f:
                old = int(f.read().strip())
            os.kill(old, signal.SIGTERM)
            time.sleep(0.5)
        except (FileNotFoundError, ValueError, ProcessLookupError, OSError):
            pass
        mcpo_bin = os.path.join(script_dir, "venv", "bin", "mcpo")
        if not os.path.isfile(mcpo_bin):
            # ponytail: Docker/system install — mcpo is on PATH
            import shutil as _shutil
            found = _shutil.which("mcpo")
            if not found:
                return jsonify({"success": False, "error": "mcpo not installed. Run: pip install mcpo"}), 500
            mcpo_bin = found
        mcpo_log = os.path.join(script_dir, ".mcpo.log")
        proc = subprocess.Popen(
            [mcpo_bin, "--type", "sse", "--port", str(MCPO_PORT),
             "--name", "mtg-search", "--",
             f"http://127.0.0.1:{MCP_SSE_PORT}/sse"],
            cwd=script_dir,
            stdout=open(mcpo_log, "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        with open(mcpo_pid_file, "w") as f:
            f.write(str(proc.pid))
        return jsonify({
            "success": True,
            "sse": {"pid": sse_pid, "port": MCP_SSE_PORT},
            "mcpo": {"pid": proc.pid, "port": MCPO_PORT},
        })
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500


@app.route("/config/ingest", methods=["POST"])
def ingest_database():
    """Accept and ingest a new SQLite database file.

    Supports .sqlite, .gz, .bz2, .xz, and .zip. Replaces the active
    DATABASE atomically.
    """
    from ingest import IngestError, process_upload

    if "database" not in request.files:
        return jsonify({"error": "No file uploaded."}), 400

    f = request.files["database"]
    if not f.filename:
        return jsonify({"error": "No file selected."}), 400

    ingest_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".last_ingest.json")
    # ponytail: Docker bind-mount of nonexistent file → directory.
    ingest_path = resolve_bind_path(ingest_path)

    try:
        result = process_upload(f, f.filename, DATABASE, ingest_path)
        return jsonify(result)

    except IngestError as e:
        return jsonify({"error": str(e)}), e.status_code

    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        traceback.print_exc()
        return jsonify({"error": str(e), "traceback": tb}), 500


@app.route("/llm-models")
def llm_models_proxy():
    base_url = request.args.get("base_url", "").strip()
    if not base_url:
        return jsonify({"models": [], "error": "No base_url provided"})

    api_key = session.get("llm_api_key") or os.environ.get("LLM_API_KEY", "")
    try:
        import urllib.request
        models = fetch_models(base_url, api_key)
        return jsonify({"models": models})
    except Exception as e:
        return jsonify({"models": [], "error": str(e)})


@app.route("/card-autocomplete")
def card_autocomplete():
    """Return matching card names + set/numbers for autocomplete widgets."""
    q = request.args.get("q", "").strip()
    if len(q) < 2:
        return jsonify([])
    db = get_db()
    rows = db.execute("""
        SELECT c.name, c.setCode, c.number, s.name as setName, s.releaseDate,
               c.type, c.manaCost, ci.scryfallId
        FROM cards c
        JOIN sets s ON c.setCode = s.code
        JOIN cardIdentifiers ci ON c.uuid = ci.uuid
        WHERE c.name LIKE ? AND c.language = 'English'
          AND (c.side IS NULL OR c.side = 'a')
        GROUP BY c.name
        ORDER BY s.releaseDate DESC
        LIMIT 10
    """, [f"%{q}%"]).fetchall()
    return jsonify([dict(r) for r in rows])


@app.route("/commander-eval", methods=["GET", "POST"])
def commander_eval_landing():
    """Landing page for Commander Eval — search by card name or drop image."""
    session.pop("eval_key", None)  # ponytail: clear stale analysis when returning to landing
    db = get_db()

    # POST: drag-and-drop image lookup
    if request.method == "POST":
        scryfall_id = (request.form.get("scryfall_id") or "").strip()
        if not scryfall_id:
            return {"error": "No card image data received."}, 400
        found = db.execute("""
            SELECT c.setCode, c.number FROM cards c
            JOIN cardIdentifiers ci ON c.uuid = ci.uuid
            WHERE ci.scryfallId = ? AND c.language = 'English'
              AND (c.side IS NULL OR c.side = 'a')
            LIMIT 1
        """, [scryfall_id]).fetchone()
        if found:
            return {"redirect": url_for('commander_eval',
                                        set_code=found['setCode'],
                                        number=found['number'])}
        return {"error": "Card not found. Try searching by name."}, 404

    # GET: name search
    by_name = request.args.get("by_name", "").strip()
    if by_name:
        found = db.execute("""
            SELECT c.setCode, c.number FROM cards c
            JOIN sets s ON c.setCode = s.code
            WHERE c.name = ? AND c.language = 'English' AND (c.side IS NULL OR c.side = 'a')
            ORDER BY s.releaseDate DESC LIMIT 1
        """, [by_name]).fetchone()
        if found:
            return redirect(url_for('commander_eval',
                                    set_code=found['setCode'],
                                    number=found['number']))

    reports_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), REPORTS_DIR)
    saved_reports = []
    if os.path.isdir(reports_dir):
        for fname in sorted(os.listdir(reports_dir), reverse=True):
            if not fname.endswith(".json") or fname == ".gitkeep" or fname.endswith("_deepdives.json"):
                continue
            try:
                with open(os.path.join(reports_dir, fname)) as f:
                    data = json.load(f)
                card = data.get("card", {})
                saved_reports.append({
                    "filename": fname,
                    "name": card.get("name", "Unknown"),
                    "setCode": card.get("setCode", ""),
                    "number": card.get("number", ""),
                    "scryfallId": card.get("scryfallId", ""),
                    "savedAt": data.get("saved_at", ""),
                })
            except (json.JSONDecodeError, OSError):
                continue


    return render_template("commander_eval_landing.html",
                           by_name=by_name,
                           by_name_error=bool(by_name),
                           saved_reports=saved_reports)


@app.route("/card/<set_code>/<number>/eval/restore", methods=["POST"])
def commander_eval_restore(set_code, number):
    """Restore eval data from a saved deck into the server-side cache.
    Called by the deck builder when loading a deck that has stored evalData,
    so the eval iframe can re-render without re-running the full pipeline."""
    body = request.get_json(silent=True) or {}
    eval_data = body.get("evalData")
    if not eval_data:
        return {"error": "No evalData provided."}, 400

    key = str(uuid.uuid4())
    entry = {"_card": f"{set_code}/{number}", "analysis": eval_data}
    # ponytail: deepdives and similar are stored as separate cache keys
    # (not nested in analysis) — extract them so they render correctly.
    if isinstance(eval_data, dict):
        if eval_data.get("_deepdives"):
            entry["deepdives"] = eval_data["_deepdives"]
        if eval_data.get("_similar"):
            entry["similar"] = eval_data["_similar"]
    _cache_put(key, entry)
    session["eval_key"] = key
    return {"success": True, "key": key}


@app.route("/card/<set_code>/<number>/eval", methods=["GET", "POST"])
def commander_eval(set_code, number):
    db = get_db()

    # --- Config from session, query params, or env ---
    llm_backend = (session.get("llm_backend") or request.args.get("llm_backend")
                   or os.environ.get("LLM_BACKEND", "openai"))
    llm_base_url = (session.get("llm_base_url") or request.args.get("llm_base_url")
                    or os.environ.get("LLM_BASE_URL", ""))
    llm_model = (session.get("llm_model") or request.args.get("llm_model")
                 or os.environ.get("LLM_MODEL", ""))
    llm_has_key = bool(session.get("llm_api_key") or os.environ.get("LLM_API_KEY"))

    # Redirect from name search on eval page
    by_name = request.args.get("by_name", "").strip()
    if by_name:
        found = db.execute("""
            SELECT c.setCode, c.number FROM cards c
            JOIN sets s ON c.setCode = s.code
            WHERE c.name = ? AND c.language = 'English' AND (c.side IS NULL OR c.side = 'a')
            ORDER BY s.releaseDate DESC LIMIT 1
        """, [by_name]).fetchone()
        if found:
            return redirect(url_for('commander_eval',
                                    set_code=found['setCode'],
                                    number=found['number']))

    # Load commander card
    card = db.execute("""
        SELECT c.*, ci.scryfallId, s.name as setName, s.releaseDate
        FROM cards c
        JOIN cardIdentifiers ci ON c.uuid = ci.uuid
        JOIN sets s ON c.setCode = s.code
        WHERE c.setCode = ? AND c.number = ? AND c.language = 'English'
          AND (c.side IS NULL OR c.side = 'a')
    """, [set_code, number]).fetchone()

    if not card:
        abort(404)

    card = dict(card)

    # Load other faces (for DFCs)
    other_faces = []
    if card.get("otherFaceIds"):
        other_uuids = [u.strip() for u in card["otherFaceIds"].split(",") if u.strip()]
        if other_uuids:
            placeholders = ",".join("?" * len(other_uuids))
            other_faces = db.execute(
                f"""SELECT c.*, ci.scryfallId
                    FROM cards c
                    JOIN cardIdentifiers ci ON c.uuid = ci.uuid
                    WHERE c.uuid IN ({placeholders})
                    ORDER BY c.side""",
                other_uuids,
            ).fetchall()
            other_faces = [dict(f) for f in other_faces]

    # Check commander legality
    try:
        ls = json.loads(card.get("leadershipSkills") or "{}")
    except json.JSONDecodeError:
        ls = {}
    is_commander = bool(ls.get("commander", False))
    is_game_changer = bool(card.get("isGameChanger"))

    # Pick up analysis from server-side cache if previously completed via AJAX.
    # ponytail: validate cached analysis belongs to this card — session bleeds across navigations.
    eval_key = session.get("eval_key")
    cached = _eval_cache.get(eval_key, {}) if eval_key else {}
    stale = cached.get("_card") != f"{set_code}/{number}"
    if stale:
        cached = {}
    analysis = cached.get("analysis")
    error = cached.get("error")

    # Load from saved report if ?report=<filename> param
    loaded_from = None
    report_filename = request.args.get("report", "").strip()
    report_data = None
    if not analysis and report_filename:
        if ".." not in report_filename and "/" not in report_filename:
            report_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), REPORTS_DIR, report_filename)
            try:
                with open(report_path) as f:
                    report_data = json.load(f)
                analysis = report_data.get("analysis", {})
                loaded_from = report_filename
            except (FileNotFoundError, json.JSONDecodeError, OSError):
                error = "Saved report not found or unreadable."

    # Seed eval_key + _eval_cache so deepdive/save routes have access to the
    # analysis. Always seed when loading a report from disk so the cache belongs
    # to the current card — stale eval_key from a prior card must not bleed in.
    if analysis:
        if report_data or not session.get("eval_key"):
            session["eval_key"] = str(uuid.uuid4())
            entry = {"_card": f"{set_code}/{number}", "analysis": analysis}
            if report_data:
                # ponytail: backwards compat — old reports use "expands" key
                entry["deepdives"] = report_data.get("deepdives") or report_data.get("expands", {})
                entry["similar"] = report_data.get("similar", [])
            _cache_put(session["eval_key"], entry)

    similar = (report_data or {}).get("similar") if loaded_from else cached.get("similar")

    # ponytail: merge auto-saved deepdives so they survive navigation away.
    # cached deepdives (from this session) > loaded report > auto-save.
    _saved_deepdives = load_auto_deepdives(
        set_code, number,
        os.path.join(os.path.dirname(os.path.abspath(__file__)), REPORTS_DIR),
    )
    if report_data:
        _saved_deepdives.update(report_data.get("deepdives") or report_data.get("expands", {}))
    _saved_deepdives.update(cached.get("deepdives", {}))

    progress_key = str(uuid.uuid4())
    session["eval_progress_key"] = progress_key

    return render_template(
        "commander_eval.html",
        card=card,
        other_faces=other_faces,
        is_commander=is_commander,
        isGameChanger=is_game_changer,
        analysis=analysis,
        error=error,
        by_name=by_name,
        by_name_error=bool(by_name),
        loaded_from=loaded_from,
        saved_deepdives=_saved_deepdives,
        similar=similar,
        progress_key=progress_key,
    )


@app.route("/card/<set_code>/<number>/eval/progress")
def eval_progress(set_code, number):
    """Poll for analysis progress — returns {step, total, label}."""
    key = session.get("eval_progress_key")
    data = _progress_cache.get(key) if key else None
    return jsonify(data if data else {"step": 0, "total": 0, "label": "Waiting…"})


@app.route("/card/<set_code>/<number>/eval/analyze", methods=["POST"])
def commander_eval_analyze(set_code, number):
    """Run analysis pipeline via AJAX, store result in session."""
    db = get_db()

    llm_backend = (session.get("llm_backend") or os.environ.get("LLM_BACKEND", "openai"))
    llm_base_url = (session.get("llm_base_url") or os.environ.get("LLM_BASE_URL", ""))
    llm_model = (session.get("llm_model") or os.environ.get("LLM_MODEL", ""))

    progress_key = session.get("eval_progress_key")
    def _step(n, label):
        if progress_key:
            _progress_cache[progress_key] = {"step": n, "total": 6, "label": label}
            while len(_progress_cache) > 50:
                _progress_cache.popitem(last=False)

    _step(1, "Loading card data…")

    card = db.execute("""
        SELECT c.*, ci.scryfallId, s.name as setName, s.releaseDate
        FROM cards c
        JOIN cardIdentifiers ci ON c.uuid = ci.uuid
        JOIN sets s ON c.setCode = s.code
        WHERE c.setCode = ? AND c.number = ? AND c.language = 'English'
          AND (c.side IS NULL OR c.side = 'a')
    """, [set_code, number]).fetchone()

    if not card:
        return {"error": "Card not found"}, 404

    card = dict(card)

    # Load back face for DFCs (for the LLM prompt)
    back_face = None
    if card.get("otherFaceIds"):
        other_uuids = [u.strip() for u in card["otherFaceIds"].split(",") if u.strip()]
        if other_uuids:
            placeholders = ",".join("?" * len(other_uuids))
            faces = db.execute(
                f"""SELECT c.*, ci.scryfallId
                    FROM cards c
                    JOIN cardIdentifiers ci ON c.uuid = ci.uuid
                    WHERE c.uuid IN ({placeholders})
                    ORDER BY c.side""",
                other_uuids,
            ).fetchall()
            for f in faces:
                if f["side"] == "b":
                    back_face = dict(f)
                    break

    try:
        # Fetch official rulings for this card
        rulings = db.execute(
            "SELECT text FROM cardRulings WHERE uuid = ? ORDER BY date DESC",
            [card["uuid"]]
        ).fetchall()
        rulings_texts = [r["text"] for r in rulings]

        # Build oracle text — include back face for DFCs
        oracle_text = card.get("text", "") or ""
        full_type = card.get("type", "") or ""
        if back_face and back_face.get("text"):
            oracle_text += "\n\n--- Back Face: " + (back_face.get("faceName") or "") + " ---\n" + back_face["text"]
            if back_face.get("type"):
                full_type += " // " + back_face["type"]

        card_data = {
            "name": card["name"],
            "manaCost": card.get("manaCost", ""),
            "manaValue": card.get("manaValue"),
            "type": full_type,
            "text": oracle_text,
            "power": card.get("power"),
            "toughness": card.get("toughness"),
            "loyalty": card.get("loyalty"),
            "colorIdentity": card.get("colorIdentity", ""),
            "keywords": card.get("keywords", ""),
            "edhrecRank": card.get("edhrecRank"),
            "edhrecSaltiness": card.get("edhrecSaltiness"),
            "leadershipSkills": json.loads(card.get("leadershipSkills") or "{}") if card.get("leadershipSkills") else {},
            "rulings": rulings_texts,
        }
        # Include back face stats if present
        if back_face:
            card_data["backFace"] = {
                "name": back_face.get("faceName") or back_face.get("name"),
                "type": back_face.get("type"),
                "text": back_face.get("text"),
                "manaCost": back_face.get("manaCost"),
                "power": back_face.get("power"),
                "toughness": back_face.get("toughness"),
                "loyalty": back_face.get("loyalty"),
            }

        # ponytail: strip DFC suffix for web search — searching
        # "Cecil, Dark Knight // Cecil, Redeemed Paladin" is far less
        # effective than just the front-face name.
        name = card["name"].split(" // ")[0]
        keywords = card.get("keywords", "")

        # ponytail: track pipeline health — surface failures so the user
        # knows when analysis ran with incomplete data.
        pipeline_status = {"web_search": {}, "embed": None, "web_search_up": True}

        _step(2, "Searching web for deck guides and discussions…")

        searches = {}
        for search_label, query in [
            ("deck_guides", f"{name} commander deck guide primer"),
            ("strategy", f"{name} commander strategy synergies"),
            ("discussion", f'"{name}" commander review reddit'),
            ("unique_archetypes", f"{name} commander unusual underrated hidden archetype brew"),
            ("mechanics", f"mtg rules {keywords} comprehensive rules guide") if keywords else ("mechanics", None),
        ]:
            if query is None:
                continue
            results = web_search(query, max_results=5)
            searches[search_label] = results or []
            pipeline_status["web_search"][search_label] = len(results) if results else 0
            if results is None:
                pipeline_status["web_search_up"] = False

        user_prompt = json.dumps({
            "commander": card_data,
            "web_research": searches,
        }, indent=2)

        # ponytail: enrich with mechanically similar cards from embedding index.
        # Gives the LLM real cards to reference instead of guessing at synergies.
        _step(3, "Retrieving mechanically similar cards…")
        try:
            similar_cards = embed.find(db_path(DATABASE), card_data["text"], top_k=200)
            # ponytail: filter out the commander's own card + off-color cards
            commander_ci = card_data.get("colorIdentity", "")
            similar_cards = [
                c for c in similar_cards
                if c["name"] != card["name"] and color_identity_subset(c.get("colorIdentity", ""), commander_ci)
            ]
            pipeline_status["embed"] = len(similar_cards)
            if similar_cards:
                # ponytail: trim to metadata only — oracle text adds 30KB+ that
                # pushes context limits. The LLM needs names + types to reason,
                # not the full card text it already knows.
                slim = []
                for c in similar_cards:
                    slim.append({
                        "name": c["name"],
                        "type": c.get("type", ""),
                        "keywords": c.get("keywords", ""),
                        "colorIdentity": c.get("colorIdentity", ""),
                        "manaValue": c.get("manaValue"),
                        "score": c.get("score"),
                    })
                user_prompt = json.dumps({
                    "commander": card_data,
                    "web_research": searches,
                    "similar_cards": slim,
                }, indent=2)
        except Exception:
            pipeline_status["embed"] = 0  # ponytail: index missing or model not loaded
            similar_cards = []  # ponytail: keep verify path safe when embed fails

        if llm_generate is None:
            raise RuntimeError("LLM dependencies not installed. Run: pip install openai anthropic")

        _step(4, "Analyzing with LLM…")

        api_key = session.get("llm_api_key") or os.environ.get("LLM_API_KEY")
        commander_prompt = session.get("eval_prompt") or COMMANDER_SYSTEM_PROMPT

        # ponytail: retry once on JSON parse failure — Gemma4 occasionally
        # produces malformed JSON that _parse_json can't repair.
        analysis = None
        for attempt in range(2):
            try:
                analysis = llm_generate(
                    commander_prompt, user_prompt,
                    backend=llm_backend,
                    api_key=api_key,
                    base_url=llm_base_url or None,
                    model=llm_model or None,
                )
                break
            except (json.JSONDecodeError, ValueError):
                if attempt == 0:
                    user_prompt += "\n\n---\nYour previous response had a JSON formatting error. Please output ONLY valid JSON. Double-check all braces, brackets, and commas."
                else:
                    raise

        # Second-pass verification — check for invented abilities, wrong zones, hallucinated card names
        _step(5, "Verifying analysis accuracy…")
        verify_prompt = session.get("verify_prompt") or VERIFY_SYSTEM_PROMPT

        verify_user = json.dumps({
            "card": {
                "name": card_data["name"],
                "oracle_text": card_data["text"],
                "type": card_data["type"],
                "keywords": card_data["keywords"],
                "colorIdentity": card_data["colorIdentity"],
                "rulings": rulings_texts,
            },
            "analysis_to_verify": analysis,
            "allowed_card_names": [c["name"] for c in similar_cards] if similar_cards else [],
        }, indent=2)

        try:
            verification = llm_generate(
                verify_prompt, verify_user,
                backend=llm_backend,
                api_key=api_key,
                base_url=llm_base_url or None,
                model=llm_model or None,
            )
            analysis["_verification"] = verification
        except Exception:
            # If verification fails, still return the analysis without it
            pass

        analysis["_pipeline"] = pipeline_status

        _step(6, "Done!")
        session["eval_key"] = str(uuid.uuid4())
        _cache_put(session["eval_key"], {"_card": f"{set_code}/{number}", "analysis": analysis})
        return {"success": True}

    except Exception as e:
        import traceback
        traceback.print_exc()
        session["eval_key"] = str(uuid.uuid4())
        _cache_put(session["eval_key"], {"_card": f"{set_code}/{number}", "error": str(e)})
        return {"error": str(e)}, 500


@app.route("/card/<set_code>/<number>/eval/deepdive", methods=["POST"])
def commander_eval_deepdive(set_code, number):
    """Deep-dive on a specific strategy or unique build for this commander."""
    db = get_db()

    llm_backend = (session.get("llm_backend") or os.environ.get("LLM_BACKEND", "openai"))
    llm_base_url = (session.get("llm_base_url") or os.environ.get("LLM_BASE_URL", ""))
    llm_model = (session.get("llm_model") or os.environ.get("LLM_MODEL", ""))

    body = request.get_json(silent=True) or {}
    expand_type = body.get("type", "")
    expand_name = body.get("name", "")
    expand_desc = body.get("description", "")

    if not expand_name or expand_type not in ("strategy", "unique_build"):
        return jsonify({"error": "Missing name or invalid type."}), 400

    card = db.execute("""
        SELECT c.*, ci.scryfallId
        FROM cards c
        JOIN cardIdentifiers ci ON c.uuid = ci.uuid
        WHERE c.setCode = ? AND c.number = ? AND c.language = 'English'
          AND (c.side IS NULL OR c.side = 'a')
    """, [set_code, number]).fetchone()

    if not card:
        return jsonify({"error": "Card not found."}), 404

    card = dict(card)

    # Fetch rulings
    rulings = db.execute(
        "SELECT text FROM cardRulings WHERE uuid = ? ORDER BY date DESC",
        [card["uuid"]]
    ).fetchall()
    rulings_texts = [r["text"] for r in rulings]

    type_label = "strategy" if expand_type == "strategy" else "unique build"

    deepdive_prompt = session.get("deepdive_prompt") or DEEPDIVE_SYSTEM_PROMPT
    system_prompt = deepdive_prompt.replace("{type_label}", type_label)

    user_prompt = json.dumps({
        "commander": {
            "name": card["name"],
            "oracle_text": card.get("text", ""),
            "type": card["type"],
            "keywords": card.get("keywords", ""),
            "colorIdentity": card.get("colorIdentity", ""),
            "manaValue": card.get("manaValue"),
            "rulings": rulings_texts,
        },
        expand_type: {
            "name": expand_name,
            "description": expand_desc,
        },
    }, indent=2)

    # ponytail: dual-pass retrieval — commander text + strategy description.
    # Commander-only retrieval biases toward similar legendaries; strategy-aware
    # retrieval surfaces enablers, payoffs, and role-players for the 99.
    dd_pipeline = {"embed": None, "web_search": {}, "web_search_up": True}
    _card_lookup = {}  # name -> {scryfallId, setCode, number}
    _allowed_names = []  # ponytail: track for verify allowlist
    try:
        commander_cards = embed.find(db_path(DATABASE), card.get("text", "") or "", top_k=100)
        strategy_text = f"{card['name']} {expand_name} {expand_desc}"
        strategy_cards = embed.find(db_path(DATABASE), strategy_text, top_k=100)
        # ponytail: filter to commander's color identity
        commander_ci = card.get("colorIdentity", "")
        commander_cards = [c for c in commander_cards if color_identity_subset(c.get("colorIdentity", ""), commander_ci)]
        strategy_cards = [c for c in strategy_cards if color_identity_subset(c.get("colorIdentity", ""), commander_ci)]
        dd_pipeline["embed"] = len(commander_cards) + len(strategy_cards)
        # ponytail: dedupe by name, commander-biased cards first
        seen = set()
        merged = []
        merge_uuids = []
        for c in commander_cards:
            if c["name"] != card["name"] and c["name"] not in seen:
                seen.add(c["name"])
                merged.append(c)
                merge_uuids.append(c.get("uuid"))
        for c in strategy_cards:
            if c["name"] not in seen:
                seen.add(c["name"])
                merged.append(c)
                merge_uuids.append(c.get("uuid"))
        # ponytail: resolve uuids to setCode/number in one query
        if merge_uuids:
            ph = ", ".join("?" for _ in merge_uuids)
            uuid_rows = db.execute(
                f"SELECT uuid, number, setCode FROM cards WHERE uuid IN ({ph}) AND language = 'English'",
                merge_uuids
            ).fetchall()
            uuid_map = {r["uuid"]: dict(r) for r in uuid_rows}
        else:
            uuid_map = {}
        if merged:
            slim = []
            for c in merged:
                slim.append({
                    "name": c["name"],
                    "type": c.get("type", ""),
                    "keywords": c.get("keywords", ""),
                    "colorIdentity": c.get("colorIdentity", ""),
                    "manaValue": c.get("manaValue"),
                    "score": c.get("score"),
                    "text": (c.get("text") or "")[:200],
                })
                info = uuid_map.get(c.get("uuid"), {})
                _card_lookup[c["name"].lower()] = {
                    "scryfallId": c.get("scryfallId", ""),
                    "setCode": info.get("setCode", ""),
                    "number": info.get("number", ""),
                }
                _allowed_names.append(c["name"])
            user_prompt = json.dumps({
                "commander": {
                    "name": card["name"],
                    "oracle_text": card.get("text", ""),
                    "type": card["type"],
                    "keywords": card.get("keywords", ""),
                    "colorIdentity": card.get("colorIdentity", ""),
                    "manaValue": card.get("manaValue"),
                    "rulings": rulings_texts,
                },
                expand_type: {
                    "name": expand_name,
                    "description": expand_desc,
                },
                "similar_cards": slim,
            }, indent=2)
    except Exception:
        dd_pipeline["embed"] = 0  # ponytail: index missing; proceed without

    # ponytail: web research for this specific strategy pairing.
    # Surfaces real decklists and community card choices the embeddings miss.
    try:
        # ponytail: strip DFC suffix — front-face name only for search queries
        search_name = card["name"].split(" // ")[0]
        searches = {}
        for search_label, query in [
            ("deck_guides", f"{search_name} {expand_name} commander deck primer"),
            ("strategy", f"{search_name} {expand_desc} synergies"),
            ("discussion", f"{search_name} {expand_name} commander reddit edh"),
        ]:
            results = web_search(query, max_results=5)
            searches[search_label] = results or []
            dd_pipeline["web_search"][search_label] = len(results) if results else 0
            if results is None:
                dd_pipeline["web_search_up"] = False
        if "similar_cards" in json.loads(user_prompt):
            data = json.loads(user_prompt)
            data["web_research"] = searches
            user_prompt = json.dumps(data, indent=2)
        else:
            user_prompt = json.dumps({
                "commander": {
                    "name": card["name"],
                    "oracle_text": card.get("text", ""),
                    "type": card["type"],
                    "keywords": card.get("keywords", ""),
                    "colorIdentity": card.get("colorIdentity", ""),
                    "manaValue": card.get("manaValue"),
                    "rulings": rulings_texts,
                },
                expand_type: {
                    "name": expand_name,
                    "description": expand_desc,
                },
                "web_research": searches,
            }, indent=2)
    except Exception:
        pass  # ponytail: SearXNG unavailable; proceed without

    if llm_generate is None:
        return jsonify({"error": "LLM dependencies not installed."}), 500

    try:
        api_key = session.get("llm_api_key") or os.environ.get("LLM_API_KEY")

        # ponytail: retry once on JSON parse failure.
        data = None
        for attempt in range(2):
            try:
                data = llm_generate(
                    system_prompt, user_prompt,
                    backend=llm_backend,
                    api_key=api_key,
                    base_url=llm_base_url or None,
                    model=llm_model or None,
                )
                break
            except (json.JSONDecodeError, ValueError):
                if attempt == 0:
                    user_prompt += "\n\n---\nYour previous response had a JSON formatting error. Please output ONLY valid JSON."
                else:
                    raise

        # ponytail: second-pass verification for deepdive results.
        verify_prompt = session.get("verify_prompt") or VERIFY_SYSTEM_PROMPT
        try:
            verify_user = json.dumps({
                "card": {
                    "name": card["name"],
                    "oracle_text": card.get("text", ""),
                    "type": card["type"],
                    "keywords": card.get("keywords", ""),
                    "colorIdentity": card.get("colorIdentity", ""),
                    "rulings": rulings_texts,
                },
                "analysis_to_verify": data,
                "allowed_card_names": _allowed_names,
            }, indent=2)
            verification = llm_generate(
                verify_prompt, verify_user,
                backend=llm_backend,
                api_key=api_key,
                base_url=llm_base_url or None,
                model=llm_model or None,
            )
            data["_verification"] = verification
        except Exception:
            pass  # ponytail: verification is best-effort; deepdive still ships

        # ponytail: resolve recommended card names to Scryfall IDs and set/numbers.
        # First try embed results (fast, no extra query). Fall back to DB for any
        # names the embed index didn't cover.
        if data.get("example_cards"):
            # ponytail: strip placeholder entries — some models ignore the prompt.
            filtered = [rc for rc in data["example_cards"]
                        if not is_placeholder_name(rc.get("name", ""))]
            if len(filtered) < len(data["example_cards"]):
                data["_placeholder_stripped"] = len(data["example_cards"]) - len(filtered)
            data["example_cards"] = filtered
            unresolved = []
            for rc in data["example_cards"]:
                match = _card_lookup.get(rc["name"].lower())
                if match and match.get("scryfallId") and match.get("setCode"):
                    rc["scryfallId"] = match["scryfallId"]
                    rc["setCode"] = match["setCode"]
                    rc["number"] = match["number"]
                else:
                    unresolved.append(rc)
            if unresolved:
                names = [rc["name"] for rc in unresolved]
                ph = ", ".join("?" for _ in names)
                rows = db.execute(
                    f"SELECT c.name, ci.scryfallId, c.setCode, c.number "
                    f"FROM cards c JOIN cardIdentifiers ci ON c.uuid = ci.uuid "
                    f"WHERE c.name IN ({ph}) AND c.language = 'English'",
                    names
                ).fetchall()
                db_lookup = {r["name"].lower(): r for r in rows}
                for rc in unresolved:
                    match = db_lookup.get(rc["name"].lower())
                    if match:
                        rc["scryfallId"] = match["scryfallId"]
                        rc["setCode"] = match["setCode"]
                        rc["number"] = match["number"]

        # Stash deepdive result so it gets included when the report is saved
        eval_key = session.get("eval_key")
        if eval_key:
            with _cache_lock:
                if eval_key in _eval_cache:
                    cache_entry = _eval_cache[eval_key]
                    if "deepdives" not in cache_entry:
                        cache_entry["deepdives"] = {}
                    cache_entry["deepdives"][f"{expand_type}:{expand_name}"] = data

        # ponytail: auto-persist deepdives so they survive navigation away.
        # Manual "Save Report" still creates the timestamped copy.
        auto_save_deepdives(
            set_code, number, {f"{expand_type}:{expand_name}": data},
            os.path.join(os.path.dirname(os.path.abspath(__file__)), REPORTS_DIR),
        )

        data["_pipeline"] = dd_pipeline
        return jsonify({"success": True, "data": data})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/card/<set_code>/<number>/eval/similar", methods=["POST"])
def commander_eval_similar(set_code, number):
    """Find similar legendary creatures (Commander-legal).

    Supports two methods via request body:
      - method: "embed" (default) — semantic similarity via embedding index
      - method: "legacy" — TF-IDF strictness-loosening loop
    """
    db = get_db()
    body = request.get_json(silent=True) or {}
    method = body.get("method") or session.get("eval_similar_method", "embed")

    card = db.execute("""
        SELECT c.*, ci.scryfallId
        FROM cards c
        JOIN cardIdentifiers ci ON c.uuid = ci.uuid
        WHERE c.setCode = ? AND c.number = ? AND c.language = 'English'
          AND (c.side IS NULL OR c.side = 'a')
    """, [set_code, number]).fetchone()
    if not card:
        return jsonify({"error": "Card not found."}), 404

    card = dict(card)

    if method == "legacy":
        results, progress = eval_similar_legacy(db, card, db_path(DATABASE))
    else:
        try:
            results, progress = eval_similar_embed(db, card, db_path(DATABASE))
        except Exception:
            # ponytail: fall back to legacy if embeddings unavailable
            results, progress = eval_similar_legacy(db, card, db_path(DATABASE))

    # ponytail: stash in cache so save picks it up
    eval_key = session.get("eval_key")
    if eval_key:
        with _cache_lock:
            if eval_key in _eval_cache:
                _cache_update_locked(eval_key, similar=results)

    return jsonify({"success": True, "results": results, "progress": progress, "method": method})



    cached = _eval_cache.get(eval_key)
    if not cached or "analysis" not in cached:
        return jsonify({"error": "No analysis found in cache."}), 400

    db = get_db()
    card = db.execute("""
        SELECT c.name, c.setCode, c.number, ci.scryfallId
        FROM cards c
        JOIN cardIdentifiers ci ON c.uuid = ci.uuid
        WHERE c.setCode = ? AND c.number = ? AND c.language = 'English'
          AND (c.side IS NULL OR c.side = 'a')
    """, [set_code, number]).fetchone()

    if not card:
        return jsonify({"error": "Card not found."}), 404

    now_iso = datetime.now(timezone.utc).isoformat()
    safe_name = f"{set_code}_{number}_{now_iso.replace(':', '-')}.json"
    report_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), REPORTS_DIR, safe_name)

    os.makedirs(os.path.dirname(report_path), exist_ok=True)

    # ponytail: merge auto-saved deepdives so save captures everything even after cache eviction.
    deepdives = cached.get("deepdives", {})
    _autosaved = load_auto_deepdives(
        set_code, number,
        os.path.join(os.path.dirname(os.path.abspath(__file__)), REPORTS_DIR),
    )
    _autosaved.update(deepdives)  # cache wins over disk
    deepdives = _autosaved

    with open(report_path, "w") as f:
        json.dump({
            "card": dict(card),
            "analysis": cached["analysis"],
            "deepdives": deepdives,
            "similar": cached.get("similar", []),
            "saved_at": now_iso,
        }, f, indent=2)

    return jsonify({"success": True, "filename": safe_name})


    """List saved eval reports as JSON."""
    reports_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), REPORTS_DIR)
    if not os.path.isdir(reports_dir):
        return jsonify([])

    reports = []
    for fname in sorted(os.listdir(reports_dir), reverse=True):
        if not fname.endswith(".json") or fname == ".gitkeep" or fname.endswith("_deepdives.json"):
            continue
        try:
            with open(os.path.join(reports_dir, fname)) as f:
                data = json.load(f)
            card = data.get("card", {})
            reports.append({
                "filename": fname,
                "name": card.get("name", "Unknown"),
                "setCode": card.get("setCode", ""),
                "number": card.get("number", ""),
                "scryfallId": card.get("scryfallId", ""),
                "savedAt": data.get("saved_at", ""),
            })
        except (json.JSONDecodeError, OSError):
            continue

    return jsonify(reports)


@app.route("/card/<set_code>/<number>/eval/load", methods=["POST"])
def commander_eval_load(set_code, number):
    """Load a saved analysis report from disk."""
    body = request.get_json(silent=True) or {}
    filename = body.get("filename", "").strip()
    if not filename or ".." in filename or "/" in filename:
        return jsonify({"error": "Invalid filename."}), 400

    report_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), REPORTS_DIR, filename)
    try:
        with open(report_path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return jsonify({"error": "Report not found or unreadable."}), 404

    return jsonify({"success": True, "analysis": data.get("analysis", {})})


@app.route("/commander-eval/reports/delete", methods=["POST"])
def commander_eval_reports_delete():
    """Delete a saved eval report by filename."""
    body = request.get_json(silent=True) or {}
    filename = body.get("filename", "").strip()
    if not filename or ".." in filename or "/" in filename:
        return jsonify({"error": "Invalid filename."}), 400
    report_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), REPORTS_DIR, filename)
    try:
        os.unlink(report_path)
    except FileNotFoundError:
        return jsonify({"error": "Report not found."}), 404
    except OSError:
        return jsonify({"error": "Could not delete report."}), 500
    return jsonify({"success": True})


# --- Deck Builder Routes ---


@app.route("/deck-builder")
def deck_builder_page():
    """Deck Builder — split-panel page: search on left, deck on right."""
    return render_template("deck_builder.html")


@app.route("/saved-decks")
def saved_decks_page():
    """Saved Decks — grid view of all locally saved decks."""
    decks = []
    if os.path.isdir(DECKS_DIR):
        for fname in sorted(os.listdir(DECKS_DIR)):
            if not fname.endswith('.json') or fname == '_tags.json':
                continue
            fpath = os.path.join(DECKS_DIR, fname)
            try:
                with open(fpath) as f:
                    data = json.load(f)
                decks.append(data)
            except (json.JSONDecodeError, OSError):
                pass
    return render_template("saved_decks.html", decks=decks)


@app.route("/api/deck/save", methods=["POST"])
def api_deck_save():
    """Save deck to disk as JSON."""
    body = request.get_json(silent=True) or {}
    name = body.get("name", "").strip()
    if not name:
        return jsonify({"error": "Deck name is required."}), 400
    safe_name = re.sub(r'[^a-zA-Z0-9_ -]', '', name)
    fname = safe_name.replace(' ', '_') + '.json'
    fpath = os.path.join(DECKS_DIR, fname)
    try:
        with open(fpath, 'w') as f:
            json.dump(body, f, indent=2)
        return jsonify({"success": True})
    except OSError as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/deck/list", methods=["GET"])
def api_deck_list():
    """List all saved decks."""
    decks = []
    if os.path.isdir(DECKS_DIR):
        for fname in sorted(os.listdir(DECKS_DIR)):
            if not fname.endswith('.json') or fname == '_tags.json':
                continue
            fpath = os.path.join(DECKS_DIR, fname)
            try:
                with open(fpath) as f:
                    data = json.load(f)
                decks.append(data)
            except (json.JSONDecodeError, OSError):
                pass
    return jsonify(decks)


@app.route("/api/deck/delete", methods=["POST"])
def api_deck_delete():
    """Delete a saved deck by name."""
    body = request.get_json(silent=True) or {}
    name = body.get("name", "").strip()
    if not name:
        return jsonify({"error": "Deck name is required."}), 400
    safe_name = re.sub(r'[^a-zA-Z0-9_ -]', '', name)
    fname = safe_name.replace(' ', '_') + '.json'
    fpath = os.path.join(DECKS_DIR, fname)
    try:
        os.unlink(fpath)
        return jsonify({"success": True})
    except FileNotFoundError:
        return jsonify({"error": "Deck not found."}), 404
    except OSError as e:
        return jsonify({"error": str(e)}), 500


# --- Tag Catalog ---
TAGS_FILE = os.path.join(DECKS_DIR, "_tags.json")

PREDEFINED_TAGS = [
    {"name": "Ramp", "color": "#4caf50"},
    {"name": "Removal", "color": "#f44336"},
    {"name": "Card Draw", "color": "#2196f3"},
    {"name": "Tutor", "color": "#9c27b0"},
    {"name": "Board Wipe", "color": "#ff9800"},
    {"name": "Protection", "color": "#009688"},
    {"name": "Recursion", "color": "#795548"},
    {"name": "Win Condition", "color": "#ffc107"},
]


@app.route("/api/tags/list", methods=["GET"])
def api_tags_list():
    """Return the global tag catalog. Seeds predefined tags on first access."""
    if not os.path.exists(TAGS_FILE):
        with open(TAGS_FILE, "w") as f:
            json.dump(PREDEFINED_TAGS, f, indent=2)
        return jsonify({"tags": PREDEFINED_TAGS})
    try:
        with open(TAGS_FILE) as f:
            tags = json.load(f)
        return jsonify({"tags": tags})
    except (json.JSONDecodeError, OSError):
        return jsonify({"tags": PREDEFINED_TAGS})


@app.route("/api/tags/save", methods=["POST"])
def api_tags_save():
    """Save the global tag catalog."""
    body = request.get_json(silent=True) or {}
    tags = body.get("tags", [])
    try:
        with open(TAGS_FILE, "w") as f:
            json.dump(tags, f, indent=2)
        return jsonify({"success": True})
    except OSError as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/deck/card-lookup", methods=["POST"])
def deck_card_lookup():
    """Look up a card by setCode+number or uuid. Returns card JSON."""
    body = request.get_json(silent=True) or {}
    db = get_db()

    set_code = body.get("setCode", "").strip()
    number = body.get("number", "").strip()
    uuid_val = body.get("uuid", "").strip()

    if uuid_val:
        card = db.execute("""
            SELECT c.*, ci.scryfallId, s.name as setName, s.releaseDate
            FROM cards c
            JOIN cardIdentifiers ci ON c.uuid = ci.uuid
            JOIN sets s ON c.setCode = s.code
            WHERE c.uuid = ? AND c.language = 'English'
              AND (c.side IS NULL OR c.side = 'a')
        """, [uuid_val]).fetchone()
    elif set_code and number:
        card = db.execute("""
            SELECT c.*, ci.scryfallId, s.name as setName, s.releaseDate
            FROM cards c
            JOIN cardIdentifiers ci ON c.uuid = ci.uuid
            JOIN sets s ON c.setCode = s.code
            WHERE c.setCode = ? AND c.number = ? AND c.language = 'English'
              AND (c.side IS NULL OR c.side = 'a')
        """, [set_code, number]).fetchone()
    else:
        return jsonify({"error": "Provide setCode+number or uuid."}), 400

    if not card:
        return jsonify({"error": "Card not found."}), 404

    return jsonify({
        "uuid": card["uuid"],
        "name": card["name"],
        "setCode": card["setCode"],
        "number": card["number"],
        "scryfallId": card["scryfallId"],
        "manaCost": card["manaCost"] or "",
        "manaValue": card["manaValue"],
        "type": card["type"] or "",
        "types": card["types"] or "",
        "subtypes": card["subtypes"] or "",
        "supertypes": card["supertypes"] or "",
        "colors": card["colors"] or "",
        "colorIdentity": card["colorIdentity"] or "",
        "text": card["text"] or "",
        "power": card["power"],
        "toughness": card["toughness"],
        "loyalty": card["loyalty"],
        "rarity": card["rarity"] or "",
        "imageUrl": card_image(card["scryfallId"]),
        "isLegendary": "Legendary" in (card["type"] or ""),
        "isCreature": "Creature" in (card["type"] or ""),
    })


@app.route("/api/deck/lookup-by-name", methods=["POST"])
def deck_lookup_by_name():
    """Look up a card by exact name (case-insensitive). Returns latest printing."""
    body = request.get_json(silent=True) or {}
    name = body.get("name", "").strip()
    if not name:
        return jsonify({"error": "No card name provided."}), 400

    db = get_db()
    card = db.execute("""
        SELECT c.*, ci.scryfallId, s.name as setName
        FROM cards c
        JOIN cardIdentifiers ci ON c.uuid = ci.uuid
        JOIN sets s ON c.setCode = s.code
        WHERE LOWER(c.name) = LOWER(?) AND c.language = 'English'
          AND (c.side IS NULL OR c.side = 'a')
        ORDER BY s.releaseDate DESC
        LIMIT 1
    """, [name]).fetchone()

    if not card:
        return jsonify({"error": f"Card not found: {name}"}), 404

    return jsonify({
        "uuid": card["uuid"],
        "name": card["name"],
        "setCode": card["setCode"],
        "number": card["number"],
        "scryfallId": card["scryfallId"],
        "manaCost": card["manaCost"] or "",
        "manaValue": card["manaValue"],
        "type": card["type"] or "",
        "types": card["types"] or "",
        "subtypes": card["subtypes"] or "",
        "supertypes": card["supertypes"] or "",
        "colors": card["colors"] or "",
        "colorIdentity": card["colorIdentity"] or "",
        "text": card["text"] or "",
        "power": card["power"],
        "toughness": card["toughness"],
        "loyalty": card["loyalty"],
        "rarity": card["rarity"] or "",
        "imageUrl": card_image(card["scryfallId"]),
        "isLegendary": "Legendary" in (card["type"] or ""),
        "isCreature": "Creature" in (card["type"] or ""),
    })


@app.route("/api/deck/lookup-by-uuid", methods=["POST"])
def deck_lookup_by_uuid():
    """Given a Scryfall UUID, find the card and return its setCode + number."""
    body = request.get_json(silent=True) or {}
    scryfall_id = body.get("scryfallId", "").strip()
    if not scryfall_id:
        return jsonify({"error": "No scryfallId provided."}), 400

    db = get_db()
    card = db.execute("""
        SELECT c.setCode, c.number, c.uuid, c.name
        FROM cards c
        JOIN cardIdentifiers ci ON c.uuid = ci.uuid
        WHERE ci.scryfallId = ? AND c.language = 'English'
          AND (c.side IS NULL OR c.side = 'a')
    """, [scryfall_id]).fetchone()

    if not card:
        return jsonify({"error": "Card not found in database."}), 404

    return jsonify({
        "setCode": card["setCode"],
        "number": card["number"],
        "uuid": card["uuid"],
        "name": card["name"],
    })



# --- Run ---

if __name__ == "__main__":
    app.run(host="0.0.0.0", debug=os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true", "yes"))
