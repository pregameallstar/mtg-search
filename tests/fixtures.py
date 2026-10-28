"""Minimal SQLite fixture for search/query tests.

The production database is a 113 MB MTGJSON dump. These tests need a small,
deterministic database with the same column names and enough variety to exercise
every search filter. The builder is intentionally explicit: when the production
schema gains a column used by search, add it here in the same PR.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from mtg.shared import VALID_FORMATS


SEARCH_COLUMNS = {
    "uuid": "TEXT PRIMARY KEY",
    "name": "TEXT",
    "text": "TEXT",
    "type": "TEXT",
    "types": "TEXT",
    "subtypes": "TEXT",
    "supertypes": "TEXT",
    "manaCost": "TEXT",
    "manaValue": "REAL",
    "colors": "TEXT",
    "colorIdentity": "TEXT",
    "keywords": "TEXT",
    "power": "TEXT",
    "toughness": "TEXT",
    "loyalty": "TEXT",
    "setCode": "TEXT",
    "number": "TEXT",
    "side": "TEXT",
    "otherFaceIds": "TEXT",
    "language": "TEXT",
    "printings": "TEXT",
    "rarity": "TEXT",
    "borderColor": "TEXT",
    "layout": "TEXT",
    "frameVersion": "TEXT",
    "isReprint": "INTEGER DEFAULT 0",
    "isReserved": "INTEGER DEFAULT 0",
    "isFunny": "INTEGER DEFAULT 0",
    "isOversized": "INTEGER DEFAULT 0",
    "isFullArt": "INTEGER DEFAULT 0",
    "isTextless": "INTEGER DEFAULT 0",
    "isPromo": "INTEGER DEFAULT 0",
    "isRebalanced": "INTEGER DEFAULT 0",
    "isGameChanger": "INTEGER DEFAULT 0",
    "availability": "TEXT",
    "artist": "TEXT",
    "watermark": "TEXT",
    "producedMana": "TEXT",
    "finishes": "TEXT",
    "edhrecRank": "INTEGER",
    "edhrecSaltiness": "REAL",
    "leadershipSkills": "TEXT",
    "faceName": "TEXT",
    "flavorText": "TEXT",
    "isTimeshifted": "INTEGER DEFAULT 0",
    "isStorySpotlight": "INTEGER DEFAULT 0",
    "hasContentWarning": "INTEGER DEFAULT 0",
}

SETS_COLUMNS = {
    "code": "TEXT PRIMARY KEY",
    "name": "TEXT",
    "releaseDate": "TEXT",
    "type": "TEXT",
    "block": "TEXT",
    "keyruneCode": "TEXT",
    "totalSetSize": "INTEGER",
    "baseSetSize": "INTEGER",
}


def _uuid(n: int) -> str:
    return f"00000000-0000-0000-0000-{n:012d}"


def build_search_db(path: str | Path) -> Path:
    """Create and seed a fixture DB at ``path``; return the path."""
    path = Path(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        _create_schema(conn)
        _seed(conn)
        conn.commit()
    finally:
        conn.close()
    return path


def _create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE cards (
            {cards}
        );
        CREATE TABLE cardIdentifiers (
            uuid TEXT PRIMARY KEY,
            scryfallId TEXT,
            scryfallOracleId TEXT
        );
        CREATE TABLE sets (
            {sets}
        );
        CREATE TABLE cardRulings (
            uuid TEXT,
            date TEXT,
            text TEXT
        );
        """.format(
            cards=",\n            ".join(f"{name} {decl}" for name, decl in SEARCH_COLUMNS.items()),
            sets=",\n            ".join(f"{name} {decl}" for name, decl in SETS_COLUMNS.items()),
        )
    )
    legality_cols = ", ".join(f"{fmt} TEXT" for fmt in sorted(VALID_FORMATS))
    conn.execute(f"CREATE TABLE cardLegalities (uuid TEXT PRIMARY KEY, {legality_cols})")
    conn.executescript(
        """
        CREATE INDEX idx_cards_name ON cards(name);
        CREATE INDEX idx_cards_setCode ON cards(setCode);
        CREATE INDEX idx_cardIdentifiers_uuid ON cardIdentifiers(uuid);
        CREATE INDEX idx_cardLegalities_uuid ON cardLegalities(uuid);
        CREATE INDEX idx_sets_code ON sets(code);
        """
    )


