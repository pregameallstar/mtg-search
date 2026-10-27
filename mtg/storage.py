"""Crash-safe JSON storage and name-collision helpers.

Small, dependency-light utilities shared by decks, templates, history, tags,
reports, and the prompt store. The Flask app is single-writer in normal use,
but disk writes can still be interrupted; every write therefore goes to a
temporary file in the same directory and is atomically replaced.

The helpers deliberately do not import Flask. They only depend on the
name-sanitizer from :mod:`mtg.shared`.
"""

import json
import os
import tempfile
from contextlib import contextmanager

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback (app targets Linux/Docker)
    fcntl = None

from mtg.shared import sanitize_filename


class StorageError(Exception):
    """A user-correctable storage error with an HTTP status code."""

    def __init__(self, message, status_code=400):
        super().__init__(message)
        self.status_code = status_code


def clean_name(value):
    """Return a stripped string for a user-supplied JSON name, or ``""``."""
    return value.strip() if isinstance(value, str) else ""


def resolve_json_path(directory, name, reserved=()):
    """Return the sanitized path for a display name.

    Rejects empty/sanitized-to-empty names and reserved basenames such as
    ``_tags.json``. Collisions between two different display names are checked
    separately by :func:`read_named_json` / :func:`assert_name_available`.
    """
    safe_name = sanitize_filename((name or "").strip())
    if safe_name == ".json" or safe_name in reserved:
        raise StorageError("Invalid or reserved name. Choose another name.")
    return os.path.join(directory, safe_name)


def read_json(path, default=None):
    """Read JSON, returning ``default`` for missing/unreadable files."""
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, IsADirectoryError):
        return default
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return default


@contextmanager
def file_lock(path):
    """Serialize read-modify-write operations for a JSON store.

    Uses an advisory ``flock`` on a sibling ``.lock`` file. The lock is held
    across processes and tabs, so two deck autosaves or history appends cannot
    lose each other's writes. Falls back to a no-op on platforms without
    ``fcntl`` (the supported target is Linux/Docker).
    """
    if fcntl is None:
        yield
        return
    lock_path = path + ".lock"
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    with open(lock_path, "a+") as lock_file:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass


def write_json_atomic(path, value, mode=0o644):
    """Write JSON via a same-directory temp file + ``os.replace``.

    The temp file is fsynced before replacement, and the directory is fsynced
    afterwards on platforms that support it. A failure leaves the old file
    intact.
    """
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=f".{os.path.basename(path)}.",
        suffix=".tmp",
        dir=directory,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(value, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(tmp_path, mode)
        except OSError:
            pass
        os.replace(tmp_path, path)
        _fsync_dir(directory)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _fsync_dir(directory):
    """Best-effort directory fsync so a rename survives a crash."""
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


def read_named_json(path, expected_name):
    """Read a deck/template file and verify its display name matches.

    Returns ``None`` when the file does not exist. Raises a 409
    :class:`StorageError` when the file exists but belongs to another display
    name (a sanitizer collision), or when it is unreadable.
    """
    if not os.path.exists(path):
        return None
    data = read_json(path, default=None)
    if not isinstance(data, dict):
        raise StorageError(
            "That filename is already used by an unreadable file. "
            "Choose a different name.",
            status_code=409,
        )
    if data.get("name") != expected_name:
        raise StorageError(
            "Another deck/template already uses this filename. "
            "Choose a different name.",
            status_code=409,
        )
    return data


def assert_name_available(path, expected_name):
    """Return existing data for ``expected_name``, or raise a 409 collision."""
    return read_named_json(path, expected_name)
