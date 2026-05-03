"""
XDG autostart helper. Manages ~/.config/autostart/jellytoast.desktop —
the standard cross-DE mechanism for "launch on login" on Linux. KDE,
GNOME, XFCE, Cinnamon, MATE, and LXQt all read this directory.

Strategy:
- Enable: copy ~/.local/share/applications/jellytoast.desktop into the
  autostart dir if it exists; otherwise synthesize a minimal entry
  pointing at the current interpreter and script path.
- Disable: delete the autostart file.
- is_enabled: file exists and isn't marked Hidden=true (some DEs flip
  this flag instead of removing the file).

We never touch the source desktop file in ~/.local/share/applications.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path


_AUTOSTART_DIR = Path.home() / ".config" / "autostart"
_AUTOSTART_FILE = _AUTOSTART_DIR / "jellytoast.desktop"
_SOURCE_DESKTOP = Path.home() / ".local" / "share" / "applications" / "jellytoast.desktop"


def is_enabled() -> bool:
    if not _AUTOSTART_FILE.exists():
        return False
    try:
        for line in _AUTOSTART_FILE.read_text().splitlines():
            line = line.strip()
            # Either Hidden=true or X-GNOME-Autostart-enabled=false
            # disables the entry without removing the file.
            if line.lower() == "hidden=true":
                return False
            if line.lower() == "x-gnome-autostart-enabled=false":
                return False
    except Exception:
        return False
    return True


def enable() -> bool:
    """Drop a .desktop entry into ~/.config/autostart. Returns True on
    success, False on filesystem errors (e.g. read-only home)."""
    try:
        _AUTOSTART_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        return False

    if _SOURCE_DESKTOP.exists():
        try:
            content = _SOURCE_DESKTOP.read_text()
        except Exception:
            content = _synth_desktop_entry()
        else:
            content = _strip_hidden_flags(content)
    else:
        content = _synth_desktop_entry()

    try:
        _AUTOSTART_FILE.write_text(content)
        # 0644 — autostart files don't need to be executable.
        _AUTOSTART_FILE.chmod(0o644)
        return True
    except Exception:
        return False


def disable() -> bool:
    """Remove the autostart entry. Returns True if a file was removed,
    False if there was nothing to do (or removal failed)."""
    if not _AUTOSTART_FILE.exists():
        return False
    try:
        _AUTOSTART_FILE.unlink()
        return True
    except Exception:
        return False


# ── internals ─────────────────────────────────────────────────────────

def _strip_hidden_flags(content: str) -> str:
    """Remove Hidden / X-GNOME-Autostart-enabled disable flags so a
    previously-disabled entry becomes active when re-enabled."""
    out_lines = []
    for line in content.splitlines():
        s = line.strip().lower()
        if s.startswith("hidden=") or s.startswith("x-gnome-autostart-enabled="):
            continue
        out_lines.append(line)
    return "\n".join(out_lines) + "\n"


def _synth_desktop_entry() -> str:
    """Fallback: build a minimal entry from the current interpreter and
    the absolute path of the running jellytoast.py. Used when the
    canonical entry under ~/.local/share/applications is missing."""
    script_path = Path(__file__).resolve().parent.parent / "jellytoast.py"
    interpreter = sys.executable or "python3"
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=JellyToast\n"
        "Comment=Audio-first Jellyfin desktop client\n"
        f"Exec={interpreter} \"{script_path}\"\n"
        f"Path={script_path.parent}\n"
        "Icon=jellytoast\n"
        "Terminal=false\n"
        "Categories=AudioVideo;Audio;Player;\n"
        "StartupNotify=true\n"
        "X-GNOME-Autostart-enabled=true\n"
    )
