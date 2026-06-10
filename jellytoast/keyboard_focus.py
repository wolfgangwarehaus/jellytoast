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
