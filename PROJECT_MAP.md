# PROJECT_MAP.md — MTG Search

High-density index for coding agents. One read covers the architecture, then
drill into specific files as needed.

---

## Shape

- **Type:** Self-hosted web application (Flask monolith, ~3400 loc) + MCP server
- **Language:** Python 3.9+, Jinja2 templates, vanilla JS + CSS (no framework, no build step)
- **Storage:** SQLite (`AllPrintings.sqlite` from MTGJSON), with on-disk indexes (`embeddings/`, `images/`)
- **Deployment:** Docker Compose (Flask + SearXNG + MCP SSE + MCPO proxy in one container)
- **Test surface:** None (no automated test suite; manual test script at `test_scripts/run_eval_test.py`)

---

## File Map

### Python (application logic)

| File | Role | Lines | Key exports / symbols |
|------|------|-------|-----------------------|
| `app.py` | Flask application — routes, search, similarity engine, commander eval pipeline, config, ingest, deck builder API, image proxy, MCP restart endpoint | ~3400 | `app` (Flask instance), `DATABASE`, `COMMANDER_SYSTEM_PROMPT`, `DEEPDIVE_SYSTEM_PROMPT`, `VERIFY_SYSTEM_PROMPT` |
| `embed.py` | Embedding index: build + search via `all-MiniLM-L6-v2` (384-dim sentence-transformers). Writes `embeddings/vectors.npy` + `embeddings/meta.json` | ~187 | `build(db_path)`, `find(db_path, oracle_text, top_k=200)`, `status()` |
| `llm.py` | Single `generate()` function with OpenAI + Anthropic backends. JSON repair pipeline for LLM output | ~126 | `generate(system_prompt, user_prompt, backend, api_key, base_url, model)` |
| `dedup.py` | Database ingestion pipeline: deduplicates MTGJSON SQLite to one row per unique English card (latest printing) | ~171 | `dedup_db(src, dst)`, `dedup_in_place(db_path)` |
| `mcp_server.py` | Standalone MCP server (FastMCP) exposing 3 tools: `semantic_search`, `keyword_search`, `get_card`. Runs as separate process via stdio/SSE | ~403 | `mcp` (FastMCP instance) |

### Templates (Jinja2)

| Template | Lines | Purpose |
|----------|-------|---------|
| `base.html` | 156 | Layout shell: nav bar, card detail side-panel (`#panel`), image lightbox. Every page extends this |
| `index.html` | 170 | Homepage: search form + card-count stats |
| `search.html` | 306 | Search results grid (30/page) + all 30+ filter controls |
| `card.html` | 100 | Full card detail page (image, rulings, legalities, printings, other faces) |
| `card_panel.html` | 98 | Same as card detail but as a side-panel fragment (loaded via AJAX, `?fragment=1`) |
| `similar.html` | 270 | Similarity results: top-N best matches + factor tuning panel + paginated remaining results |
| `similar_landing.html` | 308 | Similarity landing: name autocomplete + drag-and-drop card image |
| `similar_export.html` | 139 | Self-contained HTML export of similarity results |
| `commander_eval.html` | 1069 | Commander analysis display: tabs for strengths/weaknesses, strategies, brackets, deep-dives. Heaviest template |
| `commander_eval_landing.html` | 238 | Commander eval landing: name search + drag-and-drop + saved reports list |
| `config.html` | 590 | Configuration page: LLM connection, database ingest, MCP status, search logic, custom prompts |
| `deck_builder.html` | 341 | Split-panel deck builder: search on left, deck on right |
| `saved_decks.html` | 105 | Grid view of saved decks |

### Static assets

| File | Lines | Role |
|------|-------|------|
| `style.css` | 2746 | Dark theme (GitHub-style), mana symbol classes, card grid, rarity colors, responsive panels. All visual styling |
| `deck-builder.js` | 2457 | Vanilla JS deck builder: localStorage persistence, drag-and-drop, card sections by type, commander eval iframe integration, import/export |

### Infrastructure