def _seed(conn: sqlite3.Connection) -> None:
    sets = [
        ("LTC", "Commander: The Lord of the Rings: Tales of Middle-earth", "2023-11-03", "commander", ""),
        ("ELD", "Throne of Eldraine", "2019-10-04", "expansion", ""),
        ("2X2", "Double Masters 2022", "2022-07-08", "masters", ""),
        ("10E", "Tenth Edition", "2007-07-13", "core", ""),
        ("7ED", "Seventh Edition", "2001-04-11", "core", ""),
        ("M21", "Core Set 2021", "2020-07-03", "core", ""),
        ("PCY", "Prophecy", "2000-06-05", "expansion", ""),
        ("DKA", "Dark Ascension", "2012-02-03", "expansion", ""),
        ("MH2", "Modern Horizons 2", "2021-06-18", "expansion", ""),
        ("AFR", "Adventures in the Forgotten Realms", "2021-07-23", "expansion", ""),
        ("TST", "Test Set", "2024-01-01", "expansion", ""),
        ("C16", "Commander 2016", "2016-11-11", "commander", ""),
    ]
    conn.executemany(
        "INSERT INTO sets (code, name, releaseDate, type, block) VALUES (?, ?, ?, ?, ?)",
        sets,
    )

    counter = 0

    def add(
        *,
        name,
        set_code,
        number,
        type_line="",
        types="",
        subtypes="",
        supertypes="",
        mana_cost="",
        mana_value=0,
        colors="",
        color_identity="",
        text="",
        keywords="",
        power=None,
        toughness=None,
        loyalty=None,
        rarity="common",
        border="black",
        layout="normal",
        frame="2015",
        availability="paper,mtgo",
        flags=None,
        legalities=None,
        side=None,
        other_faces="",
        scryfall_id=None,
        edhrec_rank=None,
        salt=None,
        leadership=None,
        artist=None,
        watermark=None,
        produced_mana=None,
        finishes=None,
    ):
        nonlocal counter
        counter += 1
        uuid = _uuid(counter)
        row = {
            "uuid": uuid,
            "name": name,
            "text": text,
            "type": type_line,
            "types": types,
            "subtypes": subtypes,
            "supertypes": supertypes,
            "manaCost": mana_cost,
            "manaValue": mana_value,
            "colors": colors,
            "colorIdentity": color_identity,
            "keywords": keywords,
            "power": power,
            "toughness": toughness,
            "loyalty": loyalty,
            "setCode": set_code,
            "number": number,
            "side": side,
            "otherFaceIds": other_faces,
            "language": "English",
            "printings": set_code,
            "rarity": rarity,
            "borderColor": border,
            "layout": layout,
            "frameVersion": frame,
            "availability": availability,
            "scryfallId": scryfall_id or _uuid(10_000 + counter),
            "isGameChanger": 1 if (flags or {}).get("isGameChanger") else 0,
            "isReprint": 1 if (flags or {}).get("isReprint") else 0,
            "isReserved": 1 if (flags or {}).get("isReserved") else 0,
            "isFunny": 1 if (flags or {}).get("isFunny") else 0,
            "isOversized": 1 if (flags or {}).get("isOversized") else 0,
            "isFullArt": 1 if (flags or {}).get("isFullArt") else 0,
            "isTextless": 1 if (flags or {}).get("isTextless") else 0,
            "isPromo": 1 if (flags or {}).get("isPromo") else 0,
            "isRebalanced": 1 if (flags or {}).get("isRebalanced") else 0,
            "isTimeshifted": 1 if (flags or {}).get("isTimeshifted") else 0,
            "isStorySpotlight": 1 if (flags or {}).get("isStorySpotlight") else 0,
            "hasContentWarning": 1 if (flags or {}).get("hasContentWarning") else 0,
            "edhrecRank": edhrec_rank,
            "edhrecSaltiness": salt,
            "leadershipSkills": leadership,
            "artist": artist,
            "watermark": watermark,
            "producedMana": produced_mana,
            "finishes": finishes,
        }

        columns = [c for c in SEARCH_COLUMNS if c in row]
        placeholders = ", ".join("?" for _ in columns)
        conn.execute(
            f"INSERT INTO cards ({', '.join(columns)}) VALUES ({placeholders})",
            [row[c] for c in columns],
        )
        conn.execute(
            "INSERT INTO cardIdentifiers (uuid, scryfallId) VALUES (?, ?)",
            (uuid, row["scryfallId"]),
        )

        statuses = {fmt: "Legal" for fmt in sorted(VALID_FORMATS)}
        statuses.update(legalities or {})
        legality_columns = ["uuid"] + sorted(VALID_FORMATS)
        conn.execute(
            f"INSERT INTO cardLegalities ({', '.join(legality_columns)}) "
            f"VALUES ({', '.join('?' for _ in legality_columns)})",
            [uuid] + [statuses[fmt] for fmt in sorted(VALID_FORMATS)],
        )
        return uuid

    # A colorless mana rock; also a legacy banned / vintage restricted card.
    add(
        name="Sol Ring",
        set_code="LTC",
        number="1",
        type_line="Artifact",
        types="Artifact",
        mana_cost="{1}",
        mana_value=1,
        text="{T}: Add {C}{C}.",
        rarity="uncommon",
        availability="paper,mtgo",
        edhrec_rank=1,
        salt=0.2,
        artist="Mark Tedin",
        produced_mana="C",
        finishes="nonfoil, foil",
        legalities={"legacy": "Banned", "vintage": "Restricted"},
    )

    # Commander with keywords and multicolor identity.
    add(
        name="Alela, Artful Provocateur",
        set_code="ELD",
        number="194",
        type_line="Legendary Creature — Faerie Warlock",
        types="Creature",
        subtypes="Faerie,Warlock",
        supertypes="Legendary",
        mana_cost="{1}{W}{U}{B}",
        mana_value=4,
        colors="W, U, B",
        color_identity="B, U, W",
        text=(
            "Flying, deathtouch\nWhenever you cast an artifact or enchantment "
            "spell, create a 1/1 blue Faerie creature token with flying."
        ),
        keywords="Flying,Deathtouch",
        power="2",
        toughness="3",
        rarity="mythic",
        edhrec_rank=1200,
        salt=0.4,
        leadership='{"commander": true, "brawl": false, "oathbreaker": false}',
    )

    # Burn spell.
    add(
        name="Lightning Bolt",
        set_code="2X2",
        number="117",
        type_line="Instant",
        types="Instant",
        mana_cost="{R}",
        mana_value=1,
        colors="R",
        color_identity="R",
        text="Lightning Bolt deals 3 damage to any target.",
        rarity="common",
        edhrec_rank=200,
        legalities={"standard": "Not Legal"},
    )

    # Wipe.
    add(
        name="Wrath of God",
        set_code="10E",
        number="48",
        type_line="Sorcery",
        types="Sorcery",
        mana_cost="{2}{W}{W}",
        mana_value=4,
        colors="W",
        color_identity="W",
        text="Destroy all creatures. They can't be regenerated.",
        rarity="rare",
        edhrec_rank=1500,
    )

    # Counterspell.
    add(
        name="Counterspell",
        set_code="7ED",
        number="67",
        type_line="Instant",
        types="Instant",
        mana_cost="{U}{U}",
        mana_value=2,
        colors="U",
        color_identity="U",
        text="Counter target spell.",
        rarity="common",
    )

    # Land fetch ramp.
    add(
        name="Cultivate",
        set_code="M21",
        number="177",
        type_line="Sorcery",
        types="Sorcery",
        mana_cost="{2}{G}",
        mana_value=3,
        colors="G",
        color_identity="G",
        text=(
            "Search your library for up to two basic land cards, reveal those "
            "cards, put one onto the battlefield tapped and the other into your "
            "hand, then shuffle."
        ),
        rarity="common",
    )

    # Draw engine.
    add(
        name="Rhystic Study",
        set_code="PCY",
        number="45",
        type_line="Enchantment",
        types="Enchantment",
        mana_cost="{2}{U}",
        mana_value=3,
        colors="U",
        color_identity="U",
        text=(
            "Whenever an opponent casts a spell, you may draw a card unless "
            "that player pays {1}."
        ),
        rarity="common",
        edhrec_rank=5,
        salt=2.73,
        artist="Terese Nielsen",
        watermark="Prophecy",
        finishes="nonfoil, foil",
    )

    # Finish-token test: "nonfoil" must not match a foil filter.
    add(
        name="Nonfoil Only",
        set_code="M21",
        number="178",
        type_line="Artifact",
        types="Artifact",
        mana_cost="{1}",
        mana_value=1,
        finishes="nonfoil",
    )

    # Em-dash type line and stax text.
    add(
        name="Thalia, Guardian of Thraben",
        set_code="DKA",
        number="10",
        type_line="Legendary Creature — Human Soldier",
        types="Creature",
        subtypes="Human,Soldier",
        supertypes="Legendary",
        mana_cost="{1}{W}",
        mana_value=2,
        colors="W",
        color_identity="W",
        text="First strike\nSpells your opponents cast cost {1} more to cast.",
        keywords="First strike",
        power="2",
        toughness="1",
        rarity="rare",
    )

    # DFC front/back rows. Search must count and display the front only.
    front_uuid = add(
        name="Cecil, Dark Knight // Cecil, Redeemed Paladin",
        set_code="LTC",
        number="2",
        type_line="Legendary Creature — Human Knight // Legendary Creature — Human Knight",
        types="Creature",
        subtypes="Human,Knight",
        supertypes="Legendary",
        mana_cost="{1}{W}{B}",
        mana_value=3,
        colors="W, B",
        color_identity="B, W",
        text="Front face text.",
        power="3",
        toughness="2",
        rarity="rare",
        side="a",
        leadership='{"commander": true}',
    )
    back_uuid = add(
        name="Cecil, Dark Knight // Cecil, Redeemed Paladin",
        set_code="LTC",
        number="2",
        type_line="Legendary Creature — Human Knight // Legendary Creature — Human Knight",
        types="Creature",
        supertypes="Legendary",
        mana_cost="{1}{W}{B}",
        mana_value=3,
        colors="W, B",
        color_identity="B, W",
        text="Back face text.",
        power="4",
        toughness="3",
        rarity="rare",
        side="b",
        other_faces=front_uuid,
    )
    conn.execute(
        "UPDATE cards SET otherFaceIds = ? WHERE uuid = ?",
        (back_uuid, front_uuid),
    )

    # Star power/toughness.
    add(
        name="Aeve, Progenitor Ooze",
        set_code="MH2",
        number="161",
        type_line="Legendary Creature — Ooze",
        types="Creature",
        supertypes="Legendary",
        mana_cost="{2}{G}{G}",
        mana_value=4,
        colors="G",
        color_identity="G",
        text="Storm\nAeve enters with a +1/+1 counter on it for each other Ooze you control.",
        power="*",
        toughness="*",
        rarity="rare",
    )

    # Planeswalker loyalty.
    add(
        name="Lolth, Spider Queen",
        set_code="AFR",
        number="112",
        type_line="Legendary Planeswalker — Lolth",
        types="Planeswalker",
        supertypes="Legendary",
        mana_cost="{3}{B}{B}",
        mana_value=5,
        colors="B",
        color_identity="B",
        text="+1: Draw a card.",
        loyalty="4",
        rarity="mythic",
    )

    # Partner commander for partner/commander_only filters.
    add(
        name="Tymna the Weaver",
        set_code="C16",
        number="44",
        type_line="Legendary Creature — Human Cleric",
        types="Creature",
        subtypes="Human,Cleric",
        supertypes="Legendary",
        mana_cost="{1}{W}{B}",
        mana_value=3,
        colors="W, B",
        color_identity="B, W",
        text="Partner\nAt the beginning of your end step, you may pay X life, where X is the number of opponents you have. If you do, draw X cards.",
        keywords="Partner",
        power="2",
        toughness="2",
        rarity="rare",
        edhrec_rank=30,
        salt=0.9,
        leadership='{"commander": true}',
    )

    # Odd name, flags, border/layout/frame, arena-only availability.
    add(
        name="Test 100% Card",
        set_code="TST",
        number="100",
        type_line="Creature — Test",
        types="Creature",
        mana_cost="{1}",
        mana_value=1,
        colors="G",
        color_identity="G",
        text="When this creature enters, draw a card.",
        power="1",
        toughness="1",
        rarity="special",
        border="borderless",
        layout="saga",
        frame="future",
        availability="arena",
        artist="Test Artist",
        watermark="Test Mark",
        produced_mana="G",
        finishes="etched",
        flags={
            "isFunny": True,
            "isPromo": True,
            "isFullArt": True,
            "isTextless": True,
            "isRebalanced": True,
            "isGameChanger": True,
            "isTimeshifted": True,
            "isStorySpotlight": True,
            "hasContentWarning": True,
        },
    )

    # Reprint / reserved / oversized on separate rows.
    add(
        name="Reprint Test",
        set_code="TST",
        number="101",
        type_line="Sorcery",
        types="Sorcery",
        text="Draw a card.",
        flags={"isReprint": True},
    )
    add(
        name="Reserved Test",
        set_code="TST",
        number="102",
        type_line="Artifact",
        types="Artifact",
        flags={"isReserved": True},
    )
    add(
        name="Oversized Test",
        set_code="TST",
        number="103",
        type_line="Creature — Giant",
        types="Creature",
        flags={"isOversized": True},
    )
    # Used to prove LIKE wildcard escaping: "100%" must not match "1000".
    add(
        name="1000 Test",
        set_code="TST",
        number="104",
        type_line="Artifact",
        types="Artifact",
        mana_cost="{1}",
        mana_value=1,
        rarity="common",
    )

    # Pagination fillers in TST.
    for i in range(1, 13):
        add(
            name=f"Filler {i:02d}",
            set_code="TST",
            number=f"20{i:02d}",
            type_line="Creature — Filler",
            types="Creature",
            colors="",
            color_identity="",
            mana_value=2,
            text="A filler card.",
            rarity="common",
        )
