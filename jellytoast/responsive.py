"""Window width classes — the one place phone vs. desktop layout is decided.

Views follow the main window's *width*, never "am I on a phone": Plasma
Mobile's docked mode turns forced-maximize off mid-session, phones rotate,
and a desktop user can drag the window narrow — all should get the layout
that fits. Platform *behaviour* (no tray, background-on-close) is a separate
question, answered by ``platform_compat.is_mobile_shell()``.

The main window calls :func:`update` from its ``resizeEvent``; a class change
is broadcast as ``PlayerBus.width_class_changed(str)``. Views read
:func:`current` when they build and connect to the signal for live changes.
"""

from __future__ import annotations

COMPACT = "compact"
REGULAR = "regular"

# Below this the side-by-side layouts (Now Playing's cover|queue split, the
# Settings sidebar) stop fitting; phones (360–600 logical) and narrow
# windows land here.
COMPACT_BELOW = 720

_current = REGULAR


def width_class(width: int) -> str:
    return COMPACT if width < COMPACT_BELOW else REGULAR


def current() -> str:
    return _current


def is_compact() -> bool:
    return _current == COMPACT


def update(width: int) -> bool:
    """Record the main window's width; True (and a bus broadcast) when the
    class changed."""
    global _current
    new = width_class(width)
    if new == _current:
        return False
    _current = new
    from jellytoast.player_state import PlayerBus

    PlayerBus.get().width_class_changed.emit(new)
    return True
