"""Database ingest engine — validate, deduplicate, and replace the active DB.

ponytail: pure functions + IngestError exception. No Flask dependency.
The route wrapper in app.py delegates to process_upload() and catches IngestError.
"""

import os
import json
import sqlite3
import tempfile
import shutil
import gzip
import bz2
import lzma
import zipfile
import tarfile
import threading
from datetime import datetime, timezone
from urllib.parse import quote

import dedup
import embed
from shared import db_path, resolve_bind_path


class IngestError(Exception):
    """Expected ingestion failure with an HTTP status code for the route wrapper."""
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def process_upload(file_obj, filename, database, ingest_metadata_path):
    """Validate, deduplicate, and install an uploaded MTGJSON SQLite database.

    Args:
        file_obj: Flask file-like object (has .save() method).
        filename: Original upload filename (determines compression format).
        database: Raw DATABASE path — may be a file or a Docker bind-mount dir.
        ingest_metadata_path: Resolved path for .last_ingest.json metadata.

    Returns:
        {"success": True, "cards": N, "timestamp": "..."}

    Raises:
        IngestError: Validation or format errors (status_code set accordingly).
    """
    ext = os.path.splitext(filename)[1].lower()
    # .tgz is short for .tar.gz
    if ext == ".tgz":
        ext = ".gz"

    if ext not in (".sqlite", ".gz", ".bz2", ".xz", ".zip"):
        raise IngestError(
            f"Unsupported file type: {ext}. Accepted: .sqlite, .gz, .bz2, .xz, .zip"
        )

    tmp_upload = None
    tmp_sqlite = None
    tmp_decompressed = None
    tmpdir = None

    try:
        # Save upload to temp file
        tmp_upload = tempfile.NamedTemporaryFile(delete=False)
        file_obj.save(tmp_upload)
        tmp_upload.close()

        if ext == ".sqlite":
            tmp_sqlite = tmp_upload.name

        elif ext in (".gz", ".bz2", ".xz"):
            tmp_decompressed = tmp_upload.name + ".raw"
            if ext == ".gz":
                opener = gzip.open
            elif ext == ".bz2":
                opener = bz2.open
            else:  # .xz
                opener = lzma.open
            # ponytail: stream decompression — reading 650MB+ into RAM is OOM bait
            with opener(tmp_upload.name, "rb") as src, open(tmp_decompressed, "wb") as dst:
                shutil.copyfileobj(src, dst, length=1 << 20)

            # Try as raw SQLite first
            valid_db = sqlite3.connect(tmp_decompressed)
            try:
                valid_db.execute("SELECT COUNT(*) FROM cards WHERE language = 'English'").fetchone()
                tmp_sqlite = tmp_decompressed
            except sqlite3.DatabaseError:
                valid_db.close()
                # Not SQLite — try tar (MTGJSON ships as AllPrintings.tar.gz)
                tmpdir = tempfile.mkdtemp()
                try:
                    # ponytail: "r:" — file is already decompressed, no compression suffix
                    with tarfile.open(tmp_decompressed, "r:") as tf:
                        sqlite_member = None
                        for m in tf.getmembers():
                            if m.name.lower().endswith(".sqlite") or m.name.lower().endswith(".db"):
                                sqlite_member = m
                                break
                        if not sqlite_member:
                            names = [m.name for m in tf.getmembers()[:20]]
                            raise IngestError(
                                f"No .sqlite file found inside the archive. Contents: {names}"
                            )
                        tf.extract(sqlite_member, tmpdir)
                        tmp_sqlite = os.path.join(tmpdir, sqlite_member.name)
                except tarfile.TarError as te:
                    # Sniff first bytes for debugging (before unlinking)
                    try:
                        with open(tmp_decompressed, "rb") as peek:
                            first_bytes = peek.read(64).hex()
                    except OSError:
                        first_bytes = "could not read"
                    raise IngestError(
                        f"Decompressed payload is not SQLite or tar (tar error: {te}). "
                        f"First 64 bytes (hex): {first_bytes}"
                    )
            else:
                valid_db.close()

        elif ext == ".zip":
            tmpdir = tempfile.mkdtemp()
            with zipfile.ZipFile(tmp_upload.name, "r") as zf:
                members = [m for m in zf.namelist() if m.lower().endswith(".sqlite")]
                if not members:
                    raise IngestError("No .sqlite file found inside the zip archive.")
                zf.extract(members[0], tmpdir)
                tmp_sqlite = os.path.join(tmpdir, members[0])

        # Validate: must be a SQLite DB with a cards table
        valid_db = sqlite3.connect(tmp_sqlite)
        try:
            cnt = valid_db.execute("SELECT COUNT(*) FROM cards WHERE language = 'English'").fetchone()[0]
        except sqlite3.OperationalError:
            raise IngestError("File is not a valid MTGJSON SQLite database (no cards table).")
        finally:
            valid_db.close()

        if cnt < 1000:
            raise IngestError(
                f"Database has only {cnt} English cards — this doesn't look like a complete MTGJSON export."
            )

        # ponytail: deduplicate to one row per unique card before replacing live DB.
        # Non-English rows, older printings, and duplicate rulings removed.
        tmp_dedup = tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False)
        tmp_dedup_path = tmp_dedup.name
        tmp_dedup.close()
        try:
            dedup.dedup_db(tmp_sqlite, tmp_dedup_path)
        except Exception:
            try:
                os.unlink(tmp_dedup_path)
            except OSError:
                pass
            raise

        # Validate dedup result
        dedup_db = sqlite3.connect(tmp_dedup_path)
        try:
            dedup_cnt = dedup_db.execute(
                "SELECT COUNT(*) FROM cards WHERE language='English'"
            ).fetchone()[0]
        finally:
            dedup_db.close()

        if dedup_cnt < 500:
            try:
                os.unlink(tmp_dedup_path)
            except OSError:
                pass
            raise IngestError(
                f"Deduplicated database has only {dedup_cnt} English cards — this doesn't look right."
            )

        # Replace the active database.
        # ponytail: Docker bind-mount of a nonexistent file creates an unremovable
        # directory. Write the new DB inside it and clean up old ingest temp files.
        if os.path.isdir(database):
            dst = os.path.join(database, os.path.basename(tmp_dedup_path))
            shutil.copyfile(tmp_dedup_path, dst)
            os.unlink(tmp_dedup_path)
            # Clean up stale tmp files from previous ingests
            for f in sorted(os.listdir(database)):
                fp = os.path.join(database, f)
                if f != os.path.basename(dst) and os.path.isfile(fp):
                    try:
                        os.unlink(fp)
                    except OSError:
                        pass
        else:
            shutil.copyfile(tmp_dedup_path, database)
            os.unlink(tmp_dedup_path)

        # Persist ingest timestamp
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            with open(ingest_metadata_path, "w") as out:
                json.dump({
                    "timestamp": now_iso,
                    "filename": filename,
                    "cards": dedup_cnt,
                    "deduplicated": True,
                }, out)
        except (OSError, IsADirectoryError):
            pass  # ponytail: survive any other I/O issue

        # Trigger embedding index rebuild in background — new cards need new vectors.
        # ponytail: fire-and-forget thread; the config page polls embed.status().
        resolved_db = db_path(database)

        def _rebuild():
            try:
                embed.build(resolved_db)
            except Exception:
                pass
        threading.Thread(target=_rebuild, daemon=True).start()

        return {"success": True, "cards": dedup_cnt, "timestamp": now_iso}

    finally:
        # Clean up temp files (tmp_sqlite may have been moved already)
        try:
            if tmp_upload and os.path.exists(tmp_upload.name):
                os.unlink(tmp_upload.name)
        except Exception:
            pass
        try:
            if tmp_sqlite and os.path.exists(tmp_sqlite):
                os.unlink(tmp_sqlite)
        except Exception:
            pass
        try:
            if tmp_decompressed and os.path.exists(tmp_decompressed):
                os.unlink(tmp_decompressed)
        except Exception:
            pass
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
