import os
from datetime import datetime, timezone

from flask import Blueprint, request, render_template, jsonify, session

from mtg.shared import (
    get_db,
    DECKS_DIR,
    HISTORY_DIR,
    card_image,
    _list_json_files,
)
from mtg.deck_versions import write_version
from mtg.roles import ROLES_VERSION, classify_roles
from mtg.storage import (
    StorageError,
    clean_name,
    file_lock,
    read_json,
    read_named_json,
    resolve_json_path,
    write_json_atomic,
)

decks_bp = Blueprint("decks", __name__)

# A deck must never be allowed to take over the tag catalog's filename (or the
# history filename). Sanitization maps a deck literally named "_tags" to
# "_tags.json", which would otherwise corrupt the global tag store.
RESERVED_DECK_FILES = ("_tags.json", "_history.json")


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
    decks = _list_json_files(DECKS_DIR, skip_names=("_tags.json",))
    return render_template("saved_decks.html", decks=decks)


@decks_bp.route("/api/deck/save", methods=["POST"])
def api_deck_save():
    """Save deck to disk as JSON.

    ponytail: no server-side validation of the card list, commander legality,
    or color-identity compliance — validation is client-side (deck-builder.js).
    Downstream consumers (pricing, sectioning, history) tolerate malformed
    decks defensively, so a hand-crafted body just produces a "broken" deck
    rather than a crash.
    """
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "Deck body must be a JSON object."}), 400
    # The client sends its last successfully saved name so a rename can be
    # distinguished from an accidental collision with another deck.
    previous_name = clean_name(body.pop("previousName", ""))
    name = clean_name(body.get("name", ""))
    if not name:
        return jsonify({"error": "Deck name is required."}), 400
    try:
        fpath = resolve_json_path(DECKS_DIR, name, reserved=RESERVED_DECK_FILES)
        with file_lock(fpath):
            # Refuse to overwrite a different display name that sanitizes to
            # the same filename (e.g. "Alela, Artful" vs "Alela Artful"),
            # unless the caller is renaming the deck it last saved.
            existing = None
            try:
                existing = read_named_json(fpath, name)
            except StorageError:
                if not previous_name:
                    raise
                existing = read_named_json(fpath, previous_name)
            if existing is None and previous_name:
                # A rename writes a new filename; carry the brief over from the
                # previous file if the new one does not exist yet.
                try:
                    old_path = resolve_json_path(
                        DECKS_DIR, previous_name, reserved=RESERVED_DECK_FILES
                    )
                    existing = read_named_json(old_path, previous_name)
                except (StorageError, OSError):
                    existing = None
            # The Deck Brief is edited by Deck Doctor, not the Deck Builder.
            # A builder tab opened before the brief was written would otherwise
            # send its stale empty copy and erase it. Only Deck Doctor writes
            # these two fields; preserve the on-disk values across deck saves.
            if isinstance(existing, dict):
                for key in ("reviewNotes", "reviewNotesUpdatedAt", "deckBrief"):
                    if key in existing:
                        body[key] = existing[key]
                    else:
                        body.pop(key, None)
            write_json_atomic(fpath, body)
            # Baseline/first snapshot, then at most one automatic snapshot per
            # 10 minutes. The History tab can force an explicit snapshot.
            try:
                write_version(os.path.join(DECKS_DIR, "_versions"), body,
                              min_interval=600)
            except Exception:
                # Versioning is best-effort; never fail the deck save for it.
                pass
    except StorageError as e:
        return jsonify({"error": str(e)}), e.status_code
    except OSError as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"success": True})


@decks_bp.route("/api/deck/list", methods=["GET"])
def api_deck_list():
    """List all saved decks."""
    decks = _list_json_files(DECKS_DIR, skip_names=("_tags.json",))
    return jsonify(decks)


