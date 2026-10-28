# Additional Bugs Backlog

**Source:** deep dive of the MTG Search application, 2026-10-27
**Status:** implemented for the selected scope; B-12/B-21 notes remain
**Scope:** the "additional bugs worth queuing" list from the deep dive. The
highest-impact findings (configuration cookie/key handling, MCP restart, ingest
atomicity, SSRF, path traversal, stored XSS, filename collisions, fresh-deck
outline, and the "All Printings" data loss) are tracked separately as the
high-impact fix set.

**Update (second pass):** fixed B-01, B-02, B-03, B-04, B-05, B-06, B-07,
B-08, B-09, B-10, B-11, B-14, B-15, B-16, B-17, B-18, B-19, B-20, B-22, and
B-23. B-12 has shape guards for the main stores and remains a low-priority
audit of untouched edge routes. B-21 is fixed for the chosen scope (CSRF
tokens, loopback defaults, upload cap, restricted MCP CORS); a real login/auth
gate remains intentionally out of scope. B-13 was withdrawn after verification.

Priority is **impact × likelihood**, not effort.

| ID | Severity | Area | Summary |
|----|----------|------|---------|
| B-01 | Medium | Deck Builder / Text Import | "Replace" import wipes deck name, commander, sections, outline, guideline, and eval data |
| B-02 | Medium | Deck Builder / rules | Commander singleton rules are inconsistent and partly unenforced |
| B-03 | Medium | Deck Builder / rules | Colorless commanders are treated as "no commander" for color-identity checks |
| B-04 | Medium | Deck Builder / Commander | Card-panel "+ Add to Deck" fails for legendary creatures once a commander is set |
| B-05 | Medium | Deck Builder / Commander | Dragging or auto-completing a commander silently replaces the existing commander |
| B-06 | Medium | Card Templates | Guideline editor leaves the guideline grid hidden after Save/Cancel |
| B-07 | Medium | Deck Builder / UI | Hybrid and phyrexian mana symbols use invalid mana-font classes |
| B-08 | Medium | Similarity | Embed/legacy defaults and factor controls disagree; textless cards behave badly |
| B-09 | Medium | Pricing / autosave | Price batches over 200 silently fail; large decks can lose edits on tab hide/unload |
| B-10 | Medium | Embeddings | Index rebuild and search can race, producing `IndexError` or stale results |
| B-11 | Medium | Persistence | Deck/template/tag/history/report JSON writes are not crash-safe |
| B-12 | Medium | API robustness | Several routes assume a specific JSON shape and 500 on malformed stores |
| B-13 | — | Search page | Withdrawn after verification: the spinner lookup runs on submit, not at script load |
| B-14 | Low | Deck Builder / URL | Double-decoding the `deck` query parameter breaks names containing `%` |
| B-15 | Low | Deck Builder / export | Export and Clear refuse decks whose cards live only in custom sections |
| B-16 | Low | Deck Builder / quick add | Quick-add can submit a previously selected card after the input text changes |
| B-17 | Low | Deck Builder / tags | Tag filter chips toggle but never filter; counts go stale |
| B-18 | Low | Image proxy | Short Scryfall IDs raise `IndexError` and return 500 |
| B-19 | Low | History | Naive timestamps can raise `TypeError` in the history-check endpoint |
| B-20 | Fixed | Similarity | Removed the misleading `unique=prints` / All Printings UI; single latest printing is explicit |
| B-21 | Ops | Security posture | No auth/CSRF, wide bind addresses, permissive MCP CORS, no upload cap |
| B-22 | Ops | Docker entrypoint | `set -e` + `wait -n` exits instead of restarting a crashed child |
| B-23 | Dev | Tests/docs | Root `pytest` collection fails; PROJECT_OVERVIEW still says three MCP tools |

---

## B-01 — Text Import "Replace" wipes deck identity

**Evidence:** `static/deck-builder.js:4240-4242` calls `deck.clear()` when
"Replace current deck contents" is checked. `Deck.clear()` (`:381-391`) resets
`name`, `commander`, `cards`, `evalData`, `sections`, `outline`, `guideline`,
`pricingStore`, and `pricingSections`.

**Impact:** a user replacing only the card list loses the deck name, commander,
sections, outline, guideline, and cached eval analysis. Because `name` is now
empty, `saveCurrentDeck()` refuses to save, so the import is not persisted and
looks like it silently failed.

