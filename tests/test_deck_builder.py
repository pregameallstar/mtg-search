"""Execution tests for the Deck Builder's pure logic (static/deck-builder.js).

The deck builder is a single ~3,900-line ES5 IIFE with no module system, so the
pure functions and the `Deck` data model are normally only reachable through the
DOM. This harness loads the file into a QuickJS context, stubs the DOM/browser
globals with autovivifying Proxies, and neutralizes the auto-running `init()`
bootstrap so the IIFE body still defines every internal function but never
reaches for real DOM/fetch.

The pure functions are then exposed on a `__exposed` global and driven through a
JSON round-trip bridge (in: `json.dumps`, out: `JSON.stringify` + `json.loads`).

Why this matters: the goal is to refactor deck-builder.js (dedupe the two
tag-popover builders, remove the dead `STORAGE_PREFIX`/`renderTagFilterBar`
symbols, etc.) without breaking behavior. These execution tests pin the current
behavior of the logic most likely to move during that refactor, so a regression
surfaces as a failing assertion rather than a broken page.
"""

import json

import pytest

import quickjs

SRC_PATH = "static/deck-builder.js"


# --------------------------------------------------------------------------- #
# Harness — transform + stub globals + expose internals
# --------------------------------------------------------------------------- #

# The auto-run tail. We replace it so `init()` never executes, but every
# function declaration in the IIFE body is still defined.
_AUTORUN_TAIL = """    // Run on DOM ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

})();"""

# Replaces the tail: expose the pure logic on `globalThis.__exposed` (closure
# aware, so `deck`/`priceCache`/etc. resolve to the IIFE's own bindings).
_EXPOSE_JS = """    // ── test harness: expose pure logic (closure-aware) ──
    function __db_reset() {
        deck = new Deck();
        priceCache = {};
        globalPricingStore = 'usd';
        currentSort = 'name';
        historyBuffer.length = 0;
        cachedGuidelineTemplates = [];
        categorizedCards = {};
        // Re-bind the exposed slots to the IIFE's own (new) variables. Reaching
        // into the object literal here avoids the copy-on-assign aliasing that
        // primitive globals (priceCache, globalPricingStore) otherwise hit.
        __exposed.deck = deck;
        __exposed.priceCache = priceCache;
        __exposed.globalPricingStore = globalPricingStore;
        __exposed.currentSort = currentSort;
        __exposed.getDeckPricingStore = getDeckPricingStore;
        __exposed.getCardPrice = getCardPrice;
        __exposed.computeDeckCost = computeDeckCost;
        __exposed.isOffColorCard = isOffColorCard;
        __exposed.sortCardsForDisplay = sortCardsForDisplay;
    }
    globalThis.__exposed = {
        parseTypes: parseTypes,
        classifyCard: classifyCard,
        Deck: Deck,
        getSectionKey: getSectionKey,
        sortCardsForDisplay: sortCardsForDisplay,
        shouldShowQuantityWarning: shouldShowQuantityWarning,
        isOffColorCard: isOffColorCard,
        renderManaSymbols: renderManaSymbols,
        escapeAttr: escapeAttr,
        getDeckPricingStore: getDeckPricingStore,
        getCardPrice: getCardPrice,
        formatPrice: formatPrice,
        computeDeckCost: computeDeckCost,
        _extractScryfallId: _extractScryfallId,
        _extractScryfallIdFromTransfer: _extractScryfallIdFromTransfer,
        deck: deck,
        priceCache: priceCache,
        globalPricingStore: globalPricingStore,
        currentSort: currentSort,
        // Setters that mutate the IIFE's own closure variables (a plain property
        // assignment on __exposed would only change the exposed slot, not the
        // closure the functions actually read).
        _setSort: function (s) { currentSort = s; },
        _setGlobalStore: function (s) { globalPricingStore = s; },
        _reset: __db_reset
    };

})();"""

# Browser globals the IIFE references at load time (window._goSearchPage,
# window._goSimilarPage assignments; `$`/`$$` are function declarations so they
# only touch document when *called*). A Proxy whose every property is itself
# lets any `window.x.y.z = ...` or `document.createElement` no-op safely.
_GLOBALS_JS = """
function __viv() {
    var f = function () { return __viv(); };
    return new Proxy(f, {
        get: function (t, k) {
            if (k === Symbol.toPrimitive) return function () { return ''; };
            if (k === 'toString') return function () { return '[stub]'; };
            return __viv();
        },
        set: function (t, k, v) { return true; },
        apply: function () { return __viv(); },
        construct: function () { return __viv(); }
    });
}
var window = __viv();
var document = __viv();
var navigator = __viv();
var localStorage = __viv();
var fetch = function () { return Promise.resolve({ json: function () { return Promise.resolve({}); }, ok: true }); };
var XMLHttpRequest = function () { this.status = 0; this.responseText = ''; this.open = function () {}; this.send = function () {}; };
var Blob = function () {};
var MutationObserver = function () { this.observe = function () {}; this.disconnect = function () {}; };
var URLSearchParams = function () { this.get = function () { return null; }; };
var setTimeout = function () { return 0; };
var clearTimeout = function () {};
"""


