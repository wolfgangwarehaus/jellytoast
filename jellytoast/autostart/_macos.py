"""macOS launch-on-login backend — a per-user LaunchAgent.

launchd reads ~/Library/LaunchAgents/<label>.plist at session login; the
``RunAtLoad`` key starts the program once when the user logs in. This is the
standard, sandbox-free mechanism for a GUI app to launch on login on macOS
(the App-Store-only ``SMAppService`` API is not needed for a Developer-ID app).

Strategy mirrors the Linux XDG backend:
- Enable:  write the .plist into ~/Library/LaunchAgents/.
- Disable: delete the .plist.
- is_enabled: the file exists.

ProgramArguments point at the frozen .app's executable when bundled
(``sys.frozen``), otherwise at ``<python> -m jellytoast`` so a source / pip
checkout launches itself the same way it was started.

Pure stdlib (plistlib + pathlib) — import-safe on every platform, so the
plist-generation logic is unit-testable off a Mac.
"""

from __future__ import annotations

import plistlib
import sys
from pathlib import Path

# Reverse-DNS label, matching the app-id used by the packaging metainfo /
# bundle identifier (io.github.wolfgangwarehaus.jellytoast).
_LABEL = "io.github.wolfgangwarehaus.jellytoast"
_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
_PLIST = _AGENTS_DIR / f"{_LABEL}.plist"


def is_supported() -> bool:
    return True


def is_enabled() -> bool:
    return _PLIST.exists()


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


def enable() -> bool:
    """Write the LaunchAgent. Returns True on success, False on filesystem
    errors (e.g. read-only home)."""
    try:
        _AGENTS_DIR.mkdir(parents=True, exist_ok=True)
        _PLIST.write_bytes(_plist_bytes())
        return True
    except Exception:
        return False


def disable() -> bool:
    """Remove the LaunchAgent. Returns True if a file was removed, False if
    there was nothing to do (or removal failed)."""
    if not _PLIST.exists():
        return False
    try:
        _PLIST.unlink()
        return True
    except Exception:
        return False
