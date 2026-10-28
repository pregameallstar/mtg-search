"""API tests for deck versions and comparison."""

from __future__ import annotations

import pytest

import app as app_module
from mtg.blueprints import deck_versions
from mtg.storage import write_json_atomic


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(deck_versions, "DECKS_DIR", str(tmp_path))
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


def _card(name, qty=1, text="", mv=2, types="Creature", type_line="Creature"):
    return {
        "uuid": name.lower().replace(" ", "-"),
        "name": name,
        "setCode": "TST",
        "number": "1",
        "quantity": qty,
        "manaValue": mv,
        "types": types,
        "type": type_line,
        "text": text,
        "colorIdentity": "W",
        "supertypes": "",
    }


def _deck(name="Deck A", cards=None):
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
        "cards": cards or [_card("Sol Ring", mv=1, types="Artifact", type_line="Artifact")],
        "sections": {},
    }


def test_compare_page(client, tmp_path):
    write_json_atomic(tmp_path / "Deck_A.json", _deck("Deck A"))
    response = client.get("/compare-decks")
    assert response.status_code == 200
    assert b"Compare Decks" in response.data
    assert b"Deck A" in response.data


def test_compare_api(client, tmp_path):
    write_json_atomic(tmp_path / "Deck_A.json", _deck("Deck A"))
    write_json_atomic(tmp_path / "Deck_B.json", _deck(
        "Deck B", [_card("Cultivate", mv=3, types="Sorcery", type_line="Sorcery")]
    ))
    response = client.post("/api/deck/compare", json={"a": "Deck A", "b": "Deck B"})
    assert response.status_code == 200
    comparison = response.get_json()["comparison"]
    assert comparison["summary"]["added"] == 1
    assert comparison["summary"]["removed"] == 1
    assert comparison["a"]["name"] == "Deck A"


def test_versions_snapshot_list_load(client, tmp_path):
    write_json_atomic(tmp_path / "Deck_A.json", _deck("Deck A"))
    snap = client.post("/api/deck/versions/snapshot", json={"name": "Deck A"})
    assert snap.status_code == 200
    filename = snap.get_json()["filename"]
    assert filename

    versions = client.get("/api/deck/versions?deck=Deck A").get_json()["versions"]
    assert any(version["filename"] == filename for version in versions)

    loaded = client.get(f"/api/deck/version?deck=Deck A&filename={filename}")
    assert loaded.status_code == 200
    assert loaded.get_json()["deck"]["name"] == "Deck A"

    diff = client.get(f"/api/deck/version/diff?deck=Deck A&filename={filename}")
    assert diff.status_code == 200
    assert diff.get_json()["comparison"]["summary"]["added"] == 0


def test_version_restore(client, tmp_path):
    write_json_atomic(tmp_path / "Deck_A.json", _deck("Deck A"))
    filename = client.post("/api/deck/versions/snapshot", json={
        "name": "Deck A"
    }).get_json()["filename"]

    changed = _deck("Deck A", [_card("Cultivate")])
    write_json_atomic(tmp_path / "Deck_A.json", changed)
    restored = client.post("/api/deck/versions/restore", json={
        "deck": "Deck A", "filename": filename,
    })
    assert restored.status_code == 200
    current = client.get("/api/deck/version?deck=Deck A&filename=" + filename)
    assert current.status_code == 200
    versions = client.get("/api/deck/versions?deck=Deck A").get_json()["versions"]
    assert len(versions) >= 2  # pre-restore state is snapshotted


def test_version_delete_and_validation(client, tmp_path):
    write_json_atomic(tmp_path / "Deck_A.json", _deck("Deck A"))
    filename = client.post("/api/deck/versions/snapshot", json={
        "name": "Deck A"
    }).get_json()["filename"]
    assert client.post("/api/deck/versions/delete", json={"filename": filename}).status_code == 200
    assert client.post("/api/deck/versions/delete", json={"filename": "../x.json"}).status_code == 400
    assert client.get("/api/deck/version?deck=Deck A&filename=../x.json").status_code == 400
    assert client.post("/api/deck/compare", json={}).status_code == 400
