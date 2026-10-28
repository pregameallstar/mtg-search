"""Deck Doctor routes — review jobs, reports, and the report page."""

from __future__ import annotations

import os
import threading
import uuid
from datetime import datetime, timezone

from flask import (
    Blueprint,
    jsonify,
    make_response,
    render_template,
    request,
    session,
)
import mtg.review_cache as review_cache
from mtg import deck_review
from mtg.deck_brief import (
    brief_from_deck,
    brief_is_empty,
    brief_to_deck,
    normalize_brief,
)
from mtg.deck_enrich import enrich_deck
from mtg.prompt_store import get_prompt
from mtg.shared import (
    DECKS_DIR,
    REVIEWS_PATH,
    _list_json_files,
    get_db,
    sanitize_filename,
)
from mtg.storage import (
    StorageError,
    clean_name,
    file_lock,
    read_json,
    read_named_json,
    resolve_json_path,
    write_json_atomic,
)

deck_review_bp = Blueprint("deck_review", __name__)


def _load_deck(name: str) -> dict | None:
    path = resolve_json_path(DECKS_DIR, name)
    return read_named_json(path, name)


def _deck_quantity(card: dict) -> int:
    try:
        return max(1, int(card.get("quantity") or 1))
    except (TypeError, ValueError):
        return 1


def _report_path(filename: str) -> str | None:
    filename = str(filename or "").strip()
    if not filename or filename != os.path.basename(filename):
        return None
    if "\x00" in filename or "/" in filename or "\\" in filename or ".." in filename:
        return None
    if not filename.endswith(".json"):
        return None
    return os.path.join(REVIEWS_PATH, filename)


def _brief_for_deck(deck: dict) -> str:
    """The user-authored free-form notes for the Deck Brief.

    Outline text is intentionally not used as a fallback: the outline is already
    sent to retrieval and the LLM separately, and silently persisting a copy as
    ``reviewNotes`` made the first review rewrite the deck and then shadow
    future outline edits.
    """
    return str(deck.get("reviewNotes") or "").strip()


def _page_brief_data(deck: dict) -> dict:
    """Compact structured brief for the page.

    Notes live in ``data-brief`` for each deck, so they are omitted here to
    avoid duplicating potentially long text in every select option.
    """
    brief = brief_from_deck(deck)
    return {
        key: brief[key]
        for key in (
            "archetype", "themes", "win_plans", "constraints",
            "must_keep", "avoid",
        )
        if brief.get(key)
    }


def _save_deck_brief(deck_name: str, value) -> None:
    """Persist the structured Deck Brief on the saved deck. Best-effort."""
    if not deck_name:
        return
    brief = normalize_brief(value)
    try:
        path = resolve_json_path(DECKS_DIR, deck_name)
        with file_lock(path):
            deck = read_named_json(path, deck_name)
            if not isinstance(deck, dict):
                return
            if brief_from_deck(deck) == brief:
                return
            if brief_is_empty(brief):
                deck.pop("deckBrief", None)
                deck.pop("reviewNotes", None)
                deck.pop("reviewNotesUpdatedAt", None)
            else:
                brief_to_deck(deck, brief)
                deck["reviewNotesUpdatedAt"] = datetime.now(timezone.utc).isoformat()
            write_json_atomic(path, deck)
    except (StorageError, OSError):
        pass


def _review_deck_name(data: dict) -> str:
    """Saved reports store a deck name; live job results store a deck object."""
    deck = data.get("deck")
    if isinstance(deck, dict):
        return clean_name(deck.get("name", ""))
    return clean_name(deck or "")


def _review_fingerprint(data: dict) -> str:
    deck = data.get("deck")
    if isinstance(deck, dict) and deck.get("fingerprint"):
        return str(deck.get("fingerprint") or "")
    return str(data.get("fingerprint") or "")


def _report_stale(data: dict, cache: dict | None = None) -> tuple[bool, str]:
    """Compare a saved review or live job result with the current saved deck."""
    deck_name = _review_deck_name(data)
    if not deck_name:
        return True, "Deck name is missing."
    expected = _review_fingerprint(data)
    if not expected:
        return True, "This review has no fingerprint."
    cache = cache if cache is not None else {"decks": {}, "guidelines": {}}
    decks = cache.setdefault("decks", {})
    if deck_name not in decks:
        try:
            deck = _load_deck(deck_name)
            decks[deck_name] = enrich_deck(get_db(), deck) if deck is not None else None
        except (StorageError, OSError):
            decks[deck_name] = None
        except Exception:
            decks[deck_name] = None
    enriched = decks[deck_name]
    if enriched is None:
        return True, "The reviewed deck is no longer saved."
    options = data.get("options") if isinstance(data.get("options"), dict) else {}
    guideline_name = str(options.get("guideline") or enriched.get("guideline") or "")
    guidelines = cache.setdefault("guidelines", {})
    if guideline_name not in guidelines:
        guidelines[guideline_name] = deck_review._guideline(guideline_name)
    try:
        current = deck_review.review_fingerprint(
            enriched, options, guidelines[guideline_name]
        )
    except Exception:
        return True, "The reviewed deck could not be re-read."
    if current != expected:
        return True, "The deck changed since this review."
    return False, ""


