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
import threading
from datetime import datetime, timezone
from collections import OrderedDict
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen
from flask import Flask, g, render_template, request, abort, redirect, url_for, make_response, session, jsonify, send_file

# ponytail: optional imports — only needed for commander eval / db ingest
try:
    from llm import generate as llm_generate
except ImportError:
    llm_generate = None
import dedup
import embed

app = Flask(__name__)

# ponytail: persist secret key so sessions survive restarts
_secret_key = os.environ.get("SECRET_KEY")
if not _secret_key:
    _key_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".secret_key")
    try:
        with open(_key_path, "rb") as f:
            _secret_key = f.read()
    except (FileNotFoundError, IsADirectoryError):
        # ponytail: IsADirectoryError — Docker creates a directory for missing
        # bind-mount files. Treat it the same as missing.
        _secret_key = os.urandom(24)
        # ponytail: writeback may fail if _key_path is a Docker bind-mount directory.
        # Sessions won't survive restarts in that case, but the app still works.
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
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' https: data:; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; script-src 'self' 'unsafe-inline'; font-src 'self' https://cdn.jsdelivr.net"
    return response

# ponytail: server-side analysis cache — Flask cookie sessions cap at ~4KB,
# expanded bracket+kill-on-sight output exceeds that.
# ponytail: FIFO-capped at 100 entries, add LRU if eviction order matters.
# ponytail: _cache_lock guards mutations; dev server is single-thread but any
# threaded WSGI deployment needs it.  Reads (get) are atomic dict lookups, ok unlocked.
_eval_cache = OrderedDict()  # {id: {"analysis": {...}, "error": "..."}}
_cache_lock = threading.Lock()

_progress_cache = {}  # {id: {"step": N, "total": 6, "label": "..."}}

def _cache_put(key, entry):
    """Store in eval cache, evicting oldest if over cap."""
    with _cache_lock:
        _eval_cache[key] = entry
        while len(_eval_cache) > 100:
            _eval_cache.popitem(last=False)

DATABASE = "AllPrintings.sqlite"
REPORTS_DIR = "eval_reports"
IMAGE_DIR = "images"


# --- DB helpers ---

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA query_only = ON")
    return g.db


def _db_ready():
    """Return True if the cards table exists with at least one row."""
    try:
        db = get_db()
        row = db.execute("SELECT COUNT(*) FROM cards WHERE language='English' LIMIT 1").fetchone()
        return row is not None and row[0] > 0
    except sqlite3.OperationalError:
        return False

@app.teardown_appcontext
def close_db(e=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()

# --- Image URL ---

def card_image(scryfall_id, face="front", size="normal"):
    """Local image URL — served by proxy route, which caches from Scryfall on first hit."""
    if not scryfall_id:
        return ""
    c1, c2 = scryfall_id[0], scryfall_id[1]
    return f"/img/{size}/{face}/{c1}/{c2}/{scryfall_id}.jpg"


@app.route("/img/<size>/<face>/<c1>/<c2>/<scryfall_id>.jpg")
def serve_image(size, face, c1, c2, scryfall_id):
    """Lazy cache-through proxy. First hit fetches from Scryfall and writes to disk."""
    full_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        IMAGE_DIR, size, face, c1, c2, f"{scryfall_id}.jpg",
    )
    if os.path.isfile(full_path):
        return send_file(full_path)

    # ponytail: fetch from Scryfall, cache, serve. Single urlopen, no retry.
    try:
        scryfall_url = f"https://cards.scryfall.io/{size}/{face}/{c1}/{c2}/{scryfall_id}.jpg"
        req = Request(scryfall_url, headers={"User-Agent": "mtg-search/1.0"})
        with urlopen(req, timeout=10) as resp:
            img_data = resp.read()
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "wb") as f:
            f.write(img_data)
        return send_file(full_path)
    except Exception:
        pass
    abort(404)


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

