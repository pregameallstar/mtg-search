# QA Findings — MTG Search

**Date**: 2026-07-04
**Tester**: Automated QA agent (curl-based endpoint testing, server log inspection, database schema analysis, source code review)
**Application**: MTG Search — Flask web app for Magic: The Gathering card search
**Database**: AllPrintings.sqlite — 109,364 English cards, 34,618 unique searchable
**Server**: Flask dev server on `127.0.0.1:5000`, `debug=True`, SearXNG running on `localhost:8888`

---

## Summary

| Severity | Count |
|----------|-------|
| Critical | 1 |
| Moderate | 1 |
| Low / Design | 4 |
| Documentation | 1 |
| **Total issues** | **7** |

---

## Critical Issues

### BUG-1 [CRITICAL]: Commander Eval `analyze` crashes on cards with `keywords = NULL`

**Route**: `POST /card/<set_code>/<number>/eval/analyze`

**Error**: `TypeError: cannot unpack non-iterable NoneType object` → HTTP 500

**Reproduction**:
```bash
curl -X POST http://127.0.0.1:5000/card/SLD/1638/eval/analyze \
  -H "Content-Type: application/json" -d '{}'
# → {"error": "cannot unpack non-iterable NoneType object"}
```

**Root cause** (app.py ~line 1630): The search-label loop includes a conditional 2-tuple that evaluates to `None` when the card has no keywords:

```python
for search_label, query in [
    ("deck_guides", ...),
    ("strategy", ...),
    ("discussion", ...),
    ("unique_archetypes", ...),
    ("mechanics", f"mtg rules {keywords} ...") if keywords else None,
    #                                               ^^^^ BUG: None is not iterable
]:
```

When `keywords` is `None` (common — Lightning Bolt, vanilla creatures, simple artifacts), the list element is bare `None` rather than a 2-tuple. Python's tuple unpacking `search_label, query = None` raises `TypeError`.

**Impact**: Any card with `keywords IS NULL` in the database cannot be analyzed. Cards with keywords (e.g., Sarevok, "Menace") reach the API key check successfully.

**Fix**:
```python
*([("mechanics", f"mtg rules {keywords} ...")] if keywords else []),
```

---

## Moderate Issues

### BUG-2 [MODERATE]: Drag-and-drop broken on bare `/similar` route

**Route**: `POST /similar` → 405 Method Not Allowed

**Reproduction**:
```bash
curl -X POST http://127.0.0.1:5000/similar \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "scryfall_id=4f43c378-9e6a-4ece-9c24-5dc08c977746"
# → 405 Method Not Allowed
```

By contrast, `/cards/<set_code>/similar` accepts POST correctly:
```bash
curl -X POST http://127.0.0.1:5000/cards/dom/similar \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "scryfall_id=4f43c378-9e6a-4ece-9c24-5dc08c977746"
# → {"redirect": "/card/SLD/1638/similar"}  ✅
```

**Root cause** (app.py lines 726-727): The bare `/similar` decorator omits `methods`:
```python
@app.route("/similar")                                    # GET only
@app.route("/cards/<set_code>/similar", methods=["GET", "POST"])  # Both
```

The sidebar links to `/similar`, so users arriving via navigation cannot use drag-and-drop. Name-search GET works fine since it submits via the form's `method="get"`.

**Fix**: `@app.route("/similar", methods=["GET", "POST"])`

---

## Low / Design Issues

### Design-1: `color=C` exact/at_least returns empty results

The `C` (colorless) chip IS present in the UI color picker, but `exact` and `at_least` rules return 0 results:

| Rule | Result |
|------|--------|
| `color=C&color_rule=exact` | 0 cards found |
| `color=C&color_rule=at_least` | 0 cards found |
| `color=C&color_rule=at_most` | 4,791 cards found ✅ |

**Root cause**: MTGJSON stores colorless cards with `colors = ''` (empty string), not `'C'`. The exact rule matches against `'C'` which doesn't match `''`. `at_most` works because it excludes all WUBRG colors (which colorless cards don't contain).

**Recommendation**: Either (a) add special-case handling for `C → colors = ''` in the SQL builder, or (b) document that colorless cards are found via `at_most` with no colors selected.

### Design-2: Commander validity warnings hidden until analysis runs

The "not a legal Commander" warning (`commander_eval.html` line 100) and "Game Changer" warning (line 230) are both inside `{% if analysis %}` blocks. This means:

