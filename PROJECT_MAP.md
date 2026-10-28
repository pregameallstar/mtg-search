# PROJECT_MAP.md — MTG Search

Tool-first architecture index for coding agents. Each section covers one tool:
every file, every route, every integration point. Read the tool section relevant
to your task, then drill into specific files.

---

## Shape

- **Type:** Self-hosted web app (Flask monolith, ~5,700 loc Python, ~3,500 loc JS, ~3,500 loc CSS)
- **Stack:** Python/Flask, Jinja2 templates, vanilla JS + CSS (no framework, no build step)
- **Storage:** SQLite (`AllPrintings.sqlite` from MTGJSON), JSON files on disk
- **Deployment:** Docker Compose (Flask + SearXNG + MCP SSE + MCPO in one container)
- **Frontend:** One CDN dependency — `mana-font@1.21.0` for mana symbols

---

## Tool Index

| Tool | Page Route | Python | Template | JS | CSS section |
|------|-----------|--------|----------|----|-------------|
| **Search** | `/search` | `mtg/blueprints/search.py` (526 lines) | `templates/search.html` | inline in `base.html` | style.css §search |
| **Similarity** | `/similar`, `/card/{set}/{num}/similar` | `mtg/blueprints/similar.py` (378 lines) | `similar.html`, `similar_landing.html`, `similar_export.html` | inline in `base.html` | style.css §similarity |
| **Commander Eval** | `/commander-eval`, `/card/{set}/{num}/eval` | `mtg/blueprints/eval.py` (860 lines), `eval_landing.py` (125 lines) | `commander_eval.html`, `commander_eval_landing.html` | inline in `commander_eval.html` | style.css §eval |
| **Deck Builder** | `/deck-builder` | `mtg/blueprints/decks.py` (226 lines) | `templates/deck_builder.html` (438 lines) | `static/deck-builder.js` (3,460 lines) | style.css §deck-builder |
| **Saved Decks** | `/saved-decks` | `mtg/blueprints/decks.py` (shared) | `templates/saved_decks.html` (105 lines) | inline in `saved_decks.html` | style.css §saved-decks |
| **Deck Doctor** | `/deck-review?deck=…` | `mtg/blueprints/deck_review.py`, `mtg/deck_review.py` | `templates/deck_review.html` | inline in `deck_review.html` | style.css §Deck Doctor |
| **Compare Decks** | `/compare-decks` | `mtg/blueprints/deck_versions.py`, `mtg/deck_versions.py` | `templates/compare_decks.html` | inline in `compare_decks.html` | style.css §Compare Decks |
| **Card Templates** | `/card-templates` | `mtg/blueprints/templates_bp.py` (87 lines) | `templates/card_templates.html` (1,048 lines) | inline in `card_templates.html` | style.css §templates |
| **Configuration** | `/config` | `mtg/blueprints/config.py` (247 lines) | `templates/config.html` (590 lines) | inline in `config.html` | style.css §config |
| **MCP Server** | (standalone process) | `mtg/mcp_server.py` (404 lines) | — | — | — |

**Every page extends `templates/base.html`** (155 lines), which provides:
- The sidebar nav (hidden when `?embed=1`)
- Card detail side panel (`#card-panel`, `openPanel()`)
- Card image lightbox (`#card-lightbox`)
- Global card drag-and-drop via `MutationObserver` on `.card-cell` elements
- Escape-key handler for all overlays

---

## 1. Search (`/search`)

The core card search engine with 30+ filters.

### Files

| File | Role |
|------|------|
| `mtg/blueprints/search.py` | Route handler — delegates to `card_query` and renders results |
| `mtg/card_query.py` | Canonical query builder — parses search params, builds SQL, runs it |
| `templates/search.html` | Search form + results grid |
| `templates/commanders.html` | Commander Finder page |
| `templates/card.html` | Full card detail page (DFC faces, rulings, legalities, printings) |
| `templates/card_panel.html` | Card detail as a side-panel fragment (`?fragment=1`) |
| `templates/index.html` | Homepage redirects to `/search` |

