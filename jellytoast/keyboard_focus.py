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

from PySide6.QtCore import QEvent, QObject, Qt

from jellytoast.design_tokens import rad

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

# Reasons that are keyboard-driven by construction — always ring.
_KEYBOARD_FOCUS_REASONS = (
    Qt.FocusReason.TabFocusReason,
    Qt.FocusReason.BacktabFocusReason,
    Qt.FocusReason.ShortcutFocusReason,
)
# Ambiguous: both a deliberate programmatic focus AND Qt's own focus
# reassignment when a stacked page hides. Only rings when the user's last
# input was a key — see the modality note below.
_AMBIGUOUS_FOCUS_REASON = Qt.FocusReason.OtherFocusReason
_ARROW_KEYS = (
    Qt.Key.Key_Down,
    Qt.Key.Key_Up,
    Qt.Key.Key_Left,
    Qt.Key.Key_Right,
)

# ── Input modality (the focus-visible rule) ──────────────────────────────
#
# Was the user's last interaction a key or a click? Qt can't tell us on its
# own: when a stacked page hides, Qt hands focus to the incoming view with
# OtherFocusReason — the SAME reason a deliberate programmatic focus uses.
# Treating that as keyboard-driven lit the first album's ring every time the
# user clicked Home out of the now-playing page.
#
# Dropping OtherFocusReason outright would fix the click but break the
# keyboard: someone who tabs to Home and presses Enter gets the same
# reassignment and *should* keep a visible cursor. So gate it on how the
# navigation was actually driven — the same idea as CSS :focus-visible.
_last_input_keyboard = False


def note_keyboard_input() -> None:
    """Record that the user just pressed a key (app-level filter)."""
    global _last_input_keyboard
    _last_input_keyboard = True


def note_pointer_input() -> None:
    """Record that the user just clicked / tapped (app-level filter)."""
    global _last_input_keyboard
    _last_input_keyboard = False


