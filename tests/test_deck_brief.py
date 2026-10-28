"""Structured Deck Brief schema and its deterministic effects."""

from __future__ import annotations

from mtg.deck_brief import (
    brief_caps,
    brief_fingerprint,
    brief_from_deck,
    brief_is_empty,
    brief_role_caps,
    brief_to_deck,
    declared_win_plan_credit,
    merge_caps,
    normalize_brief,
    normalize_win_plans,
    suppressed_roles,
)
from mtg.deck_review import cut_candidates, validate_analysis
from mtg.deck_stats import analyze_deck


def _deck(*, notes=""):
    return {
        "name": "Test",
        "commander": {"name": "Cmdr", "colorIdentity": "W"},
        "cards": [],
        "sections": {},
        "guideline": "",
        "reviewNotes": notes,
    }


def test_normalize_brief_cleans_and_validates_fields():
    brief = normalize_brief({
        "archetype": "  aristocrats ",
        "themes": "sacrifice, squirrels, sacrifice",
        "win_plans": ["Combat", "alt-win", "bogus"],
        "constraints": {"counterspell": 2, "Board Wipe": "1", "Bogus": 9},
        "must_keep": ["Sol Ring", "Sol Ring"],
        "avoid": "Armageddon; Winter Orb",
        "notes": "  Rule 0  ",
    })
    assert brief["archetype"] == "aristocrats"
    assert brief["themes"] == ["sacrifice", "squirrels"]
    assert brief["win_plans"] == ["combat", "alt_win"]
    assert brief["constraints"] == {"Board Wipe": 1, "Counterspell": 2}
    assert brief["must_keep"] == ["Sol Ring"]
    assert brief["avoid"] == ["Armageddon", "Winter Orb"]
    assert brief["notes"] == "Rule 0"


def test_card_lists_keep_commas_inside_legendary_names():
    brief = normalize_brief({
        "must_keep": "Alela, Artful Provocateur; Sol Ring",
        "avoid": "Atraxa, Praetors' Voice",
    })
    assert brief["must_keep"] == ["Alela, Artful Provocateur", "Sol Ring"]
    assert brief["avoid"] == ["Atraxa, Praetors' Voice"]


def test_legacy_review_notes_migrate_to_brief_notes():
    deck = _deck(notes="Squirrel tribal.")
    brief = brief_from_deck(deck)
    assert brief["notes"] == "Squirrel tribal."
    assert brief["archetype"] == ""
    assert not brief_is_empty(brief)

    brief_to_deck(deck, {**brief, "archetype": "tribal"})
    assert deck["deckBrief"]["archetype"] == "tribal"
    assert deck["reviewNotes"] == "Squirrel tribal."


def test_brief_to_deck_clears_notes_when_empty():
    deck = _deck(notes="Old notes")
    brief_to_deck(deck, normalize_brief({}))
    assert "reviewNotes" not in deck
    # the object itself remains, but it is empty
    assert brief_is_empty(deck["deckBrief"])


def test_merge_caps_takes_the_tightest_limit():
    assert merge_caps(
        {"Board Wipe": 3, "Extra Turns": 2},
        {"Board Wipe": 1, "counterspells": 2},
    ) == {"Board Wipe": 1, "Counterspell": 2, "Extra Turns": 2}


def test_notes_and_structured_caps_both_apply():
    notes = brief_role_caps("Rule 0: max 2 board wipes, no extra turns")
    structured = brief_caps({"constraints": {"Board Wipe": 1}})
    assert merge_caps(notes, structured) == {
        "Board Wipe": 1,
        "Extra Turns": 0,
    }


