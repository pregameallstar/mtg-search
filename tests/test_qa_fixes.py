"""Regression tests for the QA-report fixes (B1-B3, M1-M4, Q1-Q5).

Each test maps to a finding in QA_REPORT.md and asserts the fix is in place.
Read-only: the Flask app is exercised via test_client with SQLite in query_only
mode; no files are written outside tempdirs.
"""

import os
import re
import sqlite3
import tempfile

import pytest

import app as app_module
from mtg.shared import (
    DATABASE,
    DEEPDIVE_SUFFIX,
    DEFAULT_PER_PAGE,
    PROJECT_ROOT,
    REPORTS_DIR,
    REPORTS_PATH,
    db_path,
)


# --------------------------------------------------------------------------- #
# B1 — eval report directory mismatch
# --------------------------------------------------------------------------- #

def test_b1_reports_path_is_project_root():
    """eval_landing and eval must read/write the SAME directory: project root."""
    assert REPORTS_PATH == os.path.join(PROJECT_ROOT, REPORTS_DIR)
    # The buggy location was mtg/blueprints/eval_reports — assert it is NOT used.
    assert not REPORTS_PATH.endswith(
        os.path.join("mtg", "blueprints", "eval_reports")
    )


def test_b1_eval_landing_uses_shared_reports_path():
    """eval_landing.py must not derive reports_dir from __file__ anymore."""
    src = open("mtg/blueprints/eval_landing.py").read()
    assert "REPORTS_PATH" in src
    # The buggy derivation is gone:
    assert 'os.path.dirname(os.path.abspath(__file__)), REPORTS_DIR' not in src
    assert 'os.path.join(os.path.dirname(os.path.abspath(__file__)), REPORTS_DIR' not in src


def test_b1_eval_uses_shared_reports_path():
    """eval.py must resolve report paths via the shared REPORTS_PATH."""
    src = open("mtg/blueprints/eval.py").read()
    assert "_project_root" not in src
    assert "REPORTS_PATH" in src


# --------------------------------------------------------------------------- #
# B2 — broken absolute imports in eval_helpers
# --------------------------------------------------------------------------- #

def test_b2_eval_helpers_imports_are_namespaced():
    src = open("mtg/eval_helpers.py").read()
    assert "from mtg.embed import find" in src
    assert "from mtg.similarity import" in src
    assert "from embed import" not in src
    assert "from similarity import" not in src


def test_b2_mcp_server_embed_import_is_namespaced():
    src = open("mtg/mcp_server.py").read()
    assert "from mtg.embed import find" in src
    assert not re.search(r"^import embed\b", src, re.MULTILINE)


def test_b2_similar_modules_resolve():
    """The lazy imports inside eval_similar_embed/legacy must actually resolve."""
    from mtg.eval_helpers import eval_similar_embed, eval_similar_legacy
    import mtg.embed  # noqa: F401
    import mtg.similarity  # noqa: F401
    # Importing is the assertion — a ModuleNotFoundError would raise here.
    assert callable(eval_similar_embed)
    assert callable(eval_similar_legacy)


# --------------------------------------------------------------------------- #
# B3 — colorless `C` filter with `at_most`
# --------------------------------------------------------------------------- #

def _distinct_names_where(where_sql, params=()):
    """Count distinct front-face names matching a WHERE fragment, mirroring
    the search route's dedupe expression."""
    conn = sqlite3.connect(db_path(DATABASE))
    conn.row_factory = sqlite3.Row
    try:
        expr = ("CASE WHEN instr(name, ' // ') > 0 "
                "THEN substr(name, 1, instr(name, ' // ') - 1) ELSE name END")
        return conn.execute(
            f"SELECT COUNT(DISTINCT {expr}) FROM cards c WHERE {where_sql}",
            params,
        ).fetchone()[0]
    finally:
        conn.close()


def _search_total(client, **qs):
    """Drive the real /search route and return the rendered `total` int."""
    rv = client.get("/search", query_string=qs)
    assert rv.status_code == 200, rv.data[:500]
    m = re.search(rb"(\d[\d,]*)\s+(?:card|cards|printing|printings)\s+found", rv.data)
    assert m, "could not locate result count in response"
    return int(m.group(1).replace(b",", b""))


@pytest.fixture(scope="module")
def client():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


def test_b3_colorless_at_most_via_route(client):
    """?color=C&color_rule=at_most must return colorless cards, not everything."""
    colorless = _distinct_names_where(
        "colors = '' AND language = 'English' AND (side IS NULL OR side = 'a')"
    )
    all_names = _distinct_names_where(
        "language = 'English' AND (side IS NULL OR side = 'a')"
    )
    assert 0 < colorless < all_names  # sanity on the DB shape

    total = _search_total(client, color="C", color_rule="at_most")
    # The bug returned all_names; the fix returns only colorless.
    assert total == colorless, (
        f"expected {colorless} colorless cards, got {total} (all={all_names})"
    )
    assert total < all_names


