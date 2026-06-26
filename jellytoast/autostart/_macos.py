"""macOS launch-on-login backend — two mechanisms, picked by build variant.

- **Mac App Store (sandboxed)**: an ``SMAppService`` login item (macOS 13+). A
  sandboxed app CANNOT write ``~/Library/LaunchAgents``, so the LaunchAgent path
  below is illegal there — register the main app as a login item through the
  ServiceManagement framework instead.
- **Developer-ID .dmg / source / pip**: a per-user LaunchAgent in
  ``~/Library/LaunchAgents``. launchd reads it at session login and ``RunAtLoad``
  starts the app once. The standard sandbox-free mechanism for a GUI app.

The public API (``is_supported`` / ``is_enabled`` / ``enable`` / ``disable``)
branches on :func:`jellytoast.platform_compat.is_macos_sandboxed`. The
LaunchAgent plist generation stays pure stdlib (plistlib + pathlib), so it's
import-safe + unit-testable off a Mac; the SMAppService calls are runtime-only
(pyobjc, macOS 13+, the bundled .app) and never run on the test machine because
``is_macos_sandboxed()`` is False there.
"""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path

from jellytoast.platform_compat import is_macos_sandboxed

# Reverse-DNS label, matching the app-id used by the packaging metainfo /
# bundle identifier (io.github.wolfgangwarehaus.jellytoast).
_LABEL = "io.github.wolfgangwarehaus.jellytoast"
_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
_PLIST = _AGENTS_DIR / f"{_LABEL}.plist"


# --- Mac App Store: SMAppService login item (sandbox-legal, macOS 13+) -------

def _sm_service():
    """The SMAppService that represents THIS app as a login item."""
    from ServiceManagement import SMAppService

    return SMAppService.mainAppService()


def _sm_supported() -> bool:
    try:
        from ServiceManagement import SMAppService  # noqa: F401

        return True
    except Exception:
        return False


def _sm_is_enabled() -> bool:
    try:
        try:
            from ServiceManagement import SMAppServiceStatusEnabled as _EN

            enabled = int(_EN)
        except Exception:
            enabled = 1  # SMAppServiceStatus.enabled
        return int(_sm_service().status()) == enabled
    except Exception:
        return False


def _sm_enable() -> bool:
    try:
        ok, _err = _sm_service().registerAndReturnError_(None)
        return bool(ok)
    except Exception:
        return False


def _sm_disable() -> bool:
    try:
        ok, _err = _sm_service().unregisterAndReturnError_(None)
        return bool(ok)
    except Exception:
        return False


# --- Developer-ID / source: per-user LaunchAgent -----------------------------

def _program_arguments() -> list[str]:
    """The launchd ProgramArguments for however this process was started."""
    if getattr(sys, "frozen", False):
        # Inside jellytoast.app/Contents/MacOS/jellytoast — launch it directly.
        return [sys.executable]
    interpreter = sys.executable or "python3"
    return [interpreter, "-m", "jellytoast"]


def _plist_bytes() -> bytes:
    """Serialize the LaunchAgent plist. Pure — unit-tested off a Mac."""
    return plistlib.dumps(
        {
            "Label": _LABEL,
            "ProgramArguments": _program_arguments(),
            "RunAtLoad": True,
            # Foreground app, not a daemon — gives it normal UI scheduling.
            "ProcessType": "Interactive",
        }
    )


def _la_is_enabled() -> bool:
    return _PLIST.exists()


def _la_enable() -> bool:
    """Write the LaunchAgent. Returns True on success, False on filesystem
    errors (e.g. read-only home)."""
    try:
        _AGENTS_DIR.mkdir(parents=True, exist_ok=True)
        _PLIST.write_bytes(_plist_bytes())
        return True
    except Exception:
        return False


def _la_disable() -> bool:
    """Remove the LaunchAgent. Returns True if a file was removed, False if
    there was nothing to do (or removal failed)."""
    if not _PLIST.exists():
        return False
    try:
        _PLIST.unlink()
        return True
    except Exception:
        return False


# --- public API: pick the mechanism by build variant -------------------------

def is_supported() -> bool:
    if is_macos_sandboxed():
        return _sm_supported()
    return True


def is_enabled() -> bool:
    if is_macos_sandboxed():
        return _sm_is_enabled()
    return _la_is_enabled()


def enable() -> bool:
    if is_macos_sandboxed():
        return _sm_enable()
    return _la_enable()


def disable() -> bool:
    if is_macos_sandboxed():
        return _sm_disable()
    return _la_disable()
