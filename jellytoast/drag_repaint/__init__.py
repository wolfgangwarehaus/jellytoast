"""Drag-repaint fix — kills the stale-blur "line artifact" that KWin
leaves behind a translucent window dragged on the NVIDIA EGL path.

The artifact (KWin bug 455526 / 457727) lives in KWin's optimized
`paintSimpleScreen` partial-damage render path. A *transformed* window
is instead rendered through `paintGenericScreen` — a full-frame repaint
— which never exercises the buggy path. jellytoast ships a tiny KWin
scripted effect that, while one of its own windows is being dragged,
holds it under an imperceptible in-progress transform (and force-blurs
it so the blur survives the transform). Visually inert; it only flips
KWin onto the clean render path for the duration of the drag.

Public API:
    is_supported() -> bool   # backend can install the effect
    install() -> bool        # idempotent install + enable + load
    uninstall() -> bool      # idempotent unload + disable + remove
    sync() -> bool           # install (or, under JT_NO_DRAG_REPAINT,
                             #   uninstall) — the one call boot makes
    diagnose() -> dict       # backend-specific debug snapshot

KDE Wayland: the `_kwin` backend copies the bundled effect into the
user's KWin effects directory and asks KWin to load it. Anywhere else
(X11, Windows, macOS, non-KDE Wayland): the unsupported backend no-ops
— the artifact is a KWin/NVIDIA-specific bug and the fix is too.

`JT_NO_DRAG_REPAINT=1` forces `sync()` to remove the effect instead of
installing it — a support escape hatch, not a user-facing setting.
"""

from __future__ import annotations

import os

from jellytoast.platform_compat import is_kde_wayland

# KDE Wayland is the only environment with the bug (and the only one
# with the KWin scripting hooks to fix it). Pick the backend at import
# time; the gate is stable for the life of the process.
if is_kde_wayland():
    from jellytoast.drag_repaint import _kwin as _backend
else:
    from jellytoast.drag_repaint import _unsupported as _backend


def is_supported() -> bool:
    return _backend.is_supported()


def install() -> bool:
    return _backend.install()


def uninstall() -> bool:
    return _backend.uninstall()


def sync() -> bool:
    """Reconcile the effect with what's wanted: install it, unless
    ``JT_NO_DRAG_REPAINT=1`` is set, in which case remove it. Call once
    at startup. A no-op off KDE Wayland."""
    if os.environ.get("JT_NO_DRAG_REPAINT") == "1":
        return uninstall()
    return install()


def diagnose() -> dict:
    return _backend.diagnose()
