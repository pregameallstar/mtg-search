"""ponytail: MCP server — semantic card search for LLM agents.

Thin wrapper over embed.find() and SQLite. No Flask, no auth, read-only.

Two-pass retrieval: the LLM translates user slang → mechanics, embed model
returns candidates with oracle text, the LLM re-ranks against original intent.

Usage:
    python mcp_server.py                          # stdio (local MCP client)
    python mcp_server.py --transport sse --port 8765  # SSE (remote agents)
"""

import argparse
import os
import re
import sqlite3
import sys

from mcp.server.fastmcp import FastMCP

DATABASE = os.environ.get("MTG_DATABASE", "AllPrintings.sqlite")


def _db_path():
    """Return the actual SQLite database path."""
    if os.path.isfile(DATABASE):
        return DATABASE
    if os.path.isdir(DATABASE):
        dbs = sorted(
            [f for f in os.listdir(DATABASE) if f.endswith(".sqlite")],
            key=lambda x: os.path.getmtime(os.path.join(DATABASE, x)),
            reverse=True,
        )
        if dbs:
            return os.path.join(DATABASE, dbs[0])
    return DATABASE

mcp = FastMCP(
    "mtg-search",
    instructions="Magic: The Gathering card search engine — semantic similarity + keyword lookup",
)


# ── helpers ──────────────────────────────────────────────────────────────


def _get_db():
    db = sqlite3.connect(_db_path())
    db.row_factory = sqlite3.Row
    return db


_COLOR_MAP = {
    "W": "W", "WHITE": "W",
    "U": "U", "BLUE": "U",
    "B": "B", "BLACK": "B",
    "R": "R", "RED": "R",
    "G": "G", "GREEN": "G",
    "C": "C", "COLORLESS": "C",
}


def _parse_ci(raw: str) -> set[str]:
    """'GW' → {'G','W'}, 'Blue/Red' → {'U','R'}, 'BU' → {'B','U'}"""
    if not raw:
        return set()
    chars = []
    raw = raw.upper().strip()
    # ponytail: split on slash/comma/space, then also split bare letter strings
    for part in re.split(r"[/, ]+", raw):
        part = part.strip()
        if not part:
            continue
        if part in _COLOR_MAP:
            chars.append(_COLOR_MAP[part])
        elif all(c in "WUBRGC" for c in part):
            chars.extend(part)  # "GW" → 'G', 'W'
    return set(chars)


def _ci_subset(card_ci: str, allowed: set[str]) -> bool:
    """True if every color in card_ci is also in allowed."""
    if not card_ci or not card_ci.strip():
        return True  # colorless goes anywhere
    card_colors = set(card_ci.replace(" ", ""))
    return card_colors <= allowed


# ── tools ────────────────────────────────────────────────────────────────


@mcp.tool()
def semantic_search(
    query: str,
    color_identity: str = "",
    mana_value_max: float = 99,
    mana_value_min: float = 0,
    limit: int = 15,
) -> list[dict]:
    """Find cards by mechanical description — semantic, not keyword.

    The embedding model maps your description into the same vector space
    as oracle text. Describe what the card DOES in literal terms, not
    community slang:

        ✓ "sacrifice a creature to drain opponents"    ✗ "Aristocrats"
        ✓ "equip and aura synergy, combat damage"      ✗ "Voltron"
        ✓ "whenever a land enters the battlefield"     ✗ "Landfall"
        ✓ "exile target creature, destroy target artifact"

    All results include the full oracle text. Read it and re-rank against
    the user's original intent — the embedding model is approximate.

    color_identity: e.g. "GW", "Blue/Red", "UBR". Card CI must be a
                    subset.  Empty = any color.
    mana_value_min/max: MV range filter (inclusive).
    limit: max results (default 15). Set higher for broader coverage
           when you'll re-rank yourself.
    """
    import embed

    results = embed.find(_db_path(), query, top_k=200)
    if not results:
        return []

    allowed_ci = _parse_ci(color_identity)

    filtered = []
    for r in results:
        mv = r.get("manaValue") or 0
        if mv < mana_value_min or mv > mana_value_max:
            continue
        if allowed_ci and not _ci_subset(r.get("colorIdentity", ""), allowed_ci):
            continue
        filtered.append({
            "name": r["name"],
            "manaValue": r.get("manaValue"),
            "type": r.get("type", ""),
            "colorIdentity": r.get("colorIdentity", ""),
            "oracle_text": r.get("text", ""),
            "keywords": r.get("keywords", ""),
            "score": round(r.get("score", 0), 4),
        })
        if len(filtered) >= limit:
            break

    return filtered


