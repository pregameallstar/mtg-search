"""Canonical card search query builder.

Phase 0: extracted from ``mtg/blueprints/search.py`` without changing the
existing search behavior. The HTML route and future ``/api/search`` route share
this module so the SQL is built and tested in one place.

No Flask imports: callers pass ``request.args`` or a plain mapping, and a
``sqlite3.Connection``.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from typing import Any

from mtg.shared import DEFAULT_PER_PAGE, VALID_FORMATS, _ENGLISH_SIDE
from mtg.similarity import normalize_dash, wubrg_sort
from mtg.images import card_image_url

SCHEMA_VERSION = 1

# The DB intentionally stores one row per unique card. Multi-faced cards carry
# names like "A // B"; search groups and displays the front-face name.
NAME_EXPR = (
    "CASE WHEN instr(c.name, ' // ') > 0 "
    "THEN substr(c.name, 1, instr(c.name, ' // ') - 1) ELSE c.name END"
)
GROUP_EXPR = NAME_EXPR

SORT_FIELDS = ("name", "mv", "edhrec", "salt", "newest", "rarity", "random")
SORT_DEFAULT_ORDER = {
    "name": "asc",
    "mv": "asc",
    "edhrec": "asc",
    "salt": "desc",
    "newest": "desc",
    "rarity": "desc",
    "random": "",
}
FINISHES = ("nonfoil", "foil", "etched")
RARITY_ORDER = ("mythic", "rare", "uncommon", "common")


def _json1_available() -> bool:
    """SQLite JSON1 is present in the app's runtime; keep a portable fallback."""
    try:
        sqlite3.connect(":memory:").execute("SELECT json_extract('{}', '$.x')")
        return True
    except sqlite3.OperationalError:  # pragma: no cover - unusual build
        return False


_JSON1 = _json1_available()
CAN_BE_COMMANDER_SQL = (
    "json_extract(c.leadershipSkills, '$.commander') = 1"
    if _JSON1
    else "(c.leadershipSkills LIKE '%\"commander\": true%' "
         "OR c.leadershipSkills LIKE '%\"commander\":true%')"
)


@dataclass(frozen=True)
class SearchQuery:
    """Everything needed to run one search, plus values for the template/API."""

    where: tuple[str, ...]
    params: tuple[Any, ...]
    join_clause: str
    name_expr: str
    group_expr: str
    count_sql: str
    data_sql: str
    page: int
    per_page: int
    has_filters: bool
    values: dict[str, Any]
    warnings: tuple[str, ...] = ()


def _raw_get(args: Any, key: str, default: Any = "") -> Any:
    """Get a single value from MultiDict, dict, or mapping-like input."""
    if hasattr(args, "get"):
        value = args.get(key, default)
    else:  # pragma: no cover - defensive for unusual input
        value = default
    if isinstance(value, (list, tuple)):
        return value[0] if value else default
    return default if value is None else value


def _getlist(args: Any, key: str) -> list[str]:
    """Get a repeated query value from MultiDict or a plain mapping."""
    if hasattr(args, "getlist"):
        return list(args.getlist(key))
    value = args.get(key, []) if hasattr(args, "get") else []
    if isinstance(value, (list, tuple)):
        return list(value)
    if value is None or value == "":
        return []
    return [value]


def _text(args: Any, key: str) -> str:
    return str(_raw_get(args, key, "")).strip()


def _int_arg(args: Any, key: str, default: int) -> int:
    try:
        return int(_raw_get(args, key, default))
    except (TypeError, ValueError):
        return default


