# MTG Search — QA Handoff Document

## 1. Application Overview

**MTG Search** is a self-hosted, single-file Flask web application for browsing, searching, and analyzing Magic: The Gathering cards. It serves as a Scryfall-like card search engine with two unique power tools: a **functional-card similarity engine** and an **AI-powered Commander evaluation tool**.

- **Repository**: https://github.com/pregameallstar/mtg-search
- **Stack**: Python 3.9+, Flask, SQLite (MTGJSON), Jinja2 templates, vanilla JavaScript, no build step
- **Data sources**: MTGJSON (card database), Scryfall CDN (card images)
- **External services**: SearXNG (web search), OpenAI/Anthropic API (LLM analysis)

---

## 2. Architecture

```
mtg-search/
├── app.py                  # Entire Flask application (single file, ~1979 lines)
├── llm.py                  # LLM client (OpenAI + Anthropic backends, ~96 lines)
├── run.sh                  # Start/stop/restart/status script
├── requirements.txt        # Python dependencies
├── templates/              # Jinja2 HTML templates (11 files)
├── static/style.css        # Single stylesheet (~31KB, dark theme)
├── searxng/                # Docker Compose + config for local SearXNG metasearch
├── AllPrintings.sqlite     # Symlink to MTGJSON database (external, not in repo)
├── .secret_key             # Auto-generated Flask session secret (persisted)
├── .last_ingest.json       # Timestamp of last database ingest
└── eval_reports/           # Saved Commander Eval analysis reports (JSON)
```

**Design notes**:
- Single-file Flask app — no ORM, no blueprints, no database migration layer
- Raw SQLite queries against MTGJSON's schema (read-only)
- Server-side rendering with Jinja2, vanilla JS for progressive enhancement
- Session-based config (API keys held in Flask session cookies, never persisted)
- Server-side analysis cache (`_eval_cache` dict) for Commander Eval results that exceed Flask's ~4KB cookie limit

---

## 3. Routes / Pages

### 3.1 Home Page
| Route | Method | Template |
|-------|--------|----------|
| `/` | GET | `index.html` |

Displays a hero section with total unique card count and the full search filter form (name, oracle, type line, mana cost, colors, stats, rarity, set, legality, boolean flags, border, layout, frame, display mode). The home page form omits the `keywords` filter that is present on `/search`. No cards are shown until the user submits a search.

**QA checks**:
- Page loads, displays total card count
- Missing database → page still renders but count shows 0 or error

### 3.2 Card Search
| Route | Method | Template |
|-------|--------|----------|
| `/search` | GET | `search.html` |

The main search interface. Supports ~20 filter dimensions:

| Filter | Type | Notes |
|--------|------|-------|
| `name` | text | Card name contains (LIKE) |
| `oracle` | text | Rules text contains (LIKE) |
| `type_line` | text | Full type line contains (LIKE) |
| `mana_cost` | text | Exact mana cost e.g. `{1}{R}{G}` |
| `keywords` | text | Comma-separated, AND logic |
| `q` | text | Free-text across name, type, AND oracle text (backend support only; no UI input exposed — orphaned parameter) |
| `color` + `color_rule` | checkboxes + select | WUBRGC colors; `at_least` / `exact` / `at_most` |
| `ci` + `ci_rule` | checkboxes + select | Color identity; same rules |
| `mv` + `mv_op` | text + select | Mana value; `=`, `<`, `>`, `<=`, `>=`, or range `X-Y` |
| `pow` / `tou` / `loy` | text + select | Power, Toughness, Loyalty; same operators |
| `rarity` | checkboxes | Multi-select: common, uncommon, rare, mythic, special, bonus |
| `set` | text | Set code (3-letter uppercase) |
| `format` + `legality` | selects | Format legality filter |
| `is_reprint`, `is_reserved`, `is_funny`, `is_oversized`, `is_fullart`, `is_textless`, `is_promo` | checkboxes | Boolean flags (7 visible; `is_rebalanced` exists in backend but has no UI checkbox) |
| `border` | select | black, white, silver, borderless (backend also supports gold, yellow — no UI)
| `layout` | select | 20+ layout types |
| `frame` | select | Frame version: 1993, 1997, 2003, 2015, future |
| `unique` | select | "Unique Cards" (dedupe by name) or "All Printings" |