@mcp.tool()
def keyword_search(
    query: str,
    color_identity: str = "",
    card_type: str = "",
    mana_value_max: float = 99,
    mana_value_min: float = 0,
    limit: int = 30,
) -> list[dict]:
    """Literal text search against card name, type line, and oracle text.

    Use this as a FALLBACK when semantic_search misses or when the user
    needs exact Oracle-text matching (e.g. "destroy target creature").

    query: text matched via LIKE against name, type, and oracle text.
    color_identity: subset filter — card CI must be within these colors.
                    e.g. "UG" matches UG, U, G, colorless. Not UBR.
    card_type: filter by type line, e.g. "Legendary Creature", "Instant".
    mana_value_min/max: MV range filter (inclusive).
    limit: max results (default 30).
    """
    db = _get_db()
    try:
        where = ["c.language = 'English'", "(c.side IS NULL OR c.side = 'a')"]
        params = []

        if query:
            escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append(
                "(c.name LIKE ? ESCAPE '\\' OR c.type LIKE ? ESCAPE '\\' "
                "OR c.text LIKE ? ESCAPE '\\')"
            )
            params.extend([f"%{escaped}%"] * 3)

        if card_type:
            escaped = card_type.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("c.type LIKE ? ESCAPE '\\'")
            params.append(f"%{escaped}%")

        if color_identity:
            allowed_ci = _parse_ci(color_identity)
            # ponytail: only exclude colors NOT in allowed set.
            # CI="GW" matches G, W, GW, and colorless — not UG or UBR.
            for ch in sorted({"W", "U", "B", "R", "G"} - allowed_ci):
                where.append("c.colorIdentity NOT LIKE ?")
                params.append(f"%{ch}%")

        if mana_value_min > 0:
            where.append("(c.manaValue >= ? OR c.manaValue IS NULL)")
            params.append(mana_value_min)
        if mana_value_max < 99:
            where.append("(c.manaValue <= ? OR c.manaValue IS NULL)")
            params.append(mana_value_max)

        # ponytail: dedup by name, pick latest printing
        rows = db.execute(
            f"""SELECT c.name, c.manaCost, c.manaValue, c.type, c.text,
                       c.colors, c.colorIdentity, c.keywords, c.rarity,
                       c.power, c.toughness, c.setCode, c.number,
                       ci.scryfallId, s.name as setName
                FROM cards c
                JOIN cardIdentifiers ci ON c.uuid = ci.uuid
                JOIN sets s ON c.setCode = s.code
                JOIN (
                    SELECT c2.name, MAX(s2.releaseDate) as maxDate
                    FROM cards c2
                    JOIN sets s2 ON c2.setCode = s2.code
                    WHERE c2.language = 'English'
                      AND (c2.side IS NULL OR c2.side = 'a')
                    GROUP BY c2.name
                ) latest ON c.name = latest.name AND s.releaseDate = latest.maxDate
                WHERE {' AND '.join(where)}
                GROUP BY c.name
                ORDER BY c.name
                LIMIT ?""",
            params + [limit],
        ).fetchall()

        return [dict(r) for r in rows]
    finally:
        db.close()


@mcp.tool()
def get_card(name: str) -> dict:
    """Get full card details by exact English name.

    Returns name, manaCost, manaValue, type, oracle text (text field),
    colors, colorIdentity, power/toughness, loyalty, keywords, rarity,
    set info, scryfallId, and Oracle rulings.
    """
    db = _get_db()
    try:
        card = db.execute(
            """SELECT c.*, ci.scryfallId, s.name as setName, s.releaseDate
               FROM cards c
               JOIN cardIdentifiers ci ON c.uuid = ci.uuid
               JOIN sets s ON c.setCode = s.code
               WHERE c.name = ? AND c.language = 'English'
                 AND (c.side IS NULL OR c.side = 'a')
               ORDER BY s.releaseDate DESC
               LIMIT 1""",
            [name],
        ).fetchone()

        if not card:
            return {"error": f"Card '{name}' not found."}

        rulings = db.execute(
            "SELECT date, text FROM cardRulings WHERE uuid = ? ORDER BY date DESC",
            [card["uuid"]],
        ).fetchall()

        legalities = db.execute(
            "SELECT * FROM cardLegalities WHERE uuid = ?",
            [card["uuid"]],
        ).fetchone()

        card_dict = dict(card)
        card_dict["rulings"] = [dict(r) for r in rulings]
        card_dict["legalities"] = dict(legalities) if legalities else {}

        return card_dict
    finally:
        db.close()


# ── entry ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MTG Search MCP Server")
    parser.add_argument(
        "--transport", choices=["stdio", "sse", "streamable-http"],
        default="stdio",
        help="Transport protocol (default: stdio)",
    )
    parser.add_argument(
        "--port", type=int, default=8765,
        help="Port for HTTP transports (default: 8765)",
    )
    parser.add_argument(
        "--host", default="0.0.0.0",
        help="Host to bind HTTP transports (default: 0.0.0.0)",
    )
    args = parser.parse_args()

    if args.transport in ("sse", "streamable-http"):
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        # ponytail: allow LAN access — disable DNS rebinding protection
        from mcp.server.fastmcp.server import TransportSecuritySettings
        mcp.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        )

        import uvicorn
        from starlette.middleware.cors import CORSMiddleware

        # ponytail: CORS — OpenWebUI browser client needs OPTIONS preflight
        app = (
            mcp.streamable_http_app()
            if args.transport == "streamable-http"
            else mcp.sse_app()
        )
        app = CORSMiddleware(
            app,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )
        print(
            f"MCP server starting on http://{args.host}:{args.port} ({args.transport})",
            file=sys.stderr,
        )
        uvicorn.run(app, host=args.host, port=args.port)
    else:
        mcp.run(transport=args.transport)
