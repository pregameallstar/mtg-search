"""Deck rules tests."""

from __future__ import annotations

from mtg.deck_rules import is_singleton_exempt, validate_deck


def commander(name="Test Commander", ci="W", **over):
    base = {
        "name": name,
        "type": "Legendary Creature — Human",
        "types": "Creature",
        "colorIdentity": ci,
        "supertypes": "Legendary",
        "text": "",
        "leadershipSkills": '{"commander": true}',
        "legalities": {"commander": "Legal"},
    }
    base.update(over)
    return base


def main_card(name, ci="W", qty=1, **over):
    base = {
        "uuid": name.lower().replace(" ", "-"),
        "name": name,
        "type": "Creature",
        "types": "Creature",
        "colorIdentity": ci,
        "supertypes": "",
        "text": "",
        "quantity": qty,
        "legalities": {"commander": "Legal"},
    }
    base.update(over)
    return base


def deck(cards=None, commander_card=None):
    return {
        "name": "Test Deck",
        "commander": commander_card or commander(),
        "cards": cards if cards is not None else [
            main_card(f"Card {i}") for i in range(99)
        ],
        "sections": {},
    }


def test_legal_deck_passes():
    result = validate_deck(deck())
    assert result["legal"] is True
    assert result["errors"] == []
    assert result["counts"]["total"] == 100


def test_wrong_count_fails():
    result = validate_deck(deck(cards=[main_card(f"Card {i}") for i in range(98)]))
    assert result["legal"] is False
    assert any(error["code"] == "wrong_count" for error in result["errors"])


def test_missing_commander_fails():
    result = validate_deck({"cards": [main_card("A")]})
    assert result["legal"] is False
    assert any(error["code"] == "missing_commander" for error in result["errors"])


def test_noncommander_fails():
    result = validate_deck(deck(commander_card=commander(
        "Not Legendary", type="Creature — Human", supertypes="", leadershipSkills="{}"
    )))
    assert any(error["code"] == "invalid_commander" for error in result["errors"])


def test_singleton_violation():
    cards = [main_card(f"Card {i}") for i in range(97)]
    cards.append(main_card("Duplicate", qty=2))
    result = validate_deck(deck(cards=cards))
    assert any(error["code"] == "singleton" for error in result["errors"])


def test_basic_land_and_any_number_are_exempt():
    cards = [main_card(f"Card {i}") for i in range(93)]
    cards.append(main_card("Plains", ci="W", qty=3, supertypes="Basic",
                           type="Basic Land — Plains", types="Land"))
    cards.append(main_card("Hare Apparent", qty=3,
                           text="A deck can have any number of cards named Hare Apparent."))
    result = validate_deck(deck(cards=cards))
    assert not any(error["code"] in ("singleton", "duplicate") for error in result["errors"])
    assert is_singleton_exempt(cards[-2]) is True
    assert is_singleton_exempt(cards[-1]) is True


def test_off_color_card_fails_including_colorless_commander():
    cards = [main_card(f"Card {i}") for i in range(98)]
    cards.append(main_card("Off Color", ci="U"))
    result = validate_deck(deck(cards=cards))
    assert any(error["code"] == "color_identity" for error in result["errors"])

    colorless = commander("Colorless Commander", ci="")
    cards = [main_card(f"Card {i}", ci="") for i in range(98)]
    cards.append(main_card("Colored", ci="W"))
    result = validate_deck(deck(cards=cards, commander_card=colorless))
    assert any(error["code"] == "color_identity" for error in result["errors"])


def test_banned_commander_fails():
    result = validate_deck(deck(commander_card=commander(
        legalities={"commander": "Banned"}
    )))
    assert any(error["code"] == "commander_not_legal" for error in result["errors"])


def test_partner_warning():
    result = validate_deck(deck(commander_card=commander(
        "Partner Commander", text="Partner", keywords="Partner"
    )))
    assert any(warning["code"] == "partner_not_modeled" for warning in result["warnings"])


def test_sections_are_counted_separately():
    d = deck()
    d["sections"] = {"Maybeboard": [main_card("Maybe", qty=2)]}
    result = validate_deck(d)
    assert result["counts"]["sections"] == {"Maybeboard": 2}
    assert result["counts"]["total"] == 100