### Routes

| Route | Purpose |
|-------|---------|
| `GET /` | Redirects to `/search` |
| `GET /search` | Search form + results (30+ query params) |
| `GET /api/search` | JSON search using the canonical query builder |
| `GET /api/search/random` | JSON: one random matching card |
| `GET /commanders` | Commander Finder page |
| `GET /api/commanders` | JSON: commander-eligible cards |
| `GET /card/<set_code>/<number>` | Card detail page (`?fragment=1` → side panel) |
| `GET /card-autocomplete?q=...` | JSON: top 10 name matches |
| `GET /img/<size>/<face>/<c1>/<c2>/<id>.jpg` | Lazy image proxy (cache from Scryfall) |
| `GET /llm-models?base_url=...` | JSON: fetch models from LLM API |

### Key detail: `/api/search`

The Deck Builder's embedded Search tool now fetches `/api/search` JSON. The HTML
route and API share `mtg/card_query.py`, so filters and sorting stay in sync.
`.card-grid` / `.card-cell` markup is still used by the HTML page and drag-and-drop.

### Key detail: color ordering

- `cards.colors`: WUBRG order
- `cards.colorIdentity`: alphabetical order

Both are stored as comma-separated strings like `"W, U"`. Always use `sorted()` or
`wubrg_sort()` from `mtg/similarity.py` when comparing CI.

---

## 2. Similarity (`/similar`, `/card/{set}/{num}/similar`)

Finds mechanically similar cards. Two backends: semantic (embeddings) and legacy (TF-IDF).

### Files

| File | Role |
|------|------|
| `mtg/blueprints/similar.py` | Route handler — dispatches to embedding or legacy scoring |
| `mtg/similarity.py` | TF-IDF scoring engine (286 lines): `extract_terms()`, `score_similarity()`, `gate()`, `get_idf()` |
| `mtg/embed.py` | Embedding index: `build()`, `find()`, `status()` |
| `templates/similar.html` | Results page with top-N + paginated remaining |
| `templates/similar_landing.html` | Landing page: name autocomplete + image drop zone |
| `templates/similar_export.html` | Self-contained HTML export |
| `embeddings/vectors.npy` | 384-dim normalized vectors for all cards |
| `embeddings/meta.json` | Metadata array (no oracle text — fetched from DB on demand) |
| `embeddings/build_info.json` | Build timestamp + card count |

### Routes

| Route | Purpose |
|-------|---------|
| `GET/POST /similar` | Landing page; POST handles drag-drop image lookup |
| `GET/POST /cards/<set>/similar` | Alias for landing with pre-selected set |
| `GET /card/<set>/<num>/similar` | Results page with 14+ tuning params |
| `GET /card/<set>/<num>/similar?export=1` | Download as self-contained HTML |

### How it works

**Semantic (embed):** `embed.find()` → 200 candidates → post-filter by name, CI, MV ±3, type → score as cosine × 100.

**Legacy (TF-IDF):** SQL candidate fetch (same name exclusion, MV ±3, latest printing) → `score_similarity()` per candidate → sort → split top-N.

### Key detail: strictness values

`STRICT_VALUES`, `COLOR_STRICT`, `ORACLE_STRICT` in `similar.py` lines 105-107 are
calibrated against real data. Changing them changes result quality non-linearly.

### Key detail: IDF cache

Module-level singleton in `mtg/similarity.py`. Rebuilt if card count drifts by >100.
Bump `_idf_version` when `extract_terms()` logic changes.

---

## 3. Commander Eval (`/commander-eval`, `/card/{set}/{num}/eval`)

6-step LLM pipeline analyzing legendary creatures as Commanders. The most complex tool.

### Files

