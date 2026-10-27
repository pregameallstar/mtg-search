"""Regression tests for the high-impact fixes.

Covers storage collisions/reserved names, server-side prompt storage, atomic
database install, printing-code splitting, image-ID validation, and MCP deck
quantity counts.
"""

import json

import pytest

import app as app_module
from mtg import prompt_store
from mtg.blueprints import decks as decks_module
from mtg.ingest import _install_database
from mtg.storage import StorageError, read_named_json, resolve_json_path, write_json_atomic


def _client():
    app_module.app.config["TESTING"] = True
    return app_module.app.test_client()


# --------------------------------------------------------------------------- #
# Storage / filename collisions
# --------------------------------------------------------------------------- #

def test_reserved_and_colliding_names_are_rejected(tmp_path):
    path = resolve_json_path(str(tmp_path), "Alela, Artful Provocateur")
    write_json_atomic(path, {"name": "Alela, Artful Provocateur"})

    # Same display name is allowed.
    assert read_named_json(path, "Alela, Artful Provocateur")["name"] == \
        "Alela, Artful Provocateur"

    # A different display name that sanitizes to the same file is a 409.
    with pytest.raises(StorageError) as exc:
        read_named_json(path, "Alela Artful Provocateur")
    assert exc.value.status_code == 409

    # Reserved tag catalog filename and an effectively empty name are 400.
    for name in ("_tags", "///"):
        with pytest.raises(StorageError) as exc:
            resolve_json_path(str(tmp_path), name, reserved=("_tags.json",))
        assert exc.value.status_code == 400


def test_deck_save_rejects_tag_catalog_name(tmp_path, monkeypatch):
    monkeypatch.setattr(decks_module, "DECKS_DIR", str(tmp_path))
    c = _client()
    rv = c.post("/api/deck/save", json={"name": "_tags", "cards": []})
    assert rv.status_code == 400


def test_deck_add_card_enforces_singleton(tmp_path, monkeypatch):
    monkeypatch.setattr(decks_module, "DECKS_DIR", str(tmp_path))
    c = _client()
    assert c.post("/api/deck/save", json={
        "name": "Singleton Test", "cards": [], "sections": {},
    }).status_code == 200

    body = {"deck": "Singleton Test", "setCode": "SLZ", "number": "184"}
    first = c.post("/api/deck/add-card", json=body)
    second = c.post("/api/deck/add-card", json=body)
    assert first.status_code == 200
    assert second.status_code == 409
    assert "Only one copy" in second.get_json()["error"]


def test_deck_save_collision_and_rename(tmp_path, monkeypatch):
    monkeypatch.setattr(decks_module, "DECKS_DIR", str(tmp_path))
    c = _client()

    assert c.post("/api/deck/save", json={
        "name": "Deck A", "cards": [],
    }).status_code == 200

    # Saving under a different sanitized name is a save-as copy: the old deck
    # is intentionally left alone (the client can delete it explicitly).
    rv = c.post("/api/deck/save", json={
        "name": "Deck B", "previousName": "Deck A", "cards": [],
    })
    assert rv.status_code == 200
    assert (tmp_path / "Deck_A.json").exists()
    assert (tmp_path / "Deck_B.json").exists()

    # A punctuation-only rename maps to the same file. It is rejected without
    # previousName (looks like a collision) and allowed with it.
    assert c.post("/api/deck/save", json={
        "name": "Alela, Artful Provocateur", "cards": [],
    }).status_code == 200
    rv = c.post("/api/deck/save", json={
        "name": "Alela Artful Provocateur", "cards": [],
    })
    assert rv.status_code == 409
    rv = c.post("/api/deck/save", json={
        "name": "Alela Artful Provocateur",
        "previousName": "Alela, Artful Provocateur",
        "cards": [],
    })
    assert rv.status_code == 200


# --------------------------------------------------------------------------- #
# Prompt storage / API-key preservation
# --------------------------------------------------------------------------- #

def test_config_prompt_uses_server_store_and_keeps_key(tmp_path, monkeypatch):
    monkeypatch.setattr(prompt_store, "PROMPTS_DIR", str(tmp_path))
    c = _client()
    c.get("/config/")
    with c.session_transaction() as s:
        token = s["_csrf_token"]

    rv = c.post("/config/", data={
        "csrf_token": token,
        "save_llm": "1",
        "llm_api_key": "sk-test",
        "llm_backend": "openai",
        "llm_base_url": "http://127.0.0.1:9/v1",
        "llm_model": "m",
    })
    assert rv.status_code == 302

    # A prompt save must not put the 10 KB prompt in the cookie or wipe the key.
    rv = c.post("/config/", data={
        "csrf_token": token,
        "save_prompt": "1",
        "eval_prompt": "CUSTOM PROMPT",
        "llm_backend": "openai",
        "llm_base_url": "http://127.0.0.1:9/v1",
        "llm_model": "m",
    })
    cookie = rv.headers.get("Set-Cookie", "")
    assert len(cookie) < 4096
    with c.session_transaction() as s:
        assert s.get("llm_api_key") == "sk-test"
        assert "eval_prompt" not in s
        assert s.get("prompt_store_id")
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    assert json.loads(files[0].read_text())["eval_prompt"] == "CUSTOM PROMPT"

    # A different sub-form must leave the key and prompt alone.
    c.post("/config/", data={
        "csrf_token": token,
        "save_history": "1",
        "history_warning_days": "45",
        "llm_backend": "openai",
        "llm_base_url": "http://127.0.0.1:9/v1",
        "llm_model": "m",
    })
    with c.session_transaction() as s:
        assert s.get("llm_api_key") == "sk-test"
        assert s.get("history_warning_days") == "45"
    assert json.loads(files[0].read_text())["eval_prompt"] == "CUSTOM PROMPT"