def _transformed_source():
    src = open(SRC_PATH).read()
    assert _AUTORUN_TAIL in src, "auto-run tail moved — update the harness transform"
    return src.replace(_AUTORUN_TAIL, _EXPOSE_JS)


class JSBridge:
    """Wraps a QuickJS context with deck-builder loaded, exposing `run`.

    `run(expr)` evaluates an arbitrary JS expression and returns its value as a
    Python object via a JSON.stringify round-trip. `reset()` restores the IIFE's
    singleton state (deck, priceCache, sort) between tests.
    """

    def __init__(self):
        self.ctx = quickjs.Context()
        self.ctx.eval(_GLOBALS_JS)
        self.ctx.eval(_transformed_source())

    def run(self, expr):
        """Evaluate `expr`, return its JSON round-trip as Python.

        `undefined` (e.g. a void function) round-trips to None, same as null.
        """
        raw = self.ctx.eval("JSON.stringify(" + expr + ")")
        return json.loads(raw) if raw is not None else None

    def eval(self, expr):
        """Evaluate `expr` for side effects only."""
        self.ctx.eval(expr)

    def reset(self):
        self.eval("__exposed._reset();")


@pytest.fixture(scope="session")
def js():
    bridge = JSBridge()
    yield bridge


@pytest.fixture(autouse=True)
def _fresh_state(js):
    """Every test starts from clean singleton state (deck/priceCache/sort)."""
    js.reset()
    yield


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def card(**over):
    """A minimally-shaped card object for the Deck model / classification."""
    base = {
        "uuid": "u1",
        "setCode": "SET",
        "number": "1",
        "name": "Card",
        "types": "Creature",
        "type": "Creature",  # full type line (distinct from comma-separated `types`)
        "supertypes": "",
        "subtypes": "",
        "text": "",
        "colors": "",
        "colorIdentity": "",
        "manaCost": "",
        "manaValue": 3,
        "quantity": 1,
        "scryfallId": "",
        "tags": [],
        "imageUrl": "",
    }
    base.update(over)
    return base


def js_card(c, **over):
    """Serialize a Python card dict into a JS object literal (drops null-free)."""
    merged = dict(c)
    merged.update(over)
    return json.dumps(merged)


# --------------------------------------------------------------------------- #
# parseTypes
# --------------------------------------------------------------------------- #

def test_parseTypes_splits_trims_lowercases(js):
    r = js.run("__exposed.parseTypes(' Creature, Land , Instant ')")
    assert r == ["creature", "land", "instant"]


def test_parseTypes_handles_empty_and_whitespace(js):
    assert js.run("__exposed.parseTypes('')") == []
    assert js.run("__exposed.parseTypes('  ,  , ')") == []
    assert js.run("__exposed.parseTypes(null)") == []
    assert js.run("__exposed.parseTypes(undefined)") == []


def test_parseTypes_no_dedup_preserves_order(js):
    r = js.run("__exposed.parseTypes('Creature, Creature, Land')")
    assert r == ["creature", "creature", "land"]


# --------------------------------------------------------------------------- #
# classifyCard
# --------------------------------------------------------------------------- #

def test_classify_tag_override_wins(js):
    r = js.run("__exposed.classifyCard(" + js_card(card(
        types="Creature", text="", tags=["Draw"])) + ")")
    assert r == "Draw"


def test_classify_tag_override_case_insensitive(js):
    r = js.run("__exposed.classifyCard(" + js_card(card(
        types="Creature", text="destroy target creature", tags=["removal"])) + ")")
    assert r == "Removal"


def test_classify_lands(js):
    assert js.run("__exposed.classifyCard(" + js_card(card(types="Land")) + ")") == "Lands"
    # Basic lands carry supertypes="Basic" but still types="Land" (the type line,
    # not the comma list). The classifier keys off `types` containing "land".
    assert js.run("__exposed.classifyCard(" + js_card(
        card(types="Land", supertypes="Basic")) + ")") == "Lands"


def test_classify_ramp_land_search(js):
    r = js.run("__exposed.classifyCard(" + js_card(card(
        types="Sorcery", text="Search your library for a land card")) + ")")
    assert r == "Ramp"


def test_classify_ramp_mana_rock(js):
    r = js.run("__exposed.classifyCard(" + js_card(card(
        types="Artifact", text="Add {C}{C} to your mana pool")) + ")")
    assert r == "Ramp"


