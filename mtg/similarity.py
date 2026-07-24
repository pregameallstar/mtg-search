"""Card similarity scoring engine — TF-IDF with configurable gating.

ponytail: pure functions, no Flask dependency. IDF cache is module-level.
Takes db_path as argument rather than reaching for a global DATABASE.
"""

import re
import math
import sqlite3

# --- Public API ---


def similarity_label(score, best_score=None):
    """Rank-based label: score relative to the best match for this card.

    Without best_score, uses absolute thresholds (legacy fallback).
    With best_score, uses relative ranking — the top match is always meaningful."""
    if best_score and best_score > 0:
        pct = score / best_score
        if pct >= 0.85:
            return "Near Perfect"
        elif pct >= 0.65:
            return "Strong"
        elif pct >= 0.40:
            return "Good"
        elif pct >= 0.15:
            return "Bad"
        return "Ignore"
    # ponytail: absolute fallback — used when best_score is unavailable
    if score >= 45:
        return "Near Perfect"
    elif score >= 35:
        return "Strong"
    elif score >= 25:
        return "Good"
    elif score >= 15:
        return "Bad"
    return "Ignore"


# --- Data constants ---

# ponytail: MTG-domain terms that appear on most cards and drown out functional signal.
MTG_STOP_WORDS = {
    "creature", "target", "player", "card", "spell", "control", "battlefield",
    "graveyard", "hand", "library", "opponent", "end", "step", "turn",
    "draw", "cast", "permanent", "damage", "counter", "land", "token",
    "artifact", "enchantment", "destroy", "exile", "sacrifice", "return",
    "phase", "combat", "beginning", "upkeep", "untap", "source",
    "yourself", "owner", "shuffle", "search", "reveal", "choose",
    "enters", "leaves", "activate", "ability", "abilities",
    # ponytail: common -es plurals that stemming skips to avoid mangling
    # verbs like "does"/"goes". Without these, the plural forms survive
    # the stop-word filter as distinct terms.
    "sacrifices", "searches", "creatures", "damages", "targets",
    "spells", "tokens", "destroys", "returns", "reveals", "counters",
}

# ponytail: keyword abilities are mechanical labels, not functional signals.
# A card with "flying, menace" and a card with just "menace" aren't similar;
# matching on keywords drowns out the oracle text signal that actually matters.
KEYWORD_TERMS = {
    "menace", "flying", "trample", "haste", "vigilance", "lifelink",
    "deathtouch", "reach", "defender", "flash", "double", "strike",
    "indestructible", "hexproof", "prowess", "ward", "crew", "equip",
    "cycling", "kicker", "scry", "surveil", "explore", "connive",
    "connives", "morph", "disguise", "ninjutsu", "mutate", "bestow",
    "suspend", "vanishing", "fading", "ripple", "storm", "dredge",
    "unearth", "annihilator", "bushido", "convoke", "delve", "devour",
    "exalted", "fear", "flanking", "intimidate", "persist", "undying",
    "wither", "infect", "proliferate", "populate", "fuse", "overload",
    "strive", "conspire", "cipher", "extort", "haunt", "bloodthirst",
    "graft", "evolve", "fabricate", "afflict", "aftermath", "boast",
    "cascade", "changeling", "dethrone", "foretell", "escape", "encore",
    "reconfigure", "offspring", "impending", "plot", "goad", "adapt",
    "battalion", "hideaway", "amplify", "heroic", "inspired", "landfall",
    "metalcraft", "raid", "revolt", "threshold", "embalm", "eternalize",
    "ascend", "surge", "spectacle", "riots", "chroma", "domain", "kinship",
    "splice", "transmute", "transfigure", "soulbond", "chronic",
}

_WUBRG_ORDER = {c: i for i, c in enumerate("WUBRG")}

# --- IDF cache (module-level) ---

_idf_cache = None
_idf_card_count = 0
_idf_version = 0  # bump when extract_terms changes to force cache rebuild


