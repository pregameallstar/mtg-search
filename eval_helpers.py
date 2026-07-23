"""Commander eval pipeline helpers — similarity search, filter sets, auto-save.

ponytail: pure functions that the eval routes delegate to. Takes all state
as arguments rather than reaching for app globals.
"""

import json
import os
import time as _time


# --- Placeholder detection ---


def is_placeholder_name(name):
    """ponytail: catch LLM placeholder names like 'Card Name Placeholder'.
    Returns True if the name looks like a placeholder, not a real card."""
    if not name:
        return True
    lower = name.lower()
    placeholder_markers = [
        "placeholder", "card name", "[placeholder", "[card name",
        "token generator", "sacrifice outlet", "mana dork", "draw engine",
        "removal spell", "counterspell", "board wipe", "ramp spell",
    ]
    return any(m in lower for m in placeholder_markers)


# --- Commander filter sets (TTL cache) ---

# ponytail: cached UUID sets for eval_similar_embed — two full table scans per
# request is wasteful.  TTL'd at 10 minutes; DB doesn't change mid-session.
_cached_legendary = None
_cached_cmdr_legal = None
_cached_filter_ttl = 0


def get_commander_filter_sets(db):
    """Return (legendary_uuids, cmdr_legal_uuids), cached for 10 minutes."""
    global _cached_legendary, _cached_cmdr_legal, _cached_filter_ttl
    now = _time.monotonic()
    if _cached_legendary is not None and now - _cached_filter_ttl < 600:
        return _cached_legendary, _cached_cmdr_legal
    _cached_legendary = {
        r["uuid"] for r in db.execute(
            "SELECT uuid FROM cards WHERE supertypes LIKE '%Legendary%'"
        ).fetchall()
    }
    _cached_cmdr_legal = {
        r["uuid"] for r in db.execute(
            "SELECT uuid FROM cardLegalities WHERE commander = 'Legal'"
        ).fetchall()
    }
    _cached_filter_ttl = now
    return _cached_legendary, _cached_cmdr_legal


# --- Commander similarity search ---


def eval_similar_embed(db, card, db_path):
    """Semantic similarity via embedding index. Filters to legendary + Cmdr-legal + MV ±3."""
    from embed import find

    candidates = find(db_path, card.get("text") or "", top_k=200)
    candidates = [c for c in candidates if c["name"] != card["name"]]

    # Filter to legendary, Commander-legal, MV ±3
    card_mv = card.get("manaValue") or 0
    mv_min = max(0, card_mv - 3)
    mv_max = card_mv + 3

    legendary_uuids, cmdr_legal = get_commander_filter_sets(db)

    results = []
    seen_names = set()
    for c in candidates:
        uuid = c["uuid"]
        if uuid not in legendary_uuids or uuid not in cmdr_legal:
            continue
        mv = c.get("manaValue")
        if mv is not None and (mv < mv_min or mv > mv_max):
            continue
        base_name = c["name"].split(" // ")[0]
        if base_name in seen_names:
            continue
        seen_names.add(base_name)

        # Fetch setCode/number for URL
        row = db.execute(
            "SELECT setCode, number, manaCost, type FROM cards WHERE uuid = ?",
            (uuid,)
        ).fetchone()
        if not row:
            continue

        results.append({
            "name": c["name"],
            "setCode": row["setCode"],
            "number": row["number"],
            "scryfallId": c.get("scryfallId", ""),
            "score": round(c["score"] * 100, 1),
            "manaCost": row["manaCost"] or "",
            "type": row["type"] or "",
        })

    progress = [{"round": 1, "label": "Semantic", "count": len(results)}]
    return results[:10], progress