def _bool_option(value, default=False) -> bool:
    """Parse booleans without treating the string "false" as truthy."""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "on"):
            return True
        if lowered in ("0", "false", "no", "off", ""):
            return False
    return default


def _llm_options() -> dict:
    return {
        "backend": session.get("llm_backend") or os.environ.get("LLM_BACKEND", "openai"),
        "base_url": session.get("llm_base_url") or os.environ.get("LLM_BASE_URL", ""),
        "model": session.get("llm_model") or os.environ.get("LLM_MODEL", ""),
        "api_key": session.get("llm_api_key") or os.environ.get("LLM_API_KEY", ""),
        "timeout": os.environ.get("LLM_TIMEOUT_SECONDS", ""),
    }


@deck_review_bp.route("/deck-review")
def deck_review_page():
    """Full Deck Doctor page; the run UI is client-side."""
    deck_name = clean_name(request.args.get("deck", ""))
    reports = _report_summaries(deck_name) if deck_name else []
    decks = [
        {
            "name": deck.get("name", ""),
            "commander": (deck.get("commander") or {}).get("name", ""),
            "brief": _brief_for_deck(deck),
            "brief_data": _page_brief_data(deck),
            "count": sum(
                _deck_quantity(card)
                for card in (deck.get("cards") or [])
                if isinstance(card, dict)
            ) + (1 if deck.get("commander") else 0),
        }
        for deck in _list_json_files(DECKS_DIR, skip_names=("_tags.json",))
        if isinstance(deck, dict) and deck.get("name")
    ]
    decks.sort(key=lambda deck: deck["name"].lower())
    selected = next((deck for deck in decks if deck["name"] == deck_name), None)
    selected_brief = selected["brief"] if selected else ""
    selected_brief_data = selected["brief_data"] if selected else normalize_brief({})
    return render_template(
        "deck_review.html",
        deck_name=deck_name,
        decks=decks,
        selected_brief=selected_brief,
        selected_brief_data=selected_brief_data,
        reports=reports,
    )


