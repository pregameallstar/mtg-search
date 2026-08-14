import json
import os

from flask import Blueprint, request, render_template, jsonify

from mtg.shared import TEMPLATES_DIR, sanitize_filename, _list_json_files

templates_bp = Blueprint("templates_bp", __name__)


@templates_bp.route("/card-templates")
def card_templates_page():
    """Card Templates — manage reusable card packages."""
    templates = _list_json_files(TEMPLATES_DIR)
    return render_template("card_templates.html", templates=templates)


@templates_bp.route("/api/template/save", methods=["POST"])
def api_template_save():
    """Save a card template to disk."""
    body = request.get_json(silent=True) or {}
    name = body.get("name", "").strip()
    if not name:
        return jsonify({"error": "Template name is required."}), 400
    fname = sanitize_filename(name)
    fpath = os.path.join(TEMPLATES_DIR, fname)
    try:
        with open(fpath, 'w') as f:
            json.dump(body, f, indent=2)
        return jsonify({"success": True})
    except OSError as e:
        return jsonify({"error": str(e)}), 500


@templates_bp.route("/api/template/list", methods=["GET"])
def api_template_list():
    """List all saved card templates. Accepts optional ?type=guideline or ?type=cards."""
    filter_type = request.args.get("type", "").strip()
    templates = _list_json_files(TEMPLATES_DIR)
    if filter_type:
        templates = [t for t in templates if t.get("type", "cards") == filter_type]
    return jsonify(templates)


@templates_bp.route("/api/template/delete", methods=["POST"])
def api_template_delete():
    """Delete a card template by name."""
    body = request.get_json(silent=True) or {}
    name = body.get("name", "").strip()
    if not name:
        return jsonify({"error": "Template name is required."}), 400
    fname = sanitize_filename(name)
    fpath = os.path.join(TEMPLATES_DIR, fname)
    try:
        os.unlink(fpath)
        return jsonify({"success": True})
    except FileNotFoundError:
        return jsonify({"error": "Template not found."}), 404
    except OSError as e:
        return jsonify({"error": str(e)}), 500
