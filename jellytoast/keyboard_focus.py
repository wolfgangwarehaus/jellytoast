"""Registry of views that track a keyboard-navigation focus flag.

Views that show accent focus rings during keyboard nav set a
``_keyboard_mode`` attribute. A single app-level mouse-press filter clears
that flag on every such view (any click puts the rings away). Previously the
filter walked ``QApplication.allWidgets()`` on every press; this registry lets
it iterate only the handful of views that actually own the flag.

Lives in a leaf module (imported by both ``jellytoast.app`` and the view
modules) so the registration doesn't create an ``app`` ↔ view-module
import cycle.
"""

from __future__ import annotations

import weakref

from PySide6.QtCore import Qt

# WeakSet so a destroyed view drops out automatically — no manual deregister.
_KEYBOARD_MODE_VIEWS: "weakref.WeakSet" = weakref.WeakSet()


def register_keyboard_mode_view(view) -> None:
    """Register a view that owns a ``_keyboard_mode`` flag (and usually a
    ``viewport()``). Idempotent."""
    _KEYBOARD_MODE_VIEWS.add(view)


def clear_all_keyboard_mode() -> None:
    """Drop ``_keyboard_mode`` on every registered view and repaint it.

    Snapshots the set to a list first so a view destroyed mid-iteration (GC)
    doesn't break the walk. Matches the old ``allWidgets()`` filter exactly:
    clear the flag, and ``viewport().update()`` only when the view exposes a
    viewport."""
    for w in list(_KEYBOARD_MODE_VIEWS):
        if getattr(w, "_keyboard_mode", False):
            w._keyboard_mode = False
            vp = getattr(w, "viewport", None)
            if callable(vp):
                vp().update()


# ── Shared keyboard-mode wiring for list-backed views ────────────────────
#
# Every list view that wants the keyboard-nav focus ring follows the same
# recipe (first proven on library_grid._LibraryListView): set a
# ``_keyboard_mode`` flag, engage it on keyboard focus / arrow keys, clear
# it on focus-out, and gate the delegate's ring paint on it. These helpers
# package that recipe so each view delegates to them instead of
# copy-pasting the logic — keeping every list surface consistent.

_KEYBOARD_FOCUS_REASONS = (
    Qt.FocusReason.TabFocusReason,
    Qt.FocusReason.BacktabFocusReason,
    Qt.FocusReason.ShortcutFocusReason,
    Qt.FocusReason.OtherFocusReason,
)
_ARROW_KEYS = (
    Qt.Key.Key_Down,
    Qt.Key.Key_Up,
    Qt.Key.Key_Left,
    Qt.Key.Key_Right,
)


def _seed_first_index(view) -> None:
    """Seed ``currentIndex`` to the top visible row if nothing is current
    yet — so the focus wash paints immediately and arrow keys step from a
    sensible base (Qt's default Down would otherwise just scroll)."""
    if view.currentIndex().isValid():
        return
    model = view.model()
    if model is None or model.rowCount() == 0:
        return
    seed = view.indexAt(view.viewport().rect().topLeft())
    if not seed.isValid():
        seed = model.index(0, 0)
    view.setCurrentIndex(seed)


def keyboard_focus_in(view, event) -> None:
    """Call at the top of a list view's ``focusInEvent`` (before super()).
    Engages keyboard mode + seeds the cursor when focus arrived via Tab /
    Shortcut / programmatic setFocus — not a mouse click (the ring is a
    keyboard affordance, not click feedback)."""
    if event.reason() in _KEYBOARD_FOCUS_REASONS:
        view._keyboard_mode = True
        _seed_first_index(view)
        view.viewport().update()


def keyboard_focus_out(view, event) -> None:
    """Call at the top of a list view's ``focusOutEvent`` (before super())."""
    view._keyboard_mode = False
    view.viewport().update()


def keyboard_arrow_press(view, event) -> bool:
    """Call at the top of a list view's ``keyPressEvent``. On an arrow key,
    engages keyboard mode and seeds the cursor if nothing is current.
    Returns True when it seeded the cursor and the caller should accept the
    event and return (skip super()) — otherwise False, let super() move
    the cursor normally."""
    if event.key() not in _ARROW_KEYS:
        return False
    need_seed = not view.currentIndex().isValid()
    if not getattr(view, "_keyboard_mode", False):
        view._keyboard_mode = True
        view.viewport().update()
    if need_seed:
        _seed_first_index(view)
        if view.currentIndex().isValid():
            event.accept()
            return True
    return False


def focus_first_item_on(view) -> None:
    """Drop keyboard focus on ``view``'s first visible row and engage
    keyboard mode. Back a wrapper's ``focus_first_item()`` with this so the
    app-level chrome-Down dive can reach the list."""
    if view is None:
        return
    model = view.model()
    if model is None or model.rowCount() == 0:
        return
    _seed_first_index(view)
    view._keyboard_mode = True
    view.setFocus(Qt.FocusReason.OtherFocusReason)
    view.viewport().update()