| File | Role |
|------|------|
| `mtg/blueprints/eval.py` | All eval routes under `/card/<set>/<num>/eval` |
| `mtg/blueprints/eval_landing.py` | Landing page + report listing/deletion |
| `mtg/eval_cache.py` | Server-side OrderedDict cache (FIFO 100); Flask sessions cap at 4KB |
| `mtg/eval_helpers.py` | Similarity search (embed + legacy), placeholder detection, deepdive auto-save |
| `mtg/websearch.py` | SearXNG query function — returns `None` when unreachable |
| `mtg/prompts.py` | Five LLM system prompts: commander, deepdive, verify, deck review, deck verify |
| `mtg/llm.py` | `generate()` — OpenAI/Anthropic backends with JSON repair |
| `mtg/embed.py` | Embedding index (shared with Similarity) |
| `templates/commander_eval.html` | Analysis display (1,069 lines): tabs for strengths, strategies, brackets, deepdives |
| `templates/commander_eval_landing.html` | Landing: name search + image drop + saved reports list |
| `eval_reports/` | Saved analysis JSON files + per-commander deepdive auto-saves |

### Routes

| Route | Purpose |
|-------|---------|
| `GET/POST /commander-eval` | Landing page; POST handles drag-drop lookup |
| `GET /card/<set>/<num>/eval` | Main analysis page (loads from cache or `?report=`) |
| `POST /card/<set>/<num>/eval/restore` | Deck Builder injects saved evalData into cache |
| `POST /card/<set>/<num>/eval/analyze` | AJAX: run 6-step pipeline |
| `GET /card/<set>/<num>/eval/progress` | AJAX poll: `{step, total, label}` |
| `POST /card/<set>/<num>/eval/deepdive` | AJAX: strategy/unique_build deep-dive |
| `POST /card/<set>/<num>/eval/similar` | AJAX: find similar legendary commanders |
| `POST /card/<set>/<num>/eval/save` | Save report to `eval_reports/` |
| `POST /card/<set>/<num>/eval/load` | Load saved report |
| `POST /commander-eval/reports/delete` | Delete report |

### Pipeline (6 steps)

1. Load card data (oracle text, rulings, DFC back face)
2. SearXNG web research (5 queries × 5 results)
3. `embed.find()` for mechanically similar cards (200, CI-filtered)
4. LLM call with commander prompt + card data + web research + similar cards
5. Second-pass LLM verification (checks for invented abilities, wrong zones, hallucinated names)
6. Store in `_eval_cache` (FIFO 100)

### Cross-tool: Deck Builder integration

- Deck Builder loads `/card/<set>/<num>/eval?embed=1` in an iframe
- `commander_eval.html` posts `{type: 'mtg-eval-data', data}` to `window.parent`
- On deck load, `restoreEvalData()` POSTs to `/eval/restore` to repopulate the cache
- The `embed=1` param removes sidebar chrome (checked in `base.html` line 11)

### Key detail: Cache validation

`_eval_cache` entries include `_card: "set/number"`. On page load, `eval.py` checks
that the cached analysis belongs to the viewed card. Session `eval_key` bleeds
across navigations — this guard prevents cross-card cache bleed.

---

## 4. Deck Builder (`/deck-builder`)

Split-panel drag-and-drop Commander deck builder. The most JS-heavy tool.

### Files

| File | Role |
|------|------|
| `mtg/blueprints/decks.py` | 2 page routes + deck JSON API endpoints |
| `mtg/blueprints/deck_analysis.py` | Role batches + deterministic deck audit API |
| `mtg/blueprints/deck_review.py` | Deck Doctor page, review jobs, saved reports |
| `mtg/blueprints/deck_versions.py` | Deck versions, restore, diff, and compare APIs |
| `mtg/blueprints/tags.py` | Global tag catalog CRUD at `/api/tags` |
| `templates/deck_builder.html` | Page layout: tool panel + right deck panel, role/bracket analysis, modals |
| `templates/deck_review.html` | Deck Doctor report/run page |
| `templates/compare_decks.html` | Compare two saved decks |
| `static/deck-builder.js` | Entire client app (3,460 lines): Deck model, drag-drop, search, charts, autosave |
| `decks/` | Saved deck JSON files (one per deck) |
| `decks/_tags.json` | Global tag catalog (shared across all decks) |
| `decks/_versions/` | Deck snapshots (created on first save, explicit snapshots, pre-restore safety) |

