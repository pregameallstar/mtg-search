"""Deck Doctor routes — review jobs, reports, and the report page."""

from __future__ import annotations

import os
import threading
from datetime import datetime, timezone

from flask import (
    Blueprint,
    jsonify,
    render_template,
    request,
    session,
)
import mtg.review_cache as review_cache
from mtg import deck_review
from mtg.prompt_store import get_prompt
from mtg.shared import DECKS_DIR, REVIEWS_PATH, _list_json_files, sanitize_filename
from mtg.storage import (
    StorageError,
    clean_name,
    read_json,
    read_named_json,
    resolve_json_path,
    write_json_atomic,
)

deck_review_bp = Blueprint("deck_review", __name__)


def _load_deck(name: str) -> dict | None:
    path = resolve_json_path(DECKS_DIR, name)
    return read_named_json(path, name)


def _report_path(filename: str) -> str | None:
    filename = str(filename or "").strip()
    if not filename or filename != os.path.basename(filename):
        return None
    if "/" in filename or "\\" in filename or ".." in filename or not filename.endswith(".json"):
        return None
    return os.path.join(REVIEWS_PATH, filename)


def _llm_options() -> dict:
    return {
        "backend": session.get("llm_backend") or os.environ.get("LLM_BACKEND", "openai"),
        "base_url": session.get("llm_base_url") or os.environ.get("LLM_BASE_URL", ""),
        "model": session.get("llm_model") or os.environ.get("LLM_MODEL", ""),
        "api_key": session.get("llm_api_key") or os.environ.get("LLM_API_KEY", ""),
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
            "count": sum(
                int(card.get("quantity") or 1)
                for card in (deck.get("cards") or [])
                if isinstance(card, dict)
            ) + (1 if deck.get("commander") else 0),
        }
        for deck in _list_json_files(DECKS_DIR, skip_names=("_tags.json",))
        if isinstance(deck, dict) and deck.get("name")
    ]
    decks.sort(key=lambda deck: deck["name"].lower())
    return render_template(
        "deck_review.html",
        deck_name=deck_name,
        decks=decks,
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
    llm = _llm_options()
    use_llm = bool(options.get("use_llm", True)) and bool(llm["api_key"])
    try:
        max_adds = min(max(int(options.get("max_adds", 10)), 0), 20)
    except (TypeError, ValueError):
        max_adds = 10
    try:
        max_cuts = min(max(int(options.get("max_cuts", 10)), 0), 20)
    except (TypeError, ValueError):
        max_cuts = 10
    options = {
        **options,
        "use_llm": use_llm,
        "llm": llm,
        "review_prompt": get_prompt("review_prompt"),
        "guideline": options.get("guideline") or deck.get("guideline") or "",
        "max_adds": max_adds,
        "max_cuts": max_cuts,
    }

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
        "fingerprint": deck_review.deck_fingerprint(
            deck, deck_review.fingerprint_options(options)
        ),
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
    return jsonify({"success": True, "jobId": job_id, **job["result"]})


def _report_summaries(deck_name: str = "") -> list[dict]:
    reports = []
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
        name = str(data.get("deck") or "")
        if deck_name and name != deck_name:
            continue
        analysis = data.get("analysis") if isinstance(data.get("analysis"), dict) else {}
        audit = data.get("audit") if isinstance(data.get("audit"), dict) else {}
        health = audit.get("health") if isinstance(audit.get("health"), dict) else {}
        bracket = audit.get("bracket") if isinstance(audit.get("bracket"), dict) else {}
        reports.append({
            "filename": filename,
            "deck": name,
            "fingerprint": data.get("fingerprint", ""),
            "createdAt": data.get("createdAt", ""),
            "verdict": analysis.get("verdict", ""),
            "health": health.get("score"),
            "bracket": bracket.get("estimate"),
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
    deck_name = clean_name(body.get("name") or result["deck"].get("name") or "deck")
    if not deck_name:
        return jsonify({"error": "Deck name is required."}), 400
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fingerprint = str(result["deck"].get("fingerprint") or "")
    safe = sanitize_filename(deck_name).replace(".json", "")
    filename = f"{safe}__{timestamp}__{fingerprint[:8]}.json"
    path = _report_path(filename)
    if not path:
        return jsonify({"error": "Invalid report filename."}), 400
    report = {
        **result,
        "schemaVersion": 1,
        "deck": deck_name,
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
    return jsonify(data)


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
    except OSError as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"success": True})