def get_idf(db_path):
    """Return {term: log(total_docs / doc_freq)} for all oracle terms.

    Computed once per server lifetime; rebuilt if card count drifts or
    extraction logic changes.
    """
    global _idf_cache, _idf_card_count, _idf_version
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    total = db.execute(
        "SELECT COUNT(DISTINCT name) FROM cards WHERE language='English' AND (side IS NULL OR side='a')"
    ).fetchone()[0]
    if _idf_cache is not None and abs(total - _idf_card_count) <= 100 and _idf_version >= 2:
        db.close()
        return _idf_cache

    text_rows = db.execute(
        "SELECT COALESCE(c.text, '') as text FROM cards c "
        "WHERE c.language='English' AND (c.side IS NULL OR c.side='a') "
        "GROUP BY c.name"
    ).fetchall()
    db.close()

    df = {}
    for row in text_rows:
        terms = extract_terms(row["text"])
        for t in terms:
            df[t] = df.get(t, 0) + 1

    _idf_cache = {t: math.log(total / freq) for t, freq in df.items()}
    _idf_card_count = total
    _idf_version = 2
    return _idf_cache


def extract_terms(text, mtg_filter=False):
    """Tokenize oracle text into significant lowercase terms.

    Strips reminder text and keyword-ability labels, filters stop words
    and short tokens.  When mtg_filter is True, also strips ubiquitous
    MTG-domain words."""
    if not text:
        return set()
    stripped = re.sub(r'\([^)]*\)', '', text)
    words = re.findall(r'[a-zA-Z]+', stripped.lower())
    stop_words = {
        'the', 'a', 'an', 'of', 'in', 'to', 'is', 'it', 'you', 'your', 'its',
        'that', 'this', 'and', 'or', 'for', 'on', 'at', 'be', 'by', 'as',
        'if', 'no', 'not', 'with', 'from', 'can', 'may', 'has', 'have',
        'are', 'was', 'were', 'been', 'each', 'any', 'all', 'their', 'they',
        'them', 'then', 'than', 'when', 'where', 'which', 'who', 'will', 'would',
        'put', 'onto', 'create', 'one', 'two', 'three',
        # ponytail: quantifiers/pronouns — carry zero functional signal.
        'many', 'gets', 'also', 'other', 'instead', 'another', 'those',
        'these', 'more', 'only', 'once',
    }
    if mtg_filter:
        stop_words |= MTG_STOP_WORDS

    terms = set()
    for w in words:
        if len(w) < 3:
            continue
        stem = w
        # ponytail: shallow plural stem — only strip a trailing 's' when both
        # forms are >= 4 chars and the word doesn't end in "ss" (e.g. "less").
        # Avoids truncating "this" → "thi", "gets" → "get".
        if w.endswith("s") and len(w) >= 5 and not w.endswith("ss") and not w.endswith("es"):
            stem = w[:-1]
        # Also handle "-ies" → "-y" (e.g. "counters" but not regular plurals)
        if w.endswith("ies") and len(w) >= 4:
            stem = w[:-3] + "y"
        if w in KEYWORD_TERMS or stem in KEYWORD_TERMS:
            continue
        if w in stop_words or stem in stop_words:
            continue
        terms.add(stem)
    return terms


def split_csv(raw):
    """Split comma-separated string into lowercased set."""
    return {t.strip().lower() for t in (raw or "").split(",") if t.strip()}


def gate(key, mismatch, factors):
    """1 / (1 + w * strictness * mismatch).

    Perfect match (mismatch=0) → gate=1.0. No penalty."""
    if mismatch <= 0:
        return 1.0
    w = float(factors.get(key.replace("s_", "w_"), 1))
    s = float(factors.get(key, 8.0))
    return 1.0 / (1.0 + w * s * mismatch)