### Routes

| Route | Method | Purpose |
|-------|--------|---------|
| `/deck-builder?deck=<name>` | GET | Deck Builder page |
| `/saved-decks` | GET | Saved decks grid |
| `/api/deck/save` | POST | Save deck JSON to `decks/name.json` |
| `/api/deck/list` | GET | List all saved decks (returns full JSON array) |
| `/api/deck/delete` | POST | Delete deck file |
| `/api/deck/card-lookup` | POST | Card by setCode+number or uuid |
| `/api/deck/lookup-by-name` | POST | Card by exact name (latest printing) |
| `/api/deck/lookup-by-uuid` | POST | Scryfall UUID → setCode+number |
| `/api/cards/roles` | POST | Classify up to 200 cards into roles/evidence |
| `/api/deck/analyze` | POST | Deterministic deck audit: curve, roles, health, bracket |
| `/api/deck/remove-card` | POST | Remove a card from a saved deck |
| `/api/deck/review/run` | POST | Start a Deck Doctor background job |
| `/api/deck/review/progress` | GET | Review job progress |
| `/api/deck/review/result` | GET | Review result |
| `/api/deck/review/reports` | GET | Saved review summaries |
| `/api/deck/review/save` | POST | Persist a review report |
| `/api/deck/review/report` | GET | Load a saved review (with stale flag) |
| `/api/deck/review/export` | GET | Download Markdown/HTML review |
| `/api/deck/review/delete` | POST | Delete a saved review |
| `/api/deck/versions` | GET | List snapshots for a deck |
| `/api/deck/versions/snapshot` | POST | Snapshot a saved deck |
| `/api/deck/version` | GET | Load a snapshot |
| `/api/deck/versions/restore` | POST | Restore a snapshot |
| `/api/deck/versions/delete` | POST | Delete a snapshot |
| `/api/deck/version/diff` | GET | Diff current deck against a snapshot |
| `/api/deck/compare` | POST | Compare two saved decks |
| `/api/tags/list` | GET | Tag catalog (auto-seeds 8 predefined tags) |
| `/api/tags/save` | POST | Save tag catalog |

### Deck data model (client-side)

```js
Deck {
  name: string,           // also the filename key
  commander: card | null, // { uuid, setCode, number, name, manaCost, types, colors, colorIdentity, imageUrl, ... }
  cards: [card],          // main 99; each card has uuid, setCode, number, name, quantity, tags, ...
  evalData: object | null,// Commander Eval analysis blob
  sections: {             // extra zones
    "Sideboard": [card],
    "Maybeboard": [card],
    ...
  },
  guideline: string | null, // guideline template name
  reviewNotes: string      // Deck Doctor brief / Rule 0 notes
}
```

Deck JSON files are stored verbatim — the server never validates card contents,
only requires a non-empty name. Filenames: `re.sub(r'[^a-zA-Z0-9_ -]', '', name).replace(' ', '_') + '.json'`.

### JS architecture (`static/deck-builder.js`)

- IIFE wrapper, ES5-style (`var`, `function`), no framework
- `Deck` constructor + prototype (lines 69–338) — the data model
- `classifyCard()` (lines 19–66) — regex-based Lands/Ramp/Removal/Draw/Core
- Tag popover is **duplicated twice**: `openTagPopover()` (line 495) for main deck, `openTagPopoverForSection()` (line 988) for custom sections (~90 lines each, near-identical)
- Search fetches `/api/search` JSON and renders cards (no HTML scrape)
- Similarity still scrapes `/card/{set}/{num}/similar` HTML, takes `grids[1]`
- Autosave: 600ms debounce + `beforeunload`/`visibilitychange` `sendBeacon`
- Load: **synchronous XHR** to `/api/deck/list`, linear search by name (line 384)
- Two mana curve renderers: `renderManaCurve()` (stats bar) and `renderManaCurveChart()` (charts panel)
- Card hover preview: `initCardHoverPreview()` (line 3269), 250ms delay, 400×560px