@deck_review_bp.route("/api/deck/review/run", methods=["POST"])
def api_review_run():
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "Request body must be a JSON object."}), 400

    name = clean_name(body.get("name", ""))
    deck = body.get("deck")
    if deck is None and not name:
        return jsonify({"error": "Provide a deck object or a deck name."}), 400
    if deck is not None and not isinstance(deck, dict):
        return jsonify({"error": "deck must be a JSON object."}), 400
    if deck is None:
        try:
            deck = _load_deck(name)
        except StorageError as exc:
            return jsonify({"error": str(exc)}), exc.status_code
        if deck is None:
            return jsonify({"error": "Deck not found."}), 404

    commander = deck.get("commander")
    if not isinstance(commander, dict) or not commander.get("name"):
        return jsonify({"error": "Set a commander before running Deck Doctor."}), 400

    options = body.get("options") if isinstance(body.get("options"), dict) else {}
    raw_brief = body.get("briefData")
    if not isinstance(raw_brief, dict):
        raw_brief = body.get("deckBrief")
    if not isinstance(raw_brief, dict):
        raw_brief = options.get("deckBrief")
    if not isinstance(raw_brief, dict):
        raw_brief = deck.get("deckBrief")
    if "brief" in body:
        notes = str(body.get("brief") or "").strip()
    elif "brief" in options:
        notes = str(options.get("brief") or "").strip()
    elif isinstance(raw_brief, dict) and raw_brief.get("notes"):
        notes = str(raw_brief.get("notes") or "").strip()
    else:
        notes = str(deck.get("reviewNotes") or "").strip()
    brief_data = normalize_brief(raw_brief, notes=notes)
    if name:
        _save_deck_brief(name, brief_data)
    brief_to_deck(deck, brief_data)
    llm = _llm_options()
    requested_use_llm = _bool_option(options.get("use_llm"), True)
    requested_verify_llm = _bool_option(options.get("verify_llm"), False)
    use_llm = requested_use_llm and bool(llm["api_key"])
    verify_llm = requested_verify_llm and use_llm
    try:
        target_bracket = int(options.get("target_bracket"))
        if target_bracket not in (1, 2, 3, 4, 5):
            target_bracket = None
    except (TypeError, ValueError):
        target_bracket = None
    max_adds = deck_review._limit(options.get("max_adds"))
    max_cuts = deck_review._limit(options.get("max_cuts"))
    try:
        combo_count = int(options["combo_count"]) if options.get("combo_count") not in (None, "") else None
    except (KeyError, TypeError, ValueError):
        combo_count = None
    options = {
        **options,
        "use_llm": use_llm,
        "verify_llm": verify_llm,
        "requested_use_llm": requested_use_llm,
        "requested_verify_llm": requested_verify_llm,
        "llm": llm,
        "review_prompt": get_prompt("review_prompt"),
        "guideline": options.get("guideline") or deck.get("guideline") or "",
        "brief": brief_data.get("notes") or "",
        "deckBrief": brief_data,
        "target_bracket": target_bracket,
        "max_adds": max_adds,
        "max_cuts": max_cuts,
        "combo_count": combo_count,
    }

    try:
        enriched = enrich_deck(get_db(), deck)
        guideline = deck_review._guideline(str(options.get("guideline") or ""))
        run_fingerprint = deck_review.review_fingerprint(enriched, options, guideline)
    except Exception:
        run_fingerprint = ""

    job_id = review_cache.create_job(total=5)
    thread = threading.Thread(
        target=deck_review.run_review_job,
        args=(job_id, deck, options),
        daemon=True,
    )
    thread.start()
    return jsonify({
        "success": True,
        "jobId": job_id,
        "fingerprint": run_fingerprint,
    }), 202


@deck_review_bp.route("/api/deck/review/progress")
def api_review_progress():
    job_id = str(request.args.get("jobId", "")).strip()
    data = review_cache.progress(job_id) if job_id else None
    if data is None:
        return jsonify({"error": "Job not found."}), 404
    return jsonify(data)


@deck_review_bp.route("/api/deck/review/result")
def api_review_result():
    job_id = str(request.args.get("jobId", "")).strip()
    job = review_cache.get(job_id) if job_id else None
    if job is None:
        return jsonify({"error": "Job not found."}), 404
    if job["status"] == "error":
        return jsonify({"error": job.get("error") or "Review failed."}), 500
    if job["status"] != "done" or not job.get("result"):
        return jsonify({"error": "Job is still running."}), 409
    stale, stale_reason = _report_stale(job["result"])
    return jsonify({
        "success": True,
        "jobId": job_id,
        "stale": stale,
        "staleReason": stale_reason,
        **job["result"],
    })


def _report_summaries(deck_name: str = "") -> list[dict]:
    reports = []
    # Reuse the enriched deck when several reports belong to the same deck;
    # otherwise listing reviews enriches the same 100-card list once per file.
    cache: dict = {"decks": {}, "guidelines": {}}
    try:
        filenames = sorted(os.listdir(REVIEWS_PATH), reverse=True)
    except OSError:
        return reports
    for filename in filenames:
        if not filename.endswith(".json"):
            continue
        path = _report_path(filename)
        if not path:
            continue
        data = read_json(path, default=None)
        if not isinstance(data, dict):
            continue
        name = _review_deck_name(data)
        if deck_name and name != deck_name:
            continue
        analysis = data.get("analysis") if isinstance(data.get("analysis"), dict) else {}
        audit = data.get("audit") if isinstance(data.get("audit"), dict) else {}
        health = audit.get("health") if isinstance(audit.get("health"), dict) else {}
        bracket = audit.get("bracket") if isinstance(audit.get("bracket"), dict) else {}
        stale, stale_reason = _report_stale(data, cache)
        reports.append({
            "filename": filename,
            "deck": name,
            "fingerprint": _review_fingerprint(data),
            "createdAt": data.get("createdAt", ""),
            "verdict": analysis.get("verdict", ""),
            "health": health.get("score"),
            "bracket": bracket.get("estimate"),
            "stale": stale,
            "staleReason": stale_reason,
        })
    return reports


@deck_review_bp.route("/api/deck/review/reports")
def api_review_reports():
    deck_name = clean_name(request.args.get("deck", ""))
    return jsonify({"reports": _report_summaries(deck_name)})