@decks_bp.route("/api/deck/delete", methods=["POST"])
def api_deck_delete():
    """Delete a saved deck by name."""
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "Deck body must be a JSON object."}), 400
    name = clean_name(body.get("name", ""))
    if not name:
        return jsonify({"error": "Deck name is required."}), 400
    try:
        fpath = resolve_json_path(DECKS_DIR, name, reserved=RESERVED_DECK_FILES)
        with file_lock(fpath):
            if read_named_json(fpath, name) is None:
                return jsonify({"error": "Deck not found."}), 404
            os.unlink(fpath)
        return jsonify({"success": True})
    except StorageError as e:
        return jsonify({"error": str(e)}), e.status_code
    except FileNotFoundError:
        return jsonify({"error": "Deck not found."}), 404
    except OSError as e:
        return jsonify({"error": str(e)}), 500


def _lookup_card(set_code="", number="", uuid_val=""):
    """Return the primary English card row (with scryfallId + set info) by
    setCode+number or by uuid. Single source of truth for card lookups in deck
    routes (card-lookup, add-card)."""
    db = get_db()
    if uuid_val:
        return db.execute("""
            SELECT c.*, ci.scryfallId, s.name as setName, s.releaseDate
            FROM cards c
            JOIN cardIdentifiers ci ON c.uuid = ci.uuid
            JOIN sets s ON c.setCode = s.code
            WHERE c.uuid = ? AND c.language = 'English'
              AND (c.side IS NULL OR c.side = 'a')
        """, [uuid_val]).fetchone()
    if set_code and number:
        return db.execute("""
            SELECT c.*, ci.scryfallId, s.name as setName, s.releaseDate
            FROM cards c
            JOIN cardIdentifiers ci ON c.uuid = ci.uuid
            JOIN sets s ON c.setCode = s.code
            WHERE c.setCode = ? AND c.number = ? AND c.language = 'English'
              AND (c.side IS NULL OR c.side = 'a')
        """, [set_code, number]).fetchone()
    return None


BASIC_LAND_NAMES = {"Plains", "Island", "Swamp", "Mountain", "Forest", "Wastes"}


def _singleton_exempt(card):
    """Commander allows one copy except basic lands / explicit any-number cards."""
    if "Basic" in (card.get("supertypes") or ""):
        return True
    if card.get("name") in BASIC_LAND_NAMES:
        return True
    return "a deck can have any number of cards named" in (card.get("text") or "").lower()


def _card_to_deck_json(card):
    """Map a looked-up card row to the saved-deck card shape.

    Mirrors `cardMapper` in static/deck-builder.js `Deck.prototype.toExport()`
    exactly (15 fields), so server-side additions and client-side saves produce
    identical card objects.
    """
    role_info = classify_roles(dict(card))
    return {
        "uuid": card["uuid"],
        "setCode": card["setCode"],
        "number": card["number"],
        "name": card["name"],
        "types": card["types"] or "",
        "manaCost": card["manaCost"] or "",
        "manaValue": card["manaValue"],
        "colors": card["colors"] or "",
        "colorIdentity": card["colorIdentity"] or "",
        "imageUrl": card_image(card["scryfallId"]),
        "quantity": 1,
        "text": card["text"] or "",
        "supertypes": card["supertypes"] or "",
        "tags": [],
        "scryfallId": card["scryfallId"] or "",
        "roles": role_info["roles"],
        "rolePrimary": role_info["primary"],
        "rolesVersion": ROLES_VERSION,
    }


@decks_bp.route("/api/deck/card-lookup", methods=["POST"])
def deck_card_lookup():
    """Look up a card by setCode+number or uuid. Returns card JSON."""
    body = request.get_json(silent=True) or {}

    set_code = clean_name(body.get("setCode", ""))
    number = clean_name(body.get("number", ""))
    uuid_val = clean_name(body.get("uuid", ""))

    if not uuid_val and not (set_code and number):
        return jsonify({"error": "Provide setCode+number or uuid."}), 400

    card = _lookup_card(set_code, number, uuid_val)
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