### Known dead/stale code in deck-builder.js

- `STORAGE_PREFIX` (line 10) — declared but unused (persistence moved to server)
- `renderTagFilterBar()` — targets `#tag-filter-bar` which doesn't exist in any template
- `showTagUI()` — defined but never called
- `activePopoverCard` — assigned but never meaningfully read

### Cross-tool: search integration

Deck Builder embeds a compact search form (tab 1). It calls `/api/search` and
renders the JSON card objects; it no longer parses the search page HTML.

### Cross-tool: card drag-and-drop

`base.html` lines 121-148 has a global `MutationObserver` that marks every `.card-cell`
draggable with `application/mtg-card` data. `deck-builder.js` has its own
`makeCardsDraggable()` for the embedded search grid. The drop handler
(`handleCardDrop`, line 2160) dispatches to copy/move/commander handlers.

### Cross-tool: card detail panel

`base.html` provides `openPanel(setCode, number)` which fetches `/card/{set}/{num}?fragment=1`.
`deck-builder.js` line 2539 uses a `MutationObserver` on `#card-panel-sheet` to inject
an "Add to Deck" button into the card detail panel.

---

## 5. Card Templates (`/card-templates`)

Reusable card packages and guideline templates (deck-building category targets).

### Files

| File | Role |
|------|------|
| `mtg/blueprints/templates_bp.py` | 1 page route + 3 JSON API endpoints |
| `templates/card_templates.html` | Split-panel editor (card search on left, template list on right) |
| `card_templates/` | Saved template JSON files |

### Routes

| Route | Purpose |
|-------|---------|
| `GET /card-templates` | Template editor page |
| `POST /api/template/save` | Save template to `card_templates/name.json` |
| `GET /api/template/list?type=cards|guideline` | List templates (filter by type) |
| `POST /api/template/delete` | Delete template |

Two template types:
- **Card templates:** `{name, cards: [...]}` — type defaults to `"cards"` when absent
- **Guideline templates:** `{type: "guideline", name, categories: {Lands, Ramp, Draw, Removal, Core}}`

### Cross-tool: Deck Builder integration

- Templates tab loads card templates via `/api/template/list` for bulk import
- Guideline selector loads guidelines via `/api/template/list?type=guideline`
- If zero guidelines exist, `loadGuidelineList()` auto-creates "Default Guideline"

---

## 6. Configuration (`/config`)

LLM settings, database ingest, embedding rebuild, MCP management.

### Files

| File | Role |
|------|------|
| `mtg/blueprints/config.py` | All config routes |
| `mtg/ingest.py` | Database upload → dedup → replace pipeline (240 lines) |
| `mtg/dedup.py` | Dedup engine: one row per unique English card (171 lines) |
| `mtg/mcp_control.py` | MCP process management: `restart_mcp()`, `port_alive()` |
| `templates/config.html` | Config form with 6 panels |

### Routes

| Route | Purpose |
|-------|---------|
| `GET/POST /config` | Config form (POST saves to Flask session) |
| `GET /config/embed-status` | JSON: embedding index status |
| `POST /config/embed-build` | Trigger async embedding rebuild |
| `GET /config/mcp-status` | JSON: MCP SSE + MCPO alive status |
| `POST /config/mcp-restart` | Kill + respawn MCP SSE + MCPO |
| `POST /config/ingest` | Upload + validate + deduplicate + replace DB |

### Config is session-based

LLM keys, models, custom prompts, and similarity method preferences are stored in
Flask's cryptographically-signed session cookie (30-day lifetime). The API key
never touches server-side storage. `.secret_key` file auto-generated on first run.

---

## 7. Saved Decks (`/saved-decks`)

Grid view of all saved decks, delete, JSON file import.

### Files

