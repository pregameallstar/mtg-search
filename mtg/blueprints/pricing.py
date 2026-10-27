"""Card pricing — Scryfall API with disk cache."""

import json
import os
import time
from datetime import datetime, timezone
from urllib.request import Request, urlopen
from urllib.error import URLError

from flask import Blueprint, request, jsonify

from mtg.shared import PRICES_DIR
from mtg.images import is_scryfall_id
from mtg.storage import write_json_atomic

pricing_bp = Blueprint("pricing", __name__)

CACHE_TTL_SECONDS = 24 * 3600  # 24 hours
SCRYFALL_API = "https://api.scryfall.com"


def _cache_path(scryfall_id):
    if not is_scryfall_id(scryfall_id):
        return None
    return os.path.join(PRICES_DIR, f"{scryfall_id}.json")


def _fetch_scryfall_prices(scryfall_id):
    """Fetch card prices from Scryfall API with retry.

    Returns price dict on success, None on failure.
    """
    if not is_scryfall_id(scryfall_id):
        return None
    url = f"{SCRYFALL_API}/cards/{scryfall_id}"
    last_err = None
    for attempt in range(3):
        try:
            req = Request(url, headers={"User-Agent": "mtg-search/1.0", "Accept": "application/json"})
            with urlopen(req, timeout=10) as resp:
                card_data = json.loads(resp.read())
            prices = card_data.get("prices", {})
            # Strip nulls
            clean = {k: v for k, v in prices.items() if v is not None}
            return clean
        except (URLError, OSError, json.JSONDecodeError) as e:
            last_err = e
            time.sleep(0.5 * (2 ** attempt))
    return None


def _get_prices(scryfall_id):
    """Return prices for a single card, from cache or fresh fetch.

    Returns (prices_dict, is_cached).
    """
    fpath = _cache_path(scryfall_id)
    if fpath is None:
        return None, False
    now = datetime.now(timezone.utc)

    # Check cache
    if os.path.isfile(fpath):
        try:
            with open(fpath) as f:
                cached = json.load(f)
            if not isinstance(cached, dict):
                raise ValueError("cache entry is not an object")
            fetched_at = cached.get("fetched_at", "")
            if fetched_at:
                fetched_dt = datetime.fromisoformat(fetched_at)
                if fetched_dt.tzinfo is None:
                    fetched_dt = fetched_dt.replace(tzinfo=timezone.utc)
                age = (now - fetched_dt).total_seconds()
                if age < CACHE_TTL_SECONDS:
                    return cached["prices"], True
        except (json.JSONDecodeError, KeyError, ValueError, TypeError):
            pass

    # Cache miss or stale — fetch fresh
    prices = _fetch_scryfall_prices(scryfall_id)
    if prices is not None:
        cache_entry = {
            "scryfallId": scryfall_id,
            "prices": prices,
            "fetched_at": now.isoformat(),
        }
        try:
            write_json_atomic(fpath, cache_entry)
        except OSError:
            pass
        return prices, False

    # Fetch failed — return stale cache if available
    if os.path.isfile(fpath):
        try:
            with open(fpath) as f:
                cached = json.load(f)
            if isinstance(cached, dict):
                return cached["prices"], True  # stale cache
        except (json.JSONDecodeError, KeyError, TypeError, OSError):
            pass

    return None, False


@pricing_bp.route("/api/pricing/fetch", methods=["POST"])
def api_pricing_fetch():
    """Fetch prices for a list of scryfallIds.

    Body: {"scryfallIds": ["id1", "id2", ...]}

    Returns {"success": True, "prices": {"id1": {"usd": "...", ...}, ...}}
    """
    body = request.get_json(silent=True) or {}
    ids = body.get("scryfallIds", [])
    if not ids or not isinstance(ids, list):
        return jsonify({"error": "scryfallIds array required."}), 400
    if len(ids) > 200:
        return jsonify({"error": "Maximum 200 IDs per request."}), 400

    result = {}
    for sid in ids:
        if not is_scryfall_id(sid):
            continue
        prices, _ = _get_prices(sid)
        if prices is not None:
            result[sid] = prices

    return jsonify({"success": True, "prices": result})


@pricing_bp.route("/api/pricing/status", methods=["GET"])
def api_pricing_status():
    """Return pricing cache stats."""
    if not os.path.isdir(PRICES_DIR):
        return jsonify({"cache_count": 0})

    files = [f for f in os.listdir(PRICES_DIR) if f.endswith(".json")]
    return jsonify({"cache_count": len(files)})
