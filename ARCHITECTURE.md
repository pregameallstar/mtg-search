# MTG Search — Architecture Overview

> Written for an agent that needs a strong working understanding of the codebase
> without reading every file. Covers what the app is, how it's organized, the
> data model, each feature's code path, the cross-cutting integration points,
> and the design conventions ("ponytail" philosophy) you must respect when
> modifying it.

---

## 1. What this is

A **self-hosted Magic: The Gathering card search / deck-building / Commander-analysis
web app** — effectively a personal Scryfall replica plus LLM-powered tooling.

| Axis | Choice |
|------|--------|
| Language / framework | Python 3.12 + **Flask 3** (blueprints), **Jinja2** templates |
| Frontend | **Vanilla JS + CSS** — no framework, no build step. One CDN dependency (`mana-font@1.18.0`) for mana symbols |
| Database | **SQLite** (`AllPrintings.sqlite`, sourced from **MTGJSON**), **no ORM** — raw SQL via `sqlite3` |
| Persistence | JSON files on disk for decks, templates, prices, eval reports |
| LLM | `openai` + `anthropic` SDKs behind one `generate()` function |
| Embeddings | `sentence-transformers` `all-MiniLM-L6-v2` → 384-dim vectors, `numpy` `.npy` on disk |
| Search infra | Self-hosted **SearXNG** (HTTP) for web research |
| Agent API | **MCP server** (FastMCP) exposing `semantic_search` / `keyword_search` / `get_card`, wrapped by an **MCPO** SSE→OpenAPI proxy |
| Deployment | **Docker Compose**: one `app` container (Flask + MCP SSE + MCPO) + one `searxng` container |

Scale: ~5,100 loc Python, ~5,000 loc templates, ~3,900 loc JS, ~3,700 loc CSS.
Database holds **~35,000 deduplicated English cards**.

**Data sources:** MTGJSON (card DB) · Scryfall (card images + prices, lazily cached) ·
SearXNG (web research) · Sentence Transformers (embeddings).

---

## 2. Design philosophy: "ponytail"

The codebase follows a self-described **"ponytail"** philosophy = *laziest thing
that works*. You will see `# ponytail:` comments everywhere; **read them before
modifying nearby code** — they explain non-obvious design choices. Recurring themes:

- **Single source of truth** via `mtg/shared.py` — constants and helpers live once,
  not re-typed per blueprint.
- **No ORM** — raw parameterized SQL, `PRAGMA query_only = ON` (app is read-only
  against the DB; the DB is replaced wholesale on ingest).
- **Pure functions, no Flask dependency** in engine modules (`similarity.py`,
  `embed.py`, `llm.py`, `images.py`, `dedup.py`, `ingest.py`, `eval_helpers.py`),
  so they're testable and reusable by the MCP server.
- **"Server validates nothing"** — deck/template rules are enforced client-side.
- **Config** — API key/backends in the signed browser session; prompts in a small server-side `prompt_store/`.

---

## 3. Directory layout