**Additional parser gaps:** `:4189-4198` accepts only `^(\d+)\s+(.+)$` or a
bare card name. It does not accept common list formats:

- `1x Sol Ring` (quantity suffix `x`)
- `1 Sol Ring (LTC) 123` (set code / collector number suffix)
- `Sideboard` / `Commander` / `Deck` section headers
- comments and blank section markers

`mtg/blueprints/decks.py:247-265` looks up an exact full name only. A DFC's
front-face name (`Cecil, Dark Knight`) does not resolve to
`Cecil, Dark Knight // Cecil, Redeemed Paladin`.

**Implemented:** replace mode now calls `clearContents()`: it clears `cards`,
`commander`, `sections`, and `evalData` but preserves `name`, `outline`,
`guideline`, `pricingStore`, and `pricingSections`. The parser accepts `Nx`,
strips `(SET) number` suffixes, skips comments/section headers, and the name
lookup falls back to a DFC front-face name. The old behavior wiped the deck name
and stopped autosave entirely.

**Acceptance tests:** replace import keeps the deck name and commander; imports
`1x`, `(SET) 123`, comments, and section headers; DFC front-face import works.

---

## B-02 — Commander singleton rules are inconsistent and partly unenforced

**Evidence:**

- `static/deck-builder.js:176-199` (`_addToMain`) enforces a 4-copy limit. A
  Commander deck is singleton (basic lands and explicit "any number" cards are
  the exceptions).
- `Deck.setQuantity` (`:361-373`) bypasses the 4-copy limit entirely, so the
  `+` button can create 10 copies of a non-basic card.
- `Deck.addWithQuantity` (`:336-359`) bypasses the 4-copy limit by design and
  is used for text/template imports.
- `mtg/blueprints/decks.py:226-236` (`/api/deck/add-card`) enforces no 100-card,
  singleton, copy-count, or commander-legality rule. The Search/Similarity
  right-click flow can push a saved deck over 100.
- `Deck.addCard` (`:157-174`) classifies any legendary creature as the
  commander, but `addCardTo99` exists specifically to force a legendary
  creature into the 99. Callers are inconsistent about which one they use
  (see B-04).

**Impact:** a legal-looking deck can contain four copies of a card, or 120
cards, and the UI only warns about quantities >1. The server accepts whatever
the client sends.

**Implemented:** `isSingletonExempt(card)` now gates `_addToMain`,
`addWithQuantity`, `setQuantity`, and section-to-main moves; the server's
`_singleton_exempt` enforces the same rule and the 100-card cap in
`/api/deck/add-card`. Basic lands and explicit "any number" cards remain
exempt. Tests cover the client and server paths.

**Acceptance tests:** 2nd copy of a non-basic is rejected; basic lands are
allowed; 101st main-deck card is rejected; server `add-card` respects the same
limits; `+` cannot bypass the limit.

---

## B-03 — Colorless commanders are treated as "no commander"

**Evidence:** `isOffColorCard` (`static/deck-builder.js:1697-1709`) starts with:

```js
if (!ci || !card.colorIdentity) return false;
```

`getCommanderCI()` returns `(this.commander && this.commander.colorIdentity) || ''`.
A colorless commander such as a colorless legendary artifact creature has an
empty color identity, so `ci` is `''` and every colored card is treated as
legal. `renderCIStatus` (`:1811-1827`) also says "Set a commander to check color
identity" whenever `ci` is empty, even when a commander is actually set.

**Impact:** no off-color warning for colorless commanders, and a misleading
"set a commander" message.

**Proposed fix:** distinguish "no commander" from "commander with empty CI" by
checking `deck.commander` directly. If a commander is set, a non-empty card CI
is off-color when the commander CI is empty.

**Acceptance tests:** colorless commander + colored card is flagged; colorless
commander + colorless card is legal; no commander still produces the prompt.

---

## B-04 — Card-panel "+ Add to Deck" fails for legendary creatures after a commander is set

**Evidence:** `enableCardPanelAddToDeck` (`static/deck-builder.js:3313-3361`)
calls `addCardToDeck(card)` (`:3348`) without the `to99` flag. `addCardToDeck`
(`:2320-2337`) then calls `deck.addCard`, which sees `isLegendary && isCreature`
and, because a commander already exists, returns:

> Commander slot already filled. Clear it first to change commander.

The search/similarity drag path uses `addCardTo99`, and quick-add does too, so
the behavior is inconsistent.

