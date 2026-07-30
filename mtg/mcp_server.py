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

from mtg.shared import db_path, color_identity_subset

DATABASE = os.environ.get("MTG_DATABASE", "AllPrintings.sqlite")


# ponytail: db_path(DATABASE) lives in shared.db_path — imported above

mcp = FastMCP(
    "mtg-search",
    instructions="""Magic: The Gathering card search engine with three tools.

CRITICAL — NO HALLUCINATION:
- Every card name, mana value, color identity, type, and oracle text you
  report MUST come from a tool result. Never fill in card details from
  training data — the tools are the only source of truth.
- If a tool returns nothing, say so. Never invent cards to satisfy a query.
- When building a table or list, copy each field verbatim from the tool
  response. Do not paraphrase, summarize, or "correct" oracle text.
- If you didn't call a tool for a card, you cannot know anything about it.

TOOL SELECTION — pick based on query SHAPE, not just content:

Query is about WHAT a card DOES (mechanics, gameplay concepts):
  → semantic_search. Describe the mechanic literally:
    "Aristocrats" → "sacrifice a creature to drain opponents"
    "Voltron"     → "equip and aura synergy, combat damage trigger"
    "Landfall"    → "whenever a land enters the battlefield"
    "Wheels"      → "each player discards their hand and draws seven cards"
    "Stax"        → "opponents' spells cost more, permanents enter tapped"
    "Group Hug"   → "each player draws additional cards"
    "Group Slug"  → "whenever an opponent does something, they lose life"
    "Superfriends"→ "planeswalker synergy and proliferate"
    "Spellslinger"→ "whenever you cast an instant or sorcery"
    "Reanimator"  → "return target creature card from graveyard to battlefield"
    "Blink"       → "exile target creature then return it to the battlefield"
    "Go-wide"     → "create multiple creature tokens"
    "Go-tall"     → "put +1/+1 counters on target creature, double power"
    "Mill"        → "target player puts cards from library into graveyard"
    "+1/+1 counters" → "put a +1/+1 counter on target creature"

Query is about card ATTRIBUTES (colors, type, MV, name patterns):
  → keyword_search with the filters applied directly. Do NOT translate
    attribute queries into mechanical descriptions:
    ✓ "commander with exactly 3 colors" → keyword_search with color_identity
    ✓ "find cards named 'Sword of'"     → keyword_search(query="Sword of")
    ✓ "Legendary Creature, MV ≤ 4"      → semantic_search with card_type="Legendary Creature"
                                           and mana_value_max filter
    ✗ Do NOT feed attribute queries to semantic_search as mechanics

Query mixes mechanics + attributes (most common):
  → semantic_search with color_identity/card_type/mana_value filters set:
    "+1/+1 counter commander, MV ≤ 3" →
       semantic_search(query="+1/+1 counter synergy commander",
                       card_type="Legendary Creature", mana_value_max=3)
    "3-color +1/+1 counter commander, MV ≤ 4" →
       semantic_search(query="+1/+1 counter synergy commander",
                       color_identity="WUB"/"UBR"/etc., mana_value_max=4)
    Tip: for "exactly N colors", the color_identity filter is a subset match
    so you must try specific N-color combinations or search broad and verify.

keyword_search — NOT just a fallback. It's the right tool when:
  - The user provides a card name fragment ("Sword of", "Liliana")
  - The query is structured attributes with no mechanical concept
  - semantic_search returned nothing and you need to try literal matching
  - You need exact text patterns in oracle text ("destroy target creature")

get_card — fetch full details (rulings, legalities, P/T, loyalty, set info)
  by exact English name. Use after identifying candidates.

FILTERS — apply during semantic_search/keyword_search; do not post-filter:
- color_identity: SUBSET match. "UG" returns U, G, UG, and colorless cards.
  It does NOT return UBR or WUBRG. Use short form: "W", "UG", "UBR", "WUBRG".
  Leave empty for any color identity.
- card_type: substring match against the full type line (case-insensitive).
  "Legendary Creature" matches "Legendary Creature — Human Warrior",
  "Legendary Artifact Creature — Construct", etc. Leave empty for any type.
- mana_value_min / mana_value_max: mana value range (inclusive). Defaults
  are 0–99 (no filter). Set tighter ranges when the user specifies a curve.

IMPORTANT BEHAVIORS:
- semantic_search returns oracle_text for every card. ALWAYS read it — do not
  rely on card names alone. The embedding model catches mechanical cousins,
  which means a searched-for name might not appear in the text.
- If the first query returns nothing useful, broaden the mechanical description
  (fewer specifics, wider concept) or try keyword_search as a fallback.
- The database covers ~35,000 unique English cards across all of Magic's
  history. Be thorough before concluding a card does not exist.
- When suggesting cards for a Commander deck, always verify color_identity
  compatibility. Cards in the 99 must have a color identity that is a subset
  of the commander's color identity.
- set limit higher (30–50) when you intend to re-rank a large candidate pool
  yourself. Use the default (15) for quick lookups.""",
)


# ── helpers ──────────────────────────────────────────────────────────────


def _get_db():
    db = sqlite3.connect(db_path(DATABASE))
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


# ── tools ────────────────────────────────────────────────────────────────


@mcp.tool()
def semantic_search(
    query: str,
    color_identity: str = "",
    card_type: str = "",
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
    card_type: substring match against the type line, e.g.
               "Legendary Creature", "Instant", "Artifact".
               Empty = any type.
    mana_value_min/max: MV range filter (inclusive).
    limit: max results (default 15). Set higher for broader coverage
           when you'll re-rank yourself.
    """
    import embed

    results = embed.find(db_path(DATABASE), query, top_k=800)
    if not results:
        return []

    allowed_ci = _parse_ci(color_identity)

    filtered = []
    for r in results:
        mv = r.get("manaValue") or 0
        if mv < mana_value_min or mv > mana_value_max:
            continue
        if allowed_ci and not color_identity_subset(r.get("colorIdentity", ""), allowed_ci):
            continue
        if card_type and card_type.lower() not in r.get("type", "").lower():
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
    game: str = "",
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
    game: limit to cards available in this game (paper, mtgo, arena).
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

        if game:
            where.append("c.availability LIKE ?")
            params.append(f"%{game}%")

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

        # ponytail: dedup by name, pick latest printing.
        # ROW_NUMBER() with tiebreakers ensures deterministic results when
        # multiple printings share the same releaseDate.
        rows = db.execute(
            f"""SELECT * FROM (
                    SELECT c.name, c.manaCost, c.manaValue, c.type, c.text,
                           c.colors, c.colorIdentity, c.keywords, c.rarity,
                           c.power, c.toughness, c.setCode, c.number,
                           ci.scryfallId, s.name as setName,
                           ROW_NUMBER() OVER (
                               PARTITION BY c.name
                               ORDER BY s.releaseDate DESC, c.setCode, c.number
                           ) as rn
                    FROM cards c
                    JOIN cardIdentifiers ci ON c.uuid = ci.uuid
                    JOIN sets s ON c.setCode = s.code
                    WHERE c.language = 'English'
                      AND (c.side IS NULL OR c.side = 'a')
                      AND {' AND '.join(where)}
                ) WHERE rn = 1
                ORDER BY name
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