def _optional_int(args: Any, key: str) -> int | None:
    raw = _text(args, key)
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _optional_float(args: Any, key: str) -> float | None:
    raw = _text(args, key)
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _n(op: str, field: str, cast: bool = False) -> str | None:
    """Return a SQL clause for a numeric operator + field.

    Handles =, <, >, <=, >=. If cast=True, the field is text — use CAST AS REAL
    for comparison, which handles integer and fractional values. '*' and
    non-numeric values cast to NULL, which fails any comparison → excluded.
    """
    ops = {"=": "=", "<": "<", ">": ">", "<=": "<=", ">=": ">="}
    if op in ops:
        col = f"CAST(c.{field} AS REAL)" if cast else f"c.{field}"
        return f"{col} {ops[op]} ?"
    return None


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def build_search_query(args: Any) -> SearchQuery:
    """Parse search args and build the count/data SQL.

    Kept behavior-compatible with the original inline implementation in
    ``mtg/blueprints/search.py``. New filters/sorting belong in later phases.
    """
    q = _text(args, "q")
    name = _text(args, "name")
    oracle = _text(args, "oracle")
    type_line = _text(args, "type_line")
    mana_cost = _text(args, "mana_cost")
    keywords = _text(args, "keywords")

    color = _text(args, "color")
    color_rule = str(_raw_get(args, "color_rule", "at_least")).strip()
    ci = _text(args, "ci")
    ci_rule = str(_raw_get(args, "ci_rule", "at_least")).strip()

    mv = _text(args, "mv")
    mv_op = str(_raw_get(args, "mv_op", "=")).strip()
    pow_val = _text(args, "pow")
    pow_op = str(_raw_get(args, "pow_op", "=")).strip()
    tou_val = _text(args, "tou")
    tou_op = str(_raw_get(args, "tou_op", "=")).strip()
    loy_val = _text(args, "loy")
    loy_op = str(_raw_get(args, "loy_op", "=")).strip()

    rarities = _getlist(args, "rarity")
    games = _getlist(args, "game")
    fmt = _text(args, "format")
    legality = str(_raw_get(args, "legality", "legal")).strip()
    set_code = _text(args, "set")

    is_reprint = _raw_get(args, "is_reprint", "")
    is_reserved = _raw_get(args, "is_reserved", "")
    is_funny = _raw_get(args, "is_funny", "")
    is_oversized = _raw_get(args, "is_oversized", "")
    is_fullart = _raw_get(args, "is_fullart", "")
    is_textless = _raw_get(args, "is_textless", "")
    is_promo = _raw_get(args, "is_promo", "")
    is_rebalanced = _raw_get(args, "is_rebalanced", "")

    border = _text(args, "border")
    layout = _text(args, "layout")
    frame = _text(args, "frame")

    # Search 2.0 filters.
    game_changer = _raw_get(args, "game_changer", "")
    edhrec_max = _optional_int(args, "edhrec_max")
    edhrec_min = _optional_int(args, "edhrec_min")
    salt_min = _optional_float(args, "salt_min")
    salt_max = _optional_float(args, "salt_max")
    artist = _text(args, "artist")
    watermark = _text(args, "watermark")
    set_type = _text(args, "set_type")
    release_after = _text(args, "release_after")
    release_before = _text(args, "release_before")
    produced_mana = _text(args, "produced_mana")
    finish = _text(args, "finish").lower()
    can_be_commander = _raw_get(args, "can_be_commander", "")
    commander_only = _raw_get(args, "commander_only", "")
    partner = _raw_get(args, "partner", "")
    is_timeshifted = _raw_get(args, "is_timeshifted", "")
    is_story_spotlight = _raw_get(args, "is_story_spotlight", "")
    has_content_warning = _raw_get(args, "has_content_warning", "")

    warnings: list[str] = []
    sort = str(_raw_get(args, "sort", "name")).strip().lower()
    if sort not in SORT_FIELDS:
        warnings.append(f"Unknown sort: {sort}")
        sort = "name"
    order_raw = str(_raw_get(args, "order", "")).strip().lower()
    if sort == "random":
        order = ""
    elif order_raw in ("asc", "desc"):
        order = order_raw
    else:
        order = SORT_DEFAULT_ORDER[sort]

    page = _int_arg(args, "page", 1)
    if page < 1:
        page = 1
    per_page_raw = str(_raw_get(args, "per_page", str(DEFAULT_PER_PAGE)))
    per_page = (
        int(per_page_raw)
        if per_page_raw.isdigit() and int(per_page_raw) in (10, 25, 50)
        else DEFAULT_PER_PAGE
    )

    where = ["c.language = 'English'", _ENGLISH_SIDE]
    params: list[Any] = []

    # Free-text: name + type + oracle text (comma-separated, AND logic).
    if q:
        for term in q.split(","):
            term = term.strip().strip("\"'")
            if term:
                where.append(
                    "(c.name LIKE ? ESCAPE '\\' OR c.type LIKE ? ESCAPE '\\' "
                    "OR c.text LIKE ? ESCAPE '\\')"
                )
                escaped = _escape_like(term)
                params.extend([f"%{escaped}%"] * 3)

    # Name search — compare the front face of a DFC.
    if name:
        front_only = (
            "CASE WHEN instr(c.name, ' // ') > 0 "
            "THEN substr(c.name, 1, instr(c.name, ' // ') - 1) ELSE c.name END"
        )
        where.append(f"{front_only} LIKE ? ESCAPE '\\'")
        params.append(f"%{_escape_like(name)}%")

    # Oracle text only (comma-separated, AND logic).
    if oracle:
        for term in oracle.split(","):
            term = term.strip().strip("\"'")
            if term:
                where.append("c.text LIKE ? ESCAPE '\\'")
                params.append(f"%{_escape_like(term)}%")

    # Full type line, normalizing Unicode dashes.
    if type_line:
        normalized = normalize_dash(type_line)
        where.append("REPLACE(c.type, '—', '-') LIKE ? ESCAPE '\\'")
        params.append(f"%{_escape_like(normalized)}%")

    if mana_cost:
        where.append("c.manaCost = ?")
        params.append(mana_cost)

    # Keywords (comma-separated, AND logic).
    if keywords:
        for kw in keywords.split(","):
            kw = kw.strip()
            if kw:
                where.append("c.keywords LIKE ?")
                params.append(f"%{kw}%")

    # Colors (WUBRGC).
    if color:
        color_chars = [ch for ch in color.upper() if ch in "WUBRGC"]
        has_c = "C" in color_chars
        color_chars = [ch for ch in color_chars if ch != "C"]
        if has_c:
            if not color_chars:
                where.append("c.colors = ''")
            elif color_rule == "exact":
                where.append("1 = 0")
        if color_chars:
            if color_rule == "exact":
                where.append("c.colors = ?")
                params.append(", ".join(wubrg_sort(color_chars)))
                where.append(
                    "LENGTH(c.colors) - LENGTH(REPLACE(c.colors, ',', '')) + "
                    "(CASE WHEN c.colors = '' THEN 0 ELSE 1 END) = ?"
                )
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

    # Color identity (WUBRGC).
    if ci:
        ci_chars = [ch for ch in ci.upper() if ch in "WUBRGC"]
        has_c = "C" in ci_chars
        ci_chars = [ch for ch in ci_chars if ch != "C"]
        if has_c:
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

    # Mana value.
    if mv:
        try:
            if mv_op in ("<", ">", "<=", ">="):
                where.append(f"c.manaValue {mv_op} ?")
                params.append(float(mv))
            else:
                range_match = _range_match(mv)
                if range_match:
                    lo, hi = range_match
                    if lo > hi:
                        lo, hi = hi, lo
                    where.append("c.manaValue BETWEEN ? AND ?")
                    params.extend([lo, hi])
                elif mv_op == "=":
                    where.append("c.manaValue = ?")
                    params.append(float(mv))
                else:
                    warnings.append(f"Invalid mana value operator: {mv_op}")
                    where.append("1 = 0")
        except (ValueError, TypeError):
            warnings.append(f"Unparseable mana value: {mv}")
            where.append("1 = 0")

    # Power (text column, CAST for comparisons).
    if pow_val:
        if pow_val == "*":
            where.append("c.power = '*'")
        else:
            clause = _n(pow_op, "power", cast=True)
            if clause:
                where.append(clause)
                params.append(pow_val)

    # Toughness.
    if tou_val:
        if tou_val == "*":
            where.append("c.toughness = '*'")
        else:
            clause = _n(tou_op, "toughness", cast=True)
            if clause:
                where.append(clause)
                params.append(tou_val)

    # Loyalty.
    if loy_val:
        clause = _n(loy_op, "loyalty", cast=True)
        if clause:
            where.append(clause)
            params.append(loy_val)

    # Rarity (multi-select).
    if rarities:
        placeholders = ",".join("?" * len(rarities))
        where.append(f"LOWER(c.rarity) IN ({placeholders})")
        params.extend([r.lower() for r in rarities])

    # Set.
    if set_code:
        where.append("c.setCode = ?")
        params.append(set_code.upper())

    # Game / availability (multi-select, OR logic).
    if games:
        clauses = []
        for g in games:
            clauses.append("c.availability LIKE ?")
            params.append(f"%{g}%")
        where.append("(" + " OR ".join(clauses) + ")")

    # Format legality. The same join also serves commander_only.
    join_clause = ""
    fmt_lower = fmt.lower()
    if fmt and fmt_lower in VALID_FORMATS:
        join_clause = "JOIN cardLegalities cl ON c.uuid = cl.uuid"
        if legality == "banned":
            where.append(f"cl.{fmt_lower} = 'Banned'")
        elif legality == "restricted":
            where.append(f"cl.{fmt_lower} = 'Restricted'")
        else:
            where.append(f"cl.{fmt_lower} = 'Legal'")
    elif fmt:
        warnings.append(f"Unknown format: {fmt}")

    if commander_only:
        join_clause = "JOIN cardLegalities cl ON c.uuid = cl.uuid"
        where.append("cl.commander = 'Legal'")
        where.append(CAN_BE_COMMANDER_SQL)

    # Boolean flags.
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

    # Border.
    if border:
        border_map = {
            "black": "black",
            "white": "white",
            "silver": "silver",
            "borderless": "borderless",
            "gold": "gold",
            "yellow": "yellow",
        }
        if border in border_map:
            where.append("c.borderColor = ?")
            params.append(border_map[border])

    # Layout / frame.
    if layout:
        where.append("c.layout = ?")
        params.append(layout)
    if frame:
        where.append("c.frameVersion = ?")
        params.append(frame)

    # Search 2.0 metadata filters.
    if game_changer:
        where.append("c.isGameChanger = 1")
    if edhrec_max is not None:
        where.append("c.edhrecRank IS NOT NULL AND c.edhrecRank <= ?")
        params.append(edhrec_max)
    if edhrec_min is not None:
        where.append("c.edhrecRank >= ?")
        params.append(edhrec_min)
    if salt_min is not None:
        where.append("c.edhrecSaltiness >= ?")
        params.append(salt_min)
    if salt_max is not None:
        where.append("c.edhrecSaltiness <= ?")
        params.append(salt_max)
    if artist:
        where.append("c.artist LIKE ? ESCAPE '\\'")
        params.append(f"%{_escape_like(artist)}%")
    if watermark:
        where.append("c.watermark LIKE ? ESCAPE '\\'")
        params.append(f"%{_escape_like(watermark)}%")
    if set_type:
        where.append("s.type = ?")
        params.append(set_type)
    if release_after:
        if re.match(r"^\d{4}-\d{2}-\d{2}$", release_after):
            where.append("s.releaseDate >= ?")
            params.append(release_after)
        else:
            warnings.append(f"Invalid release_after date: {release_after}")
    if release_before:
        if re.match(r"^\d{4}-\d{2}-\d{2}$", release_before):
            where.append("s.releaseDate <= ?")
            params.append(release_before)
        else:
            warnings.append(f"Invalid release_before date: {release_before}")
    if produced_mana:
        where.append("c.producedMana LIKE ?")
        params.append(f"%{produced_mana}%")
    if finish:
        if finish in FINISHES:
            # `finishes` is a comma-separated list ("nonfoil, foil"). A plain
            # LIKE '%foil%' would incorrectly match nonfoil-only cards.
            where.append(
                "instr(',' || REPLACE(c.finishes, ' ', '') || ',', ',' || ? || ',') > 0"
            )
            params.append(finish)
        else:
            warnings.append(f"Unknown finish: {finish}")
    if can_be_commander and not commander_only:
        where.append(CAN_BE_COMMANDER_SQL)
    if partner:
        where.append(
            "(c.keywords LIKE '%Partner%' OR c.text LIKE '%Choose a Background%' "
            "OR c.text LIKE '%Friends forever%' OR c.text LIKE '%Doctor''s companion%')"
        )
    if is_timeshifted:
        where.append("c.isTimeshifted = 1")
    if is_story_spotlight:
        where.append("c.isStorySpotlight = 1")
    if has_content_warning:
        where.append("c.hasContentWarning = 1")

    has_filters = bool(any([
        q, name, oracle, type_line, mana_cost, keywords, color, ci, mv,
        pow_val, tou_val, loy_val, rarities, games, fmt, set_code,
        is_reprint, is_reserved, is_funny, is_oversized, is_fullart,
        is_textless, is_promo, is_rebalanced, border, layout, frame,
        game_changer, edhrec_max, edhrec_min, salt_min, salt_max, artist,
        watermark, set_type, release_after, release_before, produced_mana,
        finish, can_be_commander, commander_only, partner, is_timeshifted,
        is_story_spotlight, has_content_warning,
    ]))

    # A random sort with no filters means "show me a random card", not the
    # empty search state. The no-op clause keeps the SQL shape uniform.
    if sort == "random" and not has_filters:
        has_filters = True
        where.append("1 = 1")

    where_clause = "WHERE " + " AND ".join(where)
    count_sql = (
        f"SELECT COUNT(DISTINCT {GROUP_EXPR}) "
        f"FROM cards c "
        f"JOIN sets s ON c.setCode = s.code "
        f"{join_clause} {where_clause}"
    )
    order_clause = _order_clause(sort, order)
    data_sql = f"""
        SELECT {NAME_EXPR} as name, c.name as fullName, c.manaCost,
               c.manaValue, c.type, c.types, c.rarity, c.setCode,
               c.colors, c.colorIdentity, ci.scryfallId, s.name as setName,
               MAX(s.releaseDate) as releaseDate, c.number,
               c.edhrecRank, c.edhrecSaltiness, c.isGameChanger
        FROM cards c
        JOIN cardIdentifiers ci ON c.uuid = ci.uuid
        JOIN sets s ON c.setCode = s.code
        {join_clause}
        {where_clause}
        GROUP BY {GROUP_EXPR}
        {order_clause}
        LIMIT ? OFFSET ?
    """

    values = {
        "query": q,
        "name": name,
        "oracle": oracle,
        "type_line": type_line,
        "mana_cost": mana_cost,
        "keywords": keywords,
        "color": color,
        "color_rule": color_rule,
        "ci": ci,
        "ci_rule": ci_rule,
        "mv": mv,
        "mv_op": mv_op,
        "pow_val": pow_val,
        "pow_op": pow_op,
        "tou_val": tou_val,
        "tou_op": tou_op,
        "loy_val": loy_val,
        "loy_op": loy_op,
        "rarities": rarities,
        "games": games,
        "fmt": fmt,
        "legality": legality,
        "set_code": set_code,
        "is_reprint": is_reprint,
        "is_reserved": is_reserved,
        "is_funny": is_funny,
        "is_oversized": is_oversized,
        "is_fullart": is_fullart,
        "is_textless": is_textless,
        "is_promo": is_promo,
        "is_rebalanced": is_rebalanced,
        "border": border,
        "layout": layout,
        "frame": frame,
        "game_changer": game_changer,
        "edhrec_max": "" if edhrec_max is None else str(edhrec_max),
        "edhrec_min": "" if edhrec_min is None else str(edhrec_min),
        "salt_min": "" if salt_min is None else str(salt_min),
        "salt_max": "" if salt_max is None else str(salt_max),
        "artist": artist,
        "watermark": watermark,
        "set_type": set_type,
        "release_after": release_after,
        "release_before": release_before,
        "produced_mana": produced_mana,
        "finish": finish,
        "can_be_commander": can_be_commander,
        "commander_only": commander_only,
        "partner": partner,
        "is_timeshifted": is_timeshifted,
        "is_story_spotlight": is_story_spotlight,
        "has_content_warning": has_content_warning,
        "sort": sort,
        "order": order,
        "page": page,
        "per_page": per_page,
        "has_filters": has_filters,
    }

    return SearchQuery(
        where=tuple(where),
        params=tuple(params),
        join_clause=join_clause,
        name_expr=NAME_EXPR,
        group_expr=GROUP_EXPR,
        count_sql=count_sql,
        data_sql=data_sql,
        page=page,
        per_page=per_page,
        has_filters=has_filters,
        values=values,
        warnings=tuple(warnings),
    )