@decks_bp.route("/api/deck/add-card", methods=["POST"])
def api_deck_add_card():
    """Add a card to a saved deck's main list or a named section.

    Body: {"deck": "<name>", "section": "<optional>", "setCode": "...", "number": "..."}
       or {"deck": "<name>", "section": "<optional>", "uuid": "..."}

    ponytail: server-side read-modify-write of the deck JSON so the standalone
    Search/Similarity pages (which don't load deck-builder.js) can add a card
    without shipping whole decks over the wire. If the card's uuid already
    exists in the target, its quantity is incremented (matching addToSection).
    """
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "Request body must be a JSON object."}), 400
    deck_name = clean_name(body.get("deck", ""))
    section = clean_name(body.get("section"))
    set_code = clean_name(body.get("setCode", ""))
    number = clean_name(body.get("number", ""))
    uuid_val = clean_name(body.get("uuid", ""))

    if not deck_name:
        return jsonify({"error": "Deck name is required."}), 400
    if not uuid_val and not (set_code and number):
        return jsonify({"error": "Provide setCode+number or uuid."}), 400

    card = _lookup_card(set_code, number, uuid_val)
    if not card:
        return jsonify({"error": "Card not found."}), 404

    try:
        fpath = resolve_json_path(DECKS_DIR, deck_name, reserved=RESERVED_DECK_FILES)
        with file_lock(fpath):
            deck = read_named_json(fpath, deck_name)
            if deck is None:
                return jsonify({"error": "Deck not found."}), 404

            deck_card = _card_to_deck_json(card)

            if section:
                sections = deck.get("sections")
                if not isinstance(sections, dict):
                    # Older saves may store [] for "no sections"; normalize in place.
                    sections = {}
                    deck["sections"] = sections
                target = sections.get(section)
                if not isinstance(target, list):
                    target = []
                    sections[section] = target
            else:
                target = deck.get("cards")
                if not isinstance(target, list):
                    target = []
                    deck["cards"] = target

            if not section:
                def _deck_total():
                    total = 0
                    for c in target:
                        if not isinstance(c, dict):
                            continue
                        try:
                            total += int(c.get("quantity") or 1)
                        except (TypeError, ValueError):
                            total += 1
                    if isinstance(deck.get("commander"), dict):
                        total += 1
                    return total

                if _deck_total() >= 100:
                    return jsonify({"error": "Deck is full (100 cards)."}), 409

            existing = next(
                (c for c in target if isinstance(c, dict) and c.get("uuid") == deck_card["uuid"]),
                None,
            )
            if not section and existing and not _singleton_exempt(deck_card):
                return jsonify({
                    "error": "Only one copy of this card is allowed in a Commander deck."
                }), 409
            if existing:
                existing["quantity"] = existing.get("quantity", 1) + 1
            else:
                target.append(deck_card)

            write_json_atomic(fpath, deck)
    except StorageError as e:
        return jsonify({"error": str(e)}), e.status_code
    except OSError as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({"success": True, "name": card["name"]})


