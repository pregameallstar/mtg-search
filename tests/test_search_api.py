"""Integration tests for Search 2.0 JSON APIs and Commander Finder."""

from __future__ import annotations

import sqlite3

import pytest

import app as app_module
import mtg.embed as embed
from mtg import shared
from tests.fixtures import build_search_db


@pytest.fixture()
def search_db_path(tmp_path):
    return build_search_db(tmp_path / "search.sqlite")


@pytest.fixture()
def search_db(search_db_path):
    conn = sqlite3.connect(search_db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def client(search_db_path, monkeypatch):
    monkeypatch.setattr(shared, "db_path", lambda *args, **kwargs: str(search_db_path))
    # Deterministic text fallback for Commander Finder unless a test overrides it.
    monkeypatch.setattr(embed, "find", lambda *args, **kwargs: [])
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


def test_api_search_contract(client):
    response = client.get("/api/search", query_string={"name": "Sol"})
    assert response.status_code == 200
    data = response.get_json()
    assert data["total"] == 1
    assert data["schemaVersion"] == 1
    assert data["page"] == 1
    assert data["per_page"] == 30
    assert data["random_sample"] is False
    card = data["cards"][0]
    assert card["name"] == "Sol Ring"
    assert card["fullName"] == "Sol Ring"
    assert card["setCode"] == "LTC"
    assert card["imageUrl"].startswith("/img/normal/front/")
    assert card["isGameChanger"] is False


def test_api_search_no_filters_is_empty(client):
    data = client.get("/api/search").get_json()
    assert data["cards"] == []
    assert data["total"] == 0
    assert data["total_pages"] == 0


def test_api_search_filters_and_sort(client):
    data = client.get(
        "/api/search",
        query_string={"set_type": "commander", "sort": "edhrec"},
    ).get_json()
    assert [card["name"] for card in data["cards"]] == [
        "Sol Ring", "Tymna the Weaver", "Cecil, Dark Knight",
    ]
    assert data["sort"] == "edhrec"
    assert data["order"] == "asc"


def test_api_search_new_filters(client):
    data = client.get("/api/search", query_string={"game_changer": "1"}).get_json()
    assert [card["name"] for card in data["cards"]] == ["Test 100% Card"]

    data = client.get("/api/search", query_string={"salt_min": "1"}).get_json()
    assert [card["name"] for card in data["cards"]] == ["Rhystic Study"]

    data = client.get("/api/search", query_string={"commander_only": "1"}).get_json()
    assert {card["name"] for card in data["cards"]} == {
        "Alela, Artful Provocateur", "Cecil, Dark Knight", "Tymna the Weaver",
    }


def test_api_search_random_returns_card(client):
    data = client.get("/api/search/random").get_json()
    assert data["card"] is not None
    assert data["card"]["name"]


def test_api_search_missing_db(client, search_db_path, monkeypatch):
    missing = search_db_path.parent / "missing.sqlite"
    monkeypatch.setattr(shared, "db_path", lambda *args, **kwargs: str(missing))
    response = client.get("/api/search", query_string={"name": "Sol"})
    assert response.status_code == 200
    data = response.get_json()
    assert data["cards"] == []
    assert data["warnings"] == ["Database not seeded."]


def test_api_commanders_only_eligible(client):
    data = client.get("/api/commanders").get_json()
    names = {c["name"] for c in data["commanders"]}
    assert names == {
        "Alela, Artful Provocateur", "Cecil, Dark Knight", "Tymna the Weaver",
    }
    assert data["method"] == "text"
    assert all(c["imageUrl"].startswith("/img/") for c in data["commanders"])


def test_api_commanders_color_identity_subset(client):
    # All fixture commanders include black; WUB is a valid superset.
    data = client.get("/api/commanders", query_string={"ci": "WUB"}).get_json()
    assert {c["name"] for c in data["commanders"]} == {
        "Alela, Artful Provocateur", "Cecil, Dark Knight", "Tymna the Weaver",
    }
    # None are within W only.
    data = client.get("/api/commanders", query_string={"ci": "W"}).get_json()
    assert data["commanders"] == []


def test_api_commanders_partner_and_edhrec(client):
    data = client.get(
        "/api/commanders", query_string={"partner": "1", "edhrec_max": "100"}
    ).get_json()
    assert [c["name"] for c in data["commanders"]] == ["Tymna the Weaver"]


def test_api_commanders_embedding_rerank(client, search_db, monkeypatch):
    tymna = search_db.execute(
        "SELECT uuid FROM cards WHERE name = 'Tymna the Weaver'"
    ).fetchone()["uuid"]
    alela = search_db.execute(
        "SELECT uuid FROM cards WHERE name = 'Alela, Artful Provocateur'"
    ).fetchone()["uuid"]

    # Fake an embedding result that ranks Tymna first, then Alela.
    def fake_find(db_path, text, top_k=200):
        return [
            {"uuid": tymna, "name": "Tymna the Weaver", "score": 0.9},
            {"uuid": alela, "name": "Alela, Artful Provocateur", "score": 0.8},
        ]

    monkeypatch.setattr(embed, "find", fake_find)
    data = client.get("/api/commanders", query_string={"q": "draw cards"}).get_json()
    assert data["method"] == "embed"
    assert [c["name"] for c in data["commanders"]] == [
        "Tymna the Weaver", "Alela, Artful Provocateur",
    ]


def test_commanders_page_renders(client):
    response = client.get("/commanders")
    assert response.status_code == 200
    assert b"Commander Finder" in response.data
    assert b"Alela, Artful Provocateur" in response.data


def test_commanders_page_missing_db(client, search_db_path, monkeypatch):
    missing = search_db_path.parent / "missing.sqlite"
    monkeypatch.setattr(shared, "db_path", lambda *args, **kwargs: str(missing))
    response = client.get("/commanders")
    assert response.status_code == 200
    assert b"No card database found" in response.data


def test_search_html_new_filters_and_sort(client):
    response = client.get(
        "/search", query_string={"set_type": "commander", "sort": "edhrec"}
    )
    assert response.status_code == 200
    assert b"Sol Ring" in response.data
    assert b"Tymna the Weaver" in response.data
    assert b"EDHREC" in response.data
    assert b"Advanced filters" in response.data


def test_search_html_random_sample(client):
    response = client.get("/search", query_string={"sort": "random"})
    assert response.status_code == 200
    assert b"random sample" in response.data
    assert b"Showing a random sample" in response.data


def test_mcp_search_cards_uses_canonical_builder(search_db_path, monkeypatch):
    from mtg import mcp_server

    monkeypatch.setattr(
        mcp_server, "db_path", lambda *args, **kwargs: str(search_db_path)
    )
    cards = mcp_server.search_cards(name="Sol", sort="edhrec", limit=10)
    assert [card["name"] for card in cards] == ["Sol Ring"]
    assert cards[0]["types"] == "Artifact"
    assert cards[0]["manaValue"] == 1


def test_api_search_random_sort_with_no_filters(client):
    data = client.get("/api/search", query_string={"sort": "random"}).get_json()
    assert data["random_sample"] is True
    assert data["total"] > 0
    assert len(data["cards"]) > 0


def test_card_detail_contains_role_chips(client):
    response = client.get("/card/LTC/1")
    assert response.status_code == 200
    assert b"Ramp" in response.data
    assert b"Game Changer" not in response.data  # Sol Ring fixture is not a GC