**Impact:** after setting a commander, the detail-panel button cannot add any
other legendary creature to the 99.

**Proposed fix:** the card-panel button should default to `to99 = true` (it is
never the commander slot), or `addCard` should fall back to the 99 when the
commander slot is occupied and the card is not the current commander.

**Acceptance tests:** panel add of a second legendary creature goes to the 99;
panel add of the current commander is handled without duplicating the commander.

---

## B-05 — Commander replacement is silent in two paths

**Evidence:**

- `finishCopy` (`static/deck-builder.js:2892-2903`) does
  `deck.commander = card` without moving the old commander into the main deck
  or a section.
- `initCommanderAutocomplete`'s `onSelect` (`:2430-2447`) does the same:
  `deck.commander = fullCard; renderAll();`.
- `finishCommanderMove` (`:2861-2890`) does handle the swap correctly, which
  highlights the inconsistency.

**Impact:** dragging a legendary card onto the commander slot, or choosing one
from the commander autocomplete while a commander exists, silently deletes the
old commander from the deck.

**Proposed fix:** route all commander changes through one helper that validates
legendary/creature status, moves the old commander to the main deck or the
source section, and records history. Confirm with the user when replacing.

**Acceptance tests:** replacing via drop and autocomplete keeps the old
commander in the 99 or source section; history records both events.

---

## B-06 — Guideline editor leaves the guideline grid hidden

**Evidence:** `showGuidelineEditor` (`templates/card_templates.html:909-945`)
sets `gridsGuidelines.hidden = true` and `guidelineEditor.hidden = false`.
`hideGuidelineEditor` (`:947-952`) hides the editor and then calls:

```js
switchMode(currentMode);
```

`switchMode` (`:853-875`) starts with `if (mode === currentMode) return;`, and
`currentMode` is still `'guidelines'`, so it returns immediately. The grid stays
hidden, leaving a blank panel after Save or Cancel.

**Impact:** after saving or cancelling a guideline, the guideline list vanishes
until the user switches to Card Templates and back.

**Proposed fix:** in `hideGuidelineEditor`, explicitly set
`gridsGuidelines.hidden = false` (and hide the editor) instead of relying on
`switchMode`, or make `switchMode` support a force/refresh mode.

**Acceptance tests:** Save and Cancel each restore the guideline grid.

---

## B-07 — Hybrid/phyrexian mana symbols use invalid mana-font classes

**Evidence:** `renderManaSymbols` (`static/deck-builder.js:2145-2163`):

- `{W/U}` → `ms-w-u` (mana-font defines `ms-wu`)
- `{2/W}` → `ms-2-w` (defines `ms-2w`)
- `{W/P}` → `ms-w-p` (defines `ms-wp`)

The 1.18.0 stylesheet has `.ms-wu`, `.ms-2w`, and `.ms-wp`; the classes emitted
by the deck builder have no glyphs. `tests/test_deck_builder.py:1165-1172`
currently asserts the broken output and comments that phyrexian "hits the
slash branch first".

**Impact:** hybrid and phyrexian costs render as blank/empty spaces in the deck
list and stats, unlike the server-rendered `render_mana` helper, which maps them
correctly (`mtg/images.py:60-80`).

**Proposed fix:** normalize the symbol before building the class:
`W/U -> wu`, `2/W -> 2w`, `W/P -> wp`, etc. Update the test to the correct
mana-font classes.

**Acceptance tests:** the three hybrid/phyrexian examples match the classes in
the mana-font stylesheet; server and client output agree.

---

## B-08 — Similarity defaults and factor controls disagree

**Evidence:**

- `mtg/blueprints/config.py:91` displays `embed` as the default similarity
  method. `mtg/blueprints/similar.py:192` falls back to `legacy` when the
  session has no value. Users see one default in config and get the other in
  the tool until they save the config.
- In embed mode (`similar.py:202-247`), only `use_color` (hard subset) and
  `use_types` (non-empty intersection) are applied. `use_keywords`,
  `use_subtypes`, `use_supertypes`, `use_mv`, strictness, weights, and
  `mtg_filter` are ignored. `templates/similar.html:60-64` says "Other factor
  tuning is not applicable", but the first-visit redirect (`similar.py:83-97`)
  still sets all factor flags, so the type filter is silently active.
- `embed.find` returns `[]` for an empty oracle text (`mtg/embed.py:172-173`).
  The similar page then shows nothing in embed mode. In legacy mode, the
  multiplicative score is zero and the page can display many 0-score cards
  labelled "Ignore". There are 364 English front-face cards with empty text in
  the current DB.
