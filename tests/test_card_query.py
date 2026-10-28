"""Phase 0 tests for the canonical search query builder.

These tests use a small fixture DB (see ``tests/fixtures.py``) rather than the
production 113 MB MTGJSON database. They pin the existing search semantics that
were moved out of ``mtg/blueprints/search.py``.
"""

from __future__ import annotations

import sqlite3

import pytest

import app as app_module
from mtg import shared
from mtg.card_query import build_search_query, empty_search_result, run_search
from tests.fixtures import build_search_db


@pytest.fixture()
def search_db_path(tmp_path):
    return build_search_db(tmp_path / "search.sqlite")


@pytest.fixture()
def search_db(search_db_path):
    conn = sqlite3.connect(search_db_path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def client(search_db_path, monkeypatch):
    monkeypatch.setattr(shared, "db_path", lambda *args, **kwargs: str(search_db_path))
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


def _run(db, **params):
    query = build_search_query(params)
    return query, run_search(db, query)


def _names(db, **params):
    _, result = _run(db, **params)
    return [row["name"] for row in result["results"]]


def _total(db, **params):
    _, result = _run(db, **params)
    return result["total"]


# --------------------------------------------------------------------------- #
# Empty / no-filter behavior
# --------------------------------------------------------------------------- #

def test_no_filters_returns_empty_state(search_db):
    query, result = _run(search_db)
    assert query.has_filters is False
    assert result["results"] == []
    assert result["total"] == 0
    assert result["total_pages"] == 0


def test_display_only_params_do_not_count_as_filters(search_db):
    query, result = _run(search_db, page=2, per_page=10)
    assert query.has_filters is False
    assert result["total"] == 0
    assert query.values["page"] == 2
    assert query.values["per_page"] == 10


def test_page_clamps_to_one(search_db):
    query, _ = _run(search_db, page=0)
    assert query.page == 1


def test_invalid_per_page_falls_back_to_default(search_db):
    query, _ = _run(search_db, per_page=999)
    assert query.per_page == shared.DEFAULT_PER_PAGE


# --------------------------------------------------------------------------- #
# Text filters
# --------------------------------------------------------------------------- #

def test_name_filter_is_front_face_substring(search_db):
    assert _names(search_db, name="Sol") == ["Sol Ring"]
    assert _names(search_db, name="Cecil") == ["Cecil, Dark Knight"]


def test_free_text_searches_name_type_and_text(search_db):
    names = _names(search_db, q="artifact")
    assert "Sol Ring" in names
    assert "Reserved Test" in names
    # "artifact" appears in Alela's oracle text.
    assert "Alela, Artful Provocateur" in names


def test_oracle_filter(search_db):
    assert _names(search_db, oracle="damage to any target") == ["Lightning Bolt"]


def test_type_line_normalizes_em_dash(search_db):
    names = _names(search_db, type_line="Creature - Human")
    assert "Thalia, Guardian of Thraben" in names
    assert "Cecil, Dark Knight" in names


def test_mana_cost_exact(search_db):
    names = _names(search_db, mana_cost="{1}")
    assert "Sol Ring" in names
    assert "Test 100% Card" in names
    assert "Lightning Bolt" not in names


def test_keywords_and_logic(search_db):
    assert _names(search_db, keywords="Flying") == ["Alela, Artful Provocateur"]
    assert _names(search_db, keywords="Flying,Deathtouch") == [
        "Alela, Artful Provocateur"
    ]
    assert _names(search_db, keywords="Flying,First strike") == []


def test_like_wildcards_are_escaped(search_db):
    # If "%" were not escaped, this would also match "1000 Test".
    assert _names(search_db, name="100%") == ["Test 100% Card"]


# --------------------------------------------------------------------------- #
# Color filters
# --------------------------------------------------------------------------- #

def test_color_at_least(search_db):
    names = _names(search_db, color="W", color_rule="at_least")
    assert "Wrath of God" in names
    assert "Thalia, Guardian of Thraben" in names
    assert "Alela, Artful Provocateur" in names
    assert "Sol Ring" not in names


def test_color_exact(search_db):
    assert set(_names(search_db, color="W", color_rule="exact")) == {
        "Wrath of God",
        "Thalia, Guardian of Thraben",
    }


def test_colorless_exact_and_at_most(search_db):
    exact = set(_names(search_db, color="C", color_rule="exact"))
    at_most = set(_names(search_db, color="C", color_rule="at_most"))
    assert "Sol Ring" in exact
    assert "Wrath of God" not in exact
    assert exact == at_most


def test_color_identity_exact_uses_alphabetical_db_format(search_db):
    assert _names(search_db, ci="BW", ci_rule="exact") == [
        "Cecil, Dark Knight",
        "Tymna the Weaver",
    ]


def test_color_identity_at_least(search_db):
    names = _names(search_db, ci="B", ci_rule="at_least")
    assert "Alela, Artful Provocateur" in names
    assert "Cecil, Dark Knight" in names
    assert "Lolth, Spider Queen" in names
    assert "Wrath of God" not in names


def test_color_identity_at_most_excludes_off_colors(search_db):
    names = _names(search_db, ci="W", ci_rule="at_most")
    assert "Wrath of God" in names
    assert "Thalia, Guardian of Thraben" in names
    assert "Alela, Artful Provocateur" not in names
    assert "Counterspell" not in names


# --------------------------------------------------------------------------- #
# Stats
# --------------------------------------------------------------------------- #

def test_mana_value_range(search_db):
    names = _names(search_db, mv="1-2")
    assert "Sol Ring" in names
    assert "Lightning Bolt" in names
    assert "Thalia, Guardian of Thraben" in names
    assert "Wrath of God" not in names


def test_mana_value_operator(search_db):
    assert "Wrath of God" in _names(search_db, mv="4", mv_op=">=")
    assert "Sol Ring" not in _names(search_db, mv="4", mv_op=">=")


def test_star_power(search_db):
    assert _names(search_db, pow="*") == ["Aeve, Progenitor Ooze"]


def test_power_and_toughness_cast(search_db):
    assert "Cecil, Dark Knight" in _names(
        search_db, pow="3", pow_op=">="
    )
    assert "Alela, Artful Provocateur" in _names(search_db, tou="3", tou_op=">=")


def test_loyalty(search_db):
    assert _names(search_db, loy="4") == ["Lolth, Spider Queen"]


# --------------------------------------------------------------------------- #
# Rarity / set / legality / game
# --------------------------------------------------------------------------- #

def test_rarity_multi_select(search_db):
    names = _names(search_db, rarity=["mythic", "special"])
    assert "Alela, Artful Provocateur" in names
    assert "Lolth, Spider Queen" in names
    assert "Test 100% Card" in names
    assert "Sol Ring" not in names


def test_set_filter(search_db):
    assert _total(search_db, set="LTC") == 2
    assert _total(search_db, set="TST") == 17


def test_format_legality_banned(search_db):
    assert _names(search_db, format="legacy", legality="banned") == ["Sol Ring"]


def test_format_legality_legal(search_db):
    names = _names(search_db, format="modern", legality="legal")
    assert "Lightning Bolt" in names
    assert "Wrath of God" in names


def test_unknown_format_warns_and_does_not_break(search_db):
    query, result = _run(search_db, format="not-a-format")
    assert any("Unknown format" in warning for warning in result["warnings"])
    assert result["total"] > 0  # invalid format does not add a WHERE clause
    assert "cards" in query.count_sql


def test_game_availability(search_db):
    assert _names(search_db, game="arena") == ["Test 100% Card"]


# --------------------------------------------------------------------------- #
# Flags / presentation
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "param, expected",
    [
        ("is_funny", "Test 100% Card"),
        ("is_promo", "Test 100% Card"),
        ("is_fullart", "Test 100% Card"),
        ("is_textless", "Test 100% Card"),
        ("is_rebalanced", "Test 100% Card"),
        ("is_reprint", "Reprint Test"),
        ("is_reserved", "Reserved Test"),
        ("is_oversized", "Oversized Test"),
    ],
)
def test_boolean_flags(search_db, param, expected):
    assert _names(search_db, **{param: "1"}) == [expected]


