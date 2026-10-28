"""Shared deck-card enrichment for analysis and review.

Saved deck JSON stores a compact card shape. Analysis features need the current
DB row (text, type, legalities, EDHREC, salt, Game Changer, DFC faces), so this
module centralizes the lookup so the deck-analysis API and Deck Doctor agree.
"""

from __future__ import annotations

from typing import Any

from mtg.storage import clean_name


def lookup_card(db, card: dict) -> dict | None:
    """Return a full DB card row for a saved-deck card, or None."""
    uuid = clean_name(card.get("uuid", ""))
    set_code = clean_name(card.get("setCode", ""))
    number = clean_name(card.get("number", ""))
    name = clean_name(card.get("name", ""))

    base = """
        SELECT c.*, ci.scryfallId, s.name as setName, s.releaseDate,
               cl.commander as commanderLegality
        FROM cards c
        JOIN cardIdentifiers ci ON c.uuid = ci.uuid
        JOIN sets s ON c.setCode = s.code
        LEFT JOIN cardLegalities cl ON c.uuid = cl.uuid
    """
    where = "WHERE c.language = 'English' AND (c.side IS NULL OR c.side = 'a')"
    row = None
    # Prefer the exact printing, but fall back to set/number and then name so a
    # stale uuid (after an ingest or a hand-edited deck) does not silently leave
    # the card unenriched.
    if uuid:
        row = db.execute(base + where + " AND c.uuid = ? LIMIT 1", [uuid]).fetchone()
    if row is None and set_code and number:
        row = db.execute(
            base + where + " AND c.setCode = ? AND c.number = ? LIMIT 1",
            [set_code, number],
        ).fetchone()
    if row is None and name:
        row = db.execute(
            base + where + """
              AND (LOWER(c.name) = LOWER(?) OR LOWER(
                    CASE WHEN instr(c.name, ' // ') > 0
                         THEN substr(c.name, 1, instr(c.name, ' // ') - 1)
                         ELSE c.name END
                  ) = LOWER(?))
              ORDER BY s.releaseDate DESC LIMIT 1
            """,
            [name, name],
        ).fetchone()
    if row is None:
        return None
    data = dict(row)
    data["legalities"] = {"commander": data.pop("commanderLegality", "")}

    # Include other faces' oracle text so DFC roles are not missed.
    face_ids = [
        face.strip() for face in str(data.get("otherFaceIds") or "").split(",")
        if face.strip()
    ]
    if face_ids:
        placeholders = ",".join("?" for _ in face_ids)
        faces = db.execute(
            f"SELECT text FROM cards WHERE uuid IN ({placeholders})",
            face_ids,
        ).fetchall()
        extra = [face["text"] for face in faces if face["text"]]
        if extra:
            data["text"] = "\n".join([data.get("text") or ""] + extra)

    data["tags"] = list(card.get("tags") or [])
    data["note"] = card.get("note") or ""
    data["quantity"] = card.get("quantity", 1)
    data["imageUrl"] = card.get("imageUrl") or ""
    return data


def enrich_card(db, card: dict) -> dict:
    """Merge a DB lookup into a saved-deck card, preserving user fields."""
    found = lookup_card(db, card)
    if not found:
        return card
    merged = dict(card)
    merged.update(found)
    return merged


def enrich_deck(db, deck: dict) -> dict:
    """Return a deck copy with enriched commander and main-deck cards."""
    commander = deck.get("commander")
    return {
        **deck,
        "commander": enrich_card(db, commander) if isinstance(commander, dict) else None,
        "cards": [
            enrich_card(db, card)
            for card in (deck.get("cards") or [])
            if isinstance(card, dict)
        ],
    }
