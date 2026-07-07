"""Deduplicate MTGJSON AllPrintings.sqlite to one row per unique card.

ponytail: standalone module, imported by app.py on ingest.
ponytail: keeps latest printing (by set releaseDate), drops non-English.
ponytail: remaps cardRulings/cardIdentifiers/cardLegalities to surviving UUIDs.
ponytail: dedup happens in a temp file, then shutil.move replaces the live DB.
"""

import sqlite3
import os
import tempfile
import shutil


def dedup_db(src_path, dst_path):
    """Read full MTGJSON from src_path, write deduplicated DB to dst_path."""
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(dst_path)

    # --- sets (unchanged) ---
    _clone_table(src, dst, "sets")
    dst.execute("CREATE INDEX IF NOT EXISTS idx_sets_code ON sets (code)")
    dst.execute("CREATE INDEX IF NOT EXISTS idx_sets_name ON sets (name)")

    # --- Find surviving UUIDs: latest printing per (name, side) ---
    # ponytail: side handles DFCs — keep side='a' and side='b' rows for
    # the same latest printing so otherFaceIds and card detail still work.
    keep = set()
    for row in src.execute("""
        SELECT DISTINCT name, side FROM cards
        WHERE language = 'English'
    """):
        name, side = row
        if side:
            uuid_row = src.execute("""
                SELECT c.uuid FROM cards c
                JOIN sets s ON c.setCode = s.code
                WHERE c.name = ? AND c.side = ? AND c.language = 'English'
                ORDER BY s.releaseDate DESC, c.number ASC
                LIMIT 1
            """, (name, side)).fetchone()
        else:
            uuid_row = src.execute("""
                SELECT c.uuid FROM cards c
                JOIN sets s ON c.setCode = s.code
                WHERE c.name = ? AND c.side IS NULL AND c.language = 'English'
                ORDER BY s.releaseDate DESC, c.number ASC
                LIMIT 1
            """, (name,)).fetchone()
        if uuid_row:
            keep.add(uuid_row[0])

    # --- cards ---
    _filtered_clone(src, dst, "cards", keep)
    dst.execute("CREATE INDEX IF NOT EXISTS idx_cards_setCode ON cards (setCode)")
    dst.execute("CREATE INDEX IF NOT EXISTS idx_cards_name ON cards (name)")
    dst.execute("CREATE INDEX IF NOT EXISTS idx_cards_uuid ON cards (uuid)")

    # --- cardIdentifiers ---
    _filtered_clone(src, dst, "cardIdentifiers", keep)
    dst.execute("CREATE INDEX IF NOT EXISTS idx_cardIdentifiers_uuid ON cardIdentifiers (uuid)")

    # --- cardLegalities ---
    _filtered_clone(src, dst, "cardLegalities", keep)
    dst.execute("CREATE INDEX IF NOT EXISTS idx_cardLegalities_uuid ON cardLegalities (uuid)")

    # --- cardRulings: remap + dedup (same ruling text per card = one row) ---
    _rulings_clone(src, dst, keep)
    dst.execute("CREATE INDEX IF NOT EXISTS idx_cardRulings_uuid ON cardRulings (uuid)")

    dst.commit()
    src.close()
    dst.close()


def _ddl_clone(src, dst, table):
    """Recreate table schema from src in dst."""
    create_sql = src.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=? ORDER BY type DESC",
        (table,)
    ).fetchone()
    if create_sql:
        dst.execute(create_sql[0])
    else:
        col_defs = []
        for col_info in src.execute(f"PRAGMA table_info({table})"):
            col_defs.append(
                f"{col_info[1]} {col_info[2]}"
                + (" NOT NULL" if col_info[3] else "")
                + (f" DEFAULT {col_info[4]}" if col_info[4] is not None else "")
            )
        dst.execute(f"CREATE TABLE {table} ({', '.join(col_defs)})")


def _clone_table(src, dst, table):
    """Copy entire table from src to dst with schema and all rows."""
    _ddl_clone(src, dst, table)
    cols = [d[0] for d in src.execute(f"SELECT * FROM {table} LIMIT 0").description]
    col_list = ", ".join(cols)
    placeholders = ", ".join("?" * len(cols))
    batch = []
    for row in src.execute(f"SELECT * FROM {table}"):
        batch.append(row)
        if len(batch) >= 5000:
            dst.executemany(f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})", batch)
            batch.clear()
    if batch:
        dst.executemany(f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})", batch)


def _filtered_clone(src, dst, table, keep_uuids):
    """Copy rows from src to dst, filtered to keep_uuids."""
    _ddl_clone(src, dst, table)
    cols = [d[0] for d in src.execute(f"SELECT * FROM {table} LIMIT 0").description]
    uuid_idx = cols.index("uuid")
    col_list = ", ".join(cols)
    placeholders = ", ".join("?" * len(cols))

    batch = []
    for row in src.execute(f"SELECT * FROM {table}"):
        if row[uuid_idx] in keep_uuids:
            batch.append(row)
            if len(batch) >= 5000:
                dst.executemany(f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})", batch)
                batch.clear()
    if batch:
        dst.executemany(f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})", batch)


def _rulings_clone(src, dst, keep_uuids):
    """Copy rulings, deduplicating by (uuid, text) and filtering to keep_uuids."""
    dst.execute("CREATE TABLE cardRulings (uuid TEXT, date DATE, text TEXT)")
    seen = set()
    batch = []
    for row in src.execute("SELECT uuid, date, text FROM cardRulings"):
        uuid, date, text = row
        if uuid in keep_uuids:
            key = (uuid, text)
            if key not in seen:
                seen.add(key)
                batch.append(row)
                if len(batch) >= 5000:
                    dst.executemany(
                        "INSERT INTO cardRulings (uuid, date, text) VALUES (?, ?, ?)",
                        batch,
                    )
                    batch.clear()
    if batch:
        dst.executemany(
            "INSERT INTO cardRulings (uuid, date, text) VALUES (?, ?, ?)",
            batch,
        )


def dedup_in_place(db_path):
    """Deduplicate an existing database file in-place (atomic replace)."""
    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
    tmp_path = tmp.name
    tmp.close()

    try:
        dedup_db(db_path, tmp_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    shutil.move(tmp_path, db_path)