def test_border_layout_frame(search_db):
    assert _names(search_db, border="borderless") == ["Test 100% Card"]
    assert _names(search_db, layout="saga") == ["Test 100% Card"]
    assert _names(search_db, frame="future") == ["Test 100% Card"]


# --------------------------------------------------------------------------- #
# DFC / grouping / pagination
# --------------------------------------------------------------------------- #

def test_dfc_counts_once_and_uses_front_name(search_db):
    query, result = _run(search_db, name="Cecil")
    assert result["total"] == 1
    assert len(result["results"]) == 1
    assert result["results"][0]["name"] == "Cecil, Dark Knight"
    assert query.group_expr == query.name_expr


def test_pagination_math(search_db):
    query, page1 = _run(search_db, set="TST", per_page=10, page=1)
    _, page2 = _run(search_db, set="TST", per_page=10, page=2)
    assert page1["total"] == 17
    assert page1["total_pages"] == 2
    assert len(page1["results"]) == 10
    assert len(page2["results"]) == 7
    assert not set(r["name"] for r in page1["results"]) & set(
        r["name"] for r in page2["results"]
    )


# --------------------------------------------------------------------------- #
# Route integration
# --------------------------------------------------------------------------- #

def test_search_route_uses_fixture_db(client):
    response = client.get("/search", query_string={"name": "Sol"})
    assert response.status_code == 200
    assert b"Sol Ring" in response.data
    assert b"Wrath of God" not in response.data


