"""Deterministic deck analysis: counts, curve, roles, health, bracket.

Pure functions over enriched deck-card mappings. No Flask, no LLM, no embeddings.
This is the ground truth layer used by the Deck Builder panel and Deck Doctor.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from mtg.bracket import estimate_bracket
from mtg.deck_rules import validate_deck
from mtg.roles import ROLES_VERSION, classify_roles

AUDIT_SCHEMA_VERSION = 1

DEFAULT_ROLE_TARGETS = {
    "Lands": 37,
    "Ramp": 10,
    "Card Draw": 10,
    "Card Selection": 4,
    "Targeted Removal": 10,
    "Board Wipe": 2,
    "Counterspell": 3,
    "Tutor": 3,
    "Protection": 4,
    "Recursion": 2,
    "Win Condition": 2,
}

HEALTH_WEIGHTS = {
    "roles": 35,
    "mana": 20,
    "interaction": 15,
    "draw": 10,
    "wincons": 10,
    "legality": 10,
}


def _number(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _qty(card: Mapping[str, Any]) -> int:
    try:
        return max(1, int(card.get("quantity") or 1))
    except (TypeError, ValueError):
        return 1


def _is_land(card: Mapping[str, Any]) -> bool:
    text = f"{card.get('types') or ''} {card.get('type') or ''}".lower()
    return "land" in text


def _role_counts(cards: list[Mapping[str, Any]]) -> tuple[dict[str, int], dict[str, list[dict[str, Any]]]]:
    counts: dict[str, int] = {}
    members: dict[str, list[dict[str, Any]]] = {}
    for card in cards:
        info = classify_roles(card)
        for role in info["roles"]:
            counts[role] = counts.get(role, 0) + _qty(card)
            members.setdefault(role, []).append({
                "name": str(card.get("name") or ""),
                "quantity": _qty(card),
            })
    return counts, members


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _health(
    role_counts: Mapping[str, int],
    *,
    land_count: int,
    ramp_count: int,
    avg_mv: float | None,
    legal: bool,
    error_count: int,
) -> dict[str, Any]:
    coverage_values = []
    for role, target in DEFAULT_ROLE_TARGETS.items():
        if target <= 0:
            continue
        coverage_values.append(_clamp(role_counts.get(role, 0) / target))
    roles_score = round(sum(coverage_values) / len(coverage_values) * HEALTH_WEIGHTS["roles"]) if coverage_values else 0

    land_score = _clamp(land_count / 37)
    ramp_score = _clamp(ramp_count / 10)
    curve_score = 1.0
    if avg_mv is not None:
        # A curve between 2.8 and 3.6 is comfortable for most Commander decks.
        curve_score = _clamp(1.0 - abs(avg_mv - 3.2) / 2.0)
    mana_score = round((land_score * 0.55 + ramp_score * 0.30 + curve_score * 0.15) * HEALTH_WEIGHTS["mana"])

    interaction = (
        role_counts.get("Targeted Removal", 0)
        + role_counts.get("Board Wipe", 0)
        + role_counts.get("Counterspell", 0)
    )
    interaction_score = round(_clamp(interaction / 14) * HEALTH_WEIGHTS["interaction"])
    draw = role_counts.get("Card Draw", 0) + role_counts.get("Card Selection", 0)
    draw_score = round(_clamp(draw / 12) * HEALTH_WEIGHTS["draw"])
    wincons = role_counts.get("Win Condition", 0)
    wincon_score = round(_clamp(wincons / 2) * HEALTH_WEIGHTS["wincons"])
    legality_score = HEALTH_WEIGHTS["legality"] if legal else max(0, HEALTH_WEIGHTS["legality"] - error_count * 2)

    components = {
        "roles": roles_score,
        "mana": mana_score,
        "interaction": interaction_score,
        "draw": draw_score,
        "wincons": wincon_score,
        "legality": legality_score,
    }
    return {"score": sum(components.values()), "components": components}


def deck_fingerprint(deck: Mapping[str, Any], options: Mapping[str, Any] | None = None) -> str:
    """Stable fingerprint of the parts that affect analysis."""
    commander = deck.get("commander") if isinstance(deck.get("commander"), Mapping) else {}
    cards = deck.get("cards") if isinstance(deck.get("cards"), list) else []

    def key(card: Mapping[str, Any]) -> tuple[str, int]:
        ident = card.get("uuid") or f"{card.get('setCode', '')}/{card.get('number', '')}"
        return str(ident), _qty(card)

    canonical = {
        "commander": commander.get("uuid") or commander.get("name") or "",
        "cards": sorted(key(card) for card in cards if isinstance(card, Mapping)),
        "guideline": deck.get("guideline") or "",
        "outline": [
            {"name": section.get("name", ""), "text": section.get("text", "")}
            for section in (deck.get("outline") or [])
            if isinstance(section, Mapping)
        ],
        "options": dict(options or {}),
    }
    return hashlib.sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def analyze_deck(
    deck: Mapping[str, Any],
    *,
    guideline: Mapping[str, Any] | None = None,
    target_bracket: int | None = None,
    combo_count: int | None = None,
) -> dict[str, Any]:
    """Return the deterministic audit for a Commander deck."""
    commander = deck.get("commander") if isinstance(deck.get("commander"), Mapping) else None
    raw_cards = deck.get("cards") if isinstance(deck.get("cards"), list) else []
    cards = [card for card in raw_cards if isinstance(card, Mapping)]

    legality = validate_deck(deck)
    role_counts, role_members = _role_counts(cards)

    primary_counts = {role: 0 for role in ("Lands", "Ramp", "Draw", "Removal", "Core")}
    for card in cards:
        primary = classify_roles(card)["primary"]
        if primary in primary_counts:
            primary_counts[primary] += _qty(card)

    # Curve excludes lands and the commander.
    buckets = {str(i): 0 for i in range(8)}
    mana_values: list[float] = []
    land_count = 0
    for card in cards:
        qty = _qty(card)
        if _is_land(card):
            land_count += qty
            continue
        mv = _number(card.get("manaValue"))
        if mv is None:
            continue
        mana_values.extend([mv] * qty)
        bucket = str(min(7, int(mv)))
        buckets[bucket] = buckets.get(bucket, 0) + qty
    average_mv = round(sum(mana_values) / len(mana_values), 2) if mana_values else None
    nonland_count = sum(_qty(card) for card in cards if not _is_land(card))

    target_map = dict(DEFAULT_ROLE_TARGETS)
    guideline_role_map = {
        "Draw": "Card Draw",
        "Removal": "Targeted Removal",
        # Core has no rich-role equivalent; it is shown in guideline progress.
    }
    if guideline and isinstance(guideline.get("categories"), Mapping):
        for category, target in guideline["categories"].items():
            role = guideline_role_map.get(str(category), str(category))
            if role == "Core":
                continue
            try:
                target_map[role] = int(target)
            except (TypeError, ValueError):
                continue

    role_gaps = []
    for role, target in target_map.items():
        actual = primary_counts.get(role) if role in primary_counts else role_counts.get(role, 0)
        if target <= 0 and actual <= 0:
            continue
        role_gaps.append({
            "role": role,
            "target": target,
            "actual": actual,
            "deficit": max(0, target - actual),
            "surplus": max(0, actual - target),
        })

    bracket = estimate_bracket(
        cards,
        commander,
        target_bracket=target_bracket,
        combo_count=combo_count,
    )
    health = _health(
        role_counts,
        land_count=land_count,
        ramp_count=role_counts.get("Ramp", 0),
        avg_mv=average_mv,
        legal=legality["legal"],
        error_count=len(legality["errors"]),
    )

    guideline_progress = None
    if guideline and isinstance(guideline.get("categories"), Mapping):
        guideline_progress = {
            "name": guideline.get("name", ""),
            "categories": {
                str(category): {
                    "actual": (
                        primary_counts.get(str(category))
                        if str(category) in primary_counts
                        else role_counts.get(str(category), 0)
                    ),
                    "target": target,
                }
                for category, target in guideline["categories"].items()
            },
        }

    card_roles = []
    for card in cards:
        info = classify_roles(card)
        card_roles.append({
            "uuid": card.get("uuid") or "",
            "name": str(card.get("name") or ""),
            "quantity": _qty(card),
            **info,
        })

    warnings = list(legality.get("warnings", []))
    if average_mv is None:
        warnings.append({"code": "no_mana_values", "message": "No nonland mana values found."})

    return {
        "schemaVersion": AUDIT_SCHEMA_VERSION,
        "rolesVersion": ROLES_VERSION,
        "counts": legality["counts"],
        "legality": {
            "legal": legality["legal"],
            "errors": legality["errors"],
            "warnings": legality["warnings"],
        },
        "curve": {
            "buckets": buckets,
            "averageMv": average_mv,
            "landCount": land_count,
            "rampCount": role_counts.get("Ramp", 0),
            "nonlandCount": nonland_count,
        },
        "roles": {
            role: {"count": count, "cards": role_members.get(role, [])}
            for role, count in sorted(role_counts.items())
        },
        "primary": {
            role: {"count": primary_counts[role]}
            for role in primary_counts
        },
        "roleGaps": role_gaps,
        "colorIdentity": legality["colorIdentity"],
        "bracket": bracket,
        "guideline": guideline_progress,
        "health": health,
        "cardRoles": card_roles,
        "warnings": warnings,
    }
