"""Tag catalog — CRUD for deck card tags."""
import json
import os

from flask import Blueprint, request, jsonify

from mtg.shared import DECKS_DIR

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
        with open(TAGS_FILE, "w") as f:
            json.dump(PREDEFINED_TAGS, f, indent=2)
        return jsonify({"tags": PREDEFINED_TAGS})
    try:
        with open(TAGS_FILE) as f:
            tags = json.load(f)
        return jsonify({"tags": tags})
    except (json.JSONDecodeError, OSError):
        return jsonify({"tags": PREDEFINED_TAGS})


@tags_bp.route("/save", methods=["POST"])
def api_tags_save():
    """Save the global tag catalog."""
    body = request.get_json(silent=True) or {}
    tags = body.get("tags", [])
    try:
        with open(TAGS_FILE, "w") as f:
            json.dump(tags, f, indent=2)
        return jsonify({"success": True})
    except OSError as e:
        return jsonify({"error": str(e)}), 500
