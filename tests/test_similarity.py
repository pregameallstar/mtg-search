"""Tests for extract_terms, split_csv, gate, and score_similarity.

These are pure-logic functions imported directly from the similarity module.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from similarity import (
    MTG_STOP_WORDS,
    KEYWORD_TERMS,
    extract_terms,
    split_csv,
    gate,
    score_similarity,
    normalize_dash,
    wubrg_sort,
)


class TestExtractTerms:
    def test_empty(self):
        assert extract_terms("") == set()
        assert extract_terms(None) == set()

    def test_basic_tokenization(self):
        terms = extract_terms("Destroy target creature.")
        assert "destroy" in terms
        assert "target" in terms
        assert "creature" in terms  # no stemming — "creature" doesn't end in 's'

    def test_reminder_text_stripped(self):
        terms = extract_terms(
            "Flying (This creature can't be blocked except by creatures with flying or reach.)"
        )
        assert "fly" not in terms  # keyword stripped
        assert "block" not in terms  # reminder stripped

    def test_stop_words_filtered(self):
        terms = extract_terms("the and of to a an in it you your")
        assert all(len(t) >= 3 for t in terms)

    def test_plural_stemming(self):
        terms = extract_terms("zombies tokens")
        # "zombies" → -ies stem → "zomby"
        assert "zomby" in terms
        # "tokens" ends in "s", not "ss"/"es", len >= 5 → stem to "token"
        assert "token" in terms

    def test_keyword_terms_stripped(self):
        terms = extract_terms("Flying, first strike, trample")
        assert "fly" not in terms
        assert "trampl" not in terms

    def test_mtg_filter_strips_domain_words(self):
        terms = extract_terms(
            "creature target player card spell", mtg_filter=True
        )
        assert "creatur" not in terms
        assert "player" not in terms
        assert "target" not in terms

    def test_mtg_filter_keeps_mechanical_terms(self):
        terms = extract_terms(
            "whenever sacrifice a creature draw a card", mtg_filter=True
        )
        # "sacrifice" is in MTG_STOP_WORDS, "creature" is in MTG_STOP_WORDS,
        # "draw" is in MTG_STOP_WORDS, "card" is in MTG_STOP_WORDS
        # "whenever" survives — it's not a stop word
        assert "whenever" in terms

    def test_short_tokens_filtered(self):
        terms = extract_terms("a b c ab cd ef")
        assert all(len(t) >= 3 for t in terms)

    def test_ies_plural_stemming(self):
        terms = extract_terms("counters")
        # "counters" → "counter"
        assert "counter" in terms


class TestSplitCsv:
    def test_empty(self):
        assert split_csv("") == set()
        assert split_csv(None) == set()

    def test_basic(self):
        result = split_csv("Creature, Artifact, Enchantment")
        assert result == {"creature", "artifact", "enchantment"}

    def test_whitespace(self):
        result = split_csv(" W ,  U , B ")
        assert result == {"w", "u", "b"}


class TestGate:
    def test_perfect_match(self):
        assert gate("s_types", 0.0, {"s_types": 8.0, "w_types": 1.0}) == 1.0

    def test_partial_mismatch(self):
        result = gate("s_types", 0.5, {"s_types": 8.0, "w_types": 1.0})
        # 1 / (1 + 1.0 * 8.0 * 0.5) = 1 / 5.0 = 0.2
        assert round(result, 4) == 0.2

    def test_full_mismatch(self):
        result = gate("s_types", 1.0, {"s_types": 8.0, "w_types": 1.0})
        assert round(result, 4) == round(1.0 / 9.0, 4)

    def test_custom_weight(self):
        result = gate("s_types", 0.5, {"s_types": 4.0, "w_types": 2.0})
        assert round(result, 4) == 0.2


class TestScoreSimilarity:
    def test_identical_cards_score_near_perfect(self):
        base = {
            "_terms": extract_terms("Destroy target creature."),
            "manaValue": 3,
            "_t": split_csv("Instant"),
            "_kw": set(),
            "_sub": set(),
            "_st": set(),
            "_ci": set(),
        }
        cand = {
            "name": "Murder",
            "text": "Destroy target creature.",
            "types": "Instant", "keywords": "", "subtypes": "",
            "supertypes": "", "colorIdentity": "", "manaValue": 3,
        }
        idf = {"destroy": 0.5, "target": 0.3, "creatur": 0.4}
        base["_idf_sum"] = sum(idf.get(t, 0) for t in base["_terms"])
        score = score_similarity(base, cand, {}, idf)
        assert score > 0.99

    def test_no_overlap_zero_score(self):
        base = {
            "_terms": extract_terms("Destroy target creature."),
            "manaValue": 3,
        }
        cand = {
            "name": "Healing Salve", "text": "Gain 3 life.",
            "types": "Instant", "keywords": "", "subtypes": "",
            "supertypes": "", "colorIdentity": "W", "manaValue": 1,
        }
        idf = {"destroy": 0.5, "target": 0.3, "creatur": 0.4}
        base["_idf_sum"] = sum(idf.get(t, 0) for t in base["_terms"])
        score = score_similarity(base, cand, {}, idf)
        assert score == 0.0

    def test_types_gate_penalizes_different_types(self):
        base = {
            "_terms": extract_terms("Draw a card."),
            "manaValue": 1,
            "_t": split_csv("Instant"),
        }
        cand_same = {
            "name": "Brainstorm", "text": "Draw three cards.",
            "types": "Instant", "keywords": "", "subtypes": "",
            "supertypes": "", "colorIdentity": "U", "manaValue": 1,
        }
        cand_diff = {
            "name": "Divination", "text": "Draw two cards.",
            "types": "Sorcery", "keywords": "", "subtypes": "",
            "supertypes": "", "colorIdentity": "U", "manaValue": 3,
        }
        idf = {"draw": 0.5, "card": 0.3, "three": 0.1, "two": 0.1}
        base["_idf_sum"] = sum(idf.get(t, 0) for t in base["_terms"])
        factors = {"use_types": True, "s_types": 8.0}
        score_same = score_similarity(base, cand_same, factors, idf)
        score_diff = score_similarity(base, cand_diff, factors, idf)
        assert score_same > score_diff

    def test_color_strict_tier_blocks_mismatch(self):
        base = {
            "_terms": extract_terms("Deal 3 damage to any target."),
            "manaValue": 1,
            "_ci": {"r"},
        }
        cand = {
            "name": "Lightning Bolt",
            "text": "Deal 3 damage to any target.",
            "types": "Instant", "keywords": "", "subtypes": "",
            "supertypes": "", "colorIdentity": "R", "manaValue": 1,
        }
        idf = {"deal": 0.5, "damag": 0.4, "target": 0.3}
        base["_idf_sum"] = sum(idf.get(t, 0) for t in base["_terms"])
        factors = {"use_color": True, "s_color": 200.0}
        score = score_similarity(base, cand, factors, idf)
        assert score > 0.99
        cand["colorIdentity"] = "W"
        assert score_similarity(base, cand, factors, idf) == 0.0

    def test_oracle_strictness_exponentiates_partial_overlap(self):
        # Base has 3 terms, candidate only overlaps on 2 of them
        base = {
            "_terms": extract_terms("Destroy target creature."),
            "manaValue": 3,
        }
        cand = {
            "name": "Terminate",
            "text": "Destroy target creature.",  # perfect match for oracle text
            "types": "Instant", "keywords": "", "subtypes": "",
            "supertypes": "", "colorIdentity": "B,R", "manaValue": 2,
        }
        cand_partial = {
            "name": "Murder",
            "text": "Destroy.",
            "types": "Instant", "keywords": "", "subtypes": "",
            "supertypes": "", "colorIdentity": "B", "manaValue": 3,
        }
        idf = {"destroy": 0.5, "target": 0.3, "creature": 0.4}
        base["_idf_sum"] = sum(idf.get(t, 0) for t in base["_terms"])

        # Full match: oracle = 1.0 regardless of exponent
        score_full_loose = score_similarity(base, cand, {"s_oracle": 0.5}, idf)
        score_full_strict = score_similarity(base, cand, {"s_oracle": 2.0}, idf)
        assert score_full_loose == 1.0 and score_full_strict == 1.0  # 1^x = 1

        # Partial match: oracle < 1.0 — strictness reduces it further
        score_part_loose = score_similarity(base, cand_partial, {"s_oracle": 0.5}, idf)
        score_part_strict = score_similarity(base, cand_partial, {"s_oracle": 2.0}, idf)
        # sqrt of <1.0 > square of <1.0
        assert score_part_loose > score_part_strict

    def test_mv_gate_penalizes_cost_difference(self):
        base = {"_terms": extract_terms("Draw a card."), "manaValue": 1}
        cand_small = {
            "name": "Brainstorm", "text": "Draw three cards.",
            "types": "Instant", "keywords": "", "subtypes": "",
            "supertypes": "", "colorIdentity": "U", "manaValue": 1,
        }
        cand_big = {
            "name": "Dragon", "text": "Draw a card when this enters.",
            "types": "Creature", "keywords": "Flying", "subtypes": "Dragon",
            "supertypes": "", "colorIdentity": "R", "manaValue": 7,
        }
        idf = {"draw": 0.5, "card": 0.3, "three": 0.1, "enter": 0.1}
        base["_idf_sum"] = sum(idf.get(t, 0) for t in base["_terms"])
        factors = {"use_mv": True, "s_mv": 8.0}
        score_small = score_similarity(base, cand_small, factors, idf)
        score_big = score_similarity(base, cand_big, factors, idf)
        assert score_small > score_big


class TestNormalizeDash:
    def test_em_dash(self):
        assert normalize_dash("Legendary Creature — Elf") == "Legendary Creature - Elf"

    def test_en_dash(self):
        assert normalize_dash("Artifact – Equipment") == "Artifact - Equipment"

    def test_hyphen_unchanged(self):
        assert normalize_dash("Target - Creature") == "Target - Creature"

    def test_mixed(self):
        assert normalize_dash("A—B–C-D") == "A-B-C-D"


class TestWubrgSort:
    def test_partial_colors(self):
        result = wubrg_sort(["R", "U", "G"])
        assert result == ["U", "R", "G"]

    def test_all_five(self):
        assert wubrg_sort(["G", "R", "B", "U", "W"]) == ["W", "U", "B", "R", "G"]

    def test_single_color(self):
        assert wubrg_sort(["G"]) == ["G"]
