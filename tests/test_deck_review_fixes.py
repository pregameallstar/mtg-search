"""Regressions for Deck Doctor correctness, staleness, and apply safety."""

from __future__ import annotations

import json
import sqlite3
import time

import pytest

import app as app_module
import mtg.deck_review as deck_review
from mtg import shared
from mtg.blueprints import deck_review as review_bp
from mtg.blueprints import decks as decks_bp
from mtg.deck_enrich import enrich_deck
from mtg.deck_stats import analyze_deck, deck_fingerprint
from mtg.storage import write_json_atomic
from tests.fixtures import build_search_db


@pytest.fixture()
def search_db_path(tmp_path):
    return build_search_db(tmp_path / "search.sqlite")


@pytest.fixture()
def client(search_db_path, tmp_path, monkeypatch):
    monkeypatch.setattr(shared, "db_path", lambda *args, **kwargs: str(search_db_path))
    monkeypatch.setattr(deck_review, "db_path", lambda *args, **kwargs: str(search_db_path))
    monkeypatch.setattr(deck_review.embed, "find", lambda *args, **kwargs: [])
    monkeypatch.setattr(review_bp, "REVIEWS_PATH", str(tmp_path / "reviews"))
    monkeypatch.setattr(review_bp, "DECKS_DIR", str(tmp_path / "decks"))
    (tmp_path / "decks").mkdir()
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


def _deck(name="Review Test"):
    return {
        "name": name,
        "commander": {"name": "Alela, Artful Provocateur"},
        "cards": [
            {"name": "Sol Ring", "quantity": 1},
            {"name": "Wrath of God", "quantity": 1},
            {"name": "Rhystic Study", "quantity": 1},
        ],
        "sections": {},
        "guideline": "",
    }