def similarity_label(score, best_score=None):
    """Rank-based label: score relative to the best match for this card.

    Without best_score, uses absolute thresholds (legacy fallback).
    With best_score, uses relative ranking — the top match is always meaningful."""
    if best_score and best_score > 0:
        pct = score / best_score
        if pct >= 0.85:
            return "Near Perfect"
        elif pct >= 0.65:
            return "Strong"
        elif pct >= 0.40:
            return "Good"
        elif pct >= 0.15:
            return "Bad"
        return "Ignore"
    # ponytail: absolute fallback — used when best_score is unavailable
    if score >= 45:
        return "Near Perfect"
    elif score >= 35:
        return "Strong"
    elif score >= 25:
        return "Good"
    elif score >= 15:
        return "Bad"
    return "Ignore"

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
        cand_ci = _split_csv(candidate["colorIdentity"])
        # s >= 200 means the strict tier — CI is all-or-nothing.
        if float(factors.get("s_color", 8.0)) >= 200.0:
            if base_ci != cand_ci:
                return 0.0
        else:  # moderate/loose: Jaccard gate, combined via GM with other factors
            union = base_ci | cand_ci
            if union:
                jaccard = len(base_ci & cand_ci) / len(union)
                gates.append(_gate("s_color", 1.0 - jaccard, factors))
            # ponytail: both colorless → perfect CI match, no gate needed

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

# ponytail: WUBRG canonical order — MTGJSON stores colors in this sequence, not alphabetical.
_WUBRG_ORDER = {c: i for i, c in enumerate("WUBRG")}

def _wubrg_sort(chars):
    """Sort color chars in canonical WUBRG order."""
    return sorted(chars, key=lambda c: _WUBRG_ORDER.get(c, 99))

def _normalize_dash(s):
    """Replace Unicode dashes with ASCII hyphen. MTGJSON uses U+2014 em-dash in types."""
    return s.replace('—', '-').replace('–', '-').replace('―', '-')

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
    if page < 1:
        page = 1
    per_page = 30

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
            results=[], page=page, total=0, total_pages=0,
            db_unseeded=True,
        )

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
        # ponytail: normalize dashes — DB stores U+2014 em-dash but users type '-'.
        # REPLACE on the DB column side so we don't fight LIKE escaping.
        normalized = _normalize_dash(type_line)
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
                params.append(", ".join(_wubrg_sort(color_chars)))
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
                      "method",
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
        # ponytail: when weights hidden, defaults to 1.0 (already in _score_similarity)

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
            candidates = embed.find(DATABASE, base_card.get("text") or "", top_k=200)
            candidates = [c for c in candidates if c["name"] != base_card["name"]]
            for c in candidates:
                c_mv = c.get("manaValue")
                if c_mv is not None and (c_mv < mv_min or c_mv > mv_max):
                    continue
                # --- Factor-based post-filters ---
                if factors.get("use_color"):
                    cand_ci = _split_csv(c.get("colorIdentity") or "")
                    base_ci = base_card.get("_ci")
                    if base_ci != cand_ci:
                        continue
                if factors.get("use_types"):
                    cand_t = _split_csv(c.get("types") or "")
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
            scored.append((_score_similarity(base_card, c, factors, idf, mtg_filter=mtg_filter), c))
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
        method=method,
        best_score=best_score,
    )


# --- Web Search (SearXNG) ---

SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://localhost:8888")

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

# ponytail: helper — a card is legal in the 99 only if every color in its
# color identity is also in the commander's color identity. Colorless cards
# (empty CI) are legal everywhere.
def _color_identity_legal(card_ci, commander_ci):
    """Return True if card_ci is a subset of commander_ci."""
    if not card_ci or not card_ci.strip():
        return True
    if not commander_ci or not commander_ci.strip():
        return bool(not card_ci or not card_ci.strip())
    card_colors = set(card_ci.replace(" ", ""))
    commander_colors = set(commander_ci.replace(" ", ""))
    return card_colors <= commander_colors


def _is_placeholder_name(name):
    """ponytail: catch LLM placeholder names like 'Card Name Placeholder'.
    Returns True if the name looks like a placeholder, not a real card."""
    if not name:
        return True
    lower = name.lower()
    placeholder_markers = [
        "placeholder", "card name", "[placeholder", "[card name",
        "token generator", "sacrifice outlet", "mana dork", "draw engine",
        "removal spell", "counterspell", "board wipe", "ramp spell",
    ]
    return any(m in lower for m in placeholder_markers)