- User navigates to `/card/<set_code>/<number>/eval` for a non-legendary card
- Page loads normally with **no warning visible**
- User clicks "Analyze" and only THEN sees the warning

**Recommendation**: Move `is_commander` and `isGameChanger` checks outside the `{% if analysis %}` block so warnings are visible immediately on page load.

### Design-3: `_eval_cache` is unbounded with no eviction policy

The server-side `_eval_cache` dict (app.py) grows indefinitely during server process lifetime. No TTL, no max size, no eviction. Combined with no authentication, sustained requests could exhaust server memory.

**Recommendation**: Add a TTL (e.g., 1 hour) or max cache size with LRU eviction.

### Design-4: No eviction/backup on database ingest

`/config/ingest` replaces the live database via `shutil.move` with no backup of the previous database. The only safeguard is a minimum card-count check (≥1000 English cards). This is documented as a known limitation.

---

## Documentation Issue

### DOC-1: QA_HANDOFF.md contains several inaccuracies

The QA handoff document states several things that do not match the current template code. Verified by directly inspecting template files:

| Handoff Claim | Actual (verified in templates) |
|---------------|-------------------------------|
| `q` parameter is "orphaned" with no UI input | Present in both `index.html` (line 13) and `search.html` (line 11) |
| `is_rebalanced` "has no UI checkbox" | Present in `search.html` (line 128) |
| `gold` and `yellow` border "not exposed in the UI" | Present as `<option>` elements in `search.html` (lines 136-137) |
| `C` (colorless) "has no chip in the UI color picker" | Present in both color and CI pickers: `('C','Colorless')` |
| README references non-existent `set.html`/`sets.html` | **Confirmed** — these templates don't exist |

The handoff appears to have been written against an earlier version of the codebase and does not reflect the current state. The README (stale references) is the only accurate claim among the UI-gap assertions.

---

## Verified Working — Comprehensive Test Results

The following were systematically tested and confirmed working correctly:

### Search Filters (all 20+ dimensions)

| Filter | Test Input | Result | Status |
|--------|-----------|--------|--------|
| `name` | Lightning Bolt | 2 cards (unique) | ✅ |
| `oracle` | destroy target creature | 374 cards | ✅ |
| `type_line` | Legendary Creature | 3,608 cards | ✅ |
| `mana_cost` | `{1}{R}{G}` | 82 cards | ✅ |
| `keywords` (AND) | flying,trample | 127 cards | ✅ |
| `q` (free-text) | dragon | 702 cards (cf. 315 name-only) | ✅ |
| `color` exact | R | 5,058 cards | ✅ |
| `color` at_least | R+G | 7,181 cards | ✅ |
| `color` at_most | R | 9,848 cards | ✅ |
| `ci` exact | B+G | 5,036 cards | ✅ |
| `mv` = | 3 | 8,194 cards | ✅ |
| `mv` < | 3 | 12,414 cards | ✅ |
| `mv` > | 5 | 3,514 cards | ✅ |
| `mv` <= | 1 | 5,218 cards | ✅ |
| `mv` >= | 7 | 1,423 cards | ✅ |
| `mv` range | 1-3 | 18,672 cards | ✅ |
| `pow` >= | 4 | 4,729 cards | ✅ |
| `tou` <= | 2 | 8,942 cards | ✅ |
| `loy` > | 3 | 236 cards | ✅ |
| `rarity` multi | mythic | 2,873 cards | ✅ |
| `set` | WAR | 266 cards | ✅ |
| `format`+`legality` | commander legal | 31,685 cards | ✅ |
| `format`+`legality` | commander banned | 83 cards | ✅ |
| `format`+`legality` | vintage restricted | 52 cards | ✅ |
| `is_reprint` | 1 | 17,420 cards | ✅ |
| `is_reserved` | 1 | 571 cards | ✅ |
| `is_funny` | 1 | 1,339 cards | ✅ |
| `is_fullart` | 1 | 3,204 cards | ✅ |
| `is_promo` | 1 | 6,104 cards | ✅ |
| `is_rebalanced` | 1 | 217 cards | ✅ |
| `border` | black | 33,754 cards | ✅ |
| `border` | gold/yellow/borderless | all functional | ✅ |
| `layout` | transform/saga/split/flip/etc. | all functional | ✅ |
| `frame` | 2015 | 25,747 cards | ✅ |
| `unique` | cards vs prints | 2 unique vs 71 printings (Bolt) | ✅ |
| Combined complex | dragon+rare/mythic+cmdr+R+mv≥4 | 64 cards | ✅ |
| Empty search | no filters | 0 cards (expected behavior) | ✅ |

