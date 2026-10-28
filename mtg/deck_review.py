"""Deck Doctor orchestration.

Pipeline: enrich → deterministic audit → candidate retrieval → LLM → validation.
The LLM never chooses from the full card pool; it only chooses from the candidate
list this module builds. Everything is reusable without Flask for tests.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
import traceback
from typing import Any, Callable, Mapping

import mtg.embed as embed
import mtg.review_cache as review_cache
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


def fingerprint_options(options: Mapping[str, Any]) -> dict[str, Any]:
    """Only the options that affect the deck analysis/report fingerprint."""
    return {
        "target_bracket": options.get("target_bracket"),
        "budget": options.get("budget"),
        "currency": options.get("currency"),
        "use_llm": bool(options.get("use_llm")),
        "max_adds": options.get("max_adds"),
        "max_cuts": options.get("max_cuts"),
        "guideline": options.get("guideline"),
        "strategy": options.get("strategy"),
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
    parts = [
        str(commander.get("name") or ""),
        str(commander.get("text") or ""),
        str(options.get("strategy") or ""),
    ]
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

    gaps = [
        str(gap.get("role"))
        for gap in (audit.get("roleGaps") or [])
        if isinstance(gap, Mapping) and (gap.get("deficit") or 0) > 0
    ]
    # Prefer core roles first, but keep the audit's order.
    gaps = [gap for gap in gaps if gap in ROLE_QUERIES][:8]
    if not gaps:
        return {}

    profile = _deck_profile(deck, audit, options)
    found: dict[str, dict[str, Any]] = {}
    gap_roles_by_name: dict[str, set[str]] = {}

    for result in _find(db, profile, 600):
        key = result.get("uuid") or _front_name(result.get("name"))
        if key:
            found[key] = result

    outline = " ".join(
        str(section.get("text") or "")
        for section in (deck.get("outline") or [])
        if isinstance(section, Mapping)
    )[:1200]
    for role in gaps:
        query = f"{commander.get('name', '')} {outline} {ROLE_QUERIES[role]}"
        for result in _find(db, query, 150):
            key = result.get("uuid") or _front_name(result.get("name"))
            if not key:
                continue
            old = found.get(key)
            if old is None or (result.get("score") or 0) > (old.get("score") or 0):
                found[key] = result
            gap_roles_by_name.setdefault(key, set()).add(role)

    target_bracket = options.get("target_bracket")
    try:
        target_bracket = int(target_bracket) if target_bracket is not None else None
    except (TypeError, ValueError):
        target_bracket = None
    budget = _number(options.get("budget"))
    store = str(options.get("currency") or "usd")
    max_mv = _number(options.get("max_mana_value")) or 7
    grouped: dict[str, list[dict[str, Any]]] = {role: [] for role in gaps}

    for key, result in found.items():
        uuid = result.get("uuid")
        if not uuid:
            continue
        row = _candidate_row(db, uuid)
        if not row:
            continue
        name = str(row["name"] or "")
        if _front_name(name) in deck_names:
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
        price = _cached_price(str(row.get("scryfallId") or ""), store)
        if budget is not None and price is not None and price > budget:
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
            "price": price,
            "priceUnknown": price is None,
            "oracleExcerpt": str(row.get("text") or "")[:240],
        }
        # Avoid the same card landing in several role buckets.
        already = any(
            existing["name"].lower() == name.lower()
            for bucket in grouped.values()
            for existing in bucket
        )
        if already:
            continue
        grouped.setdefault(role, []).append(candidate)

    for role, bucket in grouped.items():
        bucket.sort(key=lambda item: item["score"], reverse=True)
        del bucket[4:]

    return {role: bucket for role, bucket in grouped.items() if bucket}


def cut_candidates(deck: Mapping[str, Any], audit: Mapping[str, Any],
                   limit: int = 15) -> list[dict[str, Any]]:
    """Build a deterministic cut shortlist from surplus, curve, and metadata."""
    commander = deck.get("commander") if isinstance(deck.get("commander"), Mapping) else {}
    commander_name = _front_name(commander.get("name"))
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
        if _front_name(name) == commander_name:
            continue
        info = classify_roles(card)
        if "Lands" in info["roles"] and "Basic" in str(card.get("supertypes") or ""):
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
            elif actual == target and target > 0:
                score += 0.5
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
            "budget": options.get("budget"),
            "currency": options.get("currency"),
            "max_adds": options.get("max_adds", 10),
            "max_cuts": options.get("max_cuts", 10),
        },
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
            )
            if not isinstance(data, dict):
                raise ValueError("LLM returned a non-object response.")
            return data, None
        except Exception as exc:
            last_error = str(exc)
            if attempt == 0:
                user += "\n\nYour previous response was not valid JSON. Return ONLY valid JSON."
    return None, last_error


def _verify_llm(
    deck: Mapping[str, Any],
    analysis: Mapping[str, Any] | None,
    candidates: Mapping[str, list[dict[str, Any]]],
    options: Mapping[str, Any],
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
    deck_names = [
        str(card.get("name") or "")
        for card in (deck.get("cards") or [])
        if isinstance(card, Mapping)
    ]
    candidate_names = [
        str(card.get("name") or "")
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
        "candidate_names": candidate_names,
        "analysis": analysis,
    }, indent=2)
    try:
        data = llm_generate(
            DECK_REVIEW_VERIFY_SYSTEM_PROMPT, user,
            backend=llm.get("backend") or None,
            api_key=api_key,
            base_url=llm.get("base_url") or None,
            model=llm.get("model") or None,
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
    for item in (raw_adds if isinstance(raw_adds, list) else [])[:max_adds]:
        if not isinstance(item, Mapping):
            continue
        name = str(item.get("name") or "").strip()
        key = _front_name(name)
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
    for item in (raw_cuts if isinstance(raw_cuts, list) else [])[:max_cuts]:
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
        if any(_front_name(entry.get("name")) == key for entry in cuts):
            continue
        cuts.append(dict(item))

    result = dict(analysis)
    result["adds"] = adds
    result["cuts"] = cuts
    return result, {"warnings": warnings, "rejected": rejected}


def run_review_job(job_id: str, deck: Mapping[str, Any], options: Mapping[str, Any]) -> None:
    """Run one review job; updates the review cache as it progresses."""
    started = time.time()
    db = None
    try:
        review_cache.update(job_id, status="running", step=1, label="Loading and enriching deck…")
        db = sqlite3.connect(db_path(DATABASE))
        db.row_factory = sqlite3.Row
        enriched = enrich_deck(db, deck)

        review_cache.update(job_id, step=2, label="Auditing deck structure…")
        guideline = _guideline(str(options.get("guideline") or deck.get("guideline") or ""))
        audit = analyze_deck(
            enriched,
            guideline=guideline,
            target_bracket=options.get("target_bracket"),
            combo_count=options.get("combo_count"),
        )

        review_cache.update(job_id, step=3, label="Finding candidate cards…")
        candidates = candidate_adds(db, enriched, audit, options)
        cuts = cut_candidates(enriched, audit)

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
            max_adds=int(options.get("max_adds") or 10),
            max_cuts=int(options.get("max_cuts") or 10),
        )
        verification = None
        verify_error = None
        if options.get("verify_llm") and analysis:
            review_cache.update(job_id, step=5, label="Second-pass LLM verification…")
            verification, verify_error = _verify_llm(
                enriched, analysis, candidates, options
            )

        result = {
            "schemaVersion": 1,
            "deck": {
                "name": deck.get("name", ""),
                "fingerprint": deck_fingerprint(enriched, fingerprint_options(options)),
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
                "budget": options.get("budget"),
                "currency": options.get("currency"),
                "use_llm": bool(options.get("use_llm")),
                "verify_llm": bool(options.get("verify_llm")),
                "max_adds": options.get("max_adds"),
                "max_cuts": options.get("max_cuts"),
                "guideline": options.get("guideline"),
            },
            "pipeline": {
                "embed": sum(len(bucket) for bucket in candidates.values()),
                "llm": analysis is not None,
                "llm_error": llm_error,
                "llm_verify": verification is not None,
                "llm_verify_error": verify_error,
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


def review_to_markdown(report: Mapping[str, Any]) -> str:
    """Render a saved review or job result as Markdown."""
    import html as _html

    audit = report.get("audit") if isinstance(report.get("audit"), Mapping) else {}
    analysis = report.get("analysis") if isinstance(report.get("analysis"), Mapping) else {}
    bracket = audit.get("bracket") if isinstance(audit.get("bracket"), Mapping) else {}
    salt = bracket.get("salt") if isinstance(bracket.get("salt"), Mapping) else {}
    health = audit.get("health") if isinstance(audit.get("health"), Mapping) else {}
    candidates = report.get("candidates") if isinstance(report.get("candidates"), Mapping) else {}
    adds = candidates.get("adds") if isinstance(candidates.get("adds"), Mapping) else {}
    cuts = analysis.get("cuts") or candidates.get("cuts") or []
    if not isinstance(cuts, list):
        cuts = []

    lines = [
        f"# Deck Doctor — {_report_deck_name(report) or 'Untitled deck'}",
        "",
        f"**Health:** {health.get('score', '—')}/100  ",
        f"**Bracket:** {bracket.get('estimate', '—')}"
        + (f" · Salt {salt.get('score')}" if salt.get("score") is not None else ""),
        "",
    ]
    if analysis.get("verdict"):
        lines += [f"**Verdict:** {analysis['verdict']}", ""]
    if analysis.get("summary"):
        lines += [str(analysis["summary"]), ""]

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
    add_items = analysis.get("adds") if isinstance(analysis.get("adds"), list) else []
    if add_items:
        for item in add_items:
            if isinstance(item, Mapping):
                lines.append(
                    f"- **{item.get('name', '')}** — "
                    f"{item.get('reason', '')}"
                    + (f" ({item.get('role')})" if item.get("role") else "")
                )
    elif isinstance(adds, Mapping) and adds:
        for role, bucket in adds.items():
            for card in bucket or []:
                lines.append(
                    f"- **{card.get('name', '')}** — {card.get('reasonHint', '')} ({role})"
                )
    else:
        lines.append("- None recommended.")
    lines.append("")

    if analysis.get("mana_base"):
        lines += ["## Mana base", "", str(analysis["mana_base"]), ""]
    if analysis.get("win_conditions"):
        lines += ["## Win conditions", "", str(analysis["win_conditions"]), ""]
    if analysis.get("bracket_fit"):
        lines += ["## Bracket fit", "", str(analysis["bracket_fit"]), ""]

    rejected = (report.get("validation") or {}).get("rejected") or []
    if rejected:
        lines += ["## Rejected suggestions", ""]
        for item in rejected:
            if isinstance(item, Mapping):
                lines.append(f"- {item.get('name', '')} — {item.get('reason', '')}")
        lines.append("")

    return "\n".join(lines).strip() + "\n"


def review_to_html(report: Mapping[str, Any]) -> str:
    """Render a saved review as a small self-contained HTML document."""
    import html as _html

    md = review_to_markdown(report)
    body_parts = []
    for block in md.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        if block.startswith("# "):
            body_parts.append(f"<h1>{_html.escape(block[2:].strip())}</h1>")
        elif block.startswith("## "):
            body_parts.append(f"<h2>{_html.escape(block[3:].strip())}</h2>")
        elif block.startswith("- "):
            items = [
                line[2:].strip() for line in block.splitlines()
                if line.startswith("- ")
            ]
            body_parts.append("<ul>" + "".join(
                f"<li>{_html.escape(item)}</li>" for item in items
            ) + "</ul>")
        else:
            body_parts.append(f"<p>{_html.escape(block)}</p>")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>Deck Doctor — {_html.escape(_report_deck_name(report) or 'Deck')}</title>
<style>
body{{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem;line-height:1.55;color:#1f2328}}
h1{{border-bottom:1px solid #d0d7de;padding-bottom:.4rem}} h2{{margin-top:1.6rem;font-size:1.1rem}}
li{{margin:.3rem 0}} code{{background:#f6f8fa;padding:.1rem .25rem;border-radius:3px}}
</style></head><body>
{''.join(body_parts)}
</body></html>"""