```
app.py                  Flask app factory: secret key, security headers, Jinja globals,
                        DB teardown, blueprint registration (~118 lines). Thin.
mtg/
  __init__.py           empty (package marker)
  shared.py             THE shared module — constants + DB helpers + Jinja globals
  blueprints/
    __init__.py         empty
    search.py           /, /search, /card/<set>/<n>, /img/..., /card-autocomplete, /llm-models
    similar.py          /similar, /cards/<set>/similar, /card/<set>/<n>/similar
    eval.py             /card/<set>/<n>/eval/* (8 routes — the LLM pipeline)
    eval_landing.py     /commander-eval, /commander-eval/reports/delete
    decks.py            /deck-builder, /saved-decks, /api/deck/*, /api/deck/history/*
    pricing.py          /api/pricing/fetch, /api/pricing/status
    templates_bp.py     /card-templates, /api/template/*
    tags.py             /api/tags/* (global tag catalog)
    config.py           /config/* (LLM, ingest, embeddings, MCP, prompts)
  similarity.py         TF-IDF + factor-gate scoring engine (pure)
  embed.py              embedding index build/find/status (pure, thread-safe)
  llm.py                generate() + JSON repair, openai/anthropic (pure)
  images.py             card_image_url(), fetch_scryfall_image(), mana_symbols() (pure)
  websearch.py          SearXNG web_search() (pure)
  prompts.py            three LLM system prompt string constants (pure)
  eval_cache.py         in-memory OrderedDict caches for eval analysis
  eval_helpers.py       eval similarity backends, placeholder detection, deepdive autosave
  ingest.py             DB upload → validate → dedup → replace → rebuild embeddings (pure)
  dedup.py              dedup one row/unique English card (pure)
  mcp_server.py         FastMCP server: semantic_search, keyword_search, get_card
  mcp_control.py        MCP process management (port_alive, restart_mcp)
templates/              Jinja2 templates (all extend base.html)
static/
  deck-builder.js       the entire Deck Builder client (ES5 IIFE, ~3,900 lines)
  style.css             all styling (~3,700 lines, dark theme)
tests/                  pytest suite (Python + a QuickJS harness for deck-builder.js)
card_templates/         saved template JSON (Default_Guideline.json)
decks/                  saved deck JSON + _tags.json + _history/
prices/                 Scryfall price cache JSON
images/                 lazy Scryfall image cache
embeddings/             vectors.npy + meta.json + build_info.json + .build.lock
eval_reports/           saved eval JSON + *_deepdives.json autosaves
searxng/                settings.yml + limiter.toml (config only)
docker-compose.yml      app + searxng services
docker-entrypoint.sh    starts Flask + MCP SSE + MCPO, supervises them
Dockerfile              python:3.12-slim, non-root user matching host UID/GID
```

---

## 4. Application entry point — `app.py`

A thin factory. Responsibilities:

1. **Secret key** — read `SECRET_KEY` env or `.secret_key` file; auto-generate and
   persist on first run (so sessions survive restarts). 30-day session lifetime.
2. **Security headers** — CSP, `X-Frame-Options` (DENY, or SAMEORIGIN when `?embed=1`),
   `X-Content-Type-Options: nosniff`.
3. **Jinja globals** — `card_image`, `render_mana`, `pagination_url`, `similarity_label`.
4. **`get_db` teardown** on app context, and a context processor injecting
   `db_unseeded=False` (routes that detect a missing DB flip it to `True` so
   `base.html` can show a "seed the database" banner).
5. **Blueprint registration** — 9 blueprints (search, similar, config, eval,
   eval_landing, decks, templates_bp, tags, pricing).

New routes go in an existing blueprint or a new one registered here.

---

## 5. Data model

### 5.1 The database (`AllPrintings.sqlite`)

Sourced from MTGJSON, then **deduplicated** (`mtg/dedup.py`) to **one row per unique
English card** (latest printing by set `releaseDate`, keeps both `side='a'` and
`side='b'` rows for DFCs). Only the latest printing is retained; older printings
are intentionally dropped to keep the DB and image cache small. Five tables:

| Table | Purpose | Key columns |
|-------|---------|-------------|
| `cards` | primary card data | `uuid` (PK), `name`, `text` (oracle), `type`, `types`, `subtypes`, `supertypes`, `manaCost`, `manaValue`, `colors`, `colorIdentity`, `keywords`, `power`/`toughness`/`loyalty`, `setCode`, `number`, `side`, `otherFaceIds`, `language`, `printings`, `rarity`, `borderColor`, `layout`, `frameVersion`, boolean flags (`isReprint`, `isReserved`, `isFunny`, `isOversized`, `isFullArt`, `isTextless`, `isPromo`, `isRebalanced`, `isGameChanger`), `leadershipSkills` (JSON), `edhrecRank`, `edhrecSaltiness`, `availability`, `artist`, etc. |
| `cardIdentifiers` | Scryfall ID | `uuid`, `scryfallId` |
| `cardLegalities` | format legality | `uuid`, + one column per format (`commander`, `standard`, `modern`, …) |
| `cardRulings` | oracle rulings | `uuid`, `date`, `text` |
| `sets` | set metadata | `code`, `name`, `releaseDate` |