Results display as a card image grid (30 per page, paginated). Each card links to its detail page.

**QA checks**:
- No filters → empty state shown (not all cards)
- Single filter → correct results
- Multiple filters combined → AND logic works
- Each color rule (at_least / exact / at_most) behaves correctly
- Stat comparisons (=, <, >, <=, >=) work for manaValue, power, toughness, loyalty
- Mana value range (`X-Y`) works
- Rarity multi-select works
- Format legality + legal/banned/restricted toggle works
- Boolean flags work (is_reprint, is_reserved, etc.)
- Keywords AND logic works (comma-separated)
- "Unique Cards" vs "All Printings" toggle
- Pagination works and preserves all filters
- Active filter badges display correctly
- Empty result set shows "0 cards found"
- Special characters in search input don't break queries (SQL injection impossible via parameterized queries)

### 3.3 Card Detail
| Route | Method | Template |
|-------|--------|----------|
| `/card/<set_code>/<number>` | GET | `card.html` |
| `/card/<set_code>/<number>?fragment=1` | GET | `card_panel.html` |

Full card detail page showing:
- Large card image (from Scryfall CDN, using `scryfallId`)
- All faces for double-faced cards
- Oracle text (with newlines preserved)
- Flavor text (if present)
- Power/Toughness or Loyalty
- Set info (name, code, number, rarity, artist)
- Mana value and color identity
- All printings across sets (from `printings` field)
- Format legality table
- Official rulings from `cardRulings`

The `?fragment=1` variant returns just the panel body for side-panel loading (AJAX). The side panel opens on click from search results/similarity results and supports:
- Browser history (back button closes panel)
- Escape key to close
- Click-outside to close

**QA checks**:
- Card detail page renders correctly
- Mana symbols render as icons
- Double-faced cards show both faces
- All printings list is correct
- Legality table shows all formats
- Rulings display in reverse chronological order
- Side panel opens/closes with click, Escape, and back button
- Side panel loads content via AJAX
- Non-existent card → 404
- Cards with missing optional fields (flavorText, power/toughness, loyalty) don't error

### 3.4 Card Similarity

#### Landing Page
| Route | Method | Template |
|-------|--------|----------|
| `/cards/<set_code>/similar` | GET | `similar_landing.html` |
| `/cards/<set_code>/similar` | POST | (JSON response) |

Allows searching by:
- **Name** — text input with submit
- **Drag-and-drop** — extracts Scryfall ID from URL, HTML, or filename; looks up card and redirects to results

Displays tuning panel (same factors as results page) so users can pre-configure before selecting a card.

#### Results Page
| Route | Method | Template |
|-------|--------|----------|
| `/card/<set_code>/<number>/similar` | GET | `similar.html` |
| `/card/<set_code>/<number>/similar?export=1` | GET | `similar_export.html` |

**Algorithm**: IDF-weighted oracle text overlap with optional multiplicative factor gates.

- **Oracle text** — always scored. Terms extracted after stripping reminder text. IDF weights computed once per server lifetime (cached). Score is `shared_idf_sum / base_idf_sum`, then exponentiated by oracle strictness.
- **Optional gates** (checkbox toggles): Types, Supertypes, Subtypes, Keywords, Mana Value, Color Identity
  - Each gate uses Jaccard similarity → `1 / (1 + weight × strictness × (1 - jaccard))`
  - Mana value uses absolute difference instead of Jaccard
  - Color identity has a "strict" tier (`s >= 200`) that enforces exact match (score = 0 on mismatch)
  - All gates combined via geometric mean
- **Strictness levels** per factor: Strict (high penalty for mismatch), Moderate, Loose
- **MTG filter**: strips ubiquitous MTG terms from oracle text
- **Score threshold filter**: show only cards above/below a percentage
- **Advanced weights**: per-factor weight multipliers (collapsed by default)
- **Top N**: separate the top N cards from remaining results
- **Export**: HTML download of top N results (requires CDN for mana-font CSS and card images — not fully offline)
- **Results show**: "Top N" section first, then paginated "All Results" below

Candidates are pre-filtered to ±3 mana value of the base card.

