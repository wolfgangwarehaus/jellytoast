"""Linux launch-at-login. Two routes, portal first:

1. **XDG Background portal** (``org.freedesktop.portal.Background``) — the
   sanctioned route, and the only one that works in a sandbox: it needs no
   filesystem permission, so the Flathub build can honour the toggle at all
   (Flathub's linter hard-rejects ``--filesystem=~/.config/autostart:create``,
   which used to leave launch-at-login a silent no-op there). See
   ``_portal.py``. The user can DENY it — we report that honestly instead of
   sneaking a .desktop file in behind their back.
2. **~/.config/autostart/jellytoast.desktop** — the classic cross-DE
   mechanism (KDE, GNOME, XFCE, Cinnamon, MATE, LXQt all read this dir),
   used when no portal is reachable, or when the portal call errors out.

Desktop-file strategy:
- Enable: copy ~/.local/share/applications/jellytoast.desktop into the
  autostart dir if it exists; otherwise synthesize a minimal entry
  pointing at the current interpreter and script path.
- Disable: delete the autostart file.
- is_enabled: file exists and isn't marked Hidden=true (some DEs flip
  this flag instead of removing the file).

We never touch the source desktop file in ~/.local/share/applications.
"""

from __future__ import annotations

import sys
from pathlib import Path

from jellytoast.autostart import _portal

_AUTOSTART_DIR = Path.home() / ".config" / "autostart"
_AUTOSTART_FILE = _AUTOSTART_DIR / "jellytoast.desktop"
_SOURCE_DESKTOP = Path.home() / ".local" / "share" / "applications" / "jellytoast.desktop"


def is_supported() -> bool:
    return True


def is_enabled() -> bool:
    """Portal-granted state wins when the portal path was the one used
    (there's no autostart entry on our side of the sandbox to look at);
    otherwise the desktop-file check, exactly as before."""
    if _portal.autostart_granted():
        return True
    return _desktop_file_enabled()


def _desktop_file_enabled() -> bool:
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
    """Turn login-launch on. Tries the Background portal first, falls back to
    the .desktop file. Returns True iff launch-at-login is now on; never
    raises (the settings checkbox depends on that contract)."""
    if _portal.is_available():
        granted = _portal.request_autostart(True)
        if granted is True:
            _portal.mark_granted()
            return True
        if granted is False:
            # The user (or the portal) said no. Honour it — do NOT write a
            # .desktop file behind their back.
            _portal.clear_granted()
            return False
        # granted is None → portal unreachable/broken; fall through.
    return _enable_desktop_file()


def disable() -> bool:
    """Turn login-launch off. Returns True if something was actually
    removed, False if there was nothing to do (or removal failed)."""
    removed = False
    if _portal.autostart_granted():
        result = _portal.request_autostart(False)
        # None = the portal went away; the grant record is unverifiable, so
        # drop it rather than pin the checkbox on forever.
        if result is not False:
            _portal.clear_granted()
            removed = True
    if _disable_desktop_file():
        removed = True
    return removed


def _enable_desktop_file() -> bool:
    """Drop a .desktop entry into ~/.config/autostart. Returns True on
    success, False on filesystem errors (e.g. read-only home, or a sandbox
    with no autostart-dir grant)."""
    import os

    try:
        _AUTOSTART_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        return False

    # Inside a flatpak, ALWAYS synthesize: ~/.config/autostart is executed
    # by the HOST session, and both the copied desktop entry and the old
    # synth pointed at sandbox-internal paths (/app/…, the sandbox python)
    # that don't exist on the host — so start-at-login silently launched
    # nothing on every login (0.2.0 Steam Deck QA finding #3). The synth's
    # flatpak branch writes the host-runnable `flatpak run <app-id>` form.
    if os.environ.get("FLATPAK_ID"):
        content = _synth_desktop_entry()
    elif _SOURCE_DESKTOP.exists():
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


def _disable_desktop_file() -> bool:
    """Remove the autostart entry. Returns True if a file was removed,
    False if there was nothing to do (or removal failed)."""
    if not _AUTOSTART_FILE.exists():
        return False
    try:
        _AUTOSTART_FILE.unlink()
        return True
    except Exception:
        return False


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
    """Build a minimal autostart entry. Flatpak: the host-runnable
    ``flatpak run <app-id>`` form (no Path= — /app/… is unresolvable on the
    host that executes autostart). Otherwise: the current interpreter and
    the installed jellytoast package (`python -m jellytoast`)."""
    import os

    flatpak_id = os.environ.get("FLATPAK_ID")
    if flatpak_id:
        exec_line = f"Exec=flatpak run {flatpak_id}\n"
        path_line = ""
    else:
        # parent.parent = the jellytoast package dir; its parent is whatever
        # holds the package (repo root or site-packages) — Path= there so a
        # repo checkout launches from the repo, same as before the rename.
        pkg_dir = Path(__file__).resolve().parent.parent
        interpreter = sys.executable or "python3"
        exec_line = f"Exec={interpreter} -m jellytoast\n"
        path_line = f"Path={pkg_dir.parent}\n"
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=jellytoast\n"
        "Comment=Audio-first native music client for Jellyfin and Subsonic\n"
        f"{exec_line}"
        f"{path_line}"
        "Icon=jellytoast\n"
        "Terminal=false\n"
        "Categories=AudioVideo;Audio;Player;\n"
        "StartupNotify=true\n"
        "X-GNOME-Autostart-enabled=true\n"
    )
