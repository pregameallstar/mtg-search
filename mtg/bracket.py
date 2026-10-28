"""Advisory Commander bracket and salt estimation.

Bracket output is a transparent signal report, not an official ruling. The
thresholds live in :mod:`mtg.bracket_rules` so they can be revised without
changing the analysis pipeline.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from mtg.bracket_rules import BRACKET_RULES, FAST_MANA_NAMES
from mtg.roles import classify_roles


def _number(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _signal(key: str, label: str, count: int, cards: list[str],
            severity: str = "info") -> dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "count": count,
        "cards": cards,
        "severity": severity,
    }


def _severity(count: int, thresholds: tuple[int, int] = (1, 3)) -> str:
    if count >= thresholds[1]:
        return "high"
    if count >= thresholds[0]:
        return "warning"
    return "info"


def estimate_bracket(
    cards: Iterable[Mapping[str, Any]],
    commander: Mapping[str, Any] | None = None,
    *,
    target_bracket: int | None = None,
    combo_count: int | None = None,
    rules: Mapping[int, Mapping[str, int]] | None = None,
) -> dict[str, Any]:
    """Return an advisory bracket estimate plus the signals behind it."""
    rules = rules or BRACKET_RULES
    cards = [card for card in cards if isinstance(card, Mapping)]
    if commander:
        cards = [commander] + cards

    role_cards: dict[str, list[str]] = {}
    game_changers: list[str] = []
    fast_mana: list[str] = []
    salt_cards: list[tuple[str, float]] = []
    mana_values: list[float] = []
    interaction_count = 0

    for card in cards:
        is_commander = commander is not None and card is commander
        name = str(card.get("name") or "")
        roles = classify_roles(card)["roles"]
        for role in roles:
            role_cards.setdefault(role, []).append(name)

        if card.get("isGameChanger") in (1, True, "1", "true", "True"):
            game_changers.append(name)
        if name in FAST_MANA_NAMES and name not in fast_mana:
            fast_mana.append(name)
        if "Fast Mana" in roles and name not in fast_mana:
            fast_mana.append(name)

        salt = _number(card.get("edhrecSaltiness"))
        if salt is not None:
            salt_cards.append((name, salt))

        if (not is_commander
                and "land" not in str(card.get("types") or "").lower()
                and "land" not in str(card.get("type") or "").lower()):
            mv = _number(card.get("manaValue"))
            if mv is not None:
                mana_values.append(mv)

        interaction_count += sum(
            1 for role in ("Targeted Removal", "Board Wipe", "Counterspell")
            if role in roles
        )

    mld = role_cards.get("Mass Land Denial", [])
    extra_turns = role_cards.get("Extra Turns", [])
    tutors = role_cards.get("Tutor", [])
    wipes = role_cards.get("Board Wipe", [])
    draw = role_cards.get("Card Draw", [])
    wincons = role_cards.get("Win Condition", [])
    avg_mv = round(sum(mana_values) / len(mana_values), 2) if mana_values else None
    salt_avg = round(sum(score for _, score in salt_cards) / len(salt_cards), 2) if salt_cards else None
    top_salt = [
        {"name": name, "score": round(score, 2)}
        for name, score in sorted(salt_cards, key=lambda item: item[1], reverse=True)[:5]
    ]

    signals = [
        _signal("game_changers", "Game Changers", len(game_changers), game_changers,
                _severity(len(game_changers), (1, 4))),
        _signal("mass_land_denial", "Mass land denial", len(mld), mld,
                _severity(len(mld), (1, 2))),
        _signal("extra_turns", "Extra turns", len(extra_turns), extra_turns,
                _severity(len(extra_turns), (1, 3))),
        _signal("tutors", "Tutors", len(tutors), tutors,
                _severity(len(tutors), (3, 6))),
        _signal("fast_mana", "Fast mana", len(fast_mana), fast_mana,
                _severity(len(fast_mana), (2, 5))),
        _signal("board_wipes", "Board wipes", len(wipes), wipes,
                _severity(len(wipes), (3, 6))),
    ]
    if salt_avg is not None:
        signals.append({
            "key": "salt",
            "label": "Salt",
            "count": len(salt_cards),
            "cards": [entry["name"] for entry in top_salt],
            "average": salt_avg,
            "top": top_salt,
            "severity": "warning" if salt_avg >= 2.0 else "info",
        })

    # Provisional score. Start at 2 (average precon) and raise for signals.
    score = 2
    if len(game_changers) > 3:
        score = max(score, 4)
    elif game_changers:
        score = max(score, 3)
    if mld:
        score = max(score, 4)
    if len(extra_turns) >= 2:
        score = max(score, 4)
    if len(tutors) >= 5:
        score = max(score, 4)
    if len(fast_mana) >= 3:
        score = max(score, 4)
    if len(fast_mana) >= 6:
        score = max(score, 5)
    if combo_count is not None and combo_count >= 1:
        score = max(score, 4)
    if combo_count is not None and combo_count >= 3:
        score = max(score, 5)
    if avg_mv is not None and avg_mv >= 4.0 and not game_changers and not mld:
        score = min(score, 2)
    if (not game_changers and not mld and not extra_turns and not fast_mana
            and len(tutors) <= 2 and not combo_count):
        score = min(score, 3)
    score = max(1, min(5, score))
    low = max(1, score - 1)
    high = min(5, score + 1)

    notes = []
    if combo_count is None:
        notes.append("Combo data unavailable; bracket confidence is reduced.")
    if not salt_cards:
        notes.append("Salt scores unavailable for some cards.")

    result = {
        "estimate": score,
        "range": [low, high],
        "confidence": "high" if combo_count is not None else "medium",
        "signals": signals,
        "salt": {
            "average": salt_avg,
            "score": round(salt_avg * 25) if salt_avg is not None else None,
            "top": top_salt,
        },
        "interaction": {
            "count": interaction_count,
            "board_wipes": len(wipes),
            "card_draw": len(draw),
            "win_conditions": len(wincons),
        },
        "averageManaValue": avg_mv,
        "notes": notes,
    }

    if target_bracket is not None:
        target = max(1, min(5, int(target_bracket)))
        limits = rules.get(target, {})
        over = []
        checks = (
            ("game_changers", len(game_changers), "max_game_changers"),
            ("mass_land_denial", len(mld), "max_mass_land_denial"),
            ("extra_turns", len(extra_turns), "max_extra_turns"),
            ("tutors", len(tutors), "max_tutors"),
            ("fast_mana", len(fast_mana), "max_fast_mana"),
        )
        for key, count, limit_key in checks:
            limit = limits.get(limit_key)
            if limit is not None and count > limit:
                over.append({"key": key, "count": count, "limit": limit})
        if over or score > target:
            status = "over"
        elif score == target:
            status = "within"
        else:
            status = "below"
        result["target_fit"] = {
            "target": target,
            "status": status,
            "over": over,
        }

    return result