| File | Role |
|------|------|
| `Dockerfile` | Python 3.12-slim, pip install, copy code, 3 VOLUMEs, EXPOSE 5000/8765/8000 |
| `docker-compose.yml` | Two services: `app` (bind-mounts all .py + templates/static for live dev) + `searxng` (port 8888→8080, config from `searxng/`) |
| `docker-entrypoint.sh` | Starts Flask + MCP SSE + MCPO proxy in one container; respawns MCP/MCPO if they die; kills all if Flask dies |
| `requirements.txt` | Flask + sentence-transformers + openai + anthropic + numpy + mcp + mcpo |
| `.env.example` | Required: `SEARXNG_SECRET`. Optional: ports, LLM backend/URL/model/key |
| `searxng/settings.yml` | SearXNG: JSON enabled, rate limiter off, image proxy off |
| `searxng/limiter.toml` | Disabled |

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│ Docker container (mtg-search)                                │
│                                                              │
│  docker-entrypoint.sh                                        │
│  ├── Flask app.py         :5000   (web UI + API)             │
│  ├── MCP SSE server       :8765   (LLM agent tools)          │
│  └── MCPO proxy           :8000   (OpenAPI bridge)           │
│                                                              │
│  Depends on: SearXNG      :8080   (container-internal)       │
└─────────────────────────────────────────────────────────────┘
```

### Data flow for the three main features:

**Search:**
```
Browser → GET /search?q=...&color=... → Flask → SQLite (LIKE queries, 30 filters)
       → render_template("search.html", results, pagination)
```

**Similarity (semantic):**
```
Browser → GET /card/{set}/{num}/similar
       → Flask → embed.find(db_path, text, top_k=200)
       → Post-filter: dedupe by name, color identity gate, MV ±3 window
       → Sort by cosine similarity, split top-N from remaining
       → render_template("similar.html", ...)
```

**Similarity (legacy):**
```
Browser → GET /card/{set}/{num}/similar?method=legacy
       → Flask → SQLite (fetch candidates: same name exclusion, MV ±3, latest printing)
       → For each candidate: _score_similarity(base, candidate, factors, idf)
         - Oracle text: IDF-weighted term overlap, then exponentiated by s_oracle strictness
         - Optional gates (types, keywords, subtypes, supertypes, MV, CI): Jaccard similarity
         - Gates combined via geometric mean, multiplied against oracle score
       → Sort, split top-N, paginate remaining
```

**Commander Eval (6-step pipeline):**
```
Browser → POST /card/{set}/{num}/eval/analyze (AJAX, poll /eval/progress)
       → Step 1: Load card from DB (oracle text, rulings, DFC back face)
       → Step 2: Web research via SearXNG (5 search queries × 5 results each)
       → Step 3: embed.find() for mechanically similar cards (200 results, CI-filtered)
       → Step 4: LLM call (commander_prompt + user_prompt with card_data + web_research + similar_cards)
       → Step 5: Second-pass LLM verification (verify_prompt, checks for invented abilities, wrong zones, hallucinated names)
       → Step 6: Store in server-side cache (_eval_cache, FIFO capped at 100)
       → Browser: render_template("commander_eval.html", analysis=cached)
```

---

## Database Schema (MTGJSON `AllPrintings.sqlite`)

Key tables and columns used throughout the codebase:

### `cards` (primary table)
Used everywhere. After dedup, one row per unique English card (latest printing).

| Column | Type | Usage |
|--------|------|-------|
| `uuid` | TEXT | Primary join key |
| `name` | TEXT | Display, search, DFC ` // ` suffix for double-faced cards |
| `text` | TEXT | Oracle text — similarity scoring, embeddings, LLM prompts |
| `type` | TEXT | Full type line (includes em-dash `—` from MTGJSON) |
| `types` | TEXT | Comma-separated types (e.g. "Creature,Artifact") |
| `subtypes` | TEXT | Comma-separated subtypes |
| `supertypes` | TEXT | Comma-separated supertypes (e.g. "Legendary") |
| `manaCost` | TEXT | e.g. `{1}{R}{G}` |
| `manaValue` | REAL | Numeric mana value, nullable |
| `colors` | TEXT | e.g. "W, U" (WUBRG order) |
| `colorIdentity` | TEXT | e.g. "W, G" (alphabetical order — **important**: differs from `colors` ordering) |
| `keywords` | TEXT | Comma-separated keyword abilities |
| `power` / `toughness` / `loyalty` | TEXT | May be `'*'` for variable values; CAST AS REAL for comparisons |
| `setCode` | TEXT | Three-letter set code |
| `number` | TEXT | Collector number (with letter suffixes) |
| `side` | TEXT | `'a'` / `'b'` for DFCs, NULL for single-faced |
| `otherFaceIds` | TEXT | Comma-separated UUIDs of other faces |
| `language` | TEXT | Always filtered to `'English'` |
| `rarity`, `borderColor`, `layout`, `frameVersion` | TEXT | Search filters |
| `isReprint`, `isReserved`, `isFunny`, `isOversized`, `isFullArt`, `isTextless`, `isPromo`, `isRebalanced` | INT | Boolean search filters |
| `leadershipSkills` | TEXT | JSON string; `"commander": true` for commander-legal |
| `edhrecRank` / `edhrecSaltiness` | INT/FLOAT | EDHREC metadata |
| `faceName` | TEXT | Name of this face for DFCs |
| `printings` | TEXT | Comma-separated set codes of all printings |
| `isGameChanger` | INT | Game Changer card flag |