def test_b3_colorless_at_most_ci(client):
    """Same fix must apply to color identity (?ci=C&ci_rule=at_most)."""
    colorless_ci = _distinct_names_where(
        "colorIdentity = '' AND language = 'English' AND (side IS NULL OR side = 'a')"
    )
    total = _search_total(client, ci="C", ci_rule="at_most")
    assert total == colorless_ci


def test_b3_colorless_exact_and_at_least_unchanged(client):
    """exact and at_least rules for C-only must still work after the fix."""
    colorless = _distinct_names_where(
        "colors = '' AND language = 'English' AND (side IS NULL OR side = 'a')"
    )
    assert _search_total(client, color="C", color_rule="exact") == colorless
    assert _search_total(client, color="C", color_rule="at_least") == colorless


# --------------------------------------------------------------------------- #
# M1-M3 — deck-builder classification & type parsing
# --------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def js_src():
    return open("static/deck-builder.js").read()


def test_m1_draw_hate_excluded(js_src):
    """Draw rule must exclude draw-hate wording (can't draw / draw step / draw more than)."""
    assert "can'?t draw|draw step|draw more than" in js_src


def test_m2_getSectionKey_splits_on_comma(js_src):
    """getSectionKey must split the comma-separated types field on comma."""
    assert "split(',')" in js_src


def test_m3_shared_parse_types_helper(js_src):
    """A single parseTypes helper must be used by both classify and getSectionKey."""
    assert "function parseTypes" in js_src
    # Both call sites reference it:
    assert js_src.count("parseTypes(") >= 3  # def + classifyCard + getSectionKey


# --------------------------------------------------------------------------- #
# M4 — server-side deck validation documented
# --------------------------------------------------------------------------- #

def test_m4_save_documented():
    src = open("mtg/blueprints/decks.py").read()
    assert "no server-side validation" in src


# --------------------------------------------------------------------------- #
# Q1 — card-cell markup consistency
# --------------------------------------------------------------------------- #

def test_q1_search_uses_div_card_cell():
    src = open("templates/search.html").read()
    assert 'class="card-cell"' in src
    # No <a ... class="card-cell"> in search anymore:
    assert not re.search(r'<a\s+[^>]*class="card-cell"', src)


def test_q1_card_cell_cursor_pointer():
    css = open("static/style.css").read()
    m = re.search(r"\.card-cell\s*\{([^}]*)\}", css, re.DOTALL)
    assert m and "cursor: pointer" in m.group(1)


# --------------------------------------------------------------------------- #
# Q2 — sanitizer documented
# --------------------------------------------------------------------------- #

def test_q2_sanitizer_documented():
    # The sanitizer/path/collision logic is centralized in mtg.storage now.
    src = open("mtg/storage.py").read()
    assert "sanitize_filename" in src
    assert "resolve_json_path" in src


# --------------------------------------------------------------------------- #
# Q3 — per_page constant shared
# --------------------------------------------------------------------------- #

def test_q3_default_per_page_constant():
    assert DEFAULT_PER_PAGE == 30
    shared = open("mtg/shared.py").read()
    query = open("mtg/card_query.py").read()
    assert "DEFAULT_PER_PAGE = 30" in shared
    assert "DEFAULT_PER_PAGE" in query
    # No hardcoded 30 left in pagination_url:
    assert 'per_page=30"' not in shared


# --------------------------------------------------------------------------- #
# Q4 — getlist retained (no regression)
# --------------------------------------------------------------------------- #

def test_q4_rarity_games_use_getlist():
    src = open("mtg/card_query.py").read()
    assert '_getlist(args, "rarity")' in src
    assert '_getlist(args, "game")' in src


# --------------------------------------------------------------------------- #
# Q5 — deepdive suffix single source of truth
# --------------------------------------------------------------------------- #

def test_q5_deepdive_suffix_shared():
    assert DEEPDIVE_SUFFIX == "_deepdives.json"
    helpers = open("mtg/eval_helpers.py").read()
    landing = open("mtg/blueprints/eval_landing.py").read()
    shared = open("mtg/shared.py").read()
    assert "DEEPDIVE_SUFFIX" in shared
    assert "DEEPDIVE_SUFFIX" in helpers
    assert "DEEPDIVE_SUFFIX" in landing
    # The magic string literal is gone from both consumer sites:
    assert '"_deepdives.json"' not in helpers
    assert '"_deepdives.json"' not in landing