The DB is **read-only** at runtime (`PRAGMA query_only`). It's replaced atomically
by the ingest pipeline. Re-ingest only replaces the database files; the
`images/` cache is never cleaned, so previously cached card images remain in
place even when the latest printing changes.

### 5.2 Critical data quirks (you WILL trip on these)

1. **DFC names** — multi-faced cards have names like `"A // B"`. Many queries strip
   at `" // "` for dedup/name-search. New name logic must handle this.
2. **Color ordering differs by column** — `colors` is **WUBRG** order; `colorIdentity`
   is **alphabetical** (both comma-separated strings like `"W, U"`). When comparing
   CI across cards, always `sorted()` / `wubrg_sort()` / `color_identity_subset()`
   from `shared.py`, never string equality.
3. **English + single-faced filter** — the recurring SQL fragment
   `(c.side IS NULL OR c.side = 'a')` + `c.language = 'English'` appears everywhere.
   `shared.py` defines `_ENGLISH_SIDE` for the side clause.
4. **`power`/`toughness`/`loyalty` are TEXT** and may be `'*'` — use
   `CAST(c.x AS REAL)` for numeric comparison (`_n()` helper in `search.py`).
5. **`colors`/`colorIdentity` empty string** = colorless.

### 5.3 JSON-file stores (non-DB persistence)

| Dir | Contents | Shape |
|-----|----------|-------|
| `decks/` | one `.json` per deck + `_tags.json` | deck object (below) |
| `decks/_history/` | one `.json` per deck | array of `{timestamp, ...}` events |
| `card_templates/` | one `.json` per template | `{type: "cards"\|"guideline", name, cards/categories}` |
| `prices/` | `{scryfallId}.json` | `{scryfallId, prices, fetched_at}` |
| `eval_reports/` | `{set}_{num}_{timestamp}.json` + `{set}_{num}_deepdives.json` | saved analysis |
| `embeddings/` | `vectors.npy`, `meta.json`, `build_info.json`, `.build.lock` | embedding index |

**Filename sanitization** is centralized in `shared.sanitize_filename(name)`:
strips everything outside `[a-zA-Z0-9_ -]` (blocks `..`/`/` path traversal
incidentally), replaces spaces with `_`, appends `.json`. `mtg/storage.py` wraps
it with reserved-name checks (`_tags.json`) and collision detection, so two
distinct display names that sanitize to the same file cannot silently overwrite
each other. `write_json_atomic` is used for deck/template/history/tag/report
writes.

### 5.4 Deck object (client-side data model)

```js
Deck {
  name: string,             // doubles as the filename key
  commander: card | null,   // {uuid, setCode, number, name, manaCost, types, colors, colorIdentity, imageUrl, ...}
  cards: [card],            // main 99; each {uuid, setCode, number, name, quantity, tags, ...}
  evalData: object | null,  // cached Commander Eval analysis blob
  sections: {"Sideboard": [card], "Maybeboard": [card], ...},
  guideline: string | null  // guideline template name
}
```

The server stores deck JSON **verbatim** — it only validates a non-empty name.
Commander rules (100-card main deck, singleton except basic lands and explicit
"any number" cards, legend/color-identity checks) are enforced in
`deck-builder.js`, with the same 100-card/singleton checks on the server-side
`/api/deck/add-card` path.

---

## 6. Feature map (each tool = blueprint + template + JS)

Every page extends `templates/base.html`, which provides: sidebar nav (hidden when
`?embed=1`), the card-detail side panel (`openPanel()` → `/card/<set>/<n>?fragment=1`),
the image lightbox, a global `MutationObserver` that makes every `.card-cell`
draggable, and an Escape-key handler.

### 6.1 Search — `mtg/blueprints/search.py` (520 loc)

`GET /search` builds a SQL `WHERE` clause from **30+ query params**: free-text `q`
(name/type/oracle, comma-AND), `name`, `oracle`, `type_line` (em-dash normalized),
`mana_cost` (exact), `keywords` (comma-AND), `color`/`ci` with `*_rule`
(`at_least`/`exact`/`at_most`), `mv` (supports ranges `1-3` + operators), `pow`/`tou`/`loy`,
`rarity` (multi), `game` (multi), `format`+`legality`, 8 boolean flags, `border`,
`layout`, `frame`.

