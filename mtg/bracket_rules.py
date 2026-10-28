"""Provisional Commander bracket signals and rule table.

The official Commander bracket guidance is community-maintained and can change.
Keep the thresholds and curated lists here so the estimator can be updated
without touching the analysis pipeline. The output is advisory, never official.

Fast-mana detection is curated rather than inferred from oracle text: a card
that adds mana is not automatically mana-positive, and a wrong inference would
skew the bracket estimate.
"""

from __future__ import annotations

# Cards that are conventionally treated as fast mana. This is intentionally
# conservative; extend it deliberately rather than trying to parse "net mana".
FAST_MANA_NAMES = frozenset({
    "Ancient Tomb",
    "Chrome Mox",
    "Grim Monolith",
    "Gaea's Cradle",
    "Jeweled Lotus",
    "Lion's Eye Diamond",
    "Mana Crypt",
    "Mana Vault",
    "Mishra's Workshop",
    "Mox Diamond",
    "Mox Emerald",
    "Mox Jet",
    "Mox Opal",
    "Mox Pearl",
    "Mox Ruby",
    "Mox Sapphire",
    "Sol Ring",
    "Serra's Sanctum",
})

# Provisional defaults. Adjust after checking the current official guidance.
BRACKET_RULES = {
    1: {
        "max_game_changers": 0,
        "max_mass_land_denial": 0,
        "max_extra_turns": 0,
        "max_tutors": 0,
        "max_fast_mana": 0,
    },
    2: {
        "max_game_changers": 0,
        "max_mass_land_denial": 0,
        "max_extra_turns": 1,
        "max_tutors": 3,
        "max_fast_mana": 1,
    },
    3: {
        "max_game_changers": 3,
        "max_mass_land_denial": 0,
        "max_extra_turns": 2,
        "max_tutors": 5,
        "max_fast_mana": 2,
    },
    4: {
        "max_game_changers": 99,
        "max_mass_land_denial": 99,
        "max_extra_turns": 99,
        "max_tutors": 99,
        "max_fast_mana": 99,
    },
    5: {
        "max_game_changers": 99,
        "max_mass_land_denial": 99,
        "max_extra_turns": 99,
        "max_tutors": 99,
        "max_fast_mana": 99,
    },
}
