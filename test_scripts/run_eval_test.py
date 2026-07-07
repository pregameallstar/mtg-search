#!/usr/bin/env python3
"""End-to-end test for the Commander Evaluation + Deep Dive flows.

Picks N random commanders, runs analyze + deepdive on each, and reports results.

Usage:
    python test_scripts/run_eval_test.py [N] --api-key sk-...

    N defaults to 1. The server must already be running: ./run.sh start

    Pass --api-key to seed LLM config into the script's Flask session.
    The script will POST these to /config before running tests.
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# --- Paths ---
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "AllPrintings.sqlite"
BASE_URL = "http://127.0.0.1:5000"


def pick_random_commanders(n: int) -> list[dict]:
    """Return N random commander-legal legendary creatures from the DB."""
    db = sqlite3.connect(str(DB_PATH))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only = ON")

    rows = db.execute("""
        SELECT c.name, c.setCode, c.number
        FROM cards c
        JOIN cardLegalities cl ON c.uuid = cl.uuid
        WHERE c.language = 'English'
          AND (c.side IS NULL OR c.side = 'a')
          AND c.supertypes LIKE '%Legendary%'
          AND c.types LIKE '%Creature%'
          AND cl.commander = 'Legal'
        ORDER BY RANDOM()
        LIMIT ?
    """, [n]).fetchall()

    db.close()
    return [{"name": r["name"], "set_code": r["setCode"], "number": r["number"]} for r in rows]


def check_server() -> bool:
    """Return True if the Flask server is reachable."""
    try:
        r = requests.get(f"{BASE_URL}/", timeout=5)
        return r.status_code == 200
    except requests.ConnectionError:
        return False


def seed_config(session: requests.Session, api_key: str, backend: str,
                model: str, base_url: str):
    """POST to /config to seed the Flask session with LLM settings."""
    data = {
        "llm_api_key": api_key,
        "llm_backend": backend,
        "llm_model": model,
        "llm_base_url": base_url,
    }
    r = session.post(f"{BASE_URL}/config", data=data, timeout=10, allow_redirects=True)
    if r.status_code != 200:
        print(f"  WARNING: Config POST returned {r.status_code}", file=sys.stderr)


def run_analyze(session: requests.Session, cmd: dict) -> dict:
    """Run the analyze pipeline for one commander. Returns the cached analysis JSON."""
    set_code = cmd["set_code"]
    number = cmd["number"]

    # Step 1: GET the eval page to seed Flask session (eval_key, eval_progress_key)
    r = session.get(f"{BASE_URL}/card/{set_code}/{number}/eval", timeout=15)
    if r.status_code != 200:
        return {"error": f"Eval page returned {r.status_code}"}

    # Step 2: POST analyze
    r = session.post(
        f"{BASE_URL}/card/{set_code}/{number}/eval/analyze",
        timeout=600,
    )
    if r.status_code != 200:
        return {"error": f"Analyze returned {r.status_code}: {r.text[:200]}"}

    data = r.json()
    if not data.get("success"):
        return {"error": f"Analyze failed: {data.get('error', 'unknown')}"}

    # Step 3: Save to disk and read back (save returns filename, not content)
    r = session.post(
        f"{BASE_URL}/card/{set_code}/{number}/eval/save",
        timeout=15,
    )
    if r.status_code != 200:
        return {"error": f"Save returned {r.status_code}: {r.text[:200]}"}

    result = r.json()
    if not result.get("success"):
        return {"error": f"Save failed: {result.get('error', 'unknown')}"}

    filename = result.get("filename", "")
    report_path = PROJECT_ROOT / "eval_reports" / filename
    if not report_path.exists():
        return {"error": f"Report file not found: {report_path}"}

    with open(report_path) as f:
        report = json.load(f)

    return {"analysis": report.get("analysis", {}), "filename": filename}


def run_deepdive(session: requests.Session, cmd: dict, expand_type: str,
                 item: dict) -> dict:
    """Run a single deepdive analysis and return the result."""
    set_code = cmd["set_code"]
    number = cmd["number"]

    r = session.post(
        f"{BASE_URL}/card/{set_code}/{number}/eval/deepdive",
        json={
            "type": expand_type,
            "name": item["name"],
            "description": item.get("description", ""),
        },
        timeout=600,
    )
    if r.status_code != 200:
        return {"error": f"Deepdive returned {r.status_code}: {r.text[:200]}"}

    data = r.json()
    if not data.get("success"):
        return {"error": f"Deepdive failed: {data.get('error', 'unknown')}"}

    return data.get("data", {})


def main():
    parser = argparse.ArgumentParser(
        description="Test Commander Evaluation + Deep Dive flows"
    )
    parser.add_argument(
        "N", nargs="?", type=int, default=1,
        help="Number of random commanders to test (default: 1)",
    )
    parser.add_argument("--api-key", default=os.environ.get("LLM_API_KEY", ""),
                        help="LLM API key (falls back to LLM_API_KEY env var)")
    parser.add_argument("--backend", default=os.environ.get("LLM_BACKEND", "openai"),
                        help="LLM backend: openai or anthropic (default: openai)")
    parser.add_argument("--model", default=os.environ.get("LLM_MODEL", ""),
                        help="LLM model name (backend default if omitted)")
    parser.add_argument("--base-url", default=os.environ.get("LLM_BASE_URL", ""),
                        help="Custom LLM base URL")
    args = parser.parse_args()
    n = args.N

    # Pre-flight checks
    if not DB_PATH.exists():
        print(f"ERROR: Database not found at {DB_PATH}")
        sys.exit(1)

    if not check_server():
        print(f"ERROR: Server not reachable at {BASE_URL}")
        print("Start it with: ./run.sh start")
        sys.exit(1)

    if not args.api_key:
        print("ERROR: No API key provided.")
        print("Pass --api-key KEY or set LLM_API_KEY in the environment.")
        sys.exit(1)

    print(f"Picking {n} random commander(s)...")
    commanders = pick_random_commanders(n)
    for i, cmd in enumerate(commanders):
        print(f"  {i+1}. {cmd['name']} ({cmd['set_code']}/{cmd['number']})")

    results = []
    for i, cmd in enumerate(commanders):
        print(f"\n{'='*60}")
        print(f"[{i+1}/{n}] Testing: {cmd['name']} ({cmd['set_code']}/{cmd['number']})")
        print(f"{'='*60}")

        session = requests.Session()

        # Seed LLM config into the Flask session
        seed_config(session, args.api_key, args.backend, args.model, args.base_url)

        # --- Analyze ---
        print("  Running analyze pipeline...")
        t0 = time.monotonic()
        result = run_analyze(session, cmd)
        elapsed = time.monotonic() - t0

        if "error" in result:
            print(f"  FAILED: {result['error']}")
            results.append({"commander": cmd, "analyze": result, "deepdives": [], "ok": False})
            session.close()
            continue

        analysis = result["analysis"]
        print(f"  OK ({elapsed:.1f}s) → saved to {result['filename']}")

        # Summarize analysis
        strengths = analysis.get("strengths", [])
        weaknesses = analysis.get("weaknesses", [])
        strategies = analysis.get("strategies", [])
        unique_builds = analysis.get("unique_builds", [])
        kos = analysis.get("kill_on_sight", {})
        print(f"    Strengths: {len(strengths)} | Weaknesses: {len(weaknesses)}")
        print(f"    Strategies: {len(strategies)} | Unique builds: {len(unique_builds)}")
        print(f"    Kill-on-sight: {kos.get('score', '?')}/10")

        # --- Deepdives ---
        dd_results = []
        deepdive_targets = (
            [("strategy", s) for s in strategies] +
            [("unique_build", ub) for ub in unique_builds]
        )

        for dd_type, dd_item in deepdive_targets:
            label = f"{dd_type}: {dd_item['name']}"
            print(f"  Deep-diving: {label}...")
            t0 = time.monotonic()
            dd_result = run_deepdive(session, cmd, dd_type, dd_item)
            dd_elapsed = time.monotonic() - t0

            if "error" in dd_result:
                print(f"    FAILED: {dd_result['error']}")
                dd_results.append({"type": dd_type, "item": dd_item, "error": dd_result["error"]})
                continue

            wc = dd_result.get("win_conditions", [])
            ec = dd_result.get("example_cards", [])
            print(f"    OK ({dd_elapsed:.1f}s) → {len(wc)} win conditions, {len(ec)} example cards")
            dd_results.append({"type": dd_type, "item": dd_item, "result": dd_result, "ok": True})

        results.append({
            "commander": cmd,
            "analyze": {"ok": True, "filename": result["filename"]},
            "deepdives": dd_results,
            "ok": True,
        })
        session.close()

    # --- Report ---
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"{'='*60}")

    ok = sum(1 for r in results if r["ok"])
    total_deepdives = sum(len(r["deepdives"]) for r in results)
    ok_deepdives = sum(
        sum(1 for dd in r["deepdives"] if dd.get("ok")) for r in results
    )

    print(f"Commanders tested: {len(results)} ({ok} analyze OK)")
    print(f"Deepdives: {ok_deepdives}/{total_deepdives} OK")

    for r in results:
        status = "OK" if r["ok"] else "FAIL"
        print(f"  [{status}] {r['commander']['name']} ({r['commander']['set_code']}/{r['commander']['number']})")

    # Write timestamped report
    now = datetime.now(timezone.utc).isoformat().replace(":", "-")
    report_path = PROJECT_ROOT / "test_scripts" / f"test_report_{now}.json"
    with open(report_path, "w") as f:
        json.dump({
            "test_ran_at": datetime.now(timezone.utc).isoformat(),
            "commanders_tested": n,
            "results": results,
        }, f, indent=2, default=str)
    print(f"\nFull report: {report_path}")

    # ponytail: exit status reflects failures
    if ok < len(results) or ok_deepdives < total_deepdives:
        sys.exit(1)


if __name__ == "__main__":
    main()
