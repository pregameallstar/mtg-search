# MTG Search

A self-hosted Magic: The Gathering card search engine, deck builder, and Commander analysis tool. Flask app with blueprints backed by SQLite — no ORM, no JavaScript build step, no cloud dependencies.

## Features

- **Card search** — full Scryfall-style query engine: name, oracle text, type line, mana cost, colors, color identity, power/toughness/loyalty, rarity, set, format legality, game availability, and 10+ boolean flags. 30+ filters in total.
- **Card detail** — full card view with high-res image, oracle text, Oracle rulings, format legalities, all printings across sets, and other faces for DFCs.
- **Deck Builder** — split-panel builder with four integrated tools:
  - **Search** — the full card search engine embedded in a compact panel. Name autocomplete, drag-and-drop card results into the deck, and card image preview on hover.
  - **Similarity** — find mechanically similar cards from the same panel. Same tuning controls as the standalone Similarity tool.
  - **Commander Eval** — run full LLM-powered commander analysis from within the builder. Restore saved analyses with saved decks.
  - **Templates** — reusable card packages for deck-building patterns.
  - Commander slot, custom sections (Sideboard, Maybeboard), drag-and-drop reordering, card counts, local save/load, JSON import/export.
  - **Guideline Templates** — define expected card counts per category (Ramp, Removal, Card Draw, etc.) with progress bars that track your deck against targets. Auto-classification assigns cards to categories; category-name tags override automatic classification.
  - **Tag system** — tag cards with predefined tags (Ramp, Removal, Card Draw, Tutor, Board Wipe, Protection, Recursion, Win Condition) or custom tags. Tags are shared across all decks.
- **Similarity engine** — find cards that play mechanically similarly to a given card. Two search backends:
  - **Semantic** (default) — AI embedding vectors (`all-MiniLM-L6-v2`) map oracle text into vector space. Catches reworded abilities, mechanical cousins, and conceptually similar designs that keyword matching misses. Built once per database ingest, queries run in ~100ms.
  - **Legacy** — classic TF-IDF word overlap with tunable factor gates (types, keywords, subtypes, mana value, color identity). Each factor has a strictness knob. MTG-domain stop-word filter strips ubiquitous terms. Format legality filter for format-restricted searches.
- **Commander Eval** — LLM-powered commander analysis pipeline:
  - Web research via SearXNG (deck guides, strategy discussions, community reviews)
  - Mechanically similar card retrieval from embedding index
  - Structured analysis: strengths, weaknesses, deck-building priorities, strategies, unique builds, bracket ratings (1–5), kill-on-sight scoring
  - Second-pass verification against official Oracle text and rulings
  - Deep-dives on individual strategies and unique builds
  - Save reports to disk, export as self-contained HTML
- **Card Templates** — reusable card packages saved to `card_templates/`. Two types:
  - **Card templates** — pre-built card lists you can load into the Deck Builder
  - **Guideline templates** — category targets (Ramp, Removal, Card Draw, etc.) with expected counts, used to score decks against deck-building standards
- **MCP server** — expose `semantic_search`, `keyword_search`, and `get_card` tools to LLM agents (Claude Code, OpenWebUI, etc.) via SSE + MCPO proxy.
- **Database ingest** — drag-and-drop upload of MTGJSON `AllPrintings.sqlite` (or `.tar.gz`/`.zip` archives). Deduplication, validation, and embedding rebuild run automatically.
- **Lazy image cache** — first card view fetches from Scryfall and caches locally. Subsequent views serve from disk.

## Quick Start

### Prerequisites

