"""Blueprint: eval_landing — Commander Eval landing page and report deletion."""

import json
import os

from flask import (
    Blueprint,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from mtg.shared import get_db, REPORTS_DIR

eval_landing_bp = Blueprint("eval_landing", __name__)


@eval_landing_bp.route("/commander-eval", methods=["GET", "POST"])
def commander_eval_landing():
    """Landing page for Commander Eval — search by card name or drop image."""
    session.pop("eval_key", None)  # ponytail: clear stale analysis when returning to landing
    db = get_db()

    # POST: drag-and-drop image lookup
    if request.method == "POST":
        scryfall_id = (request.form.get("scryfall_id") or "").strip()
        if not scryfall_id:
            return {"error": "No card image data received."}, 400
        found = db.execute(
            """
            SELECT c.setCode, c.number FROM cards c
            JOIN cardIdentifiers ci ON c.uuid = ci.uuid
            WHERE ci.scryfallId = ? AND c.language = 'English'
              AND (c.side IS NULL OR c.side = 'a')
            LIMIT 1
        """,
            [scryfall_id],
        ).fetchone()
        if found:
            return {
                "redirect": url_for(
                    "eval.commander_eval",
                    set_code=found["setCode"],
                    number=found["number"],
                )
            }
        return {"error": "Card not found. Try searching by name."}, 404

    # GET: name search
    by_name = request.args.get("by_name", "").strip()
    if by_name:
        found = db.execute(
            """
            SELECT c.setCode, c.number FROM cards c
            JOIN sets s ON c.setCode = s.code
            WHERE c.name = ? AND c.language = 'English' AND (c.side IS NULL OR c.side = 'a')
            ORDER BY s.releaseDate DESC LIMIT 1
        """,
            [by_name],
        ).fetchone()
        if found:
            return redirect(
                url_for(
                    "eval.commander_eval",
                    set_code=found["setCode"],
                    number=found["number"],
                )
            )

    reports_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), REPORTS_DIR)
    saved_reports = []
    if os.path.isdir(reports_dir):
        for fname in sorted(os.listdir(reports_dir), reverse=True):
            if (
                not fname.endswith(".json")
                or fname == ".gitkeep"
                or fname.endswith("_deepdives.json")
            ):
                continue
            try:
                with open(os.path.join(reports_dir, fname)) as f:
                    data = json.load(f)
                card = data.get("card", {})
                saved_reports.append(
                    {
                        "filename": fname,
                        "name": card.get("name", "Unknown"),
                        "setCode": card.get("setCode", ""),
                        "number": card.get("number", ""),
                        "scryfallId": card.get("scryfallId", ""),
                        "savedAt": data.get("saved_at", ""),
                    }
                )
            except (json.JSONDecodeError, OSError):
                continue

    return render_template(
        "commander_eval_landing.html",
        by_name=by_name,
        by_name_error=bool(by_name),
        saved_reports=saved_reports,
    )


@eval_landing_bp.route("/commander-eval/reports/delete", methods=["POST"])
def commander_eval_reports_delete():
    """Delete a saved eval report by filename."""
    body = request.get_json(silent=True) or {}
    filename = body.get("filename", "").strip()
    if not filename or ".." in filename or "/" in filename:
        return jsonify({"error": "Invalid filename."}), 400
    report_path = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), REPORTS_DIR, filename
    )
    try:
        os.unlink(report_path)
    except FileNotFoundError:
        return jsonify({"error": "Report not found."}), 404
    except OSError:
        return jsonify({"error": "Could not delete report."}), 500
    return jsonify({"success": True})
