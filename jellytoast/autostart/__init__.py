"""Launch-on-login control. Public API is platform-agnostic; the actual
implementation lives in a per-OS backend module.

Public API:
    is_supported() -> bool   # backend can fulfil enable/disable
    is_enabled() -> bool     # currently set to launch on login
    enable() -> bool         # turn on; True iff the change took effect
    disable() -> bool        # turn off; True iff a previous entry was removed

Linux: the XDG Background portal (RequestBackground with autostart=true —
works inside a flatpak sandbox, needs no filesystem permission), falling
back to writing/reading ~/.config/autostart/jellytoast.desktop when no
portal is reachable (see jellytoast/autostart/_portal.py).
Windows: a value under the per-user Run registry key — or, when running as
a packaged MSIX app (Run keys are ignored there), the manifest startupTask
(see jellytoast/autostart/_msix.py).
macOS: a per-user LaunchAgent .plist in ~/Library/LaunchAgents/ with
RunAtLoad=true (see jellytoast/autostart/_macos.py).
"""

from __future__ import annotations

from jellytoast.platform_compat import (
    IS_LINUX,
    IS_MACOS,
    IS_WINDOWS,
    is_msix_packaged,
)

if IS_LINUX:
    from jellytoast.autostart import _linux as _backend
elif IS_WINDOWS and is_msix_packaged():
    # Packaged: Run-key autostart is ignored; drive the manifest startupTask.
    from jellytoast.autostart import _msix as _backend
elif IS_WINDOWS:
    from jellytoast.autostart import _windows as _backend
elif IS_MACOS:
    from jellytoast.autostart import _macos as _backend
else:
    from jellytoast.autostart import _unsupported as _backend


def is_supported() -> bool:
    return _backend.is_supported()


def is_enabled() -> bool:
    return _backend.is_enabled()


def enable() -> bool:
    return _backend.enable()


def disable() -> bool:
    return _backend.disable()
