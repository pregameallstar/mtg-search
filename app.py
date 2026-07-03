"""Self-hosted MTG card search — Scryfall replica.

ponytail: single-file Flask app, SQLite, no ORM, no JS, no build step.
ponytail: global lock via SQLite single-writer (we're read-only, irrelevant).
"""

import sqlite3
import re
from urllib.parse import quote
from flask import Flask, g, render_template, request, abort, redirect, url_for

app = Flask(__name__)
DATABASE = "AllPrintings.sqlite"

# --- DB helpers ---

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DATABASE)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA query_only = ON")
    return g.db

@app.teardown_appcontext
def close_db(e=None):
    g.pop("db", None)

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
    }
    result = cost
    for text, cls in SYMBOLS.items():
        result = result.replace(text, f'<i class="ms ms-{cls}"></i>')
    # Handle {2/G} etc — generic hybrid
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
}

# ponytail: module-level IDF cache — computed lazily on first similarity request.
_idf_cache = None
_idf_card_count = 0

def _get_idf():
    """Return {term: log(total_docs / doc_freq)} for all oracle terms.
    Computed once per server lifetime; rebuilt if card count drifts."""
    global _idf_cache, _idf_card_count
    import math
    # ponytail: open own connection — avoids Flask app-context dependency.
    db = sqlite3.connect(DATABASE)
    db.row_factory = sqlite3.Row
    total = db.execute(
        "SELECT COUNT(DISTINCT name) FROM cards WHERE language='English' AND (side IS NULL OR side='a')"
    ).fetchone()[0]
    if _idf_cache is not None and abs(total - _idf_card_count) <= 100:
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
    return _idf_cache


def _extract_terms(text, mtg_filter=False):
    """Tokenize oracle text into significant lowercase terms.
    Strips reminder text (parentheses), filters stop words and short tokens.
    When mtg_filter is True, also strips ubiquitous MTG-domain words."""
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
    }
    if mtg_filter:
        stop_words |= MTG_STOP_WORDS
    return {w for w in words if len(w) >= 3 and w not in stop_words}


