"""Self-hosted MTG card search — Scryfall replica.

ponytail: single-file Flask app, SQLite, no ORM, no JS, no build step.
ponytail: global lock via SQLite single-writer (we're read-only, irrelevant).
"""

import os
import json
import sqlite3
import re
import math
import uuid
import tempfile
import shutil
import gzip
import bz2
import lzma
import zipfile
import tarfile
from datetime import datetime, timezone
from collections import OrderedDict
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen
from flask import Flask, g, render_template, request, abort, redirect, url_for, make_response, session, jsonify

# ponytail: optional import — only needed for commander eval
try:
    from llm import generate as llm_generate
except ImportError:
    llm_generate = None

app = Flask(__name__)

# ponytail: persist secret key so sessions survive restarts
_secret_key = os.environ.get("SECRET_KEY")
if not _secret_key:
    _key_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".secret_key")
    try:
        with open(_key_path, "rb") as f:
            _secret_key = f.read()
    except FileNotFoundError:
        _secret_key = os.urandom(24)
        with open(_key_path, "wb") as f:
            f.write(_secret_key)
app.secret_key = _secret_key

@app.after_request
def _add_security_headers(response):
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' https: data:; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; script-src 'self' 'unsafe-inline'; font-src 'self' https://cdn.jsdelivr.net"
    return response

# ponytail: server-side analysis cache — Flask cookie sessions cap at ~4KB,
# expanded bracket+kill-on-sight output exceeds that.
# ponytail: FIFO-capped at 100 entries, add LRU if eviction order matters.
_eval_cache = OrderedDict()  # {id: {"analysis": {...}, "error": "..."}}

def _cache_put(key, entry):
    """Store in eval cache, evicting oldest if over cap."""
    _eval_cache[key] = entry
    while len(_eval_cache) > 100:
        _eval_cache.popitem(last=False)

DATABASE = "AllPrintings.sqlite"
REPORTS_DIR = "eval_reports"


# --- DB helpers ---

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA query_only = ON")
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()

# --- Image URL ---

def card_image(scryfall_id, face="front", size="normal"):
    """Build Scryfall CDN image URL."""
    if not scryfall_id:
        return ""
    c1, c2 = scryfall_id[0], scryfall_id[1]
    return f"https://cards.scryfall.io/{size}/{face}/{c1}/{c2}/{scryfall_id}.jpg"

# --- Mana symbol helpers ---

def mana_symbols(cost):
    """Replace {W}{U}{B}{R}{G}{C}{T} etc with styled spans."""
    if not cost:
        return ""
    SYMBOLS = {
        "{W}": "w", "{U}": "u", "{B}": "b", "{R}": "r", "{G}": "g",
        "{C}": "c", "{S}": "s", "{T}": "tap",
        "{W/P}": "wp", "{U/P}": "up", "{B/P}": "bp", "{R/P}": "rp", "{G/P}": "gp",
        "{E}": "e", "{PW}": "pw",
        # Hybrid pairs (two-color)
        "{W/U}": "wu", "{U/B}": "ub", "{B/R}": "br", "{R/G}": "rg", "{G/W}": "gw",
        "{W/B}": "wb", "{U/R}": "ur", "{B/G}": "bg", "{R/W}": "rw", "{G/U}": "gu",
        # Generic/color hybrid
        "{2/W}": "2w", "{2/U}": "2u", "{2/B}": "2b", "{2/R}": "2r", "{2/G}": "2g",
    }
    result = cost
    for text, cls in SYMBOLS.items():
        result = result.replace(text, f'<i class="ms ms-{cls}"></i>')
    # Fallback: bare generic/colorless — {X}, {2}, {10}, etc.
    result = re.sub(r'\{(X|\d+)\}', r'<i class="ms ms-\1"></i>', result)
    return result

def render_mana(cost):
    """Render mana cost to HTML with mana symbol classes."""
    return mana_symbols(cost)

# --- Similarity helpers ---

# ponytail: MTG-domain terms that appear on most cards and drown out functional signal.
MTG_STOP_WORDS = {
    "creature", "target", "player", "card", "spell", "control", "battlefield",
    "graveyard", "hand", "library", "opponent", "end", "step", "turn",
    "draw", "cast", "permanent", "damage", "counter", "land", "token",
    "artifact", "enchantment", "destroy", "exile", "sacrifice", "return",
    "phase", "combat", "beginning", "upkeep", "untap", "source",
    "yourself", "owner", "shuffle", "search", "reveal", "choose",
    "enters", "leaves", "activate", "ability", "abilities",
    # ponytail: common -es plurals that stemming skips to avoid mangling
    # verbs like "does"/"goes". Without these, the plural forms survive
    # the stop-word filter as distinct terms.
    "sacrifices", "searches", "creatures", "damages", "targets",
    "spells", "tokens", "destroys", "returns", "reveals", "counters",
}

# ponytail: keyword abilities are mechanical labels, not functional signals.
# A card with "flying, menace" and a card with just "menace" aren't similar;
# matching on keywords drowns out the oracle text signal that actually matters.
KEYWORD_TERMS = {
    "menace", "flying", "trample", "haste", "vigilance", "lifelink",
    "deathtouch", "reach", "defender", "flash", "double", "strike",
    "indestructible", "hexproof", "prowess", "ward", "crew", "equip",
    "cycling", "kicker", "scry", "surveil", "explore", "connive",
    "connives", "morph", "disguise", "ninjutsu", "mutate", "bestow",
    "suspend", "vanishing", "fading", "ripple", "storm", "dredge",
    "unearth", "annihilator", "bushido", "convoke", "delve", "devour",
    "exalted", "fear", "flanking", "intimidate", "persist", "undying",
    "wither", "infect", "proliferate", "populate", "fuse", "overload",
    "strive", "conspire", "cipher", "extort", "haunt", "bloodthirst",
    "graft", "evolve", "fabricate", "afflict", "aftermath", "boast",
    "cascade", "changeling", "dethrone", "foretell", "escape", "encore",
    "reconfigure", "offspring", "impending", "plot", "goad", "adapt",
    "battalion", "hideaway", "amplify", "heroic", "inspired", "landfall",
    "metalcraft", "raid", "revolt", "threshold", "embalm", "eternalize",
    "ascend", "surge", "spectacle", "riots", "chroma", "domain", "kinship",
    "splice", "transmute", "transfigure", "soulbond", "chronic",
}

