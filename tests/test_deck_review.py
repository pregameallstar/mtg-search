"""Deck Doctor orchestration tests (no live LLM)."""

from __future__ import annotations

import sqlite3

import pytest

import mtg.deck_review as review
from mtg.deck_enrich import enrich_deck
from mtg.deck_stats import analyze_deck
from tests.fixtures import build_search_db


@pytest.fixture()
def db(tmp_path):
    path = build_search_db(tmp_path / "search.sqlite")
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _deck():
    return {
        "name": "Review",
        "commander": {"name": "Alela, Artful Provocateur"},
        "cards": [
            {"name": "Sol Ring", "quantity": 1},
            {"name": "Wrath of God", "quantity": 1},
        ],
        "sections": {},
        "guideline": "",
    }


def _uuid(db, name):
    return db.execute("SELECT uuid FROM cards WHERE name = ?", (name,)).fetchone()[0]


def _audit(db, deck):
    enriched = enrich_deck(db, deck)
    return enriched, analyze_deck(enriched)


def test_candidate_adds_groups_real_cards(db, monkeypatch):
    enriched, audit = _audit(db, _deck())
    rhystic = _uuid(db, "Rhystic Study")

    def fake_find(path, text, top_k=200):
        return [{"uuid": rhystic, "name": "Rhystic Study", "score": 0.9}]

    monkeypatch.setattr(review.embed, "find", fake_find)
    candidates = review.candidate_adds(db, enriched, audit, {
        "target_bracket": None, "currency": "usd",
    })
    names = {card["name"] for bucket in candidates.values() for card in bucket}
    assert "Rhystic Study" in names
    assert all(card["priceUnknown"] for bucket in candidates.values() for card in bucket)


def test_candidate_adds_excludes_cards_already_in_deck(db, monkeypatch):
    enriched, audit = _audit(db, _deck())
    sol = _uuid(db, "Sol Ring")

    monkeypatch.setattr(review.embed, "find", lambda *a, **k: [
        {"uuid": sol, "name": "Sol Ring", "score": 0.99}
    ])
    candidates = review.candidate_adds(db, enriched, audit, {})
    assert all(card["name"] != "Sol Ring" for bucket in candidates.values() for card in bucket)


def test_cut_candidates_excludes_commander_and_basic_lands(db):
    enriched, audit = _audit(db, _deck())
    cuts = review.cut_candidates(enriched, audit)
    assert all(cut["name"] != "Alela, Artful Provocateur" for cut in cuts)
    assert all(cut["name"] != "Plains" for cut in cuts)


def test_validate_analysis_rejects_invalid_add_and_cut():
    deck = _deck()
    candidates = {"Card Draw": [{"name": "Rhystic Study"}]}
    analysis = {
        "adds": [
            {"name": "Rhystic Study", "reason": "draw", "role": "Card Draw"},
            {"name": "Imaginary Card", "reason": "bad", "role": "Draw"},
            {"name": "Sol Ring", "reason": "already in deck", "role": "Ramp"},
        ],
        "cuts": [
            {"name": "Wrath of God", "reason": "curve", "role": "Board Wipe"},
            {"name": "Not In Deck", "reason": "bad", "role": "Core"},
            {"name": "Alela, Artful Provocateur", "reason": "bad", "role": "Commander"},
        ],
    }
    result, validation = review.validate_analysis(
        analysis, deck, candidates, max_adds=10, max_cuts=10
    )
    assert [item["name"] for item in result["adds"]] == ["Rhystic Study"]
    assert [item["name"] for item in result["cuts"]] == ["Wrath of God"]
    rejected = {(item["type"], item["name"]) for item in validation["rejected"]}
    assert ("add", "Imaginary Card") in rejected
    assert ("add", "Sol Ring") in rejected
    assert ("cut", "Not In Deck") in rejected
    assert ("cut", "Alela, Artful Provocateur") in rejected


def test_validate_analysis_ignores_unknown_replacement():
    deck = _deck()
    candidates = {"Card Draw": [{"name": "Rhystic Study"}]}
    result, validation = review.validate_analysis(
        {"adds": [{"name": "Rhystic Study", "reason": "draw", "replaces": "Nope"}], "cuts": []},
        deck, candidates,
    )
    assert result["adds"][0]["replaces"] is None
    assert validation["warnings"]


def test_run_llm_returns_parsed_report(monkeypatch):
    captured = {}

    def fake_generate(system, user, **kwargs):
        captured["system"] = system
        captured["user"] = user
        return {"verdict": "ok", "summary": "fine", "adds": [], "cuts": []}

    monkeypatch.setattr(review, "llm_generate", fake_generate)
    data, err = review._run_llm(
        _deck(), {"counts": {}, "curve": {}, "roles": {}, "roleGaps": [],
                  "legality": {}, "bracket": {}, "health": {}},
        {"Card Draw": [{"name": "Rhystic Study"}]}, [],
        {"llm": {"api_key": "test"}, "target_bracket": 3},
    )
    assert err is None
    assert data["verdict"] == "ok"
    assert "Rhystic Study" in captured["user"]
    assert "candidate_adds" in captured["user"]


def test_run_llm_without_key_returns_error():
    data, err = review._run_llm(_deck(), {}, {}, [], {"llm": {"api_key": ""}})
    assert data is None
    assert "API key" in err


def test_verify_llm_returns_warnings(monkeypatch):
    monkeypatch.setattr(review, "llm_generate", lambda *a, **k: {
        "warnings": ["Imaginary Card is not in the candidate list."],
        "verified": True,
    })
    result, err = review._verify_llm(
        {"commander": {"name": "Cmdr", "colorIdentity": "W"},
         "cards": [{"name": "Sol Ring"}]},
        {"adds": [{"name": "Imaginary Card"}]},
        {"Ramp": [{"name": "Sol Ring"}]},
        {"llm": {"api_key": "test"}},
    )
    assert err is None
    assert result["warnings"]
    assert "Imaginary Card" in result["warnings"][0]


def test_build_user_prompt_includes_deck_brief():
    prompt = review._build_user_prompt(
        _deck(),
        {"counts": {}, "curve": {}, "roles": {}, "roleGaps": [],
         "legality": {}, "bracket": {}, "health": {}},
        {}, [],
        {"brief": "Squirrel tribal. Rule 0: max 2 board wipes.", "target_bracket": 3},
    )
    assert '"deck_brief": "Squirrel tribal. Rule 0: max 2 board wipes."' in prompt


def test_fingerprint_options_include_brief():
    a = review.fingerprint_options({"brief": "A"})
    b = review.fingerprint_options({"brief": "B"})
    assert a["brief"] != b["brief"]
