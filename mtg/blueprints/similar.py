"""Similar cards blueprint — find cards mechanically similar to a base card."""

from urllib.parse import quote

from flask import (
    Blueprint, render_template, request, session, abort, redirect,
    url_for, jsonify, make_response,
)

from mtg.shared import get_db, card_image, render_mana, DATABASE, db_path, color_identity_subset
from mtg.similarity import similarity_label, get_idf, extract_terms, split_csv, score_similarity
import mtg.embed as embed

similar_bp = Blueprint("similar", __name__)


@similar_bp.route("/similar", methods=["GET", "POST"])
@similar_bp.route("/cards/<set_code>/similar", methods=["GET", "POST"])
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
                "redirect": url_for('similar.similar_cards',
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
                      "mtg_filter", "top_n", "tuned", "per_page",
                      "format"):
                v = request.args.get(k)
                if v:
                    keep[k] = v
            return redirect(url_for('similar.similar_cards',
                                    set_code=found['setCode'],
                                    number=found['number'],
                                    **keep))

    return render_template("similar_landing.html",
                           set_code=set_code,
                           by_name=by_name,
                           by_name_error=bool(by_name))


@similar_bp.route("/card/<set_code>/<number>/similar")
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
        return redirect(url_for('similar.similar_cards', set_code=set_code, number=number, **args_dict))

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

    fmt = request.args.get("format", "").strip().lower()
    valid_formats = {"standard", "commander", "modern", "legacy", "pioneer", "vintage",
                     "pauper", "brawl", "historic", "oathbreaker", "penny", "duel",
                     "alchemy", "gladiator", "oldschool", "premodern", "predh",
                     "paupercommander", "timeless", "standardbrawl", "competitivebrawl"}
    if fmt not in valid_formats:
        fmt = ""

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
                      "mtg_filter", "top_n", "page", "tuned", "per_page",
                      "format"):
                v = request.args.get(k)
                if v:
                    keep[k] = v
            return redirect(url_for('similar.similar_cards',
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
                    "SELECT c.uuid, c.setCode, c.number, c.manaCost, c.manaValue, c.type, c.types, c.supertypes, "
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
            SELECT c.uuid, c.name, c.manaCost, c.manaValue, c.type, c.types, c.supertypes,
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

    # --- Fetch legality status for the selected format ---
    legalities = {}
    if fmt and scored:
        uuids = [dict(c)["uuid"] for _, c in scored]
        if uuids:
            placeholders = ",".join("?" * len(uuids))
            for row in db.execute(
                f"SELECT uuid, {fmt} as status FROM cardLegalities WHERE uuid IN ({placeholders})",
                uuids
            ):
                legalities[row["uuid"]] = row["status"] or ""

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
              "mtg_filter", "top_n", "tuned", "per_page",
              "format"):
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
        fmt=fmt,
        legalities=legalities,
        tuning_params=tuning_params,
        top_n=top_n,
        method=method,
        best_score=best_score,
    )
