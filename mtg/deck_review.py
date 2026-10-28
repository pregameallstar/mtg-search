"""Deck Doctor orchestration.

Pipeline: enrich → deterministic audit → candidate retrieval → LLM → validation.
The LLM never chooses from the full card pool; it only chooses from the candidate
list this module builds. Everything is reusable without Flask for tests.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
import traceback
from typing import Any, Mapping

import mtg.embed as embed
import mtg.review_cache as review_cache
from mtg.deck_brief import (
    BRIEF_CONSTRAINTS_VERSION,
    WIN_PLANS,
    brief_caps,
    brief_fingerprint,
    brief_is_empty,
    brief_prompt_payload,
    brief_retrieval_text,
    brief_role_caps,
    merge_caps,
    normalize_brief,
)
from mtg.deck_enrich import enrich_deck
from mtg.deck_stats import analyze_deck, deck_fingerprint
from mtg.prompts import DECK_REVIEW_SYSTEM_PROMPT, DECK_REVIEW_VERIFY_SYSTEM_PROMPT
from mtg.roles import classify_roles
from mtg.shared import (
    DATABASE,
    PRICES_DIR,
    TEMPLATES_DIR,
    _list_json_files,
    color_identity_subset,
    db_path,
)

try:
    from mtg.llm import generate as llm_generate
except ImportError:  # pragma: no cover - optional dependency path
    llm_generate = None


ROLE_QUERIES = {
    "Lands": "land, dual land, fetch land, mana fixing, land enters tapped",
    "Ramp": "mana ramp, mana rock, mana dork, put lands onto the battlefield",
    "Card Draw": "draw cards, repeatable card advantage",
    "Card Selection": "scry, surveil, look at the top cards",
    "Targeted Removal": "destroy or exile target creature, efficient interaction",
    "Board Wipe": "destroy all creatures, exile all creatures, mass removal",
    "Counterspell": "counter target spell, permission",
    "Tutor": "search your library for a card",
    "Protection": "protect my permanents, hexproof, indestructible, phase out",
    "Recursion": "return cards from graveyard to hand or battlefield",
    "Win Condition": "win the game, alternate win condition",
}

# Per-role retrieval cap before per-card database work. Only four cards per
# role survive, so a larger shortlist only wastes queries.
MAX_ROLE_HITS = 30
MAX_ADDS_PER_ROLE = 4


def review_fingerprint(
    deck: Mapping[str, Any],
    options: Mapping[str, Any],
    guideline: Mapping[str, Any] | None = None,
) -> str:
    """Fingerprint everything the review pipeline reads, not just the deck list."""
    raw_brief = options.get("deckBrief")
    notes = str(options.get("brief") or "")
    if not notes and isinstance(raw_brief, Mapping):
        notes = str(raw_brief.get("notes") or "")
    structured = normalize_brief(raw_brief, notes=notes)
    base = deck_fingerprint(deck, fingerprint_options(options))
    payload = {
        "base": base,
        "guideline": dict(guideline) if isinstance(guideline, Mapping) else None,
        "brief": brief_fingerprint(structured),
        "briefCaps": merge_caps(
            brief_role_caps(structured.get("notes")), brief_caps(structured)
        ),
        "briefCapsVersion": BRIEF_CONSTRAINTS_VERSION,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def fingerprint_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """Only the options that affect the deck analysis/report fingerprint."""
    return {
        "target_bracket": options.get("target_bracket"),
        "brief": options.get("brief") or "",
        "currency": options.get("currency"),
        "use_llm": bool(options.get("use_llm")),
        "max_adds": options.get("max_adds"),
        "max_cuts": options.get("max_cuts"),
        "max_mana_value": options.get("max_mana_value"),
        "combo_count": options.get("combo_count"),
        "deckBrief": brief_fingerprint(options.get("deckBrief")),
        "guideline": options.get("guideline"),
    }


def _guideline(name: str) -> dict | None:
    if not name:
        return None
    for template in _list_json_files(TEMPLATES_DIR):
        if template.get("type", "cards") == "guideline" and template.get("name") == name:
            return template
    return None


def _front_name(value: Any) -> str:
    return str(value or "").split(" // ", 1)[0].strip().lower()


def _effective_brief(deck: Mapping[str, Any], options: Mapping[str, Any]) -> dict[str, Any]:
    """The structured brief for this run, with the free-form notes attached."""
    raw = options.get("deckBrief")
    if not isinstance(raw, Mapping):
        raw = deck.get("deckBrief") if isinstance(deck, Mapping) else None
    notes = str(options.get("brief") or deck.get("reviewNotes") or "")
    return normalize_brief(raw, notes=notes)


def _qty(card: Mapping[str, Any]) -> int:
    try:
        return max(1, int(card.get("quantity") or 1))
    except (TypeError, ValueError):
        return 1


def _number(value: Any) -> float | None:
    try:
        if value is None or value == "":
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _cached_price(scryfall_id: str, store: str) -> float | None:
    """Read only the existing price cache; never block a review on Scryfall."""
    if not scryfall_id:
        return None
    path = os.path.join(PRICES_DIR, f"{scryfall_id}.json")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    prices = data.get("prices")
    if not isinstance(prices, dict):
        return None
    value = prices.get(store)
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _deck_profile(deck: Mapping[str, Any], audit: Mapping[str, Any], options: Mapping[str, Any]) -> str:
    commander = deck.get("commander") if isinstance(deck.get("commander"), Mapping) else {}
    brief = _effective_brief(deck, options)
    parts = [
        str(commander.get("name") or ""),
        str(commander.get("text") or ""),
        brief_retrieval_text(brief),
        str(brief.get("notes") or ""),
        str(options.get("strategy") or ""),
    ]
    if brief.get("must_keep"):
        parts.append("must keep: " + ", ".join(str(name) for name in brief["must_keep"]))
    outline = deck.get("outline")
    if isinstance(outline, list):
        parts.extend(
            str(section.get("text") or "")
            for section in outline
            if isinstance(section, Mapping)
        )
    eval_data = deck.get("evalData")
    if isinstance(eval_data, Mapping):
        for key in ("strategies", "priorities", "unique_builds"):
            values = eval_data.get(key)
            if isinstance(values, list):
                for item in values:
                    if isinstance(item, Mapping):
                        parts.append(str(item.get("name") or item.get("category") or ""))
                        parts.append(str(item.get("description") or item.get("reason") or ""))
    gaps = [
        str(gap.get("role"))
        for gap in (audit.get("roleGaps") or [])
        if isinstance(gap, Mapping) and (gap.get("deficit") or 0) > 0
    ]
    parts.extend(f"need {role}" for role in gaps)
    parts.extend(str(tag) for tag in (commander.get("tags") or []))
    return " \n ".join(part for part in parts if part).strip()[:6000]


def _candidate_row(db, uuid: str) -> dict | None:
    row = db.execute("""
        SELECT c.uuid, c.name, c.setCode, c.number, c.manaCost, c.manaValue,
               c.type, c.types, c.text, c.keywords, c.colorIdentity,
               c.supertypes, c.isGameChanger, c.edhrecRank, c.edhrecSaltiness,
               c.otherFaceIds, ci.scryfallId, cl.commander as commanderLegality
        FROM cards c
        JOIN cardIdentifiers ci ON c.uuid = ci.uuid
        LEFT JOIN cardLegalities cl ON c.uuid = cl.uuid
        WHERE c.uuid = ? AND c.language = 'English'
          AND (c.side IS NULL OR c.side = 'a')
        LIMIT 1
    """, [uuid]).fetchone()
    if not row:
        return None
    data = dict(row)
    face_ids = [
        face.strip() for face in str(data.get("otherFaceIds") or "").split(",")
        if face.strip()
    ]
    if face_ids:
        placeholders = ",".join("?" for _ in face_ids)
        faces = db.execute(
            f"SELECT text FROM cards WHERE uuid IN ({placeholders})", face_ids
        ).fetchall()
        extra = [face["text"] for face in faces if face["text"]]
        if extra:
            data["text"] = "\n".join([data.get("text") or ""] + extra)
    return data


def _find(db, text: str, top_k: int) -> list[dict]:
    try:
        return embed.find(db_path(DATABASE), text, top_k=top_k) or []
    except Exception:
        return []


def candidate_adds(
    db,
    deck: Mapping[str, Any],
    audit: Mapping[str, Any],
    options: Mapping[str, Any],
) -> dict[str, list[dict[str, Any]]]:
    """Retrieve real candidate adds grouped by the role gap they address."""
    commander = deck.get("commander") if isinstance(deck.get("commander"), Mapping) else {}
    commander_ci = str(commander.get("colorIdentity") or "")
    deck_names = {
        _front_name(card.get("name"))
        for card in (deck.get("cards") or [])
        if isinstance(card, Mapping)
    }
    deck_names.add(_front_name(commander.get("name")))
    brief = _effective_brief(deck, options)
    avoid_names = {
        _front_name(name) for name in (brief.get("avoid") or []) if _front_name(name)
    }

    gaps = [
        str(gap.get("role"))
        for gap in (audit.get("roleGaps") or [])
        if isinstance(gap, Mapping) and (gap.get("deficit") or 0) > 0
    ]
    # Keep the audit's order. Lands are included now that they have a query.
    gaps = [gap for gap in gaps if gap in ROLE_QUERIES]
    if not gaps:
        return {}

    outline = " ".join(
        str(section.get("text") or "")
        for section in (deck.get("outline") or [])
        if isinstance(section, Mapping)
    )[:1200]
    brief_text = brief_retrieval_text(brief)

    # Only cards that matched a role query can be recommended. The deck-profile
    # query is used to refine their scores, but we never do DB/role work for the
    # hundreds of profile-only hits that would be discarded anyway.
    found: dict[str, dict[str, Any]] = {}
    gap_roles_by_name: dict[str, set[str]] = {}
    for role in gaps:
        # Keep the role phrase early: the embedding model truncates long input.
        query = f"{commander.get('name', '')} {ROLE_QUERIES[role]} {brief_text} {outline}"
        hits = _find(db, query, 150)
        hits.sort(key=lambda item: item.get("score") or 0, reverse=True)
        kept = 0
        for result in hits:
            key = result.get("uuid") or _front_name(result.get("name"))
            if not key:
                continue
            # Bound the expensive per-card DB work; only the top few can ever
            # survive the 4-per-role bucket below.
            if key not in gap_roles_by_name:
                kept += 1
                if kept > MAX_ROLE_HITS:
                    break
            gap_roles_by_name.setdefault(key, set()).add(role)
            old = found.get(key)
            if old is None or (result.get("score") or 0) > (old.get("score") or 0):
                found[key] = result

    if not found:
        return {}
    profile_scores = {
        (result.get("uuid") or _front_name(result.get("name"))): (result.get("score") or 0)
        for result in _find(db, _deck_profile(deck, audit, options), 600)
        if result.get("uuid") or result.get("name")
    }
    for key, result in found.items():
        score = profile_scores.get(key)
        if score is not None and score > (result.get("score") or 0):
            found[key] = {**result, "score": score}

    target_bracket = options.get("target_bracket")
    try:
        target_bracket = int(target_bracket) if target_bracket is not None else None
    except (TypeError, ValueError):
        target_bracket = None
    max_mv = _number(options.get("max_mana_value")) or 7
    grouped: dict[str, list[dict[str, Any]]] = {role: [] for role in gaps}
    seen_names: set[str] = set()

    for key, result in found.items():
        uuid = result.get("uuid")
        if not uuid:
            continue
        row = _candidate_row(db, uuid)
        if not row:
            continue
        name = str(row["name"] or "")
        if _front_name(name) in deck_names or _front_name(name) in avoid_names:
            continue
        if name.lower() in seen_names:
            continue
        if not color_identity_subset(row.get("colorIdentity") or "", commander_ci):
            continue
        legality = str(row.get("commanderLegality") or "")
        if legality and legality.lower() != "legal":
            continue
        info = classify_roles(row)
        roles = set(info["roles"])
        matching_gaps = roles & gap_roles_by_name.get(key, set())
        if not matching_gaps:
            continue
        role = next((gap for gap in gaps if gap in matching_gaps), gaps[0])
        mv = _number(row.get("manaValue"))
        if mv is not None and mv > max_mv and "Win Condition" not in roles:
            continue
        if target_bracket is not None and target_bracket <= 2:
            if row.get("isGameChanger") or "Mass Land Denial" in roles:
                continue
        rank = _number(row.get("edhrecRank"))
        popularity = 0.5 if rank is None else max(0.0, 1.0 - min(rank, 100000) / 100000)
        score = 0.65 * float(result.get("score") or 0) + 0.20 + 0.15 * popularity
        candidate = {
            "name": name,
            "setCode": row.get("setCode") or "",
            "number": row.get("number") or "",
            "scryfallId": row.get("scryfallId") or "",
            "manaCost": row.get("manaCost") or "",
            "manaValue": row.get("manaValue"),
            "type": row.get("type") or "",
            "colorIdentity": row.get("colorIdentity") or "",
            "roles": info["roles"],
            "reasonHint": f"fills the {role} gap",
            "score": round(score, 4),
            "edhrecRank": row.get("edhrecRank"),
            "price": None,
            "priceUnknown": True,
            "oracleExcerpt": str(row.get("text") or "")[:240],
        }
        # Avoid the same card landing in several role buckets.
        seen_names.add(name.lower())
        grouped.setdefault(role, []).append(candidate)

    store = str(options.get("currency") or "usd")
    for role, bucket in grouped.items():
        bucket.sort(key=lambda item: item["score"], reverse=True)
        del bucket[MAX_ADDS_PER_ROLE:]
        # Prices are only needed for the four cards that survive the bucket.
        for candidate in bucket:
            price = _cached_price(str(candidate.get("scryfallId") or ""), store)
            candidate["price"] = price
            candidate["priceUnknown"] = price is None

    return {role: bucket for role, bucket in grouped.items() if bucket}


def cut_candidates(
    deck: Mapping[str, Any],
    audit: Mapping[str, Any],
    limit: int = 15,
    *,
    must_keep: list[str] | tuple[str, ...] | None = None,
) -> list[dict[str, Any]]:
    """Build a deterministic cut shortlist from surplus, curve, and metadata."""
    commander = deck.get("commander") if isinstance(deck.get("commander"), Mapping) else {}
    commander_name = _front_name(commander.get("name"))
    keep_names = {_front_name(name) for name in (must_keep or []) if _front_name(name)}
    gaps = {
        str(gap.get("role")): gap
        for gap in (audit.get("roleGaps") or [])
        if isinstance(gap, Mapping)
    }
    cuts = []
    for card in (deck.get("cards") or []):
        if not isinstance(card, Mapping):
            continue
        name = str(card.get("name") or "")
        if _front_name(name) == commander_name or _front_name(name) in keep_names:
            continue
        info = classify_roles(card)
        if "Lands" in info["roles"] and "basic" in str(card.get("supertypes") or "").lower():
            continue
        score = 0.0
        reasons = []
        for role in info["roles"]:
            gap = gaps.get(role)
            if not gap:
                continue
            actual = int(gap.get("actual") or 0)
            target = int(gap.get("target") or 0)
            if actual > target:
                score += 2.0
                reasons.append(f"surplus {role}")
        mv = _number(card.get("manaValue"))
        if mv is not None and mv >= 6:
            score += 1.5
            reasons.append("high mana value")
        rank = _number(card.get("edhrecRank"))
        if rank is None:
            score += 0.3
        elif rank > 20000:
            score += 0.5
            reasons.append("low EDHREC usage")
        if score <= 0:
            continue
        cuts.append({
            "name": name,
            "uuid": card.get("uuid") or "",
            "quantity": _qty(card),
            "scryfallId": card.get("scryfallId") or "",
            "imageUrl": card.get("imageUrl") or "",
            "roles": info["roles"],
            "reasonHint": ", ".join(reasons) or "low structural contribution",
            "score": round(score, 3),
        })
    cuts.sort(key=lambda item: item["score"], reverse=True)
    return cuts[:limit]


def _audit_for_prompt(audit: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "counts": audit.get("counts"),
        "curve": audit.get("curve"),
        "roles": {
            role: value.get("count")
            for role, value in (audit.get("roles") or {}).items()
            if isinstance(value, Mapping)
        },
        "roleGaps": audit.get("roleGaps"),
        "legality": audit.get("legality"),
        "bracket": audit.get("bracket"),
        "health": audit.get("health"),
    }


def _build_user_prompt(
    deck: Mapping[str, Any],
    audit: Mapping[str, Any],
    candidates: Mapping[str, list[dict[str, Any]]],
    cuts: list[dict[str, Any]],
    options: Mapping[str, Any],
) -> str:
    commander = deck.get("commander") if isinstance(deck.get("commander"), Mapping) else {}
    brief = _effective_brief(deck, options)
    rows = []
    for card in (deck.get("cards") or []):
        if not isinstance(card, Mapping):
            continue
        info = classify_roles(card)
        rows.append({
            "name": card.get("name"),
            "quantity": _qty(card),
            "manaValue": card.get("manaValue"),
            "type": card.get("type"),
            "roles": info["roles"],
            "tags": card.get("tags") or [],
        })
    outline = " ".join(
        str(section.get("text") or "")
        for section in (deck.get("outline") or [])
        if isinstance(section, Mapping)
    )[:2000]
    eval_summary = ""
    eval_data = deck.get("evalData")
    if isinstance(eval_data, Mapping):
        bits = []
        for key in ("strategies", "priorities"):
            for item in eval_data.get(key) or []:
                if isinstance(item, Mapping):
                    bits.append(str(item.get("name") or item.get("category") or ""))
        eval_summary = ", ".join(bits)[:1200]
    payload = {
        "commander": {
            "name": commander.get("name"),
            "type": commander.get("type"),
            "manaCost": commander.get("manaCost"),
            "manaValue": commander.get("manaValue"),
            "colorIdentity": commander.get("colorIdentity"),
            "oracle_text": commander.get("text"),
        },
        "deck": {"name": deck.get("name"), "cards": rows},
        "audit": _audit_for_prompt(audit),
        "candidate_adds": candidates,
        "cut_candidates": cuts,
        "options": {
            "target_bracket": options.get("target_bracket"),
            "currency": options.get("currency"),
            "max_adds": options.get("max_adds", 10),
            "max_cuts": options.get("max_cuts", 10),
        },
        "deck_brief": brief_prompt_payload(
            brief, notes=brief.get("notes") or options.get("brief") or ""
        ),
        "outline": outline,
        "commander_eval_summary": eval_summary,
    }
    return json.dumps(payload, indent=2)


def _run_llm(
    deck: Mapping[str, Any],
    audit: Mapping[str, Any],
    candidates: Mapping[str, list[dict[str, Any]]],
    cuts: list[dict[str, Any]],
    options: Mapping[str, Any],
) -> tuple[dict | None, str | None]:
    if llm_generate is None:
        return None, "LLM dependencies are not installed."
    llm = options.get("llm") if isinstance(options.get("llm"), Mapping) else {}
    api_key = llm.get("api_key")
    if not api_key:
        return None, "No LLM API key configured."
    system = str(options.get("review_prompt") or "").strip() or DECK_REVIEW_SYSTEM_PROMPT
    user = _build_user_prompt(deck, audit, candidates, cuts, options)
    last_error = None
    for attempt in range(2):
        try:
            data = llm_generate(
                system, user,
                backend=llm.get("backend") or None,
                api_key=api_key,
                base_url=llm.get("base_url") or None,
                model=llm.get("model") or None,
                timeout=llm.get("timeout") or None,
            )
            if not isinstance(data, dict):
                raise ValueError("LLM returned a non-object response.")
            return data, None
        except ValueError as exc:
            # Only malformed JSON / non-object responses benefit from the
            # "return ONLY valid JSON" retry. Retrying auth, timeout, or network
            # failures just doubles the time a broken run stays in progress.
            last_error = str(exc)
            if attempt == 0:
                user += "\n\nYour previous response was not valid JSON. Return ONLY valid JSON."
        except Exception as exc:
            return None, str(exc)
    return None, last_error


def _verify_llm(
    deck: Mapping[str, Any],
    analysis: Mapping[str, Any] | None,
    candidates: Mapping[str, list[dict[str, Any]]],
    options: Mapping[str, Any],
    audit: Mapping[str, Any] | None = None,
) -> tuple[dict | None, str | None]:
    if not analysis:
        return None, None
    if llm_generate is None:
        return None, "LLM dependencies are not installed."
    llm = options.get("llm") if isinstance(options.get("llm"), Mapping) else {}
    api_key = llm.get("api_key")
    if not api_key:
        return None, "No LLM API key configured."
    dtdeck = deck.get("commander") if isinstance(deck.get("commander"), Mapping) else {}
    brief = _effective_brief(deck, options)
    deck_names = [
        str(card.get("name") or "")
        for card in (deck.get("cards") or [])
        if isinstance(card, Mapping)
    ]
    candidate_details = [
        {
            "name": str(card.get("name") or ""),
            "colorIdentity": str(card.get("colorIdentity") or ""),
            "manaValue": card.get("manaValue"),
            "type": str(card.get("type") or ""),
            "roles": card.get("roles") or [],
        }
        for bucket in candidates.values()
        for card in bucket
        if isinstance(card, Mapping)
    ]
    user = json.dumps({
        "commander": {
            "name": dtdeck.get("name"),
            "color_identity": dtdeck.get("colorIdentity"),
        },
        "deck_names": deck_names,
        "candidate_names": [card["name"] for card in candidate_details],
        "candidate_details": candidate_details,
        "audit": _audit_for_prompt(audit or {}),
        "deck_brief": brief_prompt_payload(
            brief, notes=brief.get("notes") or options.get("brief") or ""
        ),
        "analysis": analysis,
    }, indent=2)
    try:
        data = llm_generate(
            DECK_REVIEW_VERIFY_SYSTEM_PROMPT, user,
            backend=llm.get("backend") or None,
            api_key=api_key,
            base_url=llm.get("base_url") or None,
            model=llm.get("model") or None,
            timeout=llm.get("timeout") or None,
        )
        if not isinstance(data, dict):
            return None, "Verifier returned a non-object response."
        return data, None
    except Exception as exc:
        return None, str(exc)


def validate_analysis(
    analysis: Mapping[str, Any] | None,
    deck: Mapping[str, Any],
    candidates: Mapping[str, list[dict[str, Any]]],
    *,
    max_adds: int = 10,
    max_cuts: int = 10,
    must_keep: list[str] | tuple[str, ...] | None = None,
    avoid: list[str] | tuple[str, ...] | None = None,
) -> tuple[dict | None, dict[str, Any]]:
    """Reject hallucinated or illegal recommendations before display."""
    if not isinstance(analysis, Mapping):
        return None, {"warnings": [], "rejected": []}

    deck_names = {
        _front_name(card.get("name"))
        for card in (deck.get("cards") or [])
        if isinstance(card, Mapping)
    }
    commander_name = _front_name((deck.get("commander") or {}).get("name"))
    keep_names = {_front_name(name) for name in (must_keep or []) if _front_name(name)}
    avoid_names = {_front_name(name) for name in (avoid or []) if _front_name(name)}
    candidate_names = {
        _front_name(card.get("name"))
        for bucket in candidates.values()
        for card in bucket
        if isinstance(card, Mapping)
    }
    rejected = []
    warnings = []
    adds = []
    cuts = []

    raw_adds = analysis.get("adds")
    for item in (raw_adds if isinstance(raw_adds, list) else []):
        # Count only accepted adds, so invalid early suggestions cannot crowd
        # out valid later ones.
        if len(adds) >= max_adds:
            break
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip()
        key = _front_name(name)
        if key in avoid_names:
            rejected.append({"type": "add", "name": name, "reason": "on the avoid list"})
            continue
        if not key or key not in candidate_names:
            rejected.append({"type": "add", "name": name, "reason": "not in candidate list"})
            continue
        if key in deck_names or key == commander_name:
            rejected.append({"type": "add", "name": name, "reason": "already in deck"})
            continue
        if any(_front_name(entry.get("name")) == key for entry in adds):
            continue
        item = dict(item)
        replaces = str(item.get("replaces") or "").strip()
        if replaces and _front_name(replaces) not in deck_names:
            item["replaces"] = None
            warnings.append(f"{name} named a replacement not in the deck; the replacement was ignored.")
        adds.append(item)

    raw_cuts = analysis.get("cuts")
    for item in (raw_cuts if isinstance(raw_cuts, list) else []):
        if len(cuts) >= max_cuts:
            break
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip()
        key = _front_name(name)
        if not key or key not in deck_names:
            rejected.append({"type": "cut", "name": name, "reason": "not in deck"})
            continue
        if key == commander_name:
            rejected.append({"type": "cut", "name": name, "reason": "cannot cut the commander"})
            continue
        if key in keep_names:
            rejected.append({"type": "cut", "name": name, "reason": "must-keep card"})
            continue
        if any(_front_name(entry.get("name")) == key for entry in cuts):
            continue
        cuts.append(dict(item))

    result = dict(analysis)
    result["adds"] = adds
    result["cuts"] = cuts
    return result, {"warnings": warnings, "rejected": rejected}


def _limit(value: Any, default: int = 10, maximum: int = 20) -> int:
    """Clamp a user-supplied count without treating 0 as 'use the default'."""
    if value is None or value == "":
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, min(number, maximum))


def run_review_job(job_id: str, deck: Mapping[str, Any], options: Mapping[str, Any]) -> None:
    """Run one review job; updates the review cache as it progresses."""
    started = time.time()
    db = None
    try:
        review_cache.update(job_id, status="running", step=1, label="Loading and enriching deck…")
        db = sqlite3.connect(db_path(DATABASE))
        db.row_factory = sqlite3.Row
        enriched = enrich_deck(db, deck)

        options = dict(options)
        brief = _effective_brief(enriched, options)
        if not isinstance(options.get("deckBrief"), Mapping):
            options["deckBrief"] = brief
        if not options.get("brief"):
            options["brief"] = brief.get("notes") or ""

        review_cache.update(job_id, step=2, label="Auditing deck structure…")
        guideline = _guideline(str(options.get("guideline") or deck.get("guideline") or ""))
        caps = merge_caps(brief_role_caps(brief.get("notes")), brief_caps(brief))
        audit = analyze_deck(
            enriched,
            guideline=guideline,
            target_bracket=options.get("target_bracket"),
            combo_count=options.get("combo_count"),
            brief_caps=caps,
            win_plans=brief.get("win_plans"),
        )

        review_cache.update(job_id, step=3, label="Finding candidate cards…")
        candidates = candidate_adds(db, enriched, audit, options)
        cuts = cut_candidates(enriched, audit, must_keep=brief.get("must_keep"))

        analysis = None
        llm_error = None
        if options.get("use_llm"):
            review_cache.update(job_id, step=4, label="Writing the review…")
            analysis, llm_error = _run_llm(enriched, audit, candidates, cuts, options)
        else:
            review_cache.update(job_id, step=4, label="Skipping LLM review…")

        review_cache.update(job_id, step=5, label="Validating recommendations…")
        analysis, validation = validate_analysis(
            analysis, enriched, candidates,
            max_adds=_limit(options.get("max_adds")),
            max_cuts=_limit(options.get("max_cuts")),
            must_keep=brief.get("must_keep"),
            avoid=brief.get("avoid"),
        )
        verification = None
        verify_error = None
        if options.get("verify_llm") and analysis:
            review_cache.update(job_id, step=5, label="Second-pass LLM verification…")
            verification, verify_error = _verify_llm(
                enriched, analysis, candidates, options, audit=audit
            )

        result = {
            "schemaVersion": 1,
            "deck": {
                "name": deck.get("name", ""),
                "fingerprint": review_fingerprint(enriched, options, guideline),
                "commander": {
                    "name": (enriched.get("commander") or {}).get("name", ""),
                    "colorIdentity": (enriched.get("commander") or {}).get("colorIdentity", ""),
                },
            },
            "audit": audit,
            "candidates": {"adds": candidates, "cuts": cuts},
            "analysis": analysis,
            "verification": verification,
            "validation": validation,
            "options": {
                "target_bracket": options.get("target_bracket"),
                "brief": options.get("brief") or "",
                "deckBrief": brief,
                "currency": options.get("currency"),
                "use_llm": bool(options.get("use_llm")),
                "verify_llm": bool(options.get("verify_llm")),
                "requested_use_llm": bool(options.get("requested_use_llm", options.get("use_llm"))),
                "requested_verify_llm": bool(options.get("requested_verify_llm", options.get("verify_llm"))),
                "max_adds": options.get("max_adds"),
                "max_cuts": options.get("max_cuts"),
                "max_mana_value": options.get("max_mana_value"),
                "combo_count": options.get("combo_count"),
                "guideline": options.get("guideline"),
            },
            "pipeline": {
                "embed": sum(len(bucket) for bucket in candidates.values()),
                "llm": analysis is not None,
                "llm_error": llm_error,
                "llm_verify": verification is not None,
                "llm_verify_error": verify_error,
                "llm_backend": (options.get("llm") or {}).get("backend"),
                "llm_model": (options.get("llm") or {}).get("model"),
                "prices": "cached",
                "duration_ms": round((time.time() - started) * 1000),
            },
        }
        review_cache.update(job_id, status="done", step=5, label="Done", result=result)
    except Exception as exc:
        traceback.print_exc()
        review_cache.update(job_id, status="error", label="Review failed", error=str(exc))
    finally:
        if db is not None:
            db.close()


def _report_deck_name(report: Mapping[str, Any]) -> str:
    deck = report.get("deck")
    if isinstance(deck, Mapping):
        return str(deck.get("name") or "")
    return str(deck or "")


def _report_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _message_list(values: Any, key: str = "message") -> list[str]:
    out = []
    for item in _report_list(values):
        if isinstance(item, Mapping):
            text = str(item.get(key) or item.get("name") or "").strip()
        else:
            text = str(item or "").strip()
        if text:
            out.append(text)
    return out


def review_to_markdown(report: Mapping[str, Any]) -> str:
    """Render a saved review or job result as Markdown."""
    audit = report.get("audit") if isinstance(report.get("audit"), Mapping) else {}
    analysis = report.get("analysis") if isinstance(report.get("analysis"), Mapping) else {}
    bracket = audit.get("bracket") if isinstance(audit.get("bracket"), Mapping) else {}
    salt = bracket.get("salt") if isinstance(bracket.get("salt"), Mapping) else {}
    health = audit.get("health") if isinstance(audit.get("health"), Mapping) else {}
    components = health.get("components") if isinstance(health.get("components"), Mapping) else {}
    counts = audit.get("counts") if isinstance(audit.get("counts"), Mapping) else {}
    curve = audit.get("curve") if isinstance(audit.get("curve"), Mapping) else {}
    legality = audit.get("legality") if isinstance(audit.get("legality"), Mapping) else {}
    role_gaps = _report_list(audit.get("roleGaps"))
    candidates = report.get("candidates") if isinstance(report.get("candidates"), Mapping) else {}
    adds = candidates.get("adds") if isinstance(candidates.get("adds"), Mapping) else {}
    validation = report.get("validation") if isinstance(report.get("validation"), Mapping) else {}
    verification = report.get("verification") if isinstance(report.get("verification"), Mapping) else {}
    pipeline = report.get("pipeline") if isinstance(report.get("pipeline"), Mapping) else {}
    options = report.get("options") if isinstance(report.get("options"), Mapping) else {}

    deck = report.get("deck") if isinstance(report.get("deck"), Mapping) else {}
    commander = deck.get("commander") if isinstance(deck.get("commander"), Mapping) else {}
    if not commander and isinstance(report.get("commander"), Mapping):
        commander = report["commander"]

    # The UI treats an explicitly empty LLM cuts/adds list as "none",
    # so exports must do the same instead of falling back to candidates.
    if isinstance(analysis.get("cuts"), list):
        cuts = analysis["cuts"]
    else:
        cuts = candidates.get("cuts") if isinstance(candidates.get("cuts"), list) else []
    add_items = analysis.get("adds") if isinstance(analysis.get("adds"), list) else []

    lines = [f"# Deck Doctor — {_report_deck_name(report) or 'Untitled deck'}", ""]
    meta = [f"**Health:** {health.get('score', '—')}/100"]
    bracket_text = f"**Bracket:** {bracket.get('estimate', '—')}"
    bracket_range = bracket.get("range")
    if isinstance(bracket_range, list) and len(bracket_range) == 2:
        bracket_text += f" (range {bracket_range[0]}–{bracket_range[1]})"
    meta.append(bracket_text)
    if salt.get("score") is not None:
        meta.append(f"**Salt:** {salt.get('score')}")
    lines += [" · ".join(meta), ""]
    if commander.get("name"):
        lines += [f"**Commander:** {commander.get('name')}", ""]
    if analysis.get("verdict"):
        lines += [f"**Verdict:** {analysis['verdict']}", ""]
    if analysis.get("summary"):
        lines += [str(analysis["summary"]), ""]
    if not analysis:
        if pipeline.get("llm_error"):
            note = f"No LLM narrative was generated: {pipeline['llm_error']}"
        elif options.get("requested_use_llm") is False:
            note = "No LLM narrative was generated (LLM review was turned off)."
        elif options.get("use_llm") is False:
            note = "No LLM narrative was generated (no LLM API key configured)."
        else:
            note = "No LLM narrative was generated."
        lines += [f"**Note:** {note}", ""]
    brief_value = options.get("deckBrief")
    notes = str(options.get("brief") or "")
    if not notes and isinstance(brief_value, Mapping):
        notes = str(brief_value.get("notes") or "")
    brief = normalize_brief(brief_value, notes=notes)
    if not brief_is_empty(brief):
        lines += ["## Deck brief", ""]
        if brief.get("archetype"):
            lines.append(f"- **Archetype:** {brief['archetype']}")
        if brief.get("themes"):
            lines.append("- **Themes:** " + ", ".join(brief["themes"]))
        if brief.get("win_plans"):
            labels = [WIN_PLANS.get(slug, slug) for slug in brief["win_plans"]]
            lines.append("- **Win plans:** " + ", ".join(labels))
        if brief.get("constraints"):
            limits = ", ".join(
                f"{role} max {count}" for role, count in brief["constraints"].items()
            )
            lines.append(f"- **Hard limits:** {limits}")
        if brief.get("must_keep"):
            lines.append("- **Must keep:** " + ", ".join(brief["must_keep"]))
        if brief.get("avoid"):
            lines.append("- **Avoid:** " + ", ".join(brief["avoid"]))
        if brief.get("notes"):
            lines += ["", str(brief["notes"])]
        lines.append("")

    lines += ["## Audit", ""]
    if counts:
        lines.append(
            f"- **Cards:** {counts.get('total', '—')} total "
            f"({counts.get('main', '—')} main + {counts.get('commander', 0)} commander)"
        )
    if curve:
        lines.append(
            f"- **Curve:** average mana value {curve.get('averageMv', '—')}, "
            f"{curve.get('landCount', '—')} lands, {curve.get('rampCount', '—')} ramp"
        )
        buckets = curve.get("buckets")
        if isinstance(buckets, Mapping) and buckets:
            lines.append("- **Mana curve:** " + ", ".join(
                f"{bucket}: {buckets[bucket]}" for bucket in sorted(buckets, key=str)
            ))
    legal = legality.get("legal")
    if legal is not None:
        lines.append(f"- **Legality:** {'legal' if legal else 'illegal'}")
    lines.append("")

    if legality.get("errors"):
        lines += ["### Legality errors", ""]
        lines += [f"- {message}" for message in _message_list(legality["errors"])]
        lines.append("")
    if legality.get("warnings"):
        lines += ["### Legality warnings", ""]
        lines += [f"- {message}" for message in _message_list(legality["warnings"])]
        lines.append("")
    legality_warning_keys = {
        (str(item.get("code") or ""), str(item.get("message") or ""))
        for item in _report_list(legality.get("warnings"))
        if isinstance(item, Mapping)
    }
    audit_warnings = [
        item.get("message") if isinstance(item, Mapping) else str(item)
        for item in _report_list(audit.get("warnings"))
        if not (
            isinstance(item, Mapping)
            and (str(item.get("code") or ""), str(item.get("message") or ""))
            in legality_warning_keys
        )
    ]
    if audit_warnings:
        lines += ["### Audit notes", ""]
        lines += [f"- {message}" for message in audit_warnings if message]
        lines.append("")

    if components:
        labels = (
            ("roles", "Roles"),
            ("mana", "Mana"),
            ("interaction", "Interaction"),
            ("draw", "Card draw"),
            ("wincons", "Win conditions"),
            ("legality", "Legality"),
        )
        lines += ["### Health components", ""]
        lines += [
            f"- **{label}:** {components.get(key, '—')}"
            for key, label in labels
        ]
        lines.append("")

    if role_gaps:
        lines += ["### Role coverage", ""]
        for gap in role_gaps:
            if not isinstance(gap, Mapping):
                continue
            role = gap.get("role", "")
            actual, target = gap.get("actual", "—"), gap.get("target", "—")
            deficit, surplus = gap.get("deficit") or 0, gap.get("surplus") or 0
            if deficit:
                suffix = f"needs {deficit}"
            elif surplus:
                suffix = f"{surplus} over target"
            else:
                suffix = "on target"
            lines.append(f"- **{role}:** {actual} / {target} ({suffix})")
        lines.append("")

    brief_constraints = audit.get("briefConstraints")
    if isinstance(brief_constraints, Mapping) and brief_constraints:
        lines += ["### Brief constraints", ""]
        lines += [
            f"- **{role}:** max {count}"
            for role, count in brief_constraints.items()
        ]
        lines.append("")

    signals = _report_list(bracket.get("signals"))
    signal_lines = []
    for signal in signals:
        if not isinstance(signal, Mapping):
            continue
        count = signal.get("count") or 0
        if not count:
            continue
        label = signal.get("label") or signal.get("key") or "Signal"
        cards = signal.get("cards") if isinstance(signal.get("cards"), list) else []
        text = f"- **{label}:** {count}"
        if cards:
            text += f" ({', '.join(str(card) for card in cards)})"
        signal_lines.append(text)
    if signal_lines:
        lines += ["### Bracket signals", ""] + signal_lines + [""]

    for key, title in (
        ("strengths", "Strengths"),
        ("weaknesses", "Weaknesses"),
        ("next_steps", "Next steps"),
    ):
        values = analysis.get(key) or []
        if not isinstance(values, list):
            values = []
        if values:
            lines += [f"## {title}", ""]
            lines += [f"- {value}" for value in values]
            lines.append("")

    lines += ["## Cuts", ""]
    if cuts:
        for item in cuts:
            if isinstance(item, Mapping):
                lines.append(
                    f"- **{item.get('name', '')}** — "
                    f"{item.get('reason') or item.get('reasonHint') or ''}"
                )
    else:
        lines.append("- None recommended.")
    lines.append("")

    lines += ["## Adds", ""]
    shown_adds = False
    for item in add_items:
        if not isinstance(item, Mapping):
            continue
        shown_adds = True
        text = (
            f"- **{item.get('name', '')}** — {item.get('reason', '')}"
            + (f" ({item.get('role')})" if item.get("role") else "")
        )
        if item.get("replaces"):
            text += f" — replaces **{item['replaces']}**"
        lines.append(text)
    if not shown_adds and isinstance(adds, Mapping):
        for role, bucket in adds.items():
            for card in (bucket if isinstance(bucket, list) else []):
                if not isinstance(card, Mapping):
                    continue
                shown_adds = True
                price = _number(card.get("price"))
                suffix = f" · ${price:.2f}" if price is not None else ""
                lines.append(
                    f"- **{card.get('name', '')}** — {card.get('reasonHint', '')} "
                    f"({role}){suffix}"
                )
    if not shown_adds:
        lines.append("- None recommended.")
    lines.append("")

    if analysis.get("mana_base"):
        lines += ["## Mana base", "", str(analysis["mana_base"]), ""]
    if analysis.get("win_conditions"):
        lines += ["## Win conditions", "", str(analysis["win_conditions"]), ""]
    if analysis.get("bracket_fit"):
        lines += ["## Bracket fit", "", str(analysis["bracket_fit"]), ""]

    verify_warnings = _message_list(verification.get("warnings"))
    if verify_warnings:
        lines += ["## Verifier notes", ""] + [f"- {item}" for item in verify_warnings] + [""]
    elif verification.get("verified") is False:
        lines += ["## Verifier notes", "", "- The verifier could not confirm this report.", ""]

    validation_warnings = _message_list(validation.get("warnings"))
    if validation_warnings:
        lines += ["## Validation notes", ""]
        lines += [f"- {item}" for item in validation_warnings]
        lines.append("")

    rejected = _report_list(validation.get("rejected"))
    if rejected:
        lines += ["## Rejected suggestions", ""]
        for item in rejected:
            if isinstance(item, Mapping):
                lines.append(f"- {item.get('name', '')} — {item.get('reason', '')}")
        lines.append("")

    if pipeline.get("llm_error") or pipeline.get("llm_verify_error"):
        lines += ["## Pipeline notes", ""]
        if pipeline.get("llm_error"):
            lines.append(f"- LLM review failed: {pipeline['llm_error']}")
        if pipeline.get("llm_verify_error"):
            lines.append(f"- LLM verification failed: {pipeline['llm_verify_error']}")
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def review_to_html(report: Mapping[str, Any]) -> str:
    """Render a saved review as a small self-contained HTML document."""
    import html as _html

    def inline(text: str) -> str:
        escaped = _html.escape(str(text))
        # Escape first, then turn the Markdown bold we emitted into markup.
        return re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", escaped)

    body_parts: list[str] = []
    paragraph: list[str] = []
    bullets: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            body_parts.append(
                "<p>" + "<br>".join(inline(line) for line in paragraph) + "</p>"
            )
            paragraph.clear()

    def flush_bullets() -> None:
        if bullets:
            body_parts.append(
                "<ul>" + "".join(f"<li>{inline(item)}</li>" for item in bullets) + "</ul>"
            )
            bullets.clear()

    for raw in review_to_markdown(report).splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            flush_paragraph()
            flush_bullets()
            continue
        if line.startswith("# "):
            flush_paragraph()
            flush_bullets()
            body_parts.append(f"<h1>{inline(line[2:])}</h1>")
            continue
        if line.startswith("## "):
            flush_paragraph()
            flush_bullets()
            body_parts.append(f"<h2>{inline(line[3:])}</h2>")
            continue
        if line.startswith("### "):
            flush_paragraph()
            flush_bullets()
            body_parts.append(f"<h3>{inline(line[4:])}</h3>")
            continue
        if line.startswith("- "):
            flush_paragraph()
            bullets.append(line[2:].strip())
            continue
        if bullets:
            # Continuation of a multi-line bullet (LLM text can contain newlines).
            bullets[-1] = (bullets[-1] + " " + stripped).strip()
            continue
        paragraph.append(stripped)
    flush_paragraph()
    flush_bullets()

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Deck Doctor — {_html.escape(_report_deck_name(report) or 'Deck')}</title>
<style>
body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem;line-height:1.55;color:#1f2328}}
h1{{border-bottom:1px solid #d0d7de;padding-bottom:.4rem}}
h2{{margin-top:1.6rem;font-size:1.1rem}}
h3{{margin-top:1.1rem;font-size:.95rem;color:#57606a}}
li{{margin:.3rem 0}} code{{background:#f6f8fa;padding:.1rem .25rem;border-radius:3px}}
</style></head><body>
{''.join(body_parts)}
</body></html>"""