def _score_similarity(base, candidate, factors, idf, mtg_filter=False):
    """Compute a weighted similarity score between two card rows.

    Oracle text overlap is always included (IDF-weighted).  Other factors are
    enabled and weighted via the `factors` dict.

    factors keys: use_types, w_types, use_keywords, w_keywords,
                  use_subtypes, w_subtypes, use_supertypes, w_supertypes,
                  use_mv, w_mv, use_color, w_color

    idf dict is computed once by the caller (idf = _get_idf()) and threaded
    through so we don't re-open the database for every candidate.

    Score is normalized to [0,1] regardless of how many factors are active.
    """
    score = 0.0
    active_weight = 1.0  # oracle text always counts as weight 1

    def _split_csv(raw):
        return {t.strip().lower() for t in (raw or "").split(",") if t.strip()}

    # --- Oracle text terms (always on, IDF-weighted) ---
    base_terms = base["_terms"]  # pre-computed by caller, already stop-word-filtered
    cand_terms = _extract_terms(candidate["text"], mtg_filter=mtg_filter)
    if base_terms:
        base_idf_sum = sum(idf.get(t, 0) for t in base_terms)
        shared_idf_sum = sum(idf.get(t, 0) for t in (base_terms & cand_terms))
        score += shared_idf_sum / base_idf_sum if base_idf_sum > 0 else 0.0

    # --- Types (Jaccard) ---
    if factors.get("use_types"):
        w = float(factors.get("w_types", 1))
        active_weight += w
        base_t = _split_csv(base["types"])
        cand_t = _split_csv(candidate["types"])
        union = base_t | cand_t
        if union:
            score += w * len(base_t & cand_t) / len(union)
        else:
            score += w  # both cards have no types → perfect match

    # --- Keywords (Jaccard) ---
    if factors.get("use_keywords"):
        w = float(factors.get("w_keywords", 1))
        active_weight += w
        base_kw = _split_csv(base["keywords"])
        cand_kw = _split_csv(candidate["keywords"])
        if base_kw:
            union = base_kw | cand_kw
            if union:
                score += w * len(base_kw & cand_kw) / len(union)
        elif not cand_kw:
            score += w  # both cards have no keywords → perfect match

    # --- Subtypes (Jaccard) ---
    if factors.get("use_subtypes"):
        w = float(factors.get("w_subtypes", 1))
        active_weight += w
        base_sub = _split_csv(base["subtypes"])
        cand_sub = _split_csv(candidate["subtypes"])
        union = base_sub | cand_sub
        if union:
            score += w * len(base_sub & cand_sub) / len(union)
        else:
            score += w  # both cards have no subtypes → perfect match

    # --- Supertypes (Jaccard) — "Legendary", "Snow", "Basic", "World" ---
    if factors.get("use_supertypes"):
        w = float(factors.get("w_supertypes", 1))
        active_weight += w
        base_st = _split_csv(base["supertypes"])
        cand_st = _split_csv(candidate["supertypes"])
        if base_st:
            union = base_st | cand_st
            if union:
                score += w * len(base_st & cand_st) / len(union)
        elif not cand_st:
            score += w  # both cards have no supertypes → perfect match

    # --- Mana value proximity ---
    if factors.get("use_mv"):
        w = float(factors.get("w_mv", 1))
        active_weight += w
        base_mv = base["manaValue"] or 0
        cand_mv = candidate["manaValue"] or 0
        score += w / (1.0 + abs(base_mv - cand_mv))

    # --- Color identity overlap ---
    if factors.get("use_color"):
        w = float(factors.get("w_color", 1))
        active_weight += w
        base_ci = _split_csv(base["colorIdentity"])
        cand_ci = _split_csv(candidate["colorIdentity"])
        if base_ci:
            score += w * len(base_ci & cand_ci) / len(base_ci)
        elif not cand_ci:
            score += w

    return score / active_weight if active_weight > 0 else 0.0


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
        "SELECT COUNT(DISTINCT name) FROM cards WHERE language='English'"
    ).fetchone()[0]
    set_count = db.execute("SELECT COUNT(*) FROM sets").fetchone()[0]
    return render_template("index.html", total_cards=total, total_sets=set_count)

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
        if color_rule == "exact":
            # ponytail: compare sorted comma string — works because MTGJSON stores colors sorted
            where.append("c.colors = ?")
            params.append(", ".join(color_chars))
            # Also exclude cards with more colors
            where.append(f"LENGTH(c.colors) - LENGTH(REPLACE(c.colors, ',', '')) + (CASE WHEN c.colors = '' THEN 0 ELSE 1 END) = ?")
            params.append(len(color_chars))
        else:
            for ch in color_chars:
                where.append("c.colors LIKE ?")
                params.append(f"%{ch}%")
            if color_rule == "at_most":
                where.append(f"LENGTH(c.colors) - LENGTH(REPLACE(c.colors, ',', '')) + (CASE WHEN c.colors = '' THEN 0 ELSE 1 END) <= ?")
                params.append(len(color_chars))

    # Color identity
    if ci:
        ci_chars = [ch for ch in ci.upper() if ch in "WUBRGC"]
        if ci_rule == "exact":
            where.append("c.colorIdentity = ?")
            params.append(", ".join(sorted(ci_chars)))
        elif ci_rule == "at_most":
            for ch in ci_chars:
                where.append("c.colorIdentity LIKE ?")
                params.append(f"%{ch}%")
            where.append("LENGTH(c.colorIdentity) - LENGTH(REPLACE(c.colorIdentity, ',', '')) + (CASE WHEN c.colorIdentity = '' THEN 0 ELSE 1 END) <= ?")
            params.append(len(ci_chars))
        else:  # at_least
            for ch in ci_chars:
                where.append("c.colorIdentity LIKE ?")
                params.append(f"%{ch}%")

    # Mana value
    if mv:
        if mv_op in ("<", ">", "<=", ">="):
            where.append(f"c.manaValue {mv_op} ?")
            params.append(float(mv))
        elif "-" in mv:
            lo, hi = mv.split("-", 1)
            where.append("c.manaValue BETWEEN ? AND ?")
            params.extend([float(lo.strip()), float(hi.strip())])
        else:
            where.append("c.manaValue = ?")
            params.append(float(mv))

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

@app.route("/sets")
def set_list():
    db = get_db()
    sets = db.execute("""
        SELECT s.*,
               (SELECT COUNT(*) FROM cards WHERE setCode = s.code AND language = 'English') as card_count
        FROM sets s
        ORDER BY s.releaseDate DESC
    """).fetchall()

    # Group by type
    groups = {}
    for s in sets:
        t = s["type"] or "Other"
        groups.setdefault(t, []).append(s)

    # Order groups: major types first
    type_order = {
        "expansion": 0, "core": 1, "commander": 2, "masters": 3,
        "draft_innovation": 4, "funny": 5, "promo": 6, "token": 7,
        "memorabilia": 8, "alchemy": 9, "masterpiece": 10, "duel_deck": 11,
        "from_the_vault": 12, "premium_deck": 13, "spellbook": 14,
        "starter": 15, "box": 16, "archenemy": 17, "planechase": 18,
        "vanguard": 19,
    }
    sorted_groups = sorted(groups.items(), key=lambda x: type_order.get(x[0], 99))

    return render_template("sets.html", groups=sorted_groups)