def _order_clause(sort: str, order: str) -> str:
    """Return a whitelisted ORDER BY clause."""
    if sort == "random":
        return "ORDER BY RANDOM()"
    direction = "DESC" if order == "desc" else "ASC"
    if sort == "name":
        return f"ORDER BY name COLLATE NOCASE {direction}, name {direction}"
    if sort == "mv":
        return f"ORDER BY c.manaValue IS NULL, c.manaValue {direction}, name"
    if sort == "edhrec":
        return f"ORDER BY c.edhrecRank IS NULL, c.edhrecRank {direction}, name"
    if sort == "salt":
        return f"ORDER BY c.edhrecSaltiness IS NULL, c.edhrecSaltiness {direction}, name"
    if sort == "newest":
        return f"ORDER BY MAX(s.releaseDate) {direction}, name"
    if sort == "rarity":
        pieces = " ".join(
            f"WHEN '{rarity}' THEN {len(RARITY_ORDER) - index}"
            for index, rarity in enumerate(RARITY_ORDER)
        )
        return f"ORDER BY CASE LOWER(c.rarity) {pieces} ELSE 0 END {direction}, name"
    # Unknown values are normalized before this point; keep a deterministic fallback.
    return f"ORDER BY name COLLATE NOCASE {direction}, name"