Also in this blueprint:
- `GET /` → redirect to `/search`.
- `GET /card/<set_code>/<number>` → full card detail (rulings, legalities, latest
  set info, other faces). `?fragment=1` renders `card_panel.html` for the side panel.
- `GET /img/<size>/<face>/<c1>/<c2>/<scryfallId>.jpg` → lazy cache-through image proxy
  (fetches from Scryfall with exponential backoff, caches to `images/`).
- `GET /card-autocomplete?q=` → top 10 name matches (JSON).
- `GET /llm-models?base_url=` → proxy to list models from a configured backend.

**Gotcha:** `deck-builder.js` does NOT call a search JSON API — it fetches `/search?...`
HTML and parses `.card-grid` / `.result-count` / pagination with `DOMParser`.
Changing `search.html`'s grid structure breaks the Deck Builder's embedded search tab.

### 6.2 Similarity — `mtg/blueprints/similar.py` (359 loc) + `mtg/similarity.py` (295 loc)

Finds mechanically similar cards via **two backends**:

- **`embed` (semantic, default)** — `embed.find(db_path, text, top_k=200)` cosine
  search, then hard post-filters (name exclusion, color-identity subset, MV ±3,
  optional type). Scores = cosine × 100.
- **`legacy` (TF-IDF)** — SQL candidate fetch (same-name exclusion, MV ±3, latest
  printing), scored by `score_similarity()` per candidate.

`similarity.py` is the pure engine:
- `extract_terms()` — tokenizes oracle text, strips reminder text `(…)`, keyword
  ability labels (`KEYWORD_TERMS`), stop words (`MTG_STOP_WORDS` when `mtg_filter`),
  does shallow plural stemming.
- `get_idf()` — IDF over all oracle terms; **module-level cache** rebuilt if card
  count drifts by >100 (bump `_idf_version` when `extract_terms` changes).
- `score_similarity()` — oracle text overlap (IDF-weighted, [0,1]) is the base signal;
  each active factor (types/keywords/subtypes/supertypes/MV/color-identity) is a
  **gate** `1/(1 + w·strictness·mismatch)`, combined via **geometric mean**; oracle
  score is exponentiated by `s_oracle`.
- `similarity_label()` — "Near Perfect"/"Strong"/… ranking relative to best match.
- `wubrg_sort()`, `normalize_dash()`, `split_csv()` — small utilities.

Strictness calibration constants live in `similar.py` (`STRICT_VALUES`, `COLOR_STRICT`,
`ORACLE_STRICT`); color-identity "strict" is all-or-nothing (≥200).

Routes: `/similar` + `/cards/<set>/similar` (landing, POST = image-drop lookup),
`/card/<set>/<num>/similar` (results, 14+ tuning params, `?export=1` → self-contained
HTML download). The landing first-visit sets all factors ON + strict via a redirect.

### 6.3 Commander Eval — `mtg/blueprints/eval.py` (809 loc) + `eval_landing.py` (114 loc)

The most complex tool. `url_prefix="/card/<set_code>/<number>/eval"`.

**6-step pipeline** (run via `POST /analyze`, polled via `GET /progress`):
1. Load card data (oracle text + rulings + DFC back face).
2. SearXNG web research (5 queries × 5 results) — `websearch.web_search()` returns
   `None` when unreachable, so the pipeline degrades gracefully.
3. `embed.find()` for mechanically similar cards (200, CI-filtered, trimmed to metadata).
4. LLM call (`COMMANDER_SYSTEM_PROMPT` + card data + web research + similar cards).
5. Second-pass **verification** LLM call (`VERIFY_SYSTEM_PROMPT`) — catches invented
   abilities, wrong zones, hallucinated names, CI errors.
6. Store in server-side cache.

