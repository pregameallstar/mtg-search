"""Blueprint: eval — Commander Eval per-card routes.

url_prefix="/card/<set_code>/<number>/eval"
"""

import json
import os
import uuid
import threading
import traceback
from collections import OrderedDict
from datetime import datetime, timezone

from flask import (
    Blueprint,
    abort,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from mtg.shared import (
    DATABASE,
    REPORTS_DIR,
    card_image,
    color_identity_subset,
    db_path,
    get_db,
)
from mtg.eval_cache import (
    _cache_lock,
    _eval_cache,
    _progress_cache,
    cache_put,
    cache_update,
    cache_update_locked,
)
from mtg.eval_helpers import (
    auto_save_deepdives,
    eval_similar_embed,
    eval_similar_legacy,
    get_commander_filter_sets,
    is_placeholder_name,
    load_auto_deepdives,
)
from mtg.websearch import web_search
from mtg.prompts import COMMANDER_SYSTEM_PROMPT, DEEPDIVE_SYSTEM_PROMPT, VERIFY_SYSTEM_PROMPT

try:
    from mtg.llm import generate as llm_generate
except ImportError:
    llm_generate = None

import mtg.embed as embed

eval_bp = Blueprint("eval", __name__, url_prefix="/card/<set_code>/<number>/eval")

# Resolve the project root for path construction (replaces os.path.dirname(os.path.abspath(__file__))
# from the original single-file app, since __file__ now points inside mtg/blueprints/).
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


@eval_bp.route("/restore", methods=["POST"])
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
    cache_put(key, entry)
    session["eval_key"] = key
    return {"success": True, "key": key}


@eval_bp.route("/", methods=["GET", "POST"])
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
            return redirect(url_for('eval.commander_eval',
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
            report_path = os.path.join(_project_root, REPORTS_DIR, report_filename)
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
            cache_put(session["eval_key"], entry)

    similar = (report_data or {}).get("similar") if loaded_from else cached.get("similar")

    # ponytail: merge auto-saved deepdives so they survive navigation away.
    # cached deepdives (from this session) > loaded report > auto-save.
    _saved_deepdives = load_auto_deepdives(
        set_code, number,
        os.path.join(_project_root, REPORTS_DIR),
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


@eval_bp.route("/progress")
def eval_progress(set_code, number):
    """Poll for analysis progress — returns {step, total, label}."""
    key = session.get("eval_progress_key")
    data = _progress_cache.get(key) if key else None
    return jsonify(data if data else {"step": 0, "total": 0, "label": "Waiting…"})


@eval_bp.route("/analyze", methods=["POST"])
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
        cache_put(session["eval_key"], {"_card": f"{set_code}/{number}", "analysis": analysis})
        return {"success": True}

    except Exception as e:
        traceback.print_exc()
        session["eval_key"] = str(uuid.uuid4())
        cache_put(session["eval_key"], {"_card": f"{set_code}/{number}", "error": str(e)})
        return {"error": str(e)}, 500


@eval_bp.route("/deepdive", methods=["POST"])
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
            os.path.join(_project_root, REPORTS_DIR),
        )

        data["_pipeline"] = dd_pipeline
        return jsonify({"success": True, "data": data})
    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@eval_bp.route("/similar", methods=["POST"])
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
                cache_update_locked(eval_key, similar=results)

    return jsonify({"success": True, "results": results, "progress": progress, "method": method})


@eval_bp.route("/save", methods=["POST"])
def commander_eval_save(set_code, number):
    """Save eval report to disk."""
    eval_key = session.get("eval_key")

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
    report_path = os.path.join(_project_root, REPORTS_DIR, safe_name)

    os.makedirs(os.path.dirname(report_path), exist_ok=True)

    # ponytail: merge auto-saved deepdives so save captures everything even after cache eviction.
    deepdives = cached.get("deepdives", {})
    _autosaved = load_auto_deepdives(
        set_code, number,
        os.path.join(_project_root, REPORTS_DIR),
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


@eval_bp.route("/load", methods=["POST"])
def commander_eval_load(set_code, number):
    """Load a saved analysis report from disk."""
    body = request.get_json(silent=True) or {}
    filename = body.get("filename", "").strip()
    if not filename or ".." in filename or "/" in filename:
        return jsonify({"error": "Invalid filename."}), 400

    report_path = os.path.join(_project_root, REPORTS_DIR, filename)
    try:
        with open(report_path) as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return jsonify({"error": "Report not found or unreadable."}), 404

    return jsonify({"success": True, "analysis": data.get("analysis", {})})
