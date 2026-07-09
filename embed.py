"""ponytail: embed card oracle text, retrieve top-N mechanically similar cards.

Index built once per DB ingest. Search is ~100ms, local, free.

Build:  embed.build(db_path) → saves index to disk
Search: embed.find(db_path, commander_text, top_k=200) → list of card dicts
Status: embed.status() → dict with built/building/count/timestamp
"""

import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone

import numpy as np


INDEX_DIR = "embeddings"
MODEL_NAME = "all-MiniLM-L6-v2"
LOCK_FILE = os.path.join(INDEX_DIR, ".build.lock")
INFO_FILE = os.path.join(INDEX_DIR, "build_info.json")

_index_globals = {}  # ponytail: module-level model/numpy cache per process lifetime
_build_lock = threading.Lock()  # ponytail: prevent concurrent builds


def status():
    """Return dict: {built: bool, building: bool, cards: int, built_at: str, error: str}."""
    if os.path.exists(LOCK_FILE):
        try:
            with open(LOCK_FILE) as f:
                started = f.read().strip()
        except OSError:
            started = "unknown"
        return {"built": False, "building": True, "started_at": started,
                "cards": 0, "built_at": None, "error": None}

    if os.path.exists(INFO_FILE):
        try:
            with open(INFO_FILE) as f:
                info = json.load(f)
        except (json.JSONDecodeError, OSError):
            info = {}
        return {"built": True, "building": False, "cards": info.get("cards", 0),
                "built_at": info.get("built_at"), "error": info.get("error")}

    return {"built": False, "building": False, "cards": 0, "built_at": None, "error": None}


def build(db_path):
    """Embed all unique English cards and save vectors + metadata to disk.

    Thread-safe: only one build at a time across the process."""
    if not _build_lock.acquire(blocking=False):
        return 0  # ponytail: another build is running

    # Write lock file so status() reports building=True
    os.makedirs(INDEX_DIR, exist_ok=True)
    with open(LOCK_FILE, "w") as f:
        f.write(datetime.now(timezone.utc).isoformat())

    try:
        from sentence_transformers import SentenceTransformer

        model = SentenceTransformer(MODEL_NAME)
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row

        rows = db.execute("""
            SELECT c.uuid, c.name, COALESCE(c.text, '') AS text,
                   COALESCE(c.type, '') AS type, COALESCE(c.keywords, '') AS keywords,
                   COALESCE(c.colorIdentity, '') AS colorIdentity, c.manaValue,
                   COALESCE(ci.scryfallId, '') AS scryfallId
            FROM cards c
            LEFT JOIN cardIdentifiers ci ON c.uuid = ci.uuid
            WHERE c.language = 'English' AND (c.side IS NULL OR c.side = 'a')
            ORDER BY c.name
        """).fetchall()

        texts = [r["text"] for r in rows]
        embeddings = model.encode(texts, show_progress_bar=False, batch_size=256,
                                  normalize_embeddings=True)

        meta = [
            {
                "uuid": r["uuid"],
                "name": r["name"],
                "type": r["type"],
                "keywords": r["keywords"],
                "colorIdentity": r["colorIdentity"],
                "manaValue": r["manaValue"],
                "scryfallId": r["scryfallId"],
            }
            for r in rows
        ]

        db.close()

        np.save(os.path.join(INDEX_DIR, "vectors.npy"), embeddings)
        with open(os.path.join(INDEX_DIR, "meta.json"), "w") as f:
            json.dump(meta, f)

        # Write success info
        now = datetime.now(timezone.utc).isoformat()
        with open(INFO_FILE, "w") as f:
            json.dump({"cards": len(rows), "built_at": now}, f)

        return len(rows)

    except Exception as e:
        with open(INFO_FILE, "w") as f:
            json.dump({"cards": 0, "built_at": None, "error": str(e)}, f)
        return 0

    finally:
        _build_lock.release()
        try:
            os.unlink(LOCK_FILE)
        except OSError:
            pass


def find(db_path, oracle_text, top_k=200):
    """Return top_k cards mechanically similar to oracle_text.

    Returns list of {name, type, keywords, colorIdentity, manaValue, text, score}.
    """
    if not oracle_text:
        return []

    vectors, meta = _load_index()
    if vectors is None:
        return []

    from sentence_transformers import SentenceTransformer
    model = _get_model()

    query_vec = model.encode([oracle_text], normalize_embeddings=True)[0]
    # Normalize DB vectors — index may predate normalization in build()
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    norms = np.where(norms == 0, 1.0, norms)  # ponytail: guard zero-norm edge case
    vectors = vectors / norms
    scores = np.dot(vectors, query_vec)
    top_indices = np.argsort(scores)[::-1][:top_k]

    # Enrich with oracle text from DB
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row

    results = []
    for idx in top_indices:
        card = dict(meta[idx])
        card["score"] = round(float(scores[idx]), 4)
        # Fetch oracle text on demand — meta.json stays small
        row = db.execute(
            "SELECT COALESCE(text, '') AS text FROM cards WHERE uuid = ?",
            (card["uuid"],),
        ).fetchone()
        card["text"] = row["text"] if row else ""
        results.append(card)

    db.close()
    return results


def _load_index():
    """Return (vectors, meta) from disk, or (None, None)."""
    v_path = os.path.join(INDEX_DIR, "vectors.npy")
    m_path = os.path.join(INDEX_DIR, "meta.json")
    if not os.path.exists(v_path) or not os.path.exists(m_path):
        return None, None

    vectors = np.load(v_path)
    with open(m_path) as f:
        meta = json.load(f)
    return vectors, meta


def _get_model():
    """Return cached SentenceTransformer model."""
    if "model" not in _index_globals:
        from sentence_transformers import SentenceTransformer
        _index_globals["model"] = SentenceTransformer(MODEL_NAME)
    return _index_globals["model"]