### Pagination

- Preserves all filter parameters in page links ✅
- Page 1 of 3 for 71 Lightning Bolt printings ✅
- `page=0`, `page=-1`, `page=abc` gracefully default to page 1 ✅

### Input Handling

- XSS: `<script>alert(1)</script>` in name → escaped as `&lt;script&gt;` ✅
- Special characters (apostrophes, percent signs) ✅
- Very long input (1000 chars) ✅
- SQL injection: all queries parameterized ✅
- Lowercase set codes uppercased ✅
- Invalid `mv`/`pow`/`tou`/`loy` values handled without crash ✅

### Card Detail

- Page renders with image, oracle text, legality table, printings ✅
- AJAX fragment (`?fragment=1`) returns panel body ✅
- Non-existent card → 404 ✅
- Double-faced cards, cards with ★ in number ✅
- Rulings display for cards that have them (e.g., Doubling Season, 5 rulings) ✅

### Card Similarity

- Landing page loads, name search via GET works ✅
- Drag-drop POST with valid scryfallId returns redirect JSON ✅
- Default tuning: all factors ON, strict mode ✅
- Strictness changes (strict→loose) broaden results ✅
- Score threshold filter works ✅
- Top N selector works ✅
- Export downloads self-contained HTML ✅
- Oracle-only mode (all gates off) scores 27K candidates ✅

### Commander Eval

- Landing page loads with saved reports ✅
- Autocomplete: 2+ chars returns JSON, <2 chars returns `[]` ✅
- Eval page for legendary creature renders correctly ✅
- Analyze with no API key: graceful error message ✅
- Find Similar commanders: returns results with progress ✅
- Expand/Save/Load/Delete report: all error-handling correct ✅
- Report loading via `?report=<filename>` works ✅
- SearXNG running and responding ✅

### Config & Infrastructure

- Config page loads with DB stats ✅
- LLM backend toggle, API key field, model selector ✅
- Database ingest UI present ✅
- All sidebar nav links return 200 ✅
- Static CSS served correctly ✅
- Session-based API key persists across page loads ✅

---

## Observations

1. **The existing QA_FINDINGS.md (prior version) contained several inaccurate claims** — `at_most` color logic works correctly (verified: exact W=5119, at_most W=9910 — different and correct), `is_rebalanced` is in the UI, `gold`/`yellow` border options are in the dropdown, `C` chip is in the color picker, `q` parameter has UI input on both home and search pages. This suggests those findings were written against an older version of the codebase.

2. **Server log shows prior 500 errors for `mv=abc`** (3 occurrences at 17:18) that are no longer reproducible — the `try/except (ValueError, TypeError): pass` guard around mana value parsing now catches these cleanly. These may have been from a previous version without the guard.

3. **SearXNG is functional** — web search returns real results from DuckDuckGo, Wikipedia, Startpage. Commander Eval's web research pipeline would work if an LLM API key were configured.

4. **`debug=True` is hardcoded** (line 2001) — Werkzeug debugger is enabled, exposing interactive tracebacks. Known limitation, not suitable for production.

5. **No test suite** — all verification is manual. Documented limitation.

6. **Vestigial `mtg.db`** — a 0-byte file in the project root with no apparent purpose.

---

## Recommendations

| Priority | Item |
|----------|------|
| **P0** | Fix BUG-1: Commander Eval crash on cards without keywords (NoneType unpacking) |
| **P1** | Fix BUG-2: Add `methods=["GET", "POST"]` to bare `/similar` route |
| **P2** | Update QA_HANDOFF.md to reflect current UI state (q, is_rebalanced, gold/yellow, C chip) |
| **P3** | Move Commander Eval warnings outside `{% if analysis %}` block |
| **P4** | Add special-case `C → colors = ''` handling in SQL builder for color filters |
| **P5** | Update README.md to remove `set.html`/`sets.html` references |
| **P6** | Add `_eval_cache` eviction (TTL or max size) |
| **P7** | Remove `debug=True` or gate behind env var |
| **P8** | Add regression test for cards with `keywords = NULL` in analyze pipeline |
| **P9** | Remove vestigial `mtg.db` (0 bytes) |