def score_similarity(base, candidate, factors, idf, mtg_filter=False):
    """Compute similarity score with multiplicative gating via geometric mean.

    Oracle text overlap is the base signal (IDF-weighted, [0,1]).
    Each active factor acts as a *gate* — a penalty curve that multiplies
    the oracle score. Gates are combined via geometric mean so no single
    mismatched factor zeros the score:

        final = oracle * (g1 * g2 * ... * gn) ** (1/n)

    Where each gate is:  1 / (1 + w * strictness * mismatch)

    Base card fields are pre-split by the caller (as _t, _kw, _sub, _st, _ci)
    to avoid re-splitting for every candidate.
    """
    # --- Oracle text terms (always on, IDF-weighted) ---
    oracle_score = 0.0
    base_terms = base["_terms"]
    base_idf_sum = base.get("_idf_sum", 0.0)
    cand_terms = extract_terms(candidate["text"], mtg_filter=mtg_filter)
    if base_terms and base_idf_sum > 0:
        shared_idf_sum = sum(idf.get(t, 0) for t in (base_terms & cand_terms))
        oracle_score = shared_idf_sum / base_idf_sum

    # --- Oracle strictness: exponentiate the raw overlap ---
    s_oracle = float(factors.get("s_oracle", 1.0))
    oracle_score = oracle_score ** s_oracle

    gates = []

    # --- Types gate ---
    if factors.get("use_types"):
        base_t = base.get("_t")
        if base_t:
            cand_t = split_csv(candidate["types"])
            jaccard = len(base_t & cand_t) / len(base_t | cand_t)
            gates.append(gate("s_types", 1.0 - jaccard, factors))

    # --- Keywords gate ---
    if factors.get("use_keywords"):
        base_kw = base.get("_kw")
        if base_kw:
            cand_kw = split_csv(candidate["keywords"])
            union = base_kw | cand_kw
            jaccard = len(base_kw & cand_kw) / len(union) if union else 0.0
            gates.append(gate("s_keywords", 1.0 - jaccard, factors))

    # --- Subtypes gate ---
    if factors.get("use_subtypes"):
        base_sub = base.get("_sub")
        cand_sub = split_csv(candidate["subtypes"])
        union = base_sub | cand_sub if base_sub else set()
        if union:
            jaccard = len(base_sub & cand_sub) / len(union)
            gates.append(gate("s_subtypes", 1.0 - jaccard, factors))

    # --- Supertypes gate ---
    if factors.get("use_supertypes"):
        base_st = base.get("_st")
        if base_st:
            cand_st = split_csv(candidate["supertypes"])
            union = base_st | cand_st
            jaccard = len(base_st & cand_st) / len(union) if union else 0.0
            gates.append(gate("s_supertypes", 1.0 - jaccard, factors))

    # --- Mana value gate ---
    if factors.get("use_mv"):
        base_mv = base["manaValue"] or 0
        cand_mv = candidate["manaValue"] or 0
        gates.append(gate("s_mv", abs(base_mv - cand_mv), factors))

    # --- Color identity gate ---
    if factors.get("use_color"):
        base_ci = base.get("_ci")
        cand_ci = split_csv(candidate["colorIdentity"])
        # s >= 200 means the strict tier — CI is all-or-nothing.
        if float(factors.get("s_color", 8.0)) >= 200.0:
            if base_ci != cand_ci:
                return 0.0
        else:  # moderate/loose: Jaccard gate, combined via GM with other factors
            union = base_ci | cand_ci
            if union:
                jaccard = len(base_ci & cand_ci) / len(union)
                gates.append(gate("s_color", 1.0 - jaccard, factors))
            # ponytail: both colorless → perfect CI match, no gate needed

    # --- Combine all gates via geometric mean ---
    if gates:
        gm = 1.0
        for g in gates:
            gm *= g
        gm = gm ** (1.0 / len(gates))
        oracle_score *= gm

    return oracle_score


# --- Utility helpers ---


def wubrg_sort(chars):
    """Sort color chars in canonical WUBRG order."""
    return sorted(chars, key=lambda c: _WUBRG_ORDER.get(c, 99))


def normalize_dash(s):
    """Replace Unicode dashes with ASCII hyphen. MTGJSON uses U+2014 em-dash in types."""
    return s.replace('—', '-').replace('–', '-').replace('―', '-')
