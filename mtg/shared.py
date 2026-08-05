"""ponytail: shared utilities used by both app.py and mcp_server.py.

Extracted from duplicated implementations to keep a single source of truth.
"""

import os
import sqlite3
from urllib.parse import quote

from flask import g
from mtg.images import card_image_url, mana_symbols


def db_path(db_name="AllPrintings.sqlite"):
    """Return the actual SQLite database path.

    Docker bind-mount of a file that doesn't exist on the host creates a
    directory — we write DB files inside it and pick the newest one.
    """
    if os.path.isfile(db_name):
        return db_name
    if os.path.isdir(db_name):
        dbs = sorted(
            [f for f in os.listdir(db_name) if f.endswith(".sqlite")],
            key=lambda x: os.path.getmtime(os.path.join(db_name, x)),
            reverse=True,
        )
        if dbs:
            return os.path.join(db_name, dbs[0])
    return db_name


def color_identity_subset(card_ci, allowed):
    """Return True if every color in card_ci is also in allowed.

    card_ci: raw color-identity string from the DB (e.g. "W, U" or "").
    allowed: either a raw CI string or a pre-parsed set of color letters
             (the mcp_server path passes a set; app.py passes a string).

    Colorless cards (empty CI) are legal everywhere.
    """
    if not card_ci or not card_ci.strip():
        return True
    # Normalize DB CI: strip spaces and commas so "W, U" → {"W", "U"}
    card_colors = set(card_ci.replace(" ", "").replace(",", ""))

    if isinstance(allowed, set):
        # ponytail: mcp_server path — allowed is already parsed via _parse_ci
        return card_colors <= allowed
    # app.py path — allowed is a raw CI string from another card
    if not allowed or not allowed.strip():
        return False
    allowed_colors = set(allowed.replace(" ", "").replace(",", ""))
    return card_colors <= allowed_colors


def resolve_bind_path(path, fallback_basename=None):
    """Resolve a path that might be a Docker bind-mount directory.

    Docker bind-mounts of nonexistent host files create empty directories
    on the container side.  When `path` is a directory, join the original
    basename inside it.  If `fallback_basename` is given, use that instead
    of os.path.basename(path).

    Returns the path itself when it's a regular file or doesn't exist.
    """
    if not os.path.isdir(path):
        return path
    basename = fallback_basename or os.path.basename(path)
    return os.path.join(path, basename)


# --- App-wide constants ---

DATABASE = "AllPrintings.sqlite"
REPORTS_DIR = "eval_reports"
IMAGE_DIR = "images"
DECKS_DIR = "decks"
os.makedirs(DECKS_DIR, exist_ok=True)
TEMPLATES_DIR = "card_templates"
os.makedirs(TEMPLATES_DIR, exist_ok=True)
PRICES_DIR = "prices"
os.makedirs(PRICES_DIR, exist_ok=True)
HISTORY_DIR = os.path.join(DECKS_DIR, "_history")
os.makedirs(HISTORY_DIR, exist_ok=True)

# ponytail: alias for backward compat — Jinja globals and deck routes use this name
card_image = card_image_url


# --- DB helpers ---

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(db_path(DATABASE))
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA query_only = ON")
    return g.db


def _db_ready():
    """Return True if the cards table exists with at least one row."""
    try:
        db = get_db()
        row = db.execute("SELECT COUNT(*) FROM cards WHERE language='English' LIMIT 1").fetchone()
        return row is not None and row[0] > 0
    except (sqlite3.OperationalError, sqlite3.DatabaseError):
        return False


# --- Jinja helpers ---

def render_mana(cost):
    """Render mana cost to HTML with mana symbol classes."""
    return mana_symbols(cost)


def pagination_url(args, page):
    """Build URL with existing query params, replacing page."""
    parts = []
    for k, vals in args.lists():
        if k == "page":
            continue
        for v in vals:
            parts.append(f"{quote(k)}={quote(v)}")
    parts.append(f"page={page}")
    # ponytail: preserve per_page so pagination links don't reset it to default
    if "per_page" not in args:
        parts.append("per_page=30")
    return "/search?" + "&".join(parts) if parts else "/search"
