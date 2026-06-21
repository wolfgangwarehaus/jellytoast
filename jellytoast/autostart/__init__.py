"""Launch-on-login control. Public API is platform-agnostic; the actual
implementation lives in a per-OS backend module.

Public API:
    is_supported() -> bool   # backend can fulfil enable/disable
    is_enabled() -> bool     # currently set to launch on login
    enable() -> bool         # turn on; True iff the change took effect
    disable() -> bool        # turn off; True iff a previous entry was removed

Linux: writes/reads ~/.config/autostart/jellytoast.desktop (XDG).
Windows: a value under the per-user Run registry key — or, when running as
a packaged MSIX app (Run keys are ignored there), the manifest startupTask
(see jellytoast/autostart/_msix.py).
macOS: not yet implemented — the unsupported backend returns False from
every call so call sites can no-op cleanly.
"""

from __future__ import annotations

from jellytoast.platform_compat import (
    IS_LINUX,
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
