"""Tests for mana_symbols (HTML template helper)."""

from mtg.images import mana_symbols


class TestManaSymbols:
    def test_empty(self):
        assert mana_symbols("") == ""
        assert mana_symbols(None) == ""

    def test_single_symbol(self):
        result = mana_symbols("{W}")
        assert 'ms-w' in result
        assert '{W}' not in result

    def test_multiple_symbols(self):
        result = mana_symbols("{1}{W}{U}")
        assert 'ms-1' in result
        assert 'ms-w' in result
        assert 'ms-u' in result

    def test_hybrid(self):
        result = mana_symbols("{W/U}")
        assert 'ms-wu' in result

    def test_phyrexian(self):
        result = mana_symbols("{W/P}")
        assert 'ms-wp' in result

    def test_generic(self):
        result = mana_symbols("{X}")
        assert 'ms-X' in result
        result = mana_symbols("{10}")
        assert 'ms-10' in result

    def test_tap_symbol(self):
        result = mana_symbols("{T}")
        assert 'ms-tap' in result

    def test_energy(self):
        result = mana_symbols("{E}")
        assert 'ms-e' in result

    def test_mixed_known_and_generic(self):
        result = mana_symbols("{2}{G}{G}")
        assert 'ms-2' in result
        assert 'ms-g' in result
