"""Bracket and salt estimator tests."""

from __future__ import annotations

from mtg.bracket import estimate_bracket


def card(name, text="", type_line="Artifact", types="Artifact", **over):
    base = {
        "name": name,
        "text": text,
        "type": type_line,
        "types": types,
        "manaValue": 2,
        "edhrecSaltiness": None,
        "isGameChanger": 0,
    }
    base.update(over)
    return base


def test_no_signals_is_low_bracket_with_medium_confidence():
    result = estimate_bracket([card("Plain Rock")])
    assert result["estimate"] <= 2
    assert result["confidence"] == "medium"
    assert any("Combo data unavailable" in note for note in result["notes"])


def test_game_changers_raise_bracket():
    cards = [card(f"GC {i}", isGameChanger=1) for i in range(4)]
    result = estimate_bracket(cards)
    assert result["estimate"] >= 4
    assert result["signals"][0]["count"] == 4


def test_mass_land_denial_raises_bracket():
    result = estimate_bracket([card("Armageddon", "Destroy all lands.", "Sorcery", "Sorcery")])
    assert result["estimate"] >= 4
    assert result["signals"][1]["count"] == 1


def test_extra_turns_and_tutors_raise_bracket():
    cards = [
        card("Time Warp", "Take an extra turn after this one.", "Sorcery", "Sorcery"),
        card("Time Stretch", "Take two extra turns after this one.", "Sorcery", "Sorcery"),
        card("Tutor 1", "Search your library for a card, put it into your hand.", "Sorcery", "Sorcery"),
        card("Tutor 2", "Search your library for a card, put it into your hand.", "Sorcery", "Sorcery"),
        card("Tutor 3", "Search your library for a card, put it into your hand.", "Sorcery", "Sorcery"),
        card("Tutor 4", "Search your library for a card, put it into your hand.", "Sorcery", "Sorcery"),
        card("Tutor 5", "Search your library for a card, put it into your hand.", "Sorcery", "Sorcery"),
    ]
    result = estimate_bracket(cards)
    assert result["estimate"] >= 4
    assert result["signals"][2]["count"] == 2
    assert result["signals"][3]["count"] == 5


def test_fast_mana_raises_bracket():
    cards = [card(name) for name in ("Sol Ring", "Mana Crypt", "Mana Vault", "Chrome Mox", "Mox Diamond", "Grim Monolith")]
    result = estimate_bracket(cards)
    assert result["estimate"] == 5
    assert result["signals"][4]["count"] == 6


def test_combo_count_raises_and_increases_confidence():
    result = estimate_bracket([card("Piece")], combo_count=3)
    assert result["estimate"] == 5
    assert result["confidence"] == "high"
    assert not any("Combo data unavailable" in note for note in result["notes"])


def test_salt_reported_without_forcing_bracket():
    result = estimate_bracket([
        card("Salty", edhrecSaltiness=2.7),
        card("Mild", edhrecSaltiness=0.2),
    ])
    assert result["salt"]["average"] == 1.45
    assert result["salt"]["score"] == 36
    assert result["signals"][-1]["top"][0]["name"] == "Salty"


def test_target_fit_over_and_within():
    over = estimate_bracket(
        [card("GC", isGameChanger=1)], target_bracket=2
    )["target_fit"]
    assert over["target"] == 2
    assert over["status"] == "over"

    within = estimate_bracket([card("Plain Rock")], target_bracket=2)["target_fit"]
    assert within["status"] in ("within", "below")


def test_empty_deck_is_safe():
    result = estimate_bracket([])
    assert result["estimate"] in range(1, 6)
    assert result["signals"][0]["count"] == 0