- If embed scoring raises and the code falls back to legacy, `method` is not
  updated, so the template still hides the legacy tuning controls.

**Impact:** the same card can produce different results depending on whether
the user has saved the config; some visible controls do nothing; textless cards
have no useful similarity path.

**Implemented:** embed is now the default in both the config page and the
engine. Embed mode exposes only the controls it applies — color identity is
always a hard filter and shared type is an optional checkbox. Textless cards
(and an empty/failed embedding result) fall back to the legacy attribute path;
`score_similarity` treats a missing oracle signal as neutral instead of zero.
`method` is updated after a fallback so the legacy controls render.

**Acceptance tests:** default method matches config; embed mode does not apply
hidden filters; textless card has a documented behavior; fallback updates the
UI.

---

## B-09 — Pricing batch and autosave reliability

**Evidence:**

- `fetchPrices` (`static/deck-builder.js:946-990`) collects every unique
  Scryfall ID and sends one request. `mtg/blueprints/pricing.py:107-108` rejects
  more than 200 IDs with a 400. The client checks only `data.success &&
  data.prices`; a 400 response leaves prices empty and shows no error.
- The server fetches IDs sequentially (`pricing.py:110-116`). A batch of 200
  with three 10-second retries can take many minutes.
- `saveDeckToServer` uses `navigator.sendBeacon` on `beforeunload` and
  `visibilitychange` (`deck-builder.js:670-673`, `:4591-4608`). Beacon payloads
  are typically capped around 64 KB, and its boolean return value is ignored.
  The Cloud deck JSON in this repo is 131 KB, so a pending save can be dropped.
  The visibility handler also clears the debounce timer before trying the
  beacon, so the fetch fallback never runs.

**Impact:** large decks do not get prices, and edits made just before a tab
switch can be lost.

**Proposed fix:** chunk price IDs into batches (e.g. 50), surface batch errors,
and cache negative results. For saves, either keep the debounce timer until the
beacon is known to be accepted or use `fetch(..., { keepalive: true })` with a
payload-size check. Do not clear the pending save before a successful beacon.

**Acceptance tests:** >200 IDs are split and all prices load; a 400/failed batch
shows an error; a 131 KB deck is not dropped on tab hide; a debounced save still
happens if the beacon returns false.

---

## B-10 — Embedding index rebuild/search race

**Evidence:** `mtg/embed.py:143-150` writes `vectors.npy` and `meta.json` as two
separate non-atomic writes. `_load_index` (`:213-223`) loads them without a lock
and without checking that `len(vectors) == len(meta)`. `find` (`:167-210`) then
indexes `meta[i]` for `numpy` indices. A rebuild during a similarity/eval
request can load a new vector file with an old metadata file, or a partially
written file, and raise `IndexError`/`ValueError`. `build` acquires
`_build_lock` before `os.makedirs`/`open(LOCK_FILE)`, so an I/O error at that
point can leave the lock held forever (`:98-106`).

**Impact:** intermittent 500s from Similarity/Commander Eval and MCP
`semantic_search`; a failed build can block all future rebuilds until restart.

**Implemented:** vectors and metadata are written to temporary files and
replaced under `_index_lock`; `_load_index` takes the same lock and rejects
length-mismatched pairs. The build lock is acquired/released in an outer
`try/finally`, so an I/O failure cannot lock future builds.

**Acceptance tests:** a build concurrent with repeated `find` calls never
raises; vector/meta mismatch is rejected with a clear status; lock is released
on an injected I/O failure.

---

## B-11 — JSON stores are not crash-safe

**Evidence:** direct `open(path, 'w') + json.dump(...)` writes:

- decks: `mtg/blueprints/decks.py:61-62`, `:239-241`
- templates: `mtg/blueprints/templates_bp.py:28-29`
- tags: `mtg/blueprints/tags.py:45-46`
- history: `mtg/blueprints/decks.py:410-411`
- eval reports: `mtg/blueprints/eval.py:782-789`
- autosaved deepdives: `mtg/eval_helpers.py:216-224`

A crash or power loss during a write can truncate the file. Deck autosave runs
on a 600 ms debounce; history rewrites the whole array on each flush. The
read-modify-write history and `/api/deck/add-card` paths are also unsynchronized,
so two tabs can lose each other's changes.

