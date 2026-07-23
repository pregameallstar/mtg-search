"""Image URL building and mana symbol rendering helpers.

ponytail: pure functions, no Flask dependency.
Image proxy fetching lives here so serve_image can delegate to it.
"""

import os
import re
import time
from urllib.request import Request, urlopen

# --- Image URL builder ---


def card_image_url(scryfall_id, face="front", size="normal"):
    """Local image URL — served by proxy route, which caches from Scryfall on first hit."""
    if not scryfall_id:
        return ""
    c1, c2 = scryfall_id[0], scryfall_id[1]
    return f"/img/{size}/{face}/{c1}/{c2}/{scryfall_id}.jpg"


# --- Image fetching (used by serve_image route) ---


def fetch_scryfall_image(image_dir, scryfall_id, size, face):
    """Fetch image from Scryfall with retry, cache to disk.

    Returns the local file path on success, or None on failure.
    ponytail: fetch from Scryfall with retry. 30-image search grid
    bursts 30 concurrent urlopens, which Scryfall drops. Exponential
    backoff gives each one a chance to land.
    """
    c1, c2 = scryfall_id[0], scryfall_id[1]
    full_path = os.path.join(image_dir, size, face, c1, c2, f"{scryfall_id}.jpg")

    if os.path.isfile(full_path):
        return full_path

    scryfall_url = f"https://cards.scryfall.io/{size}/{face}/{c1}/{c2}/{scryfall_id}.jpg"
    last_err = None
    for attempt in range(3):
        try:
            req = Request(scryfall_url, headers={"User-Agent": "mtg-search/1.0"})
            with urlopen(req, timeout=10) as resp:
                img_data = resp.read()
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "wb") as f:
                f.write(img_data)
            return full_path
        except Exception as e:
            last_err = e
            time.sleep(0.5 * (2 ** attempt))
    return None


# --- Mana symbol helpers ---


def mana_symbols(cost):
    """Replace {W}{U}{B}{R}{G}{C}{T} etc with styled spans."""
    if not cost:
        return ""
    SYMBOLS = {
        "{W}": "w", "{U}": "u", "{B}": "b", "{R}": "r", "{G}": "g",
        "{C}": "c", "{S}": "s", "{T}": "tap",
        "{W/P}": "wp", "{U/P}": "up", "{B/P}": "bp", "{R/P}": "rp", "{G/P}": "gp",
        "{E}": "e", "{PW}": "pw",
        # Hybrid pairs (two-color)
        "{W/U}": "wu", "{U/B}": "ub", "{B/R}": "br", "{R/G}": "rg", "{G/W}": "gw",
        "{W/B}": "wb", "{U/R}": "ur", "{B/G}": "bg", "{R/W}": "rw", "{G/U}": "gu",
        # Generic/color hybrid
        "{2/W}": "2w", "{2/U}": "2u", "{2/B}": "2b", "{2/R}": "2r", "{2/G}": "2g",
    }
    result = cost
    for text, cls in SYMBOLS.items():
        result = result.replace(text, f'<i class="ms ms-{cls}"></i>')
    # Fallback: bare generic/colorless — {X}, {2}, {10}, etc.
    result = re.sub(r'\{(X|\d+)\}', r'<i class="ms ms-\1"></i>', result)
    return result