**QA checks**:
- Default state: all factors ON, strictness = strict, score threshold ≥ 75%
- Each factor toggle on/off changes results
- Strictness changes (strict → moderate → loose) broaden results
- MTG filter reduces false matches on common terms
- Score threshold filter works (greater than / less than)
- Top N selector changes display
- Advanced weights affect scoring when enabled
- All factors off → oracle-only scoring still produces results
- Base card with no oracle text → hint shown, optional factors still work
- "Clear Commander" button returns to landing
- "Export Top N" downloads an HTML file (has CDN dependencies — not fully offline)
- Recursion: clicking the ⇄ button on a similar card makes THAT the new base card
- Card detail panel opens from similarity results
- Name search from similarity results page preserves tuning parameters
- Pagination on "All Results" preserves tuning state

### 3.5 Commander Evaluation

#### Landing Page
| Route | Method | Template |
|-------|--------|----------|
| `/commander-eval` | GET | `commander_eval_landing.html` |
| `/commander-eval` | POST | (JSON response) |

- Card name search with **autocomplete** (debounced, keyboard-navigable)
- Drag-and-drop image lookup (same as similarity tool)
- **Saved reports** section: lists previously saved analyses with delete buttons

#### Evaluation Page
| Route | Method | Template |
|-------|--------|----------|
| `/card/<set_code>/<number>/eval` | GET | `commander_eval.html` |

Displays commander card header with EDHREC rank and salt score (if available). Shows analysis once generated.

**Analysis pipeline** (triggered by "Analyze" button):
1. Load card data + official rulings from database
2. Run 5 web searches via SearXNG (sequential): deck guides, strategy, community reviews, unique archetypes, mechanic rules
3. Send combined card data + web research to LLM (OpenAI or Anthropic)
4. Second-pass verification: LLM checks for invented abilities, wrong zone references, hallucinated card names
5. Result cached server-side in `_eval_cache`

**Analysis output sections**:
- **Strengths** (3–5 items, each citing a specific mechanic)
- **Weaknesses** (3–5 items)
- **Strategies** (2–4 named strategies with descriptions)
- **Deck-Building Priorities** (3–5 ranked priorities with reasons)
- **Unique Builds** (1–3 off-meta angles) — if none found, empty array
- **Kill-On-Sight** score (1–10) with reasoning
- **Bracket Analysis** (5 entries, brackets 1–5): effectiveness rating, reasoning, per-bracket KOS score and note
- **Verification warnings** (rules-check pass): flags invented abilities, wrong zones, hallucinated card names, misstated mechanics, color identity errors

**Interactive features on results page**:
- **Deep Dive** buttons on each strategy/unique build → side panel with expanded analysis (strengths, weaknesses, priorities, win conditions)
- **Find Similar** button → finds similar legendary creatures (strictness-loosening loop: strict → moderate → loose → very loose)
- **Save Report** → saves analysis + deep dive results + similar commanders to `eval_reports/` as JSON
- **Export** → self-contained HTML file with embedded CSS, deep-dive cache, and interactive panel

**AJAX endpoints**:
| Route | Purpose |
|-------|---------|
| `POST .../eval/analyze` | Run full analysis pipeline |
| `POST .../eval/expand` | Deep-dive on a specific strategy/build |
| `POST .../eval/similar` | Find similar commander-legal legendary creatures |
| `POST .../eval/save` | Save current analysis to disk |
| `POST .../eval/load` | Load a saved analysis from disk (backend only; frontend loads reports via `?report=<filename>` URL param instead) |
| `POST /commander-eval/reports/delete` | Delete a saved report |

**QA checks**:
- Landing page loads with saved reports list
- Autocomplete works (2+ chars, debounced, arrow keys, Enter, Escape)
- Drag-and-drop image lookup finds card and redirects
- Commander that's not a legendary creature → warning shown
- Game Changer card → warning shown
- "Analyze" button triggers AJAX pipeline with progress indicator
- Analysis renders all sections correctly (strengths, weaknesses, strategies, priorities, unique builds, KOS, brackets)
- Non-commander-legal cards still analyze (with warning)
- Verification warnings display when LLM hallucinates
- Deep Dive opens side panel, loads via AJAX, caches results
- Deep Dive on already-loaded strategy uses cache (no second API call)
- Find Similar populates grid with results
- Save succeeds and appears in landing page reports list
- Delete report works with confirmation dialog
- Export downloads self-contained HTML with working Deep Dive panel
- Report loaded via `?report=<filename>` restores all analysis + saved expands + similar
- Clear button returns to landing
- Name search from eval page redirects correctly
- Error states: no API key, network failure, LLM timeout handled gracefully