**Impact:** a single unlucky write can delete a deck, template, tag catalog, or
saved report. A poisoned autosave file can also 500 the eval page because
`load_auto_deepdives` assumes the JSON is a dict (`eval_helpers.py:233-237`).

**Implemented:** `mtg/storage.write_json_atomic` (temp + fsync + `os.replace`)
is used by decks, templates, tags, history, eval reports, pricing cache, prompt
store, and deepdive autosaves. `file_lock` (fcntl `flock`) wraps deck add/save/
delete, template save/delete, history append/delete, tags, and deepdive
autosave read-modify-write paths.

**Acceptance tests:** simulate a write failure and confirm the old file is
intact; concurrent append operations do not lose events; corrupt autosave is
ignored with a warning.

---

## B-12 — API routes assume a specific JSON shape

**Evidence:**

- `mtg/blueprints/decks.py:54-55` (`/api/deck/save`) calls `body.get` after
  `request.get_json`; a JSON array/string raises `AttributeError`.
- `mtg/shared.py:196-203` (`_list_json_files`) returns whatever JSON parses to,
  including lists/strings. `saved_decks.html` and `mtg/mcp_server.py:402-465`
  assume objects. A malformed file can 500 Saved Decks or crash the MCP tool.
- `mtg/blueprints/templates_bp.py:41` calls `t.get` on every template.
- `mtg/blueprints/decks.py:405-411` assumes the history file is a list;
  `:405-409` `.extend` on a dict raises.
- `mtg/blueprints/decks.py:459-473` assumes every history event is a dict and
  every timestamp is timezone-aware; a string event or naive timestamp raises
  an uncaught `AttributeError`/`TypeError`.
- `mtg/blueprints/pricing.py:55-65` assumes a cache entry is a dict; a corrupt
  cache file can 500 that card's price fetch.
- `mtg/blueprints/eval_landing.py:78-89` assumes the report `card` is a dict.

**Impact:** any hand-edited, partially written, or older-shape file can break
the whole listing page or an MCP tool rather than being skipped.

**Proposed fix:** add small shape guards (`isinstance(x, dict)`, `isinstance(x,
list)`, `isinstance(ts, str)`) and skip/quarantine invalid entries. Return a
clear 400 for non-object JSON bodies.

**Acceptance tests:** each store with one malformed file still lists the valid
entries; malformed POST bodies return 400; history with a naive timestamp is
skipped.

---

## B-13 — Withdrawn: search loading spinner

Initially suspected as a null-reference because `#search-loading` is declared
after the inline script (`templates/search.html:380-382` vs `:391-394`).
Re-checked: the `getElementById` call is inside the `submit` callback, not at
script load, so the element exists when the handler runs. The spinner may still
be too brief to see before navigation, but this is not a confirmed defect. No
code change queued.

---

## B-14 — Double-decoding deck names containing `%`

**Evidence:** `saved_decks.html:16` generates
`/deck-builder?deck={{ deck.name | urlencode }}`; the browser's
`URLSearchParams.get('deck')` already percent-decodes. `init()`
(`static/deck-builder.js:4612-4616`) calls `decodeURIComponent(deckName)` a
second time. A deck named `100% Win` produces `100%25%20Win`, `get` returns
`100% off`/`100% Win`, and `decodeURIComponent` throws `URIError`. The callback
then aborts before `loadSavedDeckByName`, leaving a blank deck builder.

**Impact:** decks whose display names contain a literal `%` cannot be opened
from Saved Decks. The same double-decode can corrupt names containing a
percent-escape-like sequence.

**Proposed fix:** remove the extra `decodeURIComponent`; `URLSearchParams` is
already decoded. Add a test with `%`, `+`, `#`, and `&` in the name.

**Acceptance tests:** those four names load from Saved Decks and from a direct
URL.

---

## B-15 — Export/Clear refuse section-only decks

**Evidence:** `static/deck-builder.js:3083-3091` (`exportText`) checks
`deck.getTotalCount() === 0`; `getTotalCount` (`:375-379`) counts only
`deck.cards` plus the commander. A deck with only custom-section cards has a
total of 0 and cannot be exported as text. `initButtons`' Clear handler
(`:3171-3184`) shows "Deck is already empty" for the same case and refuses to
clear it.

**Impact:** sideboard/maybeboard-only decks cannot be exported or cleared from
the header controls.

