/* MTG Search — Add-to-Deck Context Menu (Search & Similarity tools)
   ponytail: loaded only by search.html and similar.html. Delegates contextmenu
   on .card-cell to offer "Add to Deck ▸ <deck> ▸ <section|Main Deck>". */
(function () {
    'use strict';

    function addCard(deckName, section, setCode, number, cardName) {
        var body = { deck: deckName, setCode: setCode, number: number };
        if (section) body.section = section;

        fetch('/api/deck/add-card', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        })
        .then(function (r) { return r.json(); })
        .then(function (data) {
            if (data.success) {
                var target = section ? '"' + section + '"' : 'Main Deck';
                ContextMenu.toast('Added: ' + (data.name || cardName) + ' — ' + deckName + ' (' + target + ')');
            } else {
                ContextMenu.toast(data.error || 'Failed to add card.');
            }
        })
        .catch(function () {
            ContextMenu.toast('Failed to add card.');
        });
    }

    function buildDeckItems(deckName, sectionNames, setCode, number, cardName) {
        var children = [{
            label: 'Main Deck',
            action: function () { addCard(deckName, '', setCode, number, cardName); },
        }];
        sectionNames.forEach(function (section) {
            children.push({
                label: section,
                action: function () { addCard(deckName, section, setCode, number, cardName); },
            });
        });
        return children;
    }

    document.addEventListener('contextmenu', function (e) {
        var cell = e.target.closest('.card-cell');
        if (!cell) return;
        var setCode = cell.dataset.setCode;
        var number = cell.dataset.number;
        if (!setCode || !number) return;

        e.preventDefault();

        var nameEl = cell.querySelector('.cell-name');
        var cardName = nameEl ? nameEl.textContent : '';

        fetch('/api/deck/list')
            .then(function (r) { return r.json(); })
            .then(function (decks) {
                var items;
                if (!decks || decks.length === 0) {
                    items = [{ label: 'No saved decks yet', disabled: true }];
                } else {
                    items = [{
                        label: 'Add to Deck',
                        children: decks.map(function (deck) {
                            var sectionNames = Object.keys(deck.sections || {});
                            return {
                                label: deck.name,
                                children: buildDeckItems(deck.name, sectionNames, setCode, number, cardName),
                            };
                        }),
                    }];
                }
                ContextMenu.show(e.clientX, e.clientY, items);
            })
            .catch(function () {
                ContextMenu.show(e.clientX, e.clientY, [{ label: 'Failed to load decks', disabled: true }]);
            });
    });
})();