### Other tables

| Table | Key columns | Used by |
|-------|-------------|---------|
| `cardIdentifiers` | `uuid`, `scryfallId` | Image URLs, MCP tools, autocomplete |
| `cardLegalities` | `uuid`, `commander`, `standard`, ... | Format legality filters, commander-legal check |
| `cardRulings` | `uuid`, `date`, `text` | Card detail, commander eval prompts |
| `sets` | `code`, `name`, `releaseDate` | Latest-printing resolution, display |

---

## Route Map

### Pages (GET, return HTML)

| Route | Template | Notes |
|-------|----------|-------|
| `/` | `index.html` | Homepage |
| `/search` | `search.html` | Card search with 30+ filters |
| `/card/<set_code>/<number>` | `card.html` or `card_panel.html` | `?fragment=1` loads side-panel version |
| `/similar` + `/cards/<set_code>/similar` | `similar_landing.html` | Similarity landing; POST handles drag-drop |
| `/card/<set_code>/<number>/similar` | `similar.html` | Similarity results; `?export=1` returns self-contained HTML |
| `/commander-eval` | `commander_eval_landing.html` | Landing page; POST handles drag-drop |
| `/card/<set_code>/<number>/eval` | `commander_eval.html` | Analysis display; `?report=<filename>` loads saved |
| `/config` | `config.html` | Settings page (GET renders, POST saves to session) |
| `/deck-builder` | `deck_builder.html` | Split-panel deck builder |
| `/saved-decks` | `saved_decks.html` | Saved deck grid |

### JSON API Endpoints

| Route | Method | Purpose |
|-------|--------|---------|
| `/card-autocomplete` | GET | Card name autocomplete (`?q=...`) |
| `/card/<set>/<num>/eval/analyze` | POST | Run commander eval pipeline |
| `/card/<set>/<num>/eval/progress` | GET | Poll pipeline progress |
| `/card/<set>/<num>/eval/deepdive` | POST | Deep-dive on strategy/unique build |
| `/card/<set>/<num>/eval/similar` | POST | Find similar legendary creatures |
| `/card/<set>/<num>/eval/save` | POST | Save analysis to `eval_reports/` |
| `/card/<set>/<num>/eval/load` | POST | Load saved report |
| `/card/<set>/<num>/eval/restore` | POST | Restore evalData from deck builder into cache |
| `/commander-eval/reports` | GET | List saved reports (JSON) |
| `/commander-eval/reports/delete` | POST | Delete saved report |
| `/config/embed-status` | GET | Embedding index status (JSON) |
| `/config/embed-build` | POST | Trigger async embedding rebuild |
| `/config/mcp-status` | GET | MCP server status (JSON) |
| `/config/mcp-restart` | POST | Restart MCP SSE + MCPO |
| `/config/ingest` | POST | Upload + ingest new database |
| `/llm-models` | GET | Proxy to fetch models from API base URL |
| `/api/deck/save` | POST | Save deck to `decks/` |
| `/api/deck/list` | GET | List saved decks |
| `/api/deck/delete` | POST | Delete deck |
| `/api/deck/card-lookup` | POST | Card by setCode+number or uuid |
| `/api/deck/lookup-by-name` | POST | Card by exact name |
| `/api/deck/lookup-by-uuid` | POST | Card by Scryfall UUID |
| `/api/tags/list` | GET | Tag catalog (auto-seeds predefined tags) |
| `/api/tags/save` | POST | Save tag catalog |