def test_classify_ramp_mana_dork(js):
    r = js.run("__exposed.classifyCard(" + js_card(card(
        types="Creature", text="{T}: Add {G} to your mana pool")) + ")")
    assert r == "Ramp"


def test_classify_ramp_ritual(js):
    # The ritual rule matches `(instant|sorcery)` against the full type *line*
    # (`card.type`), so the card must carry `type: "Instant"`.
    r = js.run("__exposed.classifyCard(" + js_card(card(
        types="Instant", type="Instant", text="Add {R}{R}{R} to your mana pool")) + ")")
    assert r == "Ramp"


def test_classify_ramp_land_to_battlefield(js):
    r = js.run("__exposed.classifyCard(" + js_card(card(
        types="Sorcery", text="Put a land card onto the battlefield")) + ")")
    assert r == "Ramp"


def test_classify_ramp_treasure(js):
    r = js.run("__exposed.classifyCard(" + js_card(card(
        types="Instant", text="Create a Treasure token")) + ")")
    assert r == "Ramp"


def test_classify_removal_targeted_and_wipes(js):
    cases = [
        "Destroy target creature.",
        "Exile target permanent.",
        "Counter target spell.",
        "Destroy all creatures.",
        "Exile all graveyards.",
        "Target player sacrifices a creature.",
    ]
    for text in cases:
        assert js.run("__exposed.classifyCard(" + js_card(card(
            types="Instant", text=text)) + ")") == "Removal", text


def test_classify_removal_damage(js):
    r = js.run("__exposed.classifyCard(" + js_card(card(
        types="Sorcery", text="Deal 3 damage to target creature")) + ")")
    assert r == "Removal"


def test_classify_draw_plain(js):
    assert js.run("__exposed.classifyCard(" + js_card(card(
        types="Sorcery", text="Draw two cards")) + ")") == "Draw"


def test_classify_draw_hate_excluded(js):
    # "can't draw" / "draw step" / "draw more than" must NOT classify as Draw.
    assert js.run("__exposed.classifyCard(" + js_card(card(
        types="Enchantment", text="Players can't draw cards")) + ")") == "Core"
    assert js.run("__exposed.classifyCard(" + js_card(card(
        types="Enchantment", text="You draw an extra card in your draw step")) + ")") == "Core"
    assert js.run("__exposed.classifyCard(" + js_card(card(
        types="Enchantment", text="No player may draw more than one card")) + ")") == "Core"


def test_classify_loot_excluded(js):
    # Draw + discard (loot) must not count as pure card advantage.
    r = js.run("__exposed.classifyCard(" + js_card(card(
        types="Instant", text="Draw a card, then discard a card")) + ")")
    assert r == "Core"


def test_classify_core_fallback(js):
    r = js.run("__exposed.classifyCard(" + js_card(card(
        types="Creature", text="Vigilance, trample")) + ")")
    assert r == "Core"


# --------------------------------------------------------------------------- #
# Deck model — commander
# --------------------------------------------------------------------------- #

def test_deck_add_legendary_creature_as_commander(js):
    r = js.run("""(function () {
        var d = new __exposed.Deck();
        var res = d.addCard(%s);
        return { res: res, name: d.commander && d.commander.name, total: d.getTotalCount() };
    })()""" % js_card(card(uuid="u1", name="The General", isLegendary=True,
                           isCreature=True, colorIdentity="W")))
    assert r["res"]["isCommander"] is True
    assert r["name"] == "The General"
    assert r["total"] == 1


def test_deck_second_legendary_blocked(js):
    r = js.run("""(function () {
        var d = new __exposed.Deck();
        d.addCard(%s);
        var res = d.addCard(%s);
        return res;
    })()""" % (js_card(card(uuid="u1", isLegendary=True, isCreature=True)),
               js_card(card(uuid="u2", isLegendary=True, isCreature=True))))
    assert "error" in r and "Commander slot already filled" in r["error"]


def test_deck_readd_same_commander_ok(js):
    r = js.run("""(function () {
        var d = new __exposed.Deck();
        d.addCard(%s);
        var res = d.addCard(%s);
        return res;
    })()""" % (js_card(card(uuid="u1", isLegendary=True, isCreature=True)),
               js_card(card(uuid="u1", isLegendary=True, isCreature=True))))
    assert r.get("isCommander") is True


def test_deck_addCard_rejects_missing_uuid(js):
    r = js.run("""(function () {
        var d = new __exposed.Deck();
        return d.addCard({ name: 'No UUID' });
    })()""")
    assert r["error"] == "Invalid card data."


