"""Tag catalog — CRUD for deck card tags."""
import json
import os

from flask import Blueprint, request, jsonify

from mtg.shared import DECKS_DIR
from mtg.storage import file_lock, write_json_atomic

tags_bp = Blueprint("tags", __name__, url_prefix="/api/tags")

TAGS_FILE = os.path.join(DECKS_DIR, "_tags.json")
PREDEFINED_TAGS = [
    {"name": "Ramp", "color": "#4caf50"},
    {"name": "Removal", "color": "#f44336"},
    {"name": "Card Draw", "color": "#2196f3"},
    {"name": "Tutor", "color": "#9c27b0"},
    {"name": "Board Wipe", "color": "#ff9800"},
    {"name": "Protection", "color": "#009688"},
    {"name": "Recursion", "color": "#795548"},
    {"name": "Win Condition", "color": "#ffc107"},
]


@tags_bp.route("/list", methods=["GET"])
def api_tags_list():
    """Return the global tag catalog. Seeds predefined tags on first access."""
    if not os.path.exists(TAGS_FILE):
        with file_lock(TAGS_FILE):
            if not os.path.exists(TAGS_FILE):
                write_json_atomic(TAGS_FILE, PREDEFINED_TAGS)
        return jsonify({"tags": PREDEFINED_TAGS})
    try:
        with open(TAGS_FILE) as f:
            tags = json.load(f)
        if not isinstance(tags, list) or not all(isinstance(t, dict) for t in tags):
            return jsonify({"tags": PREDEFINED_TAGS})
        return jsonify({"tags": tags})
    except (json.JSONDecodeError, OSError):
        return jsonify({"tags": PREDEFINED_TAGS})


@tags_bp.route("/save", methods=["POST"])
def api_tags_save():
    """Save the global tag catalog."""
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "Request body must be a JSON object."}), 400
    tags = body.get("tags", [])
    if not isinstance(tags, list) or not all(isinstance(t, dict) for t in tags):
        return jsonify({"error": "tags must be an array of objects."}), 400
    try:
        with file_lock(TAGS_FILE):
            write_json_atomic(TAGS_FILE, tags)
        return jsonify({"success": True})
    except OSError as e:
        return jsonify({"error": str(e)}), 500
