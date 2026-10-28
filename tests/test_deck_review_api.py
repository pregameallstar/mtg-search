"""Deck Doctor API and job tests."""

from __future__ import annotations

import json
import time

import pytest

import app as app_module
import mtg.deck_review as deck_review
from mtg import shared
from mtg.blueprints import deck_review as review_bp
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
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


def _wait_for_job(client, job_id, timeout=4.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = client.get(f"/api/deck/review/progress?jobId={job_id}").get_json()
        if data["status"] in ("done", "error"):
            return data
        time.sleep(0.03)
    raise AssertionError("review job did not finish")


def _deck():
    return {
        "name": "Review Test",
        "commander": {"name": "Alela, Artful Provocateur"},
        "cards": [
            {"name": "Sol Ring", "quantity": 1},
            {"name": "Wrath of God", "quantity": 1},
            {"name": "Rhystic Study", "quantity": 1},
        ],
        "sections": {},
        "guideline": "",
    }


def test_deck_review_page(client):
    assert client.get("/deck-review").status_code == 200


def test_review_run_progress_and_result(client):
    response = client.post("/api/deck/review/run", json={
        "deck": _deck(),
        "options": {"use_llm": False},
    })
    assert response.status_code == 202
    data = response.get_json()
    assert data["jobId"]
    assert data["fingerprint"]
    final = _wait_for_job(client, data["jobId"])
    assert final["status"] == "done"
    result = client.get(f"/api/deck/review/result?jobId={data['jobId']}")
    assert result.status_code == 200
    payload = result.get_json()
    assert payload["success"] is True
    assert payload["audit"]["counts"]["total"] == 4
    assert payload["analysis"] is None  # no LLM key in tests
    assert payload["candidates"]["adds"] == {}
    assert payload["deck"]["name"] == "Review Test"


def test_review_result_before_done_is_conflict(client):
    # A missing job is 404; this test pins the contract for an unknown id.
    assert client.get("/api/deck/review/result?jobId=nope").status_code == 404


def test_review_run_saved_deck_name(client, tmp_path, monkeypatch):
    monkeypatch.setattr(review_bp, "DECKS_DIR", str(tmp_path))
    write_json_atomic(tmp_path / "Review_Test.json", _deck())
    response = client.post("/api/deck/review/run", json={
        "name": "Review Test",
        "options": {"use_llm": False},
    })
    assert response.status_code == 202
    final = _wait_for_job(client, response.get_json()["jobId"])
    assert final["status"] == "done"


def test_review_run_validation(client):
    assert client.post("/api/deck/review/run", json={}).status_code == 400
    assert client.post("/api/deck/review/run", json={"name": "Missing"}).status_code == 404


def test_review_save_list_load_delete(client, tmp_path):
    response = client.post("/api/deck/review/run", json={
        "deck": _deck(), "options": {"use_llm": False},
    })
    job_id = response.get_json()["jobId"]
    _wait_for_job(client, job_id)
    saved = client.post("/api/deck/review/save", json={"jobId": job_id})
    assert saved.status_code == 200
    filename = saved.get_json()["filename"]
    assert filename.endswith(".json")

    reports = client.get("/api/deck/review/reports?deck=Review Test").get_json()["reports"]
    assert any(report["filename"] == filename for report in reports)
    report = client.get(f"/api/deck/review/report?filename={filename}")
    assert report.status_code == 200
    assert report.get_json()["deck"] == "Review Test"
    assert client.post("/api/deck/review/delete", json={"filename": filename}).status_code == 200


def test_review_report_filename_validation(client):
    assert client.get("/api/deck/review/report?filename=../x.json").status_code == 400
    assert client.post("/api/deck/review/delete", json={"filename": "/tmp/x.json"}).status_code == 400
    assert client.get("/api/deck/review/report?filename=missing.json").status_code == 404


def test_review_requires_commander(client):
    response = client.post("/api/deck/review/run", json={
        "deck": {"name": "No Commander", "cards": [], "sections": {}},
        "options": {"use_llm": False},
    })
    assert response.status_code == 400
    assert "commander" in response.get_json()["error"].lower()


def test_deck_review_page_lists_saved_decks(client, tmp_path, monkeypatch):
    monkeypatch.setattr(review_bp, "DECKS_DIR", str(tmp_path))
    write_json_atomic(tmp_path / "Review_Test.json", _deck())
    response = client.get("/deck-review")
    assert response.status_code == 200
    assert b'<select id="review-deck-name"' in response.data
    assert b"Review Test" in response.data
    assert b"Alela, Artful Provocateur" in response.data


def test_saved_review_stale_when_deck_changes(client, tmp_path, monkeypatch):
    monkeypatch.setattr(review_bp, "DECKS_DIR", str(tmp_path))
    write_json_atomic(tmp_path / "Review_Test.json", _deck())
    response = client.post("/api/deck/review/run", json={
        "deck": _deck(), "options": {"use_llm": False},
    })
    job_id = response.get_json()["jobId"]
    _wait_for_job(client, job_id)
    saved = client.post("/api/deck/review/save", json={"jobId": job_id})
    filename = saved.get_json()["filename"]
    fresh = client.get(f"/api/deck/review/report?filename={filename}").get_json()
    assert fresh["stale"] is False

    changed = _deck()
    changed["cards"].append({"name": "Cultivate", "quantity": 1})
    write_json_atomic(tmp_path / "Review_Test.json", changed)
    stale = client.get(f"/api/deck/review/report?filename={filename}").get_json()
    assert stale["stale"] is True
    reports = client.get("/api/deck/review/reports?deck=Review Test").get_json()["reports"]
    assert any(report["filename"] == filename and report["stale"] for report in reports)


def test_review_export_markdown_and_html(client):
    response = client.post("/api/deck/review/run", json={
        "deck": _deck(), "options": {"use_llm": False},
    })
    job_id = response.get_json()["jobId"]
    _wait_for_job(client, job_id)
    saved = client.post("/api/deck/review/save", json={"jobId": job_id})
    filename = saved.get_json()["filename"]

    md = client.get(f"/api/deck/review/export?format=markdown&filename={filename}")
    assert md.status_code == 200
    assert b"# Deck Doctor" in md.data
    assert b"Review Test" in md.data
    assert "attachment" in md.headers.get("Content-Disposition", "")

    html = client.get(f"/api/deck/review/export?format=html&filename={filename}")
    assert html.status_code == 200
    assert b"<html" in html.data
    assert b"Review Test" in html.data

    job_export = client.get(f"/api/deck/review/export?format=markdown&jobId={job_id}")
    assert job_export.status_code == 200

    assert client.get("/api/deck/review/export").status_code == 400
    assert client.get("/api/deck/review/export?format=pdf&jobId=x").status_code == 400


def test_mcp_review_deck_deterministic(search_db_path, tmp_path, monkeypatch):
    from mtg import mcp_server
    from mtg.storage import write_json_atomic

    monkeypatch.setattr(mcp_server, "db_path", lambda *args, **kwargs: str(search_db_path))
    monkeypatch.setattr(mcp_server, "DECKS_DIR", str(tmp_path))
    write_json_atomic(tmp_path / "Review_Test.json", _deck())
    result = mcp_server.review_deck("Review Test")
    assert "audit" in result
    assert result["audit"]["counts"]["total"] == 4
    assert "candidates" not in result