def test_declared_non_alt_win_plan_suppresses_wincon_gap_and_credits_health():
    cards = [{"name": "Wrath of God", "types": "Sorcery", "type": "Sorcery",
              "text": "Destroy all creatures.", "manaValue": 4}]
    deck = _deck()
    deck["cards"] = cards
    plain = analyze_deck(deck)
    declared = analyze_deck(deck, win_plans=["combat"])
    assert declared["winPlans"] == ["combat"]
    assert declared["suppressedRoles"] == ["Win Condition"]
    assert declared["health"]["components"]["wincons"] == 10
    assert declared["health"]["score"] > plain["health"]["score"]
    assert all(gap["role"] != "Win Condition" for gap in declared["roleGaps"])
    assert any(w["code"] == "declared_wincon" for w in declared["warnings"])


def test_alt_win_plan_keeps_the_explicit_wincon_gap():
    deck = _deck()
    deck["cards"] = []
    audit = analyze_deck(deck, win_plans=["alt_win"])
    assert any(
        gap["role"] == "Win Condition" and gap["deficit"] > 0
        for gap in audit["roleGaps"]
    )
    assert audit["health"]["components"]["wincons"] == 0


def test_cap_only_roles_flag_surplus_without_health_penalty():
    deck = _deck()
    deck["cards"] = [{
        "name": "Time Warp",
        "types": "Sorcery",
        "type": "Sorcery",
        "text": "Take an extra turn after this one.",
        "manaValue": 5,
    }]
    audit = analyze_deck(deck, brief_caps={"Extra Turns": 0})
    gap = next(g for g in audit["roleGaps"] if g["role"] == "Extra Turns")
    assert gap == {
        "role": "Extra Turns",
        "target": 0,
        "actual": 1,
        "deficit": 0,
        "surplus": 1,
        "limitOnly": True,
    }
    assert audit["health"]["components"]["wincons"] == 0  # no win plan declared
    assert audit["health"]["components"]["roles"] >= 0


def test_must_keep_cards_are_excluded_from_cuts_and_validation():
    deck = _deck()
    deck["cards"] = [
        {"name": "Big Spell", "types": "Sorcery", "type": "Sorcery",
         "text": "Draw a card.", "manaValue": 9, "edhrecRank": 50000},
        {"name": "Kept Spell", "types": "Sorcery", "type": "Sorcery",
         "text": "Draw a card.", "manaValue": 9, "edhrecRank": 50000},
    ]
    audit = analyze_deck(deck)
    cuts = cut_candidates(deck, audit, must_keep=["Kept Spell"])
    assert any(cut["name"] == "Big Spell" for cut in cuts)
    assert all(cut["name"] != "Kept Spell" for cut in cuts)

    analysis = {
        "adds": [],
        "cuts": [{"name": "Kept Spell", "reason": "test"}],
    }
    result, validation = validate_analysis(
        analysis, deck, {}, max_adds=10, max_cuts=10, must_keep=["Kept Spell"]
    )
    assert result["cuts"] == []
    assert validation["rejected"] == [{
        "type": "cut", "name": "Kept Spell", "reason": "must-keep card",
    }]


def test_avoid_cards_are_rejected_from_adds():
    deck = _deck()
    analysis = {"cuts": [], "adds": [{"name": "Armageddon", "reason": "test"}]}
    result, validation = validate_analysis(
        analysis, deck, {}, avoid=["Armageddon"]
    )
    assert result["adds"] == []
    assert validation["rejected"][0]["reason"] == "on the avoid list"


def test_brief_fingerprint_changes_with_structured_intent():
    base = {"version": 1, "archetype": "control", "themes": ["draw"]}
    changed = {**base, "archetype": "aggro"}
    assert brief_fingerprint(base) != brief_fingerprint(changed)


def test_normalize_win_plans_strips_labels_and_duplicates():
    assert normalize_win_plans("Combat, commander damage, combat") == [
        "combat", "commander_damage",
    ]
    assert declared_win_plan_credit({"win_plans": ["combat"]}) is True
    assert declared_win_plan_credit({"win_plans": ["alt_win"]}) is False
    assert suppressed_roles({"win_plans": ["alt_win"]}) == set()
