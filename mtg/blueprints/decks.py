import json
import os
import re

from flask import Blueprint, request, render_template, jsonify

from mtg.shared import get_db, DECKS_DIR, card_image

decks_bp = Blueprint("decks", __name__)


def _sanitize_name(name):
    safe_name = re.sub(r'[^a-zA-Z0-9_ -]', '', name)
    return safe_name.replace(' ', '_') + '.json'


@decks_bp.route("/deck-builder")
def deck_builder_page():
    """Deck Builder — split-panel page: search on left, deck on right."""
    return render_template("deck_builder.html")


@decks_bp.route("/saved-decks")
def saved_decks_page():
    """Saved Decks — grid view of all locally saved decks."""
    decks = []
    if os.path.isdir(DECKS_DIR):
        for fname in sorted(os.listdir(DECKS_DIR)):
            if not fname.endswith('.json') or fname == '_tags.json':
                continue
            fpath = os.path.join(DECKS_DIR, fname)
            try:
                with open(fpath) as f:
                    data = json.load(f)
                decks.append(data)
            except (json.JSONDecodeError, OSError):
                pass
    return render_template("saved_decks.html", decks=decks)


@decks_bp.route("/api/deck/save", methods=["POST"])
def api_deck_save():
    """Save deck to disk as JSON."""
    body = request.get_json(silent=True) or {}
    name = body.get("name", "").strip()
    if not name:
        return jsonify({"error": "Deck name is required."}), 400
    fname = _sanitize_name(name)
    fpath = os.path.join(DECKS_DIR, fname)
    try:
        with open(fpath, 'w') as f:
            json.dump(body, f, indent=2)
        return jsonify({"success": True})
    except OSError as e:
        return jsonify({"error": str(e)}), 500


@decks_bp.route("/api/deck/list", methods=["GET"])
def api_deck_list():
    """List all saved decks."""
    decks = []
    if os.path.isdir(DECKS_DIR):
        for fname in sorted(os.listdir(DECKS_DIR)):
            if not fname.endswith('.json') or fname == '_tags.json':
                continue
            fpath = os.path.join(DECKS_DIR, fname)
            try:
                with open(fpath) as f:
                    data = json.load(f)
                decks.append(data)
            except (json.JSONDecodeError, OSError):
                pass
    return jsonify(decks)


@decks_bp.route("/api/deck/delete", methods=["POST"])
def api_deck_delete():
    """Delete a saved deck by name."""
    body = request.get_json(silent=True) or {}
    name = body.get("name", "").strip()
    if not name:
        return jsonify({"error": "Deck name is required."}), 400
    fname = _sanitize_name(name)
    fpath = os.path.join(DECKS_DIR, fname)
    try:
        os.unlink(fpath)
        return jsonify({"success": True})
    except FileNotFoundError:
        return jsonify({"error": "Deck not found."}), 404
    except OSError as e:
        return jsonify({"error": str(e)}), 500