COMMANDER_SYSTEM_PROMPT = """You are an expert Magic: The Gathering deck builder specializing in the Commander format. Analyze the given legendary creature as a Commander and produce a structured JSON report.

Commander rules: 100-card singleton, commander starts in the command zone. Each time you cast your commander from the command zone after the first, it costs {2} more. 21 combat damage from a single commander kills a player. Color identity determines which cards are legal.

The user prompt includes web_research — search results from deck guides, strategy discussions, community reviews, and mechanic rules for this commander. The commander object also includes official Oracle rulings (cardRulings). Use rulings as the definitive mechanical interpretation — they resolve ambiguities in the card text. Use web research to inform your strategic analysis. Treat web research as community consensus and lived experience, not as rules text.

The user prompt may also include similar_cards — cards whose oracle text is mechanically similar to the commander's abilities, ranked by cosine similarity. These are real Magic cards that share mechanical DNA with the commander. When available, use them to ground your analysis: they represent cards that naturally fit in the 99. Reference mechanics and card types that appear in the similar_cards list rather than inventing synergies from scratch. You may name specific cards from the similar_cards list — but only cards that appear in that list.

If the card has leadershipSkills (partner, background, doctor's companion, friends forever, choose a background, etc.), analyze how those mechanics affect deck-building — partner pairs expand color identity, backgrounds add a second card choice, etc.

When analyzing, reference specific mechanics from the card's oracle text. Never invent abilities the card does not have. If the commander's ability targets or interacts with a specific zone (graveyard, library, battlefield, exile, hand, command zone), name that zone exactly — do not confuse zones. Do not mention other card names unless they appear in the similar_cards field. Describe synergies in terms of card types, abilities, and mechanics, not individual cards, unless the card is listed in similar_cards.

Parse abilities literally and preserve every qualifier. Trigger conditions are gated by their exact wording — "if you cast it" excludes tokens and copies; "from your hand" excludes the graveyard; "during an opponent's turn" excludes your own turn; "nontoken" excludes tokens; "one or more" does not mean "each"; "opponent controls" excludes your own permanents; "may" means the effect is optional. Dropping or broadening a qualifier changes what the card does. When describing what triggers an ability, quote or closely paraphrase the trigger condition.

Use precise card-economy language. Card advantage starts with more resources than you had before: drawing extra cards, tutoring to hand, or putting a card onto the battlefield from hand without casting it. Repeatable recursion from graveyard or exile (where you deploy a card again without spending a new one from your library) is virtual card advantage — you gain access to already-used resources without using new cards. Return-to-hand effects that require re-casting at mana cost are card recycling or resilience, not card advantage. Distinguish these three categories explicitly: advantage (net new resources), virtual advantage (repeat access from inaccessible zones at no library cost), and recycling (returning a card you already owned and must re-cast).

Note the relationship between the commander's abilities and the commander's triggering method. A "dies" or "whenever a creature dies" trigger fires on death from any cause (combat, removal, sacrifice). It is a death trigger, not a sacrifice ability — the word "sacrifice" is a specific game action and only applies when the card text uses that word. Death-trigger commanders can be enabled by sacrifice outlets, but the commander itself is not a sacrifice engine unless it says "sacrifice." The trigger's qualifier determines what the ability does: analyze it as written, not as the most common way to trigger it.

If the commander has multiple abilities that don't directly interact, analyze each one's strategic implications separately. A secondary ability (e.g., a +1/+1 counter trigger on a spell-copy commander) is support for the primary build-around, not a separate identity. Self-mill that fuels the commander's strategy (filling the graveyard for recursion) is an enabler, not a weakness — only list it as a weakness if the commander has no graveyard interaction and risks decking.

When the commander copies an object or spell, describe it as creating a copy. Copying is not removing, stealing, exiling, or destroying the original. A spell copy on the stack is not "cast" — it does not trigger cast abilities. A permanent copy (clone) retains only the printed characteristics, not counters or modifications, unless specified otherwise. A spell-copy ability that says "you may choose new targets" means the copy is independent of the original's targets.

When the commander's payoff is delayed (suspend, next upkeep, end step, beginning of combat), state the delay explicitly. A free spell in 3 upkeeps is not equivalent to getting it now — factor the timing into strengths and weaknesses. When the commander's ability grants a spell Suspend, note that suspend is a keyword mechanic that exiles with time counters and casts for free when the last counter is removed — the delay and the zero-mana cast are both relevant.

If the card has keywords (Flying, Ward, etc.), explain how they affect Commander play. Consider how the commander's color identity shapes the card pool available to it and how that interacts with the commander's specific oracle text — the analysis should reflect the intersection of color access and the commander's unique mechanics, not either in isolation. If EDHREC rank is provided, note what it implies about popularity. If salt score is high, note why players find it frustrating.

When the commander has symmetric effects, distinguish them from personal ones. "Each player draws" or "each opponent" is not the same as "you draw" — symmetric effects affect the whole table, which matters in a multiplayer format. Group-hug draw engines (everyone draws) and group-slug punishers (damage on opponent's draw) should be analyzed for their political and symmetrical implications, not presented as single-player value engines.

Commander bracket system (new official power level tiers):
- Bracket 1 (Exhibition): Ultra-casual, theme/joke/meme decks. Winning is not the goal — the experience is. Jank builds, chair tribal, ladies looking left.
- Bracket 2 (Core): Average precon power level. Casual play with some synergy and a clear game plan, but minimal tutors, efficient combos, or fast mana.
- Bracket 3 (Upgraded): Tuned decks beyond precons. Game Changer cards allowed (max 3). Stronger synergies, more efficient interaction, but not fully optimized.
- Bracket 4 (Optimized): High power. No restrictions beyond the banlist. Fast mana, efficient tutors, compact combos. Not quite cEDH but pushing the ceiling.
- Bracket 5 (cEDH): Competitive EDH. Win-at-all-costs. Fully optimized lists, meta-driven choices, every card is the most efficient version of its effect.

For strengths/weaknesses, list 3–5 each as an array of strings. Each must cite a concrete mechanic or interaction.
For strategies, list 1–4 strategies each with a name and a description tied to the commander's specific abilities. Each strategy must differ in its core game plan or primary win condition, not just its name or card choices. If the commander genuinely supports only 1-2 distinct strategies, list fewer rather than creating near-identical variants. A name change without a change in game plan is not a different strategy.
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

DEEPDIVE_SYSTEM_PROMPT = """You are an expert Magic: The Gathering deck builder specializing in the Commander format. Deep-dive on a specific {type_label} for this commander.