**Proposed fix:** include section counts in the emptiness check, and have
`toTextExport` emit sections even when the main deck is empty (it already
does). Clear should remove sections too, with confirmation.

**Acceptance tests:** a section-only deck exports and clears.

---

## B-16 — Quick-add can submit a stale card

**Evidence:** `initQuickAdd` (`static/deck-builder.js:3504-3529`) stores the
selected card in `_quickAddSelected` on autocomplete selection. The input's
`input` event does not clear it. If the user selects Sol Ring, then edits the
text to "Arcane Signet" without selecting a suggestion, clicking Add still
adds Sol Ring.

**Impact:** wrong card added; confusing because the input text says otherwise.

**Proposed fix:** clear `_quickAddSelected` on any input change that is not a
selection, or resolve the current text by name on Add.

**Acceptance tests:** editing after a selection and pressing Add does not add
the old card; selecting and adding still works.

---

## B-17 — Tag filter chips never filter

**Evidence:** `renderTagFilterBar` (`static/deck-builder.js:1128-1163`) builds
chips and toggles an `active` class (`:1157-1162`), then calls `renderSections`.
`renderSections` (`:1246-1345`) never reads the active chips, and `renderAll`
does not call `renderTagFilterBar`, so counts also go stale after add/remove.
`ARCHITECTURE.md:305` already lists this as known dead/stale JS.

**Impact:** a visible filter control does nothing. Low because the feature is
known incomplete, but it is user-facing.

**Proposed fix:** either implement filtering (read `#tag-filter-bar
.tag-filter-chip.active` and filter `deck.cards` by tag) and refresh counts in
`renderAll`, or remove the bar until it is implemented.

**Acceptance tests:** selecting a chip filters the main list; counts update
after add/remove; clearing the chip restores the list.

---

## B-18 — Short Scryfall IDs return 500 from the image proxy

**Evidence:** `mtg/images.py:34` does `c1, c2 = scryfall_id[0],
scryfall_id[1]` without validating length. Verified:
`GET /img/normal/front/a/b/a.jpg` → `IndexError` → 500. The route
(`search.py:453-468`) accepts any string before `.jpg`.

**Impact:** log spam and an avoidable 500; also a hint that the route needs
input validation (see the high-impact path-traversal fix).

**Proposed fix:** validate the full UUID shape before using any character
indices; return 404 for invalid IDs. This should be fixed together with the
path-traversal validation.

**Acceptance tests:** short, empty, and non-UUID IDs return 404; valid UUIDs
still fetch/cache.

---

## B-19 — Naive history timestamps can 500 the check endpoint

**Evidence:** `mtg/blueprints/decks.py:463-473` compares
`datetime.now(timezone.utc)` with `datetime.fromisoformat(...)`. The client
writes ISO strings with `Z` (`recordHistoryEvent`, `deck-builder.js:62-77`),
which the current Python handles, but a manually imported/older history event
without an offset yields a naive datetime and raises `TypeError`. The
`except` clause catches only `JSONDecodeError`, `OSError`, and `ValueError`,
not `TypeError`.

**Impact:** one bad event can break the history reminder endpoint for all decks.

**Proposed fix:** normalize timestamps (assume UTC when naive) and catch
`TypeError`/`AttributeError`; skip invalid events.

**Acceptance tests:** naive, invalid, and future timestamps are handled without
a 500.

---

## B-20 — `unique=prints` is misleading

**Evidence:** the search UI offers "Display: All Printings"
(`templates/search.html:177-180`), and `search.py:335-359` groups by `uuid` for
`unique=prints`. But the ingest pipeline deliberately keeps only the latest
printing per card (`mtg/dedup.py:25-51`), so the current DB has one row for
Lightning Bolt (`SLZ/184`) despite its `printings` field listing 48 sets.
`?name=Lightning%20Bolt&unique=prints` returns "1 printing found".

**Impact:** users are promised all printings but can only see one. This is a
data-model limitation rather than a rendering bug.

**Decision/implemented:** keep exactly one latest printing per card to avoid
the disk/performance cost of retaining every printing. The "All Printings"
search option and the card-detail printings list were removed; the UI now
exposes only the stored printing. Re-ingest still replaces only the DB files,
so `images/` entries remain in place.

**Acceptance tests:** `/search?unique=prints` behaves as a normal single-card
search and neither the search page nor card detail contains "All Printings".
The test suite covers both.

---

## B-21 — Security posture (systemic)

**Evidence:**

