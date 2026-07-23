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

    /* ─── Deck Data Model ─── */
    function Deck() {
        this.name = '';
        this.commander = null;    // { uuid, setCode, number, name, ... }
        this.cards = [];          // [{ uuid, setCode, number, name, quantity, ... }]
        this.evalData = null;     // Commander eval analysis (from embedded eval iframe)
    }

    Deck.prototype.addCard = function (cardData) {
        if (!cardData || !cardData.uuid) return { error: 'Invalid card data.' };

        // Commander logic
        if (cardData.isLegendary && cardData.isCreature) {
            if (!this.commander) {
                this.commander = cardData;
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
            if (existing.quantity >= 4) {
                return { error: 'Maximum 4 copies per card.' };
            }
            existing.quantity++;
            return { ok: true };
        }

        cardData.quantity = 1;
        cardData.tags = [];
        this.cards.push(cardData);
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

    Deck.prototype.removeCard = function (uuid) {
        if (this.commander && this.commander.uuid === uuid) {
            this.commander = null;
            return true;
        }
        var idx = this.cards.findIndex(function (c) { return c.uuid === uuid; });
        if (idx === -1) return false;
        this.cards.splice(idx, 1);
        return true;
    };

    Deck.prototype.addWithQuantity = function (cardData, qty) {
        /* Bulk add — bypasses the 4-copy limit for text import use cases
           (e.g. basic lands). Enforces deck size limit. Always goes to 99. */
        if (!cardData || !cardData.uuid) return { error: 'Invalid card data.' };
        if (qty < 1) return { error: 'Quantity must be at least 1.' };

        var currentTotal = this.getTotalCount();
        if (currentTotal + qty > MAX_CARDS) {
            return { error: 'Adding ' + qty + '× ' + cardData.name + ' would exceed the ' + MAX_CARDS + '-card limit.' };
        }

        var existing = this.cards.find(function (c) { return c.uuid === cardData.uuid; });
        if (existing) {
            existing.quantity += qty;
            return { ok: true };
        }

        cardData.quantity = qty;
        cardData.tags = [];
        this.cards.push(cardData);
        return { ok: true };
    };

    Deck.prototype.setQuantity = function (uuid, qty) {
        if (qty < 1) return false;
        var card = this.cards.find(function (c) { return c.uuid === uuid; });
        if (!card) return false;
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
    };

    Deck.prototype.getCommanderCI = function () {
        return (this.commander && this.commander.colorIdentity) || '';
    };

    Deck.prototype.toExport = function () {
        return {
            name: this.name,
            commander: this.commander,
            evalData: this.evalData,
            cards: this.cards.map(function (c) {
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
                };
            }),
        };
    };

    Deck.prototype.fromImport = function (data) {
        this.name = data.name || '';
        this.commander = data.commander || null;
        this.evalData = data.evalData || null;
        this.cards = (data.cards || []).map(function (c) {
            var card = Object.assign({}, c, { quantity: c.quantity || 1 });
            if (!card.tags) card.tags = [];
            return card;
        });
    };

    /* ─── Server-side Persistence ─── */

    var saveTimer = null;
    var needsSave = false;

    function saveDeckToServer(deck, useBeacon) {
        if (!deck.name) {
            updateSaveStatus('unsaved', 'Name your deck to save');
            return;
        }
        var payload = JSON.stringify(deck.toExport());

        if (useBeacon && navigator.sendBeacon) {
            navigator.sendBeacon('/api/deck/save', new Blob([payload], {type: 'application/json'}));
            return;
        }

        updateSaveStatus('saving', 'Saving…');
        fetch('/api/deck/save', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: payload,
        })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (data.success) {
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
                '<span class="tag-option-dot" style="background:' + tag.color + '"></span>' +
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

    /* ── Tag Filter Bar ─── */
    function renderTagFilterBar() {
        // Rebuild the tag filter chips above the card list.
        // Called after tag changes to keep the render pipeline intact.
        var el = $('#tag-filter-bar');
        if (!el) return;

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
            var color = tag ? tag.color : '#666';
            html += '<button class="tag-filter-chip" data-tag="' + escapeAttr(tagName) + '" style="--tag-color:' + color + '">' +
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
            var color = tag ? tag.color : '#666';
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
        renderSections();
        renderStats();
        renderDeckCharts();
        updateDeckNameInput();
    }

    function renderCommander() {
        var slot = $('#commander-slot');
        if (!slot) return;
        if (deck.commander) {
            var c = deck.commander;
            slot.innerHTML =
                '<div class="commander-slot-filled">' +
                    '<img src="' + (c.imageUrl || '') + '" alt="' + escapeHtml(c.name) + '" class="commander-card-img">' +
                    '<div class="commander-card-info">' +
                        '<div class="commander-card-name">' + escapeHtml(c.name) + '</div>' +
                        '<div class="commander-card-type">' + escapeHtml(c.type || '') + '</div>' +
                        '<div class="commander-card-ci">Color Identity: ' + (c.colorIdentity || '').split(',').map(function(ci) { var s = ci.trim(); return s ? '<span class="ms ms-' + s.toLowerCase() + '"></span>' : ''; }).join('') + '</div>' +
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

        // If all cards are filtered out, show a brief message
        if (cardsToRender.length === 0) {
            container.innerHTML = '<div class="deck-empty-filtered">No cards in deck.</div>';
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
                var dot = tag ? '<span class="tag-section-dot" style="background:' + tag.color + '"></span>' : '';

                html += '<div class="deck-section" data-section="' + escapeAttr(tagName.toLowerCase()) + '">';
                html += '<div class="deck-section-header" onclick="toggleSection(this)">' +
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
                html += '<div class="deck-section-header" onclick="toggleSection(this)">' +
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
                var newQty = card.quantity + delta;
                if (deck.setQuantity(uuid, newQty)) {
                    closeTagPopover();
                    renderAll();
                    saveCurrentDeck();
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
                var color = tag ? tag.color : '#666';
                tagBadges += '<span class="card-tag-dot" style="background:' + color + '" title="' + escapeAttr(t) + '"></span>';
            });
            tagBadges += '</span>';
        }

        return '<div class="deck-card-row' + offColorClass + '" data-uuid="' + escapeAttr(card.uuid) + '" data-set-code="' + escapeAttr(card.setCode) + '" data-number="' + escapeAttr(card.number) + '">' +
            '<button class="qty-btn qty-plus" data-uuid="' + escapeAttr(card.uuid) + '" data-delta="1" title="Increase quantity">+</button>' +
            '<button class="qty-btn qty-minus" data-uuid="' + escapeAttr(card.uuid) + '" data-delta="-1" title="Decrease quantity">−</button>' +
            '<span class="card-quantity">' + card.quantity + '×</span>' +
            (shouldShowQuantityWarning(card) ? '<span class="qty-warn-icon" title="Exceeds singleton limit">⚠</span>' : '') +
            '<span class="card-mv" title="Mana Value">' + (card.manaValue !== undefined ? card.manaValue : '—') + '</span>' +
            '<span class="card-name-text" title="' + escapeAttr(card.name) + '">' + escapeHtml(card.name) + '</span>' +
            '<span class="card-mana-cost">' + renderManaSymbols(card.manaCost || '') + '</span>' +
            tagBadges +
            '<button class="card-tag-btn" data-uuid="' + escapeAttr(card.uuid) + '" title="Edit tags">🏷</button>' +
            '<button class="card-remove-btn" data-uuid="' + escapeAttr(card.uuid) + '" title="Remove">✕</button>' +
        '</div>';
    }

    function getSectionKey(card) {
        var types = (card.types || '').split(' ');
        for (var i = 0; i < TYPE_ORDER.length; i++) {
            if (types.indexOf(TYPE_ORDER[i]) !== -1) return TYPE_ORDER[i];
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
        // Show warning only when quantity > 1 (singleton format)
        if (card.quantity <= 1) return false;

        // Basic Lands are exempt (e.g., Forest, Mountain, Island)
        var supertypes = card.supertypes || '';
        if (supertypes.indexOf('Basic') !== -1) return false;

        // Cards with "any number" Oracle text are exempt
        // e.g., "A deck can have any number of cards named Hare Apparent"
        var text = card.text || '';
        if (/a deck can have any number of cards named/i.test(text)) return false;

        return true;
    }

    function isOffColorCard(card) {
        var ci = deck.getCommanderCI();
        if (!ci || !card.colorIdentity) return false;
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

        var ci = deck.getCommanderCI();
        if (!ci) {
            container.innerHTML = '<span style="color:var(--text-dim);font-size:0.75rem;">Set a commander to check color identity</span>';
            return;
        }

        var offColorCards = deck.cards.filter(isOffColorCard);
        if (offColorCards.length > 0) {
            var names = offColorCards.map(function (c) { return c.name; }).join(', ');
            container.innerHTML = '<span style="color:#f85149;font-size:0.75rem;">⚠ Off-color: ' + names + '</span>';
        } else {
            container.innerHTML = '<span style="color:#3fb950;font-size:0.75rem;">✓ All cards within color identity</span>';
        }
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

        var colorHex = { W: '#f9faf4', U: '#0e68ab', B: '#b38e5d', R: '#d3202a', G: '#00733e', C: '#a0a0a0', Generic: '#6e7681' };
        var colorNames = { W: 'White', U: 'Blue', B: 'Black', R: 'Red', G: 'Green', C: 'Colorless', Generic: 'Generic' };
        var order = ['W','U','B','R','G','C','Generic'];

        // Count actual mana pips from manaCost strings, weighted by quantity
        var pipCounts = { W: 0, U: 0, B: 0, R: 0, G: 0, C: 0, Generic: 0 };
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
                if (inner.indexOf('/') !== -1) {
                    inner.split('/').forEach(function (p) {
                        if (pipCounts.hasOwnProperty(p)) {
                            pipCounts[p] += qty;
                            totalPips += qty;
                        } else if (/^\d+$/.test(p)) {
                            pipCounts['Generic'] += qty;
                            totalPips += qty;
                        }
                    });
                    return;
                }

                // Numeric generic: {1}, {2}, {3}, ... and {X}
                if (/^\d+$/.test(inner) || inner === 'X') {
                    pipCounts['Generic'] += qty;
                    totalPips += qty;
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
        return manaCost.replace(/\{([^}]+)\}/g, function (match, symbol) {
            var cls = symbol.toLowerCase();
            // Handle 2/W, 2/U, 2/B, 2/R, 2/G hybrid
            if (symbol.indexOf('/') !== -1) {
                return '<span class="ms ms-' + cls.replace('/', '-') + '"></span>';
            }
            // Handle tap symbols, X, Y, Z, P
            if (symbol === 'T' || symbol === 'Q' || symbol === 'S' || symbol === 'E' || symbol === 'PW' || symbol === 'X' || symbol === 'Y' || symbol === 'Z') {
                return '<span class="ms ms-' + cls + '"></span>';
            }
            // Handle P (phyrexian): {W/P} -> ms-wp, {B/P} -> ms-bp
            if (symbol.indexOf('P') === symbol.length - 1 && symbol.length > 1) {
                return '<span class="ms ms-' + symbol.charAt(0).toLowerCase() + 'p"></span>';
            }
            return '<span class="ms ms-' + cls + '"></span>';
        });
    }

    /* ─── Utilities ─── */
    function escapeHtml(str) {
        var div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    function escapeAttr(str) {
        return String(str).replace(/"/g, '&quot;').replace(/'/g, '&#39;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    function saveCurrentDeck() {
        if (!deck.name) {
            updateSaveStatus('unsaved', 'Name your deck to save');
            return;
        }
        needsSave = true;
        // Debounce saves — 600ms cooldown between writes
        if (saveTimer) clearTimeout(saveTimer);
        updateSaveStatus('saving', 'Saving…');
        saveTimer = setTimeout(function () {
            saveDeckToServer(deck);
        }, 600);
    }

    function updateDeckNameInput() {
        var input = $('#deck-name');
        if (input && input !== document.activeElement) {
            input.value = deck.name;
            input.classList.toggle('unnamed', !deck.name);
        }
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
        if (result.isCommander) {
            showToast('Commander set: ' + cardData.name);
        } else {
            showToast('Added: ' + cardData.name);
        }
    }

    /* ── Toast Notifications ─── */
    function showToast(message) {
        var existing = document.querySelector('.deck-toast');
        if (existing) existing.remove();

        var toast = document.createElement('div');
        toast.className = 'deck-toast';
        toast.textContent = message;
        document.body.appendChild(toast);

        setTimeout(function () {
            toast.style.opacity = '0';
            toast.style.transform = 'translateY(10px)';
            setTimeout(function () { toast.remove(); }, 300);
        }, 2000);
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
                    deck.commander = fullCard;
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

    function runDeckSearch(page) {
        page = page || 1;
        var params = searchParams ? new URLSearchParams(searchParams.toString()) : new URLSearchParams();

        // Build search params only on fresh search (not page navigation)
        if (!searchParams || page !== searchPage) {
            // If navigating pages, re-use cached params
            if (searchParams) {
                params = new URLSearchParams(searchParams.toString());
            } else {
                // Text Search
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
            }

            params.set('unique', 'cards');

            // Per-page
            var perPageEl = $('#deck-search-per-page');
            if (perPageEl) params.set('per_page', perPageEl.value);

            searchParams = params;
        }

        params.set('page', page);

        var grid = $('#deck-search-results');
        var count = $('#deck-search-count');
        var pagination = $('#deck-search-pagination');
        if (grid) grid.innerHTML = '<div class="panel-loading">Searching…</div>';
        if (pagination) pagination.innerHTML = '';

        fetch('/search?' + params.toString())
            .then(function (r) { return r.text(); })
            .then(function (html) {
                var parser = new DOMParser();
                var doc = parser.parseFromString(html, 'text/html');
                var results = doc.querySelector('.card-grid');
                var resultCount = doc.querySelector('.result-count');
                var pageCount = doc.querySelector('.pagination .page-indicator');

                if (grid) {
                    if (results) {
                        grid.innerHTML = results.innerHTML;
                        makeCardsDraggable(grid);
                    } else {
                        grid.innerHTML = '<div>No cards found.</div>';
                    }
                }
                if (count) {
                    count.textContent = resultCount ? resultCount.textContent : '';
                }

                // Extract total pages from the page indicator text "Page X of Y"
                if (pageCount) {
                    var m = pageCount.textContent.match(/of\s+(\d+)/);
                    searchTotalPages = m ? parseInt(m[1], 10) : 0;
                } else {
                    searchTotalPages = 0;
                }
                searchPage = page;
                renderSearchPagination();
            })
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

    /* ─── Drop Zone ─── */
    function initDropZones() {
        // Deck panel drop zone
        var deckPanel = $('.deck-builder-panel .panel-body');
        var commanderSlot = $('#commander-slot');

        if (deckPanel) {
            setupDropZone(deckPanel, function (card) {
                addCardToDeck(card, true);  // always to 99, not commander
            });
        }

        if (commanderSlot) {
            setupDropZone(commanderSlot, function (card) {
                if (!card.isLegendary || !card.isCreature) {
                    showToast('Commander must be a legendary creature.');
                    return;
                }
                addCardToDeck(card);
            });
        }
    }

    function setupDropZone(element, onCardFound) {
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

            // 1. Try application/mtg-card first (internal drag from search results)
            //    Resolve directly — avoids a second server round-trip for the ID lookup.
            var mtgData = e.dataTransfer.getData('application/mtg-card');
            if (mtgData) {
                try {
                    var parsed = JSON.parse(mtgData);
                    lookupCardBySetNumber(parsed.setCode, parsed.number, function (card, err) {
                        if (err) { showToast(err); return; }
                        onCardFound(card);
                    });
                    return;
                } catch (err) { /* fall through */ }
            }

            // 2. Try Scryfall UUID extraction (from external image / file drops)
            var scryfallId = _extractScryfallIdFromTransfer(e.dataTransfer);
            if (scryfallId) {
                lookupCardByScryfallId(scryfallId, function (card, err) {
                    if (err) { showToast(err); return; }
                    onCardFound(card);
                });
                return;
            }

            showToast('Could not identify a card from that drop.');
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

        // Export JSON
        var exportBtn = $('#deck-export-btn');
        if (exportBtn) {
            exportBtn.addEventListener('click', function () {
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
            });
        }

        // Text Import
        initTextImport();

        // Clear
        var clearBtn = $('#deck-clear-btn');
        if (clearBtn) {
            clearBtn.addEventListener('click', function () {
                if (deck.getTotalCount() === 0 && !deck.commander) {
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
        $('#deck-name').value = deck.name;
        renderAll();
        restoreEvalData();
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
                    addCardToDeck(card);
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

    /* ─── Sort Select ─── */
    function initSortSelect() {
        var sel = $('#deck-sort-select');
        if (!sel) return;
        sel.addEventListener('change', function () {
            currentSort = sel.value;
            renderSections();
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

        initToolAutocomplete('deck-quick-add-input', 'deck-quick-add-autocomplete', {
            keepValue: true,
            onSelect: function (card) {
                _quickAddSelected = card;
            },
            onEnterEmpty: addSelected
        });
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
            // Parse lines: "# Card Name" or "Card Name" (default qty 1)
            var entries = [];
            for (var i = 0; i < lines.length; i++) {
                var line = lines[i].trim();
                var m = line.match(/^(\d+)\s+(.+)/);
                if (m) {
                    entries.push({ qty: parseInt(m[1], 10), name: m[2].trim() });
                } else {
                    // No quantity prefix — treat as 1
                    entries.push({ qty: 1, name: line });
                }
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

                        // Replace mode: clear existing deck first
                        if (replaceCb.checked) {
                            deck.clear();
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

    /* ── Initialize ─── */
    function init() {
        initToolTabs();
        initSearch();
        initDropZones();
        initButtons();
        initSortSelect();
        initGroupSelect();
        initQuickAdd();
        enableGlobalDrag();
        enableCardPanelAddToDeck();

        // Listen for eval data from embedded Commander Eval iframe
        window.addEventListener('message', function (e) {
            if (!e.data || e.data.type !== 'mtg-eval-data') return;
            deck.evalData = e.data.data;
            saveCurrentDeck();
        });

        // Click on deck card rows opens the card detail side panel
        var deckSections = $('#deck-sections');
        if (deckSections) {
            deckSections.addEventListener('click', function (e) {
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

        // Flush any pending debounced save when the user navigates away
        window.addEventListener('beforeunload', function () {
            if (saveTimer) clearTimeout(saveTimer);
            if (needsSave && deck.name) {
                saveDeckToServer(deck, true);  // fire-and-forget via sendBeacon
            }
        });

        // Also flush on visibility change (tab close / mobile background)
        window.addEventListener('visibilitychange', function () {
            if (document.visibilityState === 'hidden') {
                if (saveTimer) clearTimeout(saveTimer);
                if (needsSave && deck.name) {
                    saveDeckToServer(deck, true);
                }
            }
        });

        // Load tag catalog first, then load deck
        var urlParams = new URLSearchParams(window.location.search);
        var deckName = urlParams.get('deck');
        loadTagCatalog(function () {
            if (deckName) {
                loadSavedDeckByName(decodeURIComponent(deckName));
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
