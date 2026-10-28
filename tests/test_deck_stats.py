"""Deterministic deck analysis tests."""

from __future__ import annotations

from mtg.deck_stats import analyze_deck, deck_fingerprint


def commander():
    return {
        "name": "Commander",
        "type": "Legendary Creature — Human",
        "types": "Creature",
        "colorIdentity": "W",
        "supertypes": "Legendary",
        "manaValue": 4,
        "text": "",
        "leadershipSkills": '{"commander": true}',
        "legalities": {"commander": "Legal"},
    }


def card(name, type_line="Creature", types="Creature", text="", mv=2, ci="W", qty=1):
    return {
        "uuid": name.lower().replace(" ", "-"),
        "name": name,
        "type": type_line,
        "types": types,
        "text": text,
        "manaValue": mv,
        "colorIdentity": ci,
        "quantity": qty,
        "supertypes": "",
        "legalities": {"commander": "Legal"},
    }


def deck(cards):
    return {
        "name": "Test",
        "commander": commander(),
        "cards": cards,
        "sections": {},
        "guideline": "Default Guideline",
    }


def test_counts_curve_and_roles():
    cards = [
        card("Plains", "Basic Land — Plains", "Land", mv=0, qty=5),
        card("Sol Ring", "Artifact", "Artifact", "{T}: Add {C}{C}.", mv=1),
        card("Wrath", "Sorcery", "Sorcery", "Destroy all creatures.", mv=4),
        card("Rhystic", "Enchantment", "Enchantment",
             "Whenever an opponent casts a spell, you may draw a card.", mv=3),
        card("Bear", "Creature", "Creature", "", mv=2),
    ]
    audit = analyze_deck(deck(cards))
    assert audit["counts"]["main"] == 9
    assert audit["counts"]["commander"] == 1
    assert audit["curve"]["landCount"] == 5
    assert audit["curve"]["buckets"]["1"] == 1
    assert audit["curve"]["buckets"]["4"] == 1
    assert audit["curve"]["averageMv"] == 2.5
    assert audit["roles"]["Lands"]["count"] == 5
    assert audit["roles"]["Ramp"]["count"] == 1
    assert audit["roles"]["Board Wipe"]["count"] == 1
    assert audit["roles"]["Card Draw"]["count"] == 1
    assert audit["primary"]["Lands"]["count"] == 5
    assert audit["primary"]["Ramp"]["count"] == 1
    assert audit["primary"]["Removal"]["count"] == 1


def test_guideline_uses_primary_categories():
    cards = [
        card("Plains", "Basic Land — Plains", "Land", mv=0, qty=10),
        card("Sol Ring", "Artifact", "Artifact", "{T}: Add {C}{C}.", mv=1),
        card("Rhystic", "Enchantment", "Enchantment",
             "Whenever an opponent casts a spell, you may draw a card.", mv=3),
        card("Wrath", "Sorcery", "Sorcery", "Destroy all creatures.", mv=4),
    ]
    guideline = {
        "name": "Default Guideline",
        "categories": {"Lands": 37, "Ramp": 10, "Draw": 10, "Removal": 10, "Core": 30},
    }
    audit = analyze_deck(deck(cards), guideline=guideline)
    assert audit["guideline"]["categories"]["Lands"]["actual"] == 10
    assert audit["guideline"]["categories"]["Ramp"]["actual"] == 1
    assert audit["guideline"]["categories"]["Draw"]["actual"] == 1
    assert audit["guideline"]["categories"]["Removal"]["actual"] == 1


def test_health_components_sum():
    audit = analyze_deck(deck([card("Plains", "Basic Land — Plains", "Land", mv=0)]))
    health = audit["health"]
    assert health["score"] == sum(health["components"].values())
    assert 0 <= health["score"] <= 100


def test_role_gaps():
    audit = analyze_deck(deck([card("Plains", "Basic Land — Plains", "Land", mv=0)]))
    gaps = {gap["role"]: gap for gap in audit["roleGaps"]}
    assert gaps["Lands"]["actual"] == 1
    assert gaps["Lands"]["deficit"] > 0
    assert gaps["Ramp"]["deficit"] > 0


def test_legality_errors_reduce_health():
    cards = [card("Off Color", ci="U")]
    audit = analyze_deck(deck(cards))
    assert audit["legality"]["legal"] is False
    assert audit["health"]["components"]["legality"] < 10


def test_bracket_and_salt_are_present():
    audit = analyze_deck(deck([card("Salty", text="Destroy all lands.", mv=4,
                                     type_line="Sorcery", types="Sorcery")]))
    assert audit["bracket"]["estimate"] >= 4
    assert "salt" in audit["bracket"]


def test_fingerprint_changes_with_deck_contents():
    d = deck([card("A")])
    first = deck_fingerprint(d)
    assert first == deck_fingerprint(d)
    d["cards"].append(card("B"))
    assert deck_fingerprint(d) != first


def test_empty_deck_is_safe():
    audit = analyze_deck({"commander": None, "cards": [], "sections": {}})
    assert audit["curve"]["averageMv"] is None
    assert audit["health"]["score"] >= 0
    assert audit["legality"]["legal"] is False
