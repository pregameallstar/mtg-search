"""API tests for role batches and deterministic deck analysis."""

from __future__ import annotations

import json
import sqlite3

import pytest

import app as app_module
from mtg import shared
from mtg.blueprints import deck_analysis
from mtg.storage import write_json_atomic
from tests.fixtures import build_search_db


@pytest.fixture()
def search_db_path(tmp_path):
    return build_search_db(tmp_path / "search.sqlite")


@pytest.fixture()
def client(search_db_path, monkeypatch):
    monkeypatch.setattr(shared, "db_path", lambda *args, **kwargs: str(search_db_path))
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


def _uuid(search_db_path, name):
    conn = sqlite3.connect(search_db_path)
    try:
        return conn.execute("SELECT uuid FROM cards WHERE name = ?", (name,)).fetchone()[0]
    finally:
        conn.close()


def test_cards_roles_resolves_from_db(client):
    response = client.post("/api/cards/roles", json={
        "cards": [{"setCode": "LTC", "number": "1"}],
    })
    assert response.status_code == 200
    data = response.get_json()
    assert data["rolesVersion"] == 1
    result = data["results"][0]
    assert result["name"] == "Sol Ring"
    assert "Ramp" in result["roles"]
    assert result["primary"] == "Ramp"


def test_cards_roles_uses_supplied_text(client):
    response = client.post("/api/cards/roles", json={
        "cards": [{
            "name": "Text Card",
            "text": "Counter target spell.",
            "type": "Instant",
            "types": "Instant",
        }],
    })
    assert response.status_code == 200
    result = response.get_json()["results"][0]
    assert result["roles"] == ["Counterspell"]


def test_cards_roles_rejects_bad_body_and_large_batch(client):
    assert client.post("/api/cards/roles", json={}).status_code == 400
    assert client.post("/api/cards/roles", json={"cards": "x"}).status_code == 400
    assert client.post("/api/cards/roles", json={
        "cards": [{}] * 201,
    }).status_code == 400


def test_cards_roles_unresolved_card_is_per_item_error(client):
    response = client.post("/api/cards/roles", json={
        "cards": [{"name": "Definitely Not A Card"}],
    })
    assert response.status_code == 200
    assert "error" in response.get_json()["results"][0]


def test_deck_analyze_from_object(client, search_db_path):
    deck = {
        "name": "Test Deck",
        "commander": {"name": "Alela, Artful Provocateur"},
        "cards": [
            {"name": "Sol Ring", "quantity": 1},
            {"name": "Rhystic Study", "quantity": 1},
            {"name": "Wrath of God", "quantity": 1},
        ],
        "sections": {},
    }
    response = client.post("/api/deck/analyze", json={"deck": deck})
    assert response.status_code == 200
    data = response.get_json()
    assert data["success"] is True
    assert data["schemaVersion"] == 1
    assert data["rolesVersion"] == 1
    assert data["deck"]["commander"]["name"] == "Alela, Artful Provocateur"
    assert data["deck"]["fingerprint"]
    assert data["audit"]["counts"]["total"] == 4
    assert data["audit"]["roles"].get("Lands", {"count": 0})["count"] == 0
    assert data["audit"]["bracket"]["estimate"] >= 1
    assert data["audit"]["health"]["score"] >= 0
    assert data["audit"]["cardRoles"]


def test_deck_analyze_saved_deck(client, tmp_path, monkeypatch):
    monkeypatch.setattr(deck_analysis, "DECKS_DIR", str(tmp_path))
    write_json_atomic(tmp_path / "Saved.json", {
        "name": "Saved",
        "commander": {"name": "Alela, Artful Provocateur"},
        "cards": [{"name": "Sol Ring", "quantity": 1}],
        "sections": {},
    })
    response = client.post("/api/deck/analyze", json={"name": "Saved"})
    assert response.status_code == 200
    assert response.get_json()["deck"]["name"] == "Saved"


def test_deck_analyze_errors(client):
    assert client.post("/api/deck/analyze", json={}).status_code == 400
    assert client.post("/api/deck/analyze", json={"deck": "x"}).status_code == 400
    assert client.post("/api/deck/analyze", json={"name": "Missing"}).status_code == 404


def test_deck_analyze_guideline(client, tmp_path, monkeypatch):
    monkeypatch.setattr(deck_analysis, "DECKS_DIR", str(tmp_path))
    monkeypatch.setattr(deck_analysis, "TEMPLATES_DIR", str(tmp_path / "templates"))
    (tmp_path / "templates").mkdir()
    write_json_atomic(tmp_path / "templates" / "Default_Guideline.json", {
        "type": "guideline",
        "name": "Default Guideline",
        "categories": {"Lands": 37, "Ramp": 10, "Draw": 10, "Removal": 10, "Core": 30},
    })
    response = client.post("/api/deck/analyze", json={
        "deck": {
            "name": "Test",
            "commander": {"name": "Alela, Artful Provocateur"},
            "cards": [{"name": "Sol Ring", "quantity": 1}],
            "guideline": "Default Guideline",
            "sections": {},
        },
    })
    assert response.status_code == 200
    assert response.get_json()["audit"]["guideline"]["name"] == "Default Guideline"