def _range_match(value: str) -> tuple[float, float] | None:
    """Return (lo, hi) for a simple ``1-3`` mana-value range, else None."""
    match = re.match(r"^\s*\d+(\.\d+)?\s*-\s*\d+(\.\d+)?\s*$", value)
    if not match:
        return None
    lo, hi = value.split("-", 1)
    return float(lo.strip()), float(hi.strip())


def empty_search_result(query: SearchQuery) -> dict[str, Any]:
    """Return the response shape for a missing database or an empty search."""
    return {
        "results": [],
        "total": 0,
        "total_pages": 0,
        "has_filters": query.has_filters,
        "random_sample": query.values.get("sort") == "random",
        "warnings": list(query.warnings),
    }


def run_search(db: Any, query: SearchQuery) -> dict[str, Any]:
    """Execute a built search and return JSON/template-ready results.

    Rows are sqlite3.Row objects for the HTML route; callers converting to JSON
    should map them to dicts.
    """
    if not query.has_filters:
        return empty_search_result(query)

    total = db.execute(query.count_sql, query.params).fetchone()[0]
    random_sample = query.values.get("sort") == "random"
    if random_sample:
        total_pages = 0 if total == 0 else 1
    else:
        total_pages = max(1, (total + query.per_page - 1) // query.per_page)

    if total == 0:
        results = []
    else:
        # A random sample has no stable page 2; always fetch one sample page.
        offset = 0 if random_sample else (query.page - 1) * query.per_page
        results = db.execute(
            query.data_sql, list(query.params) + [query.per_page, offset]
        ).fetchall()

    return {
        "results": results,
        "total": total,
        "total_pages": total_pages,
        "has_filters": query.has_filters,
        "random_sample": random_sample,
        "warnings": list(query.warnings),
    }


API_FILTER_KEYS = (
    "query", "name", "oracle", "type_line", "mana_cost", "keywords",
    "color", "color_rule", "ci", "ci_rule", "mv", "mv_op",
    "pow_val", "pow_op", "tou_val", "tou_op", "loy_val", "loy_op",
    "rarities", "games", "fmt", "legality", "set_code",
    "is_reprint", "is_reserved", "is_funny", "is_oversized", "is_fullart",
    "is_textless", "is_promo", "is_rebalanced", "border", "layout", "frame",
    "game_changer", "edhrec_max", "edhrec_min", "salt_min", "salt_max",
    "artist", "watermark", "set_type", "release_after", "release_before",
    "produced_mana", "finish", "can_be_commander", "commander_only", "partner",
    "is_timeshifted", "is_story_spotlight", "has_content_warning",
)


def card_row_to_dict(row: Any) -> dict[str, Any]:
    """Map a search result row to the API card shape."""
    return {
        "name": row["name"],
        "fullName": row["fullName"],
        "setCode": row["setCode"],
        "number": row["number"],
        "scryfallId": row["scryfallId"] or "",
        "manaCost": row["manaCost"] or "",
        "manaValue": row["manaValue"],
        "type": row["type"] or "",
        "types": row["types"] or "",
        "colors": row["colors"] or "",
        "colorIdentity": row["colorIdentity"] or "",
        "rarity": row["rarity"] or "",
        "setName": row["setName"] or "",
        "releaseDate": row["releaseDate"] or "",
        "edhrecRank": row["edhrecRank"],
        "edhrecSaltiness": row["edhrecSaltiness"],
        "isGameChanger": bool(row["isGameChanger"]),
        "imageUrl": card_image_url(row["scryfallId"] or ""),
    }


def result_to_api(query: SearchQuery, result: dict[str, Any]) -> dict[str, Any]:
    """Return the `/api/search` response contract."""
    filters = {
        key: query.values[key]
        for key in API_FILTER_KEYS
        if query.values.get(key) not in ("", None, [], 0, False)
    }
    return {
        "cards": [card_row_to_dict(row) for row in result["results"]],
        "total": result["total"],
        "page": query.page,
        "per_page": query.per_page,
        "total_pages": result["total_pages"],
        "sort": query.values.get("sort", "name"),
        "order": query.values.get("order", ""),
        "random_sample": result.get("random_sample", False),
        "warnings": result["warnings"],
        "filters": filters,
        "schemaVersion": SCHEMA_VERSION,
    }


def search_cards(db: Any, args: Any) -> dict[str, Any]:
    """Build, run, and serialize a search for API/MCP callers."""
    query = build_search_query(args)
    return result_to_api(query, run_search(db, query))