Produce a detailed analysis of this specific approach — not the commander in general. Focus exclusively on what makes this {type_label} work:
- Which card types and mechanics does it leverage?
- What are the key enablers and payoffs?
- How does it win games?
- What are 3 example cards that best illustrate this {type_label}?

The user prompt includes web_research — search results for this commander + {type_label} combination. Use it to identify real cards, combos, and deckbuilding patterns the community uses for this specific approach.

The user prompt also includes similar_cards — real Magic cards whose oracle text is mechanically similar, ranked by cosine similarity. The retrieval is biased toward this {type_label}, so these cards should be strong fits. Each card includes a truncated oracle text field — use it to judge whether the card actually fits this {type_label}.

You may name specific cards from the similar_cards list — but ONLY cards that appear in that list. Do not invent card names that are not in similar_cards. Pick 3 cards from similar_cards that best illustrate this {type_label} — cards whose role and synergies clarify how the strategy works. Each card should serve a distinct purpose (enabler, payoff, support piece). If no card in the list fits a particular role, describe the role abstractly (e.g., "a cheap sacrifice outlet" or "a mana dork that produces GU") — never output placeholder names like "Sacrifice Outlet Placeholder" or "Token Generator Placeholder."

Before listing any example card, verify it is legal in the commander's color identity. Cards outside the commander's color identity cannot be included in the deck. If a card in the similar_cards list has a color identity that extends beyond the commander's, do not list it. If no in-color similar card fits a role, describe the role abstractly.

When describing recursion or return effects, specify the destination zone exactly. Returning a card to hand means re-casting it at its mana cost — this is recycling, not free redeployment. Returning to the battlefield means the card is deployed without casting. The distinction matters for deck-building and tempo evaluation.

If this {type_label} is substantially similar to another approach already described for this commander, note the overlap and focus on what genuinely differentiates this approach. If there is no meaningful difference, state that explicitly rather than producing duplicate analysis.

Output ONLY valid JSON with these exact keys:
{
  "strengths": ["3-5 strings, each citing a mechanic or interaction specific to this {type_label}"],
  "weaknesses": ["3-5 strings, each citing a vulnerability or counter specific to this {type_label}"],
  "priorities": [{"category": "string", "reason": "string, why this is critical for this {type_label}"}],
  "win_conditions": [{"name": "string", "description": "string, how this approach closes out games"}],
  "example_cards": [{"name": "string, MUST be in similar_cards list", "reason": "string, why this card illustrates the {type_label} and what role it serves (enabler, payoff, support)"}]
}