@app.route("/set/<set_code>")
def set_detail(set_code):
    db = get_db()
    s = db.execute("SELECT * FROM sets WHERE code = ?", [set_code]).fetchone()
    if not s:
        abort(404)

    page = request.args.get("page", 1, type=int)
    per_page = 60

    total = db.execute(
        "SELECT COUNT(*) FROM cards WHERE setCode = ? AND language = 'English' AND (side IS NULL OR side = 'a')",
        [set_code],
    ).fetchone()[0]

    cards = db.execute("""
        SELECT c.name, c.manaCost, c.type, c.rarity, c.colors, c.number, ci.scryfallId
        FROM cards c
        JOIN cardIdentifiers ci ON c.uuid = ci.uuid
        WHERE c.setCode = ? AND c.language = 'English' AND (c.side IS NULL OR c.side = 'a')
        ORDER BY CAST(c.number AS INTEGER), c.number
        LIMIT ? OFFSET ?
    """, [set_code, per_page, (page - 1) * per_page]).fetchall()

    total_pages = max(1, (total + per_page - 1) // per_page)

    return render_template(
        "set.html", set=s, cards=cards,
        page=page, total=total, total_pages=total_pages,
    )

@app.route("/card/<set_code>/<number>/similar")
def similar_cards(set_code, number):
    db = get_db()

    # --- Parse optional factor toggles and weights ---
    factors = {}
    for key in ("use_types", "use_keywords", "use_subtypes", "use_supertypes",
                "use_mv", "use_color"):
        if request.args.get(key):
            factors[key] = True
    for key in ("w_types", "w_keywords", "w_subtypes", "w_supertypes",
                "w_mv", "w_color"):
        if request.args.get(key):
            try:
                factors[key] = float(request.args.get(key))
            except ValueError:
                pass  # ignore bad float values

    mtg_filter = request.args.get("mtg_filter") == "1"

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
                      "w_types", "w_keywords", "w_subtypes", "w_supertypes",
                      "w_mv", "w_color", "mtg_filter", "page"):
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

    # Pre-compute oracle terms for the base card (used in every scoring call)
    base_card = dict(base_card)
    base_card["_terms"] = _extract_terms(base_card["text"], mtg_filter=mtg_filter)

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
        WHERE c.language = 'English'
          AND (c.side IS NULL OR c.side = 'a')
          AND c.uuid != ?
          AND c.name != ?
          AND c.manaValue BETWEEN ? AND ?
        GROUP BY c.name
        ORDER BY s.releaseDate DESC
    """, [base_card["uuid"], base_card["name"], mv_min, mv_max]).fetchall()

    # --- Score and sort ---
    idf = _get_idf()
    scored = []
    for c in candidates:
        scored.append((_score_similarity(base_card, c, factors, idf, mtg_filter=mtg_filter), c))
    scored.sort(key=lambda x: x[0], reverse=True)

    # --- Paginate ---
    page = request.args.get("page", 1, type=int)
    per_page = 30
    total = len(scored)
    total_pages = max(1, (total + per_page - 1) // per_page)
    start = (page - 1) * per_page
    page_results = scored[start:start + per_page]

    # Build query string for pagination links (preserves tuning state)
    tuning_params = ""
    for k in ("use_types", "use_keywords", "use_subtypes", "use_supertypes",
              "w_types", "w_keywords", "w_subtypes", "w_supertypes",
              "w_mv", "w_color", "mtg_filter"):
        v = request.args.get(k)
        if v:
            tuning_params += f"&{quote(k)}={quote(v)}"

    return render_template(
        "similar.html",
        card=base_card,
        results=page_results,
        page=page,
        total=total,
        total_pages=total_pages,
        factors=factors,
        by_name=by_name,
        by_name_error=bool(by_name),  # True only when name lookup was attempted
        mtg_filter=mtg_filter,
        tuning_params=tuning_params,
    )


# --- Run ---

if __name__ == "__main__":
    app.run(debug=True)