# ponytail: module-level IDF cache — computed lazily on first similarity request.
_idf_cache = None
_idf_card_count = 0
_idf_version = 0  # bump when _extract_terms changes to force cache rebuild


def _get_idf():
    """Return {term: log(total_docs / doc_freq)} for all oracle terms.
    Computed once per server lifetime; rebuilt if card count drifts or
    extraction logic changes."""
    global _idf_cache, _idf_card_count, _idf_version
    import math
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    total = db.execute(
        "SELECT COUNT(DISTINCT name) FROM cards WHERE language='English' AND (side IS NULL OR side='a')"
    ).fetchone()[0]
    if _idf_cache is not None and abs(total - _idf_card_count) <= 100 and _idf_version >= 2:
        db.close()
        return _idf_cache

    text_rows = db.execute(
        "SELECT COALESCE(c.text, '') as text FROM cards c "
        "WHERE c.language='English' AND (c.side IS NULL OR c.side='a') "
        "GROUP BY c.name"
    ).fetchall()
    db.close()

    df = {}
    for row in text_rows:
        terms = _extract_terms(row["text"])
        for t in terms:
            df[t] = df.get(t, 0) + 1

    _idf_cache = {t: math.log(total / freq) for t, freq in df.items()}
    _idf_card_count = total
    _idf_version = 2
    return _idf_cache


def _extract_terms(text, mtg_filter=False):
    """Tokenize oracle text into significant lowercase terms.
    Strips reminder text and keyword-ability labels, filters stop words
    and short tokens.  When mtg_filter is True, also strips ubiquitous
    MTG-domain words."""
    if not text:
        return set()
    stripped = re.sub(r'\([^)]*\)', '', text)
    words = re.findall(r'[a-zA-Z]+', stripped.lower())
    stop_words = {
        'the', 'a', 'an', 'of', 'in', 'to', 'is', 'it', 'you', 'your', 'its',
        'that', 'this', 'and', 'or', 'for', 'on', 'at', 'be', 'by', 'as',
        'if', 'no', 'not', 'with', 'from', 'can', 'may', 'has', 'have',
        'are', 'was', 'were', 'been', 'each', 'any', 'all', 'their', 'they',
        'them', 'then', 'than', 'when', 'where', 'which', 'who', 'will', 'would',
        'put', 'onto', 'create', 'one', 'two', 'three',
        # ponytail: quantifiers/pronouns — carry zero functional signal.
        'many', 'gets', 'also', 'other', 'instead', 'another', 'those',
        'these', 'more', 'only', 'once',
    }
    if mtg_filter:
        stop_words |= MTG_STOP_WORDS

    terms = set()
    for w in words:
        if len(w) < 3:
            continue
        stem = w
        # ponytail: shallow plural stem — only strip a trailing 's' when both
        # forms are >= 4 chars and the word doesn't end in "ss" (e.g. "less").
        # Avoids truncating "this" → "thi", "gets" → "get".
        if w.endswith("s") and len(w) >= 5 and not w.endswith("ss") and not w.endswith("es"):
            stem = w[:-1]
        # Also handle "-ies" → "-y" (e.g. "counters" but not regular plurals)
        if w.endswith("ies") and len(w) >= 4:
            stem = w[:-3] + "y"
        if w in KEYWORD_TERMS or stem in KEYWORD_TERMS:
            continue
        if w in stop_words or stem in stop_words:
            continue
        terms.add(stem)
    return terms


def _split_csv(raw):
    """Split comma-separated string into lowercased set."""
    return {t.strip().lower() for t in (raw or "").split(",") if t.strip()}


def _gate(key, mismatch, factors):
    """1 / (1 + w * strictness * mismatch).
    Perfect match (mismatch=0) → gate=1.0. No penalty."""
    if mismatch <= 0:
        return 1.0
    w = float(factors.get(key.replace("s_", "w_"), 1))
    s = float(factors.get(key, 8.0))
    return 1.0 / (1.0 + w * s * mismatch)


def _score_similarity(base, candidate, factors, idf, mtg_filter=False):
    """Compute similarity score with multiplicative gating via geometric mean.

    Oracle text overlap is the base signal (IDF-weighted, [0,1]).
    Each active factor acts as a *gate* — a penalty curve that multiplies
    the oracle score. Gates are combined via geometric mean so no single
    mismatched factor zeros the score:

        final = oracle * (g1 * g2 * ... * gn) ** (1/n)

    Where each gate is:  1 / (1 + w * strictness * mismatch)

    Base card fields are pre-split by the caller (as _t, _kw, _sub, _st, _ci)
    to avoid re-splitting for every candidate.
    """
    # --- Oracle text terms (always on, IDF-weighted) ---
    oracle_score = 0.0
    base_terms = base["_terms"]
    base_idf_sum = base.get("_idf_sum", 0.0)
    cand_terms = _extract_terms(candidate["text"], mtg_filter=mtg_filter)
    if base_terms and base_idf_sum > 0:
        shared_idf_sum = sum(idf.get(t, 0) for t in (base_terms & cand_terms))
        oracle_score = shared_idf_sum / base_idf_sum

    # --- Oracle strictness: exponentiate the raw overlap ---
    s_oracle = float(factors.get("s_oracle", 1.0))
    oracle_score = oracle_score ** s_oracle

    gates = []

    # --- Types gate ---
    if factors.get("use_types"):
        base_t = base.get("_t")
        if base_t:
            cand_t = _split_csv(candidate["types"])
            jaccard = len(base_t & cand_t) / len(base_t | cand_t)
            gates.append(_gate("s_types", 1.0 - jaccard, factors))

    # --- Keywords gate ---
    if factors.get("use_keywords"):
        base_kw = base.get("_kw")
        if base_kw:
            cand_kw = _split_csv(candidate["keywords"])
            union = base_kw | cand_kw
            jaccard = len(base_kw & cand_kw) / len(union) if union else 0.0
            gates.append(_gate("s_keywords", 1.0 - jaccard, factors))

    # --- Subtypes gate ---
    if factors.get("use_subtypes"):
        base_sub = base.get("_sub")
        cand_sub = _split_csv(candidate["subtypes"])
        union = base_sub | cand_sub if base_sub else set()
        if union:
            jaccard = len(base_sub & cand_sub) / len(union)
            gates.append(_gate("s_subtypes", 1.0 - jaccard, factors))

    # --- Supertypes gate ---
    if factors.get("use_supertypes"):
        base_st = base.get("_st")
        if base_st:
            cand_st = _split_csv(candidate["supertypes"])
            union = base_st | cand_st
            jaccard = len(base_st & cand_st) / len(union) if union else 0.0
            gates.append(_gate("s_supertypes", 1.0 - jaccard, factors))

    # --- Mana value gate ---
    if factors.get("use_mv"):
        base_mv = base["manaValue"] or 0
        cand_mv = candidate["manaValue"] or 0
        gates.append(_gate("s_mv", abs(base_mv - cand_mv), factors))

    # --- Color identity gate ---
    if factors.get("use_color"):
        base_ci = base.get("_ci")
        if base_ci:
            cand_ci = _split_csv(candidate["colorIdentity"])
            # s >= 200 means the strict tier — CI is all-or-nothing.
            if float(factors.get("s_color", 8.0)) >= 200.0:
                if base_ci != cand_ci:
                    return 0.0
            else:  # moderate/loose: Jaccard gate, combined via GM with other factors
                jaccard = len(base_ci & cand_ci) / len(base_ci | cand_ci)
                gates.append(_gate("s_color", 1.0 - jaccard, factors))

    # --- Combine all gates via geometric mean ---
    if gates:
        gm = 1.0
        for g in gates:
            gm *= g
        gm = gm ** (1.0 / len(gates))
        oracle_score *= gm

    return oracle_score


