"""Structured Deck Brief for Deck Doctor.

The brief is intentionally small and actionable. It captures user intent that
the deterministic audit cannot infer (archetype, win plan, hard limits, cards to
keep or avoid) while keeping a free-form notes field for nuance.

Storage shape (saved on the deck as ``deckBrief``)
--------------------------------------------------
{
  "version": 1,
  "archetype": "aristocrats",
  "themes": ["sacrifice", "squirrels"],
  "win_plans": ["drain", "combat"],
  "constraints": {"Counterspell": 2, "Board Wipe": 2, "Tutor": 0},
  "must_keep": ["Blood Artist"],
  "avoid": ["Armageddon"],
  "notes": "free-form additional context"
}

``reviewNotes`` remains the canonical free-form notes field for backward
compatibility; ``deckBrief.notes`` mirrors it. A deck with only ``reviewNotes``
is migrated to an otherwise-empty brief when read.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

BRIEF_VERSION = 1
BRIEF_CONSTRAINTS_VERSION = 1

WIN_PLANS = {
    "combat": "Combat damage",
    "commander_damage": "Commander damage",
    "combo": "Combo",
    "drain": "Drain / life loss",
    "mill": "Mill",
    "alt_win": "Alternate win condition",
    "other": "Other",
}

_CONSTRAINT_ROLE_ORDER = (
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
    "Win Condition",
    "Extra Turns",
    "Mass Land Denial",
)
CONSTRAINT_ROLES = tuple(_CONSTRAINT_ROLE_ORDER)
_ROLE_BY_LOWER = {role.lower(): role for role in CONSTRAINT_ROLES}
_WIN_PLAN_BY_LABEL = {
    label.lower(): slug for slug, label in WIN_PLANS.items()
}


# --------------------------------------------------------------------------- #
# Brief parsing (free-form notes -> caps)
# --------------------------------------------------------------------------- #

# Conservative mapping from words that appear in a Deck Brief to role names.
# Longer phrases are matched first so "card draw" wins over "draw".
_BRIEF_ROLE_WORDS = {
    "counterspell": "Counterspell",
    "counter": "Counterspell",
    "board wipe": "Board Wipe",
    "boardwipe": "Board Wipe",
    "wipe": "Board Wipe",
    "targeted removal": "Targeted Removal",
    "spot removal": "Targeted Removal",
    "removal": "Targeted Removal",
    "tutor": "Tutor",
    "mana rock": "Ramp",
    "ramp": "Ramp",
    # Plural only: "no land denial" must not be read as "no lands".
    "lands": "Lands",
    "card draw": "Card Draw",
    "draw": "Card Draw",
    "protection": "Protection",
    "recursion": "Recursion",
    "win condition": "Win Condition",
    "wincon": "Win Condition",
    "extra turn": "Extra Turns",
    "mass land denial": "Mass Land Denial",
    "land denial": "Mass Land Denial",
}

_BRIEF_ROLE_RE = re.compile(
    r"\b(?:" + "|".join(
        re.escape(word) for word in sorted(_BRIEF_ROLE_WORDS, key=len, reverse=True)
    ) + r")s?\b",
    re.IGNORECASE,
)
# A bare "2 counterspells" is only treated as a cap inside a sentence that
# looks like a Rule 0 / playgroup constraint, to avoid reading description
# ("the deck has two counterspells") as a limit.
_BRIEF_CAP_CONTEXT_RE = re.compile(
    r"rule\s*0|constrain|constraint|restrict|limit|maximum|\bmax\b|"
    r"at most|no more than|only|cap(?:ped)?|playgroup",
    re.IGNORECASE,
)


def _brief_role(phrase: str) -> str | None:
    lowered = phrase.lower().strip()
    if lowered in _BRIEF_ROLE_WORDS:
        return _BRIEF_ROLE_WORDS[lowered]
    if lowered.endswith("s"):
        return _BRIEF_ROLE_WORDS.get(lowered[:-1])
    return None


def _brief_role_in(text: str) -> str | None:
    match = _BRIEF_ROLE_RE.search(text)
    return _brief_role(match.group(0)) if match else None


def brief_role_caps(brief: str | None) -> dict[str, int]:
    """Extract explicit maximum role counts from free-form notes.

    Returns ``{role: max_count}`` for sentences that look like constraints.
    This is intentionally conservative: it understands patterns such as
    "Rule 0: max 2 board wipes", "no more than 2 counterspells", "no tutors",
    and "2 boardwipes" inside a Rule 0 / playgroup clause.
    """
    text = str(brief or "")
    if not text.strip():
        return {}
    caps: dict[str, int] = {}
    for sentence in re.split(r"(?<=[.!?;])\s+|\n+", text):
        if not _BRIEF_CAP_CONTEXT_RE.search(sentence):
            continue
        for match in re.finditer(
            r"\bno\s+" + _BRIEF_ROLE_RE.pattern, sentence, re.IGNORECASE
        ):
            role = _brief_role_in(match.group(0))
            if role:
                caps[role] = 0
        for match in re.finditer(
            r"(\d{1,2})\s+" + _BRIEF_ROLE_RE.pattern, sentence, re.IGNORECASE
        ):
            role = _brief_role_in(match.group(0))
            if not role:
                continue
            count = max(0, int(match.group(1)))
            caps[role] = min(caps.get(role, count), count)
    return caps


# --------------------------------------------------------------------------- #
# Normalisation and storage helpers
# --------------------------------------------------------------------------- #

def _dedupe_list(parts: Iterable[Any], *, max_items: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        text = str(part or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(text[:120])
        if len(out) >= max_items:
            break
    return out


def _string_list(value: Any, *, max_items: int = 100) -> list[str]:
    if isinstance(value, str):
        parts: Iterable[Any] = re.split(r"[,;\n]+", value)
    elif isinstance(value, (list, tuple, set)):
        parts = value
    else:
        return []
    return _dedupe_list(parts, max_items=max_items)


def _card_list(value: Any, *, max_items: int = 100) -> list[str]:
    """Card names may contain commas; only split on semicolons or newlines."""
    if isinstance(value, str):
        parts: Iterable[Any] = re.split(r"[;\n]+", value)
    elif isinstance(value, (list, tuple, set)):
        parts = value
    else:
        return []
    return _dedupe_list(parts, max_items=max_items)


def normalize_win_plans(value: Any) -> list[str]:
    out: list[str] = []
    for item in _string_list(value, max_items=len(WIN_PLANS)):
        slug = re.sub(r"[\s-]+", "_", item.strip().lower())
        slug = _WIN_PLAN_BY_LABEL.get(item.strip().lower(), slug)
        if slug in WIN_PLANS and slug not in out:
            out.append(slug)
    return out


def _role_name(raw: Any) -> str | None:
    lowered = str(raw or "").strip().lower()
    role = _ROLE_BY_LOWER.get(lowered)
    if not role and lowered.endswith("s"):
        role = _ROLE_BY_LOWER.get(lowered[:-1])
    return role


def normalize_constraints(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    out: dict[str, int] = {}
    for raw_role, raw_count in value.items():
        role = _role_name(raw_role)
        if not role:
            continue
        if raw_count is None or raw_count == "":
            continue
        try:
            count = int(raw_count)
        except (TypeError, ValueError):
            continue
        out[role] = max(0, min(count, 99))
    return {role: out[role] for role in CONSTRAINT_ROLES if role in out}


def normalize_brief(value: Any, *, notes: str | None = None) -> dict[str, Any]:
    """Return a complete, validated v1 brief."""
    data = dict(value) if isinstance(value, Mapping) else {}
    if notes is None:
        notes = data.get("notes") or ""
    try:
        version = int(data.get("version") or BRIEF_VERSION)
    except (TypeError, ValueError):
        version = BRIEF_VERSION
    if version < 1:
        version = BRIEF_VERSION
    return {
        "version": version,
        "archetype": str(data.get("archetype") or "").strip()[:80],
        "themes": _string_list(data.get("themes"), max_items=12),
        "win_plans": normalize_win_plans(data.get("win_plans")),
        "constraints": normalize_constraints(data.get("constraints")),
        "must_keep": _card_list(data.get("must_keep"), max_items=100),
        "avoid": _card_list(data.get("avoid"), max_items=100),
        "notes": str(notes or "").strip()[:4000],
    }


def brief_from_deck(deck: Mapping[str, Any]) -> dict[str, Any]:
    """Load a deck's brief, migrating a legacy ``reviewNotes`` value."""
    raw = deck.get("deckBrief") if isinstance(deck, Mapping) else None
    review_notes = str(deck.get("reviewNotes") or "") if isinstance(deck, Mapping) else ""
    raw_notes = str(raw.get("notes") or "") if isinstance(raw, Mapping) else ""
    notes = review_notes or raw_notes
    if isinstance(raw, Mapping):
        return normalize_brief(raw, notes=notes)
    return normalize_brief({}, notes=notes)


