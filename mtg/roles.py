"""Shared, evidence-based role classifier for cards and deck analysis.

The Deck Builder used to classify cards with a five-category first-match regex.
This module is the canonical replacement: one card can have several roles, the
five legacy guideline buckets remain available as ``primary``, and every
classification includes evidence.

No Flask or database dependency. Callers pass a DB row or a deck-card mapping.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from mtg.bracket_rules import FAST_MANA_NAMES

ROLES_VERSION = 1

ROLE_ORDER = (
    "Lands",
    "Ramp",
    "Card Draw",
    "Card Selection",
    "Targeted Removal",
    "Board Wipe",
    "Counterspell",
    "Tutor",
    "Protection",
    "Recursion",
    "Tokens",
    "Counters",
    "Sac Outlet",
    "Lifegain",
    "Stax",
    "Extra Turns",
    "Mass Land Denial",
    "Fast Mana",
    "Win Condition",
)

GUIDELINE_CATEGORIES = ("Lands", "Ramp", "Draw", "Removal", "Core")

PRIMARY_MAP = {
    "Lands": "Lands",
    "Ramp": "Ramp",
    "Fast Mana": "Ramp",
    "Card Draw": "Draw",
    "Card Selection": "Draw",
    "Targeted Removal": "Removal",
    "Board Wipe": "Removal",
    "Counterspell": "Removal",
    # Everything else falls through to Core unless a tag overrides.
}

TAG_ALIASES = {
    "draw": "Card Draw",
    "card draw": "Card Draw",
    "card advantage": "Card Draw",
    "ramp": "Ramp",
    "mana ramp": "Ramp",
    "removal": "Targeted Removal",
    "spot removal": "Targeted Removal",
    "wipe": "Board Wipe",
    "boardwipe": "Board Wipe",
    "board wipe": "Board Wipe",
    "tutor": "Tutor",
    "protection": "Protection",
    "recursion": "Recursion",
    "reanimate": "Recursion",
    "win condition": "Win Condition",
    "wincon": "Win Condition",
    "tokens": "Tokens",
    "counters": "Counters",
    "sac outlet": "Sac Outlet",
    "lifegain": "Lifegain",
    "life gain": "Lifegain",
    "stax": "Stax",
    "extra turns": "Extra Turns",
    "mass land denial": "Mass Land Denial",
    "fast mana": "Fast Mana",
}

# Tags that are also legacy guideline categories. These force the primary bucket.
_PRIMARY_TAGS = {c.lower(): c for c in GUIDELINE_CATEGORIES}

_REMINDER_RE = re.compile(r"\([^)]*\)")


def _split_csv(value: Any) -> set[str]:
    return {part.strip().lower() for part in str(value or "").split(",") if part.strip()}


def _text(card: Mapping[str, Any]) -> tuple[str, str]:
    raw = str(card.get("text") or card.get("oracle_text") or "").lower()
    return raw, _REMINDER_RE.sub(" ", raw)


def _types(card: Mapping[str, Any]) -> tuple[set[str], str]:
    type_line = str(card.get("type") or "").lower()
    types = _split_csv(card.get("types"))
    if not types and type_line:
        # Fallback for deck cards that only have a full type line.
        types = {word for word in re.split(r"[^a-z]+", type_line) if word}
    return types, type_line


def _keywords(card: Mapping[str, Any]) -> set[str]:
    return _split_csv(card.get("keywords"))


def _has(pattern: str, text: str) -> bool:
    return bool(re.search(pattern, text))


def _add(evidence: list[dict[str, str]], role: str, reason: str,
         confidence: str = "high") -> None:
    if any(item["role"] == role for item in evidence):
        return
    evidence.append({"role": role, "reason": reason, "confidence": confidence})


def _tag_roles(card: Mapping[str, Any]) -> tuple[list[str], str | None]:
    """Return roles added by tags and an optional forced primary bucket."""
    roles: list[str] = []
    forced_primary = None
    for raw in card.get("tags") or []:
        name = str(raw).strip()
        if not name:
            continue
        lowered = name.lower()
        if lowered in _PRIMARY_TAGS:
            forced_primary = _PRIMARY_TAGS[lowered]
        role = TAG_ALIASES.get(lowered)
        if role and role not in roles:
            roles.append(role)
    return roles, forced_primary


def _classify(card: Mapping[str, Any]) -> list[dict[str, str]]:
    _, clean = _text(card)
    types, type_line = _types(card)
    keywords = _keywords(card)
    is_land = "land" in types or _has(r"\bland\b", type_line)
    evidence: list[dict[str, str]] = []

    # Lands are a type, not a text pattern.
    if is_land:
        _add(evidence, "Lands", "card type is Land")

    # Ramp / mana acceleration. Lands are already counted as Lands.
    if not is_land:
        if _has(r"\badds?\b[^.]*\{", clean) or _has(r"\badds? (one|two|three|x|\d+) mana", clean):
            _add(evidence, "Ramp", "produces additional mana")
        elif _has(r"search your library for (?:up to )?(?:a |two |three |four |x |\d+ )*(?:basic )?land", clean) and _has(r"battlefield", clean):
            _add(evidence, "Ramp", "puts additional lands onto the battlefield")
        elif _has(r"put .*land.*onto the battlefield", clean):
            _add(evidence, "Ramp", "puts a land onto the battlefield")
        elif _has(r"create .*treasure", clean):
            _add(evidence, "Ramp", "creates Treasure tokens")
        elif _has(r"untap target land", clean):
            _add(evidence, "Ramp", "untaps a land for extra mana", "medium")

    # Card draw. Avoid draw-hate and draw-step wording.
    draw_hate = _has(
        r"can'?t draw|draw step|draw more than|opponents? draws?|an opponent draws",
        clean,
    )
    if not draw_hate and _has(
        r"\bdraws?\b.*\bcard|\bdraws? (?:a|an|one|two|three|four|five|six|seven|x|\d+) cards?|"
        r"each player draws|you may draw",
        clean,
    ):
        _add(evidence, "Card Draw", "draws cards")

    if _has(r"\bscry\b|\bsurveil\b|look at the top|"
            r"exile the top .* you may (?:play|cast)", clean):
        _add(evidence, "Card Selection", "improves card selection", "medium")

    # Targeted removal.
    if _has(
        r"destroy target|exile target|deals? .* damage to (?:any target|target)|"
        r"target creature gets -|target (?:player|opponent) sacrifices|"
        r"return target .* to (?:its|their) owner'?s hand",
        clean,
    ):
        _add(evidence, "Targeted Removal", "answers a targeted permanent or spell")

    # Board wipes (lands are Mass Land Denial instead).
    if (
        _has(r"destroy all (?:creatures|other creatures|artifacts|enchantments|permanents)", clean)
        or _has(r"exile all (?:creatures|artifacts|enchantments|permanents)", clean)
        or _has(r"all creatures get -", clean)
        or _has(r"deals? .* damage to each creature", clean)
        or _has(r"each player sacrifices|sacrifice all", clean)
    ):
        _add(evidence, "Board Wipe", "answers multiple permanents")

    if _has(r"counter target (?:spell|ability|activated or triggered ability)", clean):
        _add(evidence, "Counterspell", "counters a spell or ability")

    # Tutors: a non-land search. Land-only search is Ramp above.
    if _has(r"search your library for", clean) and not _has(
        r"search your library for (?:up to )?(?:a |two |three |four |x |\d+ )*(?:basic )?land(?: card)?", clean
    ):
        _add(evidence, "Tutor", "searches the library for a non-land card")

    # Protection.
    if (
        keywords & {"hexproof", "indestructible", "ward", "protection"}
        or _has(r"\bhexproof\b|\bindestructible\b|\bward\b|protection from|phase out|"
                r"can'?t be targeted|prevent all damage|regenerate target", clean)
    ):
        _add(evidence, "Protection", "protects a permanent or player")

    # Recursion.
    if (
        _has(r"return .* from your graveyard to (?:your hand|the battlefield)|"
             r"put .* from a graveyard onto the battlefield|"
             r"cast .* from your graveyard", clean)
        or keywords & {"escape", "flashback", "disturb", "unearth", "aftermath"}
    ):
        _add(evidence, "Recursion", "returns or recasts cards from the graveyard")

    if _has(r"create .*tokens?|token copy", clean):
        _add(evidence, "Tokens", "creates tokens")

    if _has(r"\+1/\+1 counter|-1/-1 counter|proliferate|counter on", clean):
        _add(evidence, "Counters", "uses +1/+1 or -1/-1 counters")

    if _has(r"sacrifice (?:a|an|another|one or more) (?:creature|artifact|permanent|land|token)[^.]*:|"
            r"as an additional cost to cast .*sacrifice", clean):
        _add(evidence, "Sac Outlet", "has a repeatable sacrifice cost")

    if _has(r"gain(?:s)? .* life", clean) or "lifelink" in keywords:
        _add(evidence, "Lifegain", "gains life")

    if _has(
        r"opponents? can'?t|your opponents can'?t|spells your opponents cast cost|"
        r"lands don'?t untap|doesn'?t untap during|can'?t cast spells|can'?t attack",
        clean,
    ):
        _add(evidence, "Stax", "restricts opponents' resources", "medium")

    if _has(r"extra turn|additional turn", clean):
        _add(evidence, "Extra Turns", "takes an extra turn")

    if _has(r"destroy all lands|each player sacrifices a land|lands don'?t untap", clean):
        _add(evidence, "Mass Land Denial", "denies opponents' lands", "medium")

    name = str(card.get("name") or "")
    if name in FAST_MANA_NAMES:
        _add(evidence, "Fast Mana", "curated fast-mana list", "medium")

    if _has(r"you win the game|target player loses the game|each opponent loses the game", clean):
        _add(evidence, "Win Condition", "explicitly wins the game")

    return evidence


def classify_roles(card: Mapping[str, Any], *, tags: list[str] | None = None) -> dict[str, Any]:
    """Classify one card.

    Returns ``{roles, primary, confidence, source, evidence}``. The result is
    deterministic and ordered by ``ROLE_ORDER``.
    """
    if not isinstance(card, Mapping):
        card = dict(card) if hasattr(card, "keys") else {}
    if tags is not None:
        card = dict(card)
        card["tags"] = tags

    tag_roles, forced_primary = _tag_roles(card)
    rule_evidence = _classify(card)
    evidence: list[dict[str, str]] = []

    for role in tag_roles:
        _add(evidence, role, "assigned by tag", "high")
    for item in rule_evidence:
        _add(evidence, item["role"], item["reason"], item["confidence"])

    roles = [role for role in ROLE_ORDER if any(item["role"] == role for item in evidence)]
    if not roles:
        return {
            "roles": [],
            "primary": "Core",
            "confidence": "low",
            "source": "none",
            "evidence": [],
        }

    primary = forced_primary
    if primary is None:
        for role in roles:
            if role in PRIMARY_MAP:
                primary = PRIMARY_MAP[role]
                break
    if primary is None:
        primary = "Core"

    if tag_roles and rule_evidence:
        source = "mixed"
    elif tag_roles:
        source = "tag"
    else:
        source = "rule"

    confidences = [item["confidence"] for item in evidence]
    confidence = "high" if "high" in confidences else ("medium" if "medium" in confidences else "low")

    return {
        "roles": roles,
        "primary": primary,
        "confidence": confidence,
        "source": source,
        "evidence": [item for item in evidence if item["role"] in roles],
    }


def classify_many(cards: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [classify_roles(card) for card in cards]