@decks_bp.route("/api/deck/remove-card", methods=["POST"])
def api_deck_remove_card():
    """Remove a card from a saved deck's main list or a named section.

    Body: {"deck": "<name>", "name": "<card name>", "section": "<optional>",
           "quantity": <optional>}
    """
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "Request body must be a JSON object."}), 400
    deck_name = clean_name(body.get("deck", ""))
    name = clean_name(body.get("name", ""))
    uuid_val = clean_name(body.get("uuid", ""))
    section = clean_name(body.get("section"))
    if not deck_name:
        return jsonify({"error": "Deck name is required."}), 400
    if not name and not uuid_val:
        return jsonify({"error": "Provide a card name or uuid."}), 400
    try:
        qty = int(body.get("quantity") or 0)
    except (TypeError, ValueError):
        qty = 0

    try:
        fpath = resolve_json_path(DECKS_DIR, deck_name, reserved=RESERVED_DECK_FILES)
        with file_lock(fpath):
            deck = read_named_json(fpath, deck_name)
            if deck is None:
                return jsonify({"error": "Deck not found."}), 404
            if section:
                sections = deck.get("sections")
                target = sections.get(section) if isinstance(sections, dict) else None
                if not isinstance(target, list):
                    return jsonify({"error": "Section not found."}), 404
            else:
                target = deck.get("cards")
                if not isinstance(target, list):
                    target = []
                    deck["cards"] = target

            def matches(card):
                if not isinstance(card, dict):
                    return False
                if uuid_val:
                    return card.get("uuid") == uuid_val
                return (
                    str(card.get("name") or "").lower() == name.lower()
                    or str(card.get("name") or "").split(" // ", 1)[0].lower() == name.lower()
                )

            found = next((card for card in target if matches(card)), None)
            if found is None:
                return jsonify({"error": "Card not found in deck."}), 404

            if qty and qty > 0:
                current = int(found.get("quantity") or 1)
                if qty >= current:
                    target.remove(found)
                else:
                    found["quantity"] = current - qty
            else:
                target.remove(found)
            write_json_atomic(fpath, deck)
    except StorageError as e:
        return jsonify({"error": str(e)}), e.status_code
    except OSError as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"success": True, "name": name})


@decks_bp.route("/api/deck/lookup-by-name", methods=["POST"])
def deck_lookup_by_name():
    """Look up a card by exact name (case-insensitive). Returns latest printing."""
    body = request.get_json(silent=True) or {}
    name = clean_name(body.get("name", ""))
    if not name:
        return jsonify({"error": "No card name provided."}), 400

    db = get_db()
    card = db.execute("""
        SELECT c.*, ci.scryfallId, s.name as setName
        FROM cards c
        JOIN cardIdentifiers ci ON c.uuid = ci.uuid
        JOIN sets s ON c.setCode = s.code
        WHERE (
            LOWER(c.name) = LOWER(?)
            OR LOWER(
                CASE WHEN instr(c.name, ' // ') > 0
                     THEN substr(c.name, 1, instr(c.name, ' // ') - 1)
                     ELSE c.name END
            ) = LOWER(?)
        )
          AND c.language = 'English'
          AND (c.side IS NULL OR c.side = 'a')
        ORDER BY
            CASE WHEN LOWER(c.name) = LOWER(?) THEN 0 ELSE 1 END,
            s.releaseDate DESC
        LIMIT 1
    """, [name, name, name]).fetchone()

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
    scryfall_id = clean_name(body.get("scryfallId", ""))
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


@decks_bp.route("/api/deck/cards/enrich", methods=["POST"])
def api_deck_cards_enrich():
    """Batch enrich cards with scryfallId from the database.

    Body: {"cards": [{"setCode": "...", "number": "..."}, ...]}
    Returns enriched cards with scryfallId, uuid, name.
    """
    body = request.get_json(silent=True) or {}
    cards = body.get("cards", [])
    if not cards or not isinstance(cards, list):
        return jsonify({"error": "cards array required."}), 400
    if len(cards) > 300:
        return jsonify({"error": "Maximum 300 cards per request."}), 400

    # Build IN clauses from setCode/number pairs
    pairs = [
        (clean_name(c.get("setCode", "")), clean_name(c.get("number", "")))
        for c in cards if isinstance(c, dict)
    ]
    pairs = [(sc, n) for sc, n in pairs if sc and n]

    if not pairs:
        return jsonify({"enriched": []})

    db = get_db()
    placeholders = ", ".join(["(?, ?)"] * len(pairs))
    flat = [v for p in pairs for v in p]

    rows = db.execute(f"""
        SELECT DISTINCT c.uuid, c.name, c.setCode, c.number, ci.scryfallId
        FROM cards c
        JOIN cardIdentifiers ci ON c.uuid = ci.uuid
        WHERE (c.setCode, c.number) IN ({placeholders})
          AND c.language = 'English'
          AND (c.side IS NULL OR c.side = 'a')
    """, flat).fetchall()

    return jsonify({
        "enriched": [{
            "uuid": r["uuid"],
            "name": r["name"],
            "setCode": r["setCode"],
            "number": r["number"],
            "scryfallId": r["scryfallId"],
        } for r in rows],
    })