- No authentication or authorization on any Flask route (`app.py` +
  blueprints), including destructive deck/template/report deletes,
  `/config/ingest`, `/config/mcp-restart`, `/llm-models`, image/pricing
  proxies.
- Flask binds `0.0.0.0` (`app.py:118`); `docker-compose.yml:9-12` publishes
  5000/8765/8000 on all interfaces by default.
- MCP HTTP transports set `enable_dns_rebinding_protection=False` and
  `allow_origins=["*"]` (`mtg/mcp_server.py:490-510`), so any web page can
  read deck/card data from an exposed MCPO/MCP port.
- No CSRF tokens and no `MAX_CONTENT_LENGTH`; uploads/decks can be arbitrarily
  large. `.secret_key` is mode `0644`.
- API key and prompts live in a signed (not encrypted) cookie.

**Impact:** on a LAN or shared host, anyone can read decks, delete data,
replace the database, restart services, or trigger outbound requests. This is
the umbrella issue behind several individual findings.

**Partially fixed:** Flask/MCP now bind to `127.0.0.1` by default (Docker
publishes ports on loopback unless `BIND_ADDR=0.0.0.0`), `MAX_CONTENT_LENGTH` and
a decompressed-upload cap are in place, MCP CORS defaults to loopback origins,
and config/upload/restart forms reject cross-site browser origins. Still open:
a single-user auth gate for LAN mode, a CSRF token for non-browser clients, and
hardening of `.secret_key` permissions.

**Acceptance tests:** unauthenticated mutating requests return 401/403 in
LAN mode; localhost mode still works; CORS preflight from an unlisted origin is
rejected; oversized uploads return 413.

---

## B-22 — Docker entrypoint exits instead of restarting a crashed child

**Evidence:** `docker-entrypoint.sh:2` uses `set -e`; `:29-45` runs:

```bash
while true; do
    wait -n $mcp_pid $mcpo_pid $flask_pid
    dead=$?
    ...
```

I reproduced the shell behavior: when a child exits nonzero, `wait -n` returns
nonzero and `set -e` terminates the script before the `kill -0` restart checks.
The comment says crashed children are restarted, but only zero-exit children
reach that logic.

**Impact:** a crashed MCP/MCPO process does not get restarted by the loop; the
container may exit or be restarted by Docker’s policy instead, depending on
which child died.

**Proposed fix:** use `wait -n ... || true` and capture the status, or wrap
the wait in a conditional (`if ! wait -n ...; then ...; fi`). Keep `set -e` for
startup commands if desired.

**Acceptance tests:** kill each child with a nonzero exit and confirm the loop
restarts it; kill Flask and confirm full cleanup.

---

## B-23 — Test and documentation drift

**Evidence:**

- Running `pytest` from the repository root failed collection because
  `test_scripts/run_eval_test.py` imports `requests`, which is not in
  `requirements.txt` (the directory is gitignored but present locally).
  `pytest tests` passed 171 tests (now 189 after the regression tests).
- `PROJECT_OVERVIEW.md:276` said the MCP server exposed three tools; the current code
  exposes five (`semantic_search`, `keyword_search`, `get_card`, `list_decks`,
  `get_deck`).
- `ARCHITECTURE.md:305` already labels `renderTagFilterBar`, `showTagUI`,
  `STORAGE_PREFIX`, and `activePopoverCard` as known dead/stale.

**Impact:** developer friction and stale user documentation; not a runtime bug.

**Fixed:** added `pytest.ini` with `testpaths = tests` and updated the PROJECT_OVERVIEW
tool list. `pytest` from the root now passes. The tag filter bar is now wired
up; the other dead JS names remain cleanup opportunities.

**Acceptance tests:** a clean checkout can run `pytest` successfully; PROJECT_OVERVIEW
matches the MCP manifest.

---

## Suggested batching

1. **Data safety:** B-01, B-11, B-12, B-19 (with the high-impact atomic
   ingest and filename-collision fixes).
2. **Deck builder correctness:** B-02, B-03, B-04, B-05, B-14, B-15, B-16.
3. **Broken visible controls:** B-06, B-07, B-17.
4. **Engine/pricing reliability:** B-08, B-09, B-10, B-20.
5. **Ops/docs:** B-21, B-22, B-23.

Each batch should ship with the acceptance tests listed above. The high-impact
set should be fixed before or alongside batch 1 because several items share the
same helpers (`write_json_atomic`, UUID/path validation, shape guards).