### Special

| Route | Purpose |
|-------|---------|
| `/img/<size>/<face>/<c1>/<c2>/<scryfall_id>.jpg` | Lazy image cache-through proxy (fetches from Scryfall, caches locally, retries 3× with exponential backoff) |

---

## Key Idioms & Patterns

### `ponytail:` comments
Every block comment beginning with `ponytail:` is a design note / rationale. These are throughout the codebase and explain **why** a non-obvious choice was made. Agents should read these when modifying code near them — they encode edge cases and trade-offs.

### Session-based config (no database for settings)
LLM keys, model selection, custom prompts, and similarity method preferences are stored in Flask's cryptographically-signed session cookie. The cookie is permanent (30-day lifetime). The `.secret_key` file (auto-generated on first run) ensures sessions survive restarts. The API key **never** touches server-side storage.

### Server-side analysis cache (`_eval_cache`)
Commander eval results are too large for Flask's 4KB cookie limit. Stored in an `OrderedDict` (FIFO capped at 100). A UUID key is stored in the session. Cache entries are validated against the card being viewed to prevent cross-card cache bleed.

### DFC (Double-Faced Card) handling
- DB stores front face as name, back face has `side='b'` and `otherFaceIds` pointing to each other
- Names like `"Sol Ring // Sol Ring"` are split on ` // ` for dedup purposes
- Back face oracle text is included in LLM prompts
- Web searches use only the front-face name (before ` // `)

### Color ordering quirk
- `cards.colors`: WUBRG order (W, U, B, R, G)
- `cards.colorIdentity`: alphabetical order (B, G, R, U, W)

This matters for exact-match comparisons. `_wubrg_sort()` handles colors; CI is handled with `sorted()`.

### IDF cache
`_get_idf()` computes term frequencies once per server lifetime. Invalidates if card count drifts by >100. The extraction logic version (`_idf_version`) forces rebuild when `_extract_terms` changes.

### Embedding index (`embeddings/`)
- `vectors.npy`: 384-dim normalized embeddings for all unique English cards
- `meta.json`: metadata array (uuid, name, type, keywords, CI, MV, scryfallId) — no oracle text
- `build_info.json`: status info (card count, build timestamp)
- `.build.lock`: temporary file indicating build-in-progress
- Oracle text for results is fetched from DB on the fly (keeps meta.json small)

### Ingest pipeline
1. Accept upload (`.sqlite`, `.gz`, `.bz2`, `.xz`, `.zip`, `.tgz`)
2. Decompress if needed; handle `tar.gz` archives containing `.sqlite`
3. Validate (must have ≥1000 English cards)
4. `dedup.dedup_db()` — one row per unique English card (latest printing)
5. Validate dedup result (≥500 cards)
6. Atomically replace live database
7. Trigger background embedding rebuild

### Image proxy
`/img/normal/front/0/1/abcd1234.jpg` → checks local disk → fetches from Scryfall → caches → serves. Three retries with exponential backoff (Scryfall drops burst requests from 30-image search grids).

### MCP infrastructure
- `mcp_server.py`: standalone process using FastMCP, supports `--transport stdio|sse|streamable-http`
- MCP SSE runs on `:8765`, MCPO proxy on `:8000` (OpenAPI bridge for OpenWebUI)
- Flask manages MCP lifecycle via `_restart_mcp()` (kill by PID file, respawn)
- CORS is wide-open on the MCP server (needed for browser-based OpenWebUI clients)

---

## Configuration Surface

### Environment variables (Flask app)

| Variable | Default | Used in |
|----------|---------|---------|
| `SEARXNG_URL` | `http://localhost:8888` | `app.py::_web_search()` |
| `LLM_BACKEND` | `openai` | `llm.py::generate()` |
| `LLM_BASE_URL` | (empty) | `llm.py::generate()` |
| `LLM_MODEL` | (empty) | `llm.py::generate()` |
| `LLM_API_KEY` | (empty) | `llm.py::generate()` |
| `SECRET_KEY` | (auto-generated) | Flask session signing |
| `PORT` | `5000` | docker-compose port mapping |
| `FLASK_DEBUG` | (empty) | Debug mode (any truthy) |
| `MCP_SSE_PORT` | `8765` | MCP SSE + MCPO config |
| `MCPO_PORT` | `8000` | MCPO proxy port |
| `MCP_HOST` | `0.0.0.0` | MCP bind address |
| `MTG_DATABASE` | `AllPrintings.sqlite` | Only used by `mcp_server.py` |