# --------------------------------------------------------------------------- #
# Deck model — main deck (99)
# --------------------------------------------------------------------------- #

def test_deck_add_regular_card(js):
    r = js.run("""(function () {
        var d = new __exposed.Deck();
        var res = d.addCard(%s);
        return { res: res, cards: d.cards.length, total: d.getTotalCount() };
    })()""" % js_card(card(uuid="u1", name="Creature One")))
    assert r["res"]["ok"] is True
    assert r["cards"] == 1
    assert r["total"] == 1


def test_deck_add_same_card_increments_quantity(js):
    r = js.run("""(function () {
        var d = new __exposed.Deck();
        d.addCard(%s);
        d.addCard(%s);
        return { cards: d.cards.length, qty: d.cards[0].quantity, total: d.getTotalCount() };
    })()""" % (js_card(card(uuid="u1")), js_card(card(uuid="u1"))))
    assert r["cards"] == 1
    assert r["qty"] == 2
    assert r["total"] == 2


def test_deck_4_copy_limit(js):
    r = js.run("""(function () {
        var d = new __exposed.Deck();
        var res;
        for (var i = 0; i < 4; i++) res = d.addCard(%s);
        var fifth = d.addCard(%s);
        return { qty: d.cards[0].quantity, fifth: fifth };
    })()""" % (js_card(card(uuid="u1")), js_card(card(uuid="u1"))))
    assert r["qty"] == 4
    assert r["fifth"]["error"] == "Maximum 4 copies per card."


def test_deck_100_card_cap(js):
    r = js.run("""(function () {
        var d = new __exposed.Deck();
        for (var i = 0; i < 100; i++) {
            d.addCard({ uuid: 'u' + i, name: 'Card' + i, types: 'Creature', quantity: 1 });
        }
        var res = d.addCard({ uuid: 'overflow', name: 'Overflow', types: 'Creature' });
        return { total: d.getTotalCount(), res: res };
    })()""")
    assert r["total"] == 100
    assert r["res"]["error"] == "Deck is full (100 cards)."


def test_deck_addCardTo99_puts_commander_card_in_99(js):
    r = js.run("""(function () {
        var d = new __exposed.Deck();
        var c = %s;
        d.commander = c;
        var res = d.addCardTo99(c);
        return { res: res, cards: d.cards.length, commander: d.commander ? d.commander.name : null };
    })()""" % js_card(card(uuid="u1", name="The General", isLegendary=True, isCreature=True)))
    assert r["res"]["ok"] is True
    assert r["cards"] == 1          # commander card is also in the 99
    assert r["commander"] == "The General"


# --------------------------------------------------------------------------- #
# Deck model — sections
# --------------------------------------------------------------------------- #

def test_deck_addToSection_requires_existing_section(js):
    r = js.run("""(function () {
        var d = new __exposed.Deck();
        return d.addToSection('Maybeboard', %s);
    })()""" % js_card(card(uuid="u1")))
    assert "does not exist" in r["error"]


def test_deck_section_add_increment_remove(js):
    r = js.run("""(function () {
        var d = new __exposed.Deck();
        d.createSection('Maybeboard');
        d.addToSection('Maybeboard', %s);
        d.addToSection('Maybeboard', %s);
        var count = d.getSectionCount('Maybeboard');
        var removed = d.removeFromSection('Maybeboard', 'u1');
        return { count: count, removed: removed, after: d.getSectionCount('Maybeboard') };
    })()""" % (js_card(card(uuid="u1")), js_card(card(uuid="u1"))))
    assert r["count"] == 2
    assert r["removed"] is True
    assert r["after"] == 0


def test_deck_setSectionQuantity(js):
    r = js.run("""(function () {
        var d = new __exposed.Deck();
        d.createSection('Sideboard');
        d.addToSection('Sideboard', %s);
        var ok = d.setSectionQuantity('Sideboard', 'u1', 3);
        var bad = d.setSectionQuantity('Sideboard', 'u1', 0);
        return { ok: ok, qty: d.sections['Sideboard'][0].quantity, bad: bad };
    })()""" % js_card(card(uuid="u1")))
    assert r["ok"] is True
    assert r["qty"] == 3
    assert r["bad"] is False


def test_deck_createSection_validation(js):
    r = js.run("""(function () {
        var d = new __exposed.Deck();
        var empty = d.createSection('   ');
        var reserved = d.createSection('main');
        var ok = d.createSection('Custom');
        var dup = d.createSection('Custom');
        return { empty: empty, reserved: reserved, ok: ok, dup: dup };
    })()""")
    assert r["empty"]["error"] == "Section name is required."
    assert r["reserved"]["error"] == "Reserved name."
    assert r["ok"]["ok"] is True and r["ok"]["name"] == "Custom"
    assert r["dup"]["error"] == "Section already exists."