def brief_to_deck(deck: dict, value: Any) -> dict[str, Any]:
    """Write a normalised brief onto a deck, keeping ``reviewNotes`` in sync."""
    brief = normalize_brief(value)
    deck["deckBrief"] = brief
    notes = brief.get("notes") or ""
    if notes:
        deck["reviewNotes"] = notes
    else:
        deck.pop("reviewNotes", None)
    return brief


def brief_is_empty(brief: Mapping[str, Any] | None) -> bool:
    if not isinstance(brief, Mapping):
        return True
    return not any(
        brief.get(key)
        for key in (
            "archetype", "themes", "win_plans", "constraints",
            "must_keep", "avoid", "notes",
        )
    )


def brief_fingerprint(value: Any) -> dict[str, Any]:
    """Canonical brief shape for deck fingerprints."""
    brief = normalize_brief(value)
    return {
        "version": brief["version"],
        "archetype": brief["archetype"],
        "themes": sorted(tag.lower() for tag in brief["themes"]),
        "win_plans": sorted(brief["win_plans"]),
        "constraints": dict(brief["constraints"]),
        "must_keep": sorted(name.lower() for name in brief["must_keep"]),
        "avoid": sorted(name.lower() for name in brief["avoid"]),
        "notes": brief["notes"],
    }