### Session-based settings (browser cookie)
Configured from `/config` page:
- `llm_backend`, `llm_base_url`, `llm_model`, `llm_api_key`
- `eval_prompt`, `deepdive_prompt`, `verify_prompt` (custom system prompts)
- `similar_method`, `eval_similar_method` (embed vs legacy)
- Override env vars when set

### Docker Compose env vars (.env file)
- `SEARXNG_SECRET` (required)
- `PORT`, `SEARXNG_HOST_PORT`, `MCP_SSE_PORT`, `MCPO_PORT`
- `LLM_BACKEND`, `LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`
- `FLASK_DEBUG`

---

## Dependencies

| Package | Role |
|---------|------|
| Flask 3.x | Web framework |
| sentence-transformers 4.x | Embedding model (`all-MiniLM-L6-v2`) |
| numpy 2.x | Vector operations |
| openai 2.x | OpenAI-compatible LLM backend |
| anthropic 0.116+ | Anthropic LLM backend |
| mcp 1.28+ | MCP server framework (FastMCP) |
| mcpo 0.0.20+ | MCP-to-OpenAPI proxy |

No ORM, no migrations, no task queue, no Redis — SQLite is the only database.

---

## Hooks & Entry Points

- **Flask dev server:** `python app.py` (binds 0.0.0.0:5000)
- **MCP stdio:** `python mcp_server.py` (for local Claude Code / VS Code)
- **MCP SSE:** `python mcp_server.py --transport sse --port 8765` (for remote agents)
- **Docker:** `docker compose up -d` (all three services via `docker-entrypoint.sh`)
- **Embedding build:** `embed.build(db_path)` — called from ingest pipeline or config page button (async via thread)
- **Database dedup:** `dedup.dedup_db(src, dst)` or `dedup.dedup_in_place(db_path)`

---

## Areas to Watch When Modifying

1. **`app.py` is ~3400 lines.** New routes should follow the existing pattern: use `ponytail:` comments, session for config, `_eval_cache` for large data. Avoid adding new Python files unless the module has a clear standalone concern (like `embed.py`).

2. **DFC names** — many places split on ` // ` for dedup. Adding name-based logic without handling this produces duplicate results for double-faced cards.

3. **Color ordering** — `colors` is WUBRG, `colorIdentity` is alphabetical. Off-by-one in color filtering produces wrong results.

4. **Session cookie size** — Flask sessions cap at ~4KB. Commander eval results are too big; use `_eval_cache` with UUID keys. New features storing large data should follow this pattern.

5. **Embedding index must be rebuilt** after any DB change. The config page shows status and has a "Build Now" button. `embed.build()` is async but thread-safe (mutex lock).

6. **Image proxy burst handling** — Scryfall drops concurrent requests. The exponential backoff (3 retries, 0.5s/1s/2s) is load-bearing. Changing image loading (e.g., lazy loading more images at once) may need a fetch queue instead.

7. **LLM JSON parsing** — `llm._parse_json()` handles markdown fences, trailing commas, missing commas, unescaped newlines, and invalid escape sequences. Changes to LLM prompt output format may need updates here.

8. **Legacy similarity scoring** — the strictness values (`STRICT_VALUES`, `COLOR_STRICT`, `ORACLE_STRICT`) were calibrated against real card data. Changing them changes result quality non-linearly. The round thresholds in `_eval_similar_legacy()` are tuned for commander similarity specifically.

9. **`_idf_version`** — bump this integer when `_extract_terms()` logic changes. Forces IDF cache rebuild so old term frequencies don't poison new scoring.

10. **SearXNG unavailable** is handled gracefully: `_web_search()` returns `None` on connection errors, the pipeline continues with web research as empty dicts, and the LLM still produces analysis from card data + similar cards alone.
