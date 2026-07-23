"""Tests for shared.py utility functions."""

import os
import tempfile

import pytest

from shared import db_path, color_identity_subset, resolve_bind_path


class TestResolveBindPath:
    def test_regular_file_returns_itself(self):
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"hello")
            path = f.name
        try:
            assert resolve_bind_path(path) == path
        finally:
            os.unlink(path)

    def test_directory_joins_basename(self):
        with tempfile.TemporaryDirectory() as d:
            result = resolve_bind_path(d)
            assert result == os.path.join(d, os.path.basename(d))

    def test_directory_with_fallback_basename(self):
        with tempfile.TemporaryDirectory() as d:
            result = resolve_bind_path(d, fallback_basename=".secret_key")
            assert result == os.path.join(d, ".secret_key")

    def test_nonexistent_returns_itself(self):
        assert resolve_bind_path("/nonexistent/path/foo.txt") == "/nonexistent/path/foo.txt"


class TestDbPath:
    def test_file_returns_itself(self):
        with tempfile.NamedTemporaryFile(suffix=".sqlite", delete=False) as f:
            path = f.name
        try:
            assert db_path(path) == path
        finally:
            os.unlink(path)

    def test_directory_picks_newest_sqlite(self):
        with tempfile.TemporaryDirectory() as d:
            # Create two .sqlite files with different mtimes
            old = os.path.join(d, "old.sqlite")
            new = os.path.join(d, "new.sqlite")
            with open(old, "w") as f:
                f.write("old")
            os.utime(old, (0, 0))  # epoch
            with open(new, "w") as f:
                f.write("new")
            assert db_path(d) == new

    def test_directory_ignores_non_sqlite(self):
        with tempfile.TemporaryDirectory() as d:
            with open(os.path.join(d, "readme.txt"), "w") as f:
                f.write("not a db")
            assert db_path(d) == d  # no .sqlite files found

    def test_default_returns_nonexistent(self):
        path = db_path("/nonexistent/path.sqlite")
        assert path == "/nonexistent/path.sqlite"


class TestColorIdentitySubset:
    # --- String path (app.py) ---

    def test_empty_card_ci_always_true(self):
        assert color_identity_subset("", "W, U") is True
        assert color_identity_subset("  ", "R") is True

    def test_card_in_commander_ci(self):
        assert color_identity_subset("W", "W, U") is True
        assert color_identity_subset("U, R", "W, U, B, R, G") is True

    def test_card_not_in_commander_ci(self):
        assert color_identity_subset("B", "W, U") is False
        assert color_identity_subset("U, B", "R") is False

    def test_card_equal_to_commander_ci(self):
        assert color_identity_subset("W, U", "W, U") is True

    def test_card_ci_larger_than_commander_ci(self):
        assert color_identity_subset("W, U, B", "W, U") is False

    def test_empty_commander_ci(self):
        # Empty commander CI → only colorless cards are legal
        assert color_identity_subset("W", "") is False
        assert color_identity_subset("", "") is True

    def test_db_format_ci_with_commas_and_spaces(self):
        assert color_identity_subset("W, U", "W, U, B") is True

    # --- Set path (mcp_server) ---

    def test_set_path_empty_card(self):
        assert color_identity_subset("", {"W", "U"}) is True
        assert color_identity_subset("  ", set()) is True

    def test_set_path_card_in_allowed(self):
        assert color_identity_subset("W", {"W", "U", "B"}) is True
        assert color_identity_subset("G, W", {"W", "U", "B", "R", "G"}) is True

    def test_set_path_card_not_in_allowed(self):
        assert color_identity_subset("R", {"W", "U"}) is False

    def test_set_path_empty_allowed_set(self):
        assert color_identity_subset("W", set()) is False
        assert color_identity_subset("", set()) is True

    def test_set_path_comma_formatted_ci(self):
        assert color_identity_subset("W, U", {"W", "U", "B"}) is True
