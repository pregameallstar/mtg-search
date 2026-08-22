/* MTG Search — Context Menu
   ponytail: vanilla JS, one reused menu element + one reused toast.
   Exposes window.ContextMenu with no page wiring or feature knowledge:
   callers pass { label, action?, children? } items (children = submenu) and
   { type: 'sep' } separators. */
(function () {
    'use strict';

    var MENU_ID = 'context-menu';
    var TOAST_ID = 'context-toast';
    var SUBMENU_GAP = 4;      // px between a menu and its submenu
    var SUBMENU_WIDTH = 220;  // px reserved when clamping submenu to viewport
    var OPEN_DELAY = 60;      // ms before a submenu opens on hover
    var CLOSE_DELAY = 150;    // ms grace before a submenu closes on mouse-out

    var closeTimer = null;
    var openSubmenus = [];    // [{ depth, el }] — open submenus, shallow→deep

    function el(tag, className) {
        var node = document.createElement(tag);
        if (className) node.className = className;
        return node;
    }

    function removeMenu() {
        var menu = document.getElementById(MENU_ID);
        if (menu) menu.remove();
    }

    function closeSubmenusFrom(depth) {
        // Remove open submenus at or below `depth`, keeping shallower ancestors.
        for (var i = openSubmenus.length - 1; i >= 0; i--) {
            if (openSubmenus[i].depth >= depth) {
                openSubmenus[i].el.remove();
                openSubmenus.splice(i, 1);
            }
        }
    }

    function closeAllSubmenus() {
        openSubmenus.forEach(function (s) { s.el.remove(); });
        openSubmenus = [];
    }

    function cancelClose() {
        if (closeTimer) { clearTimeout(closeTimer); closeTimer = null; }
    }

    function scheduleCloseFrom(depth) {
        cancelClose();
        closeTimer = setTimeout(function () {
            closeSubmenusFrom(depth);
            closeTimer = null;
        }, CLOSE_DELAY);
    }

    function position(node, x, y) {
        // Clamp so the menu stays fully in the viewport.
        var rect = node.getBoundingClientRect();
        var left = Math.min(x, window.innerWidth - rect.width - 4);
        var top = Math.min(y, window.innerHeight - rect.height - 4);
        node.style.left = Math.max(4, left) + 'px';
        node.style.top = Math.max(4, top) + 'px';
    }

    function positionSubmenu(submenu, row) {
        // Appended to body as a sibling (not DOM-nested), so position it
        // relative to its parent row and clamp to the viewport.
        submenu.style.display = 'block';  // .context-submenu is display:none by default
        document.body.appendChild(submenu);

        var rect = row.getBoundingClientRect();
        submenu.style.top = rect.top + 'px';
        if (rect.right + SUBMENU_WIDTH < window.innerWidth) {
            submenu.style.left = rect.right + SUBMENU_GAP + 'px';
        } else {
            submenu.style.left = Math.max(4, rect.left - SUBMENU_WIDTH - SUBMENU_GAP) + 'px';
        }

        // Clamp vertical overflow after the element is measurable.
        var sRect = submenu.getBoundingClientRect();
        if (sRect.bottom > window.innerHeight - 4) {
            submenu.style.top = Math.max(4, window.innerHeight - sRect.height - 4) + 'px';
        }
    }

    function buildItem(item, depth) {
        if (item && item.type === 'sep') {
            return el('div', 'context-menu-sep');
        }

        var row = el('div', 'context-menu-item' + (item.disabled ? ' disabled' : ''));
        var label = el('span', 'context-menu-label');
        label.textContent = item.label;
        row.appendChild(label);

        if (item.children && item.children.length) {
            row.classList.add('has-submenu');
            var arrow = el('span', 'context-menu-arrow');
            arrow.textContent = '▸';
            row.appendChild(arrow);

            var childDepth = depth + 1;
            var submenu = el('div', 'context-menu context-submenu');
            item.children.forEach(function (child) {
                submenu.appendChild(buildItem(child, childDepth));
            });

            var openTimer = null;

            row.addEventListener('mouseenter', function () {
                cancelClose();
                if (openTimer) clearTimeout(openTimer);
                openTimer = setTimeout(function () {
                    closeSubmenusFrom(childDepth);
                    positionSubmenu(submenu, row);
                    openSubmenus.push({ depth: childDepth, el: submenu });
                }, OPEN_DELAY);
            });
            row.addEventListener('mouseleave', function () {
                if (openTimer) { clearTimeout(openTimer); openTimer = null; }
                scheduleCloseFrom(childDepth);
            });

            // Keep the submenu open while the pointer is over it (and its own
            // children) — this is what lets the pointer actually reach items.
            submenu.addEventListener('mouseenter', cancelClose);
            submenu.addEventListener('mouseleave', function () {
                scheduleCloseFrom(childDepth);
            });
        }

        if (!item.disabled && item.action) {
            row.addEventListener('click', function (e) {
                e.stopPropagation();
                close();
                item.action();
            });
        }

        return row;
    }

    function close() {
        removeMenu();
        cancelClose();
        closeAllSubmenus();
    }

    function show(x, y, items) {
        close();
        var menu = el('div', 'context-menu');
        menu.id = MENU_ID;
        items.forEach(function (item) {
            menu.appendChild(buildItem(item, 0));
        });
        document.body.appendChild(menu);
        position(menu, x, y);
        return menu;
    }

    function toast(message, duration) {
        var existing = document.getElementById(TOAST_ID);
        if (existing) existing.remove();

        var t = el('div', 'context-toast');
        t.id = TOAST_ID;
        t.textContent = message;
        document.body.appendChild(t);

        var dur = duration || 2000;
        setTimeout(function () {
            t.style.opacity = '0';
            t.style.transform = 'translateY(10px)';
            setTimeout(function () { t.remove(); }, 300);
        }, dur);
    }

    // Auto-close on any outside interaction.
    document.addEventListener('click', close);
    document.addEventListener('contextmenu', close);
    document.addEventListener('keydown', function (e) {
        if (e.key === 'Escape') close();
    });
    window.addEventListener('scroll', close, true);
    window.addEventListener('blur', close);

    window.ContextMenu = {
        show: show,
        close: close,
        toast: toast,
    };
})();