def test_deck_deleteSection(js):
    r = js.run("""(function () {
        var d = new __exposed.Deck();
        d.createSection('Temp');
        var ok = d.deleteSection('Temp');
        var missing = d.deleteSection('Temp');
        return { ok: ok, missing: missing };
    })()""")
    assert r["ok"]["ok"] is True
    assert r["missing"]["error"] == "Section not found."


def test_deck_moveCardBetweenSections_preserves_quantity(js):
    r = js.run("""(function () {
        var d = new __exposed.Deck();
        d.addWithQuantity(%s, 3);
        d.createSection('Sideboard');
        d.moveCardBetweenSections(d.cards[0], '', 'Sideboard');
        var inSection = d.getSectionCount('Sideboard');
        var inMain = d.cards.length;
        var qty = d.sections['Sideboard'][0].quantity;
        d.moveCardBetweenSections(d.sections['Sideboard'][0], 'Sideboard', '');
        return { inSection: inSection, inMain: inMain, qty: qty, mainQty: d.cards[0].quantity };
    })()""" % js_card(card(uuid="u1")))
    assert r["inSection"] == 3
    assert r["inMain"] == 0
    assert r["qty"] == 3
    assert r["mainQty"] == 3  # quantity survives the round-trip


def test_deck_moveCardBetweenSections_merges_duplicate_uuid(js):
    r = js.run("""(function () {
        var d = new __exposed.Deck();
        d.createSection('Sideboard');
        // Same uuid already in the target section at qty 2.
        d.addToSection('Sideboard', %s);
        d.sections['Sideboard'][0].quantity = 2;
        // Add the same uuid to the main deck (qty 1), then move it into Sideboard.
        d.addCardTo99(%s);
        d.moveCardBetweenSections(d.cards[0], '', 'Sideboard');
        return { sideboardLen: d.sections['Sideboard'].length, qty: d.sections['Sideboard'][0].quantity, mainLen: d.cards.length };
    })()""" % (js_card(card(uuid="u1")), js_card(card(uuid="u1"))))
    assert r["sideboardLen"] == 1      # no duplicate entry
    assert r["qty"] == 3               # 2 + 1 merged
    assert r["mainLen"] == 0           # removed from source


def test_deck_moveCardBetweenSections_missing_target_keeps_source(js):
    r = js.run("""(function () {
        var d = new __exposed.Deck();
        d.createSection('Sideboard');
        d.addToSection('Sideboard', %s);
        // Move into a nonexistent section must error and NOT remove from source.
        var result = d.moveCardBetweenSections(d.sections['Sideboard'][0], 'Sideboard', 'Nope');
        return { error: result.error, stillThere: d.sections['Sideboard'].length };
    })()""" % js_card(card(uuid="u1")))
    assert "does not exist" in r["error"]
    assert r["stillThere"] == 1


# --------------------------------------------------------------------------- #
# Deck model — remove / quantities / totals / clear / CI
# --------------------------------------------------------------------------- #

def test_deck_removeCard_commander_and_main(js):
    r = js.run("""(function () {
        var d = new __exposed.Deck();
        d.addCard(%s);          // commander
        d.addCard(%s);          // main
        var remCmd = d.removeCard('u1');
        var remMain = d.removeCard('u2');
        var missing = d.removeCard('u9');
        return { remCmd: remCmd, commander: d.commander, remMain: remMain, missing: missing };
    })()""" % (js_card(card(uuid="u1", isLegendary=True, isCreature=True)),
               js_card(card(uuid="u2"))))
    assert r["remCmd"] is True
    assert r["commander"] is None
    assert r["remMain"] is True
    assert r["missing"] is False


def test_deck_addWithQuantity_bulk_and_cap(js):
    r = js.run("""(function () {
        var d = new __exposed.Deck();
        var ok = d.addWithQuantity(%s, 10);
        var qty = d.cards[0].quantity;
        var over = d.addWithQuantity({ uuid: 'big', name: 'Big' }, 200);
        return { ok: ok, qty: qty, over: over };
    })()""" % js_card(card(uuid="u1", name="Forest")))
    assert r["ok"]["ok"] is True
    assert r["qty"] == 10
    assert "exceed" in r["over"]["error"]


def test_deck_addWithQuantity_bypasses_4_copy(js):
    r = js.run("""(function () {
        var d = new __exposed.Deck();
        d.addWithQuantity(%s, 30);
        return d.cards[0].quantity;
    })()""" % js_card(card(uuid="u1")))
    assert r == 30