def last_input_was_keyboard() -> bool:
    return _last_input_keyboard


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
    Engages keyboard mode + seeds the cursor when focus arrived by
    keyboard — not a mouse click (the ring is a keyboard affordance, not
    click feedback).

    Tab / Backtab / Shortcut are keyboard by construction. OtherFocusReason
    is ambiguous — it covers both a deliberate ``setFocus`` and Qt's
    automatic reassignment when a stacked page is hidden — so it only rings
    when the user's last input was actually a key. That is what keeps the
    ring off the first album when you CLICK Home out of the now-playing
    page, while still showing it for a keyboard user who tabs to Home and
    presses Enter."""
    reason = event.reason()
    engaged = reason in _KEYBOARD_FOCUS_REASONS or (
        reason == _AMBIGUOUS_FOCUS_REASON and last_input_was_keyboard()
    )
    if engaged:
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


def keyboard_cursor_active(view, index) -> bool:
    """True when ``index`` is ``view``'s current (keyboard-cursor) row AND
    the view is in keyboard mode — for a delegate to gate its focus
    highlight on.

    Deliberately keyed on ``currentIndex`` rather than
    ``QStyle.StateFlag.State_HasFocus``: on a ``NoSelection`` list view the
    cursor moves correctly but the Qt focus flag only flickers for a frame
    (the "grey flash"), so painting off it leaves no stable highlight."""
    if view is None or not getattr(view, "_keyboard_mode", False):
        return False
    return index.row() == view.currentIndex().row()


# ── Left/Right arrow traversal across a horizontal button cluster ────────
#
# The app's Tab cycles between SECTIONS (top bar / content / transport);
# within a section the user moves between buttons with Left/Right. Chrome
# button rows (the top-bar controls, the transport buttons) get this via
# install_arrow_nav(). Each button becomes Tab-focusable but NOT
# click-focusable (TabFocus) so the focus ring is a keyboard-only
# affordance, and Left/Right steps focus across the visible+enabled buttons.


# Focus reasons that count as keyboard navigation for chrome buttons (top
# bar, transport). A chrome button shows its accent focus ring ONLY when
# focus arrived this way — not boot auto-focus (ActiveWindow / Other), a
# mouse path, or a closing popup — so nothing is highlighted on launch.
_CHROME_KEYBOARD_REASONS = (
    Qt.FocusReason.TabFocusReason,
    Qt.FocusReason.BacktabFocusReason,
    Qt.FocusReason.ShortcutFocusReason,
)


class _ArrowNav(QObject):
    """Event filter that moves focus across a button list on Left/Right."""

    def __init__(self, buttons):
        super().__init__()
        self._buttons = list(buttons)

    def eventFilter(self, obj, event):
        et = event.type()
        # Keyboard-only focus ring: reject focus that didn't arrive via the
        # keyboard (boot auto-focus, a closing popup) so a chrome button
        # never shows its accent ring on launch. Tab / arrow nav focuses
        # with TabFocusReason, which passes through.
        if et == QEvent.Type.FocusIn:
            if event.reason() not in _CHROME_KEYBOARD_REASONS:
                obj.clearFocus()
            return False
        if et != QEvent.Type.KeyPress or event.modifiers():
            return False
        key = event.key()
        # Enter/Return activates the focused button — a non-default
        # QPushButton doesn't fire on Enter on its own.
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            obj.click()
            return True
        if key not in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            return False
        # Recompute the reachable set each press — top-bar controls show/hide
        # with the view (library controls, window buttons, the Music dropdown).
        live = [b for b in self._buttons if b.isVisible() and b.isEnabled()]
        if obj not in live:
            return False
        i = live.index(obj)
        j = i + (1 if key == Qt.Key.Key_Right else -1)
        if 0 <= j < len(live):
            live[j].setFocus(Qt.FocusReason.TabFocusReason)
            return True
        # At an end — consume so focus doesn't leak out of the cluster.
        return True


class _RowGridNav(QObject):
    """Keyboard nav across a VERTICAL list of QFrame rows whose action
    widgets are the focus targets — Left/Right within a row, Up/Down between
    rows (to the same column, clamped). Drives a per-row keyboard-focus
    highlight: when a target in a row holds focus, the row's ``_kb_active``
    flag is set so its paintEvent can draw the accent ring
    (``paint_kb_row_ring``). For the model/view lists use the
    keyboard_focus_in/out + keyboard_cursor_active recipe instead; this is
    for the hand-built QFrame-row surfaces (Smart playlists, Radio,
    Downloads) that have no QListView/currentIndex."""

    def __init__(self, get_rows, get_targets):
        super().__init__()
        self._get_rows = get_rows        # () -> ordered visible row widgets
        self._get_targets = get_targets  # (row) -> [focus targets L->R]

    def wire(self, row) -> None:
        """Make ``row``'s targets keyboard-focusable + filtered. Call once
        per row at build time."""
        row._kb_active = False
        for t in self._get_targets(row):
            t.setFocusPolicy(Qt.FocusPolicy.TabFocus)
            t.installEventFilter(self)

    def _locate(self, target):
        for row in self._get_rows():
            targets = self._get_targets(row)
            if target in targets:
                return row, targets, targets.index(target)
        return None, None, -1

    def _set_active(self, row, on) -> None:
        if row is not None and getattr(row, "_kb_active", False) != on:
            row._kb_active = on
            row.update()

    def eventFilter(self, obj, event):
        et = event.type()
        if et == QEvent.Type.FocusIn:
            row, _, _ = self._locate(obj)
            self._set_active(row, True)
            return False
        if et == QEvent.Type.FocusOut:
            row, _, _ = self._locate(obj)
            if row is not None:
                # Defer: focus may be moving to a sibling target in the SAME
                # row (Left/Right) — only clear once it has settled elsewhere.
                from PySide6.QtCore import QTimer

                QTimer.singleShot(0, lambda r=row: self._maybe_clear(r))
            return False
        if et != QEvent.Type.KeyPress or event.modifiers():
            return False
        key = event.key()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            from PySide6.QtWidgets import QAbstractButton

            if isinstance(obj, QAbstractButton):
                obj.click()
                return True
            return False  # a row body (e.g. a Radio station) plays on Enter
        arrows = (Qt.Key.Key_Left, Qt.Key.Key_Right, Qt.Key.Key_Up, Qt.Key.Key_Down)
        if key not in arrows:
            return False
        rows = self._get_rows()
        row, targets, col = self._locate(obj)
        if row is None or row not in rows:
            return False
        if key in (Qt.Key.Key_Left, Qt.Key.Key_Right):
            j = col + (1 if key == Qt.Key.Key_Right else -1)
            if 0 <= j < len(targets):
                targets[j].setFocus(Qt.FocusReason.TabFocusReason)
            return True
        ri = rows.index(row)
        nj = ri + (1 if key == Qt.Key.Key_Down else -1)
        if 0 <= nj < len(rows):
            ntargets = self._get_targets(rows[nj])
            if ntargets:
                ntargets[min(col, len(ntargets) - 1)].setFocus(
                    Qt.FocusReason.TabFocusReason
                )
        return True

    def _maybe_clear(self, row) -> None:
        from PySide6.QtWidgets import QApplication

        try:
            targets = self._get_targets(row)
        except RuntimeError:
            return  # row was deleted
        if QApplication.focusWidget() not in targets:
            self._set_active(row, False)


def install_row_grid_nav(get_rows, get_targets):
    """Keyboard nav for a vertical list of QFrame rows (Left/Right within a
    row, Up/Down between rows + per-row focus highlight). ``get_rows()``
    returns the ordered visible rows; ``get_targets(row)`` returns that row's
    focus targets left-to-right. Returns the nav object — keep a reference
    (e.g. ``self._row_nav = ...``) and call ``.wire(row)`` for each row as
    it's built."""
    return _RowGridNav(get_rows, get_targets)


def paint_kb_row_ring(widget) -> None:
    """Draw the keyboard-focus accent ring over ``widget`` — call from a
    row's paintEvent when its ``_kb_active`` flag is set. Reads ACCENT fresh
    for live-accent."""
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen

    from jellytoast.ui_helpers import ACCENT

    p = QPainter(widget)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    ring = QColor(ACCENT)
    ring.setAlpha(235)
    pen = QPen(ring)
    pen.setWidth(2)
    p.setPen(pen)
    p.setBrush(Qt.BrushStyle.NoBrush)
    inset = widget.rect().adjusted(1, 1, -2, -2)
    path = QPainterPath()
    path.addRoundedRect(QRectF(inset), rad(9), rad(9))
    p.drawPath(path)
    p.end()


def install_arrow_nav(buttons):
    """Wire Left/Right focus traversal across a horizontal ``buttons`` row.

    Each button is made keyboard-focusable (``TabFocus`` — reachable by Tab
    / the section walker / arrow keys, but a mouse click does NOT focus it,
    so the focus ring stays a keyboard affordance). Returns the filter; the
    caller must keep a reference to it (e.g. ``self._nav = install_arrow_nav(...)``)
    or it'll be garbage-collected and the traversal goes dead."""
    nav = _ArrowNav(buttons)
    for b in buttons:
        b.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        b.installEventFilter(nav)
    return nav