Other routes: `/` (page), `/restore` (Deck Builder re-injects saved `evalData` into
cache), `/deepdive` (strategy/unique-build expansion — dual-pass embed retrieval:
commander text + strategy text), `/similar` (find similar legendaries, embed/legacy),
`/save` (write report to `eval_reports/`), `/load`, plus landing-page routes in
`eval_landing.py` (`/commander-eval`, `/commander-eval/reports/delete`).

**Cache (`mtg/eval_cache.py`)** — because Flask session cookies cap at ~4KB but
analysis output exceeds it, results live in an in-memory `OrderedDict`
(`_eval_cache`, FIFO 100) keyed by a UUID stored in the session. **Critical guard:**
each entry stores `_card: "set/number"`; on page load `eval.py` verifies the cached
analysis belongs to the viewed card (session `eval_key` bleeds across navigation).

Deepdives auto-save to `{set}_{num}_deepdives.json` (`eval_helpers.auto_save_deepdives`).

### 6.4 Deck Builder — `mtg/blueprints/decks.py` (381 loc) + `static/deck-builder.js` (3,931 loc)

Split-panel drag-and-drop Commander deck builder — the most JS-heavy tool. The
entire client is one **ES5 IIFE** (no module system, `var`/`function` style).

Server routes in `decks.py`:
- Pages: `/deck-builder?deck=<name>`, `/saved-decks`.
- Deck CRUD: `/api/deck/save|list|delete`.
- Card lookups: `/api/deck/card-lookup` (setCode+number or uuid),
  `/api/deck/lookup-by-name` (latest printing), `/api/deck/lookup-by-uuid`
  (Scryfall ID → setCode+number), `/api/deck/cards/enrich` (batch add `scryfallId`).
- History: `/api/deck/history|history/save|history/delete|history/check`.

`tags.py` provides `/api/tags/list|save` for a global tag catalog (auto-seeds 8
predefined tags, stored in `decks/_tags.json`).

**JS architecture (`deck-builder.js`):**
- `Deck` constructor + prototype — the data model.
- `classifyCard()` — regex-based Lands/Ramp/Removal/Draw/Core auto-classification.
- Embedded tools in a 4-tab left panel: **Search** (scrapes `/search` HTML),
  **Similarity** (scrapes `/card/.../similar`, takes `grids[1]`), **Commander Eval**
  (iframe of `.../eval?embed=1`), **Templates** (via `/api/template/list`).
- Autosave: 600ms debounce + `sendBeacon` on `beforeunload`/`visibilitychange`.
- Load: synchronous XHR to `/api/deck/list`, linear search by name.
- Card hover preview (250ms delay, 400×560px).

**Known dead/stale JS:** `STORAGE_PREFIX` and `activePopoverCard`. The tag
popover is duplicated twice (main deck + custom sections); the tag filter bar is
now wired into `renderSections`/`renderAll`.

### 6.5 Card Templates — `mtg/blueprints/templates_bp.py` (60 loc)

`/card-templates` page + `/api/template/save|list|delete`. Two types:
- **card templates** `{name, cards:[…]}` (default type `"cards"`).
- **guideline templates** `{type:"guideline", name, categories:{Lands, Ramp, Draw, Removal, Core}}`
  — category-count targets. The Deck Builder auto-creates "Default Guideline" if none exist.

### 6.6 Pricing — `mtg/blueprints/pricing.py` (128 loc)

`/api/pricing/fetch` (POST, up to 200 scryfallIds) → fetches Scryfall card prices
(`usd`, `usd_foil`, `usd_etched`, `eur`, …) with a **24h disk cache** in `prices/`.
Retries 3× with backoff; on failure falls back to stale cache. `/api/pricing/status`
returns cache count. Pricing store selection (`usd`/`usd_foil`/`eur`) is a session config.

### 6.7 Configuration — `mtg/blueprints/config.py` (265 loc)

`GET/POST /config` manages:
- **LLM** — backend (`openai`/`anthropic`), base URL, model, API key, and three
  **custom prompts** (commander, deepdive, verify). The API key stays in the
  signed session cookie (it is small); prompts live in `prompt_store/`, keyed by
  an opaque browser id, because the default prompt is larger than the ~4 KB
  cookie limit.
