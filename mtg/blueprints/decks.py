import json
import os
import re
from datetime import datetime, timezone

from flask import Blueprint, request, render_template, jsonify, session

from mtg.shared import get_db, DECKS_DIR, HISTORY_DIR, card_image

decks_bp = Blueprint("decks", __name__)


def _sanitize_name(name):
    safe_name = re.sub(r'[^a-zA-Z0-9_ -]', '', name)
    return safe_name.replace(' ', '_') + '.json'


@decks_bp.route("/deck-builder")
def deck_builder_page():
    """Deck Builder — split-panel page: search on left, deck on right."""
    pricing_store = session.get("pricing_store") or os.environ.get("PRICING_STORE", "usd")
    history_warning_days = session.get("history_warning_days", "30")
    return render_template("deck_builder.html",
                           pricing_store=pricing_store,
                           history_warning_days=history_warning_days)


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


# --- Deck History ---

@decks_bp.route("/api/deck/history", methods=["GET"])
def api_deck_history():
    """Return history array for a deck."""
    name = request.args.get("deck", "").strip()
    if not name:
        return jsonify({"error": "Deck name is required."}), 400
    if ".." in name or "/" in name:
        return jsonify({"error": "Invalid deck name."}), 400
    fname = _sanitize_name(name)
    fpath = os.path.join(HISTORY_DIR, fname)
    try:
        with open(fpath) as f:
            history = json.load(f)
        return jsonify({"success": True, "history": history})
    except FileNotFoundError:
        return jsonify({"success": True, "history": []})
    except (json.JSONDecodeError, OSError) as e:
        return jsonify({"error": str(e)}), 500


@decks_bp.route("/api/deck/history/save", methods=["POST"])
def api_deck_history_save():
    """Append events to a deck's history file."""
    body = request.get_json(silent=True) or {}
    name = body.get("name", "").strip()
    events = body.get("events", [])
    if not name:
        return jsonify({"error": "Deck name is required."}), 400
    if not isinstance(events, list) or len(events) == 0:
        return jsonify({"error": "events array required."}), 400
    if ".." in name or "/" in name:
        return jsonify({"error": "Invalid deck name."}), 400
    fname = _sanitize_name(name)
    fpath = os.path.join(HISTORY_DIR, fname)
    try:
        existing = []
        if os.path.exists(fpath):
            with open(fpath) as f:
                existing = json.load(f)
        existing.extend(events)
        with open(fpath, "w") as f:
            json.dump(existing, f, indent=2)
        return jsonify({"success": True, "count": len(existing)})
    except OSError as e:
        return jsonify({"error": str(e)}), 500


@decks_bp.route("/api/deck/history/delete", methods=["POST"])
def api_deck_history_delete():
    """Delete the history file for a deck."""
    body = request.get_json(silent=True) or {}
    name = body.get("name", "").strip()
    if not name:
        return jsonify({"error": "Deck name is required."}), 400
    if ".." in name or "/" in name:
        return jsonify({"error": "Invalid deck name."}), 400
    fname = _sanitize_name(name)
    fpath = os.path.join(HISTORY_DIR, fname)
    try:
        os.unlink(fpath)
        return jsonify({"success": True})
    except FileNotFoundError:
        return jsonify({"error": "History not found."}), 404
    except OSError as e:
        return jsonify({"error": str(e)}), 500


@decks_bp.route("/api/deck/history/check", methods=["GET"])
def api_deck_history_check():
    """Check if any deck needs a history cleanup reminder.

    Query param: threshold_days (default 30).
    Returns decks whose last history event is >= threshold_days ago.
    """
    try:
        threshold = int(request.args.get("threshold_days", "30"))
    except ValueError:
        threshold = 30

    reminders = []
    if not os.path.isdir(HISTORY_DIR):
        return jsonify({"reminders": []})

    now = datetime.now(timezone.utc)
    for fname in os.listdir(HISTORY_DIR):
        if not fname.endswith(".json"):
            continue
        fpath = os.path.join(HISTORY_DIR, fname)
        try:
            with open(fpath) as f:
                history = json.load(f)
            if not history:
                continue
            last_event = max(history, key=lambda e: e.get("timestamp", ""))
            last_ts = last_event.get("timestamp", "")
            if not last_ts:
                continue
            # ponytail: handle Z suffix compatibly across Python versions
            last_dt = datetime.fromisoformat(last_ts.replace("Z", "+00:00"))
            days = (now - last_dt).days
            if days >= threshold:
                deck_name = fname.replace("_", " ").replace(".json", "")
                reminders.append({"deck": deck_name, "days": days})
        except (json.JSONDecodeError, OSError, ValueError):
            pass

    return jsonify({"reminders": reminders})
