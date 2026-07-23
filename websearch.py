"""Web search via SearXNG for Commander eval pipeline.

ponytail: self-contained HTTP request, one function, one config var.
"""

import json
import os
import urllib.error
import sys
from urllib.parse import quote
from urllib.request import Request, urlopen

SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://localhost:8888")


def web_search(query, max_results=10):
    """Query SearXNG, return list of {title, url, snippet} dicts.

    Returns None when SearXNG is unreachable (connection refused / DNS failure),
    so callers can distinguish "server down" from "genuinely no results".
    Also returns None on JSON decode errors (proxy/captcha page).
    Empty results from a working server returns [].
    """
    # ponytail: encode slashes so DFC names like "A // B" don't break query strings
    safe_query = quote(query, safe='')
    url = f"{SEARXNG_URL}/search?q={safe_query}&format=json&categories=general&language=en"
    try:
        req = Request(url, headers={"User-Agent": "mtg-search/1.0"})
        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        return [
            {"title": r["title"], "url": r.get("url", ""),
             "snippet": r.get("content", "")[:500]}
            for r in data.get("results", [])[:max_results]
        ]
    except (urllib.error.URLError, OSError) as e:
        print(f"web_search error: {e}", file=sys.stderr)
        return None
    except json.JSONDecodeError as e:
        print(f"web_search JSON error: {e}", file=sys.stderr)
        return None