def merge_caps(*sources: Mapping[str, int] | None) -> dict[str, int]:
    """Merge cap maps, taking the tightest explicit maximum for each role."""
    out: dict[str, int] = {}
    for source in sources:
        if not isinstance(source, Mapping):
            continue
        for raw_role, raw_count in source.items():
            role = _role_name(raw_role)
            if not role:
                continue
            try:
                count = max(0, int(raw_count))
            except (TypeError, ValueError):
                continue
            out[role] = min(out.get(role, count), count)
    return {role: out[role] for role in CONSTRAINT_ROLES if role in out}


def brief_caps(brief: Mapping[str, Any] | None) -> dict[str, int]:
    if not isinstance(brief, Mapping):
        return {}
    return normalize_constraints(brief.get("constraints"))


def declared_win_plan_credit(brief: Mapping[str, Any] | None) -> bool:
    """True when the user named a plan the heuristic cannot verify directly."""
    if not isinstance(brief, Mapping):
        return False
    plans = normalize_win_plans(brief.get("win_plans"))
    return bool(plans) and "alt_win" not in plans


def suppressed_roles(brief: Mapping[str, Any] | None) -> set[str]:
    return {"Win Condition"} if declared_win_plan_credit(brief) else set()


def brief_retrieval_text(brief: Mapping[str, Any] | None) -> str:
    """Compact intent text for embedding queries."""
    if not isinstance(brief, Mapping):
        return ""
    parts: list[str] = []
    if brief.get("archetype"):
        parts.append(str(brief["archetype"]))
    if brief.get("themes"):
        parts.append("themes: " + ", ".join(str(tag) for tag in brief["themes"]))
    plans = normalize_win_plans(brief.get("win_plans"))
    if plans:
        parts.append("win plan: " + ", ".join(WIN_PLANS[slug] for slug in plans))
    for role, count in brief_caps(brief).items():
        parts.append(f"max {role} {count}")
    return " ".join(parts)


def brief_prompt_payload(
    brief: Mapping[str, Any] | None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Structured brief for the LLM/verifier payloads."""
    data = normalize_brief(brief, notes=notes)
    return {
        "archetype": data["archetype"],
        "themes": data["themes"],
        "win_plans": [
            {"id": slug, "label": WIN_PLANS[slug]}
            for slug in data["win_plans"]
        ],
        "constraints": data["constraints"],
        "must_keep": data["must_keep"],
        "avoid": data["avoid"],
        "notes": data["notes"],
    }