| File | Role |
|------|------|
| `mtg/blueprints/decks.py` | Shared with Deck Builder (routes above) |
| `templates/saved_decks.html` | Deck grid + inline JS for delete/import |

### Entry point to Deck Builder

Clicks link to `/deck-builder?deck=<urlencoded name>`. Deck Builder reads the
`?deck=` param at init (line 3436) and loads via `loadDeckFromServer()`.

---

## Cross-Tool Integration Points

### Card drag-and-drop (global)
- **Source:** `base.html` inline script — `MutationObserver` marks all `.card-cell` draggable
- **Data:** `application/mtg-card` = `{setCode, number}`, `text/uri-list` = card URL
- **Consumer:** `deck-builder.js` `handleCardDrop()`, which also handles Scryfall UUIDs from external drops

### Deck Builder ↔ Search
- Deck Builder tab 1 calls `/api/search` and renders JSON cards
- Card grid HTML structure must be stable — changes to `search.html` break the builder

### Deck Builder ↔ Similarity
- Deck Builder tab 2 fetches `/card/{set}/{num}/similar` HTML, takes `grids[1]` ("All Results")
- Paginates from the API's `page` / `total_pages` fields

### Deck Builder ↔ Commander Eval
- Deck Builder tab 3 loads `/card/{set}/{num}/eval?embed=1` in an iframe
- Eval page posts `mtg-eval-data` via `window.parent.postMessage()`
- On deck load, `restoreEvalData()` POSTs to `/eval/restore` to warm the cache
- `enableCardPanelAddToDeck()` injects "Add to Deck" into the card detail panel via MutationObserver

### Deck Builder ↔ Templates
- Templates tab loads via `/api/template/list` and `/api/template/list?type=guideline`
- Guideline auto-creation on first load if none exist

### Card detail panel (global)
- `base.html` provides `openPanel(setCode, number)` → fetches `/card/{set}/{num}?fragment=1`
- `deck-builder.js` injects "Add to Deck" button via MutationObserver
- Panel is shared across ALL tools — any `.card-cell` click opens it

---

## Supporting Modules

These are not tied to a single tool but used by several.

| File | Lines | Role | Used by |
|------|-------|------|---------|
| `mtg/shared.py` | 127 | `get_db()`, `db_path()`, `color_identity_subset()`, `card_image`, `render_mana()`, `pagination_url()`, `DECKS_DIR`/`TEMPLATES_DIR` constants | All blueprints, MCP server |
| `mtg/card_query.py` | — | `build_search_query()`, `run_search()`, `search_cards()`, `empty_search_result()` | Search route, future JSON API/MCP |
| `mtg/roles.py` | — | `classify_roles()` — multi-role taxonomy, evidence, primary bucket | Deck Builder, deck analysis, bracket |
| `mtg/bracket.py` / `mtg/bracket_rules.py` | — | `estimate_bracket()` — advisory signals, salt, target fit | Deck Builder, deck analysis |
| `mtg/deck_rules.py` | — | `validate_deck()` — count, singleton, color identity, legality | Deck Builder, deck analysis |
| `mtg/deck_stats.py` | — | `analyze_deck()`, `deck_fingerprint()` — curve, roles, gaps, health | Deck Builder, Deck Doctor |
| `mtg/deck_enrich.py` | — | `lookup_card()`, `enrich_deck()` — saved deck → DB-backed cards | Deck analysis, Deck Doctor |
| `mtg/deck_review.py` | — | `candidate_adds()`, `cut_candidates()`, `validate_analysis()`, `run_review_job()` | Deck Doctor |
| `mtg/review_cache.py` | — | bounded job progress/result cache | Deck Doctor |
| `mtg/deck_versions.py` | — | snapshot listing/writing, restore payloads, `compare_decks()` | Deck Builder History, Compare Decks |
| `mtg/similarity.py` | 286 | `score_similarity()`, `extract_terms()`, `get_idf()`, `gate()`, `split_csv()`, `similarity_label()`, `wubrg_sort()` | Similarity tool, Eval deepdive/similar, MCP server |
| `mtg/embed.py` | 232 | `build(db_path)`, `find(db_path, text, top_k=200)`, `status()` | Similarity tool, Eval pipeline, Config page |
| `mtg/llm.py` | 163 | `generate()`, `_parse_json()` (JSON repair), `fetch_models()` | Commander Eval, Config page |
| `mtg/images.py` | 81 | `card_image_url()`, `fetch_scryfall_image()`, `mana_symbols()` | Search routes, shared helpers |
| `mtg/prompts.py` | 142 | Five system prompt constants | Commander Eval, Deck Doctor |
| `mtg/websearch.py` | 42 | `web_search(query, max_results)` → SearXNG | Commander Eval pipeline |
| `mtg/eval_cache.py` | 42 | `_eval_cache` (OrderedDict, FIFO 100), `cache_put/get/update` | Commander Eval routes |
| `mtg/eval_helpers.py` | 237 | `eval_similar_embed/legacy`, `get_commander_filter_sets()`, `is_placeholder_name()`, `auto_save_deepdives()` | Commander Eval routes |
| `mtg/ingest.py` | 240 | `process_upload()` — validate, dedup, replace DB | Config page |
| `mtg/dedup.py` | 171 | `dedup_db()`, `dedup_in_place()` | Config page (ingest) |
| `mtg/mcp_server.py` | 404 | FastMCP server: `semantic_search`, `keyword_search`, `search_cards`, `get_card`, `review_deck` | Standalone process (managed by `mcp_control.py`) |
| `mtg/mcp_control.py` | 61 | `restart_mcp()`, `port_alive()` | Config page, docker-entrypoint |
| `app.py` | 116 | Flask app factory: secret key, security headers, Jinja globals, blueprint registration | All web routes |