- **Database** — status (card count, last ingest), upload → ingest pipeline.
- **Embeddings** — status + async rebuild (`/config/embed-build`).
- **MCP** — status + restart (`/config/mcp-status`, `/config/mcp-restart`).
- **Search Logic** — default similarity methods (embed/legacy) for both tools.
- **Pricing / History** — pricing store, history warning threshold.

Ingest pipeline (`mtg/ingest.py` + `mtg/dedup.py`): validate uploaded file
(`.sqlite`, `.gz`, `.bz2`, `.xz`, `.zip` — stream-decompressed), require ≥1000 English
cards, dedup to one row/unique card, atomically replace the live DB, persist
`.last_ingest.json`, then fire-and-forget an embedding rebuild.

---

## 7. Cross-tool integration points (the hard-won coupling)

These are where changes in one tool silently break another:

1. **Deck Builder ↔ Search/Similarity:** the builder *scrapes* the search and
   similarity **HTML** (`DOMParser`), not a JSON API. `.card-grid` / `.result-count`
   / pagination structure in `search.html` and `similar.html` is load-bearing.

2. **Deck Builder ↔ Commander Eval:** iframe of `/card/<set>/<n>/eval?embed=1`;
   the eval page posts `{type:'mtg-eval-data', data}` to `window.parent`; on deck
   load `restoreEvalData()` POSTs to `/eval/restore` to warm the server cache.

3. **Card drag-and-drop (global):** `base.html`'s `MutationObserver` marks every
   `.card-cell` draggable with `application/mtg-card` = `{setCode, number}`.
   `deck-builder.js` `handleCardDrop()` consumes it (also handles external Scryfall UUIDs).

4. **Card detail panel (global):** `openPanel(setCode, number)` → fetches
   `?fragment=1`; `deck-builder.js` injects an "Add to Deck" button into it via a
   `MutationObserver` on `#card-panel-sheet`.

5. **`embed=1` param:** removes sidebar chrome in `base.html` and relaxes
   `X-Frame-Options` to `SAMEORIGIN` in `app.py` (so the iframe can render).

---

## 8. Supporting modules (used by multiple features)

| Module | Role | Consumers |
|--------|------|-----------|
| `shared.py` | `db_path()` (handles Docker bind-mount-dir case), `get_db()` (query_only), `color_identity_subset()`, `card_image`/`render_mana`/`pagination_url`, all `*_DIR`/`*_PATH` constants, `sanitize_filename()`, `_latest_printing()`, `_load_card()`, `_other_faces()`, `_list_json_files()`, `_db_ready()`, `VALID_FORMATS` | everything |
| `similarity.py` | TF-IDF scoring engine (pure) | similar, eval_helpers, mcp_server |
| `embed.py` | `build()`, `find()`, `status()` — thread-safe, model cached per process | similar, eval, config, mcp_server |
| `llm.py` | `generate()` (openai/anthropic), `_parse_json()` JSON repair, `fetch_models()` | eval, config |
| `images.py` | `card_image_url()`, `fetch_scryfall_image()` (3-retry backoff), `mana_symbols()` | search, shared |
| `prompts.py` | `COMMANDER_SYSTEM_PROMPT`, `DEEPDIVE_SYSTEM_PROMPT`, `VERIFY_SYSTEM_PROMPT` | eval, config |
| `websearch.py` | `web_search(query, max)` → SearXNG, `None` on failure | eval |
| `eval_cache.py` | in-memory FIFO caches + lock | eval |
| `eval_helpers.py` | `eval_similar_embed/legacy`, `get_commander_filter_sets()` (10-min TTL), `is_placeholder_name()`, deepdive autosave | eval |
| `ingest.py` / `dedup.py` | DB upload → validate → dedup → replace → rebuild | config |
| `mcp_server.py` | FastMCP `semantic_search`, `keyword_search`, `get_card` | standalone process |
| `mcp_control.py` | `port_alive()`, `restart_mcp()` | config, docker-entrypoint |

---

## 9. MCP server (`mtg/mcp_server.py`, 403 loc)