# --- Deck History ---

@decks_bp.route("/api/deck/history", methods=["GET"])
def api_deck_history():
    """Return history array for a deck."""
    name = clean_name(request.args.get("deck", ""))
    if not name:
        return jsonify({"error": "Deck name is required."}), 400
    try:
        fpath = resolve_json_path(HISTORY_DIR, name, reserved=RESERVED_DECK_FILES)
    except StorageError as e:
        return jsonify({"error": str(e)}), e.status_code
    if not os.path.exists(fpath):
        return jsonify({"success": True, "history": []})
    history = read_json(fpath, default=None)
    if not isinstance(history, list):
        return jsonify({"error": "History file is unreadable."}), 500
    return jsonify({"success": True, "history": history})


@decks_bp.route("/api/deck/history/save", methods=["POST"])
def api_deck_history_save():
    """Append events to a deck's history file."""
    body = request.get_json(silent=True) or {}
    name = clean_name(body.get("name", ""))
    events = body.get("events", [])
    if not name:
        return jsonify({"error": "Deck name is required."}), 400
    if not isinstance(events, list) or len(events) == 0:
        return jsonify({"error": "events array required."}), 400
    if not all(isinstance(e, dict) for e in events):
        return jsonify({"error": "Each event must be an object."}), 400
    try:
        fpath = resolve_json_path(HISTORY_DIR, name, reserved=RESERVED_DECK_FILES)
    except StorageError as e:
        return jsonify({"error": str(e)}), e.status_code
    try:
        with file_lock(fpath):
            existing = read_json(fpath, default=[]) if os.path.exists(fpath) else []
            if not isinstance(existing, list):
                return jsonify({"error": "History file is unreadable."}), 500
            existing.extend(events)
            write_json_atomic(fpath, existing)
    except OSError as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"success": True, "count": len(existing)})


@decks_bp.route("/api/deck/history/delete", methods=["POST"])
def api_deck_history_delete():
    """Delete the history file for a deck."""
    body = request.get_json(silent=True) or {}
    name = clean_name(body.get("name", ""))
    if not name:
        return jsonify({"error": "Deck name is required."}), 400
    try:
        fpath = resolve_json_path(HISTORY_DIR, name, reserved=RESERVED_DECK_FILES)
        with file_lock(fpath):
            os.unlink(fpath)
        return jsonify({"success": True})
    except StorageError as e:
        return jsonify({"error": str(e)}), e.status_code
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
        history = read_json(fpath, default=None)
        if not isinstance(history, list) or not history:
            continue
        events = [e for e in history if isinstance(e, dict) and e.get("timestamp")]
        if not events:
            continue
        last_event = max(events, key=lambda e: e.get("timestamp", ""))
        last_ts = last_event.get("timestamp", "")
        try:
            last_dt = datetime.fromisoformat(str(last_ts).replace("Z", "+00:00"))
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            days = (now - last_dt).days
        except (TypeError, ValueError):
            continue
        if days >= threshold:
            # Use the deck's real display name, not the sanitized filename.
            # Sanitizing strips punctuation ("Alela, Artful" -> "Alela_Artful"),
            # which used to make the client's exact-name comparison fail.
            deck_data = read_json(os.path.join(DECKS_DIR, fname), default=None)
            deck_name = deck_data.get("name") if isinstance(deck_data, dict) else ""
            if not deck_name:
                deck_name = os.path.splitext(fname)[0].replace("_", " ")
            reminders.append({"deck": deck_name, "days": days})

    return jsonify({"reminders": reminders})
