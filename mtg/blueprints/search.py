"""Search, card detail, image serving, and autocomplete.

ponytail: core routes for browsing and searching the MTG database.
"""

import os
import sys
import re

from flask import Blueprint, g, render_template, request, abort, send_file, jsonify, session, redirect, url_for
from urllib.parse import quote

from mtg.shared import (
    get_db, _db_ready, card_image, render_mana, pagination_url,
    IMAGE_DIR, DATABASE,
)
from mtg.similarity import normalize_dash, wubrg_sort
from mtg.images import fetch_scryfall_image

search_bp = Blueprint("search", __name__)


# === Helpers ===

def _n(op, field, cast=False):
    """Return SQL clause for a numeric operator + field. Handles =, <, >, <=, >=.
    If cast=True, the field is text — use CAST AS REAL for comparison, which
    handles integer and fractional values. '*' and non-numeric values cast to NULL,
    which fails any comparison → excluded."""
    ops = {"=": "=", "<": "<", ">": ">", "<=": "<=", ">=": ">="}
    if op in ops:
        col = f"CAST(c.{field} AS REAL)" if cast else f"c.{field}"
        return f"{col} {ops[op]} ?"


# === Routes ===

@search_bp.route("/")
def index():
    return redirect(url_for("search.search"))


@search_bp.route("/search")
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
    # Game / Availability (multi-select)
    games = request.args.getlist("game")                        # paper, mtgo, arena
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
            rarities=rarities, games=games, fmt=fmt, legality=legality, set_code=set_code,
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
                where.append("c.colors = ''")
            elif color_rule == "exact":
                where.append("1 = 0")
        if color_chars:
            if color_rule == "exact":
                where.append("c.colors = ?")
                params.append(", ".join(wubrg_sort(color_chars)))
                where.append(f"LENGTH(c.colors) - LENGTH(REPLACE(c.colors, ',', '')) + (CASE WHEN c.colors = '' THEN 0 ELSE 1 END) = ?")
                params.append(len(color_chars))
            elif color_rule == "at_most":
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
                where.append("c.colorIdentity = ''")
            elif ci_rule == "exact":
                where.append("1 = 0")
        if ci_chars:
            if ci_rule == "exact":
                where.append("c.colorIdentity = ?")
                params.append(", ".join(sorted(ci_chars)))
            elif ci_rule == "at_most":
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
                    lo_val, hi_val = hi_val, lo_val
                where.append("c.manaValue BETWEEN ? AND ?")
                params.extend([lo_val, hi_val])
            elif mv_op == "=":
                val = float(mv)
                where.append("c.manaValue = ?")
                params.append(val)
            else:
                print(f"search: invalid mv format '{mv}', showing empty results", file=sys.stderr)
                where.append("1 = 0")
        except (ValueError, TypeError):
            print(f"search: unparseable mv '{mv}', showing empty results", file=sys.stderr)
            where.append("1 = 0")

    # Power (text column, needs CAST for comparisons)
    if pow_val:
        if pow_val == '*':
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

    # Game / Availability (multi-select, OR logic)
    if games:
        clauses = []
        for g in games:
            clauses.append("c.availability LIKE ?")
            params.append(f"%{g}%")
        where.append("(" + " OR ".join(clauses) + ")")

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
        loy_val, rarities, games, fmt, set_code, is_reprint, is_reserved, is_funny,
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
        rarities=rarities, games=games, fmt=fmt, legality=legality, set_code=set_code,
        is_reprint=is_reprint, is_reserved=is_reserved, is_funny=is_funny,
        is_oversized=is_oversized, is_fullart=is_fullart, is_textless=is_textless,
        is_promo=is_promo, is_rebalanced=is_rebalanced,
        border=border, layout=layout, frame=frame, unique=unique,
        results=results, page=page, total=total, total_pages=total_pages, per_page=per_page,
        has_filters=has_filters,
    )


@search_bp.route("/card/<set_code>/<number>")
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


@search_bp.route("/img/<size>/<face>/<c1>/<c2>/<scryfall_id>.jpg")
def serve_image(size, face, c1, c2, scryfall_id):
    """Lazy cache-through proxy. First hit fetches from Scryfall and writes to disk."""
    image_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..", IMAGE_DIR,
    )
    full_path = fetch_scryfall_image(image_dir, scryfall_id, size, face)
    if full_path:
        return send_file(full_path)
    # app.logger.warning — use print to stderr instead since we don't have app here
    import sys
    print(
        f"serve_image failed after 3 retries: {size}/{face}/{scryfall_id[0]}/{scryfall_id[1]}/{scryfall_id}.jpg",
        file=sys.stderr,
    )
    abort(404)


@search_bp.route("/card-autocomplete")
def card_autocomplete():
    """Return top 10 card name matches for search autocomplete."""
    q = request.args.get("q", "").strip()
    if not q:
        return jsonify([])
    db = get_db()
    escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    rows = db.execute("""
        SELECT DISTINCT c.name, c.setCode, c.number, ci.scryfallId, s.name as setName
        FROM cards c
        JOIN cardIdentifiers ci ON c.uuid = ci.uuid
        JOIN sets s ON c.setCode = s.code
        WHERE c.name LIKE ? ESCAPE '\\' AND c.language = 'English'
          AND (c.side IS NULL OR c.side = 'a')
        ORDER BY
            CASE WHEN c.name = ? THEN 0 ELSE 1 END,
            c.name
        LIMIT 10
    """, [f"%{escaped}%", q]).fetchall()
    results = []
    for r in rows:
        results.append({
            "name": r["name"],
            "setCode": r["setCode"],
            "number": r["number"],
            "scryfallId": r["scryfallId"],
            "setName": r["setName"],
        })
    return jsonify(results)


@search_bp.route("/llm-models")
def llm_models_proxy():
    """Proxy to fetch available models from a user-configured LLM backend."""
    base_url = request.args.get("base_url", "")
    api_key = session.get("llm_api_key") or os.environ.get("LLM_API_KEY", "")
    try:
        from mtg.llm import fetch_models
    except ImportError:
        return jsonify({"models": [], "error": "LLM module not available."})
    try:
        models = fetch_models(base_url, api_key) if base_url else []
    except Exception as e:
        models = []
        import sys
        import traceback
        print(f"llm_models_proxy error: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
    return jsonify({"models": models})