def _wait(client, job_id, timeout=4.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.get(f"/api/deck/review/progress?jobId={job_id}").get_json()
        if data["status"] in ("done", "error"):
            return data
        time.sleep(0.03)
    raise AssertionError("review job did not finish")


def _run(client, deck=None, name=None, brief=None, options=None):
    body = {"options": {"use_llm": False, **(options or {})}}
    if deck is not None:
        body["deck"] = deck
    if name is not None:
        body["name"] = name
    if brief is not None:
        body["brief"] = brief
    response = client.post("/api/deck/review/run", json=body)
    assert response.status_code == 202, response.get_json()
    job_id = response.get_json()["jobId"]
    _wait(client, job_id)
    return job_id


def _save(client, job_id, name=None):
    body = {"jobId": job_id}
    if name is not None:
        body["name"] = name
    response = client.post("/api/deck/review/save", json=body)
    assert response.status_code == 200, response.get_json()
    return response.get_json()["filename"]


# --------------------------------------------------------------------------- #
# Fingerprint completeness
# --------------------------------------------------------------------------- #

def test_fingerprint_changes_with_tags_eval_data_and_engine_inputs():
    deck = {
        "commander": {"name": "Cmdr"},
        "cards": [{"name": "Sol Ring", "quantity": 1}],
        "sections": {},
        "guideline": "",
        "evalData": {"strategies": [{"name": "Artifacts", "description": "x"}]},
    }
    base = deck_fingerprint(deck)
    assert base == deck_fingerprint(deck)

    tagged = json.loads(json.dumps(deck))
    tagged["cards"][0]["tags"] = ["draw"]
    assert deck_fingerprint(tagged) != base

    evaluated = json.loads(json.dumps(deck))
    evaluated["evalData"]["strategies"][0]["name"] = "Enchantments"
    assert deck_fingerprint(evaluated) != base


def test_saved_review_goes_stale_when_tags_or_eval_data_change(client, tmp_path):
    deck = _deck()
    write_json_atomic(tmp_path / "decks" / "Review_Test.json", deck)
    job_id = _run(client, name="Review Test")
    filename = _save(client, job_id, "Review Test")
    assert client.get(f"/api/deck/review/report?filename={filename}").get_json()["stale"] is False

    changed = json.loads((tmp_path / "decks" / "Review_Test.json").read_text())
    changed["cards"][0]["tags"] = ["draw"]
    write_json_atomic(tmp_path / "decks" / "Review_Test.json", changed)
    stale = client.get(f"/api/deck/review/report?filename={filename}").get_json()
    assert stale["stale"] is True

    changed["evalData"] = {"strategies": [{"name": "Different", "description": "x"}]}
    write_json_atomic(tmp_path / "decks" / "Review_Test.json", changed)
    assert client.get(f"/api/deck/review/report?filename={filename}").get_json()["stale"] is True


def test_job_result_is_stale_when_deck_changes_mid_run(client, tmp_path, monkeypatch):
    deck = _deck("Slow Deck")
    write_json_atomic(tmp_path / "decks" / "Slow_Deck.json", deck)
    original = deck_review.embed.find

    def slow_find(*args, **kwargs):
        time.sleep(0.2)
        return original(*args, **kwargs)

    monkeypatch.setattr(deck_review.embed, "find", slow_find)
    response = client.post("/api/deck/review/run", json={
        "name": "Slow Deck", "options": {"use_llm": False},
    })
    job_id = response.get_json()["jobId"]
    time.sleep(0.1)

    changed = json.loads((tmp_path / "decks" / "Slow_Deck.json").read_text())
    changed["cards"].append({"name": "Cultivate", "quantity": 1})
    write_json_atomic(tmp_path / "decks" / "Slow_Deck.json", changed)
    _wait(client, job_id, timeout=10.0)

    result = client.get(f"/api/deck/review/result?jobId={job_id}").get_json()
    assert result["stale"] is True
    assert "changed" in result["staleReason"].lower()


# --------------------------------------------------------------------------- #
# Saving and applying
# --------------------------------------------------------------------------- #

def test_review_cannot_be_saved_under_a_different_deck(client, tmp_path):
    write_json_atomic(tmp_path / "decks" / "Review_Test.json", _deck())
    job_id = _run(client, name="Review Test")
    response = client.post("/api/deck/review/save", json={
        "jobId": job_id, "name": "Some Other Deck",
    })
    assert response.status_code == 409
    assert "Review Test" in response.get_json()["error"]


def test_two_saves_do_not_overwrite_each_other(client, tmp_path):
    write_json_atomic(tmp_path / "decks" / "Review_Test.json", _deck())
    job_id = _run(client, name="Review Test")
    first = _save(client, job_id, "Review Test")
    second = _save(client, job_id, "Review Test")
    assert first != second
    assert (tmp_path / "reviews" / first).exists()
    assert (tmp_path / "reviews" / second).exists()


def test_deck_save_preserves_review_notes_from_deck_doctor(client, tmp_path, monkeypatch):
    monkeypatch.setattr(decks_bp, "DECKS_DIR", str(tmp_path / "decks"))
    write_json_atomic(tmp_path / "decks" / "Review_Test.json", _deck())
    job_id = _run(client, name="Review Test", brief="Squirrel tribal.")
    assert json.loads((tmp_path / "decks" / "Review_Test.json").read_text())["reviewNotes"] == "Squirrel tribal."

    # The Deck Builder sends a whole deck object with its stale empty brief.
    stale_builder_payload = _deck()
    stale_builder_payload["reviewNotes"] = ""
    response = client.post("/api/deck/save", json=stale_builder_payload)
    assert response.status_code == 200, response.get_json()
    saved = json.loads((tmp_path / "decks" / "Review_Test.json").read_text())
    assert saved["reviewNotes"] == "Squirrel tribal."
    assert saved.get("reviewNotesUpdatedAt")


def test_brief_is_not_copied_from_outline(client, tmp_path):
    deck = _deck()
    deck["outline"] = [{"name": "Strategy", "text": "Aristocrats.", "cards": []}]
    write_json_atomic(tmp_path / "decks" / "Review_Test.json", deck)

    page = client.get("/deck-review?deck=Review%20Test").get_data(as_text=True)
    assert 'data-brief=""' in page

    _run(client, name="Review Test", brief="")
    saved = json.loads((tmp_path / "decks" / "Review_Test.json").read_text())
    assert "reviewNotes" not in saved


# --------------------------------------------------------------------------- #
# Brief constraints and candidates
# --------------------------------------------------------------------------- #

def test_brief_caps_parser_is_conservative():
    assert deck_review.brief_role_caps(
        "Rule 0 constraints: 2 counterspells, 2 boardwipe"
    ) == {"Counterspell": 2, "Board Wipe": 2}
    assert deck_review.brief_role_caps("No more than 1 tutor and no land denial") == {
        "Tutor": 1,
    }
    # A descriptive sentence is not a cap.
    assert deck_review.brief_role_caps("The deck has 2 counterspells.") == {}


def test_brief_caps_lower_targets_and_health():
    commander = {"name": "Cmdr", "colorIdentity": "W"}
    cards = [
        {"name": "Wrath of God", "types": "Sorcery", "type": "Sorcery",
         "text": "Destroy all creatures.", "manaValue": 4, "colorIdentity": "W"},
        {"name": "Counterspell", "types": "Instant", "type": "Instant",
         "text": "Counter target spell.", "manaValue": 2, "colorIdentity": "U"},
    ]
    deck = {"commander": commander, "cards": cards, "sections": {}, "guideline": ""}
    plain = analyze_deck(deck)
    capped = analyze_deck(deck, brief_caps={"Board Wipe": 1, "Counterspell": 1})
    by_role = {gap["role"]: gap for gap in capped["roleGaps"]}
    assert by_role["Board Wipe"]["target"] == 1
    assert by_role["Counterspell"]["target"] == 1
    assert capped["briefConstraints"] == {"Board Wipe": 1, "Counterspell": 1}
    assert capped["health"]["score"] >= plain["health"]["score"]


def test_land_deficit_retrieves_land_candidates(search_db_path, monkeypatch):
    conn = sqlite3.connect(search_db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("""
            INSERT INTO cards (
                uuid, name, text, type, types, supertypes, manaCost, manaValue,
                colors, colorIdentity, setCode, number, language, side,
                edhrecRank, isGameChanger
            ) VALUES (
                'land-1', 'Plains', '({T}: Add {W}.)', 'Basic Land — Plains',
                'Land', 'Basic', '', 0, '', '', 'TST', '999', 'English', NULL,
                10, 0
            )
        """)
        conn.execute(
            "INSERT INTO cardIdentifiers (uuid, scryfallId) VALUES ('land-1', 'sf-land')"
        )
        conn.execute(
            "INSERT INTO cardLegalities (uuid, commander) VALUES ('land-1', 'Legal')"
        )
        conn.commit()

        deck = {
            "name": "Land Test",
            "commander": {"name": "Alela, Artful Provocateur"},
            "cards": [{"name": "Sol Ring", "quantity": 1}],
            "sections": {},
            "guideline": "",
        }
        enriched = enrich_deck(conn, deck)
        audit = analyze_deck(enriched)
        monkeypatch.setattr(deck_review.embed, "find", lambda *a, **k: [
            {"uuid": "land-1", "name": "Plains", "score": 0.9}
        ])
        candidates = deck_review.candidate_adds(conn, enriched, audit, {})
        assert [card["name"] for card in candidates["Lands"]] == ["Plains"]
    finally:
        conn.close()


# --------------------------------------------------------------------------- #
# LLM verification and retry behavior
# --------------------------------------------------------------------------- #

def test_verify_llm_receives_audit_and_candidate_details(monkeypatch):
    captured = {}

    def fake_generate(system, user, **kwargs):
        captured["user"] = user
        return {"warnings": [], "verified": True}

    monkeypatch.setattr(deck_review, "llm_generate", fake_generate)
    deck = {"commander": {"name": "Cmdr", "colorIdentity": "G"}, "cards": []}
    candidates = {"Ramp": [{
        "name": "Cultivate",
        "colorIdentity": "G",
        "manaValue": 3,
        "type": "Sorcery",
        "roles": ["Ramp"],
    }]}
    deck_review._verify_llm(
        deck,
        {"adds": [], "cuts": []},
        candidates,
        {"llm": {"api_key": "test"}},
        audit={"counts": {"total": 100}, "roleGaps": []},
    )
    payload = json.loads(captured["user"])
    assert payload["audit"]["counts"]["total"] == 100
    assert payload["candidate_details"][0]["colorIdentity"] == "G"


def test_run_llm_does_not_retry_network_errors(monkeypatch):
    calls = []

    def failing_generate(*args, **kwargs):
        calls.append(1)
        raise RuntimeError("connection reset")

    monkeypatch.setattr(deck_review, "llm_generate", failing_generate)
    data, error = deck_review._run_llm(
        {"commander": {}, "cards": []},
        {"counts": {}, "curve": {}, "roles": {}, "roleGaps": [],
         "legality": {}, "bracket": {}, "health": {}},
        {},
        [],
        {"llm": {"api_key": "test"}},
    )
    assert data is None
    assert "connection reset" in error
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# Input robustness and exports
# --------------------------------------------------------------------------- #

def test_limits_honor_zero_and_reject_bad_values():
    assert deck_review._limit(0) == 0
    assert deck_review._limit(None) == 10
    assert deck_review._limit("bad") == 10
    assert deck_review._limit(99) == 20


def test_deck_review_page_survives_malformed_quantity(client, tmp_path):
    bad = _deck("Bad Deck")
    bad["cards"] = [{"name": "Sol Ring", "quantity": [1]}]
    write_json_atomic(tmp_path / "decks" / "Bad_Deck.json", bad)
    assert client.get("/deck-review").status_code == 200


def test_exports_include_audit_legality_warnings_and_replaces():
    report = {
        "deck": "Export Test",
        "audit": {
            "counts": {"total": 99, "main": 98, "commander": 1},
            "curve": {"averageMv": 3.1, "landCount": 30, "rampCount": 8, "buckets": {"0": 1}},
            "legality": {
                "legal": False,
                "errors": [{"code": "wrong_count", "message": "Deck must contain exactly 100 cards."}],
                "warnings": [{"code": "partner", "message": "Partner is not fully modeled."}],
            },
            "health": {"score": 61, "components": {"roles": 20, "legality": 8}},
            "roleGaps": [{"role": "Lands", "actual": 30, "target": 37, "deficit": 7, "surplus": 0}],
            "briefConstraints": {"Board Wipe": 1},
            "bracket": {"estimate": 3, "signals": [{"label": "Tutors", "count": 2, "cards": ["Mystical Tutor"]}]},
            "warnings": [{"code": "no_explicit_wincon", "message": "No explicit win condition."}],
        },
        "analysis": {
            "verdict": "Needs work",
            "summary": "Fix the mana base.",
            "adds": [{"name": "Cultivate", "reason": "land ramp", "role": "Ramp", "replaces": "Explosive Vegetation"}],
            "cuts": [],
        },
        "candidates": {"adds": {}, "cuts": [{"name": "Old Card"}]},
        "validation": {"warnings": ["Replacement ignored."], "rejected": []},
        "verification": {"warnings": ["Check the mana base."], "verified": True},
        "pipeline": {"llm_error": "timeout"},
        "options": {"brief": "Rule 0: max 1 board wipe"},
    }
    markdown = deck_review.review_to_markdown(report)
    assert "Deck must contain exactly 100 cards." in markdown
    assert "Partner is not fully modeled." in markdown
    assert "max 1" in markdown
    assert "replaces **Explosive Vegetation**" in markdown
    assert "None recommended." in markdown  # explicit empty LLM cuts
    assert "timeout" in markdown

    html = deck_review.review_to_html(report)
    assert "Deck must contain exactly 100 cards." in html
    assert "<strong>replaces" not in html
    assert "<strong>Explosive Vegetation</strong>" in html
    assert "**" not in html.split("<body>", 1)[1]
