"""ponytail: shared utilities used by both app.py and mcp_server.py.

Extracted from duplicated implementations to keep a single source of truth.
"""

import json
import os
import re
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


# Valid format keys for legality filters. Single source of truth so search and
# similarity don't drift when a new format ships. Referenced in SQL via a
# validated fmt_lower, so entries must stay valid column names.
VALID_FORMATS = {
    "standard", "commander", "modern", "legacy", "pioneer", "vintage",
    "pauper", "brawl", "historic", "oathbreaker", "penny", "duel",
    "alchemy", "gladiator", "oldschool", "premodern", "predh",
    "paupercommander", "timeless", "standardbrawl", "competitivebrawl",
}


def sanitize_filename(name):
    """Map a display name to a safe on-disk basename.

    Strips any character outside [a-zA-Z0-9_ -] (including ., /, ..) so path
    traversal is prevented incidentally, then replaces spaces with underscores
    and appends .json. Used by deck, template, and history routes so a name
    like "My Deck (v1)" consistently becomes My_Deck_v1.json.
    """
    safe_name = re.sub(r'[^a-zA-Z0-9_ -]', '', name)
    return safe_name.replace(' ', '_') + '.json'


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

# Project root — everything persists here (DB, reports, decks, images, prices).
# Derived once so every blueprint resolves paths identically regardless of where
# its __file__ lives (mtg/ vs mtg/blueprints/).
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

DATABASE = "AllPrintings.sqlite"
DEFAULT_PER_PAGE = 30
REPORTS_DIR = "eval_reports"
REPORTS_PATH = os.path.join(PROJECT_ROOT, REPORTS_DIR)
REVIEWS_DIR = "deck_reviews"
REVIEWS_PATH = os.path.join(PROJECT_ROOT, REVIEWS_DIR)
# Auto-saved deepdive files use this suffix; the eval landing page skips them
# when listing saved reports. Single source of truth so a rename can't desync.
DEEPDIVE_SUFFIX = "_deepdives.json"
IMAGE_DIR = "images"
DECKS_DIR = "decks"
os.makedirs(DECKS_DIR, exist_ok=True)
TEMPLATES_DIR = "card_templates"
os.makedirs(TEMPLATES_DIR, exist_ok=True)
PRICES_DIR = "prices"
os.makedirs(PRICES_DIR, exist_ok=True)
HISTORY_DIR = os.path.join(DECKS_DIR, "_history")
os.makedirs(HISTORY_DIR, exist_ok=True)
os.makedirs(REVIEWS_DIR, exist_ok=True)

# ponytail: alias for backward compat — Jinja globals and deck routes use this name
card_image = card_image_url


# --- DB helpers ---

def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(db_path(DATABASE))
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA query_only = ON")
    return g.db


# SQL fragment repeated across routes: restrict to English, single-faced
# printings only. Keeping it here avoids each blueprint re-typing it (and
# drifting).
_ENGLISH_SIDE = "(c.side IS NULL OR c.side = 'a')"


def _latest_printing(name):
    """Return (set_code, number) for the latest printing of an English card name."""
    db = get_db()
    return db.execute(
        "SELECT c.setCode, c.number FROM cards c "
        "JOIN sets s ON c.setCode = s.code "
        "WHERE c.name = ? AND c.language = 'English' AND " + _ENGLISH_SIDE + " "
        "ORDER BY s.releaseDate DESC LIMIT 1",
        [name],
    ).fetchone()


def _load_card(set_code, number):
    """Return the primary English card row (dict) with scryfallId + set info."""
    db = get_db()
    return db.execute(
        "SELECT c.*, ci.scryfallId, s.name as setName, s.releaseDate "
        "FROM cards c "
        "JOIN cardIdentifiers ci ON c.uuid = ci.uuid "
        "JOIN sets s ON c.setCode = s.code "
        "WHERE c.setCode = ? AND c.number = ? AND c.language = 'English' "
        "AND " + _ENGLISH_SIDE,
        [set_code, number],
    ).fetchone()


def _other_faces(card):
    """Return the other faces of a DFC card as a list of rows (dicts)."""
    if not card.get("otherFaceIds"):
        return []
    other_uuids = [u.strip() for u in card["otherFaceIds"].split(",") if u.strip()]
    if not other_uuids:
        return []
    db = get_db()
    placeholders = ",".join("?" * len(other_uuids))
    return db.execute(
        "SELECT c.*, ci.scryfallId "
        "FROM cards c "
        "JOIN cardIdentifiers ci ON c.uuid = ci.uuid "
        "WHERE c.uuid IN (" + placeholders + ") "
        "ORDER BY c.side",
        other_uuids,
    ).fetchall()


def _list_json_files(directory, skip_names=None):
    """Return parsed JSON objects from every *.json file in a directory.

    Skips unparseable files and any basenames in skip_names. Sorted by filename.
    """
    skip_names = skip_names or ()
    out = []
    if not os.path.isdir(directory):
        return out
    for fname in sorted(os.listdir(directory)):
        if not fname.endswith(".json") or fname in skip_names:
            continue
        try:
            with open(os.path.join(directory, fname), encoding="utf-8") as f:
                data = json.load(f)
            # Deck/template stores are objects. A malformed array/string should
            # not be returned to pages or MCP tools that assume object access.
            if isinstance(data, dict):
                out.append(data)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            pass
    return out


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
        parts.append(f"per_page={DEFAULT_PER_PAGE}")
    return "/search?" + "&".join(parts) if parts else "/search"