@deck_review_bp.route("/api/deck/review/save", methods=["POST"])
def api_review_save():
    body = request.get_json(silent=True) or {}
    job_id = str(body.get("jobId", "")).strip()
    job = review_cache.get(job_id) if job_id else None
    if job is None:
        return jsonify({"error": "Job not found."}), 404
    if job["status"] != "done" or not job.get("result"):
        return jsonify({"error": "Job is not finished."}), 409

    result = job["result"]
    result_deck = result.get("deck") if isinstance(result.get("deck"), dict) else {}
    reviewed_name = clean_name(result_deck.get("name", ""))
    requested_name = clean_name(body.get("name", ""))
    if reviewed_name and requested_name and requested_name != reviewed_name:
        return jsonify({
            "error": (
                f"This review belongs to \"{reviewed_name}\". "
                "Save it under that deck, or run a new review."
            )
        }), 409
    deck_name = reviewed_name or requested_name or "deck"
    if not deck_name:
        return jsonify({"error": "Deck name is required."}), 400
    # Microseconds + a collision check so two saves of the same review do not
    # silently replace each other.
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    fingerprint = str(result_deck.get("fingerprint") or "")
    safe = sanitize_filename(deck_name).replace(".json", "")
    filename = f"{safe}__{stamp}__{fingerprint[:8]}.json"
    path = _report_path(filename)
    if not path:
        return jsonify({"error": "Invalid report filename."}), 400
    if os.path.exists(path):
        filename = f"{safe}__{stamp}_{uuid.uuid4().hex[:6]}__{fingerprint[:8]}.json"
        path = _report_path(filename)
        if not path:
            return jsonify({"error": "Invalid report filename."}), 400
    report = {
        **result,
        "schemaVersion": 1,
        "deck": deck_name,
        "commander": result_deck.get("commander") or {},
        "fingerprint": fingerprint,
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }
    try:
        write_json_atomic(path, report)
    except OSError as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"success": True, "filename": filename})


@deck_review_bp.route("/api/deck/review/report")
def api_review_report():
    path = _report_path(request.args.get("filename", ""))
    if not path:
        return jsonify({"error": "Invalid report filename."}), 400
    data = read_json(path, default=None)
    if not isinstance(data, dict):
        return jsonify({"error": "Report not found."}), 404
    stale, stale_reason = _report_stale(data)
    return jsonify({**data, "stale": stale, "staleReason": stale_reason})


@deck_review_bp.route("/api/deck/review/export")
def api_review_export():
    """Download a saved review or completed job as Markdown/HTML."""
    fmt = str(request.args.get("format", "markdown")).lower()
    if fmt not in ("markdown", "md", "html"):
        return jsonify({"error": "format must be markdown or html."}), 400
    filename = str(request.args.get("filename", "")).strip()
    job_id = str(request.args.get("jobId", "")).strip()
    report = None
    if filename:
        path = _report_path(filename)
        if not path:
            return jsonify({"error": "Invalid report filename."}), 400
        report = read_json(path, default=None)
        if not isinstance(report, dict):
            return jsonify({"error": "Report not found."}), 404
    elif job_id:
        job = review_cache.get(job_id)
        if job is None:
            return jsonify({"error": "Job not found."}), 404
        if job["status"] != "done" or not job.get("result"):
            return jsonify({"error": "Job is not finished."}), 409
        report = job["result"]
    else:
        return jsonify({"error": "Provide a filename or jobId."}), 400

    if fmt in ("markdown", "md"):
        content = deck_review.review_to_markdown(report)
        mimetype = "text/markdown; charset=utf-8"
        extension = "md"
    else:
        content = deck_review.review_to_html(report)
        mimetype = "text/html; charset=utf-8"
        extension = "html"
    safe_name = sanitize_filename(deck_review._report_deck_name(report) or "deck-review")
    safe_name = safe_name.replace(".json", "")
    response = make_response(content)
    response.headers["Content-Type"] = mimetype
    response.headers["Content-Disposition"] = (
        f'attachment; filename="{safe_name}-review.{extension}"'
    )
    return response


@deck_review_bp.route("/api/deck/review/delete", methods=["POST"])
def api_review_delete():
    body = request.get_json(silent=True) or {}
    path = _report_path(body.get("filename", ""))
    if not path:
        return jsonify({"error": "Invalid report filename."}), 400
    try:
        os.unlink(path)
    except FileNotFoundError:
        return jsonify({"error": "Report not found."}), 404
    except (OSError, ValueError) as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"success": True})