def test_config_post_requires_csrf_token():
    c = _client()
    rv = c.post("/config/", data={"save_history": "1", "history_warning_days": "45"})
    assert rv.status_code == 403


# --------------------------------------------------------------------------- #
# Atomic database install
# --------------------------------------------------------------------------- #

def test_install_database_replaces_and_backs_up(tmp_path):
    db = tmp_path / "AllPrintings.sqlite"
    db.write_bytes(b"old-db")
    src = tmp_path / "new.sqlite"
    src.write_bytes(b"new-db")

    _install_database(str(src), str(db))

    assert db.read_bytes() == b"new-db"
    assert (tmp_path / "AllPrintings.sqlite.bak").read_bytes() == b"old-db"


def test_install_database_handles_bind_mount_directory(tmp_path):
    src = tmp_path / "new.sqlite"
    src.write_bytes(b"new-db")
    (tmp_path / "stale.sqlite").write_bytes(b"old-db")
    images = tmp_path / "images" / "normal"
    images.mkdir(parents=True)
    cached_image = images / "cached.jpg"
    cached_image.write_bytes(b"cached-image")

    _install_database(str(src), str(tmp_path))

    assert (tmp_path / "AllPrintings.sqlite").read_bytes() == b"new-db"
    assert not (tmp_path / "stale.sqlite").exists()
    assert (tmp_path / "AllPrintings.sqlite.bak").read_bytes() == b"old-db"
    # Re-ingest must not clear the image cache.
    assert cached_image.read_bytes() == b"cached-image"


# --------------------------------------------------------------------------- #
# Printing list / image IDs
# --------------------------------------------------------------------------- #

def test_single_printing_is_the_only_mode():
    c = _client()
    # The old UI offered "All Printings" even though the DB keeps one latest
    # printing. It must not promise or render that option.
    search = c.get("/search", query_string={
        "name": "Lightning Bolt", "unique": "prints",
    })
    assert search.status_code == 200
    assert b"All Printings" not in search.data
    assert b"1 card found" in search.data

    detail = c.get("/card/SLZ/184")
    assert detail.status_code == 200
    assert b"All Printings" not in detail.data


def test_similarity_defaults_to_embed_with_honest_controls():
    c = _client()
    rv = c.get("/card/SLZ/184/similar", follow_redirects=False)
    assert rv.status_code == 302
    location = rv.headers["Location"]
    assert "use_types=1" in location
    assert "use_keywords" not in location
    assert "mtg_filter" not in location


def test_textless_card_falls_back_to_attribute_similarity():
    c = _client()
    # Aegis Turtle has no oracle text, so the embedding signal is empty.
    rv = c.get("/card/FDN/150/similar", follow_redirects=True)
    assert rv.status_code == 200
    assert b"No cards matched" not in rv.data
    assert b"card-cell" in rv.data


def test_embed_index_rejects_mismatched_lengths(tmp_path, monkeypatch):
    import numpy as np
    from mtg import embed

    monkeypatch.setattr(embed, "INDEX_DIR", str(tmp_path))
    np.save(tmp_path / "vectors.npy", np.array([[1.0, 0.0], [0.0, 1.0]]))
    (tmp_path / "meta.json").write_text(json.dumps([{"uuid": "only-one"}]))
    vectors, meta = embed._load_index()
    assert vectors is None
    assert meta is None


def test_image_proxy_rejects_short_or_traversing_ids():
    c = _client()
    assert c.get("/img/normal/front/a/b/a.jpg").status_code == 404
    assert c.get(
        "/img/normal/../0/0/00000000-0000-0000-0000-000000000000.jpg"
    ).status_code == 404


# --------------------------------------------------------------------------- #
# MCP deck counts
# --------------------------------------------------------------------------- #

def test_mcp_list_decks_counts_quantities(tmp_path, monkeypatch):
    from mtg import mcp_server

    deck = {
        "name": "Quantity Test",
        "commander": {"name": "Commander", "colorIdentity": "W"},
        "cards": [
            {"name": "A", "quantity": 2},
            {"name": "B", "quantity": 3},
        ],
        "sections": {"Sideboard": [{"name": "C", "quantity": 4}]},
    }
    (tmp_path / "Quantity_Test.json").write_text(json.dumps(deck))
    monkeypatch.setattr(mcp_server, "DECKS_DIR", str(tmp_path))

    rows = mcp_server.list_decks("")
    assert len(rows) == 1
    assert rows[0]["card_count"] == 5
    assert rows[0]["total_count"] == 6
    assert rows[0]["sections"]["Sideboard"] == 4