- Python 3.12+ (Docker recommended)
- An MTGJSON `AllPrintings.sqlite` file ([download from mtgjson.com](https://mtgjson.com/downloads/all-files/))
- (Optional) Docker + Docker Compose for the containerized setup with SearXNG

### Docker Setup (recommended)

```bash
git clone https://github.com/pregameallstar/mtg-search
cd mtg-search

# Copy the database into the project root
cp ~/Downloads/AllPrintings.sqlite .

# Create .env from template and generate a SearXNG secret
cp .env.example .env
python3 -c "import secrets; print(secrets.token_hex(32))"  # copy the output
# Edit .env and paste the secret: SEARXNG_SECRET=<output>

# Build and start (app + SearXNG)
docker compose up -d

# Open http://localhost:5000
```

The docker-compose stack includes:
- **app** — the Flask application (port 5000), MCP SSE server (8765), and MCPO proxy (8000)
- **searxng** — SearXNG search engine for Commander Eval web research (port 8888, accessible on localhost only)

Code changes to `.py`, `.html`, `.css`, or `.js` files are reflected live via bind mounts — no rebuild required. Only `requirements.txt` changes need `docker compose build app && docker compose up -d app`.

Volume mounts:
| Host path | Container path | Purpose |
|-----------|---------------|---------|
| `./AllPrintings.sqlite` | `/app/AllPrintings.sqlite` | Card database |
| `./embeddings/` | `/app/embeddings/` | Embedding index (persisted) |
| `./images/` | `/app/images/` | Lazy image cache (persisted) |
| `./eval_reports/` | `/app/eval_reports/` | Saved Commander Eval reports |
| `./.secret_key` | `/app/.secret_key` | Flask session secret (writeable) |
| `./.last_ingest.json` | `/app/.last_ingest.json` | Database ingest metadata (writeable) |
| `./decks/` | `/app/decks/` | Saved deck files |
| `./mtg/` | `/app/mtg/` | Application package (live-edit during dev) |
| `./requirements.txt` | `/app/requirements.txt` | Python dependencies |
| `./app.py` | `/app/app.py` | App entry point |
| `./templates/` | `/app/templates/` | Jinja2 templates |
| `./static/` | `/app/static/` | Static assets (CSS + JS) |

Environment variables for the app container:

| Variable | Default | Purpose |
|----------|---------|---------|
| `SEARXNG_URL` | `http://searxng:8080` | SearXNG endpoint (set by compose) |
| `LLM_BACKEND` | `openai` | LLM provider: `openai` or `anthropic` |
| `LLM_BASE_URL` | (empty) | API base URL for OpenAI-compatible backends |
| `LLM_MODEL` | (empty) | Default model name |
| `LLM_API_KEY` | (empty) | API key (also configurable in-app) |
| `PORT` | `5000` | Host port mapping |
| `SEARXNG_HOST_PORT` | `8888` | Host port for SearXNG |
| `FLASK_DEBUG` | (empty) | Set to `1` for debug mode |
| `MCP_SSE_PORT` | `8765` | MCP SSE server port |
| `MCPO_PORT` | `8000` | MCPO proxy port |
| `MCP_HOST` | `0.0.0.0` | MCP server bind address |
| `SECRET_KEY` | (auto-generated) | Flask secret key (written to `.secret_key`) |
| `HOST_UID` | `1000` | Host user UID for file ownership |
| `HOST_GID` | `1000` | Host group GID for file ownership |

## Tools

### Search

`GET /search` — Full card search with 30+ filter fields.

**Text filters:**
- `q` — free-text search across name, type line, and oracle text. Comma-separated, AND logic (cards must match all terms)
- `name` — card name contains (separate from free-text)
- `oracle` — rules text contains. Comma-separated, AND logic (cards must match all terms)
- `type_line` — type line search (normalizes em-dashes so you can type regular hyphens)
- `mana_cost` — exact mana cost, e.g. `{1}{R}{G}`
- `keywords` — comma-separated, AND logic (card must have all listed keywords)

**Color filters:**
- `color` — WUBRGC string, with `color_rule` (`at_least` / `exact` / `at_most`)
- `ci` — color identity string, with `ci_rule`

**Stats:** `mv` (mana value, supports ranges like `1-3` and operators), `pow` (power), `tou` (toughness), `loy` (loyalty)

**Rarity & Set:** `rarity` (multi-select checkboxes), `set` (three-letter set code)

**Legality:** `format` + `legality` (`legal` / `banned` / `restricted`)

**Game / Availability:** `game` — multi-select for `paper`, `mtgo`, `arena`

**Boolean flags:** `is_reprint`, `is_reserved`, `is_funny`, `is_oversized`, `is_fullart`, `is_textless`, `is_promo`, `is_rebalanced`

**Display:** `border` (black/white/silver/gold/yellow/borderless), `layout` (normal/split/transform/saga/etc.), `frame` (1993/1997/2003/2015/future), `unique` (deduplicate by name or show all printings)

Results are paginated (10, 25, or 50 per page). Click any card to open its detail panel. The root URL `/` redirects to `/search`.

### Card Detail

`GET /card/<set_code>/<number>` — Full card view with:
- High-resolution card image (lazy-cached from Scryfall)
- Oracle text and flavor text
- Card stats (P/T, loyalty, mana value, color identity)
- Set info, rarity, artist
- **Oracle Rulings** — official Gatherer rulings sorted by date
- **Format Legalities** — status in every format
- **All Printings** — every set the card has appeared in
- **Other Faces** — back faces for DFCs (transform, MDFC, split cards)
- Quick links: Similar Cards, Commander Eval (for legendary creatures)

Card images are fetched from Scryfall on first view and cached locally in `images/`. Subsequent views serve from disk.

### Deck Builder

`GET /deck-builder` — Split-panel deck building interface with four integrated tools:

**Search tool** — Embedded card search with the same filters as the standalone search page. Results show card images on hover. Drag-and-drop cards into the deck list. Name autocomplete for quick commander selection.

**Similarity tool** — Run the similarity engine from within the builder. Same semantic/legacy modes and tuning controls as the standalone tool. Drag results directly into the deck.

**Commander Eval tool** — Run the full LLM-powered commander analysis pipeline inline. Analysis loads in an iframe; save reports that persist when loading the deck later.

**Templates tool** — Browse and load card templates. Manage guideline templates with category progress bars showing how your deck measures against deck-building targets (Ramp, Removal, Card Draw, etc.).

**Deck management:**
- Commander slot — set your commander with card image
- Custom sections — Sideboard, Maybeboard, and custom named sections by drag-and-drop
- Card counts — track quantity per card
- Local save/load — decks saved as JSON to `decks/`
- JSON import/export — import from or export to `.json` files
- Card image hover preview — hover any card name in the deck list to see a 400px image
- Tags — assign predefined or custom tags to cards (shared across all decks)

**Guideline Templates** — Define category targets as reusable templates:
- Categories: Lands, Ramp, Removal, Card Draw, Tutor, Board Wipe, Protection, Recursion, Win Condition, and custom categories
- Auto-classification assigns cards to categories based on card text, types, and keywords
- Category-name tags override automatic classification
- Progress bars show real-time counts vs. targets
- Default Guideline created automatically on first use

`GET /saved-decks` — Grid view of all saved decks with commander images. Import deck JSON files. Click to open in the Deck Builder. Delete from the grid.

### Similarity

`GET /similar` — Find mechanically similar cards to a given card. Two search modes:

**Semantic mode** (default, requires embedding index):
- Maps oracle text into a 384-dimensional vector space using `all-MiniLM-L6-v2`
- Finds cards with similar functional meaning — catches "destroy target creature" ≈ "exile target creature", reworded mechanics, and conceptual cousins
- Color identity is applied as a subset post-filter (colorless cards are legal everywhere)
- Mana value window of ±3 from the base card
- Optional hard post-filters for types and subtypes
- Returns cosine similarity ranked 0–100

**Legacy mode** (works without embeddings):
- IDF-weighted oracle text overlap is always the base signal
- Optional factor gates: types, keywords, subtypes, supertypes, mana value, color identity
- Each gate uses the formula `1 / (1 + w × strictness × mismatch)` and gates are combined via geometric mean
- **Strictness** controls how picky each factor is — Strict means close match required, Loose means rough similarity is fine
- **MTG Filter** strips ubiquitous terms (creature, target, player, graveyard…) from oracle text matching
- Optional format legality filter restricts results to cards legal in a chosen format
- Results normalized to 0–100

Both modes support drag-and-drop card images or name search to select a base card. The top N results are shown in a "best matches" section; remaining results are paginated. Results can be exported as a self-contained HTML file.

### Commander Eval

`GET /commander-eval` → select a commander → `/card/<set>/<number>/eval`

AI-powered commander analysis pipeline. Requires an LLM configured (see Configuration). The analysis pipeline runs six steps:

1. **Load card data** — oracle text, rulings, EDHREC rank, leadership skills, game changer status
2. **Web research** — queries SearXNG for deck guides, strategy discussions, community reviews, unique archetypes, and mechanic rules
3. **Retrieve similar cards** — semantic search finds mechanically similar cards that fit the commander's color identity, giving the LLM real card names to reference
4. **LLM analysis** — generates a structured JSON report (strengths, weaknesses, strategies, priorities, unique builds, bracket ratings, kill-on-sight score)
5. **Second-pass verification** — a separate LLM call checks the analysis against the card's actual oracle text for invented abilities, wrong zone references, hallucinated card names, and color identity errors
6. **Render** — the analysis is displayed on the page with interactive tabs

**Analysis content:**

- **Strengths & Weaknesses** — 3–5 each, citing concrete mechanics
- **Strategies** — 1–4 distinct build approaches with game plans
- **Priorities** — ranked deck-building categories with reasoning
- **Unique Builds** — off-meta angles discovered from web research
- **Bracket Ratings** — effectiveness and kill-on-sight score for each bracket (1–5)
- **Kill-on-Sight** — default reputation at a typical LGS table (brackets 2–3)

**Deep-dives:** Click any strategy or unique build to get a detailed analysis of that specific approach — key enablers, payoffs, win conditions, and 3 example cards from the similar cards list. Deep-dives auto-save so they survive navigation away.

**Save & Export:** Save reports to `eval_reports/` for later reference. Export as a self-contained HTML file.

Analysis is cached server-side (capped at 100 entries). Use the "Clear" button on the page to return to the landing page and search for a new commander. Drag-and-drop card images on the landing page to identify a card.

### Card Templates

`GET /card-templates` — Manage reusable card packages. Two template types:

**Card templates** (`type: "cards"`) — Pre-built lists of cards with quantities. Load into the Deck Builder as a starting point or sideboard package.

**Guideline templates** (`type: "guideline"`) — Define category targets that measure how a deck stacks up against deck-building standards. Each category (Lands, Ramp, Removal, Card Draw, etc.) has a target count. The Deck Builder's Templates tab shows progress bars comparing your actual counts against guideline targets.

Default Guideline is auto-created on first use. Templates are saved as JSON in `card_templates/`.

### Configuration

`GET /config` — Central settings page with six sections:

**LLM Connection** — Configure the LLM backend for Commander Eval:
- **Backend**: OpenAI-compatible or Anthropic
- **Base URL**: API endpoint (auto-populated for OpenAI, not used for Anthropic)
- **Model**: selected from the dropdown after testing the connection
- **API Key**: stored in your browser session cookie only — never logged or persisted server-side

**Database** — Manage the card database:
- Shows current database status (card count, last ingest timestamp)
- Drag-and-drop upload for new databases (`.sqlite`, `.gz`, `.bz2`, `.xz`, `.zip`)
- Ingest pipeline: validate → deduplicate → replace → rebuild embeddings
- Embedding index status with one-click "Build Now" (builds asynchronously, polls for completion)

**MCP Server** — Manage the MCP services:
- Status display showing whether both MCP SSE and MCPO proxy are alive
- Service table with ports and connection URLs
- "Restart MCP Servers" button
- Port configuration via env vars (`MCP_SSE_PORT`, `MCPO_PORT`, `MCP_HOST`)

**Search Logic** — Default similarity methods:
- Similarity Tool: semantic or legacy (controls the standalone Similarity page)
- Commander Eval: semantic or legacy (controls the "Find Similar" button on eval pages)

**Custom Prompts** — Full text editing for all three LLM prompts:
- **Commander Eval** — system prompt for the main analysis (default: 100+ line structured prompt covering bracket system, card economy language, trigger analysis, etc.)
- **Deep Dive** — system prompt for strategy/build deep-dives
- **Verification** — system prompt for second-pass factual verification against oracle text

Each prompt has a "Reset to Default" button. Prompts are stored in your session cookie.

## MCP Server

The MCP server exposes three tools to LLM agents:

### `semantic_search`
Find cards by mechanical description using embedding vectors. Describes what the card **does**, not community slang:
```
✓ "sacrifice a creature to drain opponents"  ✗ "Aristocrats"
✓ "equip and aura synergy, combat damage"   ✗ "Voltron"
✓ "whenever a land enters the battlefield"  ✗ "Landfall"
```

Parameters: `query`, `color_identity` (e.g. `"GW"`), `mana_value_min`/`max`, `limit`

### `keyword_search`
Literal text search against name, type line, and oracle text. Fallback when semantic search misses.

Parameters: `query`, `color_identity`, `card_type`, `mana_value_min`/`max`, `limit`

### `get_card`
Get full card details by exact English name — oracle text, rulings, legalities, stats.

### Usage

**Claude Code**: Add to `.claude/mcp.json`:
```json
{
  "mcpServers": {
    "mtg-search": {
      "type": "sse",
      "url": "http://localhost:8765/sse"
    }
  }
}
```

**OpenWebUI**: Connect to `http://<host>:8000/openapi.json` (the MCPO proxy endpoint).

The MCP server works with or without the embedding index. `semantic_search` returns empty results if the index hasn't been built — build it from the Configuration page.

## Project Structure

```
app.py              # Flask app entry point — config, hooks, blueprint registration (~100 lines)
docker-compose.yml  # Container orchestration (app + SearXNG)
docker-entrypoint.sh # Container startup (Flask + MCP SSE + MCPO)
Dockerfile          # Docker image (Python 3.12-slim)
requirements.txt    # Python dependencies

mtg/                # Application package
├── blueprints/     # Flask blueprints — one per feature area
│   ├── search.py          #  /, /search, /card/<set>/<n>, /img/..., /card-autocomplete, /llm-models
│   ├── similar.py         #  /similar, /cards/<set>/similar, /card/<set>/<n>/similar
│   ├── config.py          #  /config/* (LLM, embed, MCP, ingest)
│   ├── eval.py            #  /card/<set>/<n>/eval/* (8 routes: analyze, deepdive, similar, save, load, restore, progress)
│   ├── eval_landing.py    #  /commander-eval, /commander-eval/reports/delete
│   ├── decks.py           #  /deck-builder, /saved-decks, /api/deck/*
│   ├── templates_bp.py    #  /card-templates, /api/template/*
│   └── tags.py            #  /api/tags/*
├── shared.py        #  db_path, get_db, constants, Jinja helpers
├── eval_cache.py    #  Server-side in-memory cache for eval analysis
├── similarity.py    #  TF-IDF + factor-gate scoring engine
├── embed.py         #  Embedding index build + search (sentence-transformers)
├── dedup.py         #  Database deduplication (one row per unique card)
├── llm.py           #  LLM client — single generate() function (OpenAI + Anthropic)
├── websearch.py     #  SearXNG web search client
├── prompts.py       #  LLM system prompts (eval, deepdive, verify)
├── eval_helpers.py  #  Eval similarity backends, deepdive persistence
├── images.py        #  Scryfall image fetching + mana symbol rendering
├── ingest.py        #  Database file upload + ingest pipeline
├── mcp_server.py    #  MCP server — semantic_search, keyword_search, get_card
└── mcp_control.py   #  MCP process management (status, restart)

templates/          # Jinja2 templates
├── base.html       # Base layout with navigation, theme, search bar
├── search.html     # Card search page
├── card.html       # Full card detail page
├── similar_landing.html  # Similarity landing page
├── similar.html    # Similarity results page
├── commander_eval_landing.html  # Commander Eval landing
├── commander_eval.html  # Commander Eval analysis view
├── config.html     # Configuration page (LLM, DB, MCP, prompts)
├── deck_builder.html     # Deck Builder page
├── saved_decks.html      # Saved Decks grid
├── card_templates.html   # Card Templates page
└── similar_export.html   # Self-contained HTML export

static/
├── style.css       # Stylesheet (~1,400 lines, dark theme)
└── deck-builder.js # Deck builder interactive app (~3,400 lines)

searxng/
├── settings.yml    # SearXNG configuration
└── limiter.toml    # SearXNG rate limiter (disabled)
```

## Data Sources

- Card data: [MTGJSON](https://mtgjson.com) — AllPrintings.sqlite
- Card images: [Scryfall](https://scryfall.com) — lazy-cached on first view
- Web research: self-hosted [SearXNG](https://searxng.github.io/searxng/) instance
- Embedding model: `all-MiniLM-L6-v2` from [Sentence Transformers](https://www.sbert.net/)

## First-Time Setup Checklist

1. **Get a database**: Download `AllPrintings.sqlite` from [mtgjson.com](https://mtgjson.com/downloads/all-files/) and place it in the project root.
2. **Configure environment**: Copy `.env.example` to `.env`, generate a `SEARXNG_SECRET`, and configure any LLM credentials you need.
3. **Start the stack**: `docker compose up -d` starts Flask, SearXNG, MCP SSE, and MCPO.
4. **Build embeddings**: Go to Configuration → Database → "Build Now" (or upload the database via the ingest UI, which triggers a rebuild automatically). This takes ~2 minutes for ~35,000 cards on a modern CPU.
5. **Configure LLM**: Go to Configuration → LLM Connection. Enter your API key, test the connection, and select a model. Claude Opus or Claude Sonnet are recommended; any OpenAI-compatible model works.

## License

MIT
