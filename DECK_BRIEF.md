# Deck Brief schema and rollout

The Deck Brief is the user's statement of intent for one deck. It is not a
second deck list and it does not replace the Deck Outline. It captures the
things the deterministic audit cannot infer and makes them actionable:

- what the deck is trying to do,
- how it intends to win,
- hard Rule 0 limits,
- cards that must stay,
- cards that must not be suggested.

## v1 schema

Stored on the deck as `deckBrief`:

```json
{
  "version": 1,
  "archetype": "aristocrats",
  "themes": ["sacrifice", "squirrels"],
  "win_plans": ["drain", "combat"],
  "constraints": {
    "Counterspell": 2,
    "Board Wipe": 2,
    "Tutor": 0,
    "Extra Turns": 0
  },
  "must_keep": ["Blood Artist", "Sol Ring"],
  "avoid": ["Armageddon"],
  "notes": "Rule 0: no infinite combos; keep the curve low."
}
```

`reviewNotes` remains the canonical free-form notes field for backward
compatibility and is mirrored by `deckBrief.notes`. A deck that only has
`reviewNotes` is read as an otherwise-empty v1 brief. In the page form,
`must_keep` and `avoid` are semicolon-separated (or one per line) because card
names frequently contain commas; `themes` accepts commas.

### Fields

| Field | Type | Deterministic effect |
|---|---|---|
| `version` | int | Schema version; currently `1`. |
| `archetype` | string | Included in the retrieval profile and LLM/verifier payload. |
| `themes` | string[] | Included in retrieval, role queries, and LLM/verifier payload. |
| `win_plans` | string[] | Declaring a non-`alt_win` plan suppresses the explicit `Win Condition` gap and credits the win-condition health component. `alt_win` keeps the explicit gap. |
| `constraints` | role → int | Hard maximum counts. Core roles lower the role target; cap-only roles (`Extra Turns`, `Mass Land Denial`) flag surplus without creating a "needs more" gap. |
| `must_keep` | string[] | Excluded from the deterministic cut shortlist and rejected by validation; also sent to the LLM and verifier. |
| `avoid` | string[] | Excluded from candidate adds and rejected by validation; also sent to the LLM and verifier. |
| `notes` | string | Free-form context for the LLM/verifier. Explicit numeric caps in notes are parsed as before and merged with `constraints` (tightest wins). |

Allowed `win_plans`: `combat`, `commander_damage`, `combo`, `drain`, `mill`,
`alt_win`, `other`.

Allowed `constraints` roles: `Lands`, `Ramp`, `Card Draw`, `Card Selection`,
`Targeted Removal`, `Board Wipe`, `Counterspell`, `Tutor`, `Protection`,
`Recursion`, `Win Condition`, `Extra Turns`, `Mass Land Denial`.

### Precedence

1. Structured `constraints`, `must_keep`, and `avoid`.
2. Explicit numeric caps parsed from `notes` (only when they are tighter).
3. `archetype`, `themes`, and `win_plans` for retrieval and LLM context.
4. `notes` as free-form context.
5. The Deck Outline and `evalData` as narrative/prefill context.

Outline text is no longer copied into `reviewNotes`: the outline is sent to
retrieval and the LLM independently.

## Where it is wired in

- `mtg/deck_brief.py` — schema normalization, migration, cap merging, retrieval
  text, prompt payloads, fingerprint shape.
- `mtg/deck_stats.py` — cap-aware targets, cap-only role surplus, declared
  win-plan suppression/health credit, `deckBrief` in the deck fingerprint.
- `mtg/deck_review.py` — structured retrieval profile, `avoid` filtering,
  `must_keep` cut filtering and validation, role reordering in embedding
  queries, structured LLM/verifier payloads, structured export.
- `mtg/blueprints/deck_review.py` — persistence, request parsing, structured
  page payloads, stale checks.
- `mtg/blueprints/deck_analysis.py`, `mtg/deck_versions.py`, `mtg/mcp_server.py`
  — the same brief-driven audit for the builder panel, comparisons, and MCP.
- `templates/deck_review.html` + `static/style.css` — the structured form;
  values are populated from the selected deck and restored from saved reports.
- `static/deck-builder.js` + `mtg/blueprints/decks.py` — the builder preserves
  the Deck Doctor-owned brief on whole-deck saves.

The brief is part of the deck fingerprint, so changing it marks saved reviews
stale. Saved reports also store the brief in their options for export and for
restoring the form. `POST /api/deck/review/run` accepts the object as
`briefData` (or `deckBrief`) alongside the free-form `brief` notes; the server
persists it and returns it in `options.deckBrief`.

## Rollout plan

### Shipped in this slice

- v1 schema, normalization, legacy migration.
- Archetype, themes, win plans, hard limits, must-keep, avoid, notes.
- Deterministic caps (including cap-only extra turns / mass land denial).
- Declared win-plan credit and explicit-wincon suppression.
- Retrieval and LLM/verifier payload wiring.
- Structured form and storage/preservation.
- Tests for schema, caps, win plans, cut/avoid protection, persistence, and
  page wiring.

### Next slices

1. **Target profiles by archetype.** Use `archetype` to adjust role targets
   (not just retrieval), with an explicit, testable table and bounds.
2. **Budget and collection.** Add `budget` (`owned_only`, `max_price`,
   `currency`) and apply it during candidate ranking/filtering; unknown prices
   must be handled explicitly.
3. **Power target in the brief.** Decide whether the target bracket belongs in
   the brief (and therefore the fingerprint) or remains a per-run option.
4. **Meta/playgroup constraints.** Structured fields for common social limits
   and a deterministic conflict report.
5. **Measurement.** Label a set of decks and measure cap compliance, must-keep
   preservation, candidate recall@k, and recommendation relevance before
   expanding the form further.

### Non-goals for v1

- Parsing arbitrary prose constraints into hard rules.
- Detecting combos or verifying declared win plans.
- Replacing the classifier, retrieval, or verifier.
- Asking the user for information already in the deck, outline, or eval data.
