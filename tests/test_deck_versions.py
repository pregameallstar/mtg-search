"""Unit tests for deck snapshots and comparison."""

from __future__ import annotations

from mtg.deck_versions import (
    compare_decks,
    deck_content_hash,
    list_versions,
    load_version,
    write_version,
)


def card(name, qty=1, ci="W", mv=2, type_line="Creature", types="Creature", text=""):
    return {
        "uuid": name.lower().replace(" ", "-"),
        "name": name,
        "setCode": "TST",
        "number": "1",
        "quantity": qty,
        "colorIdentity": ci,
        "manaValue": mv,
        "type": type_line,
        "types": types,
        "text": text,
        "supertypes": "",
    }


def deck(name="Deck"):
    return {
        "name": name,
        "commander": {
            "uuid": "cmdr",
            "name": "Commander",
            "type": "Legendary Creature — Human",
            "types": "Creature",
            "colorIdentity": "W",
            "manaValue": 4,
            "supertypes": "Legendary",
            "legalities": {"commander": "Legal"},
        },
        "cards": [card("Sol Ring", mv=1, type_line="Artifact", types="Artifact"),
                  card("Wrath of God", mv=4, type_line="Sorcery", types="Sorcery",
                       text="Destroy all creatures.")],
        "sections": {},
    }


def test_write_list_load_snapshot(tmp_path):
    filename, reason = write_version(str(tmp_path), deck(), force=True)
    assert reason == "created"
    assert filename
    versions = list_versions(str(tmp_path), "Deck")
    assert [version["filename"] for version in versions] == [filename]
    snapshot = load_version(str(tmp_path), filename)
    assert snapshot["name"] == "Deck"
    assert len(snapshot["cards"]) == 2


def test_duplicate_snapshot_is_skipped(tmp_path):
    write_version(str(tmp_path), deck(), force=True)
    filename, reason = write_version(str(tmp_path), deck(), force=True)
    assert filename is None
    assert reason == "unchanged"


def test_throttled_snapshot(tmp_path):
    write_version(str(tmp_path), deck(), force=True)
    changed = deck()
    changed["cards"].append(card("Cultivate", mv=3, type_line="Sorcery", types="Sorcery"))
    filename, reason = write_version(str(tmp_path), changed, min_interval=1000)
    assert filename is None
    assert reason == "throttled"


def test_content_hash_stable():
    assert deck_content_hash(deck()) == deck_content_hash(deck())
    changed = deck()
    changed["cards"].append(card("Cultivate"))
    assert deck_content_hash(changed) != deck_content_hash(deck())


def test_compare_decks_add_remove_change():
    a = deck("A")
    b = deck("B")
    b["cards"] = [b["cards"][0], card("Cultivate", mv=3, type_line="Sorcery", types="Sorcery")]
    b["cards"][0]["quantity"] = 2
    comparison = compare_decks(a, b)
    assert comparison["summary"]["added"] == 1
    assert comparison["summary"]["removed"] == 1
    assert comparison["summary"]["changed"] == 1
    assert comparison["added"][0]["name"] == "Cultivate"
    assert comparison["removed"][0]["name"] == "Wrath of God"


def test_compare_decks_role_deltas():
    a = deck("A")
    b = deck("B")
    b["cards"].append(card("Rhystic Study", mv=3, type_line="Enchantment", types="Enchantment",
                           text="Whenever an opponent casts a spell, you may draw a card."))
    comparison = compare_decks(a, b)
    assert comparison["roles"]["Card Draw"]["b"] >= 1