def test_deck_setQuantity_rollback_over_cap(js):
    r = js.run("""(function () {
        var d = new __exposed.Deck();
        // 98 unique cards -> 98 in the 99, then 'u98' at qty 1 -> total 99.
        for (var i = 0; i < 98; i++) d.addWithQuantity({ uuid: 'u' + i, name: 'C' + i }, 1);
        d.addWithQuantity(%s, 1);   // 'u98', total now 99
        var ok = d.setQuantity('u98', 2);   // would be 100 -> allowed
        var over = d.setQuantity('u98', 5); // would be 103 -> rollback to 2
        return { ok: ok, over: over, qty: d.cards[d.cards.length-1].quantity, total: d.getTotalCount() };
    })()""" % js_card(card(uuid="u98")))
    assert r["ok"] is True
    assert r["over"] is False
    assert r["qty"] == 2     # rolled back to the pre-failed quantity
    assert r["total"] == 100  # still 100, rollback happened


def test_deck_getTotalCount_includes_commander(js):
    r = js.run("""(function () {
        var d = new __exposed.Deck();
        d.addCard(%s);   // commander
        d.addCard(%s);   // main
        d.addCard(%s);   // main (same card -> qty 2)
        return d.getTotalCount();
    })()""" % (js_card(card(uuid="u1", isLegendary=True, isCreature=True)),
               js_card(card(uuid="u2")),
               js_card(card(uuid="u2"))))
    assert r == 3  # 1 commander + 2 copies


def test_deck_clear_resets_everything(js):
    r = js.run("""(function () {
        var d = new __exposed.Deck();
        d.name = 'My Deck';
        d.addCard(%s);
        d.addCard(%s);
        d.createSection('Sideboard');
        d.clear();
        return { name: d.name, commander: d.commander, cards: d.cards.length,
                 sections: Object.keys(d.sections).length };
    })()""" % (js_card(card(uuid="u1", isLegendary=True, isCreature=True)),
               js_card(card(uuid="u2"))))
    assert r == {"name": "", "commander": None, "cards": 0, "sections": 0}


def test_deck_getCommanderCI(js):
    r = js.run("""(function () {
        var d = new __exposed.Deck();
        d.addCard(%s);
        return d.getCommanderCI();
    })()""" % js_card(card(uuid="u1", isLegendary=True, isCreature=True, colorIdentity="W, U")))
    assert r == "W, U"


# --------------------------------------------------------------------------- #
# Deck model — toExport / fromImport round-trip
# --------------------------------------------------------------------------- #

def test_deck_toExport_fromImport_roundtrip(js):
    r = js.run("""(function () {
        var d = new __exposed.Deck();
        d.name = 'Test Deck';
        d.addCard(%s);             // commander
        d.addWithQuantity(%s, 2);  // main x2
        d.createSection('Sideboard');
        d.addToSection('Sideboard', %s);
        var exported = d.toExport();

        var d2 = new __exposed.Deck();
        d2.fromImport(exported);

        return {
            name: d2.name,
            commander: d2.commander && d2.commander.name,
            mainCount: d2.getTotalCount(),
            sideCount: d2.getSectionCount('Sideboard'),
            mainQty: d2.cards[0].quantity,
            hasTags: Array.isArray(d2.cards[0].tags)
        };
    })()""" % (js_card(card(uuid="u1", name="Gen", isLegendary=True, isCreature=True, colorIdentity="W")),
               js_card(card(uuid="u2", name="Two-of", tags=["Draw"])),
               js_card(card(uuid="u3", name="Side", types="Instant"))))
    assert r["name"] == "Test Deck"
    assert r["commander"] == "Gen"
    assert r["mainCount"] == 3      # 1 commander + 2 copies of Two-of
    assert r["sideCount"] == 1
    assert r["mainQty"] == 2
    assert r["hasTags"] is True


def test_deck_fromImport_defaults_quantity_and_tags(js):
    r = js.run("""(function () {
        var d = new __exposed.Deck();
        d.fromImport({ cards: [{ uuid: 'u1', name: 'NoQty' }] });
        return { qty: d.cards[0].quantity, tags: d.cards[0].tags };
    })()""")
    assert r["qty"] == 1
    assert r["tags"] == []


# --------------------------------------------------------------------------- #
# getSectionKey / sortCardsForDisplay / warnings / color identity
# --------------------------------------------------------------------------- #

def test_getSectionKey_priority_order(js):
    # getSectionKey splits the comma-separated `types` list and returns the first
    # token in TYPE_ORDER. "Artifact Creature" is a single (unknown) type token
    # -> "Other"; "Creature, Artifact" yields "Creature" (Creature precedes
    # Artifact in TYPE_ORDER).
    r = js.run("""(function () {
        return [
            __exposed.getSectionKey({ types: 'Creature, Artifact' }),
            __exposed.getSectionKey({ types: 'Instant' }),
            __exposed.getSectionKey({ types: 'Land' }),
            __exposed.getSectionKey({ types: 'Weird New Type' })
        ];
    })()""")
    assert r == ["Creature", "Instant", "Land", "Other"]


