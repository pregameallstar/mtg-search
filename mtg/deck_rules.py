"""Commander deck rules — count, singleton, color identity, legality.

The Deck Builder still validates live for immediate feedback; this module is the
server-side source of truth for analysis and future deck validation endpoints.
No Flask dependency.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Mapping

from mtg.shared import color_identity_subset

BASIC_LAND_NAMES = {"Plains", "Island", "Swamp", "Mountain", "Forest", "Wastes"}


def _front_name(value: Any) -> str:
    name = str(value or "").strip()
    return name.split(" // ", 1)[0].strip().lower()


def _quantity(card: Mapping[str, Any]) -> int:
    try:
        return max(1, int(card.get("quantity") or 1))
    except (TypeError, ValueError):
        return 1


def _supertypes(card: Mapping[str, Any]) -> set[str]:
    return {
        part.strip().lower()
        for part in str(card.get("supertypes") or "").split(",")
        if part.strip()
    }


def is_singleton_exempt(card: Mapping[str, Any]) -> bool:
    """Commander allows one copy except basic lands and explicit any-number cards."""
    if "basic" in _supertypes(card):
        return True
    if str(card.get("name") or "") in BASIC_LAND_NAMES:
        return True
    text = str(card.get("text") or "").lower()
    return "a deck can have any number of cards named" in text


def commander_ci(commander: Mapping[str, Any] | None) -> set[str]:
    if not commander:
        return set()
    return {
        ch for ch in str(commander.get("colorIdentity") or "").upper()
        if ch in "WUBRG"
    }


def is_color_identity_legal(card: Mapping[str, Any],
                            commander: Mapping[str, Any] | None) -> bool:
    if not commander:
        return True
    return color_identity_subset(card.get("colorIdentity") or "", commander_ci(commander))


def _leadership_commander(card: Mapping[str, Any]) -> bool:
    raw = card.get("leadershipSkills")
    if isinstance(raw, dict):
        return bool(raw.get("commander"))
    if isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw)
            return isinstance(data, dict) and bool(data.get("commander"))
        except json.JSONDecodeError:
            return False
    return False


def is_commander_card(card: Mapping[str, Any]) -> bool:
    if _leadership_commander(card):
        return True
    type_line = str(card.get("type") or "").lower()
    return "legendary" in type_line and "creature" in type_line


def _partnerish(card: Mapping[str, Any]) -> bool:
    text = str(card.get("text") or "").lower()
    keywords = str(card.get("keywords") or "").lower()
    return any(token in text or token in keywords for token in (
        "partner", "choose a background", "background", "friends forever",
        "doctor's companion", "doctors companion",
    ))


def _legality(card: Mapping[str, Any]) -> str:
    legalities = card.get("legalities")
    if isinstance(legalities, Mapping):
        return str(legalities.get("commander") or "")
    return str(card.get("commanderLegality") or "")


def validate_deck(
    deck: Mapping[str, Any],
    *,
    enrich: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Validate a Commander deck and return errors/warnings/counts.

    ``enrich`` may fill in text/types/legalities for cards that only carry
    minimal saved-deck fields.
    """
    commander = deck.get("commander")
    commander = commander if isinstance(commander, Mapping) else None
    raw_cards = deck.get("cards")
    cards = [c for c in raw_cards if isinstance(c, Mapping)] if isinstance(raw_cards, list) else []

    if enrich:
        commander = enrich(commander) if commander else None
        cards = [enrich(card) for card in cards]

    sections = deck.get("sections")
    section_counts = {}
    if isinstance(sections, Mapping):
        for name, value in sections.items():
            if isinstance(value, list):
                section_counts[str(name)] = sum(
                    _quantity(card) for card in value if isinstance(card, Mapping)
                )

    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    main_count = sum(_quantity(card) for card in cards)
    total = main_count + (1 if commander else 0)

    if not commander:
        errors.append({"code": "missing_commander", "message": "No commander is set."})
    else:
        if not is_commander_card(commander):
            errors.append({
                "code": "invalid_commander",
                "message": f"{commander.get('name', 'Commander')} is not commander-eligible.",
            })
        legality = _legality(commander)
        if legality and legality.lower() != "legal":
            errors.append({
                "code": "commander_not_legal",
                "message": f"{commander.get('name', 'Commander')} is {legality} in Commander.",
            })
        if _partnerish(commander):
            warnings.append({
                "code": "partner_not_modeled",
                "message": "Partner/background commanders are not fully modeled yet.",
            })

    if total != 100:
        errors.append({
            "code": "wrong_count",
            "message": f"Commander decks must contain exactly 100 cards; found {total}.",
        })

    # Singleton and duplicate oracle identities.
    seen: dict[str, int] = {}
    exempt_names: set[str] = set()
    off_color: list[dict[str, str]] = []
    for card in cards:
        name = _front_name(card.get("name"))
        qty = _quantity(card)
        seen[name] = seen.get(name, 0) + qty
        if is_singleton_exempt(card):
            exempt_names.add(name)
        if not is_singleton_exempt(card) and qty > 1:
            errors.append({
                "code": "singleton",
                "message": f"Only one copy of {card.get('name', name)} is allowed.",
            })
        if not is_color_identity_legal(card, commander):
            off_color.append({
                "name": str(card.get("name") or ""),
                "colorIdentity": str(card.get("colorIdentity") or ""),
            })
        legality = _legality(card)
        if legality and legality.lower() not in ("legal", "not legal"):
            errors.append({
                "code": "card_not_legal",
                "message": f"{card.get('name', name)} is {legality} in Commander.",
            })

    for name, qty in seen.items():
        if qty > 1 and name and name not in exempt_names:
            # A singleton violation may already be reported per card; this
            # catches duplicates split across array entries.
            already = any(
                error["code"] == "singleton" and name in error["message"].lower()
                for error in errors
            )
            if not already:
                errors.append({
                    "code": "duplicate",
                    "message": f"Duplicate card identity: {name}.",
                })

    for item in off_color:
        errors.append({
            "code": "color_identity",
            "message": f"{item['name']} is outside the commander's color identity.",
        })

    return {
        "legal": not errors,
        "errors": errors,
        "warnings": warnings,
        "counts": {
            "total": total,
            "main": main_count,
            "commander": 1 if commander else 0,
            "sections": section_counts,
        },
        "colorIdentity": {
            "commander": ", ".join(sorted(commander_ci(commander))),
            "offColor": off_color,
        },
    }
