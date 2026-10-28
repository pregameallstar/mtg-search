"""Deck version snapshots, history comparison, and deck-twin comparison APIs."""

from __future__ import annotations

import os

from flask import Blueprint, jsonify, render_template, request

from mtg.deck_versions import (
    compare_decks,
    list_versions,
    load_version,
    write_version,
)
from mtg.shared import DECKS_DIR, _list_json_files
from mtg.storage import (
    StorageError,
    clean_name,
    file_lock,
    read_named_json,
    resolve_json_path,
    write_json_atomic,
)

deck_versions_bp = Blueprint("deck_versions", __name__)


def _versions_dir() -> str:
    return os.path.join(DECKS_DIR, "_versions")


def _load_deck(name: str) -> dict | None:
    path = resolve_json_path(DECKS_DIR, name)
    return read_named_json(path, name)


def _version_filename(filename: str) -> str | None:
    filename = str(filename or "").strip()
    if not filename or filename != os.path.basename(filename):
        return None
    if "/" in filename or "\\" in filename or ".." in filename:
        return None
    if not filename.endswith(".json"):
        return None
    return filename


def _decks_for_page() -> list[dict]:
    decks = []
    for deck in _list_json_files(DECKS_DIR, skip_names=("_tags.json",)):
        if not isinstance(deck, dict) or not deck.get("name"):
            continue
        decks.append({
            "name": deck.get("name", ""),
            "commander": (deck.get("commander") or {}).get("name", "")
            if isinstance(deck.get("commander"), dict) else "",
        })
    decks.sort(key=lambda item: item["name"].lower())
    return decks


@deck_versions_bp.route("/compare-decks")
def compare_decks_page():
    return render_template(
        "compare_decks.html",
        decks=_decks_for_page(),
        a=clean_name(request.args.get("a", "")),
        b=clean_name(request.args.get("b", "")),
    )


@deck_versions_bp.route("/api/deck/versions")
def api_versions_list():
    deck_name = clean_name(request.args.get("deck", ""))
    if not deck_name:
        return jsonify({"error": "Deck name is required."}), 400
    return jsonify({"success": True, "versions": list_versions(_versions_dir(), deck_name)})


@deck_versions_bp.route("/api/deck/versions/snapshot", methods=["POST"])
def api_versions_snapshot():
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "Request body must be a JSON object."}), 400
    name = clean_name(body.get("name", ""))
    deck = body.get("deck")
    if deck is None and not name:
        return jsonify({"error": "Provide a deck name or deck object."}), 400
    if deck is None:
        try:
            deck = _load_deck(name)
        except StorageError as exc:
            return jsonify({"error": str(exc)}), exc.status_code
        if deck is None:
            return jsonify({"error": "Deck not found."}), 404
    if not isinstance(deck, dict) or not deck.get("name"):
        return jsonify({"error": "Deck must be a named JSON object."}), 400
    try:
        filename, reason = write_version(_versions_dir(), deck, force=True)
    except OSError as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({
        "success": filename is not None,
        "filename": filename,
        "reason": reason,
    })


@deck_versions_bp.route("/api/deck/version")
def api_version_load():
    deck_name = clean_name(request.args.get("deck", ""))
    filename = _version_filename(request.args.get("filename", ""))
    if not deck_name or not filename:
        return jsonify({"error": "Deck name and filename are required."}), 400
    snapshot = load_version(_versions_dir(), filename)
    if snapshot is None:
        return jsonify({"error": "Version not found."}), 404
    return jsonify({"success": True, "deck": snapshot})


@deck_versions_bp.route("/api/deck/versions/restore", methods=["POST"])
def api_versions_restore():
    body = request.get_json(silent=True) or {}
    deck_name = clean_name(body.get("deck", ""))
    filename = _version_filename(body.get("filename", ""))
    if not deck_name or not filename:
        return jsonify({"error": "Deck name and filename are required."}), 400
    snapshot = load_version(_versions_dir(), filename)
    if snapshot is None:
        return jsonify({"error": "Version not found."}), 404
    snapshot = dict(snapshot)
    snapshot["name"] = deck_name
    try:
        path = resolve_json_path(DECKS_DIR, deck_name)
        with file_lock(path):
            current = read_named_json(path, deck_name)
            if isinstance(current, dict):
                # Preserve the pre-restore state so the restore is reversible.
                write_version(_versions_dir(), current, force=True)
            write_json_atomic(path, snapshot)
    except StorageError as exc:
        return jsonify({"error": str(exc)}), exc.status_code
    except OSError as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"success": True, "deck": snapshot})


@deck_versions_bp.route("/api/deck/versions/delete", methods=["POST"])
def api_versions_delete():
    body = request.get_json(silent=True) or {}
    filename = _version_filename(body.get("filename", ""))
    if not filename:
        return jsonify({"error": "Invalid version filename."}), 400
    path = os.path.join(_versions_dir(), filename)
    try:
        os.unlink(path)
    except FileNotFoundError:
        return jsonify({"error": "Version not found."}), 404
    except OSError as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"success": True})


@deck_versions_bp.route("/api/deck/compare", methods=["POST"])
def api_deck_compare():
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "Request body must be a JSON object."}), 400

    def resolve(value):
        if isinstance(value, dict):
            return value
        name = clean_name(value)
        if not name:
            return None
        return _load_deck(name)

    deck_a = resolve(body.get("a"))
    deck_b = resolve(body.get("b"))
    if not isinstance(deck_a, dict) or not isinstance(deck_b, dict):
        return jsonify({"error": "Two decks are required."}), 400
    try:
        comparison = compare_decks(deck_a, deck_b)
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500
    return jsonify({"success": True, "comparison": comparison})


@deck_versions_bp.route("/api/deck/version/diff")
def api_version_diff():
    deck_name = clean_name(request.args.get("deck", ""))
    filename = _version_filename(request.args.get("filename", ""))
    if not deck_name or not filename:
        return jsonify({"error": "Deck name and filename are required."}), 400
    current = _load_deck(deck_name)
    snapshot = load_version(_versions_dir(), filename)
    if current is None:
        return jsonify({"error": "Deck not found."}), 404
    if snapshot is None:
        return jsonify({"error": "Version not found."}), 404
    return jsonify({"success": True, "comparison": compare_decks(current, snapshot)})