---

## Database Schema

Key tables in `AllPrintings.sqlite` (MTGJSON). After dedup, one row per unique English card (latest printing).

### `cards` (primary table)

| Column | Type | Notes |
|--------|------|-------|
| `uuid` | TEXT | Primary key |
| `name` | TEXT | DFCs have ` // ` separating faces |
| `text` | TEXT | Oracle text |
| `type` | TEXT | Full type line with em-dash `—` |
| `types` | TEXT | Comma-separated (e.g. "Creature,Artifact") |
| `subtypes` | TEXT | Comma-separated |
| `supertypes` | TEXT | Comma-separated (e.g. "Legendary") |
| `manaCost` | TEXT | e.g. `{1}{R}{G}` |
| `manaValue` | REAL | Nullable |
| `colors` | TEXT | WUBRG order (e.g. "W, U") |
| `colorIdentity` | TEXT | Alphabetical order (e.g. "U, W") — **different from colors** |
| `keywords` | TEXT | Comma-separated |
| `power`/`toughness`/`loyalty` | TEXT | May be `'*'` — CAST AS REAL for comparisons |
| `setCode` | TEXT | Three-letter set code |
| `number` | TEXT | Collector number (may have letter suffixes) |
| `side` | TEXT | `'a'`/`'b'` for DFCs, NULL for single-faced |
| `otherFaceIds` | TEXT | Comma-separated UUIDs of other faces |
| `language` | TEXT | Always filtered to `'English'` |
| `printings` | TEXT | Comma-separated set codes |
| `rarity`/`borderColor`/`layout`/`frameVersion` | TEXT | Search filters |
| `isReprint`/`isReserved`/`isFunny`/`isOversized`/`isFullArt`/`isTextless`/`isPromo`/`isRebalanced` | INT | Boolean flags |
| `isGameChanger` | INT | Game Changer card flag |
| `leadershipSkills` | TEXT | JSON string; `"commander": true` |
| `edhrecRank`/`edhrecSaltiness` | INT/FLOAT | EDHREC metadata |

### Other tables