def test_search_route_empty_state(client):
    response = client.get("/search")
    assert response.status_code == 200
    assert b"Enter a search query" in response.data
    assert b"Sol Ring" not in response.data


def test_search_route_pagination(client):
    response = client.get("/search", query_string={"set": "TST", "per_page": 10})
    assert response.status_code == 200
    assert b"17 cards found" in response.data
    assert b"Page" in response.data


def test_search_route_no_db_banner(client, search_db_path, monkeypatch):
    # Point the fixture at a non-existent DB to exercise the unseeded path.
    missing = search_db_path.parent / "missing.sqlite"
    monkeypatch.setattr(shared, "db_path", lambda *args, **kwargs: str(missing))
    response = client.get("/search", query_string={"name": "Sol"})
    assert response.status_code == 200
    assert b"No card database found" in response.data
    assert b"Enter a search query" in response.data


def test_empty_search_result_shape(search_db):
    query = build_search_query({})
    result = empty_search_result(query)
    assert result["results"] == []
    assert result["total"] == 0
    assert result["total_pages"] == 0
    assert result["has_filters"] is False


def test_sql_injection_attempt_is_parameterized(search_db):
    assert _names(search_db, name="' OR 1=1 --") == []
    # The table still exists and the schema is intact.
    assert search_db.execute("SELECT COUNT(*) FROM cards").fetchone()[0] > 0


# --------------------------------------------------------------------------- #
# Search 2.0 filters
# --------------------------------------------------------------------------- #

def test_game_changer_filter(search_db):
    assert _names(search_db, game_changer="1") == ["Test 100% Card"]


def test_edhrec_rank_filters(search_db):
    top = _names(search_db, edhrec_max=10)
    assert "Sol Ring" in top
    assert "Rhystic Study" in top
    assert "Wrath of God" not in top

    high = _names(search_db, edhrec_min=1000)
    assert "Alela, Artful Provocateur" in high
    assert "Wrath of God" in high
    assert "Sol Ring" not in high


def test_salt_filters(search_db):
    assert _names(search_db, salt_min=1) == ["Rhystic Study"]
    assert set(_names(search_db, salt_max=0.5)) == {
        "Sol Ring",
        "Alela, Artful Provocateur",
    }


def test_artist_and_watermark_filters(search_db):
    assert _names(search_db, artist="Mark") == ["Sol Ring"]
    assert _names(search_db, watermark="Prophecy") == ["Rhystic Study"]