### 3.6 Configuration
| Route | Method | Template |
|-------|--------|----------|
| `/config` | GET/POST | `config.html` |

**LLM Configuration**:
- Backend selector: OpenAI/Compatible or Anthropic
- Base URL (for OpenAI-compatible endpoints)
- Model selector (populated via "Test Connection")
- API Key (stored in session cookie only, never persisted)

**Database Management**:
- Shows current database stats (card count, last ingest timestamp/filename)
- Drag-and-drop or file picker to ingest a new database
- Supports: `.sqlite`, `.gz`, `.bz2`, `.xz`, `.zip`
- Decompresses archives, finds `.sqlite` inside
- Validates card count (must have ≥ 1000 English cards)
- Atomically replaces active database via `shutil.move`

**Prompt Customization**:
- Commander Eval system prompt (editable textarea, reset to default)
- Expand Strategy system prompt (editable textarea, reset to default)

**QA checks**:
- Config page loads with current settings
- "Test Connection" populates model dropdown for OpenAI endpoints
- Anthropic backend shows hardcoded model list without API call
- API key persists across page loads (session cookie)
- Blank API key submission clears stored key
- Database ingest: valid file accepted, invalid file shows error
- Database ingest: compressed formats (gz, bz2, xz, zip) work
- Database ingest: tar.gz with .sqlite inside works
- Database ingest: invalid file (not SQLite, no cards table, too few cards) properly rejected
- Prompt save persists to session
- Prompt reset restores default text

---

## 4. Data Sources & External Dependencies

### 4.1 MTGJSON SQLite Database
- **File**: `AllPrintings.sqlite` (symlink to actual file)
- **Download**: https://mtgjson.com/downloads/all-files/
- **Tables used**:
  - `cards` — all card data (name, text, type, manaCost, colors, power, toughness, etc.)
  - `cardIdentifiers` — maps card UUID to scryfallId (for images)
  - `cardLegalities` — format legality per UUID
  - `cardRulings` — official rulings per UUID
  - `sets` — set metadata (name, code, releaseDate)
- **Key assumptions**:
  - English-language cards only (`language = 'English'`)
  - Front face only for double-faced cards (`side IS NULL OR side = 'a'`)
  - Grouping by `name` for unique card deduplication

### 4.2 Scryfall CDN (Card Images)
- **URL pattern**: `https://cards.scryfall.io/{size}/{face}/{c1}/{c2}/{scryfallId}.jpg`
- **Sizes used**: `normal` (card grids), `large` (detail views)
- **No API key required** — CDN is public
- **Fallback**: If `scryfallId` is missing, a placeholder div is shown

### 4.3 Mana Font CDN
- **URL**: `https://cdn.jsdelivr.net/npm/mana-font@1.21.0/css/mana.min.css`
- **Purpose**: Render mana symbols (`{W}`, `{U}`, `{B}`, `{R}`, `{G}`, `{C}`, hybrid, phyrexian, etc.) as SVG icons
- **Required** for proper mana cost display

### 4.4 SearXNG (Web Search)
- **URL**: `http://localhost:8888` (hardcoded in `app.py` line 998 — no env var override)
- **Used by**: Commander Eval analysis pipeline
- **Searches performed** (5 sequential):
  1. Deck guides: `"{card_name} commander deck guide primer"`
  2. Strategy: `"{card_name} commander strategy synergies"`
  3. Community reviews: `"{card_name}" commander review reddit`
  4. Unique archetypes: `"{card_name} commander unusual underrated hidden archetype brew"`
  5. Mechanic rules: `"mtg rules {keywords} comprehensive rules guide"` (only if card has keywords)
- **Timeout**: 15 seconds per search
- **Failure mode**: Individual search failures are silent; analysis proceeds with whatever results returned
- **No SearXNG running** → web research returns empty, LLM analysis still runs with just card data

### 4.5 LLM API (OpenAI / Anthropic)
- **OpenAI**: Uses `openai` Python package, `/chat/completions` endpoint
- **Anthropic**: Uses `anthropic` Python package, Messages API
- **Config**: backend, model, base URL, API key (all stored in session or env vars)
- **Default models**: OpenAI → `gpt-4o`, Anthropic → `claude-sonnet-5`
- **JSON parsing**: Robust parser handles markdown code fences, trailing commas, unescaped newlines
- **Verification pass**: Second LLM call checks for factual errors after main analysis