example_cards must have exactly 3 entries. Pick the 3 cards from similar_cards that best clarify how the {type_label} works. Each should serve a distinct role.

CRITICAL: The "name" field in each example_card entry MUST be the exact, verbatim name of a card that appears in the similar_cards list. Do NOT wrap names in brackets, do NOT append "(Placeholder)", do NOT write descriptions like "Card Name Placeholder" — copy the card name exactly as it appears in similar_cards. If no suitable card exists in similar_cards for a role, you MUST describe the role abstractly in the strengths/weaknesses/priorities fields instead — do not include placeholder entries in example_cards."""

VERIFY_SYSTEM_PROMPT = """You are a Magic: The Gathering rules judge. Verify an AI-generated Commander analysis against the card's actual Oracle text and official rulings.

The user prompt includes allowed_card_names — cards the analysis was explicitly permitted to name. Only flag as hallucinated if a card name appears that is NOT in this list.

Check for these factual errors:
1. **Invented abilities**: The analysis describes a mechanic the card does not actually have. Only flag if the claimed ability is absent from the oracle text — do NOT flag strategic interpretations or synergy suggestions.
2. **Wrong zone references**: The card interacts with a specific zone (graveyard, library, battlefield, exile, hand, command zone, stack) and the analysis names the wrong one.
3. **Hallucinated card names**: The analysis mentions a card by name that is NOT in the allowed_card_names list.
4. **Color identity errors**: The analysis recommends cards whose color identity symbols are not a subset of the commander's color identity.

CRITICAL — when checking mechanical claims:
- Read the oracle text literally. If the card text supports the claim, do NOT flag it.
- Do NOT flag synergy suggestions. "This card combos with Impact Tremors" is a strategic claim, not a rules error.
- If you are uncertain whether a claim is correct, do NOT flag it. Only flag errors you are certain about.
- Example: A creature that returns from exile WILL trigger "when a creature enters the battlefield" effects. That is correct rules function — do NOT flag it.