def test_set_type_filter(search_db):
    names = _names(search_db, set_type="commander")
    assert "Sol Ring" in names
    assert "Cecil, Dark Knight" in names
    assert "Tymna the Weaver" in names
    assert "Wrath of God" not in names


def test_release_date_filters(search_db):
    names = _names(search_db, release_after="2020-01-01")
    assert "Sol Ring" in names
    assert "Wrath of God" not in names
    assert "Rhystic Study" not in names

    old = _names(search_db, release_before="2010-01-01")
    assert "Wrath of God" in old
    assert "Rhystic Study" in old
    assert "Lightning Bolt" not in old


def test_produced_mana_and_finish_filters(search_db):
    assert _names(search_db, produced_mana="C") == ["Sol Ring"]
    assert _names(search_db, finish="etched") == ["Test 100% Card"]
    nonfoil = set(_names(search_db, finish="nonfoil"))
    assert {"Nonfoil Only", "Sol Ring", "Rhystic Study"} <= nonfoil
    assert "Nonfoil Only" not in _names(search_db, finish="foil")


def test_commander_eligibility_filters(search_db):
    expected = {
        "Alela, Artful Provocateur",
        "Cecil, Dark Knight",
        "Tymna the Weaver",
    }
    assert set(_names(search_db, can_be_commander="1")) == expected
    assert set(_names(search_db, commander_only="1")) == expected


def test_partner_filter(search_db):
    assert _names(search_db, partner="1") == ["Tymna the Weaver"]


def test_new_flag_filters(search_db):
    assert _names(search_db, is_timeshifted="1") == ["Test 100% Card"]
    assert _names(search_db, is_story_spotlight="1") == ["Test 100% Card"]
    assert _names(search_db, has_content_warning="1") == ["Test 100% Card"]


# --------------------------------------------------------------------------- #
# Search 2.0 sorting / API shape
# --------------------------------------------------------------------------- #

def test_sort_edhrec_puts_null_ranks_last(search_db):
    names = _names(search_db, set_type="commander", sort="edhrec")
    assert names == ["Sol Ring", "Tymna the Weaver", "Cecil, Dark Knight"]


def test_sort_salt_descending(search_db):
    names = _names(search_db, q="draw", sort="salt")
    assert names[0] == "Rhystic Study"


def test_sort_mv_ascending(search_db):
    names = _names(search_db, set_type="commander", sort="mv")
    assert names[0] == "Sol Ring"


def test_sort_newest_descending(search_db):
    names = _names(search_db, set_type="commander", sort="newest")
    assert names[0] in ("Sol Ring", "Cecil, Dark Knight")
    assert names[-1] == "Tymna the Weaver"


def test_sort_rarity_default_descending(search_db):
    names = _names(search_db, set_type="commander", sort="rarity")
    assert names[-1] == "Sol Ring"  # uncommon behind the rares


def test_sort_name_direction(search_db):
    asc = _names(search_db, set_type="commander", sort="name", order="asc")
    desc = _names(search_db, set_type="commander", sort="name", order="desc")
    assert asc == list(reversed(desc))


def test_random_sort_is_a_sample(search_db):
    query, result = _run(search_db, set_type="commander", sort="random")
    assert result["total"] == 3
    assert result["total_pages"] == 1
    assert result["random_sample"] is True
    assert len(result["results"]) == 3


def test_invalid_sort_falls_back_with_warning(search_db):
    query, result = _run(search_db, set_type="commander", sort="bogus")
    assert query.values["sort"] == "name"
    assert any("Unknown sort" in warning for warning in result["warnings"])


def test_api_result_shape(search_db):
    from mtg.card_query import result_to_api

    query = build_search_query({"name": "Sol Ring"})
    result = run_search(search_db, query)
    payload = result_to_api(query, result)
    assert payload["total"] == 1
    assert payload["schemaVersion"] == 1
    card = payload["cards"][0]
    assert card["name"] == "Sol Ring"
    assert card["fullName"] == "Sol Ring"
    assert card["imageUrl"].startswith("/img/normal/front/")
    assert card["isGameChanger"] is False
    assert card["types"] == "Artifact"
