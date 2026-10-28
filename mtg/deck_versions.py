"""Deck version snapshots and deck-to-deck comparison.

Snapshots are plain JSON files under ``decks/_versions``. They are created
explicitly from the History tab, and the first save of a deck gets a baseline.
Content hashes prevent duplicate snapshots from rapid autosaves.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Mapping

from mtg.deck_stats import analyze_deck
from mtg.shared import sanitize_filename
from mtg.storage import write_json_atomic


def deck_content_hash(deck: Mapping[str, Any]) -> str:
    """Stable hash of the saved deck content."""
    canonical = json.dumps(deck, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def snapshot_filename(deck_name: str, content_hash: str,
                      when: datetime | None = None) -> str:
    when = when or datetime.now(timezone.utc)
    base = sanitize_filename(deck_name or "deck").replace(".json", "")
    return f"{base}__{when.strftime('%Y%m%dT%H%M%SZ')}__{content_hash[:8]}.json"


def _version_files(directory: str, deck_name: str = "") -> list[str]:
    if not os.path.isdir(directory):
        return []
    out = []
    for filename in os.listdir(directory):
        if not filename.endswith(".json"):
            continue
        out.append(filename)
    out.sort(reverse=True)
    return out


def _read_version(directory: str, filename: str) -> dict | None:
    if filename != os.path.basename(filename) or ".." in filename:
        return None
    try:
        with open(os.path.join(directory, filename), encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def list_versions(directory: str, deck_name: str) -> list[dict[str, Any]]:
    versions = []
    for filename in _version_files(directory, deck_name):
        data = _read_version(directory, filename)
        if not isinstance(data, dict):
            continue
        if data.get("deck") != deck_name:
            continue
        snapshot = data.get("snapshot") if isinstance(data.get("snapshot"), dict) else {}
        cards = snapshot.get("cards") if isinstance(snapshot.get("cards"), list) else []
        total = sum(
            int(card.get("quantity") or 1) for card in cards
            if isinstance(card, dict)
        )
        versions.append({
            "filename": filename,
            "deck": deck_name,
            "createdAt": data.get("createdAt", ""),
            "contentHash": data.get("contentHash", ""),
            "total": total + (1 if snapshot.get("commander") else 0),
            "main": total,
            "commander": (snapshot.get("commander") or {}).get("name", "")
            if isinstance(snapshot.get("commander"), dict) else "",
            "sections": len(snapshot.get("sections") or {})
            if isinstance(snapshot.get("sections"), dict) else 0,
        })
    return versions


def load_version(directory: str, filename: str) -> dict | None:
    data = _read_version(directory, filename)
    if not isinstance(data, dict):
        return None
    snapshot = data.get("snapshot")
    return snapshot if isinstance(snapshot, dict) else None


def write_version(
    directory: str,
    deck: Mapping[str, Any],
    *,
    force: bool = False,
    min_interval: int = 0,
) -> tuple[str | None, str]:
    """Write a snapshot unless the latest one is identical or too recent.

    Returns ``(filename or None, reason)``. ``force=True`` ignores the interval
    but still skips an exact duplicate content hash.
    """
    name = str(deck.get("name") or "")
    if not name:
        return None, "missing_name"
    os.makedirs(directory, exist_ok=True)
    content_hash = deck_content_hash(deck)
    latest = list_versions(directory, name)
    if latest and latest[0]["contentHash"] == content_hash:
        return None, "unchanged"
    if latest and not force and min_interval > 0:
        try:
            created = datetime.fromisoformat(
                str(latest[0]["createdAt"]).replace("Z", "+00:00")
            )
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - created).total_seconds()
            if age < min_interval:
                return None, "throttled"
        except (TypeError, ValueError):
            pass
    now = datetime.now(timezone.utc)
    filename = snapshot_filename(name, content_hash, now)
    path = os.path.join(directory, filename)
    payload = {
        "schemaVersion": 1,
        "deck": name,
        "createdAt": now.isoformat(),
        "contentHash": content_hash,
        "snapshot": dict(deck),
    }
    write_json_atomic(path, payload)
    return filename, "created"


def _card_key(card: Mapping[str, Any]) -> str:
    uuid = str(card.get("uuid") or "").strip()
    if uuid:
        return uuid
    name = str(card.get("name") or "").split(" // ", 1)[0].strip().lower()
    return name or str(card.get("name") or "")


def _card_map(deck: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    out = {}
    for card in deck.get("cards") or []:
        if not isinstance(card, Mapping):
            continue
        key = _card_key(card)
        if not key:
            continue
        existing = out.get(key)
        qty = int(card.get("quantity") or 1)
        if existing:
            existing["quantity"] += qty
        else:
            out[key] = {
                "name": card.get("name") or "",
                "setCode": card.get("setCode") or "",
                "number": card.get("number") or "",
                "quantity": qty,
            }
    return out


def compare_decks(a: Mapping[str, Any], b: Mapping[str, Any]) -> dict[str, Any]:
    """Compare two deck objects by card identity, counts, and analysis."""
    map_a = _card_map(a)
    map_b = _card_map(b)

    added = [
        {**map_b[key], "key": key}
        for key in sorted(map_b)
        if key not in map_a
    ]
    removed = [
        {**map_a[key], "key": key}
        for key in sorted(map_a)
        if key not in map_b
    ]
    changed = []
    for key in sorted(set(map_a) & set(map_b)):
        if map_a[key]["quantity"] != map_b[key]["quantity"]:
            changed.append({
                "key": key,
                "name": map_b[key]["name"],
                "from": map_a[key]["quantity"],
                "to": map_b[key]["quantity"],
            })

    audit_a = analyze_deck(a)
    audit_b = analyze_deck(b)
    role_names = set(audit_a.get("roles") or {}) | set(audit_b.get("roles") or {})
    role_delta = {
        role: {
            "a": (audit_a.get("roles", {}).get(role) or {}).get("count", 0),
            "b": (audit_b.get("roles", {}).get(role) or {}).get("count", 0),
        }
        for role in sorted(role_names)
    }

    def commander(deck):
        value = deck.get("commander")
        return value.get("name", "") if isinstance(value, Mapping) else ""

    return {
        "a": {
            "name": a.get("name", ""),
            "commander": commander(a),
            "total": audit_a["counts"]["total"],
            "health": audit_a["health"]["score"],
            "bracket": audit_a["bracket"]["estimate"],
            "averageMv": audit_a["curve"]["averageMv"],
        },
        "b": {
            "name": b.get("name", ""),
            "commander": commander(b),
            "total": audit_b["counts"]["total"],
            "health": audit_b["health"]["score"],
            "bracket": audit_b["bracket"]["estimate"],
            "averageMv": audit_b["curve"]["averageMv"],
        },
        "added": added,
        "removed": removed,
        "changed": changed,
        "roles": role_delta,
        "summary": {
            "added": len(added),
            "removed": len(removed),
            "changed": len(changed),
        },
    }
