# Similarity Improvements

Ideas for better functional similarity results. Roughly ranked by
effort-to-impact.

## 1. MTG-domain stop words filter → **IMPLEMENTED** 2026-07-02

Remove ubiquitous MTG terms from oracle text tokens: `creature`, `target`,
`player`, `battlefield`, `graveyard`, etc. These appear on so many cards they
drown out the signal words that actually describe what the card DOES.

A toggle (`mtg_filter`) lets the user enable/disable this on the similarity
page so they can choose the noise level.

## 2. Normalize the final score to [0,1] → **IMPLEMENTED** 2026-07-02

Oracle text is always 0–1, but enabling optional factors adds unbounded
weight on top. "90%" means different things depending on which toggles are
on. Divide by `1 + sum(active weights)` so the percentage is comparable
regardless of factor selection.

## 3. Supertypes factor → **IMPLEMENTED** 2026-07-02

Investigation showed that MTGJSON v5 already separates `types` (card types
only: Creature, Instant, Sorcery…) from `supertypes` (Legendary, Snow,
Basic, World). The original improvement was a no-op — the data was already
clean. Instead, added Supertypes as a new optional factor toggle. Two cards
sharing "Legendary" or "Snow" get a Jaccard boost.

## 4. TF-IDF on oracle terms → **IMPLEMENTED** 2026-07-02

Lazy-computed on first similarity request, cached at module level. Shared
terms weighted by inverse document frequency so "flying" (appears on 6% of
cards) counts more than common filler words. Base-card terms score as
IDF-weighted overlap: `sum(IDF shared) / sum(IDF base)`. Refreshes
automatically if the term count changes by more than 100.