def eval_similar_legacy(db, card, db_path):
    """TF-IDF strictness-loosening loop — original algorithm."""
    from similarity import get_idf, extract_terms, split_csv, score_similarity

    base = dict(card)
    base["_t"] = split_csv(base["types"])
    base["_kw"] = split_csv(base["keywords"])
    base["_sub"] = split_csv(base["subtypes"])
    base["_st"] = split_csv(base["supertypes"])
    base["_ci"] = split_csv(base["colorIdentity"])

    mv = base["manaValue"] or 0
    mv_min = max(0, mv - 3)
    mv_max = mv + 3
    candidates = db.execute("""
        SELECT c.name, c.manaCost, c.manaValue, c.type, c.types, c.supertypes,
               c.subtypes, c.keywords, c.text, c.colors, c.colorIdentity,
               c.setCode, c.number, ci.scryfallId, s.name as setName
        FROM cards c
        JOIN cardIdentifiers ci ON c.uuid = ci.uuid
        JOIN sets s ON c.setCode = s.code
        JOIN cardLegalities cl ON c.uuid = cl.uuid
        WHERE c.language = 'English'
          AND (c.side IS NULL OR c.side = 'a')
          AND c.uuid != ?
          AND c.name != ?
          AND c.supertypes LIKE '%Legendary%'
          AND cl.commander = 'Legal'
          AND (c.manaValue BETWEEN ? AND ? OR c.manaValue IS NULL)
        GROUP BY c.name
        ORDER BY s.releaseDate DESC
    """, [base["uuid"], base["name"], mv_min, mv_max]).fetchall()

    idf = get_idf(db_path)

    rounds = [
        (1.0, 2.0, 200.0, 0.45, "Strict"),
        (0.5, 0.5, 2.0, 0.35, "Moderate"),
        (0.3, 0.15, 0.5, 0.25, "Loose"),
        (0.15, 0.05, 0.5, 0.0, "Very Loose"),
    ]
    factors_on = {
        "use_types": True, "use_keywords": True, "use_subtypes": True,
        "use_supertypes": True, "use_mv": True, "use_color": True,
    }
    scored = []
    progress = []

    for s_oracle, strict, color_strict, threshold, label in rounds:
        base["_terms"] = extract_terms(base["text"], mtg_filter=True)
        base["_idf_sum"] = sum(idf.get(t, 0) for t in base["_terms"])
        factors = {
            **factors_on,
            "s_oracle": s_oracle,
            "s_types": strict, "s_keywords": strict, "s_subtypes": strict,
            "s_supertypes": strict, "s_mv": strict,
            "s_color": color_strict,
        }
        scored = []
        for c in candidates:
            scored.append((score_similarity(base, c, factors, idf, mtg_filter=True), c))
        scored.sort(key=lambda x: x[0], reverse=True)
        if threshold > 0:
            scored = [(s, c) for s, c in scored if s > threshold]
        progress.append({"round": len(progress) + 1, "label": label, "count": len(scored)})
        if len(scored) >= 10:
            break

    # ponytail: pull from scored until we have 10 deduped results
    results = []
    seen_names = set()
    for score, c in scored:
        if len(results) >= 10:
            break
        # ponytail: DFC names like "Kardur, Doomscourge // Kardur, Doomscourge"
        # are distinct from "Kardur, Doomscourge" in the DB but are the same card.
        base_name = c["name"].split(" // ")[0]
        if base_name in seen_names:
            continue
        seen_names.add(base_name)
        results.append({
            "name": c["name"],
            "setCode": c["setCode"],
            "number": c["number"],
            "scryfallId": c["scryfallId"],
            "score": round(score * 100, 1),
            "manaCost": c["manaCost"] or "",
            "type": c["type"] or "",
        })
    return results, progress


# --- Deepdive auto-save ---


def auto_save_deepdives(set_code, number, new_deepdives, reports_dir):
    """Persist deepdives to a deterministic file so they survive navigation away."""
    if not new_deepdives:
        return
    os.makedirs(reports_dir, exist_ok=True)
    autosave_path = os.path.join(reports_dir, f"{set_code}_{number}_deepdives.json")
    try:
        existing = {}
        if os.path.exists(autosave_path):
            with open(autosave_path) as f:
                existing = json.load(f)
        merged = existing.get("deepdives", {})
        merged.update(new_deepdives)
        with open(autosave_path, "w") as f:
            json.dump({"deepdives": merged}, f, indent=2)
    except (OSError, json.JSONDecodeError):
        pass  # ponytail: fail silently — deepdives still live in cache for this session


def load_auto_deepdives(set_code, number, reports_dir):
    """Load auto-saved deepdives from the deterministic file."""
    autosave_path = os.path.join(
        reports_dir, f"{set_code}_{number}_deepdives.json",
    )
    try:
        if os.path.exists(autosave_path):
            with open(autosave_path) as f:
                return json.load(f).get("deepdives", {})
    except (OSError, json.JSONDecodeError):
        pass
    return {}