def pagination_url(args, page):
    """Build URL with existing query params, replacing page."""
    parts = []
    for k, vals in args.lists():
        if k == "page":
            continue
        for v in vals:
            parts.append(f"{quote(k)}={quote(v)}")
    parts.append(f"page={page}")
    return "/search?" + "&".join(parts) if parts else "/search"

app.jinja_env.globals.update(card_image=card_image, render_mana=render_mana, pagination_url=pagination_url)

# --- Routes ---

@app.route("/")
def index():
    # Quick counts for homepage
    db = get_db()
    total = db.execute(
        "SELECT COUNT(DISTINCT name) FROM cards WHERE language='English' AND (side IS NULL OR side='a')"
    ).fetchone()[0]
    return render_template("index.html", total_cards=total)

def _n(op, field, cast=False):
    """Return SQL clause for a numeric operator + field. Handles =, <, >, <=, >=.
    If cast=True, wrap field in CAST(field AS INTEGER) for text columns like power/toughness."""
    ops = {"=": "=", "<": "<", ">": ">", "<=": "<=", ">=": ">="}
    if op in ops:
        col = f"CAST(c.{field} AS INTEGER)" if cast else f"c.{field}"
        return f"{col} {ops[op]} ?"

@app.route("/search")
def search():
    q = request.args.get("q", "").strip()                      # free-text name/type/text
    name = request.args.get("name", "").strip()                 # exact card name search
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
    per_page = 30

    db = get_db()

    where = ["c.language = 'English'", "(c.side IS NULL OR c.side = 'a')"]
    params = []

    # Free-text: search name + type + oracle text
    if q:
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("(c.name LIKE ? ESCAPE '\\' OR c.type LIKE ? ESCAPE '\\' OR c.text LIKE ? ESCAPE '\\')")
        params.extend([f"%{escaped}%"] * 3)

    # Exact name search (separate from free-text)
    if name:
        escaped = name.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("c.name LIKE ? ESCAPE '\\'")
        params.append(f"%{escaped}%")

    # Oracle text only
    if oracle:
        escaped = oracle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("c.text LIKE ? ESCAPE '\\'")
        params.append(f"%{escaped}%")

    # Full type line
    if type_line:
        escaped = type_line.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("c.type LIKE ? ESCAPE '\\'")
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
                params.append(", ".join(sorted(color_chars)))
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
    _MV_OPS = {"=", "<", ">", "<=", ">="}
    if mv:
        try:
            if mv_op in ("<", ">", "<=", ">="):
                val = float(mv)
                where.append(f"c.manaValue {mv_op} ?")
                params.append(val)
            elif re.match(r"^\s*\d+(\.\d+)?\s*-\s*\d+(\.\d+)?\s*$", mv):
                lo, hi = mv.split("-", 1)
                lo_val, hi_val = float(lo.strip()), float(hi.strip())
                where.append("c.manaValue BETWEEN ? AND ?")
                params.extend([lo_val, hi_val])
            elif mv_op == "=":
                val = float(mv)
                where.append("c.manaValue = ?")
                params.append(val)
        except (ValueError, TypeError):
            pass  # ponytail: invalid mv input → silently skip filter

    # Power (text column, needs CAST for comparisons)
    if pow_val:
        clause = _n(pow_op, "power", cast=True)
        if clause:
            where.append(clause)
            params.append(pow_val)

    # Toughness
    if tou_val:
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
                         "paupercommander","timeless","standardbrawl"}
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
    group_col = "c.name" if unique == "cards" else "c.uuid"
    count_sql = f"""
        SELECT COUNT(DISTINCT {group_col})
        FROM cards c
        {join_clause}
        {where_clause}
    """
    total = db.execute(count_sql, params).fetchone()[0]

    # Fetch
    data_sql = f"""
        SELECT c.name, c.manaCost, c.type, c.rarity, c.setCode, c.colors,
               ci.scryfallId, s.name as setName, s.releaseDate, c.number
        FROM cards c
        JOIN cardIdentifiers ci ON c.uuid = ci.uuid
        JOIN sets s ON c.setCode = s.code
        {join_clause}
        {where_clause}
        GROUP BY {group_col}
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
        results=results, page=page, total=total, total_pages=total_pages,
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
                      "score_filter", "score_op", "score_value",
                      "mtg_filter", "top_n", "tuned"):
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
        args_dict["score_filter"] = "1"
        args_dict["score_op"] = "greater_than"
        args_dict["score_value"] = "75"
        return redirect(url_for('similar_cards', set_code=set_code, number=number, **args_dict))

    # --- Parse factor toggles, weights, and strictness ---
    STRICT_VALUES = {"strict": 200.0, "moderate": 8.0, "loose": 1.5}
    COLOR_STRICT = {"strict": 200.0, "moderate": 6.0, "loose": 0.5}
    # Oracle uses exponentiation (overlap ** s), not the gate formula.
    # Exponent > 1 requires near-exact text match; 1.0 = raw IDF overlap.
    ORACLE_STRICT = {"strict": 5.0, "moderate": 2.0, "loose": 1.0}

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
        # ponytail: when weights hidden, defaults to 1.0 (already in _score_similarity)

    mtg_filter = request.args.get("mtg_filter") == "1"

    top_n = request.args.get("top_n", "5")
    if top_n not in ("5", "10", "15", "20"):
        top_n = "5"
    top_n = int(top_n)

    # --- Score threshold filter ---
    score_filter = request.args.get("score_filter", "0") == "1"
    score_op = request.args.get("score_op", "greater_than")
    try:
        score_value = float(request.args.get("score_value", "75"))
    except (ValueError, TypeError):
        score_value = 75.0

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
            for k in ("use_types", "use_keywords", "use_subtypes", "use_supertypes",
                      "use_mv", "use_color",
                      "s_types", "s_keywords", "s_subtypes", "s_supertypes",
                      "s_mv", "s_color", "s_oracle",
                      "w_types", "w_keywords", "w_subtypes", "w_supertypes",
                      "w_mv", "w_color", "use_weights",
                      "score_filter", "score_op", "score_value",
                      "mtg_filter", "top_n", "page", "tuned"):
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
    base_card["_terms"] = _extract_terms(base_card["text"], mtg_filter=mtg_filter)
    base_card["_t"] = _split_csv(base_card["types"])
    base_card["_kw"] = _split_csv(base_card["keywords"])
    base_card["_sub"] = _split_csv(base_card["subtypes"])
    base_card["_st"] = _split_csv(base_card["supertypes"])
    base_card["_ci"] = _split_csv(base_card["colorIdentity"])
    # ponytail: pre-compute IDF sum once, not per-candidate.
    idf = _get_idf()
    base_card["_idf_sum"] = sum(idf.get(t, 0) for t in base_card["_terms"])

    # --- Candidate query ---
    mv = base_card["manaValue"] or 0
    mv_min = max(0, mv - 3)
    mv_max = mv + 3

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
        scored.append((_score_similarity(base_card, c, factors, idf, mtg_filter=mtg_filter), c))
    scored.sort(key=lambda x: x[0], reverse=True)

    # --- Score threshold filter ---
    if score_filter and score_value > 0:
        threshold = score_value / 100.0
        if score_op == "greater_than":
            scored = [(s, c) for s, c in scored if s > threshold]
        else:
            scored = [(s, c) for s, c in scored if s < threshold]

    # --- Split top N from remaining ---
    top_results = scored[:top_n]
    remaining = scored[top_n:]

    # --- Export (self-contained HTML download) ---
    if request.args.get("export") == "1":
        rendered = render_template(
            "similar_export.html",
            card=base_card,
            top_results=top_results,
            top_n=top_n,
        )
        filename = f"similar-{base_card['name'].replace(' ', '-')}-top{top_n}.html"
        response = make_response(rendered)
        response.headers["Content-Type"] = "text/html; charset=utf-8"
        response.headers["Content-Disposition"] = f'attachment; filename="{filename}"'
        return response

    # --- Paginate remaining ---
    page = request.args.get("page", 1, type=int)
    per_page = 30
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
              "score_filter", "score_op", "score_value",
              "mtg_filter", "top_n", "tuned"):
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
        factors=factors,
        use_weights=use_weights,
        tuned=True,
        by_name=by_name,
        by_name_error=bool(by_name),
        mtg_filter=mtg_filter,
        tuning_params=tuning_params,
        top_n=top_n,
        score_filter=score_filter,
        score_op=score_op,
        score_value=score_value,
    )


# --- Web Search (SearXNG) ---

SEARXNG_URL = "http://localhost:8888"

def _web_search(query, max_results=10):
    """Query SearXNG, return list of {title, url, snippet} dicts."""
    import urllib.error
    try:
        url = f"{SEARXNG_URL}/search?q={quote(query)}&format=json&categories=general&language=en"
        req = Request(url, headers={"User-Agent": "mtg-search/1.0"})
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        return [
            {"title": r["title"], "url": r.get("url", ""),
             "snippet": r.get("content", "")[:500]}
            for r in data.get("results", [])[:max_results]
        ]
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        import sys
        print(f"web_search error: {e}", file=sys.stderr)
        return []


# --- Commander Eval ---

COMMANDER_SYSTEM_PROMPT = """You are an expert Magic: The Gathering deck builder specializing in the Commander format. Analyze the given legendary creature as a Commander and produce a structured JSON report.

