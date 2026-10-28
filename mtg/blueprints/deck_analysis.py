"""Deck analysis API — role batches and deterministic deck audits.

No write operations. The Deck Builder calls this for its role/guideline panel;
the same functions are reused by Deck Doctor.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request
from mtg.deck_enrich import enrich_card, enrich_deck
from mtg.deck_stats import (
    AUDIT_SCHEMA_VERSION,
    analyze_deck,
    deck_fingerprint,
)
from mtg.roles import ROLES_VERSION, classify_roles
from mtg.shared import DECKS_DIR, TEMPLATES_DIR, _list_json_files, get_db
from mtg.storage import (
    StorageError,
    clean_name,
    read_named_json,
    resolve_json_path,
)

deck_analysis_bp = Blueprint("deck_analysis", __name__)


def _guideline(name: str) -> dict | None:
    if not name:
        return None
    for template in _list_json_files(TEMPLATES_DIR):
        if template.get("type", "cards") == "guideline" and template.get("name") == name:
            return template
    return None


@deck_analysis_bp.route("/api/cards/roles", methods=["POST"])
def api_card_roles():
    """Classify up to 200 cards, resolving missing data from the DB."""
    body = request.get_json(silent=True) or {}
    cards = body.get("cards") if isinstance(body, dict) else None
    if not isinstance(cards, list):
        return jsonify({"error": "cards array required."}), 400
    if len(cards) > 200:
        return jsonify({"error": "Maximum 200 cards per request."}), 400

    db = get_db()
    results = []
    for item in cards:
        if not isinstance(item, dict):
            results.append({"error": "Card must be an object."})
            continue
        enriched = enrich_card(db, item)
        if not (enriched.get("text") or enriched.get("type") or enriched.get("types")):
            results.append({
                "uuid": item.get("uuid", ""),
                "name": item.get("name", ""),
                "error": "Card not found.",
            })
            continue
        info = classify_roles(enriched)
        results.append({
            "uuid": enriched.get("uuid", ""),
            "name": enriched.get("name", item.get("name", "")),
            "roles": info["roles"],
            "primary": info["primary"],
            "confidence": info["confidence"],
            "source": info["source"],
            "evidence": info["evidence"],
        })

    return jsonify({"rolesVersion": ROLES_VERSION, "results": results})


@deck_analysis_bp.route("/api/deck/analyze", methods=["POST"])
def api_deck_analyze():
    """Analyze a saved deck or a posted deck object without writing anything."""
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict):
        return jsonify({"error": "Request body must be a JSON object."}), 400

    deck = body.get("deck")
    name = clean_name(body.get("name", ""))
    if deck is None and not name:
        return jsonify({"error": "Provide a deck object or a deck name."}), 400
    if deck is not None and not isinstance(deck, dict):
        return jsonify({"error": "deck must be a JSON object."}), 400

    if deck is None:
        try:
            path = resolve_json_path(DECKS_DIR, name)
            deck = read_named_json(path, name)
        except StorageError as exc:
            return jsonify({"error": str(exc)}), exc.status_code
        if deck is None:
            return jsonify({"error": "Deck not found."}), 404

    options = body.get("options") if isinstance(body.get("options"), dict) else {}
    db = get_db()
    enriched_deck = enrich_deck(db, deck)

    guideline_name = options.get("guideline") or deck.get("guideline") or ""
    guideline = _guideline(str(guideline_name))
    target_bracket = options.get("target_bracket")
    try:
        target_bracket = int(target_bracket) if target_bracket is not None else None
    except (TypeError, ValueError):
        target_bracket = None

    audit = analyze_deck(
        enriched_deck,
        guideline=guideline,
        target_bracket=target_bracket,
    )
    fingerprint = deck_fingerprint(enriched_deck, options)
    return jsonify({
        "success": True,
        "schemaVersion": AUDIT_SCHEMA_VERSION,
        "rolesVersion": ROLES_VERSION,
        "deck": {
            "name": deck.get("name", name),
            "fingerprint": fingerprint,
            "commander": {
                "name": (enriched_deck.get("commander") or {}).get("name", ""),
                "colorIdentity": (enriched_deck.get("commander") or {}).get("colorIdentity", ""),
            },
        },
        "audit": audit,
    })