| Table | Key columns | Used by |
|-------|-------------|---------|
| `cardIdentifiers` | `uuid`, `scryfallId` | Image URLs, autocomplete, MCP |
| `cardLegalities` | `uuid`, `commander`, `standard`, ... | Format legality filters |
| `cardRulings` | `uuid`, `date`, `text` | Card detail, eval prompts |
| `sets` | `code`, `name`, `releaseDate` | Latest-printing resolution, display |

---

## Infrastructure

```
docker-compose.yml
├── app (Flask :5000, MCP SSE :8765, MCPO :8000)
│   └── docker-entrypoint.sh — starts all 3, respawns MCP/MCPO, kills all if Flask dies
└── searxng (:8888→8080)
    ├── searxng/settings.yml — JSON enabled, rate limiter off
    └── searxng/limiter.toml — disabled
```

**Bind mounts for live dev:** All `.py` files, `templates/`, `static/`, `decks/`, `card_templates/` are bind-mounted — edits take effect on browser refresh, no rebuild.

### Environment variables

| Variable | Default | Used by |
|----------|---------|---------|
| `SEARXNG_URL` | `http://localhost:8888` | `websearch.py` |
| `LLM_BACKEND` | `openai` | `llm.py` |
| `LLM_BASE_URL` | (empty) | `llm.py` |
| `LLM_MODEL` | (empty) | `llm.py` |
| `LLM_API_KEY` | (empty) | `llm.py` |
| `SECRET_KEY` | auto-generated | Flask session signing |
| `PORT` | `5000` | docker-compose |
| `FLASK_DEBUG` | (empty) | Debug mode |
| `MCP_SSE_PORT` | `8765` | MCP server |
| `MCPO_PORT` | `8000` | MCPO proxy |
| `MTG_DATABASE` | `AllPrintings.sqlite` | MCP server only |

---

## Patterns & Gotchas When Modifying

1. **DFC names** — many places split on ` // ` for dedup. New name-based logic must handle this.

2. **Color ordering quirk** — `colors` is WUBRG, `colorIdentity` is alphabetical. When comparing CI across cards, always `sorted()` or use `color_identity_subset()` from `shared.py`.

3. **Session cookie size** — capped at ~4KB. Eval results are too large → stored in `_eval_cache` (in-memory OrderedDict) with UUID keys in the session.

4. **Deck Builder HTML coupling** — the embedded Similarity tab still parses the similarity HTML page. Search now uses `/api/search`; changes to `search.html`'s grid no longer break search.

5. **Embedding index** — must be rebuilt after DB ingest. `embed.build()` is async (threaded) and thread-safe (mutex). `status()` shows building state via lock file.

6. **Image proxy burst handling** — 30-image grids burst 30 concurrent requests to Scryfall, which drops them. Exponential backoff (3 retries, 0.5s/1s/2s) is load-bearing in `fetch_scryfall_image()`.

7. **LLM JSON repair** — `_parse_json()` in `llm.py` handles markdown fences, trailing commas, missing commas, unescaped newlines, and invalid escape sequences. When changing LLM prompt output format, check this still works.

8. **SearXNG unavailable** — `web_search()` returns `None` on connection errors. The eval pipeline continues with web research as empty dicts. The LLM still produces analysis from card data + similar cards.

9. **Server validates nothing** — deck save only checks non-empty name. All rules (100-card max, 4-copy limit, legend rule, CI compliance) are enforced client-side in `deck-builder.js`.

10. **ECON 2024** — this codebase follows a "ponytail" design philosophy (laziest-thing-that-works). Comment blocks beginning with `ponytail:` explain non-obvious design choices. Read them before modifying nearby code.

11. **app.py is only 116 lines now** — it's a thin app factory. All route logic is in `mtg/blueprints/`. New routes go in the appropriate blueprint or a new one registered in `app.py`.

12. **No tests for Deck Builder or templates** — the 4 pytest files (`tests/`) only cover LLM, mana, shared, and similarity. Manual QA docs are in `QA_TEST_PLAN.md` and `QA_FINDINGS.md`.