Commander rules: 100-card singleton, commander starts in the command zone. Each time you cast your commander from the command zone after the first, it costs {2} more. 21 combat damage from a single commander kills a player. Color identity determines which cards are legal.

The user prompt includes web_research — search results from deck guides, strategy discussions, community reviews, and mechanic rules for this commander. The commander object also includes official Oracle rulings (cardRulings). Use rulings as the definitive mechanical interpretation — they resolve ambiguities in the card text. Use web research to inform your strategic analysis. Treat web research as community consensus and lived experience, not as rules text.

If the card has leadershipSkills (partner, background, doctor's companion, friends forever, choose a background, etc.), analyze how those mechanics affect deck-building — partner pairs expand color identity, backgrounds add a second card choice, etc.

When analyzing, reference specific mechanics from the card's oracle text. Never invent abilities the card does not have. If the commander's ability targets or interacts with a specific zone (graveyard, library, battlefield, exile, hand, command zone), name that zone exactly — do not confuse zones. Never mention other Magic card names in strategies or descriptions. Describe synergies in terms of card types, abilities, and mechanics, not individual cards.

If the card has keywords (Flying, Ward, etc.), explain how they affect Commander play. Consider how the commander's color identity shapes the card pool available to it and how that interacts with the commander's specific oracle text — the analysis should reflect the intersection of color access and the commander's unique mechanics, not either in isolation. If EDHREC rank is provided, note what it implies about popularity. If salt score is high, note why players find it frustrating.

Commander bracket system (new official power level tiers):
- Bracket 1 (Exhibition): Ultra-casual, theme/joke/meme decks. Winning is not the goal — the experience is. Jank builds, chair tribal, ladies looking left.
- Bracket 2 (Core): Average precon power level. Casual play with some synergy and a clear game plan, but minimal tutors, efficient combos, or fast mana.
- Bracket 3 (Upgraded): Tuned decks beyond precons. Game Changer cards allowed (max 3). Stronger synergies, more efficient interaction, but not fully optimized.
- Bracket 4 (Optimized): High power. No restrictions beyond the banlist. Fast mana, efficient tutors, compact combos. Not quite cEDH but pushing the ceiling.
- Bracket 5 (cEDH): Competitive EDH. Win-at-all-costs. Fully optimized lists, meta-driven choices, every card is the most efficient version of its effect.

For strengths/weaknesses, list 3–5 each as an array of strings. Each must cite a concrete mechanic or interaction.
For strategies, list 2–4 strategies each with a name and a description tied to the commander's specific abilities.
For priorities, list 3–5 deck-building priority categories (e.g. Card Draw, Ramp, Sacrifice Outlets, Protection, Recursion, Removal, Token Generators, etc.) ranked from most to least important for this specific commander. Each must include a "category" and a "reason" explaining why this category is critical given the commander's abilities and color identity.
For unique_builds, list 1–3 unconventional ways to build this commander — strategies that deviate from the obvious or most popular approach. Look to the unique_archetypes search results and to under-exploited angles in the card's oracle text. Each must include a "name" and a "description" explaining the off-meta angle and why it works. If the research reveals no viable off-meta builds, return an empty array.

For brackets, analyze how effective this commander would be in each bracket (1 through 5). Some commanders scale well across brackets, others peak at a specific power level. Consider: how well the commander's abilities scale with better card quality, whether the strategy requires cards only available at higher brackets, and whether the commander is too oppressive for lower brackets or too slow for cEDH. Each bracket entry must include:
  - "bracket": the bracket number (1-5)
  - "label": the bracket name (e.g. "Exhibition", "Core", "Upgraded", "Optimized", "cEDH")
  - "effectiveness": a rating string ("Very Weak", "Weak", "Average", "Strong", "Very Strong", "Dominant")
  - "reasoning": 1-2 sentences explaining why this commander performs at that level in this bracket
  - "kos_score": integer 1-10 representing the kill-on-sight threat level in this specific bracket (1 = ignored, 10 = remove immediately or lose). Vary by bracket — a commander that dominates casual tables may be a lower priority at cEDH tables where faster threats exist.
  - "kos_note": 1 sentence explaining why the kill-on-sight score is what it is in this bracket

For kill_on_sight, provide a single summary rating representing the commander's default kill-on-sight reputation at a typical LGS table (brackets 2-3). Include:
  - "score": integer 1-10 (1 = ignored, 10 = remove immediately or lose).
  - "reasoning": 1-2 sentences explaining the default score. Reference the commander's mechanics — does it generate immediate value? Win the game if untapped with? Shut down opponents' strategies?

Output ONLY valid JSON with these exact keys:
{
  "strengths": ["..."],
  "weaknesses": ["..."],
  "strategies": [{"name": "...", "description": "..."}],
  "priorities": [{"category": "...", "reason": "..."}],
  "unique_builds": [{"name": "...", "description": "..."}],
  "brackets": [
    {
      "bracket": 1,
      "label": "Exhibition",
      "effectiveness": "Weak",
      "reasoning": "...",
      "kos_score": 4,
      "kos_note": "..."
    }
  ],
  "kill_on_sight": {
    "score": 7,
    "reasoning": "..."
  }
}

brackets must have exactly 5 entries, one per bracket (1-5)."""

EXPAND_SYSTEM_PROMPT = """You are an expert Magic: The Gathering deck builder specializing in the Commander format. Deep-dive on a specific {type_label} for this commander.

Produce a detailed analysis of this specific approach — not the commander in general. Focus exclusively on what makes this {type_label} work:
- Which card types and mechanics does it leverage?
- What are the key enablers and payoffs?
- How does it win games?

Remember: never mention other Magic card names. Describe synergies in terms of mechanics, card types, and abilities.

Output ONLY valid JSON with these exact keys:
{{
  "strengths": ["3-5 strings, each citing a mechanic or interaction specific to this {type_label}"],
  "weaknesses": ["3-5 strings, each citing a vulnerability or counter specific to this {type_label}"],
  "priorities": [{{"category": "string", "reason": "string, why this is critical for this {type_label}"}}],
  "win_conditions": [{{"name": "string", "description": "string, how this approach closes out games"}}]
}}"""


# ponytail: proxy /models so the browser can fetch from self-hosted endpoints
# without CORS issues. Only used for OpenAI-compatible backends.
# Tries OpenAI /v1/models first, then Ollama /api/tags as fallback.

def _fetch_models(base_url, api_key):
    """Return [{"id": ..., "name": ...}] from the endpoint. Tries both
    OpenAI /v1/models and Ollama /api/tags formats."""
    import urllib.request
    urls = [
        urljoin(base_url.rstrip("/") + "/", "models"),
        urljoin(base_url.rstrip("/") + "/", "api/tags"),
    ]
    last_err = None
    for url in urls:
        try:
            req = urllib.request.Request(url)
            if api_key:
                req.add_header("Authorization", f"Bearer {api_key}")
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())
                # OpenAI format: {"object": "list", "data": [{"id": "gpt-4o", ...}]}
                if "data" in data and isinstance(data["data"], list):
                    return sorted(
                        [{"id": m["id"], "name": m["id"]} for m in data["data"] if m.get("id")],
                        key=lambda x: x["id"],
                    )
                # Ollama format: {"models": [{"name": "llama3.1:8b", ...}]}
                if "models" in data and isinstance(data["models"], list):
                    return sorted(
                        [{"id": m["name"], "name": m["name"]} for m in data["models"] if m.get("name")],
                        key=lambda x: x["id"],
                    )
        except Exception as e:
            last_err = e
    raise last_err or RuntimeError("No models found at base URL")


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
        if request.form.get("save_expand_prompt") == "1":
            expand_prompt = request.form.get("expand_prompt", "").strip()
            if expand_prompt:
                session["expand_prompt"] = expand_prompt
            elif "expand_prompt" in session:
                session.pop("expand_prompt", None)
        return redirect(url_for("config_page"))

    llm_backend = (session.get("llm_backend") or os.environ.get("LLM_BACKEND", "openai"))
    llm_base_url = (session.get("llm_base_url") or os.environ.get("LLM_BASE_URL", ""))
    llm_model = (session.get("llm_model") or os.environ.get("LLM_MODEL", ""))
    llm_has_key = bool(session.get("llm_api_key") or os.environ.get("LLM_API_KEY"))
    eval_prompt = session.get("eval_prompt", "")
    expand_prompt = session.get("expand_prompt", "")

    last_ingest = None
    _ingest_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".last_ingest.json")
    try:
        with open(_ingest_path) as f:
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

    return render_template("config.html",
                           llm_backend=llm_backend,
                           llm_base_url=llm_base_url,
                           llm_model=llm_model,
                           llm_has_key=llm_has_key,
                           eval_prompt=eval_prompt,
                           expand_prompt=expand_prompt,
                           default_prompt=COMMANDER_SYSTEM_PROMPT,
                           default_expand_prompt=EXPAND_SYSTEM_PROMPT,
                           last_ingest=last_ingest,
                           db_card_count=db_card_count)


@app.route("/config/ingest", methods=["POST"])
def ingest_database():
    """Accept and ingest a new SQLite database file.

    Supports .sqlite, .gz, .bz2, .xz, and .zip. Replaces the active
    DATABASE atomically.
    """
    if "database" not in request.files:
        return jsonify({"error": "No file uploaded."}), 400

    f = request.files["database"]
    if not f.filename:
        return jsonify({"error": "No file selected."}), 400

    filename = f.filename
    tmpdir = None
    tmp_sqlite = None
    tmp_upload = None
    tmp_decompressed = None

    try:
        # Save upload to temp file
        tmp_upload = tempfile.NamedTemporaryFile(delete=False)
        f.save(tmp_upload)
        tmp_upload.close()

        ext = os.path.splitext(filename)[1].lower()
        # .tgz is short for .tar.gz
        if ext == ".tgz":
            ext = ".gz"

        if ext == ".sqlite":
            tmp_sqlite = tmp_upload.name

        elif ext in (".gz", ".bz2", ".xz"):
            tmp_decompressed = tmp_upload.name + ".raw"
            if ext == ".gz":
                opener = gzip.open
            elif ext == ".bz2":
                opener = bz2.open
            else:  # .xz
                opener = lzma.open
            # ponytail: stream decompression — reading 650MB+ into RAM is OOM bait
            with opener(tmp_upload.name, "rb") as src, open(tmp_decompressed, "wb") as dst:
                shutil.copyfileobj(src, dst, length=1 << 20)

            # Try as raw SQLite first
            valid_db = sqlite3.connect(tmp_decompressed)
            try:
                valid_db.execute("SELECT COUNT(*) FROM cards WHERE language = 'English'").fetchone()
                tmp_sqlite = tmp_decompressed
            except sqlite3.DatabaseError:
                valid_db.close()
                # Not SQLite — try tar (MTGJSON ships as AllPrintings.tar.gz)
                tmpdir = tempfile.mkdtemp()
                try:
                    # ponytail: "r:" — file is already decompressed, no compression suffix
                    with tarfile.open(tmp_decompressed, "r:") as tf:
                        sqlite_member = None
                        for m in tf.getmembers():
                            if m.name.lower().endswith(".sqlite") or m.name.lower().endswith(".db"):
                                sqlite_member = m
                                break
                        if not sqlite_member:
                            names = [m.name for m in tf.getmembers()[:20]]
                            return jsonify({"error": f"No .sqlite file found inside the archive. Contents: {names}"}), 400
                        tf.extract(sqlite_member, tmpdir)
                        tmp_sqlite = os.path.join(tmpdir, sqlite_member.name)
                except tarfile.TarError as te:
                    # Sniff first bytes for debugging (before unlinking)
                    try:
                        with open(tmp_decompressed, "rb") as peek:
                            first_bytes = peek.read(64).hex()
                    except OSError:
                        first_bytes = "could not read"
                    return jsonify({
                        "error": f"Decompressed payload is not SQLite or tar (tar error: {te}). "
                                 f"First 64 bytes (hex): {first_bytes}"
                    }), 400
            else:
                valid_db.close()

        elif ext == ".zip":
            tmpdir = tempfile.mkdtemp()
            with zipfile.ZipFile(tmp_upload.name, "r") as zf:
                members = [m for m in zf.namelist() if m.lower().endswith(".sqlite")]
                if not members:
                    return jsonify({"error": "No .sqlite file found inside the zip archive."}), 400
                zf.extract(members[0], tmpdir)
                tmp_sqlite = os.path.join(tmpdir, members[0])

        else:
            return jsonify({"error": f"Unsupported file type: {ext}. Accepted: .sqlite, .gz, .bz2, .xz, .zip"}), 400

        # Validate: must be a SQLite DB with a cards table
        valid_db = sqlite3.connect(tmp_sqlite)
        try:
            cnt = valid_db.execute("SELECT COUNT(*) FROM cards WHERE language = 'English'").fetchone()[0]
        except sqlite3.OperationalError:
            return jsonify({"error": "File is not a valid MTGJSON SQLite database (no cards table)."}), 400
        finally:
            valid_db.close()

        if cnt < 1000:
            return jsonify({"error": f"Database has only {cnt} English cards — this doesn't look like a complete MTGJSON export."}), 400

        # Replace the active database — shutil.move handles cross-device (/tmp → project dir)
        shutil.move(tmp_sqlite, DATABASE)

        # Persist ingest timestamp
        now_iso = datetime.now(timezone.utc).isoformat()
        ingest_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".last_ingest.json")
        with open(ingest_path, "w") as out:
            json.dump({"timestamp": now_iso, "filename": filename, "cards": cnt}, out)

        return jsonify({"success": True, "cards": cnt, "timestamp": now_iso})

    except Exception as e:
        import sys
        import traceback
        tb = traceback.format_exc()
        traceback.print_exc(file=sys.stderr)
        try:
            os.unlink(tmp_sqlite) if tmp_sqlite and os.path.exists(tmp_sqlite) else None
        except Exception:
            pass
        return jsonify({"error": str(e), "traceback": tb}), 500

    finally:
        # Clean up temp files (tmp_sqlite may have been os.replace'd already)
        try:
            if tmp_upload and os.path.exists(tmp_upload.name):
                os.unlink(tmp_upload.name)
        except Exception:
            pass
        try:
            if tmp_sqlite and os.path.exists(tmp_sqlite):
                os.unlink(tmp_sqlite)
        except Exception:
            pass
        try:
            if tmp_decompressed and os.path.exists(tmp_decompressed):
                os.unlink(tmp_decompressed)
        except Exception:
            pass
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)


@app.route("/llm-models")
def llm_models_proxy():
    base_url = request.args.get("base_url", "").strip()
    if not base_url:
        return jsonify({"models": [], "error": "No base_url provided"})

    api_key = session.get("llm_api_key") or os.environ.get("LLM_API_KEY", "")
    try:
        import urllib.request
        models = _fetch_models(base_url, api_key)
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
        SELECT c.name, c.setCode, c.number, s.name as setName, s.releaseDate
        FROM cards c
        JOIN sets s ON c.setCode = s.code
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
            if not fname.endswith(".json") or fname == ".gitkeep":
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
    analysis = None if stale else cached.get("analysis")
    error = None if stale else cached.get("error")

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

    # If no fresh analysis, seed eval_key + _eval_cache so expand/save routes work
    if analysis and not session.get("eval_key"):
        session["eval_key"] = str(uuid.uuid4())
        _cache_put(session["eval_key"], {"_card": f"{set_code}/{number}", "analysis": analysis})
        if report_data:
            _eval_cache[session["eval_key"]]["expands"] = report_data.get("expands", {})
            _eval_cache[session["eval_key"]]["similar"] = report_data.get("similar", [])

    similar = (report_data or {}).get("similar") if loaded_from else cached.get("similar")

    return render_template(
        "commander_eval.html",
        card=card,
        is_commander=is_commander,
        isGameChanger=is_game_changer,
        analysis=analysis,
        error=error,
        by_name=by_name,
        by_name_error=bool(by_name),
        loaded_from=loaded_from,
        saved_expands=report_data.get("expands", {}) if report_data else cached.get("expands", {}),
        similar=similar,
    )


@app.route("/card/<set_code>/<number>/eval/analyze", methods=["POST"])
def commander_eval_analyze(set_code, number):
    """Run analysis pipeline via AJAX, store result in session."""
    db = get_db()

    llm_backend = (session.get("llm_backend") or os.environ.get("LLM_BACKEND", "openai"))
    llm_base_url = (session.get("llm_base_url") or os.environ.get("LLM_BASE_URL", ""))
    llm_model = (session.get("llm_model") or os.environ.get("LLM_MODEL", ""))

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

    try:
        # Fetch official rulings for this card
        rulings = db.execute(
            "SELECT text FROM cardRulings WHERE uuid = ? ORDER BY date DESC",
            [card["uuid"]]
        ).fetchall()
        rulings_texts = [r["text"] for r in rulings]

        card_data = {
            "name": card["name"],
            "manaCost": card.get("manaCost", ""),
            "manaValue": card.get("manaValue"),
            "type": card["type"],
            "text": card.get("text", ""),
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

        name = card["name"]
        keywords = card.get("keywords", "")

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
            searches[search_label] = _web_search(query, max_results=5)

        user_prompt = json.dumps({
            "commander": card_data,
            "web_research": searches,
        }, indent=2)

        if llm_generate is None:
            raise RuntimeError("LLM dependencies not installed. Run: pip install openai anthropic")

        api_key = session.get("llm_api_key") or os.environ.get("LLM_API_KEY")
        commander_prompt = session.get("eval_prompt") or COMMANDER_SYSTEM_PROMPT
        analysis = llm_generate(
            commander_prompt, user_prompt,
            backend=llm_backend,
            api_key=api_key,
            base_url=llm_base_url or None,
            model=llm_model or None,
        )

        # Second-pass verification — check for invented abilities, wrong zones, hallucinated card names
        verify_prompt = """You are a Magic: The Gathering rules judge. Verify an AI-generated Commander analysis against the card's actual Oracle text and official rulings.

Check for these specific factual errors:
1. **Invented abilities**: The analysis describes a mechanic the card does not actually have.
2. **Wrong zone references**: The card interacts with a specific zone (graveyard, library, battlefield, exile, hand, command zone, stack) and the analysis names the wrong one.
3. **Hallucinated card names**: The analysis mentions another specific Magic card by name (the analysis was forbidden from doing this).
4. **Misstated mechanics**: A keyword or ability is described incorrectly (e.g. saying "Ward {2} counters spells" when Ward triggers on targeting).
5. **Color identity errors**: The analysis recommends cards or strategies outside the commander's color identity.

Only flag factual rules errors. Do NOT flag:
- Strategic opinions or card evaluations you disagree with
- Wording style or phrasing preferences
- Edhrec rank or salt score interpretations
- Missing strategies (incompleteness is not an error)

Output ONLY valid JSON:
{"warnings": ["warning 1", "warning 2", ...], "verified": true}"""

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

        session["eval_key"] = str(uuid.uuid4())
        _cache_put(session["eval_key"], {"_card": f"{set_code}/{number}", "analysis": analysis})
        return {"success": True}

    except Exception as e:
        import traceback
        traceback.print_exc()
        session["eval_key"] = str(uuid.uuid4())
        _cache_put(session["eval_key"], {"_card": f"{set_code}/{number}", "error": str(e)})
        return {"error": str(e)}, 500


@app.route("/card/<set_code>/<number>/eval/expand", methods=["POST"])
def commander_eval_expand(set_code, number):
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

    expand_prompt = session.get("expand_prompt") or EXPAND_SYSTEM_PROMPT
    system_prompt = expand_prompt.replace("{type_label}", type_label)

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

    if llm_generate is None:
        return jsonify({"error": "LLM dependencies not installed."}), 500

    try:
        api_key = session.get("llm_api_key") or os.environ.get("LLM_API_KEY")
        data = llm_generate(
            system_prompt, user_prompt,
            backend=llm_backend,
            api_key=api_key,
            base_url=llm_base_url or None,
            model=llm_model or None,
        )

        # Stash expand result so it gets included when the report is saved
        eval_key = session.get("eval_key")
        if eval_key and eval_key in _eval_cache:
            cache_entry = _eval_cache[eval_key]
            if "expands" not in cache_entry:
                cache_entry["expands"] = {}
            cache_entry["expands"][f"{expand_type}:{expand_name}"] = data

        return jsonify({"success": True, "data": data})
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route("/card/<set_code>/<number>/eval/similar", methods=["POST"])
def commander_eval_similar(set_code, number):
    """Find similar legendary creatures (Commander-legal) with strictness-loosening loop."""
    db = get_db()
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
    base = dict(card)
    base["_t"] = _split_csv(base["types"])
    base["_kw"] = _split_csv(base["keywords"])
    base["_sub"] = _split_csv(base["subtypes"])
    base["_st"] = _split_csv(base["supertypes"])
    base["_ci"] = _split_csv(base["colorIdentity"])

    # Fetch candidates: legendary creatures, Commander-legal, within mana-value ±3
    mv = base["manaValue"] or 0
    mv_min = max(0, mv - 3)
    mv_max = mv + 3
    candidates = db.execute("""
        SELECT c.name, c.manaCost, c.manaValue, c.type, c.types, c.supertypes,
               c.subtypes, c.keywords, c.text, c.colors, c.colorIdentity,
               c.setCode, c.number, ci.scryfallId, s.name as setName
        FROM cards c
        JOIN cardIdentifiers ci ON c.uuid = ci.uuid
        JOIN sets s ON c.setCode = s.code
        JOIN cardLegalities cl ON c.uuid = cl.uuid
        WHERE c.language = 'English'
          AND (c.side IS NULL OR c.side = 'a')
          AND c.uuid != ?
          AND c.name != ?
          AND c.supertypes LIKE '%Legendary%'
          AND cl.commander = 'Legal'
          AND (c.manaValue BETWEEN ? AND ? OR c.manaValue IS NULL)
        GROUP BY c.name
        ORDER BY s.releaseDate DESC
    """, [base["uuid"], base["name"], mv_min, mv_max]).fetchall()

    idf = _get_idf()

    # Strictness-loosening rounds: (s_oracle, general_strictness, color_strictness, score_threshold, label)
    rounds = [
        (5.0, 200.0, 200.0, 0.75, "Strict"),
        (2.0, 8.0, 6.0, 0.60, "Moderate"),
        (1.0, 1.5, 0.5, 0.40, "Loose"),
        (0.5, 0.5, 0.5, 0.0, "Very Loose"),
    ]
    factors_on = {
        "use_types": True, "use_keywords": True, "use_subtypes": True,
        "use_supertypes": True, "use_mv": True, "use_color": True,
    }
    scored = []
    progress = []

    for s_oracle, strict, color_strict, threshold, label in rounds:
        base["_terms"] = _extract_terms(base["text"], mtg_filter=True)
        base["_idf_sum"] = sum(idf.get(t, 0) for t in base["_terms"])
        factors = {
            **factors_on,
            "s_oracle": s_oracle,
            "s_types": strict, "s_keywords": strict, "s_subtypes": strict,
            "s_supertypes": strict, "s_mv": strict,
            "s_color": color_strict,
        }
        scored = []
        for c in candidates:
            scored.append((_score_similarity(base, c, factors, idf, mtg_filter=True), c))
        scored.sort(key=lambda x: x[0], reverse=True)
        if threshold > 0:
            scored = [(s, c) for s, c in scored if s > threshold]
        progress.append({"round": len(progress) + 1, "label": label, "count": len(scored)})
        if len(scored) >= 10:
            break

    top = scored[:10]
    results = []
    for score, c in top:
        results.append({
            "name": c["name"],
            "setCode": c["setCode"],
            "number": c["number"],
            "scryfallId": c["scryfallId"],
            "score": round(score * 100, 1),
            "manaCost": c["manaCost"] or "",
            "type": c["type"] or "",
        })

    # ponytail: stash in cache so save picks it up
    eval_key = session.get("eval_key")
    if eval_key and eval_key in _eval_cache:
        _eval_cache[eval_key]["similar"] = results

    return jsonify({"success": True, "results": results, "progress": progress})


@app.route("/card/<set_code>/<number>/eval/save", methods=["POST"])
def commander_eval_save(set_code, number):
    """Save the current analysis from cache to disk."""
    eval_key = session.get("eval_key")
    if not eval_key:
        return jsonify({"error": "No active analysis to save."}), 400

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
    with open(report_path, "w") as f:
        json.dump({
            "card": dict(card),
            "analysis": cached["analysis"],
            "expands": cached.get("expands", {}),
            "similar": cached.get("similar", []),
            "saved_at": now_iso,
        }, f, indent=2)

    return jsonify({"success": True, "filename": safe_name})


@app.route("/commander-eval/reports")
def commander_eval_reports():
    """List saved eval reports as JSON."""
    reports_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), REPORTS_DIR)
    if not os.path.isdir(reports_dir):
        return jsonify([])

    reports = []
    for fname in sorted(os.listdir(reports_dir), reverse=True):
        if not fname.endswith(".json") or fname == ".gitkeep":
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


# --- Run ---

if __name__ == "__main__":
    app.run(debug=True)