def test_getSectionKey_case_insensitive(js):
    r = js.run("__exposed.getSectionKey({ types: 'creature' })")
    assert r == "Creature"


def test_sortCardsForDisplay_by_name(js):
    r = js.run("""(function () {
        var cards = [{ name: 'Zebra', manaValue: 1 }, { name: 'Apple', manaValue: 5 }];
        __exposed.sortCardsForDisplay(cards);
        return cards.map(function (c) { return c.name; });
    })()""")
    assert r == ["Apple", "Zebra"]


def test_sortCardsForDisplay_by_mv(js):
    r = js.run("""(function () {
        __exposed.currentSort = 'mv';
        var cards = [{ name: 'B', manaValue: 5 }, { name: 'A', manaValue: 1 }];
        __exposed.sortCardsForDisplay(cards);
        return cards.map(function (c) { return c.name; });
    })()""")
    assert r == ["A", "B"]


def test_sortCardsForDisplay_mv_missing_treated_high(js):
    r = js.run("""(function () {
        __exposed.currentSort = 'mv';
        var cards = [{ name: 'NoMV' }, { name: 'HasMV', manaValue: 2 }];
        __exposed.sortCardsForDisplay(cards);
        return cards.map(function (c) { return c.name; });
    })()""")
    assert r == ["HasMV", "NoMV"]


def test_shouldShowQuantityWarning(js):
    c = card(supertypes="", text="", quantity=1)
    # quantity 1 -> no warning
    assert js.run("__exposed.shouldShowQuantityWarning(" + js_card(c) + ")") is False
    # quantity > 1 non-basic -> warning
    assert js.run("__exposed.shouldShowQuantityWarning(" + js_card(c, quantity=2) + ")") is True
    # basic land exempt
    assert js.run("__exposed.shouldShowQuantityWarning(" + js_card(
        c, quantity=10, supertypes="Basic") + ")") is False
    # "any number" oracle text exempt
    assert js.run("__exposed.shouldShowQuantityWarning(" + js_card(
        c, quantity=10, text="A deck can have any number of cards named Hare Apparent") + ")") is False


def test_isOffColorCard(js):
    r = js.run("""(function () {
        __exposed.deck.commander = { colorIdentity: 'W, U' };
        var onColor = __exposed.isOffColorCard({ colorIdentity: 'W' });
        var onColor2 = __exposed.isOffColorCard({ colorIdentity: 'U, W' });
        var offColor = __exposed.isOffColorCard({ colorIdentity: 'R' });
        var colorless = __exposed.isOffColorCard({ colorIdentity: '' });
        return { onColor: onColor, onColor2: onColor2, offColor: offColor, colorless: colorless };
    })()""")
    assert r["onColor"] is False
    assert r["onColor2"] is False
    assert r["offColor"] is True
    assert r["colorless"] is False


def test_isOffColorCard_no_commander_returns_false(js):
    assert js.run("__exposed.isOffColorCard({ colorIdentity: 'R' })") is False


# --------------------------------------------------------------------------- #
# Pricing
# --------------------------------------------------------------------------- #

def test_formatPrice(js):
    assert js.run("__exposed.formatPrice(null)") == "--"
    assert js.run("__exposed.formatPrice(undefined)") == "--"
    assert js.run("__exposed.formatPrice(1.5)") == "$1.50"
    assert js.run("__exposed.formatPrice(0)") == "$0.00"


def test_getDeckPricingStore_default_and_override(js):
    r = js.run("""(function () {
        var d = __exposed.deck;
        var a = __exposed.getDeckPricingStore();
        d.pricingStore = 'eur';
        var b = __exposed.getDeckPricingStore();
        d.pricingStore = null;
        __exposed._setGlobalStore('tix');
        var c = __exposed.getDeckPricingStore();
        return { a: a, b: b, c: c };
    })()""")
    assert r["a"] == "usd"
    assert r["b"] == "eur"
    assert r["c"] == "tix"


def test_getCardPrice(js):
    r = js.run("""(function () {
        __exposed.priceCache['abc-123'] = { usd: '2.50', eur: '1.80' };
        var has = __exposed.getCardPrice({ scryfallId: 'abc-123' });
        var missingCache = __exposed.getCardPrice({ scryfallId: 'nope' });
        var missingId = __exposed.getCardPrice({});
        return { has: has, missingCache: missingCache, missingId: missingId };
    })()""")
    assert r["has"] == 2.5
    assert r["missingCache"] is None
    assert r["missingId"] is None