---

## 5. Key Algorithms

### 5.1 Similarity Scoring
1. **IDF computation**: Term frequency across all English cards, computed once per server lifetime, cached. `idf(term) = log(total_cards / cards_containing_term)`.
2. **Term extraction**: Strip reminder text → lowercase → tokenize → filter stop words → light plural stemming → remove keyword abilities → optional MTG-domain filtering.
3. **Oracle score**: `sum(idf for shared_terms) / sum(idf for base_terms)`, exponentiated by oracle strictness.
4. **Factor gates**: Each active factor computes Jaccard similarity → penalty curve `1 / (1 + weight × strictness × (1 - similarity))`.
5. **Combination**: Geometric mean of all active gates, multiplied by oracle score.
6. **Score normalization**: 0–1 range, displayed as 0–100%.

### 5.2 Commander Similarity (strictness loosening)
Runs similarity scoring over legendary creatures with Commander legality in rounds:
| Round | Oracle | General Strictness | Color Strictness | Threshold |
|-------|--------|--------------------|--------------------|-----------|
| Strict | 5.0 | 200.0 | 200.0 | 0.75 |
| Moderate | 2.0 | 8.0 | 6.0 | 0.60 |
| Loose | 1.0 | 1.5 | 0.5 | 0.40 |
| Very Loose | 0.5 | 0.5 | 0.5 | 0.0 |

Stops when ≥ 10 results found or all rounds exhausted. Returns top 10.

---

## 6. Session & State Management

- **Flask session**: Signed cookie (`SECRET_KEY` persisted in `.secret_key` for restarts)
- **Session data stored**: `llm_api_key`, `llm_backend`, `llm_base_url`, `llm_model`, `eval_prompt`, `expand_prompt`, `eval_key`
- **Server-side cache**: `_eval_cache` dict holds Commander Eval analysis results (too large for cookies). Keyed by UUID stored in session's `eval_key`. Validated per-card to prevent cross-navigation bleed.
- **Cache lifetime**: Server process lifetime (cleared on restart)
- **Persistence**: Save reports to `eval_reports/` directory as JSON files

---

## 7. Error Handling & Edge Cases

| Scenario | Behavior |
|----------|----------|
| Missing database | App starts but queries fail with SQLite errors |
| Invalid card route | HTTP 404 (abort) |
| Card with no oracle text | Similarity page shows hint, optional factors still work |
| Card with no Scryfall ID | Placeholder div instead of image |
| No LLM API key configured | "Analyze" returns error message |
| LLM returns non-JSON | Robust parser attempts repair (strip fences, fix trailing commas, fix unescaped strings) |
| LLM verification fails | Analysis still returns, just without `_verification` field |
| SearXNG unavailable | Web research returns empty array, analysis proceeds |
| Non-commander legendary creature | Analysis runs but warning shown |
| Game Changer card | Warning shown |
| Drag-and-drop with unidentifiable image | Error message, cleared after 5 seconds |
| Database ingest with corrupt file | Error with traceback |
| Database ingest with too few cards | Rejected with message |
| Cross-device shutil.move | Handled (shutil.move handles /tmp → project dir) |
| Large database file (>650MB) | Stream decompression avoids OOM |
| Concurrent requests | SQLite read-only mode; single writer is irrelevant for this app |
| XSS in card names/oracle text | Oracle text is escaped (`| e`), card names rendered as text |

---

## 8. Browser Compatibility

- **Target**: Modern browsers (Chrome, Firefox, Safari, Edge) — last 2 versions
- **Required APIs**: Fetch, History API (pushState/popstate), Drag and Drop API, CSS Grid, CSS Custom Properties
- **No mobile-specific design** (responsive via viewport meta, but primarily desktop-targeted)
- **No IE11 support** (uses Fetch, CSS Grid, Custom Properties)

---

## 9. Performance Considerations

