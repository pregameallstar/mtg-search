/* MTG Search — Deck Builder
   ponytail: vanilla JS, no framework, no build step.
   Manages a Commander deck: commander slot, card sections by type,
   localStorage persistence, drag-and-drop from any card in the app. */

(function () {
    'use strict';

    /* ─── Constants ─── */
    var STORAGE_PREFIX = 'mtg-search-deck-';
    var MAX_CARDS = 100;
    var CARD_COLORS = 'WUBRG';
    var TYPE_ORDER = [
        'Creature', 'Instant', 'Sorcery', 'Artifact',
        'Enchantment', 'Planeswalker', 'Battle', 'Land',
    ];

    // Standalone stylesheet for the presentation export (self-contained, no CDN).
    var PRESENTATION_CSS = [
        ':root{--bg:#0d1117;--bg-card:#161b22;--border:#30363d;--text:#c9d1d9;--text-dim:#8b949e;--accent:#58a6ff;}',
        '*{box-sizing:border-box;}',
        'body{margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;line-height:1.5;padding:2rem 1.25rem 4rem;}',
        'h1,h2,h3{margin:0 0 0.5rem;}',
        'h2{font-size:1.25rem;color:var(--text);border-bottom:1px solid var(--border);padding-bottom:0.35rem;margin-top:2rem;}',
        '.hero{display:flex;gap:1.5rem;align-items:flex-start;flex-wrap:wrap;background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:1.5rem;margin-bottom:0.5rem;max-width:1000px;margin-left:auto;margin-right:auto;}',
        '.cmdr-img{width:244px;height:340px;object-fit:cover;border-radius:10px;flex-shrink:0;}',
        '.cmdr-ph{width:244px;height:340px;display:flex;align-items:center;justify-content:center;background:var(--bg-card);border:1px dashed var(--border);border-radius:10px;color:var(--text-dim);text-align:center;padding:1rem;flex-shrink:0;}',
        '.hero-body{flex:1;min-width:260px;}',
        '.hero-body h1{font-size:1.8rem;margin:0;}',
        '.cmdr-mana{display:flex;flex-wrap:wrap;gap:0.2rem;margin:0.4rem 0;}',
        '.cmdr-type{color:var(--text-dim);font-size:0.95rem;margin-bottom:0.25rem;}',
        '.cmdr-ci{color:var(--text-dim);font-size:0.85rem;}',
        '.oracle{font-size:0.9rem;color:var(--text);background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:0.75rem;margin:0.5rem 0 0;}',
        '.block{max-width:1000px;margin:0 auto;}',
        '.outline-text{color:var(--text);font-size:0.95rem;line-height:1.6;margin:0.25rem 0 0.5rem;}',
        '.expand-all{background:var(--bg-card);border:1px solid var(--border);color:var(--text-dim);border-radius:6px;padding:0.3rem 0.7rem;font-size:0.8rem;cursor:pointer;}',
        '.expand-all:hover{border-color:var(--accent);color:var(--text);}',
        '.card-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:0.75rem;margin-bottom:0.5rem;}',
        'button.card{display:flex;gap:0.6rem;align-items:center;padding:0.5rem;cursor:pointer;background:var(--bg-card);border:1px solid var(--border);border-radius:8px;color:var(--text);font:inherit;text-align:left;}',
        'button.card:hover{border-color:var(--accent);}',
        '.card-thumb{width:40px;height:56px;object-fit:cover;border-radius:4px;flex-shrink:0;background:var(--bg);}',
        '.card-name{font-weight:600;font-size:0.9rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}',
        '.modal{position:fixed;inset:0;background:rgba(0,0,0,0.8);display:none;align-items:center;justify-content:center;z-index:1000;padding:2rem;}',
        '.modal.open{display:flex;}',
        '.modal img{max-width:min(360px,90vw);max-height:90vh;border-radius:12px;box-shadow:0 8px 40px rgba(0,0,0,0.6);}',
        '.mc{display:inline-flex;align-items:center;justify-content:center;min-width:1.35rem;height:1.35rem;padding:0 0.25rem;border-radius:999px;font-size:0.72rem;font-weight:700;background:#2a2f38;color:#e6edf3;}',
        '.mc-w{background:#f8f6e3;color:#1a1a1a;}',
        '.mc-u{background:#4a90d9;color:#fff;}',
        '.mc-b{background:#2a2a2a;color:#e6e6e6;}',
        '.mc-r{background:#d94a4a;color:#fff;}',
        '.mc-g{background:#3f9d58;color:#fff;}',
        '.mc-c{background:#b0b8c0;color:#1a1a1a;}',
        '.mc-tap{background:#a0a0a0;color:#1a1a1a;}',
        '.mc-x,.mc-y,.mc-z{background:#5a6572;color:#fff;}'
    ].join('\n');

    /* ─── Card Classification ─── */
    // ponytail: history buffer — flushed to server on add/remove events
    var historyBuffer = [];
    var historyWarningDays = 30;

    function recordHistoryEvent(action, cardData, sectionName) {
        if (!deck.name) return;
        var event = {
            timestamp: new Date().toISOString(),
            action: action,
            card: {
                name: cardData.name,
                uuid: cardData.uuid,
                setCode: cardData.setCode || '',
                number: cardData.number || '',
            },
            section: sectionName || 'main',
            quantity: cardData.quantity || 1,
        };
        historyBuffer.push(event);
    }

    function flushHistory() {
        if (!historyBuffer.length || !deck.name) return;
        var events = historyBuffer.splice(0);
        fetch('/api/deck/history/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: deck.name, events: events }),
        }).catch(function () { /* silent — history is nice-to-have */ });
    }
    /* Split the comma-separated `types` field into trimmed, lowercase tokens.
       Shared by classifyCard() and getSectionKey() so both parse types identically. */
    function parseTypes(typesField) {
        return (typesField || '').split(',').map(function (t) { return t.trim().toLowerCase(); }).filter(Boolean);
    }

    function isSingletonExempt(card) {
        /* Commander allows one copy of a card, except basic lands and cards
           that explicitly permit any number (e.g. Hare Apparent). */
        if (!card) return false;
        if ((card.supertypes || '').indexOf('Basic') !== -1) return true;
        var basicNames = {
            'Plains': 1, 'Island': 1, 'Swamp': 1, 'Mountain': 1,
            'Forest': 1, 'Wastes': 1
        };
        if (basicNames[card.name] === 1) return true;
        return /a deck can have any number of cards named/i.test(card.text || '');
    }

    function classifyCard(card) {
        /* Classify a card into one of: Lands, Ramp, Removal, Draw, Core.
           Evaluated in priority order — first match wins.

           Tags override regex: if a card has a tag matching a category name
           (case-insensitive, any of: Lands, Ramp, Removal, Draw, Core),
           that tag's category wins the match, skipping regex classification. */
        var types = parseTypes(card.types);
        var text  = (card.text  || '').toLowerCase();
        var typeLine = (card.type || '').toLowerCase();
        var tags = (card.tags || []);

        // 0. Tag override — user-assigned tags take priority over regex
        var CAT_KEYS = ['Lands', 'Ramp', 'Removal', 'Draw', 'Core'];
        for (var ti = 0; ti < CAT_KEYS.length; ti++) {
            for (var tj = 0; tj < tags.length; tj++) {
                if (tags[tj].toLowerCase() === CAT_KEYS[ti].toLowerCase()) {
                    return CAT_KEYS[ti];
                }
            }
        }

        // 1. Lands — basic and non-basic lands
        if (types.indexOf('land') !== -1) return 'Lands';

        // 2. Ramp — mana acceleration
        if (/search.*library.*for.*land/.test(text)) return 'Ramp';
        if (types.indexOf('artifact') !== -1 && /add.*\{/.test(text)) return 'Ramp'; // mana rock
        if (types.indexOf('creature') !== -1 && /\{t\}.*add/.test(text)) return 'Ramp'; // mana dork
        if (/(instant|sorcery)/.test(typeLine) && /add.*\{/.test(text)) return 'Ramp'; // ritual
        if (/put.*land.*onto the battlefield/.test(text)) return 'Ramp';
        if (/create.*treasure/.test(text)) return 'Ramp'; // treasure generation

        // 3. Removal — targeted removal and board wipes
        if (/destroy target/.test(text)) return 'Removal';
        if (/exile target/.test(text)) return 'Removal';
        if (/deal.*damage to.*target/.test(text)) return 'Removal';
        if (/counter target spell/.test(text)) return 'Removal';
        if (/destroy all/.test(text)) return 'Removal';
        if (/exile all/.test(text)) return 'Removal'; // exile-based board wipes
        if (/target player sacrifices/.test(text)) return 'Removal'; // edict effects

        // 4. Draw — card advantage, exclude loot/rummage and draw-hate
        if (/draw/.test(text) && !/discard/.test(text)
            && !/can'?t draw|draw step|draw more than/.test(text)) return 'Draw';

        // 5. Core — everything else
        return 'Core';
    }

    /* ─── Deck Data Model ─── */
    function Deck() {
        this.name = '';
        this.commander = null;    // { uuid, setCode, number, name, ... }
        this.cards = [];          // [{ uuid, setCode, number, name, quantity, ... }]
        this.evalData = null;     // Commander eval analysis (from embedded eval iframe)
        this.sections = {};       // { "Sideboard": [card, ...], "Maybeboard": [card, ...], custom: [...] }
        this.outline = [];        // [{ name, text, cards: [card, ...] }, ...] — Deck Outline sections
        this.guideline = null;    // Guideline template name string (or null)
        this.reviewNotes = '';    // Deck Doctor brief / Rule 0 notes
        this.reviewNotesUpdatedAt = ''; // Written by Deck Doctor only
        this.deckBrief = null;    // Structured Deck Doctor brief (owned by Deck Doctor)
        this.pricingStore = null; // Per-deck pricing override (null = use global)
        this.pricingSections = {}; // Section include toggles for cost { "main": true, "Sideboard": false, ... }
        this._savedName = null;   // Last name successfully persisted (rename tracking)
    }

    Deck.prototype.addCard = function (cardData) {
        if (!cardData || !cardData.uuid) return { error: 'Invalid card data.' };

        // Commander logic
        if (cardData.isLegendary && cardData.isCreature) {
            if (!this.commander) {
                this.commander = cardData;
                recordHistoryEvent('add', cardData, 'commander');
                return { ok: true, isCommander: true };
            } else if (this.commander.uuid === cardData.uuid) {
                return { ok: true, isCommander: true }; // already set
            }
            // Try to replace commander
            return { error: 'Commander slot already filled. Clear it first to change commander.' };
        }

        return this._addToMain(cardData);
    };

    Deck.prototype._addToMain = function (cardData) {
        // Regular card — check max deck size
        var currentTotal = this.getTotalCount();
        if (currentTotal >= MAX_CARDS) {
            return { error: 'Deck is full (100 cards).' };
        }

        // Check if card already in deck
        var existing = this.cards.find(function (c) { return c.uuid === cardData.uuid; });
        if (existing) {
            if (!isSingletonExempt(cardData) && (existing.quantity || 1) >= 1) {
                return { error: 'Only one copy of this card is allowed in a Commander deck.' };
            }
            existing.quantity = (parseInt(existing.quantity, 10) || 1) + 1;
            recordHistoryEvent('add', cardData, 'main');
            return { ok: true };
        }

        cardData.quantity = 1;
        cardData.tags = [];
        this.cards.push(cardData);
        recordHistoryEvent('add', cardData, 'main');
        return { ok: true };
    };

    Deck.prototype.addCardTo99 = function (cardData) {
        if (!cardData || !cardData.uuid) return { error: 'Invalid card data.' };

        // If this is the commander card, treat as a regular card (put in 99)
        if (this.commander && this.commander.uuid === cardData.uuid) {
            return this._addToMain(cardData);
        }

        // Legendary creatures can also be in the 99 — skip commander logic
        return this._addToMain(cardData);
    };

    /* ── Custom Sections ── */
    Deck.prototype.addToSection = function (sectionName, cardData) {
        if (!cardData || !cardData.uuid) return { error: 'Invalid card data.' };
        if (!this.sections) this.sections = {};
        var section = this.sections[sectionName];
        if (!section) return { error: 'Section "' + sectionName + '" does not exist. Create it first.' };
        var existing = section.find(function (c) { return c.uuid === cardData.uuid; });
        if (existing) {
            existing.quantity = (parseInt(existing.quantity, 10) || 1) + 1;
        } else {
            cardData.quantity = 1;
            cardData.tags = [];
            section.push(cardData);
        }
        recordHistoryEvent('add', cardData, sectionName);
        return { ok: true };
    };

    Deck.prototype.removeFromSection = function (sectionName, uuid) {
        var section = this.sections[sectionName];
        if (!section) return false;
        var idx = section.findIndex(function (c) { return c.uuid === uuid; });
        if (idx === -1) return false;
        var removed = section[idx];
        section.splice(idx, 1);
        recordHistoryEvent('remove', removed, sectionName);
        return true;
    };

    Deck.prototype.setSectionQuantity = function (sectionName, uuid, qty) {
        if (qty < 1) return false;
        var section = this.sections[sectionName];
        if (!section) return false;
        var card = section.find(function (c) { return c.uuid === uuid; });
        if (!card) return false;
        card.quantity = qty;
        return true;
    };

    Deck.prototype.getSectionCount = function (sectionName) {
        var section = this.sections[sectionName];
        if (!section) return 0;
        return section.reduce(function (sum, c) { return sum + (c.quantity || 1); }, 0);
    };

    Deck.prototype.createSection = function (name) {
        if (!name || !name.trim()) return { error: 'Section name is required.' };
        var key = name.trim();
        var reserved = ['main', 'commander'];
        if (reserved.indexOf(key.toLowerCase()) !== -1) return { error: 'Reserved name.' };
        if (!this.sections) this.sections = {};
        if (this.sections.hasOwnProperty(key)) return { error: 'Section already exists.' };
        this.sections[key] = [];
        return { ok: true, name: key };
    };

    Deck.prototype.deleteSection = function (name) {
        if (!this.sections || !this.sections.hasOwnProperty(name)) return { error: 'Section not found.' };
        delete this.sections[name];
        return { ok: true };
    };

    Deck.prototype.moveCardBetweenSections = function (cardData, fromSection, toSection) {
        // fromSection or toSection can be '' for main deck, or a section name
        // Preserve card quantity and tags across the move
        var movedCard = Object.assign({}, cardData);

        // Validate target BEFORE removing from source (otherwise a missing
        // section silently destroys the card).
        var target = null;
        if (toSection) {
            target = this.sections[toSection];
            if (!target) return { error: 'Section "' + toSection + '" does not exist.' };
        } else {
            target = this.cards;
            var alreadyInMain = target.find(function (c) { return c.uuid === movedCard.uuid; });
            if (!isSingletonExempt(movedCard) && (movedCard.quantity || 1) > 1) {
                return { error: 'Only one copy of this card is allowed in a Commander deck.' };
            }
            if (!isSingletonExempt(movedCard) && alreadyInMain) {
                return { error: 'Only one copy of this card is allowed in a Commander deck.' };
            }
        }

        // Remove from source
        if (fromSection) {
            this.removeFromSection(fromSection, cardData.uuid);
        } else {
            this.removeCard(cardData.uuid);
        }

        // Add to target (preserving quantity/tags). Merge on duplicate uuid so a
        // card already present in the target increments instead of duplicating.
        var existing = target.find(function (c) { return c.uuid === movedCard.uuid; });
        if (existing) {
            existing.quantity = (existing.quantity || 1) + (movedCard.quantity || 1);
            recordHistoryEvent('add', movedCard, toSection || 'main');
        } else {
            target.push(movedCard);
            recordHistoryEvent('add', movedCard, toSection || 'main');
        }
        return { ok: true };
    };

    Deck.prototype.setCardNote = function (uuid, sectionName, note) {
        // Persist a free-text note onto a single card. sectionName '' = main
        // deck, otherwise the named custom section. Cards with duplicate uuids
        // across the main deck and a section are disambiguated by section.
        var list = sectionName ? (this.sections && this.sections[sectionName]) : this.cards;
        if (!list) return { error: 'Card not found.' };
        var card = list.find(function (c) { return c.uuid === uuid; });
        if (!card) return { error: 'Card not found.' };
        card.note = note || '';
        return { ok: true };
    };

    Deck.prototype.removeCard = function (uuid) {
        if (this.commander && this.commander.uuid === uuid) {
            recordHistoryEvent('remove', this.commander, 'commander');
            this.commander = null;
            return true;
        }
        var idx = this.cards.findIndex(function (c) { return c.uuid === uuid; });
        if (idx === -1) return false;
        var removed = this.cards[idx];
        this.cards.splice(idx, 1);
        recordHistoryEvent('remove', removed, 'main');
        return true;
    };

    Deck.prototype.addWithQuantity = function (cardData, qty) {
        /* Bulk add for text/template imports. Basic lands and explicit
           "any number" cards may exceed one copy; other cards are still
           singleton. Enforces the deck size limit. Always goes to 99. */
        if (!cardData || !cardData.uuid) return { error: 'Invalid card data.' };
        qty = parseInt(qty, 10);
        if (!qty || qty < 1) return { error: 'Quantity must be at least 1.' };

        var currentTotal = this.getTotalCount();
        if (currentTotal + qty > MAX_CARDS) {
            return { error: 'Adding ' + qty + '× ' + cardData.name + ' would exceed the ' + MAX_CARDS + '-card limit.' };
        }

        var existing = this.cards.find(function (c) { return c.uuid === cardData.uuid; });
        if (!isSingletonExempt(cardData)) {
            if (existing) {
                return { error: 'Only one copy of this card is allowed in a Commander deck.' };
            }
            if (qty > 1) {
                return { error: 'Only one copy of this card is allowed in a Commander deck.' };
            }
        }
        if (existing) {
            existing.quantity = (parseInt(existing.quantity, 10) || 1) + qty;
            recordHistoryEvent('add', Object.assign({}, cardData, { quantity: qty }), 'main');
            return { ok: true };
        }

        cardData.quantity = qty;
        cardData.tags = [];
        this.cards.push(cardData);
        recordHistoryEvent('add', cardData, 'main');
        return { ok: true };
    };

    Deck.prototype.setQuantity = function (uuid, qty) {
        qty = parseInt(qty, 10);
        if (!qty || qty < 1) return false;
        var card = this.cards.find(function (c) { return c.uuid === uuid; });
        if (!card) return false;
        if (!isSingletonExempt(card) && qty > 1) return false;
        var oldQty = card.quantity;
        card.quantity = qty;
        // Check max deck size
        if (this.getTotalCount() > MAX_CARDS) {
            card.quantity = oldQty;
            return false;
        }
        return true;
    };

    Deck.prototype.getTotalCount = function () {
        var total = this.cards.reduce(function (sum, c) { return sum + c.quantity; }, 0);
        if (this.commander) total += 1;
        return total;
    };

    Deck.prototype.clear = function () {
        this.name = '';
        this.commander = null;
        this.cards = [];
        this.evalData = null;
        this.sections = {};
        this.outline = [];
        this.guideline = null;
        this.reviewNotes = '';
        this.reviewNotesUpdatedAt = '';
        this.deckBrief = null;
        this.pricingStore = null;
        this.pricingSections = {};
    };

    Deck.prototype.clearContents = function () {
        /* Replace the deck's card contents while preserving its identity and
           presentation metadata (name, outline, guideline, pricing options).
           Used by the "Replace current deck contents" text import. */
        this.commander = null;
        this.cards = [];
        this.evalData = null;
        this.sections = {};
    };

    Deck.prototype.getCommanderCI = function () {
        return (this.commander && this.commander.colorIdentity) || '';
    };

    Deck.prototype.toExport = function () {
        var cardMapper = function (c) {
            return {
                uuid: c.uuid,
                setCode: c.setCode,
                number: c.number,
                name: c.name,
                types: c.types,
                manaCost: c.manaCost,
                manaValue: c.manaValue,
                colors: c.colors,
                colorIdentity: c.colorIdentity,
                imageUrl: c.imageUrl,
                quantity: c.quantity,
                text: c.text,
                supertypes: c.supertypes,
                tags: c.tags || [],
                note: c.note || '',
                scryfallId: c.scryfallId || '',
                roles: c.roles || [],
                rolePrimary: c.rolePrimary || '',
                rolesVersion: c.rolesVersion || 0,
            };
        };

        var sectionsExport = {};
        if (this.sections) {
            Object.keys(this.sections).forEach(function (key) {
                sectionsExport[key] = this.sections[key].map(cardMapper);
            }.bind(this));
        }

        var outlineExport = (this.outline || []).map(function (sec) {
            return {
                name: sec.name || '',
                text: sec.text || '',
                cards: (sec.cards || []).map(cardMapper),
            };
        });

        return {
            name: this.name,
            commander: this.commander,
            evalData: this.evalData,
            cards: this.cards.map(cardMapper),
            sections: sectionsExport,
            outline: outlineExport,
            guideline: this.guideline,
            reviewNotes: this.reviewNotes || '',
            reviewNotesUpdatedAt: this.reviewNotesUpdatedAt || '',
            deckBrief: this.deckBrief || null,
            pricingStore: this.pricingStore || null,
            pricingSections: this.pricingSections || {},
        };
    };

    Deck.prototype.toTextExport = function () {
        // Plain-text "deckbox" list, importable by Moxfield / Archidekt.
        // Commander first (auto-detected), then the main deck, then each
        // non-empty section as a header line followed by its cards.
        var lines = [];

        if (this.commander && this.commander.name) {
            lines.push('1 ' + this.commander.name);
            lines.push('');  // blank line separates commander from main deck
        }

        (this.cards || []).forEach(function (c) {
            lines.push((c.quantity || 1) + ' ' + c.name);
        });

        if (this.sections) {
            Object.keys(this.sections).forEach(function (key) {
                var cards = this.sections[key] || [];
                if (!cards.length) return;
                lines.push('');
                lines.push(key);
                cards.forEach(function (c) {
                    lines.push((c.quantity || 1) + ' ' + c.name);
                });
            }.bind(this));
        }

        return lines.join('\n') + '\n';
    };

    Deck.prototype.toOutlineTextExport = function () {
        // Plain-text export of the Deck Outline: each section's name as a
        // header, its narrative text, and (when present) a list of example
        // card names. Empty sections are skipped so pasting stays clean.
        var lines = [];
        this.ensureOutline().forEach(function (sec) {
            var text = (sec.text || '').trim();
            var cards = (sec.cards || []).map(function (c) { return c.name || ''; }).filter(Boolean);
            if (!text && !cards.length) return;
            if (lines.length) lines.push('');
            lines.push((sec.name || 'Untitled').trim());
            if (text) lines.push(text);
            if (cards.length) lines.push('Example cards: ' + cards.join(', '));
        });
        return lines.join('\n') + '\n';
    };

    Deck.prototype.toPresentationHTML = function (imgResolver) {
        /* Self-contained presentation page (no external deps). `imgResolver`
           is (size, scryfallId) -> dataURI string or '' for a placeholder. */
        function typeLine(card) {
            if (card.type) return card.type;  // full type line when present (commander / card-lookup)
            var parts = [];
            if (card.supertypes) parts.push(card.supertypes);
            if (card.types) parts.push(card.types.replace(/,/g, ' '));
            return parts.join(' ');
        }

        function imgTag(size, scryfallId, alt) {
            var uri = imgResolver(size, scryfallId);
            if (!uri) return '';
            var cls = size === 'normal' ? 'cmdr-img' : 'card-thumb';
            return '<img class="' + cls + '" src="' + uri + '" alt="' + escapeAttr(alt) + '" loading="lazy">';
        }

        var parts = [];

        // ── Commander hero ──
        parts.push('<header class="hero">');
        if (this.commander) {
            var cmdr = this.commander;
            var img = imgTag('normal', cmdr.scryfallId, cmdr.name);
            parts.push(img || '<div class="cmdr-ph">' + escapeHtml(cmdr.name) + '</div>');
            parts.push('<div class="hero-body">');
            parts.push('<h1>' + escapeHtml(cmdr.name) + '</h1>');
            parts.push('<div class="cmdr-mana">' + renderManaChips(cmdr.manaCost) + '</div>');
            parts.push('<div class="cmdr-type">' + escapeHtml(typeLine(cmdr)) + '</div>');
            if (cmdr.colorIdentity) {
                parts.push('<div class="cmdr-ci">Color identity: ' + escapeHtml(cmdr.colorIdentity) + '</div>');
            }
            if (cmdr.text) parts.push('<p class="oracle">' + escapeHtml(cmdr.text) + '</p>');
            parts.push('</div>');
        }
        parts.push('</header>');

        // ── Deck Outline sections ──
        // Each example card is a clickable thumbnail: no mana cost, type, or
        // oracle text — clicking opens a modal with the enlarged card image.
        var outline = this.ensureOutline();
        outline.forEach(function (sec) {
            var cards = sec.cards || [];

            parts.push('<section class="block"><h2>' + escapeHtml(sec.name || '') + '</h2>');
            if (sec.text) {
                parts.push('<div class="outline-text">' + escapeHtml(sec.text).replace(/\n/g, '<br>') + '</div>');
            }
            if (cards.length) {
                parts.push('<div class="card-grid">');
                cards.forEach(function (c) {
                    var img = imgTag('small', c.scryfallId, c.name);
                    var large = imgResolver('normal', c.scryfallId) || '';
                    parts.push('<button type="button" class="card" data-large="' + escapeAttr(large) + '" data-name="' + escapeAttr(c.name) + '">'
                        + img
                        + '<span class="card-name">' + escapeHtml(c.name) + '</span>'
                        + '</button>');
                });
                parts.push('</div>');
            }
            parts.push('</section>');
        });

        parts.push('<div class="modal" id="card-modal" role="dialog" aria-modal="true">'
            + '<img id="card-modal-img" alt="">'
            + '</div>');

        var title = (this.name || 'Deck') + ' — Deck Presentation';
        var body = parts.join('\n');

        return '<!DOCTYPE html>\n<html lang="en">\n<head>\n'
            + '<meta charset="UTF-8">\n'
            + '<meta name="viewport" content="width=device-width, initial-scale=1.0">\n'
            + '<title>' + escapeHtml(title) + '</title>\n'
            + '<style>\n' + PRESENTATION_CSS + '\n</style>\n'
            + '</head>\n<body>\n'
            + body
            + '\n<script>\n'
            + '(function(){'
            + 'var modal=document.getElementById("card-modal");'
            + 'var img=document.getElementById("card-modal-img");'
            + 'if(!modal||!img)return;'
            + 'var cards=document.querySelectorAll("button.card");'
            + 'for(var i=0;i<cards.length;i++){'
            + 'cards[i].addEventListener("click",function(){'
            + 'var large=this.getAttribute("data-large");'
            + 'if(!large)return;'
            + 'img.src=large;'
            + 'img.alt=this.getAttribute("data-name")||"";'
            + 'modal.classList.add("open");'
            + '});}'
            + 'modal.addEventListener("click",function(e){'
            + 'if(e.target===modal)modal.classList.remove("open");'
            + '});'
            + 'document.addEventListener("keydown",function(e){'
            + 'if(e.key==="Escape")modal.classList.remove("open");'
            + '});'
            + '})();'
            + '\n</script>\n</body>\n</html>\n';
    };

    Deck.prototype.fromImport = function (data) {
        this.name = data.name || '';
        this.commander = data.commander || null;
        this.evalData = data.evalData || null;
        this.guideline = data.guideline || null;
        this.reviewNotes = data.reviewNotes || '';
        this.reviewNotesUpdatedAt = data.reviewNotesUpdatedAt || '';
        this.deckBrief = data.deckBrief || null;
        this.pricingStore = data.pricingStore || null;
        this.pricingSections = data.pricingSections || {};
        this.cards = (data.cards || []).map(function (c) {
            var card = Object.assign({}, c, {
                quantity: parseInt(c.quantity, 10) || 1
            });
            if (!card.tags) card.tags = [];
            if (!card.note) card.note = '';
            return card;
        });
        this.sections = {};
        var sectionsData = data.sections || {};
        var self = this;
        Object.keys(sectionsData).forEach(function (key) {
            self.sections[key] = (sectionsData[key] || []).map(function (c) {
                var card = Object.assign({}, c, {
                    quantity: parseInt(c.quantity, 10) || 1
                });
                if (!card.tags) card.tags = [];
                if (!card.note) card.note = '';
                return card;
            });
        });

        // Deck Outline — ordered array of { name, text, cards[] }. Seed the
        // three defaults when absent so the editor/export never start blank.
        this.outline = (data.outline || []).map(function (sec) {
            return {
                name: sec.name || '',
                text: sec.text || '',
                cards: (sec.cards || []).map(function (c) {
                    var card = Object.assign({}, c, {
                        quantity: parseInt(c.quantity, 10) || 1
                    });
                    if (!card.tags) card.tags = [];
                    if (!card.note) card.note = '';
                    return card;
                }),
            };
        });
        if (!this.outline.length) {
            this.outline = self.defaultOutline();
        }
    };

    Deck.prototype.defaultOutline = function () {
        /* Seed the three default Deck Outline sections. A fresh section carries
           a name, an empty narrative body, and an empty example-card list. */
        return [
            { name: 'Description', text: '', cards: [] },
            { name: 'Strategy', text: '', cards: [] },
            { name: 'How to Win', text: '', cards: [] },
        ];
    };

    Deck.prototype.ensureOutline = function () {
        /* Return the current outline, seeding the three defaults when empty.
           The seed must be written into the model: every editor/drop handler
           looks up deck.outline[idx], so returning a detached default array
           made the visible sections impossible to edit. clear() can still set
           an empty outline; the defaults reappear on the next read. */
        if (!this.outline || !this.outline.length) {
            this.outline = this.defaultOutline();
        }
        return this.outline;
    };

    /* ─── Server-side Persistence ─── */

    var saveTimer = null;
    var needsSave = false;

    function saveDeckToServer(deck, useBeacon) {
        if (!deck.name) {
            updateSaveStatus('unsaved', 'Name your deck to save');
            return;
        }
        var exportData = deck.toExport();
        if (deck._savedName && deck._savedName !== deck.name) {
            exportData.previousName = deck._savedName;
        }
        var payload = JSON.stringify(exportData);

        if (useBeacon && navigator.sendBeacon) {
            var queued = navigator.sendBeacon(
                '/api/deck/save',
                new Blob([payload], {type: 'application/json'})
            );
            if (queued) {
                needsSave = false;
                return;
            }
            // Beacon queue rejected the payload (often > ~64 KB). Fall through
            // to a keepalive fetch; the visibility handler leaves the debounce
            // timer alive as a second chance.
        }

        updateSaveStatus('saving', 'Saving…');
        fetch('/api/deck/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: payload,
            keepalive: !!useBeacon,
        })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (data.success) {
                deck._savedName = deck.name;
                updateSaveStatus('saved', 'Saved');
                needsSave = false;
            } else {
                updateSaveStatus('error', data.error || 'Save failed');
            }
        })
        .catch(function (e) {
            updateSaveStatus('error', 'Save failed');
        });
    }

    function updateSaveStatus(state, text) {
        var el = $('#deck-save-status');
        if (!el) return;
        el.textContent = text;
        el.className = 'deck-save-status ' + state;
    }

    function loadDeckFromServer(name) {
        var xhr = new XMLHttpRequest();
        xhr.open('GET', '/api/deck/list', false);
        xhr.send();
        if (xhr.status === 200) {
            var decks = JSON.parse(xhr.responseText);
            for (var i = 0; i < decks.length; i++) {
                if (decks[i].name === name) return decks[i];
            }
        }
        return null;
    }

    function listSavedDecksFromServer() {
        try {
            var xhr = new XMLHttpRequest();
            xhr.open('GET', '/api/deck/list', false);
            xhr.send();
            if (xhr.status === 200) {
                var decks = JSON.parse(xhr.responseText);
                return decks.map(function (d) {
                    return {
                        name: d.name,
                        count: (d.cards || []).length,
                        commander: d.commander ? d.commander.name : ''
                    };
                }).sort(function (a, b) { return a.name.localeCompare(b.name); });
            }
        } catch (e) { /* ignore */ }
        return [];
    }

    /* ─── State ─── */
    var deck = new Deck();
    var currentSearchResults = [];
    var searchPage = 1;
    var searchTotalPages = 0;
    var searchParams = null;  // cached URLSearchParams for page navigation
    var similarPage = 1;
    var similarTotalPages = 0;
    var similarCardUrl = null;  // base URL for similar pagination
    var deckAnalysis = null;
    var analysisTimer = null;
    var analysisRequestId = 0;

    /* ─── DOM References ─── */
    var $ = function (sel) { return document.querySelector(sel); };
    var $$ = function (sel) { return document.querySelectorAll(sel); };

    /* ─── Sort State ─── */
    var currentSort = 'name'; // 'name' | 'mv'

    /* ─── Tag State ─── */
    var tagCatalog = [];       // [{name, color}, ...] loaded from server
    var currentGroupBy = 'type'; // 'type' | 'tag'

    /* Tag color palette for custom tags (cycles through 12 colors) */
    var TAG_COLORS = [
        '#4caf50', '#f44336', '#2196f3', '#9c27b0', '#ff9800', '#009688',
        '#795548', '#ffc107', '#e91e63', '#00bcd4', '#8bc34a', '#ff5722'
    ];

    /* ── Tag Catalog Management ─── */
    function loadTagCatalog(callback) {
        fetch('/api/tags/list')
            .then(function (r) { return r.json(); })
            .then(function (data) {
                tagCatalog = (data.tags || []).slice();
                showTagUI(tagCatalog.length > 0);
                if (callback) callback();
            })
            .catch(function () {
                // Offline fallback
                if (callback) callback();
            });
    }

    function saveTagCatalog() {
        fetch('/api/tags/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ tags: tagCatalog }),
        }).catch(function () { /* ignore */ });
    }

    function addCustomTag(name) {
        // Pick a color — cycle through palette, avoiding already-used colors
        var usedColors = {};
        tagCatalog.forEach(function (t) { usedColors[t.color] = true; });
        var color = TAG_COLORS[0];
        for (var i = 0; i < TAG_COLORS.length; i++) {
            if (!usedColors[TAG_COLORS[i]]) { color = TAG_COLORS[i]; break; }
        }
        tagCatalog.push({ name: name, color: color });
        saveTagCatalog();
    }

    function findTagByName(name) {
        for (var i = 0; i < tagCatalog.length; i++) {
            if (tagCatalog[i].name === name) return tagCatalog[i];
        }
        return null;
    }

    function showTagUI(show) {
        var groupBar = $('#deck-group-toolbar');
        var tagStats = $('#tag-stats');
        if (groupBar) groupBar.hidden = !show;
        if (tagStats) tagStats.hidden = !show;
    }

    /* ── Tag Popover ─── */
    var activePopoverCard = null;

    function openTagPopover(uuid, event) {
        var card = deck.cards.find(function (c) { return c.uuid === uuid; });
        if (!card) return;
        closeTagPopover();

        activePopoverCard = uuid;

        var popover = document.createElement('div');
        popover.className = 'tag-popover';
        popover.id = 'tag-popover';

        // Card name header
        var html = '<div class="tag-popover-header">' + escapeHtml(card.name) + '</div>';
        html += '<div class="tag-popover-list">';

        tagCatalog.forEach(function (tag) {
            var checked = (card.tags || []).indexOf(tag.name) !== -1 ? ' checked' : '';
            html += '<label class="tag-option">' +
                '<input type="checkbox" class="tag-cb" data-tag="' + escapeAttr(tag.name) + '"' + checked + '>' +
                '<span class="tag-option-dot" style="background:' + safeColor(tag.color) + '"></span>' +
                '<span class="tag-option-name">' + escapeHtml(tag.name) + '</span>' +
            '</label>';
        });

        html += '</div>';
        // Add new tag input
        html += '<div class="tag-popover-add">' +
            '<input type="text" class="tag-popover-input" placeholder="New tag…" maxlength="24">' +
            '<button class="tag-popover-add-btn">+</button>' +
        '</div>';

        popover.innerHTML = html;
        document.body.appendChild(popover);

        // Position near the trigger element
        var btn = event.target.closest('.card-tag-btn');
        if (btn) {
            var rect = btn.getBoundingClientRect();
            popover.style.position = 'fixed';
            popover.style.top = (rect.bottom + 4) + 'px';
            popover.style.left = Math.min(rect.left, window.innerWidth - 250) + 'px';
        }

        // Checkbox change handler
        popover.querySelectorAll('.tag-cb').forEach(function (cb) {
            cb.addEventListener('change', function () {
                var tagName = this.dataset.tag;
                if (this.checked) {
                    if (card.tags.indexOf(tagName) === -1) card.tags.push(tagName);
                } else {
                    var idx = card.tags.indexOf(tagName);
                    if (idx !== -1) card.tags.splice(idx, 1);
                }
                renderTagFilterBar();
                renderSections();
                renderTagStats();
                saveCurrentDeck();
            });
        });

        // Add new tag
        var addInput = popover.querySelector('.tag-popover-input');
        var addBtn = popover.querySelector('.tag-popover-add-btn');

        function submitNewTag() {
            var name = addInput.value.trim();
            if (!name) return;
            // Check for duplicates
            if (findTagByName(name)) {
                addInput.value = '';
                return;
            }
            addCustomTag(name);
            // Re-open popover with updated catalog
            var idx = card.tags.indexOf(name);
            if (idx === -1) card.tags.push(name);
            openTagPopover(uuid, event);
            renderTagFilterBar();
            renderSections();
            renderTagStats();
            saveCurrentDeck();
        }

        addBtn.addEventListener('click', submitNewTag);
        addInput.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') { e.preventDefault(); submitNewTag(); }
        });

        // Close on outside click
        setTimeout(function () {
            document.addEventListener('click', closeTagPopoverOnOutside);
        }, 0);
    }

    function closeTagPopoverOnOutside(e) {
        var popover = document.getElementById('tag-popover');
        if (!popover) return;
        if (!popover.contains(e.target) && !e.target.closest('.card-tag-btn')) {
            closeTagPopover();
        }
    }

    function closeTagPopover() {
        var popover = document.getElementById('tag-popover');
        if (popover) popover.remove();
        activePopoverCard = null;
        document.removeEventListener('click', closeTagPopoverOnOutside);
    }

    /* ── Pricing ── */
    var priceCache = {};      // { scryfallId: { usd: "0.50", ... } }
    var pricesLoaded = false;
    var globalPricingStore = 'usd';

    function getDeckPricingStore() {
        return deck.pricingStore || globalPricingStore || 'usd';
    }

    function getCardPrice(card) {
        if (!card || !card.scryfallId) return null;
        var prices = priceCache[card.scryfallId];
        if (!prices) return null;
        var store = getDeckPricingStore();
        var val = prices[store];
        if (val === null || val === undefined) return null;
        return parseFloat(val);
    }

    function formatPrice(price) {
        if (price === null || price === undefined) return '--';
        return '$' + price.toFixed(2);
    }

    function fetchPrices(onComplete) {
        // Collect unique scryfallIds
        var ids = [];
        function collect(card) {
            if (card && card.scryfallId && ids.indexOf(card.scryfallId) === -1) {
                ids.push(card.scryfallId);
            }
        }
        if (deck.commander) collect(deck.commander);
        deck.cards.forEach(collect);
        if (deck.sections) {
            Object.keys(deck.sections).forEach(function (k) {
                (deck.sections[k] || []).forEach(collect);
            });
        }

        if (ids.length === 0) {
            pricesLoaded = true;
            if (onComplete) onComplete();
            return;
        }

        _setRefreshBtnLoading(true);

        // The API caps a request at 200 IDs. Split larger decks/sections into
        // batches instead of letting a 400 silently leave every price empty.
        var BATCH_SIZE = 50;
        var chain = Promise.resolve();
        for (var start = 0; start < ids.length; start += BATCH_SIZE) {
            (function (batch) {
                chain = chain.then(function () {
                    return fetch('/api/pricing/fetch', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ scryfallIds: batch }),
                    })
                    .then(function (r) { return r.json(); })
                    .then(function (data) {
                        if (data.success && data.prices) {
                            Object.keys(data.prices).forEach(function (id) {
                                priceCache[id] = data.prices[id];
                            });
                        }
                    })
                    .catch(function () { /* try the remaining batches */ });
                });
            })(ids.slice(start, start + BATCH_SIZE));
        }

        chain.then(function () {
            pricesLoaded = true;
            _setRefreshBtnLoading(false);
            if (onComplete) onComplete();
        });
    }

    function _setRefreshBtnLoading(loading) {
        var btn = $('#deck-refresh-prices-btn');
        if (!btn) return;
        btn.disabled = loading;
        btn.textContent = loading ? 'Pricing…' : '💲 Refresh';
    }

    function computeDeckCost() {
        var total = 0;
        var hasAnyPrice = false;
        var hasMissingPrice = false;

        function sumCards(cards, sectionName) {
            if (!cards) return;
            cards.forEach(function (c) {
                var p = getCardPrice(c);
                if (p !== null) {
                    total += p * (c.quantity || 1);
                    hasAnyPrice = true;
                } else {
                    hasMissingPrice = true;
                }
            });
        }

        // Main deck — always check include via checkbox
        var pSections = deck.pricingSections || {};
        var includeMain = pSections['main'] !== false; // default true
        if (includeMain) {
            if (deck.commander) {
                var cp = getCardPrice(deck.commander);
                if (cp !== null) { total += cp; hasAnyPrice = true; }
                else { hasMissingPrice = true; }
            }
            sumCards(deck.cards, 'main');
        }

        // Custom sections
        if (deck.sections) {
            Object.keys(deck.sections).forEach(function (key) {
                var include = pSections[key] !== false; // default true
                if (include) {
                    sumCards(deck.sections[key] || [], key);
                }
            });
        }

        return {
            total: total,
            hasAnyPrice: hasAnyPrice,
            hasMissingPrice: hasMissingPrice,
            prefix: hasMissingPrice ? '≥ ' : '',
        };
    }

    function renderDeckCost() {
        var bar = $('#deck-cost-bar');
        var costEl = $('#deck-total-cost');
        if (!bar || !costEl) return;

        bar.style.display = ''; // always show the bar

        if (!pricesLoaded) {
            costEl.textContent = 'Pricing…';
            return;
        }

        var cost = computeDeckCost();
        if (cost.hasAnyPrice) {
            costEl.textContent = cost.prefix + formatPrice(cost.total);
        } else {
            costEl.textContent = '--';
        }
    }

    function initRefreshPricesButton() {
        var btn = $('#deck-refresh-prices-btn');
        if (!btn) return;
        btn.addEventListener('click', function () {
            // Clear cache to force re-fetch
            priceCache = {};
            pricesLoaded = false;
            fetchPrices(function () {
                renderAll();
                showToast('Prices refreshed.');
            });
        });
    }

    function initPricingStoreSelect() {
        var sel = $('#deck-pricing-store');
        if (!sel) return;
        // Set initial value from deck
        sel.value = deck.pricingStore || '';
        sel.addEventListener('change', function () {
            deck.pricingStore = this.value || null;
            saveCurrentDeck();
            renderAll();
            renderDeckCost();
        });
    }

    function initSectionIncludeCheckboxes() {
        var mainCb = $('#deck-include-main-cb');
        var mainLabel = $('#deck-include-main-label');
        if (mainCb && mainLabel) {
            var pSections = deck.pricingSections || {};
            mainCb.checked = pSections['main'] !== false;
            mainCb.addEventListener('change', function () {
                if (!deck.pricingSections) deck.pricingSections = {};
                deck.pricingSections['main'] = this.checked;
                saveCurrentDeck();
                renderDeckCost();
            });
        }
    }

    function updatePricingStoreSelect() {
        var sel = $('#deck-pricing-store');
        if (sel) {
            sel.value = deck.pricingStore || '';
        }
    }

    function updateSectionIncludeCheckboxes() {
        var mainCb = $('#deck-include-main-cb');
        var mainLabel = $('#deck-include-main-label');
        var pSections = deck.pricingSections || {};
        if (mainCb && mainLabel) {
            mainCb.checked = pSections['main'] !== false;
            mainLabel.style.display = pricesLoaded ? '' : 'none';
        }
    }

    /* ── Tag Filter Bar ─── */
    function renderTagFilterBar() {
        // Rebuild the tag filter chips above the card list.
        // Preserve active filters across the rebuild; renderAll calls this so
        // counts stay fresh after add/remove.
        var el = $('#tag-filter-bar');
        if (!el) return;

        var activeTags = {};
        el.querySelectorAll('.tag-filter-chip.active').forEach(function (chip) {
            activeTags[chip.dataset.tag] = true;
        });

        var tagCounts = {};
        deck.cards.forEach(function (card) {
            (card.tags || []).forEach(function (t) {
                tagCounts[t] = (tagCounts[t] || 0) + card.quantity;
            });
        });

        var entries = Object.keys(tagCounts);
        if (entries.length === 0) { el.innerHTML = ''; el.hidden = true; return; }
        el.hidden = false;
        entries.sort(function (a, b) { return tagCounts[b] - tagCounts[a]; });

        var html = '';
        entries.forEach(function (tagName) {
            var tag = findTagByName(tagName);
            var color = safeColor(tag ? tag.color : '#666');
            var active = activeTags[tagName] ? ' active' : '';
            html += '<button class="tag-filter-chip' + active + '" data-tag="' + escapeAttr(tagName) + '" style="--tag-color:' + color + '">' +
                escapeHtml(tagName) + ' ' + tagCounts[tagName] +
            '</button>';
        });
        el.innerHTML = html;

        // Toggle filter on click
        el.querySelectorAll('.tag-filter-chip').forEach(function (chip) {
            chip.addEventListener('click', function () {
                var tagName = this.dataset.tag;
                this.classList.toggle('active');
                renderSections();
            });
        });
    }

    /* ── Tag Stats ─── */
    function renderTagStats() {
        var el = $('#tag-stats');
        if (!el) return;

        var tagCounts = {};
        deck.cards.forEach(function (card) {
            (card.tags || []).forEach(function (t) {
                tagCounts[t] = (tagCounts[t] || 0) + card.quantity;
            });
        });

        var entries = Object.keys(tagCounts);
        if (entries.length === 0) { el.innerHTML = ''; return; }
        entries.sort(function (a, b) { return tagCounts[b] - tagCounts[a]; });

        var html = '';
        entries.forEach(function (tagName, i) {
            var tag = findTagByName(tagName);
            var color = safeColor(tag ? tag.color : '#666');
            html += '<span class="tag-stat-badge" style="background:' + color + '20;color:' + color + ';border:1px solid ' + color + '40">' +
                escapeHtml(tagName) + ' ' + tagCounts[tagName] +
            '</span>';
        });

        el.innerHTML = html;
    }

    /* ── Rendering ─── */
    function renderAll() {
        renderCommander();
        renderTagStats();
        renderTagFilterBar();
        renderSections();
        renderCustomSections();
        renderOutline();
        renderStats();
        renderDeckCharts();
        renderGuidelineProgress();
        renderDeckCost();
        updateDeckNameInput();
        updateReviewTab();
        updateHistoryLinks();
        updatePricingStoreSelect();
        updateSectionIncludeCheckboxes();
        makeDeckCardRowsDraggable();
        initSectionDropZones();
        scheduleDeckAnalysis();
    }

    function analysisPayload() {
        return {
            name: deck.name,
            commander: deck.commander,
            cards: deck.cards,
            sections: deck.sections,
            guideline: deck.guideline,
            outline: deck.outline
        };
    }

    function scheduleDeckAnalysis() {
        if (analysisTimer) clearTimeout(analysisTimer);
        if (!deck.commander && (!deck.cards || deck.cards.length === 0)) {
            deckAnalysis = null;
            renderDeckAnalysis(null);
            return;
        }
        analysisTimer = setTimeout(runDeckAnalysis, 350);
    }

    function runDeckAnalysis() {
        var requestId = ++analysisRequestId;
        fetch('/api/deck/analyze', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ deck: analysisPayload() })
        })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (requestId !== analysisRequestId) return;
            if (data.error || !data.audit) return;
            deckAnalysis = data.audit;
            applyAnalysisRoles(data.audit);
            renderDeckAnalysis(data.audit);
            renderGuidelineProgress();
        })
        .catch(function () { /* analysis is advisory; keep the deck usable */ });
    }

    function applyAnalysisRoles(audit) {
        var byUuid = {};
        var byName = {};
        (audit.cardRoles || []).forEach(function (info) {
            if (info.uuid) byUuid[info.uuid] = info;
            if (info.name) byName[String(info.name).toLowerCase()] = info;
        });
        function apply(card) {
            if (!card) return;
            var info = (card.uuid && byUuid[card.uuid]) ||
                byName[String(card.name || '').toLowerCase()];
            if (!info) return;
            card.roles = info.roles || [];
            card.rolePrimary = info.primary || 'Core';
            card.rolesVersion = audit.rolesVersion || 1;
        }
        apply(deck.commander);
        (deck.cards || []).forEach(apply);
        Object.keys(deck.sections || {}).forEach(function (name) {
            (deck.sections[name] || []).forEach(apply);
        });
    }

    function getCardRoles(card) {
        if (card && card.roles && card.roles.length) return card.roles;
        return [classifyCard(card)];
    }

    function getPrimaryCategory(card) {
        if (card && card.rolePrimary) return card.rolePrimary;
        var roles = getCardRoles(card);
        var map = {
            'Lands': 'Lands', 'Ramp': 'Ramp', 'Fast Mana': 'Ramp',
            'Card Draw': 'Draw', 'Card Selection': 'Draw',
            'Targeted Removal': 'Removal', 'Board Wipe': 'Removal',
            'Counterspell': 'Removal'
        };
        for (var i = 0; i < roles.length; i++) {
            if (map[roles[i]]) return map[roles[i]];
        }
        return 'Core';
    }

    function renderDeckAnalysis(audit) {
        var panel = $('#deck-analysis');
        if (!panel) return;
        if (!audit) {
            panel.hidden = true;
            return;
        }
        panel.hidden = false;

        var health = audit.health || {};
        var healthEl = $('#deck-health-score');
        if (healthEl) healthEl.textContent = health.score == null ? '--' : health.score;

        var bracket = audit.bracket || {};
        var salt = bracket.salt || {};
        var badge = $('#deck-bracket-badge');
        if (badge) {
            var label = bracket.estimate == null ? 'Bracket unknown' : 'Est. bracket ' + bracket.estimate;
            if (bracket.confidence) label += ' · ' + bracket.confidence;
            if (salt.score != null) label += ' · Salt ' + salt.score;
            badge.textContent = label;
            badge.title = (bracket.signals || []).map(function (signal) {
                return signal.label + ': ' + signal.count;
            }).join('\n');
        }

        var rolesEl = $('#role-breakdown');
        if (rolesEl) {
            var gaps = audit.roleGaps || [];
            var html = '';
            gaps.forEach(function (gap) {
                var target = gap.target || 0;
                var actual = gap.actual || 0;
                var pct = target > 0 ? Math.min(100, Math.round(actual / target * 100)) : (actual ? 100 : 0);
                var cls = actual >= target ? 'surplus' : (actual >= target * 0.75 ? '' : 'under');
                html += '<div class="role-row" title="' + escapeAttr(gap.role + ': ' + actual + ' / ' + target) + '">' +
                    '<span class="role-row-label">' + escapeHtml(gap.role) + '</span>' +
                    '<div class="role-row-track"><div class="role-row-fill ' + cls + '" style="width:' + pct + '%"></div></div>' +
                    '<span class="role-row-count">' + actual + ' / ' + target + '</span>' +
                '</div>';
            });
            rolesEl.innerHTML = html;
        }

        var warnEl = $('#deck-analysis-warnings');
        if (warnEl) {
            var warnings = (audit.legality && audit.legality.errors || []).concat(audit.warnings || []);
            warnEl.innerHTML = warnings.slice(0, 4).map(function (warning) {
                return '<div>⚠ ' + escapeHtml(warning.message || String(warning)) + '</div>';
            }).join('');
        }
    }

    function renderCommander() {
        var slot = $('#commander-slot');
        if (!slot) return;
        if (deck.commander) {
            var c = deck.commander;
            var commanderCI = String(c.colorIdentity || '')
                .split(',')
                .map(function (ci) { return ci.trim().toUpperCase(); })
                .filter(function (ci) { return /^[WUBRG]$/.test(ci); })
                .map(function (ci) { return '<span class="ms ms-' + ci.toLowerCase() + '"></span>'; })
                .join('');
            slot.innerHTML =
                '<div class="commander-slot-filled">' +
                    '<img src="' + escapeAttr(c.imageUrl || '') + '" alt="' + escapeAttr(c.name || '') + '" class="commander-card-img">' +
                    '<div class="commander-card-info">' +
                        '<div class="commander-card-name">' + escapeHtml(c.name || '') + '</div>' +
                        '<div class="commander-card-type">' + escapeHtml(c.type || '') + '</div>' +
                        '<div class="commander-card-ci">Color Identity: ' + commanderCI + '</div>' +
                    '</div>' +
                    '<button class="commander-clear-btn" title="Remove commander">✕</button>' +
                '</div>';
            slot.querySelector('.commander-clear-btn').addEventListener('click', function () {
                deck.removeCard(c.uuid);
                renderAll();
                saveCurrentDeck();
            });
        } else {
            slot.innerHTML =
                '<div class="commander-slot-empty">' +
                    '<span class="commander-slot-label">COMMANDER</span>' +
                    '<div class="commander-search-wrap">' +
                        '<input type="text" id="commander-search-input" class="commander-search-input" placeholder="Search legendary creature…" autocomplete="off">' +
                        '<ul id="commander-autocomplete-list" class="autocomplete-list" hidden></ul>' +
                    '</div>' +
                    '<span class="commander-slot-hint">Search for a legendary creature or drag one here</span>' +
                '</div>';
            initCommanderAutocomplete();
        }
    }

    function renderSections() {
        var container = $('#deck-sections');
        var emptyState = $('#deck-empty-state');
        if (!container) return;

        var cardsToRender = deck.cards;

        if (deck.cards.length === 0) {
            container.innerHTML = '';
            if (emptyState) emptyState.style.display = '';
            return;
        }
        if (emptyState) emptyState.style.display = 'none';

        // Honor the active tag filter chips. Multiple selected tags use OR
        // (show cards carrying any selected tag).
        var activeTags = [];
        $$('#tag-filter-bar .tag-filter-chip.active').forEach(function (chip) {
            activeTags.push(chip.dataset.tag);
        });
        if (activeTags.length) {
            cardsToRender = cardsToRender.filter(function (card) {
                var tags = card.tags || [];
                return tags.some(function (tag) { return activeTags.indexOf(tag) !== -1; });
            });
        }

        // If all cards are filtered out, show a brief message
        if (cardsToRender.length === 0) {
            container.innerHTML = '<div class="deck-empty-filtered">No cards in deck match the active tags.</div>';
            return;
        }

        var html = '';

        if (currentGroupBy === 'tag') {
            // Group by tag
            var tagGroups = {};
            var untagged = [];
            cardsToRender.forEach(function (card) {
                var cardTags = card.tags || [];
                if (cardTags.length === 0) {
                    untagged.push(card);
                    return;
                }
                cardTags.forEach(function (tagName) {
                    if (!tagGroups[tagName]) tagGroups[tagName] = [];
                    if (tagGroups[tagName].indexOf(card) === -1) tagGroups[tagName].push(card);
                });
            });

            // Sort tag names by catalog order
            var tagOrder = tagCatalog.map(function (t) { return t.name; });
            var sortedTags = Object.keys(tagGroups).sort(function (a, b) {
                return tagOrder.indexOf(a) - tagOrder.indexOf(b);
            });
            if (untagged.length > 0) sortedTags.push('Untagged');

            sortedTags.forEach(function (tagName) {
                var cards = tagName === 'Untagged' ? untagged : tagGroups[tagName];
                sortCardsForDisplay(cards);
                var totalCount = cards.reduce(function (s, c) { return s + c.quantity; }, 0);
                var tag = findTagByName(tagName);
                var dot = tag ? '<span class="tag-section-dot" style="background:' + safeColor(tag.color) + '"></span>' : '';

                html += '<div class="deck-section" data-section="' + escapeAttr(tagName.toLowerCase()) + '">';
                html += '<div class="deck-section-header">' +
                    '<span class="deck-section-title">' + dot + escapeHtml(tagName) + '</span>' +
                    '<span class="deck-section-count">' + totalCount + '</span>' +
                    '<span class="deck-section-arrow">▼</span>' +
                '</div>';
                html += '<div class="deck-section-body">';
                cards.forEach(function (card) { html += renderCardRow(card); });
                html += '</div></div>';
            });
        } else {
            // Group by type (existing behavior)
            var groups = {};
            cardsToRender.forEach(function (card) {
                var sectionKey = getSectionKey(card);
                if (!groups[sectionKey]) groups[sectionKey] = [];
                groups[sectionKey].push(card);
            });

            var displayOrder = TYPE_ORDER.slice();
            displayOrder.push('Other');

            displayOrder.forEach(function (typeName) {
                var cards = groups[typeName];
                if (!cards || cards.length === 0) return;

                sortCardsForDisplay(cards);

                var totalCount = cards.reduce(function (s, c) { return s + c.quantity; }, 0);
                html += '<div class="deck-section" data-section="' + escapeAttr(typeName.toLowerCase()) + '">';
                html += '<div class="deck-section-header">' +
                    '<span class="deck-section-title">' + escapeHtml(typeName) + '</span>' +
                    '<span class="deck-section-count">' + totalCount + '</span>' +
                    '<span class="deck-section-arrow">▼</span>' +
                '</div>';
                html += '<div class="deck-section-body">';
                cards.forEach(function (card) {
                    html += renderCardRow(card);
                });
                html += '</div></div>';
            });
        }

        container.innerHTML = html;

        // Attach listeners
        attachCardRowListeners();
    }

    function attachCardRowListeners() {
        $$('#deck-sections .qty-btn').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var uuid = this.dataset.uuid;
                var delta = parseInt(this.dataset.delta, 10);
                var card = deck.cards.find(function (c) { return c.uuid === uuid; });
                if (!card) return;
                var newQty = (parseInt(card.quantity, 10) || 1) + delta;
                if (deck.setQuantity(uuid, newQty)) {
                    closeTagPopover();
                    renderAll();
                    saveCurrentDeck();
                } else {
                    showToast('Commander decks allow one copy (basic lands excepted), and 100 cards total.');
                }
            });
        });

        $$('#deck-sections .card-remove-btn').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var uuid = this.dataset.uuid;
                closeTagPopover();
                deck.removeCard(uuid);
                renderAll();
                saveCurrentDeck();
            });
        });

        $$('#deck-sections .card-tag-btn').forEach(function (btn) {
            btn.addEventListener('click', function (e) {
                e.stopPropagation();
                openTagPopover(this.dataset.uuid, e);
            });
        });
    }

    /* ── Custom Section Rendering ── */
    function renderCustomSections() {
        var container = $('#custom-sections');
        if (!container) return;
        if (!deck.sections) deck.sections = {};

        var sectionNames = Object.keys(deck.sections);
        if (sectionNames.length === 0) {
            container.innerHTML = '';
            return;
        }

        var html = '';
        sectionNames.forEach(function (sectionName) {
            var cards = (deck.sections[sectionName] || []).slice();
            sortCardsForDisplay(cards);
            var totalCount = cards.reduce(function (s, c) { return s + (c.quantity || 1); }, 0);

            html += '<div class="deck-section custom-deck-section" data-section-name="' + escapeAttr(sectionName) + '">';
            html += '<div class="deck-section-header custom-section-header">';
            html += '<span class="deck-section-title">' + escapeHtml(sectionName) + '</span>';
            html += '<span class="deck-section-count">' + totalCount + '</span>';
            // Include in cost checkbox
            var pSections = deck.pricingSections || {};
            var checked = pSections[sectionName] !== false ? ' checked' : '';
            html += '<label class="section-include-label" title="Include in deck cost"><input type="checkbox" class="section-include-cb" data-section="' + escapeAttr(sectionName) + '"' + checked + '> Cost</label>';
            html += '<button class="section-delete-btn" data-section="' + escapeAttr(sectionName) + '" title="Delete section">✕</button>';
            html += '<span class="deck-section-arrow">▼</span>';
            html += '</div>';

            html += '<div class="deck-section-body custom-section-body" data-section-name="' + escapeAttr(sectionName) + '">';
            if (cards.length === 0) {
                html += '<div class="section-empty-hint">Drop cards here to add to ' + escapeHtml(sectionName) + '</div>';
            } else {
                cards.forEach(function (card) {
                    html += renderSectionCardRow(card, sectionName);
                });
            }
            html += '</div></div>';
        });

        container.innerHTML = html;

        attachSectionCardRowListeners();
        attachSectionDeleteListeners();
        attachSectionIncludeListeners();
    }

    function renderSectionCardRow(card, sectionName) {
        // Like renderCardRow but with section-aware data attributes
        var isOffColor = isOffColorCard(card);
        var offColorClass = isOffColor ? ' card-off-color' : '';

        var tagBadges = '';
        var cardTags = card.tags || [];
        if (cardTags.length > 0) {
            tagBadges = '<span class="card-tags">';
            cardTags.forEach(function (t) {
                var tag = findTagByName(t);
                var color = safeColor(tag ? tag.color : '#666');
                tagBadges += '<span class="card-tag-dot" style="background:' + color + '" title="' + escapeAttr(t) + '"></span>';
            });
            tagBadges += '</span>';
        }

        return '<div class="deck-card-row' + offColorClass + '" data-uuid="' + escapeAttr(card.uuid) + '" data-set-code="' + escapeAttr(card.setCode) + '" data-number="' + escapeAttr(card.number) + '" data-image-url="' + escapeAttr(card.imageUrl || '') + '" data-section-name="' + escapeAttr(sectionName) + '">' +
            '<button class="qty-btn qty-plus" data-uuid="' + escapeAttr(card.uuid) + '" data-section="' + escapeAttr(sectionName) + '" data-delta="1" title="Increase quantity">+</button>' +
            '<button class="qty-btn qty-minus" data-uuid="' + escapeAttr(card.uuid) + '" data-section="' + escapeAttr(sectionName) + '" data-delta="-1" title="Decrease quantity">−</button>' +
            '<span class="card-quantity">' + card.quantity + '×</span>' +
            '<span class="card-note-indicator' + (card.note ? ' has-note' : '') + '" title="' + escapeAttr(card.note || 'No note') + '">📝</span>' +
            '<span class="card-name-text" title="' + escapeAttr(card.name) + '">' + escapeHtml(card.name) + '</span>' +
            '<span class="card-mana-cost">' + renderManaSymbols(card.manaCost || '') + '</span>' +
            '<span class="card-price">' + formatPrice(getCardPrice(card)) + '</span>' +
            tagBadges +
            '<button class="card-tag-btn" data-uuid="' + escapeAttr(card.uuid) + '" data-section="' + escapeAttr(sectionName) + '" title="Edit tags">🏷</button>' +
            '<button class="card-remove-btn" data-uuid="' + escapeAttr(card.uuid) + '" data-section="' + escapeAttr(sectionName) + '" title="Remove">✕</button>' +
        '</div>';
    }

    function attachSectionCardRowListeners() {
        $$('#custom-sections .qty-btn').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var uuid = this.dataset.uuid;
                var sectionName = this.dataset.section;
                var delta = parseInt(this.dataset.delta, 10);
                var section = deck.sections[sectionName];
                if (!section) return;
                var card = section.find(function (c) { return c.uuid === uuid; });
                if (!card) return;
                var newQty = (parseInt(card.quantity, 10) || 1) + delta;
                if (deck.setSectionQuantity(sectionName, uuid, newQty)) {
                    closeTagPopover();
                    renderAll();
                    saveCurrentDeck();
                }
            });
        });

        $$('#custom-sections .card-remove-btn').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var uuid = this.dataset.uuid;
                var sectionName = this.dataset.section;
                closeTagPopover();
                deck.removeFromSection(sectionName, uuid);
                renderAll();
                saveCurrentDeck();
            });
        });

        $$('#custom-sections .card-tag-btn').forEach(function (btn) {
            btn.addEventListener('click', function (e) {
                e.stopPropagation();
                var uuid = this.dataset.uuid;
                var sectionName = this.dataset.section;
                // Cards in sections share the same tag system — look them up by uuid across both
                var card = deck.cards.find(function (c) { return c.uuid === uuid; });
                if (!card && deck.sections[sectionName]) {
                    card = deck.sections[sectionName].find(function (c) { return c.uuid === uuid; });
                }
                if (card) {
                    // Temporarily expose so openTagPopover can find it
                    openTagPopoverForSection(uuid, sectionName, e);
                }
            });
        });
    }

    function attachSectionDeleteListeners() {
        $$('#custom-sections .section-delete-btn').forEach(function (btn) {
            btn.addEventListener('click', function (e) {
                e.stopPropagation();
                var sectionName = this.dataset.section;
                if (!confirm('Delete section "' + sectionName + '" and all its cards?')) return;
                deck.deleteSection(sectionName);
                renderAll();
                saveCurrentDeck();
                showToast('Section "' + sectionName + '" deleted.');
            });
        });
    }

    function attachSectionIncludeListeners() {
        $$('#custom-sections .section-include-cb').forEach(function (cb) {
            cb.addEventListener('click', function (e) {
                e.stopPropagation();
            });
            cb.addEventListener('change', function () {
                if (!deck.pricingSections) deck.pricingSections = {};
                deck.pricingSections[this.dataset.section] = this.checked;
                saveCurrentDeck();
                renderDeckCost();
            });
        });
    }

    function openTagPopoverForSection(uuid, sectionName, event) {
        var card = deck.sections[sectionName] ? deck.sections[sectionName].find(function (c) { return c.uuid === uuid; }) : null;
        if (!card) return;
        closeTagPopover();

        activePopoverCard = uuid;

        var popover = document.createElement('div');
        popover.className = 'tag-popover';
        popover.id = 'tag-popover';

        var html = '<div class="tag-popover-header">' + escapeHtml(card.name) + '</div>';
        html += '<div class="tag-popover-list">';

        tagCatalog.forEach(function (tag) {
            var checked = (card.tags || []).indexOf(tag.name) !== -1 ? ' checked' : '';
            html += '<label class="tag-option">' +
                '<input type="checkbox" class="tag-cb" data-tag="' + escapeAttr(tag.name) + '"' + checked + '>' +
                '<span class="tag-option-dot" style="background:' + safeColor(tag.color) + '"></span>' +
                '<span class="tag-option-name">' + escapeHtml(tag.name) + '</span>' +
            '</label>';
        });

        html += '</div>';
        html += '<div class="tag-popover-add">' +
            '<input type="text" class="tag-popover-input" placeholder="New tag…" maxlength="24">' +
            '<button class="tag-popover-add-btn">+</button>' +
        '</div>';

        popover.innerHTML = html;
        document.body.appendChild(popover);

        var btn = event.target.closest('.card-tag-btn');
        if (btn) {
            var rect = btn.getBoundingClientRect();
            popover.style.position = 'fixed';
            popover.style.top = (rect.bottom + 4) + 'px';
            popover.style.left = Math.min(rect.left, window.innerWidth - 250) + 'px';
        }

        popover.querySelectorAll('.tag-cb').forEach(function (cb) {
            cb.addEventListener('change', function () {
                var tagName = this.dataset.tag;
                if (this.checked) {
                    if (card.tags.indexOf(tagName) === -1) card.tags.push(tagName);
                } else {
                    var idx = card.tags.indexOf(tagName);
                    if (idx !== -1) card.tags.splice(idx, 1);
                }
                renderTagFilterBar();
                renderSections();
                renderCustomSections();
                renderTagStats();
                saveCurrentDeck();
            });
        });

        var addInput = popover.querySelector('.tag-popover-input');
        var addBtn = popover.querySelector('.tag-popover-add-btn');

        function submitNewTag() {
            var name = addInput.value.trim();
            if (!name) return;
            if (findTagByName(name)) {
                addInput.value = '';
                return;
            }
            addCustomTag(name);
            var idx = card.tags.indexOf(name);
            if (idx === -1) card.tags.push(name);
            openTagPopoverForSection(uuid, sectionName, event);
            renderTagFilterBar();
            renderSections();
            renderCustomSections();
            renderTagStats();
            saveCurrentDeck();
        }

        addBtn.addEventListener('click', submitNewTag);
        addInput.addEventListener('keydown', function (e) {
            if (e.key === 'Enter') { e.preventDefault(); submitNewTag(); }
        });

        setTimeout(function () {
            document.addEventListener('click', closeTagPopoverOnOutside);
        }, 0);
    }

    function renderCardRow(card) {
        var isOffColor = isOffColorCard(card);
        var offColorClass = isOffColor ? ' card-off-color' : '';

        // Tag badges
        var tagBadges = '';
        var cardTags = card.tags || [];
        if (cardTags.length > 0) {
            tagBadges = '<span class="card-tags">';
            cardTags.forEach(function (t) {
                var tag = findTagByName(t);
                var color = safeColor(tag ? tag.color : '#666');
                tagBadges += '<span class="card-tag-dot" style="background:' + color + '" title="' + escapeAttr(t) + '"></span>';
            });
            tagBadges += '</span>';
        }

        return '<div class="deck-card-row' + offColorClass + '" data-uuid="' + escapeAttr(card.uuid) + '" data-set-code="' + escapeAttr(card.setCode) + '" data-number="' + escapeAttr(card.number) + '" data-image-url="' + escapeAttr(card.imageUrl || '') + '">' +
            '<button class="qty-btn qty-plus" data-uuid="' + escapeAttr(card.uuid) + '" data-delta="1" title="Increase quantity">+</button>' +
            '<button class="qty-btn qty-minus" data-uuid="' + escapeAttr(card.uuid) + '" data-delta="-1" title="Decrease quantity">−</button>' +
            '<span class="card-quantity">' + card.quantity + '×</span>' +
            (shouldShowQuantityWarning(card) ? '<span class="qty-warn-icon" title="Exceeds singleton limit">⚠</span>' : '') +
            '<span class="card-note-indicator' + (card.note ? ' has-note' : '') + '" title="' + escapeAttr(card.note || 'No note') + '">📝</span>' +
            '<span class="card-name-text" title="' + escapeAttr(card.name) + '">' + escapeHtml(card.name) + '</span>' +
            '<span class="card-mana-cost">' + renderManaSymbols(card.manaCost || '') + '</span>' +
            '<span class="card-price">' + formatPrice(getCardPrice(card)) + '</span>' +
            tagBadges +
            '<button class="card-tag-btn" data-uuid="' + escapeAttr(card.uuid) + '" title="Edit tags">🏷</button>' +
            '<button class="card-remove-btn" data-uuid="' + escapeAttr(card.uuid) + '" title="Remove">✕</button>' +
        '</div>';
    }

    function getSectionKey(card) {
        var types = parseTypes(card.types);
        for (var i = 0; i < TYPE_ORDER.length; i++) {
            if (types.indexOf(TYPE_ORDER[i].toLowerCase()) !== -1) return TYPE_ORDER[i];
        }
        return 'Other';
    }

    function sortCardsForDisplay(cards) {
        switch (currentSort) {
            case 'mv':
                cards.sort(function (a, b) {
                    var mvA = a.manaValue || 99, mvB = b.manaValue || 99;
                    if (mvA !== mvB) return mvA - mvB;
                    return a.name.localeCompare(b.name);
                });
                break;
            default: // 'name'
                cards.sort(function (a, b) {
                    return a.name.localeCompare(b.name);
                });
                break;
        }
    }

    function shouldShowQuantityWarning(card) {
        // Show warning only when quantity > 1 (singleton format).
        if (card.quantity <= 1) return false;
        // Basic lands and explicit "any number" cards are exempt.
        return !isSingletonExempt(card);
    }

    function isOffColorCard(card) {
        if (!deck.commander) return false;
        var ci = deck.getCommanderCI();
        // A colorless commander has an empty CI; that is not the same as "no
        // commander". Any colored card is then off-color.
        if (!ci) return !!card.colorIdentity;
        if (!card.colorIdentity) return false;
        // colorIdentity is comma-separated like "G, R, U" — parse it properly
        var cmdCI = ci.split(',').map(function (c) { return c.trim(); }).filter(Boolean);
        var cardColors = card.colorIdentity.split(',').map(function (c) { return c.trim(); }).filter(Boolean);
        for (var i = 0; i < cardColors.length; i++) {
            if (cmdCI.indexOf(cardColors[i]) === -1) {
                return true;
            }
        }
        return false;
    }

    function renderStats() {
        var totalEl = $('#deck-total-count');
        var total = deck.getTotalCount();
        if (totalEl) totalEl.textContent = total;

        // Over-100 warning
        var warnEl = $('#deck-overcount-warn');
        if (warnEl) warnEl.hidden = total <= 100;

        // Commander label
        var cmdLabel = $('#deck-commander-label');
        if (cmdLabel) {
            cmdLabel.textContent = deck.commander ? 'Commander: ' + deck.commander.name : '';
        }

        renderManaCurve();
        renderColorBreakdown();
        renderCIStatus();
    }

    function renderManaCurve() {
        var container = $('#deck-mana-curve');
        if (!container) return;

        var buckets = {};
        for (var i = 0; i <= 10; i++) buckets[i] = 0;
        buckets['10+'] = 0;

        deck.cards.forEach(function (card) {
            var mv = card.manaValue;
            if (mv === undefined || mv === null) return;
            if (card.types && card.types.indexOf('Land') !== -1) return;
            if (mv >= 10) buckets['10+'] += card.quantity;
            else buckets[mv] += card.quantity;
        });

        // Commander doesn't count in mana curve (it's in command zone)

        var maxVal = 0;
        Object.keys(buckets).forEach(function (k) { if (buckets[k] > maxVal) maxVal = buckets[k]; });

        var barW = 14, barGap = 2, barH = 38;
        var totalW = Object.keys(buckets).length * (barW + barGap) + barGap;

        var svg = '<svg width="' + totalW + '" height="' + (barH + 14) + '" viewBox="0 0 ' + totalW + ' ' + (barH + 14) + '">';
        var labels = ['0','1','2','3','4','5','6','7','8','9','10+'];
        labels.forEach(function (label, i) {
            var val = buckets[i];
            var h = maxVal > 0 ? Math.round((val / maxVal) * barH) : 0;
            var x = i * (barW + barGap) + barGap;
            var y = barH - h;
            svg += '<rect x="' + x + '" y="' + y + '" width="' + barW + '" height="' + h + '" rx="2" fill="var(--accent)" opacity="' + (h > 0 ? '0.8' : '0.2') + '"></rect>';
            svg += '<text x="' + (x + barW / 2) + '" y="' + (barH + 11) + '" text-anchor="middle" fill="var(--text-dim)" font-size="8">' + label + '</text>';
            if (val > 0) {
                svg += '<text x="' + (x + barW / 2) + '" y="' + (y - 2) + '" text-anchor="middle" fill="var(--text)" font-size="7">' + val + '</text>';
            }
        });
        svg += '</svg>';

        container.innerHTML = svg;
    }

    function renderColorBreakdown() {
        var container = $('#deck-color-breakdown');
        if (!container) return;

        var colorCounts = { W: 0, U: 0, B: 0, R: 0, G: 0 };
        var colorNames = { W: 'White', U: 'Blue', B: 'Black', R: 'Red', G: 'Green' };
        var colorHex = { W: '#f9faf4', U: '#0e68ab', B: '#b38e5d', R: '#d3202a', G: '#00733e' };
        var total = 0;

        deck.cards.forEach(function (card) {
            // colors is comma-separated like "U, R, G"
            (card.colors || '').split(',').forEach(function (c) {
                c = c.trim();
                if (colorCounts.hasOwnProperty(c)) {
                    colorCounts[c] += card.quantity;
                    total += card.quantity;
                }
            });
        });

        if (total === 0) {
            container.innerHTML = '';
            return;
        }

        var html = '<span style="font-size:0.75rem;color:var(--text-dim)">Colors: </span>';
        CARD_COLORS.split('').forEach(function (c) {
            if (colorCounts[c] > 0) {
                var pct = Math.round(colorCounts[c] / total * 100);
                html += '<span class="color-bar-label" style="color:' + colorHex[c] + '">' +
                    '<span class="ms ms-' + c.toLowerCase() + '"></span> ' + colorCounts[c] +
                '</span> ';
            }
        });

        container.innerHTML = html;
    }

    function renderCIStatus() {
        var container = $('#deck-ci-status');
        if (!container) return;

        if (!deck.commander) {
            container.innerHTML = '<span style="color:var(--text-dim);font-size:0.75rem;">Set a commander to check color identity</span>';
            return;
        }
        var offColorCards = deck.cards.filter(isOffColorCard);
        if (offColorCards.length > 0) {
            var names = offColorCards.map(function (c) { return escapeHtml(c.name || ''); }).join(', ');
            container.innerHTML = '<span style="color:#f85149;font-size:0.75rem;">⚠ Off-color: ' + names + '</span>';
        } else {
            container.innerHTML = '<span style="color:#3fb950;font-size:0.75rem;">✓ All cards within color identity</span>';
        }
    }

    /* ─── Guideline Progress Bars ─── */
    var cachedGuidelineTemplates = [];
    var categorizedCards = {}; // { Lands: [card, ...], Ramp: [...], ... }

    function loadGuidelineList() {
        fetch('/api/template/list?type=guideline')
            .then(function(r) { return r.json(); })
            .then(function(templates) {
                cachedGuidelineTemplates = templates || [];
                // If no guidelines exist yet, auto-create the default one
                if (cachedGuidelineTemplates.length === 0) {
                    var defaultCategories = { Lands: 38, Ramp: 10, Draw: 10, Removal: 12, Core: 30 };
                    return fetch('/api/template/save', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ type: 'guideline', name: 'Default Guideline', categories: defaultCategories }),
                    }).then(function(r) { return r.json(); }).then(function() {
                        cachedGuidelineTemplates = [{ type: 'guideline', name: 'Default Guideline', categories: defaultCategories }];
                        populateGuidelineSelects();
                        renderGuidelineProgress();
                    });
                }
                populateGuidelineSelects();
                // Deck may have loaded before guidelines did; re-render
                // progress bars now that the template data is available.
                renderGuidelineProgress();
            })
            .catch(function() { /* offline */ });
    }

    function populateGuidelineSelects() {
        var html = '<option value="">None</option>';
        cachedGuidelineTemplates.forEach(function(t) {
            html += '<option value="' + escapeAttr(t.name) + '">' + escapeHtml(t.name) + '</option>';
        });
        var headerSelect = $('#deck-guideline-select');
        var tplSelect = $('#deck-template-guideline-select');
        if (headerSelect) {
            headerSelect.innerHTML = html;
            if (deck.guideline) headerSelect.value = deck.guideline;
        }
        if (tplSelect) {
            tplSelect.innerHTML = html;
            if (deck.guideline) tplSelect.value = deck.guideline;
        }
    }

    function renderGuidelineProgress() {
        var panel = $('#guideline-panel');
        var barsEl = $('#guideline-bars');
        var nameEl = $('#guideline-name');
        if (!panel || !barsEl) return;

        if (!deck.guideline) {
            panel.hidden = true;
            categorizedCards = {};
            return;
        }

        // Find the guideline template in cache
        var guideline = null;
        for (var i = 0; i < cachedGuidelineTemplates.length; i++) {
            if (cachedGuidelineTemplates[i].name === deck.guideline) {
                guideline = cachedGuidelineTemplates[i];
                break;
            }
        }
        if (!guideline || !guideline.categories) {
            panel.hidden = true;
            categorizedCards = {};
            return;
        }

        panel.hidden = false;
        if (nameEl) nameEl.textContent = guideline.name;

        var targets = guideline.categories;
        var counts = { Lands: 0, Ramp: 0, Draw: 0, Removal: 0, Core: 0 };
        categorizedCards = { Lands: [], Ramp: [], Draw: [], Removal: [], Core: [] };

        // Classify all cards in the main deck (not sections, not commander)
        deck.cards.forEach(function(card) {
            var cat = getPrimaryCategory(card);
            if (counts.hasOwnProperty(cat)) {
                counts[cat] += card.quantity || 1;
            }
            if (categorizedCards[cat]) {
                categorizedCards[cat].push(card);
            }
        });

        var order = ['Lands', 'Ramp', 'Draw', 'Removal', 'Core'];
        var html = '';
        order.forEach(function(cat) {
            var actual = counts[cat] || 0;
            var target = targets[cat] || 0;
            var pct = target > 0 ? Math.min(100, Math.round((actual / target) * 100)) : (actual > 0 ? 100 : 0);
            var cls = actual >= target ? 'at' : (actual >= target * 0.75 ? 'over' : 'under');
            html += '<div class="guideline-bar-row guideline-bar-clickable" data-cat="' + escapeAttr(cat) + '" title="Click to view ' + escapeAttr(cat) + ' cards">' +
                '<span class="guideline-bar-label">' + escapeHtml(cat) + '</span>' +
                '<div class="guideline-bar-track">' +
                    '<div class="guideline-bar-fill ' + cls + '" style="width:' + pct + '%"></div>' +
                '</div>' +
                '<span class="guideline-bar-count">' + actual + ' / ' + target + '</span>' +
            '</div>';
        });
        barsEl.innerHTML = html;

        // Attach click handlers to open category breakdown modal
        barsEl.querySelectorAll('.guideline-bar-row').forEach(function(row) {
            row.addEventListener('click', function() {
                openCategoryModal(this.dataset.cat);
            });
        });
    }

    /* ─── Category Breakdown Modal ─── */
    function openCategoryModal(cat) {
        var overlay = $('#category-modal-overlay');
        var title = $('#category-modal-title');
        var body = $('#category-modal-body');
        if (!overlay || !body) return;

        if (title) title.textContent = cat + ' Cards';
        overlay.hidden = false;
        overlay.classList.add('open');

        var cards = categorizedCards[cat] || [];
        if (cards.length === 0) {
            body.innerHTML = '<p style="color:var(--text-dim);text-align:center;padding:1rem;">No cards in this category.</p>';
            return;
        }

        // Sort by name
        cards.sort(function(a, b) { return a.name.localeCompare(b.name); });

        var html = '';
        cards.forEach(function(card) {
            var qty = card.quantity || 1;
            var manaStr = card.manaCost ? '<span class="card-mana-cost" style="font-size:0.8rem;">' + renderManaSymbols(card.manaCost) + '</span>' : '';
            html += '<div class="category-card-row">' +
                '<span class="category-card-qty">' + qty + '×</span>' +
                '<span class="category-card-name">' + escapeHtml(card.name) + '</span>' +
                manaStr +
            '</div>';
        });
        body.innerHTML = html;

        // Close handler
        var closeBtn = $('#category-modal-close');
        if (closeBtn) {
            closeBtn.onclick = function() {
                overlay.hidden = true;
                overlay.classList.remove('open');
            };
        }
        overlay.onclick = function(e) {
            if (e.target === overlay) {
                overlay.hidden = true;
                overlay.classList.remove('open');
            }
        };
    }

    /* ─── Deck Panel Charts ─── */
    function renderDeckCharts() {
        var chartsEl = $('#deck-charts');
        if (!chartsEl) return;

        var hasCards = deck.cards.length > 0;
        chartsEl.hidden = !hasCards;
        if (!hasCards) return;

        renderManaCurveChart();
        renderPipCounter();
    }

    /* Mana Curve — vertical bar chart in SVG */
    function renderManaCurveChart() {
        var container = $('#deck-chart-mana-body');
        if (!container) return;

        var buckets = {};
        for (var i = 0; i <= 9; i++) buckets[i] = 0;
        buckets['10+'] = 0;

        var total = 0;
        deck.cards.forEach(function (card) {
            var mv = card.manaValue;
            if (mv === undefined || mv === null) return;
            // Exclude lands — they have no mana cost
            if (card.types && card.types.indexOf('Land') !== -1) return;
            var qty = card.quantity || 1;
            if (mv >= 10) { buckets['10+'] += qty; }
            else { buckets[mv] += qty; }
            total += qty;
        });

        if (total === 0) { container.innerHTML = ''; return; }

        var labels = ['0','1','2','3','4','5','6','7','8','9','10+'];
        var maxVal = 0;
        labels.forEach(function (l) { if (buckets[l] > maxVal) maxVal = buckets[l]; });
        if (maxVal === 0) { container.innerHTML = ''; return; }

        var chartH = 98, padT = 14, padB = 18, padL = 24, padR = 6;
        var plotH = chartH - padT - padB;
        var barCount = labels.length;
        var barGap = 2;
        var barW = 11;
        var plotW = barCount * (barW + barGap) + barGap;
        var totalW = plotW + padL + padR;

        var svg = '<svg viewBox="0 0 ' + totalW + ' ' + chartH + '" width="' + totalW + '" height="' + chartH + '">';
        svg += '<defs><linearGradient id="mana-bar-grad" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#58a6ff"/><stop offset="100%" stop-color="#58a6ff" stop-opacity="0.5"/></linearGradient></defs>';

        // Grid lines
        var gridLines = 4;
        for (var gl = 0; gl <= gridLines; gl++) {
            var gy = padT + Math.round(plotH * (1 - gl / gridLines));
            svg += '<line x1="' + padL + '" y1="' + gy + '" x2="' + (totalW - padR) + '" y2="' + gy + '" stroke="var(--border)" stroke-width="0.5" stroke-dasharray="2,2"/>';
        }

        // Bars
        labels.forEach(function (label, i) {
            var val = buckets[label];
            var h = Math.round((val / maxVal) * plotH);
            var x = padL + i * (barW + barGap) + barGap;
            var y = padT + plotH - h;
            svg += '<rect x="' + x + '" y="' + y + '" width="' + barW + '" height="' + h + '" rx="2" fill="url(#mana-bar-grad)" data-count="' + val + '" data-mv="' + label + '" opacity="0.85"/>';
            // X-axis label
            svg += '<text x="' + (x + barW / 2) + '" y="' + (chartH - 4) + '" text-anchor="middle" fill="var(--text-dim)" font-size="7" font-family="inherit">' + label + '</text>';
            // Value label on top
            if (val > 0) {
                svg += '<text x="' + (x + barW / 2) + '" y="' + (y - 3) + '" text-anchor="middle" fill="var(--text)" font-size="7" font-family="inherit" font-weight="600">' + val + '</text>';
            }
        });

        svg += '</svg>';
        container.innerHTML = svg;
    }

    /* Pip Counter — counts mana symbols in mana costs */
    function renderPipCounter() {
        var container = $('#deck-chart-pips-body');
        if (!container) return;

        var colorHex = { W: '#f9faf4', U: '#0e68ab', B: '#b38e5d', R: '#d3202a', G: '#00733e', C: '#a0a0a0' };
        var colorNames = { W: 'White', U: 'Blue', B: 'Black', R: 'Red', G: 'Green', C: 'Colorless' };
        var order = ['W','U','B','R','G','C'];

        // Count colored and colorless mana pips from manaCost strings, weighted by quantity
        var pipCounts = { W: 0, U: 0, B: 0, R: 0, G: 0, C: 0 };
        var totalPips = 0;

        deck.cards.forEach(function (card) {
            var qty = card.quantity || 1;
            var cost = card.manaCost || '';
            var matches = cost.match(/\{([^}]+)\}/g);
            if (!matches) return;
            matches.forEach(function (sym) {
                var inner = sym.replace(/[{}]/g, '');

                // Direct color/colorless match: {W}, {U}, {B}, {R}, {G}, {C}
                if (pipCounts.hasOwnProperty(inner)) {
                    pipCounts[inner] += qty;
                    totalPips += qty;
                    return;
                }

                // Hybrid like {W/U}, {2/W} — count each colored half
                // Phyrexian like {W/P}, {B/P} — count the color half
                // Skip generic halves ({2}, {X}, numeric) — only count colored/colorless pips
                if (inner.indexOf('/') !== -1) {
                    inner.split('/').forEach(function (p) {
                        if (pipCounts.hasOwnProperty(p)) {
                            pipCounts[p] += qty;
                            totalPips += qty;
                        }
                    });
                }
            });
        });

        if (totalPips === 0) { container.innerHTML = ''; return; }

        var chartH = 128, padT = 3, padB = 3, padL = 56, padR = 30;
        var plotW = 140;
        var totalW = padL + plotW + padR;
        var rowH = Math.floor((chartH - padT - padB) / order.length);
        var barH = rowH - 3;

        var maxPips = 0;
        order.forEach(function (c) { if (pipCounts[c] > maxPips) maxPips = pipCounts[c]; });

        var svg = '<svg viewBox="0 0 ' + totalW + ' ' + chartH + '" width="' + totalW + '" height="' + chartH + '">';

        order.forEach(function (c, i) {
            var val = pipCounts[c];
            var rowY = padT + i * rowH;
            var barW = maxPips > 0 ? Math.max(val > 0 ? 3 : 0, Math.round((val / maxPips) * plotW)) : 0;

            svg += '<text x="' + (padL - 5) + '" y="' + (rowY + rowH / 2 + 4) + '" text-anchor="end" fill="var(--text-dim)" font-size="9" font-family="inherit">' + colorNames[c] + '</text>';

            if (barW > 0) {
                svg += '<rect x="' + padL + '" y="' + (rowY + 1) + '" width="' + barW + '" height="' + barH + '" rx="2" fill="' + colorHex[c] + '" opacity="0.85"/>';
                svg += '<text x="' + (padL + barW + 4) + '" y="' + (rowY + rowH / 2 + 4) + '" fill="var(--text)" font-size="9" font-family="inherit" font-weight="600">' + val + '</text>';
            }
        });

        svg += '</svg>';
        container.innerHTML = svg;
    }

    /* ─── Mana Symbol Rendering ─── */
    function renderManaSymbols(manaCost) {
        if (!manaCost) return '';
        return String(manaCost).replace(/\{([^}]+)\}/g, function (match, symbol) {
            var raw = String(symbol).trim();
            var lower = raw.toLowerCase();
            var cls = null;

            // mana-font uses compact class names: {W/U} -> ms-wu,
            // {2/W} -> ms-2w, {W/P} -> ms-wp. Do not leave slashes in the
            // class or emit the old ms-w-u/ms-w-p names (no glyphs exist).
            if (lower.indexOf('/') !== -1) {
                var parts = lower.split('/');
                if (parts.length === 2) {
                    var a = parts[0], b = parts[1];
                    if (/^[wubrgc]$/.test(a) && /^[wubrgc]$/.test(b)) {
                        cls = a + b;                 // hybrid
                    } else if (/^\d+$/.test(a) && /^[wubrgc]$/.test(b)) {
                        cls = a + b;                 // generic/color hybrid
                    } else if (/^[wubrgc]$/.test(a) && b === 'p') {
                        cls = a + 'p';               // phyrexian
                    }
                }
            } else {
                var special = {
                    't': 'tap', 'q': 'untap', 's': 's', 'e': 'e',
                    'pw': 'pw', 'x': 'x', 'y': 'y', 'z': 'z',
                    'p': 'p', 'infinity': 'infinity', '∞': 'infinity'
                };
                if (special.hasOwnProperty(lower)) {
                    cls = special[lower];
                } else if (/^[wubrgc]$/.test(lower) || /^\d+$/.test(lower)) {
                    cls = lower;
                }
            }

            if (!cls || !/^[a-z0-9]+$/.test(cls)) {
                // Unknown symbol: render it as text instead of injecting it
                // into an HTML class/attribute.
                return escapeHtml(match);
            }
            return '<span class="ms ms-' + cls + '"></span>';
        });
    }

    function renderManaChips(manaCost) {
        /* Self-contained colored mana chips (no mana-font dependency) for the
           presentation export. {2}{W}{U} -> colored "2","W","U" badges. */
        if (!manaCost) return '';
        return manaCost.replace(/\{([^}]+)\}/g, function (match, symbol) {
            var cls = symbol.toLowerCase();
            // Hybrid {W/U} and phyrexian {W/P} collapse to a neutral chip
            // colored by their first color symbol.
            if (symbol.indexOf('/') !== -1) {
                cls = symbol.charAt(0).toLowerCase();
            }
            if (!/^[a-z0-9]+$/.test(cls)) cls = 'c';
            return '<span class="mc mc-' + cls + '">' + escapeHtml(symbol) + '</span>';
        });
    }

    /* ─── Utilities ─── */
    function copyTextToClipboard(text, onDone) {
        // ponytail: prefer the async Clipboard API, fall back to a hidden
        // textarea + execCommand for non-HTTPS self-hosted installs.
        function done() { if (onDone) onDone(); }
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(text).then(done, function () { legacyCopy(); });
            return;
        }
        legacyCopy();

        function legacyCopy() {
            var ta = document.createElement('textarea');
            ta.value = text;
            ta.setAttribute('readonly', '');
            ta.style.position = 'fixed';
            ta.style.left = '-9999px';
            document.body.appendChild(ta);
            ta.select();
            try { document.execCommand('copy'); } catch (e) { /* ignore */ }
            document.body.removeChild(ta);
            done();
        }
    }

    function _fetchAsDataURI(url) {
        return fetch(url).then(function (r) {
            if (!r.ok) throw new Error('HTTP ' + r.status);
            return r.blob();
        }).then(function (blob) {
            return new Promise(function (resolve, reject) {
                var reader = new FileReader();
                reader.onload = function () { resolve(reader.result); };
                reader.onerror = function () { reject(new Error('read failed')); };
                reader.readAsDataURL(blob);
            });
        });
    }

    function escapeHtml(str) {
        // Pure string escape — equivalent to textContent → innerHTML for text
        // content (escapes & < >; quotes are left alone, escapeAttr handles
        // attribute contexts). Pure so it works in the QuickJS test harness.
        return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    function escapeAttr(str) {
        return String(str).replace(/"/g, '&quot;').replace(/'/g, '&#39;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    function safeColor(value) {
        // Tag colors come from user-editable JSON; only allow CSS hex colors.
        var color = String(value || '');
        return /^#[0-9a-fA-F]{3,8}$/.test(color) ? color : '#666';
    }

    function saveCurrentDeck() {
        if (!deck.name) {
            updateSaveStatus('unsaved', 'Name your deck to save');
            return;
        }
        needsSave = true;
        // Flush history events immediately (fire-and-forget)
        flushHistory();
        // Debounce saves — 600ms cooldown between writes
        if (saveTimer) clearTimeout(saveTimer);
        updateSaveStatus('saving', 'Saving…');
        saveTimer = setTimeout(function () {
            saveDeckToServer(deck);
        }, 600);
    }

    function updateHistoryBtnVisibility() {
        var btn = $('#deck-history-delete-btn');
        if (btn) btn.style.display = deck.name ? '' : 'none';
    }

    function updateHistoryLinks() {
        var link = $('#history-compare-link');
        if (!link) return;
        if (deck.name) {
            link.href = '/compare-decks?a=' + encodeURIComponent(deck.name);
        } else {
            link.href = '/compare-decks';
        }
    }

    function updateReviewTab() {
        var link = $('#deck-review-link');
        var hint = $('#deck-review-hint');
        if (!link) return;
        if (deck.name) {
            link.href = '/deck-review?deck=' + encodeURIComponent(deck.name);
            if (hint) hint.textContent = 'Review "' + deck.name + '" with the deterministic audit, candidate cards, and optional LLM report.';
        } else {
            link.href = '/deck-review';
            if (hint) hint.textContent = 'Name and save the deck to open a review tied to its saved list.';
        }
    }

    function updateDeckNameInput() {
        var input = $('#deck-name');
        if (input && input !== document.activeElement) {
            input.value = deck.name;
            input.classList.toggle('unnamed', !deck.name);
        }
        updateHistoryBtnVisibility();
        if (deck.name) {
            updateSaveStatus('saved', 'Saved');
        } else {
            updateSaveStatus('unsaved', 'Name your deck to save');
        }
    }

    function toggleSection(header) {
        var body = header.nextElementSibling;
        var arrow = header.querySelector('.deck-section-arrow');
        if (body) {
            var hidden = body.style.display === 'none';
            body.style.display = hidden ? '' : 'none';
            if (arrow) arrow.textContent = hidden ? '▼' : '▶';
        }
    }

    /* ─── Card Lookup (server API) ─── */
    function lookupCardBySetNumber(setCode, number, callback) {
        fetch('/api/deck/card-lookup', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ setCode: setCode, number: number }),
        })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (data.error) { callback(null, data.error); return; }
            callback(data, null);
        })
        .catch(function (err) { callback(null, err.message); });
    }

    function lookupCardByScryfallId(scryfallId, callback) {
        fetch('/api/deck/lookup-by-uuid', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ scryfallId: scryfallId }),
        })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (data.error) { callback(null, data.error); return; }
            // Now look up full card data
            lookupCardBySetNumber(data.setCode, data.number, callback);
        })
        .catch(function (err) { callback(null, err.message); });
    }

    function lookupCardByName(name, callback) {
        fetch('/api/deck/lookup-by-name', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: name }),
        })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (data.error) { callback(null, data.error); return; }
            callback(data, null);
        })
        .catch(function (err) { callback(null, err.message); });
    }

    /* ─── Add Card to Deck (from any source) ─── */
    function addCardToDeck(cardData, to99) {
        var result = to99 ? deck.addCardTo99(cardData) : deck.addCard(cardData);
        if (result.error) {
            showToast(result.error);
            return;
        }
        renderAll();
        saveCurrentDeck();
        // Fetch prices so new card shows price immediately
        fetchPrices(function () {
            renderAll();
        });
        if (result.isCommander) {
            showToast('Commander set: ' + cardData.name);
        } else {
            showToast('Added: ' + cardData.name);
        }
    }

    /* ── Toast Notifications ─── */
    function showToast(message, duration) {
        var existing = document.querySelector('.deck-toast');
        if (existing) existing.remove();

        var toast = document.createElement('div');
        toast.className = 'deck-toast';
        toast.textContent = message;
        document.body.appendChild(toast);

        var dur = duration || 2000;
        setTimeout(function () {
            toast.style.opacity = '0';
            toast.style.transform = 'translateY(10px)';
            setTimeout(function () { toast.remove(); }, 300);
        }, dur);
    }

    /* ─── Search Panel ─── */
    function initSearch() {
        var form = $('#deck-search-form');
        if (!form) return;

        form.addEventListener('submit', function (e) {
            e.preventDefault();
            searchParams = null;  // force fresh param build
            runDeckSearch();
        });

        // Sync color chips (Card Colors)
        $$('.deck-color-cb').forEach(function (cb) {
            cb.addEventListener('change', function () { syncDeckChips('deck-color-cb', 'deck-color'); });
        });

        // Sync CI chips
        $$('.deck-ci-cb').forEach(function (cb) {
            cb.addEventListener('change', function () { syncDeckChips('deck-ci-cb', 'deck-ci'); });
        });

        // Name autocomplete
        initDeckNameAutocomplete();

        // Reset button
        var resetBtn = $('#deck-search-reset');
        if (resetBtn) {
            resetBtn.addEventListener('click', function () {
                form.reset();
                // Also clear hidden inputs that form.reset() skips
                $('#deck-color').value = '';
                $('#deck-ci').value = '';
                // Clear active chip styling
                $$('.deck-color-cb, .deck-ci-cb').forEach(function (cb) {
                    var chip = cb.closest('.chip');
                    if (chip) chip.classList.remove('active');
                });
            });
        }

        // Chip click — toggle "active" class for visual feedback
        document.querySelectorAll('.color-chips.multi .chip').forEach(function (chip) {
            chip.addEventListener('click', function (e) {
                // Don't interfere with the checkbox's own click
                var cb = chip.querySelector('input[type=checkbox]');
                if (cb && e.target !== cb) {
                    // After the checkbox state changes, update active class
                    setTimeout(function () {
                        chip.classList.toggle('active', cb.checked);
                    }, 0);
                }
            });
        });

        // Per-page dropdown — re-fetch when changed
        var searchPerPage = $('#deck-search-per-page');
        if (searchPerPage) {
            searchPerPage.addEventListener('change', function () {
                if (searchParams) {
                    searchParams.set('per_page', searchPerPage.value);
                    runDeckSearch(1);
                }
            });
        }
    }

    function syncDeckChips(className, targetId) {
        var vals = [];
        $$('.' + className + ':checked').forEach(function (cb) { vals.push(cb.value); });
        var el = document.getElementById(targetId);
        if (el) el.value = vals.join('');
    }

    function initCommanderAutocomplete() {
        initToolAutocomplete('commander-search-input', 'commander-autocomplete-list', {
            onSelect: function (card) {
                lookupCardBySetNumber(card.setCode, card.number, function (fullCard, err) {
                    if (err) { showToast(err); return; }
                    if (!fullCard.isLegendary || !fullCard.isCreature) {
                        showToast('Commander must be a legendary creature.');
                        return;
                    }
                    if (!replaceCommander(fullCard)) return;
                    renderAll();
                    saveCurrentDeck();
                    showToast('Commander set: ' + fullCard.name);
                });
                document.getElementById('commander-search-input').value = '';
            }
        });
    }

    function initDeckNameAutocomplete() {
        initToolAutocomplete('deck-search-name', 'deck-name-autocomplete', {
            keepValue: true
        });
    }

    function buildDeckSearchParams(page) {
        // Pure-ish form serializer; exposed to the QuickJS harness.
        var params = new URLSearchParams();

        var q = $('#deck-search-q').value.trim();
        if (q) params.set('q', q);

        var name = $('#deck-search-name').value.trim();
        if (name) params.set('name', name);

        var oracle = $('#deck-search-oracle').value.trim();
        if (oracle) params.set('oracle', oracle);

        var typeLine = $('#deck-search-type').value.trim();
        if (typeLine) params.set('type_line', typeLine);

        var manaCost = $('#deck-search-mana_cost').value.trim();
        if (manaCost) params.set('mana_cost', manaCost);

        var keywords = $('#deck-search-keywords').value.trim();
        if (keywords) params.set('keywords', keywords);

        var color = $('#deck-color').value;
        if (color) {
            params.set('color', color);
            params.set('color_rule', $('#deck-color-rule').value);
        }

        var ci = $('#deck-ci').value;
        if (ci) {
            params.set('ci', ci);
            params.set('ci_rule', $('#deck-ci-rule').value);
        }

        var mv = $('#deck-mv').value.trim();
        if (mv) {
            params.set('mv', mv);
            params.set('mv_op', $('#deck-mv-op').value);
        }

        var powVal = $('#deck-pow').value.trim();
        if (powVal) {
            params.set('pow', powVal);
            params.set('pow_op', $('#deck-pow-op').value);
        }

        var touVal = $('#deck-tou').value.trim();
        if (touVal) {
            params.set('tou', touVal);
            params.set('tou_op', $('#deck-tou-op').value);
        }

        var loyVal = $('#deck-loy').value.trim();
        if (loyVal) {
            params.set('loy', loyVal);
            params.set('loy_op', $('#deck-loy-op').value);
        }

        $$('#deck-rarity-chips input:checked').forEach(function (cb) {
            params.append('rarity', cb.value);
        });

        var setCode = $('#deck-set').value.trim();
        if (setCode) params.set('set', setCode);

        var format = $('#deck-format').value;
        if (format) params.set('format', format);
        var legality = $('#deck-legality').value;
        if (legality) params.set('legality', legality);

        var perPageEl = $('#deck-search-per-page');
        if (perPageEl) params.set('per_page', perPageEl.value);
        if (page) params.set('page', page);
        return params;
    }

    function searchResultCardHtml(card) {
        var img = card.imageUrl
            ? '<img src="' + escapeAttr(card.imageUrl) + '" alt="' + escapeAttr(card.name) +
              '" loading="lazy" width="244" height="340">'
            : '<div class="card-placeholder">' + escapeHtml(card.name) + '</div>';
        var mana = card.manaCost
            ? '<div class="cell-mana">' + renderManaSymbols(card.manaCost) + '</div>'
            : '';
        return '<div class="card-cell" data-set-code="' + escapeAttr(card.setCode || '') +
            '" data-number="' + escapeAttr(card.number || '') + '">' +
            img +
            '<div class="cell-body">' +
                '<div class="cell-name">' + escapeHtml(card.name || '') + '</div>' +
                mana +
                '<div class="cell-type">' + escapeHtml(card.type || '') + '</div>' +
            '</div>' +
        '</div>';
    }

    function renderDeckSearchResults(data) {
        var grid = $('#deck-search-results');
        var count = $('#deck-search-count');
        if (grid) {
            if (data.cards && data.cards.length) {
                grid.innerHTML = data.cards.map(searchResultCardHtml).join('');
                makeCardsDraggable(grid);
            } else {
                grid.innerHTML = '<div>No cards found.</div>';
            }
        }
        if (count) {
            count.textContent = data.total + ' card' + (data.total === 1 ? '' : 's') + ' found';
        }
        searchTotalPages = data.total_pages || 0;
        searchPage = data.page || 1;
        renderSearchPagination();
    }

    function runDeckSearch(page) {
        page = page || 1;
        var params = searchParams
            ? new URLSearchParams(searchParams.toString())
            : buildDeckSearchParams(page);
        params.set('page', page);
        searchParams = new URLSearchParams(params.toString());

        var grid = $('#deck-search-results');
        var pagination = $('#deck-search-pagination');
        if (grid) grid.innerHTML = '<div class="panel-loading">Searching…</div>';
        if (pagination) pagination.innerHTML = '';

        fetch('/api/search?' + params.toString())
            .then(function (r) {
                if (!r.ok) throw new Error('Search failed');
                return r.json();
            })
            .then(renderDeckSearchResults)
            .catch(function () {
                if (grid) grid.innerHTML = '<div>Search failed. Try again.</div>';
            });
    }

    function renderSearchPagination() {
        var container = $('#deck-search-pagination');
        if (!container) return;
        if (searchTotalPages <= 1) { container.innerHTML = ''; return; }

        var html = '';
        if (searchPage > 1) {
            html += '<button class="page prev" onclick="window._goSearchPage(' + (searchPage - 1) + ')">← Previous</button>';
        } else {
            html += '<span class="page prev disabled">← Previous</span>';
        }
        html += '<span class="page-indicator">Page ' + searchPage + ' of ' + searchTotalPages + '</span>';
        if (searchPage < searchTotalPages) {
            html += '<button class="page next" onclick="window._goSearchPage(' + (searchPage + 1) + ')">Next →</button>';
        } else {
            html += '<span class="page next disabled">Next →</span>';
        }
        container.innerHTML = html;
    }

    // Expose page navigation for inline onclick
    window._goSearchPage = function (page) {
        runDeckSearch(page);
        var grid = $('#deck-search-results');
        if (grid) grid.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    };

    /* ─── Similarity Inline ─── */
    function fetchSimilarResults(page) {
        page = page || 1;
        if (!similarCardUrl) return;

        var grid = $('#deck-similar-results');
        var count = $('#deck-similar-count');
        var pagination = $('#deck-similar-pagination');
        if (grid) grid.innerHTML = '<div class="panel-loading">Loading…</div>';
        if (pagination) pagination.innerHTML = '';

        var perPageEl = $('#deck-similar-per-page');
        var perPage = perPageEl ? perPageEl.value : '30';
        fetch(similarCardUrl + '?page=' + page + '&per_page=' + perPage)
            .then(function (r) { return r.text(); })
            .then(function (html) {
                var parser = new DOMParser();
                var doc = parser.parseFromString(html, 'text/html');

                // Extract card grid (the second one, which is "All Results")
                var grids = doc.querySelectorAll('.card-grid');
                var resultGrid = grids.length > 1 ? grids[1] : grids[0];

                var resultCount = doc.querySelector('.result-count');
                var pageIndicator = doc.querySelector('.pagination .page-indicator');

                if (grid) {
                    if (resultGrid) {
                        grid.innerHTML = resultGrid.innerHTML;
                        makeCardsDraggable(grid);
                    } else {
                        grid.innerHTML = '<div>No similar cards found.</div>';
                    }
                }
                if (count) {
                    count.textContent = resultCount ? resultCount.textContent : '';
                }

                if (pageIndicator) {
                    var m = pageIndicator.textContent.match(/of\s+(\d+)/);
                    similarTotalPages = m ? parseInt(m[1], 10) : 0;
                } else {
                    similarTotalPages = 0;
                }
                similarPage = page;
                renderSimilarPagination();
            })
            .catch(function () {
                if (grid) grid.innerHTML = '<div>Similarity search failed. Try again.</div>';
            });
    }

    function renderSimilarPagination() {
        var container = $('#deck-similar-pagination');
        if (!container) return;
        if (similarTotalPages <= 1) { container.innerHTML = ''; return; }

        var html = '';
        if (similarPage > 1) {
            html += '<button class="page prev" onclick="window._goSimilarPage(' + (similarPage - 1) + ')">← Previous</button>';
        } else {
            html += '<span class="page prev disabled">← Previous</span>';
        }
        html += '<span class="page-indicator">Page ' + similarPage + ' of ' + similarTotalPages + '</span>';
        if (similarPage < similarTotalPages) {
            html += '<button class="page next" onclick="window._goSimilarPage(' + (similarPage + 1) + ')">Next →</button>';
        } else {
            html += '<span class="page next disabled">Next →</span>';
        }
        container.innerHTML = html;
    }

    window._goSimilarPage = function (page) {
        fetchSimilarResults(page);
        var grid = $('#deck-similar-results');
        if (grid) grid.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    };

    /* ─── Commander Eval Inline ─── */
    function loadEvalInline(url) {
        var container = $('#deck-eval-container');
        var iframe = $('#deck-eval-iframe');
        if (!container || !iframe) return;

        iframe.src = url + '?embed=1';
        container.hidden = false;
    }

    /* ─── Make Cards Draggable ─── */
    function makeCardsDraggable(container) {
        if (!container) return;
        container.querySelectorAll('.card-cell').forEach(function (cell) {
            cell.setAttribute('draggable', 'true');

            // Prevent child images from starting their own native drag
            cell.querySelectorAll('img').forEach(function (img) {
                img.setAttribute('draggable', 'false');
            });

            cell.addEventListener('dragstart', function (e) {
                var setCode = cell.dataset.setCode;
                var number = cell.dataset.number;

                // Set drag data
                e.dataTransfer.setData('text/plain', '');
                e.dataTransfer.setData('application/mtg-card', JSON.stringify({
                    setCode: setCode,
                    number: number,
                }));

                e.dataTransfer.effectAllowed = 'copy';

                // Also set URL for Scryfall-like drops
                e.dataTransfer.setData('text/uri-list', window.location.origin + '/card/' + setCode + '/' + number);
            });
        });
    }

    /* ── Deck Card Row Drag (move between sections) ── */
    function makeDeckCardRowsDraggable() {
        // Make deck card rows (main + sections) draggable for inter-section moves
        $$('.deck-card-row').forEach(function (row) {
            row.setAttribute('draggable', 'true');

            row.addEventListener('dragstart', function (e) {
                var setCode = row.dataset.setCode;
                var number = row.dataset.number;
                var uuid = row.dataset.uuid;
                var sectionName = row.dataset.sectionName || '';
                var cardText = row.querySelector('.card-name-text');
                var cardName = cardText ? cardText.textContent : '';

                e.dataTransfer.effectAllowed = 'move';
                e.dataTransfer.setData('application/mtg-card-move', JSON.stringify({
                    setCode: setCode,
                    number: number,
                    uuid: uuid,
                    sourceSection: sectionName,
                    cardName: cardName,
                }));

                // Also set the general mtg-card for fallback resolution
                e.dataTransfer.setData('application/mtg-card', JSON.stringify({
                    setCode: setCode,
                    number: number,
                }));

                // Set text for external compat
                e.dataTransfer.setData('text/plain', cardName);

                // Visual feedback
                row.classList.add('dragging');
            });

            row.addEventListener('dragend', function () {
                row.classList.remove('dragging');
                // Remove drop-active from all zones
                $$('.drop-active').forEach(function (el) { el.classList.remove('drop-active'); });
            });
        });
    }

    /* ── Card Move Resolver ── */
    function handleCardDrop(dataTransfer, targetSection, onComplete) {
        // targetSection: '' = main deck, string = section name, 'commander' = commander slot
        var isCommander = targetSection === 'commander';

        // First check if this is a move (from one deck zone to another)
        var moveData = dataTransfer.getData('application/mtg-card-move');
        if (moveData) {
            try {
                var parsed = JSON.parse(moveData);
                var fromSection = parsed.sourceSection;  // '' = main deck

                // Same source and target — no-op
                if (fromSection === targetSection) {
                    onComplete({ skipped: true });
                    return;
                }

                // Find the card object in the source
                var card = null;
                if (fromSection) {
                    var section = deck.sections[fromSection];
                    if (section) card = section.find(function (c) { return c.uuid === parsed.uuid; });
                } else {
                    card = deck.cards.find(function (c) { return c.uuid === parsed.uuid; });
                }

                if (!card) {
                    // Card not found in source — fall back to lookup
                    lookupCardBySetNumber(parsed.setCode, parsed.number, function (resolved, err) {
                        if (err) { onComplete({ error: err }); return; }
                        if (isCommander) {
                            finishCommanderMove(resolved, fromSection, onComplete);
                        } else {
                            finishMove(resolved, fromSection, targetSection, onComplete);
                        }
                    });
                    return;
                }

                if (isCommander) {
                    finishCommanderMove(card, fromSection, onComplete);
                } else {
                    finishMove(card, fromSection, targetSection, onComplete);
                }
                return;
            } catch (e) { /* fall through to copy mode */ }
        }

        // Copy mode — resolve card from search results (no sourceSection)
        var mtgData = dataTransfer.getData('application/mtg-card');
        if (mtgData) {
            try {
                var p = JSON.parse(mtgData);
                lookupCardBySetNumber(p.setCode, p.number, function (card, err) {
                    if (err) { onComplete({ error: err }); return; }
                    finishCopy(card, targetSection, onComplete);
                });
                return;
            } catch (e) { /* fall through */ }
        }

        // Scryfall UUID fallback (external image / file drops)
        var scryfallId = _extractScryfallIdFromTransfer(dataTransfer);
        if (scryfallId) {
            lookupCardByScryfallId(scryfallId, function (card, err) {
                if (err) { onComplete({ error: err }); return; }
                finishCopy(card, targetSection, onComplete);
            });
            return;
        }

        onComplete({ error: 'Could not identify a card from that drop.' });
    }

    function finishMove(card, fromSection, toSection, onComplete) {
        var result = deck.moveCardBetweenSections(card, fromSection, toSection);
        if (result.error) {
            onComplete({ error: result.error });
            return;
        }
        renderAll();
        saveCurrentDeck();
        onComplete({ ok: true, cardName: card.name, moved: true, from: fromSection, to: toSection });
    }

    function finishCommanderMove(card, fromSection, onComplete) {
        // Validate legendary creature
        if (!card.isLegendary || !card.isCreature) {
            onComplete({ error: 'Commander must be a legendary creature.' });
            return;
        }
        // Remove from source
        if (fromSection) {
            deck.removeFromSection(fromSection, card.uuid);
        } else {
            deck.removeCard(card.uuid);
        }
        // If there's an existing commander, move it to main/where this card came from
        if (deck.commander) {
            var oldCommander = Object.assign({}, deck.commander, { quantity: 1, tags: [] });
            recordHistoryEvent('remove', deck.commander, 'commander');
            if (fromSection) {
                var source = deck.sections[fromSection] || [];
                if (!source.find(function (c) { return c.uuid === oldCommander.uuid; })) {
                    source.push(oldCommander);
                }
                recordHistoryEvent('add', oldCommander, fromSection);
            } else {
                // The old commander may already have a copy in the 99.
                if (!deck.cards.find(function (c) { return c.uuid === oldCommander.uuid; })) {
                    deck.cards.push(oldCommander);
                }
                recordHistoryEvent('add', oldCommander, 'main');
            }
        }
        deck.commander = Object.assign({}, card, { quantity: 1 });
        recordHistoryEvent('add', card, 'commander');
        renderAll();
        saveCurrentDeck();
        onComplete({ ok: true, cardName: card.name, moved: true, from: fromSection, to: 'commander' });
    }

    function replaceCommander(card) {
        /* Explicitly confirm replacing an existing commander. The previous
           code assigned deck.commander directly in two paths, silently
           dropping the old commander from the deck. */
        if (!card) return false;
        if (deck.commander && deck.commander.uuid !== card.uuid) {
            if (!window.confirm(
                'Replace commander "' + deck.commander.name + '" with "' +
                card.name + '"? The old commander will be removed.'
            )) {
                return false;
            }
            recordHistoryEvent('remove', deck.commander, 'commander');
        }
        deck.commander = Object.assign({}, card, { quantity: 1 });
        recordHistoryEvent('add', card, 'commander');
        return true;
    }

    function finishCopy(card, targetSection, onComplete) {
        if (targetSection === 'commander') {
            if (!card.isLegendary || !card.isCreature) {
                onComplete({ error: 'Commander must be a legendary creature.' });
                return;
            }
            if (!replaceCommander(card)) {
                onComplete({ skipped: true });
                return;
            }
            renderAll();
            saveCurrentDeck();
            onComplete({ ok: true, cardName: card.name, isCommander: true });
            return;
        }

        if (targetSection) {
            var result = deck.addToSection(targetSection, card);
            if (result.error) {
                onComplete({ error: result.error });
                return;
            }
            renderAll();
            saveCurrentDeck();
            onComplete({ ok: true, cardName: card.name });
            return;
        }

        // Copy to main deck
        var mainResult = deck.addCardTo99(card);
        if (mainResult.error) {
            onComplete({ error: mainResult.error });
            return;
        }
        renderAll();
        saveCurrentDeck();
        onComplete({ ok: true, cardName: card.name });
    }

    /* ── Drop Zone ── */
    function initDropZones() {
        // Deck panel drop zone (main deck)
        var deckPanel = $('.deck-builder-panel .panel-body');
        var commanderSlot = $('#commander-slot');

        if (deckPanel) {
            setupDropZone(deckPanel, function (onComplete) {
                return function (dataTransfer) {
                    handleCardDrop(dataTransfer, '', onComplete);
                };
            });
        }

        if (commanderSlot) {
            setupDropZone(commanderSlot, function (onComplete) {
                return function (dataTransfer) {
                    handleCardDrop(dataTransfer, 'commander', onComplete);
                };
            });
        }

        initSectionDropZones();
    }

    function initSectionDropZones() {
        // Custom section bodies are drop zones — re-initialized after each render
        $$('#custom-sections .custom-section-body').forEach(function (body) {
            var sectionName = body.dataset.sectionName;
            if (!sectionName) return;

            setupDropZone(body, function (onComplete) {
                return function (dataTransfer) {
                    handleCardDrop(dataTransfer, sectionName, onComplete);
                };
            });
        });
    }

    function setupDropZone(element, makeHandler) {
        var dragCounter = 0;

        element.addEventListener('dragenter', function (e) {
            e.preventDefault();
            dragCounter++;
            element.classList.add('drop-active');
        });

        element.addEventListener('dragover', function (e) {
            e.preventDefault();
        });

        element.addEventListener('dragleave', function () {
            dragCounter--;
            if (dragCounter === 0) {
                element.classList.remove('drop-active');
            }
        });

        element.addEventListener('drop', function (e) {
            e.preventDefault();
            e.stopPropagation();  // prevent commander drop from firing deck panel handler
            dragCounter = 0;
            element.classList.remove('drop-active');

            var handler = makeHandler(function onComplete(result) {
                if (!result) return;
                if (result.skipped) return;
                if (result.error) {
                    showToast(result.error);
                    return;
                }
                if (result.moved) {
                    var fromLabel = result.from ? '"' + result.from + '"' : 'main deck';
                    var toLabel = result.to ? '"' + result.to + '"' : 'main deck';
                    showToast('Moved ' + result.cardName + ' from ' + fromLabel + ' to ' + toLabel);
                } else if (result.cardName) {
                    if (result.isCommander) {
                        showToast('Commander set: ' + result.cardName);
                    } else {
                        showToast('Added: ' + result.cardName);
                    }
                }
            });

            handler(e.dataTransfer);
        });
    }

    /* ─── Shared Scryfall ID helpers ─── */
    function _extractScryfallId(str) {
        var m = str.match(/([a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})/i);
        return m ? m[1] : null;
    }

    function _extractScryfallIdFromTransfer(dataTransfer) {
        // Try text/uri-list
        var uri = dataTransfer.getData('text/uri-list');
        if (uri) {
            var id = _extractScryfallId(uri);
            if (id) return id;
        }
        // Try text/html
        var html = dataTransfer.getData('text/html');
        if (html) {
            var id2 = _extractScryfallId(html);
            if (id2) return id2;
        }
        // Try file names
        if (dataTransfer.files && dataTransfer.files.length > 0) {
            var id3 = _extractScryfallId(dataTransfer.files[0].name);
            if (id3) return id3;
        }
        return null;
    }

    /* ─── Global: make .card-cell draggable everywhere ─── */
    function enableGlobalDrag() {
        // This runs on the deck builder page and makes card-cell elements
        // inside the search results panel draggable.
        var observer = new MutationObserver(function () {
            makeCardsDraggable($('#deck-search-results'));
        });
        var grid = $('#deck-search-results');
        if (grid) {
            observer.observe(grid, { childList: true, subtree: true });
        }
    }

    /* ─── Button Handlers ─── */
    function initButtons() {

        // Export (dropdown: text list, JSON backup, presentation HTML)
        var exportBtn = $('#deck-export-btn');
        if (exportBtn) {
            exportBtn.addEventListener('click', function (e) {
                e.stopPropagation();  // keep the click from reaching ContextMenu's document-close handler

                function exportJson() {
                    var data = deck.toExport();
                    var json = JSON.stringify(data, null, 2);
                    var blob = new Blob([json], { type: 'application/json' });
                    var url = URL.createObjectURL(blob);
                    var a = document.createElement('a');
                    a.href = url;
                    var fileName = (deck.name || 'deck').replace(/[^a-zA-Z0-9]/g, '-').toLowerCase();
                    a.download = fileName + '.json';
                    document.body.appendChild(a);
                    a.click();
                    document.body.removeChild(a);
                    URL.revokeObjectURL(url);
                    showToast('Deck exported as ' + a.download);
                }

                function exportText() {
                    var hasSections = deck.sections && Object.keys(deck.sections).some(function (k) {
                        return (deck.sections[k] || []).length > 0;
                    });
                    if (deck.getTotalCount() === 0 && !hasSections) {
                        showToast('Deck is empty.');
                        return;
                    }
                    copyTextToClipboard(deck.toTextExport(), function () {
                        showToast('Deck list copied to clipboard.');
                    });
                }

                function exportPresentation() {
                    if (!deck.commander) {
                        showToast('Add a commander first.');
                        return;
                    }

                    exportBtn.disabled = true;
                    showToast('Generating presentation…');

                    // Collect unique image URLs to inline (commander normal +
                    // outline example cards small).
                    var jobs = [];  // [{ key: 'size:id', url }]
                    var seen = {};

                    function addJob(size, card) {
                        if (!card || !card.scryfallId) return;
                        var key = size + ':' + card.scryfallId;
                        if (seen[key]) return;
                        seen[key] = true;
                        var url = size === 'small' && card.imageUrl
                            ? card.imageUrl.replace('/normal/', '/small/')
                            : (card.imageUrl || '/img/' + size + '/front/' + card.scryfallId[0] + '/' + card.scryfallId[1] + '/' + card.scryfallId + '.jpg');
                        jobs.push({ key: key, url: url });
                    }

                    addJob('normal', deck.commander);
                    deck.ensureOutline().forEach(function (sec) {
                        (sec.cards || []).forEach(function (c) {
                            addJob('small', c);
                            addJob('normal', c);  // enlarged image for the click-to-view modal
                        });
                    });

                    Promise.all(jobs.map(function (job) {
                        return _fetchAsDataURI(job.url).then(function (dataURI) {
                            return { key: job.key, dataURI: dataURI };
                        }).catch(function () {
                            return { key: job.key, dataURI: '' };
                        });
                    })).then(function (results) {
                        var map = {};
                        results.forEach(function (r) { if (r && r.dataURI) map[r.key] = r.dataURI; });

                        var html = deck.toPresentationHTML(function (size, scryfallId) {
                            return map[size + ':' + scryfallId] || '';
                        });

                        var blob = new Blob([html], { type: 'text/html' });
                        var url = URL.createObjectURL(blob);
                        var a = document.createElement('a');
                        a.href = url;
                        var fileName = (deck.name || 'deck').replace(/[^a-zA-Z0-9]/g, '-').toLowerCase();
                        a.download = fileName + '-presentation.html';
                        document.body.appendChild(a);
                        a.click();
                        document.body.removeChild(a);
                        URL.revokeObjectURL(url);
                        showToast('Presentation exported as ' + a.download);
                    }).catch(function () {
                        showToast('Presentation export failed.');
                    }).then(function () {
                        exportBtn.disabled = false;
                    });
                }

                var rect = exportBtn.getBoundingClientRect();
                ContextMenu.show(rect.left, rect.bottom + 4, [
                    { label: 'Presentation HTML', action: exportPresentation },
                    { label: 'Text (Moxfield / Archidekt)', action: exportText },
                    { label: 'JSON (backup)', action: exportJson }
                ]);
            });
        }

        // Text Import
        initTextImport();

        // Clear
        var clearBtn = $('#deck-clear-btn');
        if (clearBtn) {
            clearBtn.addEventListener('click', function () {
                var hasSections = deck.sections && Object.keys(deck.sections).some(function (k) {
                    return (deck.sections[k] || []).length > 0;
                });
                if (deck.getTotalCount() === 0 && !deck.commander && !hasSections) {
                    showToast('Deck is already empty.');
                    return;
                }
                if (!confirm('Clear the entire deck? This cannot be undone.')) return;
                deck.clear();
                $('#deck-name').value = '';
                renderAll();
                saveCurrentDeck();
                showToast('Deck cleared.');
            });
        }

        // Deck name input — auto-save on input and update status
        var nameInput = $('#deck-name');
        if (nameInput) {
            nameInput.addEventListener('input', function () {
                var trimmed = nameInput.value.trim();
                deck.name = trimmed;
                nameInput.classList.toggle('unnamed', !trimmed);
                if (!trimmed) {
                    updateSaveStatus('unsaved', 'Name your deck to save');
                } else {
                    updateSaveStatus('saving', 'Saving…');
                }
            });
            nameInput.addEventListener('blur', function () {
                if (deck.name) saveCurrentDeck();
            });
        }
    }

    /* ─── Load Saved Deck (from server by name) ─── */
    function loadSavedDeckByName(name) {
        if (!name) return;
        var data = loadDeckFromServer(name);
        if (!data) return;
        deck.fromImport(data);
        deck.name = data.name || name;
        deck._savedName = deck.name;
        updateHistoryBtnVisibility();

        // Render deck UI immediately — don't wait for network calls
        $('#deck-name').value = deck.name;
        renderAll();
        restoreEvalData();
        checkHistoryReminder();
        if (cachedGuidelineTemplates.length > 0) {
            populateGuidelineSelects();
        }

        // Then recover scryfallIds in the background; re-render when prices arrive
        recoverMissingScryfallIds(function () {
            if (deck.name) {
                fetchPrices(function () {
                    renderAll();
                });
            }
        });
    }

    /* ─── ScryfallId Recovery ─── */
    function recoverMissingScryfallIds(onComplete) {
        // Collect all cards with missing/empty scryfallId
        var toRecover = [];  // [{ setCode, number, cardRef }]

        function collect(card, cardRef) {
            if (card && card.setCode && card.number && !card.scryfallId) {
                toRecover.push({ setCode: card.setCode, number: card.number, cardRef: cardRef });
            }
        }

        if (deck.commander) {
            collect(deck.commander, deck.commander);
        }

        deck.cards.forEach(function (c) { collect(c, c); });

        if (deck.sections) {
            Object.keys(deck.sections).forEach(function (key) {
                (deck.sections[key] || []).forEach(function (c) { collect(c, c); });
            });
        }

        if (toRecover.length === 0) {
            onComplete();
            return;
        }

        fetch('/api/deck/cards/enrich', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ cards: toRecover }),
        })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (data.enriched && data.enriched.length > 0) {
                // Build lookup: setCode|number -> scryfallId
                var lookup = {};
                data.enriched.forEach(function (e) {
                    lookup[e.setCode + '|' + e.number] = e.scryfallId;
                });
                // Patch scryfallIds back into cards
                toRecover.forEach(function (item) {
                    var key = item.setCode + '|' + item.number;
                    if (lookup[key]) {
                        item.cardRef.scryfallId = lookup[key];
                    }
                });
                // Persist the repaired deck
                saveDeckToServer(deck);
            }
        })
        .catch(function () { /* recovery failed — prices won't work, but deck still loads */ })
        .finally(function () {
            onComplete();
        });
    }

    /* Push stored evalData to server cache so the iframe can re-render it */
    function restoreEvalData() {
        if (!deck.evalData || !deck.commander) return;
        var payload = JSON.stringify({
            evalData: deck.evalData
        });
        // Restore into the eval cache so the iframe has data when opened
        fetch('/card/' + deck.commander.setCode + '/' + deck.commander.number + '/eval/restore', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: payload,
        })
        .then(function (r) { return r.json(); })
        .then(function () {
            // Pre-load the eval iframe so it's ready when the user opens the tab
            loadEvalInline('/card/' + deck.commander.setCode + '/' + deck.commander.number + '/eval');
        })
        .catch(function () { /* no-op if eval is unavailable */ });
    }

    /* ─── Add-to-Deck buttons on card panel ─── */
    function enableCardPanelAddToDeck() {
        // Inject an "Add to Deck" button into the card detail side panel
        // when it's opened. We hook into the openPanel callback via MutationObserver.
        var panel = $('#card-panel-sheet');
        if (!panel) return;

        var observer = new MutationObserver(function () {
            var addBtn = panel.querySelector('.btn-add-to-deck');
            if (addBtn) return; // already added

            // Find the card header area
            var header = panel.querySelector('.card-header');
            if (!header) return;

            var setCode = header.dataset.setCode || panel.dataset.setCode;
            var number = header.dataset.number || panel.dataset.number;

            // Extract from the card-panel-body if present
            var body = panel.querySelector('.card-panel-body');
            if (!body) return;

            // The card data is in the template — we need setCode and number from the URL or data attrs
            var currentUrl = window.location.pathname;
            var match = currentUrl.match(/\/card\/([^\/]+)\/([^\/]+)/);
            if (!match) return;
            var sc = match[1];
            var num = match[2];

            var btn = document.createElement('button');
            btn.className = 'btn-add-to-deck';
            btn.textContent = '+ Add to Deck';
            btn.title = 'Add this card to your deck';
            btn.addEventListener('click', function () {
                lookupCardBySetNumber(sc, num, function (card, err) {
                    if (err) { showToast(err); return; }
                    // This button never represents the commander slot; force
                    // the 99 so a legendary creature does not hit the
                    // "commander already filled" error.
                    addCardToDeck(card, true);
                });
            });

            // Insert after the card header
            var meta = body.querySelector('.card-meta-info');
            if (meta) {
                meta.parentNode.insertBefore(btn, meta);
            } else {
                body.appendChild(btn);
            }
        });

        observer.observe(panel, { childList: true, subtree: true });
    }

    /* ─── Card Context Menu (move between sections) ─── */
    function initCardContextMenus() {
        // Right-click a deck card row → "Move to ▸ <section|Main Deck>".
        // Main rows and section rows both carry data-uuid; section rows also
        // carry data-section-name (main rows omit it → '' = main deck).
        document.addEventListener('contextmenu', function (e) {
            var row = e.target.closest('.deck-card-row');
            if (!row) return;

            var uuid = row.dataset.uuid;
            if (!uuid) return;

            var sourceSection = row.dataset.sectionName || '';
            var card = null;
            if (sourceSection) {
                var section = deck.sections[sourceSection];
                if (section) card = section.find(function (c) { return c.uuid === uuid; });
            } else {
                card = deck.cards.find(function (c) { return c.uuid === uuid; });
            }
            if (!card) return;

            e.preventDefault();

            // Hide any hover preview so it doesn't linger behind the menu, and
            // cancel its pending 250ms show-timer (a right-click can beat the
            // timer, re-showing the preview after the menu opens).
            if (hoverTimeout) { clearTimeout(hoverTimeout); hoverTimeout = null; }
            hoverActiveRow = null;
            if (hoverPreviewEl) hoverPreviewEl.style.display = 'none';

            var targets = [{ label: 'Main Deck', action: function () {
                if (sourceSection === '') return;
                finishMove(card, sourceSection, '', function (result) { onMoveResult(result); });
            } }];

            Object.keys(deck.sections || {}).forEach(function (name) {
                if (name === sourceSection) return; // same-section move is a no-op
                targets.push({ label: name, action: function () {
                    finishMove(card, sourceSection, name, function (result) { onMoveResult(result); });
                } });
            });

            if (targets.length === 1) {
                // No other destinations — still show a disabled hint.
                targets = [{ label: 'No other sections', disabled: true }];
            }

            ContextMenu.show(e.clientX, e.clientY, [
                { label: 'Note', action: function () { openNoteEditor(uuid, sourceSection); } },
                { label: 'Move to', children: targets }
            ]);
        });

        function onMoveResult(result) {
            if (!result) return;
            if (result.error) { showToast(result.error); return; }
            var toLabel = result.to ? '"' + result.to + '"' : 'main deck';
            showToast('Moved ' + result.cardName + ' to ' + toLabel);
        }
    }

    /* ─── Card Note Editor ─── */
    function openNoteEditor(uuid, sectionName) {
        // Open a modal textarea to edit a single card's note ('' = main deck).
        // The note is stored on the card object and persisted with the deck.
        var overlay = $('#card-note-overlay');
        var textarea = $('#card-note-textarea');
        var titleEl = $('#card-note-title');
        if (!overlay || !textarea) return;

        // Resolve the card (main deck vs. named section) to seed the editor.
        var card = null;
        if (sectionName) {
            var section = deck.sections[sectionName];
            if (section) card = section.find(function (c) { return c.uuid === uuid; });
        } else {
            card = deck.cards.find(function (c) { return c.uuid === uuid; });
        }
        if (!card) { showToast('Card not found.'); return; }

        if (titleEl) titleEl.textContent = 'Note — ' + card.name;
        textarea.value = card.note || '';

        overlay.hidden = false;
        overlay.classList.add('open');
        textarea.focus();

        function save() {
            var result = deck.setCardNote(uuid, sectionName, textarea.value);
            if (result.error) { showToast(result.error); return; }
            overlay.classList.remove('open');
            overlay.hidden = true;
            renderAll();
            saveCurrentDeck();
            showToast(card.name + ' note saved.');
        }

        function cancel() {
            overlay.classList.remove('open');
            overlay.hidden = true;
        }

        var saveBtn = $('#card-note-save');
        var cancelBtn = $('#card-note-cancel');
        // Rebind on each open so a stale handler can't reference a previous card.
        if (saveBtn) saveBtn.onclick = save;
        if (cancelBtn) cancelBtn.onclick = cancel;

        overlay.onclick = function (e) {
            if (e.target === overlay) cancel();
        };

        textarea.onkeydown = function (e) {
            if (e.key === 'Escape') { e.preventDefault(); cancel(); }
        };
    }

    /* ─── Sort Select ─── */
    function initSortSelect() {
        var sel = $('#deck-sort-select');
        if (!sel) return;
        sel.addEventListener('change', function () {
            currentSort = sel.value;
            renderSections();
            renderCustomSections();
        });
    }

    function initGroupSelect() {
        var sel = $('#deck-group-select');
        if (!sel) return;
        sel.addEventListener('change', function () {
            currentGroupBy = sel.value;
            renderSections();
            renderTagStats();
        });
    }

    /* ─── Quick Add ─── */
    var _quickAddSelected = null; // { setCode, number, name }

    function initQuickAdd() {
        var addBtn = $('#deck-quick-add-btn');
        if (!addBtn) return;

        function addSelected() {
            if (!_quickAddSelected) return;
            lookupCardBySetNumber(_quickAddSelected.setCode, _quickAddSelected.number, function (card, err) {
                if (err) { showToast(err); return; }
                addCardToDeck(card, true); // always to 99
            });
            document.getElementById('deck-quick-add-input').value = '';
            _quickAddSelected = null;
        }

        addBtn.addEventListener('click', addSelected);

        // Editing the text invalidates a previous autocomplete selection, so
        // Add cannot silently submit the old card.
        var quickInput = document.getElementById('deck-quick-add-input');
        if (quickInput) {
            quickInput.addEventListener('input', function () { _quickAddSelected = null; });
        }

        initToolAutocomplete('deck-quick-add-input', 'deck-quick-add-autocomplete', {
            keepValue: true,
            onSelect: function (card) {
                _quickAddSelected = card;
            },
            onEnterEmpty: addSelected
        });
    }

    /* ─── Panel Collapse ─── */
    var panelCollapsed = false;
    var PANEL_COLLAPSE_KEY = 'mtg-search-tools-collapsed';

    function initPanelCollapse() {
        var btn = $('#deck-tools-collapse-btn');
        var page = document.querySelector('.deck-builder-page');
        if (!btn || !page) return;

        // Restore state from localStorage
        try {
            panelCollapsed = localStorage.getItem(PANEL_COLLAPSE_KEY) === 'true';
        } catch (e) { /* ignore */ }
        if (panelCollapsed) {
            updateCollapseState();
        }

        function toggle() {
            panelCollapsed = !panelCollapsed;
            try { localStorage.setItem(PANEL_COLLAPSE_KEY, String(panelCollapsed)); } catch (e) { /* ignore */ }
            updateCollapseState();
        }

        btn.addEventListener('click', toggle);

        // Click on the collapsed strip expands
        var searchPanel = page.querySelector('.search-panel');
        if (searchPanel) {
            searchPanel.addEventListener('click', function (e) {
                if (panelCollapsed && e.target === searchPanel) {
                    toggle();
                }
            });
        }
    }

    function updateCollapseState() {
        var page = document.querySelector('.deck-builder-page');
        var btn = $('#deck-tools-collapse-btn');
        if (!page) return;
        page.classList.toggle('collapsed-tools', panelCollapsed);
        if (btn) {
            btn.textContent = panelCollapsed ? '▶' : '◀';
            btn.title = panelCollapsed ? 'Expand tools panel' : 'Collapse tools panel';
        }
    }

    /* ─── Tool Tabs ─── */
    function initToolTabs() {
        var tabs = $$('.tool-tab-btn');
        var contents = $$('.tool-content');

        tabs.forEach(function (btn) {
            btn.addEventListener('click', function () {
                var tool = btn.dataset.tool;
                // Update active button
                tabs.forEach(function (b) { b.classList.remove('active'); });
                btn.classList.add('active');
                // Show matching content, hide others
                contents.forEach(function (c) {
                    c.hidden = c.dataset.tool !== tool;
                });
            });
        });

        // --- Similarity autocomplete ---
        initToolAutocomplete('deck-similar-input', 'deck-similar-autocomplete', {
            onSelect: function (card) {
                similarCardUrl = '/card/' + card.setCode + '/' + card.number + '/similar';
                fetchSimilarResults(1);
            }
        });

        // --- Eval autocomplete ---
        initToolAutocomplete('deck-eval-input', 'deck-eval-autocomplete', {
            onSelect: function (card) {
                loadEvalInline('/card/' + card.setCode + '/' + card.number + '/eval');
            }
        });

        // --- Drag-and-drop for similarity ---
        initToolDropZone('deck-similar-drop', 'deck-similar-loading', 'deck-similar-error', function (card) {
            similarCardUrl = '/card/' + card.setCode + '/' + card.number + '/similar';
            fetchSimilarResults(1);
        });

        // Per-page dropdown for similarity — re-fetch when changed
        var similarPerPage = $('#deck-similar-per-page');
        if (similarPerPage) {
            similarPerPage.addEventListener('change', function () {
                if (similarCardUrl) fetchSimilarResults(1);
            });
        }

        // --- Drag-and-drop for eval ---
        initToolDropZone('deck-eval-drop', 'deck-eval-loading', 'deck-eval-error', function (card) {
            loadEvalInline('/card/' + card.setCode + '/' + card.number + '/eval');
        });

        // --- Templates tab ---
        initTemplatesTab();

        // --- Deck Outline tab ---
        initOutlineTab();
    }

    /* ─── Generic Autocomplete (shared by all autocomplete inputs) ─── */
    function initToolAutocomplete(inputId, listId, options) {
        options = options || {};
        var onSelect = options.onSelect || function () {};
        var onEnterEmpty = options.onEnterEmpty || null;
        var keepValue = options.keepValue || false;

        var input = document.getElementById(inputId);
        var list = document.getElementById(listId);

        // Create list element if it doesn't exist (e.g. deck name search)
        if (!list) {
            list = document.createElement('ul');
            list.id = listId;
            list.className = 'autocomplete-list';
            list.hidden = true;
            input.parentNode.appendChild(list);
        }
        if (!input) return;

        var timer = null;
        var selectedIndex = -1;

        function clearList() {
            list.innerHTML = '';
            list.hidden = true;
            selectedIndex = -1;
        }

        function selectItem(item) {
            var data = {
                setCode: item.dataset.setCode,
                number: item.dataset.number,
                name: item.dataset.name,
            };
            if (keepValue) {
                input.value = data.name;
            }
            clearList();
            onSelect(data);
        }

        input.addEventListener('input', function () {
            clearTimeout(timer);
            var q = input.value.trim();
            if (q.length < 2) { clearList(); return; }
            timer = setTimeout(function () {
                fetch('/card-autocomplete?q=' + encodeURIComponent(q))
                    .then(function (r) { return r.json(); })
                    .then(function (results) {
                        list.innerHTML = '';
                        if (!results || results.length === 0) { list.hidden = true; return; }
                        results.forEach(function (card) {
                            var li = document.createElement('li');
                            li.textContent = card.name;
                            li.dataset.name = card.name;
                            li.dataset.setCode = card.setCode;
                            li.dataset.number = card.number;
                            li.addEventListener('mousedown', function (e) {
                                e.preventDefault();
                                selectItem(li);
                            });
                            list.appendChild(li);
                        });
                        list.hidden = false;
                        selectedIndex = -1;
                    });
            }, 150);
        });

        input.addEventListener('keydown', function (e) {
            var items = list.querySelectorAll('li');
            if (!items.length || list.hidden) {
                if (e.key === 'Enter' && onEnterEmpty) {
                    e.preventDefault();
                    onEnterEmpty();
                }
                return;
            }
            if (e.key === 'ArrowDown') {
                e.preventDefault();
                selectedIndex = Math.min(selectedIndex + 1, items.length - 1);
                items.forEach(function (li, i) { li.classList.toggle('active', i === selectedIndex); });
            } else if (e.key === 'ArrowUp') {
                e.preventDefault();
                selectedIndex = Math.max(selectedIndex - 1, 0);
                items.forEach(function (li, i) { li.classList.toggle('active', i === selectedIndex); });
            } else if (e.key === 'Enter' && selectedIndex >= 0) {
                e.preventDefault();
                selectItem(items[selectedIndex]);
            } else if (e.key === 'Escape') {
                clearList();
            }
        });

        input.addEventListener('blur', function () {
            setTimeout(clearList, 150);
        });
    }

    /* ─── Templates Tab ─── */
    function initTemplatesTab() {
        var select = $('#deck-template-select');
        var importBtn = $('#deck-template-import-btn');
        var preview = $('#deck-template-preview');
        if (!select) return;

        // Load templates into dropdown
        function loadTemplateList() {
            fetch('/api/template/list')
                .then(function(r) {
                    if (!r.ok) throw new Error('HTTP ' + r.status);
                    return r.json();
                })
                .then(function(templates) {
                    if (!Array.isArray(templates)) {
                        console.warn('Template list API returned non-array:', templates);
                        templates = [];
                    }
                    // Filter to card templates only (exclude guidelines)
                    var cardTmpls = templates.filter(function(t) { return t.type !== 'guideline'; });
                    var html = '<option value="">Select a template…</option>';
                    cardTmpls.forEach(function(t) {
                        var cardCount = (t.cards || []).reduce(function(s, c) { return s + (c.quantity || 1); }, 0);
                        html += '<option value="' + escapeAttr(t.name) + '">' + escapeHtml(t.name) + ' (' + cardCount + ' card' + (cardCount !== 1 ? 's' : '') + ')</option>';
                    });
                    select.innerHTML = html;
                    importBtn.disabled = true;
                    preview.textContent = cardTmpls.length === 0 ? 'No card templates yet. Create one on the Templates page.' : '';
                })
                .catch(function(err) {
                    console.error('Failed to load templates:', err);
                    select.innerHTML = '<option value="">(Failed to load templates — check console)</option>';
                });
        }

        loadTemplateList();

        // Reload template list each time the Templates tab is clicked
        var tplTab = document.querySelector('.tool-tab-btn[data-tool="templates"]');
        if (tplTab) {
            tplTab.addEventListener('click', function() {
                loadTemplateList();
            });
        }

        // Preview on selection
        select.addEventListener('change', function() {
            var name = select.value;
            if (!name) {
                importBtn.disabled = true;
                preview.textContent = '';
                return;
            }
            importBtn.disabled = false;

            fetch('/api/template/list')
                .then(function(r) { return r.json(); })
                .then(function(templates) {
                    var tmpl = (templates || []).find(function(t) { return t.name === name; });
                    if (tmpl) {
                        var total = (tmpl.cards || []).reduce(function(s, c) { return s + (c.quantity || 1); }, 0);
                        var cardCount = (tmpl.cards || []).length;
                        preview.textContent = total + ' total cards in ' + cardCount + ' unique card' + (cardCount !== 1 ? 's' : '') + '.';
                    }
                });
        });

        // Import
        importBtn.addEventListener('click', function() {
            var name = select.value;
            if (!name) return;

            importBtn.disabled = true;
            importBtn.textContent = 'Importing…';

            fetch('/api/template/list')
                .then(function(r) { return r.json(); })
                .then(function(templates) {
                    var tmpl = (templates || []).find(function(t) { return t.name === name; });
                    if (!tmpl || !tmpl.cards) {
                        showToast('Template not found or empty.');
                        return;
                    }

                    var imported = 0;
                    var skipped = 0;
                    tmpl.cards.forEach(function(card) {
                        var result = deck.addWithQuantity(card, card.quantity || 1);
                        if (result.ok) {
                            imported++;
                        } else {
                            skipped++;
                        }
                    });
                    renderAll();
                    saveCurrentDeck();
                    var msg = 'Imported ' + imported + ' card' + (imported !== 1 ? 's' : '') + ' from ' + tmpl.name + '.';
                    if (skipped > 0) msg += ' (' + skipped + ' skipped — deck full or limit reached)';
                    showToast(msg);
                })
                .catch(function() {
                    showToast('Failed to import template.');
                })
                .finally(function() {
                    importBtn.disabled = false;
                    importBtn.textContent = 'Import to Deck';
                });
        });

        // ── Guideline select within Templates tab ──
        var guidelineSelect = $('#deck-template-guideline-select');
        var guidelinePreview = $('#deck-template-guideline-preview');

        if (guidelineSelect) {
            loadGuidelineList();

            guidelineSelect.addEventListener('change', function() {
                deck.guideline = this.value || null;
                renderGuidelineProgress();
                saveCurrentDeck();
                // Sync header bar select
                var headerSelect = $('#deck-guideline-select');
                if (headerSelect) headerSelect.value = this.value;

                // Show preview of guideline targets
                if (!this.value || !guidelinePreview) {
                    if (guidelinePreview) guidelinePreview.textContent = '';
                    return;
                }
                var g = null;
                for (var i = 0; i < cachedGuidelineTemplates.length; i++) {
                    if (cachedGuidelineTemplates[i].name === this.value) {
                        g = cachedGuidelineTemplates[i];
                        break;
                    }
                }
                if (g && g.categories) {
                    var parts = ['Lands','Ramp','Draw','Removal','Core'].map(function(c) {
                        return c + ': ' + (g.categories[c] || 0);
                    });
                    guidelinePreview.textContent = parts.join(' · ');
                }
            });
        }
    }

    /* ─── Deck Outline Tab ─── */
    function renderOutline() {
        var container = $('#deck-outline');
        if (!container) return;

        var sections = deck.ensureOutline();
        var html = '';
        sections.forEach(function (sec, idx) {
            html += '<div class="deck-section outline-section" data-outline-index="' + idx + '">';
            html += '<div class="deck-section-header custom-section-header">';
            html += '<span class="deck-section-title">' + escapeHtml(sec.name || 'Untitled') + '</span>';
            html += '<button class="outline-rename-btn" data-outline-index="' + idx + '" title="Rename section">✎</button>';
            html += '<button class="outline-delete-btn" data-outline-index="' + idx + '" title="Delete section">✕</button>';
            html += '</div>';

            html += '<div class="outline-section-body" data-outline-index="' + idx + '">';
            html += '<textarea class="outline-text" data-outline-index="' + idx + '" placeholder="Write this section…" rows="3">' + escapeHtml(sec.text || '') + '</textarea>';

            var cards = sec.cards || [];
            if (cards.length === 0) {
                html += '<div class="section-empty-hint outline-drop-hint">Drop cards here to use as examples</div>';
            } else {
                html += '<div class="outline-cards">';
                cards.forEach(function (card, cIdx) {
                    html += '<div class="outline-card" data-outline-index="' + idx + '" data-card-index="' + cIdx + '">'
                        + '<img src="' + escapeAttr(card.imageUrl || '') + '" alt="' + escapeAttr(card.name || '') + '" loading="lazy">'
                        + '<span class="outline-card-name" title="' + escapeAttr(card.name || '') + '">' + escapeHtml(card.name || '') + '</span>'
                        + '<button class="outline-card-remove-btn" data-outline-index="' + idx + '" data-card-index="' + cIdx + '" title="Remove example">✕</button>'
                        + '</div>';
                });
                html += '</div>';
            }
            html += '</div></div>';
        });

        container.innerHTML = html;

        attachOutlineListeners();
        initOutlineDropZones();
    }

    function attachOutlineListeners() {
        // Textarea edits
        $$('#deck-outline .outline-text').forEach(function (ta) {
            ta.addEventListener('input', function () {
                var idx = parseInt(this.dataset.outlineIndex, 10);
                var sec = deck.outline[idx];
                if (!sec) return;
                sec.text = this.value;
                saveCurrentDeck();
            });
        });

        // Rename
        $$('#deck-outline .outline-rename-btn').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var idx = parseInt(this.dataset.outlineIndex, 10);
                var sec = deck.outline[idx];
                if (!sec) return;
                var name = window.prompt('Rename section', sec.name || '');
                if (name === null) return;
                name = name.trim();
                if (!name) { showToast('Section name is required.'); return; }
                sec.name = name;
                renderOutline();
                saveCurrentDeck();
            });
        });

        // Delete
        $$('#deck-outline .outline-delete-btn').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var idx = parseInt(this.dataset.outlineIndex, 10);
                deck.outline.splice(idx, 1);
                renderOutline();
                saveCurrentDeck();
            });
        });

        // Remove example card
        $$('#deck-outline .outline-card-remove-btn').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var idx = parseInt(this.dataset.outlineIndex, 10);
                var cIdx = parseInt(this.dataset.cardIndex, 10);
                var sec = deck.outline[idx];
                if (!sec || !sec.cards) return;
                sec.cards.splice(cIdx, 1);
                renderOutline();
                saveCurrentDeck();
            });
        });
    }

    function initOutlineDropZones() {
        $$('#deck-outline .outline-section-body').forEach(function (body) {
            var idx = parseInt(body.dataset.outlineIndex, 10);
            setupDropZone(body, function (onComplete) {
                return function (dataTransfer) {
                    handleOutlineCardDrop(dataTransfer, idx, onComplete);
                };
            });
        });
    }

    function handleOutlineCardDrop(dataTransfer, outlineIndex, onComplete) {
        // Resolve a card from the drag and push an independent copy onto the
        // target outline section's example-card list. Does NOT move cards out of
        // deck.cards/deck.sections — outline examples are separate references.
        var sec = deck.outline[outlineIndex];
        if (!sec) { onComplete({ error: 'Outline section not found.' }); return; }

        function addCard(card) {
            if (!card || !card.uuid) { onComplete({ error: 'Could not identify a card from that drop.' }); return; }
            var copy = Object.assign({}, card, { quantity: 1, tags: [] });
            if (!sec.cards) sec.cards = [];
            sec.cards.push(copy);
            renderOutline();
            saveCurrentDeck();
            onComplete({ ok: true, cardName: card.name });
        }

        // Both copy ({setCode,number}) and move ({setCode,number,sourceSection,...})
        // payloads carry setCode+number, so resolve either via lookup.
        var payload = null;
        var mtgData = dataTransfer.getData('application/mtg-card');
        if (mtgData) {
            try { payload = JSON.parse(mtgData); } catch (e) { /* ignore */ }
        }
        if (!payload) {
            var moveData = dataTransfer.getData('application/mtg-card-move');
            if (moveData) {
                try { payload = JSON.parse(moveData); } catch (e) { /* ignore */ }
            }
        }

        if (payload && payload.setCode && payload.number) {
            lookupCardBySetNumber(payload.setCode, payload.number, function (card, err) {
                if (err) { onComplete({ error: err }); return; }
                addCard(card);
            });
            return;
        }

        var scryfallId = _extractScryfallIdFromTransfer(dataTransfer);
        if (scryfallId) {
            lookupCardByScryfallId(scryfallId, function (card, err) {
                if (err) { onComplete({ error: err }); return; }
                addCard(card);
            });
            return;
        }

        onComplete({ error: 'Could not identify a card from that drop.' });
    }

    function initOutlineTab() {
        var addBtn = $('#outline-add-btn');
        if (!addBtn) return;

        addBtn.addEventListener('click', function () {
            if (!deck.outline) deck.outline = [];
            deck.outline.push({ name: 'New Section', text: '', cards: [] });
            renderOutline();
            saveCurrentDeck();
            showToast('Section added.');
        });

        var exportBtn = $('#outline-export-btn');
        if (exportBtn) {
            exportBtn.addEventListener('click', function () {
                var text = deck.toOutlineTextExport();
                if (!text.trim()) { showToast('Outline is empty.'); return; }
                copyTextToClipboard(text, function () {
                    showToast('Outline copied to clipboard.');
                });
            });
        }

        renderOutline();
    }

    function initToolDropZone(dropId, loadingId, errorId, onFound) {
        var zone = document.getElementById(dropId);
        var loading = document.getElementById(loadingId);
        var errorEl = document.getElementById(errorId);
        if (!zone) return;

        var dragCounter = 0;

        function showError(msg) {
            if (!errorEl) return;
            errorEl.textContent = msg;
            errorEl.hidden = false;
            setTimeout(function () { errorEl.hidden = true; }, 5000);
        }

        function handleDrop(e) {
            e.preventDefault();
            dragCounter = 0;
            zone.classList.remove('drop-active');

            // 1. Internal drag from search results (direct setCode/number)
            var mtgData = e.dataTransfer.getData('application/mtg-card');
            if (mtgData) {
                try {
                    var parsed = JSON.parse(mtgData);
                    if (loading) loading.hidden = false;
                    lookupCardBySetNumber(parsed.setCode, parsed.number, function (card, err) {
                        if (loading) loading.hidden = true;
                        if (err) { showError(err); return; }
                        onFound(card);
                    });
                    return;
                } catch (err) { /* fall through */ }
            }

            // 2. External image / file / URL drop
            var scryfallId = _extractScryfallIdFromTransfer(e.dataTransfer);

            if (scryfallId) {
                if (loading) loading.hidden = false;
                lookupCardByScryfallId(scryfallId, function (card, err) {
                    if (loading) loading.hidden = true;
                    if (err) { showError(err); return; }
                    onFound(card);
                });
            } else {
                showError('Could not identify a Magic card from that image. Try searching by name.');
            }
        }

        zone.addEventListener('dragenter', function (e) {
            e.preventDefault();
            dragCounter++;
            zone.classList.add('drop-active');
        });

        zone.addEventListener('dragover', function (e) {
            e.preventDefault();
        });

        zone.addEventListener('dragleave', function () {
            dragCounter--;
            if (dragCounter === 0) {
                zone.classList.remove('drop-active');
            }
        });

        zone.addEventListener('drop', handleDrop);
    }

    /* ── Text Import ─── */
    function initTextImport() {
        var openBtn = $('#deck-text-import-btn');
        var overlay = $('#text-import-overlay');
        var closeBtn = $('#text-import-close');
        var goBtn = $('#text-import-go-btn');
        var textarea = $('#text-import-textarea');
        var statusEl = $('#text-import-status');
        var replaceCb = $('#text-import-replace-cb');

        if (!openBtn || !overlay) return;

        function openModal() {
            overlay.hidden = false;
            overlay.classList.add('open');
            textarea.focus();
        }

        function closeModal() {
            overlay.classList.remove('open');
            overlay.hidden = true;
            textarea.value = '';
            replaceCb.checked = false;
            if (statusEl) {
                statusEl.textContent = '';
                statusEl.className = 'text-import-status';
            }
        }

        openBtn.addEventListener('click', openModal);

        closeBtn.addEventListener('click', closeModal);

        overlay.addEventListener('click', function (e) {
            if (e.target === overlay) closeModal();
        });

        document.addEventListener('keydown', function (e) {
            if (e.key === 'Escape' && overlay.classList.contains('open')) {
                closeModal();
            }
        });

        // Parse text and import cards
        goBtn.addEventListener('click', function () {
            var raw = textarea.value.trim();
            if (!raw) {
                showTextImportStatus('Paste a card list first.', 'error');
                return;
            }

            var lines = raw.split('\n').filter(function (l) { return l.trim(); });
            // Common one-line formats: "1 Sol Ring", "1x Sol Ring",
            // "1 Sol Ring (LTC) 123", plus comments/section headers.
            // Bare names default to quantity 1.
            var sectionHeaders = {
                'commander': true, 'deck': true, 'mainboard': true,
                'main deck': true, 'sideboard': true, 'maybeboard': true,
                'companion': true
            };
            var entries = [];
            for (var i = 0; i < lines.length; i++) {
                var line = lines[i].trim();
                if (!line || line.indexOf('//') === 0) continue;
                if (sectionHeaders[line.toLowerCase()]) continue;

                var m = line.match(/^(\d+)\s*[xX]?\s+(.+)/);
                var qty = 1;
                var cardName = line;
                if (m) {
                    qty = parseInt(m[1], 10);
                    cardName = m[2].trim();
                }
                // Strip a Moxfield/Archidekt suffix: "Name (SET) 123 [*F*]".
                cardName = cardName.replace(
                    /\s+\([A-Za-z0-9]{2,8}\)\s+[A-Za-z0-9★-]+\s*(?:\*[^*]*\*)?\s*$/,
                    ''
                ).trim();
                if (!cardName) continue;
                entries.push({ qty: qty, name: cardName });
            }

            if (entries.length === 0) {
                showTextImportStatus('No cards found in input.', 'error');
                return;
            }

            // Check deck capacity (only if not replacing)
            if (!replaceCb.checked) {
                var totalToAdd = entries.reduce(function (s, e) { return s + e.qty; }, 0);
                var currentTotal = deck.getTotalCount();
                if (currentTotal + totalToAdd > MAX_CARDS) {
                    showTextImportStatus(
                        'Adding ' + totalToAdd + ' cards would exceed the ' + MAX_CARDS + '-card limit. Deck currently has ' + currentTotal + ' cards.',
                        'error'
                    );
                    return;
                }
            }

            showTextImportStatus('Looking up ' + entries.length + ' cards…', '');
            goBtn.disabled = true;

            // Look up each card, then add all at once
            var pending = entries.length;
            var found = [];
            var notFound = [];

            entries.forEach(function (entry) {
                lookupCardByName(entry.name, function (card, err) {
                    pending--;
                    if (err) {
                        notFound.push(entry.name);
                    } else {
                        found.push({ card: card, qty: entry.qty });
                    }

                    if (pending <= 0) {
                        // All lookups done — add cards
                        goBtn.disabled = false;

                        // Replace mode: clear card contents but keep the deck
                        // name, outline, guideline, and pricing options so the
                        // import can still autosave and the deck keeps its
                        // identity. Do not clear when every lookup failed.
                        if (replaceCb.checked && found.length > 0) {
                            deck.clearContents();
                        }

                        var added = 0;

                        found.forEach(function (item) {
                            var result = deck.addWithQuantity(item.card, item.qty);
                            if (result.ok) {
                                added += item.qty;
                            }
                            if (result.error) {
                                // Show the error but continue importing other cards
                                notFound.push(item.card.name + ' (' + result.error + ')');
                            }
                        });

                        renderAll();
                        saveCurrentDeck();

                        var msg = 'Imported ' + added + ' cards.';
                        if (notFound.length > 0) {
                            msg += ' Not found: ' + notFound.join(', ');
                        }
                        showTextImportStatus(msg, notFound.length > 0 ? 'error' : 'success');

                        if (added > 0) {
                            showToast(msg);
                            if (notFound.length === 0) {
                                closeModal();
                            }
                        }
                    }
                });
            });
        });

        function showTextImportStatus(msg, cls) {
            if (!statusEl) return;
            statusEl.textContent = msg;
            statusEl.className = 'text-import-status' + (cls ? ' ' + cls : '');
        }
    }

    /* ── Section Management UI ── */
    function initSectionManagement() {
        var addBtn = $('#section-add-btn');
        var addForm = $('#section-add-form');
        var addInput = $('#section-add-input');
        var confirmBtn = $('#section-add-confirm');
        var cancelBtn = $('#section-add-cancel');

        if (!addBtn || !addForm) return;

        // Show the inline form
        addBtn.addEventListener('click', function () {
            addForm.hidden = false;
            addBtn.hidden = true;
            if (addInput) {
                addInput.value = '';
                addInput.focus();
            }
        });

        // Cancel
        if (cancelBtn) {
            cancelBtn.addEventListener('click', function () {
                addForm.hidden = true;
                addBtn.hidden = false;
            });
        }

        // Add input — submit on Enter
        if (addInput) {
            addInput.addEventListener('keydown', function (e) {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    submitAddSection();
                }
                if (e.key === 'Escape') {
                    addForm.hidden = true;
                    addBtn.hidden = false;
                }
            });
        }

        if (confirmBtn) {
            confirmBtn.addEventListener('click', function () {
                submitAddSection();
            });
        }

        function submitAddSection() {
            var name = addInput ? addInput.value.trim() : '';
            var result = deck.createSection(name);
            if (result.error) {
                showToast(result.error);
                return;
            }
            addForm.hidden = true;
            addBtn.hidden = false;
            renderAll();
            saveCurrentDeck();
            showToast('Section "' + result.name + '" created.');
        }

        // Preset buttons (Sideboard, Maybeboard)
        $$('.section-preset-btn').forEach(function (btn) {
            btn.addEventListener('click', function () {
                var name = this.dataset.name;
                if (!deck.sections) deck.sections = {};
                if (deck.sections.hasOwnProperty(name)) {
                    showToast('Section "' + name + '" already exists.');
                    return;
                }
                var result = deck.createSection(name);
                if (result.error) {
                    showToast(result.error);
                    return;
                }
                renderAll();
                saveCurrentDeck();
                showToast('Section "' + result.name + '" created.');
            });
        });
    }
    function initGuideline() {
        loadGuidelineList();

        // Re-fresh guideline list when Templates tab is clicked (new guidelines may have been created)
        var tplTab = document.querySelector('.tool-tab-btn[data-tool="templates"]');
        if (tplTab) {
            tplTab.addEventListener('click', function() {
                loadGuidelineList();
            });
        }

        // Guideline select change
        var select = $('#deck-guideline-select');
        if (select) {
            select.addEventListener('change', function() {
                deck.guideline = this.value || null;
                renderGuidelineProgress();
                saveCurrentDeck();
            });
        }
    }

    /* ── Card Hover Preview ── */
    var hoverPreviewEl = null;
    var hoverTimeout = null;
    var hoverActiveRow = null;

    /* ── History Delete Button ── */
    function initHistoryDeleteButton() {
        var btn = $('#deck-history-delete-btn');
        if (!btn) return;
        btn.addEventListener('click', function () {
            if (!deck.name) { showToast('Save your deck first.'); return; }
            if (!confirm('Delete all change history for "' + deck.name + '"? This cannot be undone.')) return;
            fetch('/api/deck/history/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ name: deck.name }),
            })
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data.success || data.error === 'History not found.') {
                    showToast('History deleted.');
                } else {
                    showToast(data.error || 'Failed to delete history.');
                }
            })
            .catch(function () { showToast('Network error.'); });
        });
    }

    function checkHistoryReminder() {
        if (!deck.name) return;
        fetch('/api/deck/history/check?threshold_days=' + historyWarningDays)
            .then(function (r) { return r.json(); })
            .then(function (data) {
                var match = (data.reminders || []).find(function (r) { return r.deck === deck.name; });
                if (match) {
                    showToast('No changes in ' + match.days + ' days. Consider clearing history to save space.', 10000);
                }
            })
            .catch(function () { /* ignore */ });
    }

    function initCardHoverPreview() {
        // Create the preview element once, reuse it
        hoverPreviewEl = document.createElement('img');
        hoverPreviewEl.className = 'card-hover-preview';
        hoverPreviewEl.style.display = 'none';
        document.body.appendChild(hoverPreviewEl);

        // Delegate mouseover on both main deck sections and custom sections
        function onMouseOver(e) {
            // Skip if still inside the same row (moving between row children)
            var newRow = e.target.closest('.deck-card-row');
            if (newRow === hoverActiveRow) return;

            var row = newRow;
            // Don't show preview over buttons — they have their own interactions
            if (e.target.closest('button')) return;

            if (!row) {
                clearTimeout(hoverTimeout);
                hoverActiveRow = null;
                hoverPreviewEl.style.display = 'none';
                return;
            }

            var imageUrl = row.dataset.imageUrl;
            if (!imageUrl) return;

            hoverActiveRow = row;
            clearTimeout(hoverTimeout);
            hoverTimeout = setTimeout(function () {
                hoverPreviewEl.src = imageUrl;
                hoverPreviewEl.style.display = '';
                positionPreview(e);
            }, 250);
        }

        function onMouseOut(e) {
            // Only hide if we left the row entirely
            var related = e.relatedTarget;
            var leavingRow = e.target.closest('.deck-card-row');
            var enteringRow = related ? related.closest('.deck-card-row') : null;
            if (leavingRow === enteringRow) return; // staying in same row

            clearTimeout(hoverTimeout);
            hoverActiveRow = null;
            hoverPreviewEl.style.display = 'none';
        }

        function onMouseMove(e) {
            if (hoverPreviewEl.style.display !== 'none') {
                positionPreview(e);
            }
        }

        function positionPreview(e) {
            var previewW = 400;
            var previewH = 560;
            var left = e.clientX + 16;
            var top = e.clientY - previewH / 2;

            // Keep within viewport
            if (left + previewW > window.innerWidth) left = e.clientX - previewW - 16;
            if (top < 8) top = 8;
            if (top + previewH > window.innerHeight - 8) top = window.innerHeight - previewH - 8;

            hoverPreviewEl.style.left = left + 'px';
            hoverPreviewEl.style.top = top + 'px';
        }

        var deckEl = document.querySelector('.deck-builder-panel .panel-body');
        if (deckEl) {
            deckEl.addEventListener('mouseover', onMouseOver);
            deckEl.addEventListener('mouseout', onMouseOut);
            deckEl.addEventListener('mousemove', onMouseMove);
        }
    }

    function historyDate(value) {
        if (!value) return '';
        var date = new Date(value);
        return isNaN(date.getTime()) ? String(value) : date.toLocaleString();
    }

    function initHistoryTab() {
        var tab = document.querySelector('.tool-tab-btn[data-tool="history"]');
        if (tab) tab.addEventListener('click', loadHistoryTab);
        var snapshotBtn = $('#history-snapshot-btn');
        if (snapshotBtn) snapshotBtn.addEventListener('click', snapshotDeckVersion);
        var refreshBtn = $('#history-refresh-btn');
        if (refreshBtn) refreshBtn.addEventListener('click', loadHistoryTab);
    }

    function historyStatus(text, kind) {
        var el = $('#history-status');
        if (!el) return;
        el.textContent = text || '';
        el.className = 'history-status ' + (kind || '');
    }

    function loadHistoryTab() {
        var versionsEl = $('#deck-versions-list');
        var historyEl = $('#deck-history-list');
        if (!deck.name) {
            if (versionsEl) versionsEl.innerHTML = '<p class="history-empty">Save the deck first.</p>';
            if (historyEl) historyEl.innerHTML = '<p class="history-empty">Save the deck first.</p>';
            return;
        }
        var name = encodeURIComponent(deck.name);
        Promise.all([
            fetch('/api/deck/versions?deck=' + name).then(function (r) { return r.json(); }),
            fetch('/api/deck/history?deck=' + name).then(function (r) { return r.json(); })
        ]).then(function (results) {
            renderVersions(results[0].versions || []);
            renderHistoryEvents(results[1].history || []);
        }).catch(function () {
            historyStatus('Could not load history.', 'error');
        });
    }

    function renderVersions(versions) {
        var el = $('#deck-versions-list');
        if (!el) return;
        if (!versions.length) {
            el.innerHTML = '<p class="history-empty">No versions yet. Click “Snapshot current”.</p>';
            return;
        }
        el.innerHTML = versions.map(function (version) {
            return '<div class="history-version-row">' +
                '<div><strong>' + escapeHtml(version.commander || 'No commander') + '</strong>' +
                '<div class="history-version-meta">' + escapeHtml(historyDate(version.createdAt)) +
                ' · ' + escapeHtml(version.total) + ' cards</div></div>' +
                '<div class="history-version-actions">' +
                    '<button type="button" data-version-diff="' + escapeAttr(version.filename) + '">Diff</button>' +
                    '<button type="button" data-version-restore="' + escapeAttr(version.filename) + '">Restore</button>' +
                '</div>' +
            '</div>';
        }).join('');
        el.querySelectorAll('[data-version-diff]').forEach(function (btn) {
            btn.addEventListener('click', function () { loadVersionDiff(btn.dataset.versionDiff); });
        });
        el.querySelectorAll('[data-version-restore]').forEach(function (btn) {
            btn.addEventListener('click', function () { restoreVersion(btn.dataset.versionRestore); });
        });
    }

    function renderHistoryEvents(events) {
        var el = $('#deck-history-list');
        if (!el) return;
        if (!events.length) {
            el.innerHTML = '<p class="history-empty">No change events yet.</p>';
            return;
        }
        el.innerHTML = events.slice().reverse().slice(0, 100).map(function (event) {
            var card = event.card && event.card.name ? event.card.name : '';
            var action = event.action === 'remove' ? '−' : '+';
            var section = event.section && event.section !== 'main' ? ' · ' + event.section : '';
            return '<div class="history-event-row">' +
                '<div><strong>' + escapeHtml(action + ' ' + card) + '</strong>' +
                '<div class="history-event-meta">' + escapeHtml(historyDate(event.timestamp)) + section + '</div></div>' +
                '<span class="history-event-meta">' + escapeHtml(event.quantity || 1) + '</span>' +
            '</div>';
        }).join('');
    }

    function snapshotDeckVersion() {
        if (!deck.name) { historyStatus('Save the deck first.', 'error'); return; }
        historyStatus('Saving snapshot…');
        fetch('/api/deck/versions/snapshot', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ name: deck.name })
        }).then(function (r) { return r.json(); }).then(function (data) {
            if (data.error) throw new Error(data.error);
            if (!data.success) { historyStatus('No snapshot created (' + data.reason + ').', 'error'); return; }
            historyStatus('Snapshot created.', 'ok');
            loadHistoryTab();
        }).catch(function (err) { historyStatus(err.message, 'error'); });
    }

    function restoreVersion(filename) {
        if (!deck.name) return;
        if (!window.confirm('Restore this version? The current deck will be snapshotted first.')) return;
        fetch('/api/deck/versions/restore', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ deck: deck.name, filename: filename })
        }).then(function (r) { return r.json(); }).then(function (data) {
            if (data.error) throw new Error(data.error);
            historyStatus('Version restored.', 'ok');
            loadSavedDeckByName(deck.name);
            loadHistoryTab();
        }).catch(function (err) { historyStatus(err.message, 'error'); });
    }

    function loadVersionDiff(filename) {
        if (!deck.name) return;
        var diffEl = $('#deck-version-diff');
        fetch('/api/deck/version/diff?deck=' + encodeURIComponent(deck.name) +
              '&filename=' + encodeURIComponent(filename))
            .then(function (r) { return r.json(); })
            .then(function (data) {
                if (data.error) throw new Error(data.error);
                var c = data.comparison || {};
                var s = c.summary || {};
                var html = '<h4>Current deck vs snapshot</h4>' +
                    '<div>+' + escapeHtml(s.added || 0) + ' added · −' +
                    escapeHtml(s.removed || 0) + ' removed · ' +
                    escapeHtml(s.changed || 0) + ' quantity changes</div>';
                function list(title, items, mode) {
                    if (!items || !items.length) return '';
                    return '<h4>' + escapeHtml(title) + '</h4><ul>' + items.map(function (item) {
                        var suffix = mode === 'changed' ? ' (' + item.from + ' → ' + item.to + ')' : '';
                        return '<li>' + escapeHtml(item.name) + suffix + '</li>';
                    }).join('') + '</ul>';
                }
                html += list('Only in current', c.removed, 'removed');
                html += list('Only in snapshot', c.added, 'added');
                html += list('Quantity changes', c.changed, 'changed');
                diffEl.innerHTML = html;
                diffEl.hidden = false;
            }).catch(function (err) { historyStatus(err.message, 'error'); });
    }

    function init() {
        initToolTabs();
        initPanelCollapse();
        initSearch();
        initDropZones();
        initButtons();
        initSortSelect();
        initGroupSelect();
        initQuickAdd();
        initSectionManagement();
        initGuideline();
        initHistoryTab();

        // Read config from Flask session
        if (window._mtgConfig) {
            globalPricingStore = window._mtgConfig.pricingStore || 'usd';
            historyWarningDays = window._mtgConfig.historyWarningDays || 30;
        }

        initRefreshPricesButton();
        initPricingStoreSelect();
        initSectionIncludeCheckboxes();
        initHistoryDeleteButton();
        enableGlobalDrag();
        enableCardPanelAddToDeck();
        initCardContextMenus();

        // Listen for eval data from embedded Commander Eval iframe
        window.addEventListener('message', function (e) {
            if (!e.data || e.data.type !== 'mtg-eval-data') return;
            deck.evalData = e.data.data;
            saveCurrentDeck();
        });

        // Click on deck card rows opens the card detail side panel
        // Click on deck section headers toggles collapse
        var deckSections = $('#deck-sections');
        if (deckSections) {
            deckSections.addEventListener('click', function (e) {
                // Section header toggle
                var header = e.target.closest('.deck-section-header');
                if (header && !e.target.closest('button')) {
                    toggleSection(header);
                    return;
                }
                // Don't open panel when clicking buttons
                if (e.target.closest('button')) return;
                var row = e.target.closest('.deck-card-row');
                if (!row) return;
                var setCode = row.dataset.setCode;
                var number = row.dataset.number;
                if (setCode && number) {
                    closeTagPopover();
                    openPanel(setCode, number);
                }
            });
        }

        // Click on custom section card rows opens the card detail side panel
        // Click on custom section headers toggles collapse
        var customSections = $('#custom-sections');
        if (customSections) {
            customSections.addEventListener('click', function (e) {
                // Section header toggle — exclude delete button clicks
                var header = e.target.closest('.custom-section-header');
                if (header && !e.target.closest('.section-delete-btn')) {
                    toggleSection(header);
                    return;
                }

                if (e.target.closest('button')) return;
                var row = e.target.closest('.deck-card-row');
                if (!row) return;
                var setCode = row.dataset.setCode;
                var number = row.dataset.number;
                if (setCode && number) {
                    closeTagPopover();
                    openPanel(setCode, number);
                }
            });
        }

        // Card hover preview — show card image on mouse hover
        initCardHoverPreview();

        // Flush any pending debounced save when the user navigates away.
        // Do not clear the timer here: on visibilitychange the page may stay
        // alive, so the debounced fetch is a fallback if the beacon is rejected.
        window.addEventListener('beforeunload', function () {
            if (needsSave && deck.name) {
                saveDeckToServer(deck, true);  // fire-and-forget via sendBeacon
            }
            flushHistory(); // fire-and-forget history events
        });

        // Also flush on visibility change (tab close / mobile background)
        window.addEventListener('visibilitychange', function () {
            if (document.visibilityState === 'hidden') {
                if (needsSave && deck.name) {
                    saveDeckToServer(deck, true);
                }
                flushHistory(); // fire-and-forget history events
            }
        });

        // Load tag catalog first, then load deck
        var urlParams = new URLSearchParams(window.location.search);
        var deckName = urlParams.get('deck');
        loadTagCatalog(function () {
            if (deckName) {
                // URLSearchParams already percent-decodes; a second decode
                // throws for names containing a literal "%".
                loadSavedDeckByName(deckName);
            } else {
                renderAll();
            }
        });

        // Link to saved decks page when empty
        var emptyState = $('#deck-empty-state');
        if (emptyState) {
            emptyState.innerHTML = '<p>No cards yet. Search for cards or drag-and-drop to start building.</p>';
        }
    }

    // Run on DOM ready
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }

})();