Only flag clear factual rules errors. Do NOT flag:
- Strategic opinions or card evaluations you disagree with
- Wording style or phrasing preferences
- Edhrec rank or salt score interpretations
- Missing strategies (incompleteness is not an error)
- Correct card names that appear in allowed_card_names
- Synergy suggestions between cards (even if you think the combo doesn't work)

Output ONLY valid JSON:
{"warnings": ["warning 1", "warning 2", ...], "verified": true}"""


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
    try:
        with open(_ingest_path) as f:
            last_ingest = json.load(f)
    except (FileNotFoundError, IsADirectoryError, json.JSONDecodeError):
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
                           mcp_host=MCP_HOST)


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
            embed.build(DATABASE)
        except Exception:
            pass
    threading.Thread(target=_build, daemon=True).start()
    return jsonify({"success": True})


# ── MCP server status ─────────────────────────────────────────────────────

MCP_SSE_PORT = int(os.environ.get("MCP_SSE_PORT", "8765"))
MCPO_PORT = int(os.environ.get("MCPO_PORT", "8000"))
MCP_HOST = os.environ.get("MCP_HOST", "0.0.0.0")


def _port_alive(port):
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


@app.route("/config/mcp-status")
def mcp_status():
    """Return MCP server status — SSE backend + MCPO proxy."""
    sse_alive = _port_alive(MCP_SSE_PORT)
    mcpo_alive = _port_alive(MCPO_PORT)
    return jsonify({
        "running": sse_alive and mcpo_alive,
        "host": MCP_HOST,
        "sse": {
            "alive": sse_alive,
            "port": MCP_SSE_PORT,
        },
        "mcpo": {
            "alive": mcpo_alive,
            "port": MCPO_PORT,
            "url": f"http://{MCP_HOST}:{MCPO_PORT}/openapi.json",
        },
    })


def _restart_mcp(transport, port, pid_file, log_file):
    """Kill existing MCP process by PID file, then re-launch."""
    import signal, time, subprocess, sys
    script_dir = os.path.dirname(os.path.abspath(__file__))
    # ponytail: sys.executable works in Docker (system python) and venv
    venv_python = os.path.join(script_dir, "venv", "bin", "python3")
    if not os.path.isfile(venv_python):
        venv_python = sys.executable
    mcp_script = os.path.join(script_dir, "mcp_server.py")

    try:
        with open(pid_file) as f:
            os.kill(int(f.read().strip()), signal.SIGTERM)
        time.sleep(0.5)
    except (FileNotFoundError, ValueError, ProcessLookupError, OSError):
        pass

    proc = subprocess.Popen(
        [venv_python, mcp_script, "--transport", transport,
         "--port", str(port), "--host", MCP_HOST],
        cwd=script_dir,
        stdout=open(log_file, "a"),
        stderr=subprocess.STDOUT,
        start_new_session=True,
    )
    with open(pid_file, "w") as f:
        f.write(str(proc.pid))
    return proc.pid


@app.route("/config/mcp-restart", methods=["POST"])
def mcp_restart():
    """Restart MCP SSE server + MCPO proxy."""
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        sse_pid = _restart_mcp(
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

        # ponytail: deduplicate to one row per unique card before replacing live DB.
        # Non-English rows, older printings, and duplicate rulings removed.
        tmp_dedup = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        tmp_dedup_path = tmp_dedup.name
        tmp_dedup.close()
        try:
            dedup.dedup_db(tmp_sqlite, tmp_dedup_path)
        except Exception:
            try:
                os.unlink(tmp_dedup_path)
            except OSError:
                pass
            raise

        # Validate dedup result
        dedup_db = sqlite3.connect(tmp_dedup_path)
        try:
            dedup_cnt = dedup_db.execute(
                "SELECT COUNT(*) FROM cards WHERE language='English'"
            ).fetchone()[0]
        finally:
            dedup_db.close()

        if dedup_cnt < 500:
            try:
                os.unlink(tmp_dedup_path)
            except OSError:
                pass
            return jsonify({
                "error": f"Deduplicated database has only {dedup_cnt} English cards — this doesn't look right."
            }), 400

        # Replace the active database.
        # ponytail: copy + unlink instead of shutil.move — Docker overlay
        # causes cross-device errors with os.rename in the container.
        shutil.copyfile(tmp_dedup_path, DATABASE)
        os.unlink(tmp_dedup_path)

        # Persist ingest timestamp
        now_iso = datetime.now(timezone.utc).isoformat()
        ingest_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".last_ingest.json")
        try:
            with open(ingest_path, "w") as out:
                json.dump({
                    "timestamp": now_iso,
                    "filename": filename,
                    "cards": dedup_cnt,
                    "deduplicated": True,
                }, out)
        except (OSError, IsADirectoryError):
            pass  # ponytail: Docker creates dirs for missing bind-mount files

        # Trigger embedding index rebuild in background — new cards need new vectors.
        # ponytail: fire-and-forget thread; the config page polls embed.status().
        def _rebuild():
            try:
                embed.build(DATABASE)
            except Exception:
                pass
        threading.Thread(target=_rebuild, daemon=True).start()

        return jsonify({"success": True, "cards": dedup_cnt, "timestamp": now_iso})

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

    # Seed eval_key + _eval_cache so deepdive/save routes have access to the
    # analysis. Always seed when loading a report from disk so the cache belongs
    # to the current card — stale eval_key from a prior card must not bleed in.
    if analysis:
        if report_data or not session.get("eval_key"):
            session["eval_key"] = str(uuid.uuid4())
            _cache_put(session["eval_key"], {"_card": f"{set_code}/{number}", "analysis": analysis})
            if report_data:
                # ponytail: backwards compat — old reports use "expands" key
                _eval_cache[session["eval_key"]]["deepdives"] = report_data.get("deepdives") or report_data.get("expands", {})
                _eval_cache[session["eval_key"]]["similar"] = report_data.get("similar", [])

    similar = (report_data or {}).get("similar") if loaded_from else cached.get("similar")

    # ponytail: merge auto-saved deepdives so they survive navigation away.
    # cached deepdives (from this session) > loaded report > auto-save.
    _saved_deepdives = _load_auto_deepdives(set_code, number)
    if report_data:
        _saved_deepdives.update(report_data.get("deepdives") or report_data.get("expands", {}))
    _saved_deepdives.update(cached.get("deepdives", {}))

    progress_key = str(uuid.uuid4())
    session["eval_progress_key"] = progress_key

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

        # ponytail: track pipeline health — surface failures so the user
        # knows when analysis ran with incomplete data.
        pipeline_status = {"web_search": {}, "embed": None}

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
            results = _web_search(query, max_results=5)
            searches[search_label] = results
            pipeline_status["web_search"][search_label] = len(results)

        user_prompt = json.dumps({
            "commander": card_data,
            "web_research": searches,
        }, indent=2)

        # ponytail: enrich with mechanically similar cards from embedding index.
        # Gives the LLM real cards to reference instead of guessing at synergies.
        _step(3, "Retrieving mechanically similar cards…")
        try:
            similar_cards = embed.find(DATABASE, card_data["text"], top_k=200)
            # ponytail: filter out the commander's own card + off-color cards
            commander_ci = card_data.get("colorIdentity", "")
            similar_cards = [
                c for c in similar_cards
                if c["name"] != card["name"] and _color_identity_legal(c.get("colorIdentity", ""), commander_ci)
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
    dd_pipeline = {"embed": None, "web_search": {}}
    _card_lookup = {}  # name -> {scryfallId, setCode, number}
    _allowed_names = []  # ponytail: track for verify allowlist
    try:
        commander_cards = embed.find(DATABASE, card.get("text", "") or "", top_k=100)
        strategy_text = f"{card['name']} {expand_name} {expand_desc}"
        strategy_cards = embed.find(DATABASE, strategy_text, top_k=100)
        # ponytail: filter to commander's color identity
        commander_ci = card.get("colorIdentity", "")
        commander_cards = [c for c in commander_cards if _color_identity_legal(c.get("colorIdentity", ""), commander_ci)]
        strategy_cards = [c for c in strategy_cards if _color_identity_legal(c.get("colorIdentity", ""), commander_ci)]
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
        searches = {}
        for search_label, query in [
            ("deck_guides", f"{card['name']} {expand_name} commander deck primer"),
            ("strategy", f"{card['name']} {expand_desc} synergies"),
            ("discussion", f"{card['name']} {expand_name} commander reddit edh"),
        ]:
            results = _web_search(query, max_results=5)
            searches[search_label] = results
            dd_pipeline["web_search"][search_label] = len(results)
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
                        if not _is_placeholder_name(rc.get("name", ""))]
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
        _auto_save_deepdives(set_code, number, {f"{expand_type}:{expand_name}": data})

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
        results, progress = _eval_similar_legacy(db, card)
    else:
        try:
            results, progress = _eval_similar_embed(db, card)
        except Exception:
            # ponytail: fall back to legacy if embeddings unavailable
            results, progress = _eval_similar_legacy(db, card)

    # ponytail: stash in cache so save picks it up
    eval_key = session.get("eval_key")
    if eval_key:
        with _cache_lock:
            if eval_key in _eval_cache:
                _eval_cache[eval_key]["similar"] = results

    return jsonify({"success": True, "results": results, "progress": progress, "method": method})


# ponytail: cached UUID sets for _eval_similar_embed — two full table scans per
# request is wasteful.  TTL'd at 10 minutes; DB doesn't change mid-session.
_cached_legendary = None
_cached_cmdr_legal = None
_cached_filter_ttl = 0

def _get_commander_filter_sets(db):
    """Return (legendary_uuids, cmdr_legal_uuids), cached for 10 minutes."""
    global _cached_legendary, _cached_cmdr_legal, _cached_filter_ttl
    import time as _time
    now = _time.monotonic()
    if _cached_legendary is not None and now - _cached_filter_ttl < 600:
        return _cached_legendary, _cached_cmdr_legal
    _cached_legendary = {
        r["uuid"] for r in db.execute(
            "SELECT uuid FROM cards WHERE supertypes LIKE '%Legendary%'"
        ).fetchall()
    }
    _cached_cmdr_legal = {
        r["uuid"] for r in db.execute(
            "SELECT uuid FROM cardLegalities WHERE commander = 'Legal'"
        ).fetchall()
    }
    _cached_filter_ttl = now
    return _cached_legendary, _cached_cmdr_legal


def _eval_similar_embed(db, card):
    """Semantic similarity via embedding index. Filters to legendary + Cmdr-legal + MV ±3."""
    from embed import find

    candidates = find(DATABASE, card.get("text") or "", top_k=200)
    candidates = [c for c in candidates if c["name"] != card["name"]]

    # Filter to legendary, Commander-legal, MV ±3
    card_mv = card.get("manaValue") or 0
    mv_min = max(0, card_mv - 3)
    mv_max = card_mv + 3

    legendary_uuids, cmdr_legal = _get_commander_filter_sets(db)

    results = []
    seen_names = set()
    for c in candidates:
        uuid = c["uuid"]
        if uuid not in legendary_uuids or uuid not in cmdr_legal:
            continue
        mv = c.get("manaValue")
        if mv is not None and (mv < mv_min or mv > mv_max):
            continue
        base_name = c["name"].split(" // ")[0]
        if base_name in seen_names:
            continue
        seen_names.add(base_name)

        # Fetch setCode/number for URL
        row = db.execute(
            "SELECT setCode, number, manaCost, type FROM cards WHERE uuid = ?",
            (uuid,)
        ).fetchone()
        if not row:
            continue

        results.append({
            "name": c["name"],
            "setCode": row["setCode"],
            "number": row["number"],
            "scryfallId": c.get("scryfallId", ""),
            "score": round(c["score"] * 100, 1),
            "manaCost": row["manaCost"] or "",
            "type": row["type"] or "",
        })

    progress = [{"round": 1, "label": "Semantic", "count": len(results)}]
    return results[:10], progress


def _eval_similar_legacy(db, card):
    """TF-IDF strictness-loosening loop — original algorithm."""
    base = dict(card)
    base["_t"] = _split_csv(base["types"])
    base["_kw"] = _split_csv(base["keywords"])
    base["_sub"] = _split_csv(base["subtypes"])
    base["_st"] = _split_csv(base["supertypes"])
    base["_ci"] = _split_csv(base["colorIdentity"])

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

    rounds = [
        (1.0, 2.0, 200.0, 0.45, "Strict"),
        (0.5, 0.5, 2.0, 0.35, "Moderate"),
        (0.3, 0.15, 0.5, 0.25, "Loose"),
        (0.15, 0.05, 0.5, 0.0, "Very Loose"),
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

    # ponytail: pull from scored until we have 10 deduped results
    results = []
    seen_names = set()
    for score, c in scored:
        if len(results) >= 10:
            break
        # ponytail: DFC names like "Kardur, Doomscourge // Kardur, Doomscourge"
        # are distinct from "Kardur, Doomscourge" in the DB but are the same card.
        base_name = c["name"].split(" // ")[0]
        if base_name in seen_names:
            continue
        seen_names.add(base_name)
        results.append({
            "name": c["name"],
            "setCode": c["setCode"],
            "number": c["number"],
            "scryfallId": c["scryfallId"],
            "score": round(score * 100, 1),
            "manaCost": c["manaCost"] or "",
            "type": c["type"] or "",
        })
    return results, progress


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

    # ponytail: merge auto-saved deepdives so save captures everything even after cache eviction.
    deepdives = cached.get("deepdives", {})
    _autosaved = _load_auto_deepdives(set_code, number)
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


def _auto_save_deepdives(set_code, number, new_deepdives):
    """Persist deepdives to a deterministic file so they survive navigation away."""
    if not new_deepdives:
        return
    reports_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), REPORTS_DIR)
    os.makedirs(reports_dir, exist_ok=True)
    autosave_path = os.path.join(reports_dir, f"{set_code}_{number}_deepdives.json")
    try:
        existing = {}
        if os.path.exists(autosave_path):
            with open(autosave_path) as f:
                existing = json.load(f)
        merged = existing.get("deepdives", {})
        merged.update(new_deepdives)
        with open(autosave_path, "w") as f:
            json.dump({"deepdives": merged}, f, indent=2)
    except (OSError, json.JSONDecodeError):
        pass  # ponytail: fail silently — deepdives still live in cache for this session


def _load_auto_deepdives(set_code, number):
    """Load auto-saved deepdives from the deterministic file."""
    autosave_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        REPORTS_DIR,
        f"{set_code}_{number}_deepdives.json",
    )
    try:
        if os.path.exists(autosave_path):
            with open(autosave_path) as f:
                return json.load(f).get("deepdives", {})
    except (OSError, json.JSONDecodeError):
        pass
    return {}


@app.route("/commander-eval/reports")
def commander_eval_reports():
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


# --- Run ---

if __name__ == "__main__":
    app.run(host="0.0.0.0", debug=os.environ.get("FLASK_DEBUG", "").lower() in ("1", "true", "yes"))