- **IDF cache**: Computed once per server lifetime (~30k cards), reused across all similarity requests
- **Candidate pre-filter**: Similarity candidates filtered to ±3 mana value before scoring
- **SQLite read-only**: `PRAGMA query_only = ON` prevents accidental writes
- **Image loading**: `loading="lazy"` on all card images
- **No pagination of top results**: "All Results" section below Top N handles large result sets
- **Commander Eval**: Web searches + LLM call = typically 15–45 seconds for full analysis

---

## 10. Setup for Testing

```bash
# 1. Clone repo
git clone https://github.com/pregameallstar/mtg-search
cd mtg-search

# 2. Create venv and install
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# 3. Get database
# Download AllPrintings.sqlite from https://mtgjson.com/downloads/all-files/
# Place in project root or symlink:
ln -s /path/to/AllPrintings.sqlite AllPrintings.sqlite

# 4. Start server
./run.sh start
# → http://127.0.0.1:5000

# 5. Stop
./run.sh stop

# Via config page, you can also ingest compressed formats:
# .sqlite, .gz, .bz2, .xz, .zip (with .sqlite inside)
```

### Optional: Commander Eval testing

1. Start SearXNG on `localhost:8888`
2. Configure LLM at `http://127.0.0.1:5000/config`:
   - Set backend (OpenAI or Anthropic)
   - Enter API key
   - Verify model
3. Navigate to Commander Eval, search a legendary creature, click "Analyze"

---

## 11. Test Scenarios Summary

### High-priority
1. **Search**: Combine multiple filters (colors + mana value + rarity + format legality) → verify intersection
2. **Similarity**: Default tuning produces relevant results; changing strictness broadens results
3. **Commander Eval**: Full pipeline from search → analyze → deep dive → find similar → save → export → reload
4. **Database ingest**: Upload compressed database, verify card count updates
5. **Side panel**: Open from search results, close with Escape, verify back button behavior

### Medium-priority
1. **Color rules**: at_least / exact / at_most produce correct card sets
2. **Edge cards**: Cards with no oracle text, no Scryfall ID, planeswalkers, double-faced cards, meld cards
3. **Similarity recursion**: Click ⇄ on a similar card, verify new base card with preserved tuning
4. **Export**: Both similarity export and Commander Eval export produce working offline HTML
5. **MTG filter**: Toggle on/off → verify results change for cards with common oracle terms
6. **Drag-and-drop**: Drop from Scryfall URL, HTML, and local file → all resolve correctly

### Low-priority
1. **Very large result sets** → pagination performance
2. **Server restart while cached analysis exists** → cache clears cleanly
3. **Concurrent similarity requests** → no errors
4. **Special characters in search** → escaped properly
5. **Config persistence** across browser restarts (session cookie)

---

## 12. Known Limitations & QA Risk Areas

- **No test suite** — the application has no automated tests; all verification is manual
- **`debug=True` hardcoded** — Flask runs with Werkzeug debugger/reloader enabled, not production-safe
- **In-memory state** — `_eval_cache` and IDF cache are lost on server restart; `_eval_cache` is unbounded (grows for session lifetime) and not thread-safe across workers
- **`/config/ingest` replaces the live DB** via `shutil.move` — destructive operation with no backup/rollback mechanism (validates card count ≥1000 but doesn't preserve the prior DB)
- **Single SQLite connection** — read-only, but no connection pooling
- **No authentication** — the app is designed for local/trusted-network use only
- **SearXNG dependency** — Commander Eval web research fails silently if SearXNG is unavailable
- **No HTTPS** — Flask dev server, HTTP only; use a reverse proxy for production
- **README is stale** — documents non-existent `set.html`/`sets.html` templates and omits Commander Eval, LLM integration, config page, and DB ingest entirely
- **UI gaps in search filters** — `is_rebalanced` boolean flag works in backend but has no checkbox in search.html; `gold` and `yellow` border values work in backend but not exposed in the UI dropdown; `C` (colorless) is supported by the color filter backend but has no chip in the UI color picker (W, U, B, R, G only)
- **Similarity performance** — per-request O(candidates) Python scoring iteration; candidate pool is the full card set within ±3 MV of the base card
- **External CDN dependencies** — Scryfall (card images) and jsDelivr (mana-font CSS) must be reachable from the client browser
- **Orphaned `q` parameter** — the backend supports a free-text `q` search across name/type/oracle, but no HTML input in either `index.html` or `search.html` exposes it; the parameter can only be used by hand-editing the URL