@decks_bp.route("/api/deck/card-lookup", methods=["POST"])
def deck_card_lookup():
    """Look up a card by setCode+number or uuid. Returns card JSON."""
    body = request.get_json(silent=True) or {}
    db = get_db()

    set_code = body.get("setCode", "").strip()
    number = body.get("number", "").strip()
    uuid_val = body.get("uuid", "").strip()

    if uuid_val:
        card = db.execute("""
            SELECT c.*, ci.scryfallId, s.name as setName, s.releaseDate
            FROM cards c
            JOIN cardIdentifiers ci ON c.uuid = ci.uuid
            JOIN sets s ON c.setCode = s.code
            WHERE c.uuid = ? AND c.language = 'English'
              AND (c.side IS NULL OR c.side = 'a')
        """, [uuid_val]).fetchone()
    elif set_code and number:
        card = db.execute("""
            SELECT c.*, ci.scryfallId, s.name as setName, s.releaseDate
            FROM cards c
            JOIN cardIdentifiers ci ON c.uuid = ci.uuid
            JOIN sets s ON c.setCode = s.code
            WHERE c.setCode = ? AND c.number = ? AND c.language = 'English'
              AND (c.side IS NULL OR c.side = 'a')
        """, [set_code, number]).fetchone()
    else:
        return jsonify({"error": "Provide setCode+number or uuid."}), 400

    if not card:
        return jsonify({"error": "Card not found."}), 404

    return jsonify({
        "uuid": card["uuid"],
        "name": card["name"],
        "setCode": card["setCode"],
        "number": card["number"],
        "scryfallId": card["scryfallId"],
        "manaCost": card["manaCost"] or "",
        "manaValue": card["manaValue"],
        "type": card["type"] or "",
        "types": card["types"] or "",
        "subtypes": card["subtypes"] or "",
        "supertypes": card["supertypes"] or "",
        "colors": card["colors"] or "",
        "colorIdentity": card["colorIdentity"] or "",
        "text": card["text"] or "",
        "power": card["power"],
        "toughness": card["toughness"],
        "loyalty": card["loyalty"],
        "rarity": card["rarity"] or "",
        "imageUrl": card_image(card["scryfallId"]),
        "isLegendary": "Legendary" in (card["type"] or ""),
        "isCreature": "Creature" in (card["type"] or ""),
    })


@decks_bp.route("/api/deck/lookup-by-name", methods=["POST"])
def deck_lookup_by_name():
    """Look up a card by exact name (case-insensitive). Returns latest printing."""
    body = request.get_json(silent=True) or {}
    name = body.get("name", "").strip()
    if not name:
        return jsonify({"error": "No card name provided."}), 400

    db = get_db()
    card = db.execute("""
        SELECT c.*, ci.scryfallId, s.name as setName
        FROM cards c
        JOIN cardIdentifiers ci ON c.uuid = ci.uuid
        JOIN sets s ON c.setCode = s.code
        WHERE LOWER(c.name) = LOWER(?) AND c.language = 'English'
          AND (c.side IS NULL OR c.side = 'a')
        ORDER BY s.releaseDate DESC
        LIMIT 1
    """, [name]).fetchone()

    if not card:
        return jsonify({"error": f"Card not found: {name}"}), 404

    return jsonify({
        "uuid": card["uuid"],
        "name": card["name"],
        "setCode": card["setCode"],
        "number": card["number"],
        "scryfallId": card["scryfallId"],
        "manaCost": card["manaCost"] or "",
        "manaValue": card["manaValue"],
        "type": card["type"] or "",
        "types": card["types"] or "",
        "subtypes": card["subtypes"] or "",
        "supertypes": card["supertypes"] or "",
        "colors": card["colors"] or "",
        "colorIdentity": card["colorIdentity"] or "",
        "text": card["text"] or "",
        "power": card["power"],
        "toughness": card["toughness"],
        "loyalty": card["loyalty"],
        "rarity": card["rarity"] or "",
        "imageUrl": card_image(card["scryfallId"]),
        "isLegendary": "Legendary" in (card["type"] or ""),
        "isCreature": "Creature" in (card["type"] or ""),
    })


@decks_bp.route("/api/deck/lookup-by-uuid", methods=["POST"])
def deck_lookup_by_uuid():
    """Given a Scryfall UUID, find the card and return its setCode + number."""
    body = request.get_json(silent=True) or {}
    scryfall_id = body.get("scryfallId", "").strip()
    if not scryfall_id:
        return jsonify({"error": "No scryfallId provided."}), 400

    db = get_db()
    card = db.execute("""
        SELECT c.setCode, c.number, c.uuid, c.name
        FROM cards c
        JOIN cardIdentifiers ci ON c.uuid = ci.uuid
        WHERE ci.scryfallId = ? AND c.language = 'English'
          AND (c.side IS NULL OR c.side = 'a')
    """, [scryfall_id]).fetchone()

    if not card:
        return jsonify({"error": "Card not found in database."}), 404

    return jsonify({
        "setCode": card["setCode"],
        "number": card["number"],
        "uuid": card["uuid"],
        "name": card["name"],
    })
