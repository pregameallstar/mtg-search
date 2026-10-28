"""Search, card detail, image serving, and autocomplete.

ponytail: core routes for browsing and searching the MTG database.
"""

import os
import sys

from flask import Blueprint, render_template, request, abort, send_file, jsonify, session, redirect, url_for
from werkzeug.datastructures import MultiDict

from mtg.shared import get_db, _db_ready, IMAGE_DIR, DATABASE, db_path
from mtg.card_query import (
    CAN_BE_COMMANDER_SQL,
    NAME_EXPR,
    build_search_query,
    card_row_to_dict,
    empty_search_result,
    result_to_api,
    run_search,
)
from mtg.roles import classify_roles
from mtg.images import (
    VALID_IMAGE_FACES,
    VALID_IMAGE_SIZES,
    card_image_url,
    fetch_scryfall_image,
    is_scryfall_id,
)

search_bp = Blueprint("search", __name__)


# === Routes ===

@search_bp.route("/")
def index():
    return redirect(url_for("search.search"))


@search_bp.route("/search")
def search():
    """Card search — build the canonical query and render results."""
    query = build_search_query(request.args)

    if not _db_ready():
        empty = empty_search_result(query)
        # The unseeded page is an empty state, not a "no results" result.
        empty_values = dict(query.values)
        empty_values["has_filters"] = False
        return render_template(
            "search.html",
            **empty_values,
            results=[],
            total=0,
            total_pages=0,
            random_sample=False,
            db_unseeded=True,
            warnings=empty["warnings"],
        )

    result = run_search(get_db(), query)
    return render_template(
        "search.html",
        **query.values,
        results=result["results"],
        total=result["total"],
        total_pages=result["total_pages"],
        random_sample=result["random_sample"],
        db_unseeded=False,
        warnings=result["warnings"],
    )


@search_bp.route("/api/search")
def api_search():
    """JSON card search. Shares the canonical builder with /search."""
    query = build_search_query(request.args)
    if not _db_ready():
        payload = result_to_api(
            query,
            {
                "results": [],
                "total": 0,
                "total_pages": 0,
                "random_sample": False,
                "warnings": ["Database not seeded."],
            },
        )
        return jsonify(payload)
    return jsonify(result_to_api(query, run_search(get_db(), query)))


@search_bp.route("/api/search/random")
def api_search_random():
    """Return one random card matching the current filters (if any)."""
    if not _db_ready():
        return jsonify({"card": None, "warnings": ["Database not seeded."]})
    args = MultiDict(request.args)
    args["sort"] = "random"
    args["per_page"] = "10"
    query = build_search_query(args)
    if not query.has_filters:
        # A match-all filter is needed because an empty query intentionally
        # renders the search page's empty state rather than all cards.
        args["release_after"] = "1900-01-01"
        query = build_search_query(args)
    result = run_search(get_db(), query)
    cards = [card_row_to_dict(row) for row in result["results"]]
    return jsonify({"card": cards[0] if cards else None,
                    "warnings": result["warnings"]})


@search_bp.route("/commanders")
def commanders_page():
    """Commander Finder — browse/search commander-eligible cards."""
    values = build_commander_values(request.args)
    if not _db_ready():
        return render_template(
            "commanders.html", **values,
            commanders=[], total=0, total_pages=0, method="text",
            commanders_page_url=commanders_page_url, db_unseeded=True,
        )
    data = build_commander_results(request.args)
    return render_template(
        "commanders.html", **values,
        commanders=data["commanders"], total=data["total"],
        total_pages=data["total_pages"], method=data["method"],
        commanders_page_url=commanders_page_url, db_unseeded=False,
    )


@search_bp.route("/api/commanders")
def api_commanders():
    """JSON Commander Finder results."""
    if not _db_ready():
        return jsonify({
            "commanders": [], "total": 0, "page": 1, "per_page": 30,
            "total_pages": 0, "method": "text",
            "warnings": ["Database not seeded."], "schemaVersion": 1,
        })
    data = build_commander_results(request.args)
    return jsonify(data)


