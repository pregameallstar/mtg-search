"""Database ingest engine — validate, deduplicate, and replace the active DB.

ponytail: pure functions + IngestError exception. No Flask dependency.
The route wrapper in app.py delegates to process_upload() and catches IngestError.
"""

import os
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

from mtg import dedup, embed
from mtg.shared import db_path
from mtg.storage import write_json_atomic


# Decompressed uploads are intentionally allowed to be large (MTGJSON is
# hundreds of MB), but not unbounded. This also limits zip/tar bombs.
MAX_DECOMPRESSED_BYTES = int(
    os.environ.get("MAX_INGEST_DECOMPRESSED_BYTES", str(4 * 1024 ** 3))
)


class IngestError(Exception):
    """Expected ingestion failure with an HTTP status code for the route wrapper."""
    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def _safe_archive_member(name):
    """Reject absolute paths and parent-directory components in archives."""
    if not name or name.startswith(("/", "\\")):
        return False
    parts = name.replace("\\", "/").split("/")
    return ".." not in parts


def _copy_limited(src, dst, limit=MAX_DECOMPRESSED_BYTES):
    """Stream-copy with a decompressed-size cap."""
    total = 0
    while True:
        chunk = src.read(1 << 20)
        if not chunk:
            return
        total += len(chunk)
        if total > limit:
            raise IngestError(
                "Decompressed upload is too large. Increase "
                "MAX_INGEST_DECOMPRESSED_BYTES if this is intentional."
            )
        dst.write(chunk)


def _install_database(src_path, database):
    """Atomically install a validated database, keeping a .bak copy.

    ``database`` may be a real file or a Docker bind-mount directory (a
    nonexistent host file becomes a directory inside the container). In the
    directory case, install a deterministic AllPrintings.sqlite and remove
    stale database files only after the replacement succeeds.
    """
    if os.path.isdir(database):
        target_dir = database
        target = os.path.join(database, "AllPrintings.sqlite")
    else:
        target = os.path.abspath(database)
        target_dir = os.path.dirname(target) or "."

    os.makedirs(target_dir, exist_ok=True)
    backup = target + ".bak"
    # In the directory/bind-mount shape the active DB may still have a random
    # temp name. Prefer the deterministic target if it exists; otherwise pick
    # the newest .sqlite that is not the source file we are installing.
    if os.path.isdir(database):
        current_db = target if os.path.isfile(target) else None
        if current_db is None:
            candidates = [
                os.path.join(database, f)
                for f in os.listdir(database)
                if f.endswith(".sqlite")
                and os.path.abspath(os.path.join(database, f)) != os.path.abspath(src_path)
            ]
            if candidates:
                current_db = max(candidates, key=os.path.getmtime)
    else:
        current_db = target
    if current_db and os.path.isfile(current_db):
        try:
            shutil.copy2(current_db, backup)
        except OSError as e:
            raise IngestError(f"Could not back up the current database: {e}", 500)

    fd, tmp_path = tempfile.mkstemp(
        prefix=".ingest-", suffix=".sqlite", dir=target_dir,
    )
    try:
        with os.fdopen(fd, "wb") as dst, open(src_path, "rb") as src:
            shutil.copyfileobj(src, dst, length=1 << 20)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(tmp_path, target)
        _fsync_dir(target_dir)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise

    if os.path.isdir(database):
        target_name = os.path.basename(target)
        for fname in os.listdir(database):
            fpath = os.path.join(database, fname)
            if (
                os.path.isfile(fpath)
                and fname != target_name
                and not fname.endswith(".bak")
                and fname != ".last_ingest.json"
            ):
                try:
                    os.unlink(fpath)
                except OSError:
                    pass


def _fsync_dir(directory):
    """Best-effort fsync of a directory after an atomic replace."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


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
    tmp_dedup_path = None
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
            # ponytail: stream decompression — reading 650MB+ into RAM is OOM bait.
            # The helper also enforces a decompressed-size cap.
            with opener(tmp_upload.name, "rb") as src, open(tmp_decompressed, "wb") as dst:
                _copy_limited(src, dst)

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
                            if not m.isfile():
                                continue
                            if not (m.name.lower().endswith(".sqlite") or m.name.lower().endswith(".db")):
                                continue
                            if not _safe_archive_member(m.name):
                                raise IngestError(
                                    f"Refusing unsafe archive member: {m.name!r}"
                                )
                            if m.size > MAX_DECOMPRESSED_BYTES:
                                raise IngestError(
                                    "Database member inside the archive is too large."
                                )
                            sqlite_member = m
                            break
                        if not sqlite_member:
                            names = [m.name for m in tf.getmembers()[:20]]
                            raise IngestError(
                                f"No .sqlite file found inside the archive. Contents: {names}"
                            )
                        # Prefer the hardened extraction filter on Python 3.12+;
                        # fall back only after the explicit member check above.
                        try:
                            tf.extract(sqlite_member, tmpdir, filter="data")
                        except TypeError:
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
                members = [
                    m for m in zf.namelist()
                    if m.lower().endswith(".sqlite") and _safe_archive_member(m)
                ]
                if not members:
                    raise IngestError("No safe .sqlite file found inside the zip archive.")
                info = zf.getinfo(members[0])
                if info.file_size > MAX_DECOMPRESSED_BYTES:
                    raise IngestError("Database member inside the zip is too large.")
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

        # Replace the active database atomically. Docker bind-mounts of a
        # nonexistent file become a directory; _install_database handles both
        # shapes and keeps a .bak copy of the previous DB.
        _install_database(tmp_dedup_path, database)
        try:
            os.unlink(tmp_dedup_path)
        except OSError:
            pass

        # Persist ingest timestamp
        now_iso = datetime.now(timezone.utc).isoformat()
        try:
            write_json_atomic(ingest_metadata_path, {
                "timestamp": now_iso,
                "filename": filename,
                "cards": dedup_cnt,
                "deduplicated": True,
            })
        except OSError:
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
        try:
            if tmp_dedup_path and os.path.exists(tmp_dedup_path):
                os.unlink(tmp_dedup_path)
        except Exception:
            pass
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