def test_computeDeckCost_sums_and_flags_missing(js):
    r = js.run("""(function () {
        __exposed.priceCache['id1'] = { usd: '2.00' };
        __exposed.priceCache['id2'] = { usd: '0.50' };
        __exposed.deck.commander = { scryfallId: 'id1' };
        __exposed.deck.cards = [
            { scryfallId: 'id2', quantity: 2 },
            { scryfallId: 'missing', quantity: 1 }
        ];
        return __exposed.computeDeckCost();
    })()""")
    assert r["total"] == 3.0          # 2.00 + (0.50 * 2)
    assert r["hasAnyPrice"] is True
    assert r["hasMissingPrice"] is True
    assert r["prefix"] == "≥ "


def test_computeDeckCost_no_missing_no_prefix(js):
    r = js.run("""(function () {
        __exposed.priceCache['id1'] = { usd: '1.25' };
        __exposed.deck.cards = [{ scryfallId: 'id1', quantity: 4 }];
        return __exposed.computeDeckCost();
    })()""")
    assert r["total"] == 5.0
    assert r["hasMissingPrice"] is False
    assert r["prefix"] == ""


def test_computeDeckCost_respects_section_toggles(js):
    r = js.run("""(function () {
        __exposed.priceCache['main'] = { usd: '1.00' };
        __exposed.priceCache['side'] = { usd: '10.00' };
        __exposed.deck.cards = [{ scryfallId: 'main', quantity: 1 }];
        __exposed.deck.sections = { Sideboard: [{ scryfallId: 'side', quantity: 1 }] };
        __exposed.deck.pricingSections = { main: true, Sideboard: false };
        return __exposed.computeDeckCost();
    })()""")
    assert r["total"] == 1.0  # sideboard excluded


def test_computeDeckCost_empty_deck(js):
    r = js.run("__exposed.computeDeckCost()")
    assert r == {"total": 0, "hasAnyPrice": False, "hasMissingPrice": False, "prefix": ""}


# --------------------------------------------------------------------------- #
# renderManaSymbols / escapeAttr / scryfallId extraction
# --------------------------------------------------------------------------- #

def test_renderManaSymbols_generic_and_colored(js):
    r = js.run("__exposed.renderManaSymbols('{2}{W}')")
    assert r == '<span class="ms ms-2"></span><span class="ms ms-w"></span>'


def test_renderManaSymbols_hybrid_and_phyrexian(js):
    # Hybrid {W/U} -> "w-u"; phyrexian {W/P} hits the `symbol.indexOf('/')`
    # branch FIRST (not the "ends with P" branch), so it becomes "w-p".
    r = js.run("__exposed.renderManaSymbols('{W/U}{W/P}{T}')")
    assert r == ('<span class="ms ms-w-u"></span>'
                 '<span class="ms ms-w-p"></span>'
                 '<span class="ms ms-t"></span>')


def test_renderManaSymbols_empty(js):
    assert js.run("__exposed.renderManaSymbols('')") == ""
    assert js.run("__exposed.renderManaSymbols(null)") == ""


def test_escapeAttr(js):
    r = js.run("__exposed.escapeAttr('a\"b<c>d\\'e')")
    assert r == "a&quot;b&lt;c&gt;d&#39;e"


def test_extractScryfallId(js):
    assert js.run("__exposed._extractScryfallId('https://example.com/card/12345678-1234-1234-1234-123456789012')") == \
        "12345678-1234-1234-1234-123456789012"
    assert js.run("__exposed._extractScryfallId('no id here')") is None


def test_extractScryfallIdFromTransfer_uri_list(js):
    r = js.run("""(function () {
        var dt = {
            getData: function (t) {
                return t === 'text/uri-list'
                    ? 'https://example.com/card/abcdefab-cdef-abcd-efab-abcdefabcdef'
                    : '';
            },
            files: []
        };
        return __exposed._extractScryfallIdFromTransfer(dt);
    })()""")
    assert r == "abcdefab-cdef-abcd-efab-abcdefabcdef"


def test_extractScryfallIdFromTransfer_html_fallback(js):
    r = js.run("""(function () {
        var dt = {
            getData: function (t) {
                return t === 'text/html'
                    ? '<img src="https://example.com/11111111-2222-3333-4444-555555555555">'
                    : '';
            },
            files: []
        };
        return __exposed._extractScryfallIdFromTransfer(dt);
    })()""")
    assert r == "11111111-2222-3333-4444-555555555555"


def test_extractScryfallIdFromTransfer_file_fallback(js):
    r = js.run("""(function () {
        var dt = {
            getData: function () { return ''; },
            files: [{ name: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jpg' }]
        };
        return __exposed._extractScryfallIdFromTransfer(dt);
    })()""")
    assert r == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