Standalone FastMCP server exposing five tools to LLM agents, backed by
`embed.find()` and raw SQL. **No Flask, no auth, read-only for cards; deck tools
are read-only.** Long `instructions` string encodes the tool-selection
heuristics: `semantic_search` for "what a card does" (mechanics),
`keyword_search` for attributes/name-fragments, `get_card` for full detail,
and `list_decks`/`get_deck` for saved-deck inspection.

- `semantic_search(query, color_identity, card_type, mana_value_min/max, limit)` —
  embed search (top_k=800) + CI subset + type substring + MV range filters.
- `keyword_search(...)` — literal LIKE over name/type/oracle, deduped by name via
  `ROW_NUMBER()` picking the latest printing.
- `get_card(name)` — full detail incl. rulings + legalities.

Runs `stdio` (default) or `--transport sse --port 8765` (LAN, CORS `*`, DNS-rebinding
protection off). MCPO (`mcpo`) fronts the SSE endpoint as an OpenAPI proxy on `:8000`
for OpenWebUI. `mcp_control.py` provides the restart logic used by the config page.

---

## 10. Infrastructure / deployment

```
docker-compose.yml
├── app    (Flask :5000, MCP SSE :8765, MCPO :8000 — all in ONE container)
│   └── docker-entrypoint.sh — starts all 3, supervises: restarts MCP/MCPO if they
│       die, shuts everything down if Flask dies.
└── searxng (:8888→8080, localhost only) — JSON enabled, rate limiter off.
```

`Dockerfile` builds as a non-root user matching host UID/GID so bind-mount writes are
owned correctly (and `~/.cache` exists for model downloads). **Dev bind-mounts**
overlay `app.py`, `mtg/`, `templates/`, `static/`, `decks/`, `card_templates/`,
`prices/` — live edit, no rebuild.

Key env vars: `SEARXNG_URL`, `LLM_BACKEND`/`LLM_BASE_URL`/`LLM_MODEL`/`LLM_API_KEY`,
`SECRET_KEY`, `PORT`, `MCP_SSE_PORT`, `MCPO_PORT`, `MCP_HOST`, `MTG_DATABASE`
(MCP only), `PRICING_STORE`.

---

## 11. Tests (`tests/`, 1,680 loc)

- `test_deck_builder.py` (928) — **execution** tests of `static/deck-builder.js` pure
  logic via a **QuickJS** harness: loads the IIFE, stubs DOM/browser globals with
  autovivifying Proxies, neutralizes the auto-run `init()`, exposes internals on
  `__exposed`, drives them through a JSON round-trip. See [[deck-builder-js-tests]].
- `test_similarity.py` (267) — `extract_terms`, `score_similarity`, gates, `similarity_label`.
- `test_qa_fixes.py` (255) — regression tests keyed to prior QA findings (B1-B3, M1-M4, Q1-Q5).
- `test_shared.py` (114) — `db_path`, `color_identity_subset`, `resolve_bind_path`, filename sanitization.
- `test_llm.py` (69), `test_mana.py` (47).

**No tests for the Search/Similarity/Templates frontends or the pricing/ingest flows** —
those are manual-QA'd. The QuickJS harness exists specifically to enable a safe
refactor of `deck-builder.js`.

---

## 12. Checklist before modifying code

1. Read the `# ponytail:` comments in the file and its neighbors.
2. If it touches DB access: use `get_db()` (query_only), respect the English +
   `side IS NULL OR 'a'` filter, and the `colors`(WUBRG) vs `colorIdentity`(alpha) split.
3. If it touches search/similar **HTML structure**: check `deck-builder.js`'s scraper.
4. If it touches LLM output format: check `llm._parse_json()` still repairs it.
5. If it touches filename handling: route through `sanitize_filename()`.
6. If it touches embeddings: rebuild is async + thread-safe; `status()` reports state.
7. New routes → existing blueprint or a new one registered in `app.py`.

---

## 13. Related docs

- `README.md` — user-facing feature documentation and setup.
- `PROJECT_MAP.md` — an older tool-first index (some line counts and details are now
  stale, e.g. it predates the pricing blueprint, deck history, and the enrich endpoint).
- Memory: [[project-map]], [[deck-builder-js-tests]].
