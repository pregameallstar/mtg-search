import os

from flask import Blueprint, request, render_template, jsonify

from mtg.shared import TEMPLATES_DIR, _list_json_files
from mtg.storage import (
    StorageError,
    clean_name,
    file_lock,
    read_named_json,
    resolve_json_path,
    write_json_atomic,
)

templates_bp = Blueprint("templates_bp", __name__)

# No template may claim a reserved helper filename.
RESERVED_TEMPLATE_FILES = (".json",)


@templates_bp.route("/card-templates")
def card_templates_page():
    """Card Templates — manage reusable card packages."""
    templates = _list_json_files(TEMPLATES_DIR)
    return render_template("card_templates.html", templates=templates)


@templates_bp.route("/api/template/save", methods=["POST"])
def api_template_save():
    """Save a card template to disk."""
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "Template body must be a JSON object."}), 400
    previous_name = clean_name(body.pop("previousName", ""))
    name = clean_name(body.get("name", ""))
    if not name:
        return jsonify({"error": "Template name is required."}), 400
    try:
        fpath = resolve_json_path(TEMPLATES_DIR, name, reserved=RESERVED_TEMPLATE_FILES)
        with file_lock(fpath):
            try:
                read_named_json(fpath, name)
            except StorageError:
                if not previous_name:
                    raise
                read_named_json(fpath, previous_name)
            write_json_atomic(fpath, body)

            if previous_name and previous_name != name:
                try:
                    old_path = resolve_json_path(
                        TEMPLATES_DIR, previous_name, reserved=RESERVED_TEMPLATE_FILES,
                    )
                    if old_path != fpath and os.path.exists(old_path):
                        with file_lock(old_path):
                            if read_named_json(old_path, previous_name) is not None:
                                os.unlink(old_path)
                except StorageError:
                    pass
    except StorageError as e:
        return jsonify({"error": str(e)}), e.status_code
    except OSError as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"success": True})


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
    if not isinstance(body, dict):
        return jsonify({"error": "Template body must be a JSON object."}), 400
    name = clean_name(body.get("name", ""))
    if not name:
        return jsonify({"error": "Template name is required."}), 400
    try:
        fpath = resolve_json_path(TEMPLATES_DIR, name, reserved=RESERVED_TEMPLATE_FILES)
        with file_lock(fpath):
            if read_named_json(fpath, name) is None:
                return jsonify({"error": "Template not found."}), 404
            os.unlink(fpath)
        return jsonify({"success": True})
    except StorageError as e:
        return jsonify({"error": str(e)}), e.status_code
    except FileNotFoundError:
        return jsonify({"error": "Template not found."}), 404
    except OSError as e:
        return jsonify({"error": str(e)}), 500