def commanders_page_url(args, page):
    """Build a Commander Finder pagination URL preserving filters."""
    from urllib.parse import quote

    parts = []
    for key, values in args.lists():
        if key == "page":
            continue
        for value in values:
            parts.append(f"{quote(key)}={quote(value)}")
    parts.append(f"page={page}")
    return "/commanders?" + "&".join(parts)


def build_commander_values(args):
    """Normalize Commander Finder form values for the template."""
    ci = "".join(ch for ch in str(args.get("ci", "")).upper() if ch in "WUBRG")
    sort = str(args.get("sort", "edhrec")).lower()
    if sort not in ("edhrec", "name", "mv"):
        sort = "edhrec"
    try:
        per_page = int(args.get("per_page", 30))
    except (TypeError, ValueError):
        per_page = 30
    if per_page not in (10, 25, 50):
        per_page = 30
    try:
        page = max(1, int(args.get("page", 1)))
    except (TypeError, ValueError):
        page = 1
    return {
        "ci": ci,
        "q": str(args.get("q", "")).strip(),
        "partner": args.get("partner", ""),
        "edhrec_max": str(args.get("edhrec_max", "")).strip(),
        "sort": sort,
        "page": page,
        "per_page": per_page,
    }


def build_commander_results(args):
    """Build, filter, optionally embed-rerank, and paginate commanders."""
    import mtg.embed as embed

    values = build_commander_values(args)
    db = get_db()
    where = [
        "c.language = 'English'",
        "(c.side IS NULL OR c.side = 'a')",
        "cl.commander = 'Legal'",
        CAN_BE_COMMANDER_SQL,
    ]
    params = []

    allowed = set(values["ci"])
    if allowed:
        for ch in "WUBRG":
            if ch not in allowed:
                where.append("c.colorIdentity NOT LIKE ?")
                params.append(f"%{ch}%")

    if values["edhrec_max"]:
        try:
            rank = int(values["edhrec_max"])
            if rank >= 0:
                where.append("c.edhrecRank IS NOT NULL AND c.edhrecRank <= ?")
                params.append(rank)
        except ValueError:
            values["edhrec_max"] = ""

    if values["partner"]:
        where.append(
            "(c.keywords LIKE '%Partner%' OR c.text LIKE '%Choose a Background%' "
            "OR c.text LIKE '%Friends forever%' OR c.text LIKE '%Doctor''s companion%')"
        )

    q = values["q"]
    embed_scores = {}
    method = "text"
    if q:
        try:
            found = embed.find(db_path(DATABASE), q, top_k=800)
            if found:
                embed_scores = {row["uuid"]: row.get("score", 0) for row in found}
                method = "embed"
        except Exception:
            embed_scores = {}
        if not embed_scores:
            escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append(
                "(c.name LIKE ? ESCAPE '\\' OR c.type LIKE ? ESCAPE '\\' "
                "OR c.text LIKE ? ESCAPE '\\')"
            )
            params.extend([f"%{escaped}%"] * 3)

    order = {
        "edhrec": "c.edhrecRank IS NULL, c.edhrecRank ASC",
        "name": "c.name COLLATE NOCASE ASC",
        "mv": "c.manaValue IS NULL, c.manaValue ASC",
    }[values["sort"]]
    rows = db.execute(f"""
        SELECT c.uuid, {NAME_EXPR} as name, c.setCode, c.number,
               ci.scryfallId, c.manaCost, c.manaValue, c.type,
               c.colorIdentity, c.edhrecRank, c.edhrecSaltiness, c.keywords
        FROM cards c
        JOIN cardIdentifiers ci ON c.uuid = ci.uuid
        JOIN sets s ON c.setCode = s.code
        JOIN cardLegalities cl ON c.uuid = cl.uuid
        WHERE {' AND '.join(where)}
        ORDER BY {order}
        LIMIT 5000
    """, params).fetchall()

    if embed_scores:
        rows = [row for row in rows if row["uuid"] in embed_scores]
        rows.sort(key=lambda row: embed_scores.get(row["uuid"], 0), reverse=True)

    total = len(rows)
    per_page = values["per_page"]
    page = values["page"]
    total_pages = max(1, (total + per_page - 1) // per_page)
    start = (page - 1) * per_page
    page_rows = rows[start:start + per_page]

    commanders = []
    for row in page_rows:
        commanders.append({
            "name": row["name"],
            "setCode": row["setCode"],
            "number": row["number"],
            "scryfallId": row["scryfallId"] or "",
            "manaCost": row["manaCost"] or "",
            "manaValue": row["manaValue"],
            "type": row["type"] or "",
            "colorIdentity": row["colorIdentity"] or "",
            "edhrecRank": row["edhrecRank"],
            "salt": row["edhrecSaltiness"],
            "keywords": row["keywords"] or "",
            "imageUrl": card_image_url(row["scryfallId"] or ""),
        })

    return {
        "commanders": commanders,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "method": method,
        "warnings": [],
        "schemaVersion": 1,
    }


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

    # NOTE: the DB intentionally keeps only the latest printing per card.
    # The MTGJSON `printings` field is a list of set codes only, not printable
    # printings with collector numbers/Scryfall IDs, so it is not used here.

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

    role_info = classify_roles(dict(card))
    template = "card_panel.html" if request.args.get("fragment") else "card.html"
    return render_template(
        template,
        card=card, rulings=rulings, legalities=legalities,
        other_faces=other_faces, role_info=role_info,
    )


@search_bp.route("/img/<size>/<face>/<c1>/<c2>/<scryfall_id>.jpg")
def serve_image(size, face, c1, c2, scryfall_id):
    """Lazy cache-through proxy. First hit fetches from Scryfall and writes to disk."""
    if (
        not is_scryfall_id(scryfall_id)
        or size not in VALID_IMAGE_SIZES
        or face not in VALID_IMAGE_FACES
    ):
        abort(404)
    image_dir = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "..", IMAGE_DIR,
    )
    full_path = fetch_scryfall_image(image_dir, scryfall_id, size, face)
    if full_path:
        return send_file(full_path)
    # app.logger.warning — use print to stderr instead since we don't have app here
    import sys
    print(
        f"serve_image failed after 3 retries: {size}/{face}/{scryfall_id}.jpg",
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
    """Proxy to fetch available models from the saved LLM backend.

    The URL is deliberately not taken from the request: this route is
    unauthenticated, and accepting an arbitrary URL lets any caller make the
    server send the configured API key to an attacker-controlled host.
    """
    from urllib.parse import urlparse

    # Block cross-site subresource/navigation probes; same-origin config JS and
    # non-browser clients (no Sec-Fetch-Site) are allowed.
    fetch_site = request.headers.get("Sec-Fetch-Site")
    if fetch_site and fetch_site not in ("same-origin", "none"):
        return jsonify({"models": [], "error": "Cross-origin request rejected."}), 403

    base_url = (session.get("llm_base_url") or os.environ.get("LLM_BASE_URL", "")).strip()
    if not base_url:
        return jsonify({
            "models": [],
            "error": "Save an LLM base URL in Configuration before testing.",
        })
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return jsonify({"models": [], "error": "Saved base URL must be http(s)."})

    api_key = session.get("llm_api_key") or os.environ.get("LLM_API_KEY", "")
    try:
        from mtg.llm import fetch_models
    except ImportError:
        return jsonify({"models": [], "error": "LLM module not available."})
    try:
        models = fetch_models(base_url, api_key)
    except Exception as e:
        models = []
        import sys
        import traceback
        print(f"llm_models_proxy error: {e}", file=sys.stderr)
        traceback.print_exc(file=sys.stderr)
        return jsonify({"models": [], "error": str(e)})
    return jsonify({"models": models})
