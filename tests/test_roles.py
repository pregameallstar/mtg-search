"""Role classification tests."""

from __future__ import annotations

import pytest

from mtg.roles import classify_roles


def card(name, text, type_line="", types="", keywords="", tags=None, supertypes=""):
    return {
        "name": name,
        "text": text,
        "type": type_line,
        "types": types,
        "keywords": keywords,
        "supertypes": supertypes,
        "tags": tags or [],
    }


@pytest.mark.parametrize("c, expected, primary", [
    (card("Sol Ring", "{T}: Add {C}{C}.", "Artifact", "Artifact"),
     {"Ramp", "Fast Mana"}, "Ramp"),
    (card("Cultivate", "Search your library for up to two basic land cards, put one onto the battlefield tapped and the other into your hand.", "Sorcery", "Sorcery"),
     {"Ramp"}, "Ramp"),
    (card("Rhystic Study", "Whenever an opponent casts a spell, you may draw a card unless that player pays {1}.", "Enchantment", "Enchantment"),
     {"Card Draw"}, "Draw"),
    (card("Swords to Plowshares", "Exile target creature.", "Instant", "Instant"),
     {"Targeted Removal"}, "Removal"),
    (card("Wrath of God", "Destroy all creatures. They can't be regenerated.", "Sorcery", "Sorcery"),
     {"Board Wipe"}, "Removal"),
    (card("Counterspell", "Counter target spell.", "Instant", "Instant"),
     {"Counterspell"}, "Removal"),
    (card("Demonic Tutor", "Search your library for a card, put that card into your hand, then shuffle.", "Sorcery", "Sorcery"),
     {"Tutor"}, "Core"),
    (card("Heroic Intervention", "Permanents you control gain hexproof and indestructible until end of turn.", "Instant", "Instant"),
     {"Protection"}, "Core"),
    (card("Reanimate", "Put target creature card from a graveyard onto the battlefield under your control.", "Sorcery", "Sorcery"),
     {"Recursion"}, "Core"),
    (card("Viscera Seer", "Sacrifice a creature: Scry 1.", "Creature", "Creature"),
     {"Card Selection", "Sac Outlet"}, "Draw"),
    (card("Smothering Tithe", "Whenever an opponent draws a card, that player may pay {2}. If they don't, you create a Treasure token.", "Enchantment", "Enchantment"),
     {"Ramp", "Tokens"}, "Ramp"),
    (card("Time Warp", "Target player takes an extra turn after this one.", "Sorcery", "Sorcery"),
     {"Extra Turns"}, "Core"),
    (card("Armageddon", "Destroy all lands.", "Sorcery", "Sorcery"),
     {"Mass Land Denial"}, "Core"),
    (card("Thassa's Oracle", "When this creature enters, look at the top X cards of your library. If your devotion to blue is ten or greater, you win the game.", "Creature", "Creature"),
     {"Card Selection", "Win Condition"}, "Draw"),
    (card("Doubling Season", "If an effect would create one or more tokens, it creates twice that many. If an effect would put one or more +1/+1 counters on a permanent, it puts twice that many.", "Enchantment", "Enchantment"),
     {"Tokens", "Counters"}, "Core"),
    (card("Thalia, Guardian of Thraben", "Spells your opponents cast cost {1} more to cast.", "Legendary Creature — Human Soldier", "Creature"),
     {"Stax"}, "Core"),
    (card("Plains", "({T}: Add {W}.)", "Basic Land — Plains", "Land"),
     {"Lands"}, "Lands"),
])
def test_roles(c, expected, primary):
    result = classify_roles(c)
    assert set(result["roles"]) == expected
    assert result["primary"] == primary


def test_draw_hate_is_not_draw():
    assert "Card Draw" not in classify_roles(card("Narset", "Each opponent can't draw more than one card each turn.", "Planeswalker", "Planeswalker"))["roles"]


def test_death_trigger_is_not_sac_outlet():
    result = classify_roles(card("Blood Artist", "Whenever a creature dies, target player loses 1 life.", "Creature", "Creature"))
    assert "Sac Outlet" not in result["roles"]


def test_counter_target_spell_is_not_targeted_removal():
    result = classify_roles(card("Counterspell", "Counter target spell.", "Instant", "Instant"))
    assert "Targeted Removal" not in result["roles"]


def test_land_mana_ability_is_not_ramp():
    result = classify_roles(card("Forest", "({T}: Add {G}.)", "Basic Land — Forest", "Land"))
    assert "Ramp" not in result["roles"]


def test_reminder_text_does_not_create_roles():
    result = classify_roles(card("Vanilla", "Flying (This creature can't be blocked except by creatures with flying or reach.)", "Creature", "Creature"))
    assert result["roles"] == []


def test_tags_add_roles_and_override_primary():
    result = classify_roles(card("Mystery", "Nothing here.", "Artifact", "Artifact", tags=["Card Draw"]))
    assert "Card Draw" in result["roles"]
    assert result["primary"] == "Draw"
    assert result["source"] == "tag"


def test_wipe_alias():
    result = classify_roles(card("Mystery", "Nothing here.", "Sorcery", "Sorcery", tags=["Wipe"]))
    assert "Board Wipe" in result["roles"]
    assert result["primary"] == "Removal"


def test_unknown_tag_is_ignored():
    result = classify_roles(card("Mystery", "Nothing here.", "Artifact", "Artifact", tags=["My Custom Tag"]))
    assert result["roles"] == []
    assert result["primary"] == "Core"


def test_roles_are_deterministically_ordered():
    result = classify_roles(card("Sol Ring", "{T}: Add {C}{C}.", "Artifact", "Artifact"))
    assert result["roles"] == ["Ramp", "Fast Mana"]
    assert [item["role"] for item in result["evidence"]] == result["roles"]


def test_empty_card_is_safe():
    result = classify_roles({})
    assert result["roles"] == []
    assert result["primary"] == "Core"
