"""Tests for llm.py _parse_json — pure string-to-dict, no network."""

import json

import pytest

from mtg.llm import _parse_json


class TestParseJson:
    def test_valid_json(self):
        result = _parse_json('{"foo": "bar", "num": 42}')
        assert result == {"foo": "bar", "num": 42}

    def test_empty_string_raises(self):
        with pytest.raises(ValueError, match="empty"):
            _parse_json("")

    def test_none_raises(self):
        with pytest.raises(ValueError, match="empty"):
            _parse_json(None)

    def test_strips_markdown_fences(self):
        result = _parse_json('```json\n{"key": "value"}\n```')
        assert result == {"key": "value"}

    def test_strips_markdown_fences_no_language(self):
        result = _parse_json('```\n{"key": "value"}\n```')
        assert result == {"key": "value"}

    def test_fixes_trailing_comma(self):
        result = _parse_json('{"key": "value",}')
        assert result == {"key": "value"}

    def test_fixes_trailing_comma_in_array(self):
        result = _parse_json('["a", "b",]')
        assert result == ["a", "b"]

    def test_missing_commas_between_strings(self):
        result = _parse_json('{"a": "1"\n"b": "2"}')
        assert result == {"a": "1", "b": "2"}

    def test_unescaped_newlines_in_strings(self):
        result = _parse_json('{"text": "line 1\nline 2"}')
        assert result == {"text": "line 1\nline 2"}

    def test_invalid_json_raises(self):
        with pytest.raises(json.JSONDecodeError):
            _parse_json('not json at all')

    def test_realistic_commander_output(self):
        """Simulate a mildly-malformed LLM output for a commander eval."""
        text = (
            '{\n'
            '  "strengths": [\n'
            '    "Token generation is efficient"\n'
            '    "Strong late-game inevitability"\n'
            '  ],\n'
            '  "weaknesses": [\n'
            '    "Vulnerable to board wipes",\n'
            '    "Slow to set up"\n'
            '  ]\n'
            '}'
        )
        result = _parse_json(text)
        assert len(result["strengths"]) == 2
        assert len(result["weaknesses"]) == 2
        assert "Token generation is efficient" in result["strengths"]
        assert "Slow to set up" in result["weaknesses"]
