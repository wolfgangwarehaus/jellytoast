"""KWin scripted-effect backend for the drag-repaint fix.

Installs jellytoast's bundled `jellytoast_dragrepaint` scripted effect
into the user's KWin effects directory, flips its `kwinrc` enable key,
and asks KWin to (re)load it — all idempotent, all best-effort.

The effect itself is `effect/jellytoast_dragrepaint/` (a `metadata.json`
+ `contents/code/main.js` pair), shipped as package data. `install()`
copies it over any existing copy, so a jellytoast update refreshes the
JS; `_reload_effect` then unload-then-loads so KWin re-reads it.

Why copy-and-load rather than just write a `kwinrc` key: a freshly
enabled effect isn't picked up until KWin is told to load it, and we
want the fix live on first launch without a compositor restart.

Everything degrades to a silent no-op: missing tools, an unwritable
data dir, a KWin that rejects the effect — none of it should ever take
down the app. The artifact is a cosmetic bug; the fix is best-effort.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

# Effect id — must match KPlugin.Id in the bundled metadata.json and is
# also the kwinrc [Plugins] key stem (`<id>Enabled`).
_EFFECT_ID = "jellytoast_dragrepaint"

# Bundled source: modules/drag_repaint/effect/jellytoast_dragrepaint/.
_SOURCE_DIR = Path(__file__).resolve().parent / "effect" / _EFFECT_ID


def is_supported() -> bool:
    """KDE Wayland with `kwriteconfig` + `qdbus` on PATH, and the
    bundled effect actually present in the package. A False here is
    what `__init__`'s callers read to skip the work entirely."""
    return bool(_kwriteconfig_bin() and _qdbus_bin() and _SOURCE_DIR.is_dir())


def install() -> bool:
    """Idempotently install, enable, and load the effect. Copies the
    bundled effect over any existing copy, sets the `kwinrc` key, and
    asks KWin to reload. Returns True on success, False if unsupported
    or the copy failed."""
    if not is_supported():
        return False
    dest = _dest_dir()
    try:
        if dest.exists():
            shutil.rmtree(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(_SOURCE_DIR, dest)
    except OSError:
        return False
    _set_enabled(True)
    _reload_effect()
    return True


def uninstall() -> bool:
    """Idempotently unload, disable, and delete the effect. Returns
    True if the environment is supported (i.e. the work was attempted),
    False otherwise."""
    if not is_supported():
        return False
    _effects_call("unloadEffect", _EFFECT_ID)
    _set_enabled(False)
    try:
        shutil.rmtree(_dest_dir())
    except OSError:
        pass
    return True


def diagnose() -> dict:
    """Runtime paths + tool resolution — call from a debug hook and log
    the dict if the effect isn't taking."""
    return {
        "backend": "kwin",
        "is_supported": is_supported(),
        "effect_id": _EFFECT_ID,
        "source_dir": str(_SOURCE_DIR),
        "source_present": _SOURCE_DIR.is_dir(),
        "dest_dir": str(_dest_dir()),
        "kwriteconfig": _kwriteconfig_bin(),
        "qdbus": _qdbus_bin(),
    }


# ── internals ─────────────────────────────────────────────────────────


def _data_home() -> Path:
    """XDG_DATA_HOME, or its ~/.local/share default."""
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    return Path(xdg) if xdg else Path.home() / ".local" / "share"


def _dest_dir() -> Path:
    return _data_home() / "kwin" / "effects" / _EFFECT_ID


def _kwriteconfig_bin() -> str | None:
    for cand in ("kwriteconfig6", "kwriteconfig5"):
        path = shutil.which(cand)
        if path:
            return path
    return None


def _qdbus_bin() -> str | None:
    for cand in ("qdbus6", "qdbus-qt6", "qdbus"):
        path = shutil.which(cand)
        if path:
            return path
    return None


def _set_enabled(on: bool) -> None:
    """Write the `kwinrc` [Plugins] enable key for the effect."""
    bin_ = _kwriteconfig_bin()
    if not bin_:
        return
    try:
        subprocess.run(
            [
                bin_, "--file", "kwinrc", "--group", "Plugins",
                "--key", f"{_EFFECT_ID}Enabled", "true" if on else "false",
            ],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass


def _effects_call(method: str, *args: str) -> None:
    """Invoke a method on KWin's `/Effects` D-Bus object (loadEffect /
    unloadEffect)."""
    bin_ = _qdbus_bin()
    if not bin_:
        return
    try:
        subprocess.run(
            [bin_, "org.kde.KWin", "/Effects", method, *args],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except Exception:
        pass


def _reload_effect() -> None:
    """Unload-then-load so a refreshed main.js is re-read by KWin."""
    _effects_call("unloadEffect", _EFFECT_ID)
    _effects_call("loadEffect", _EFFECT_ID)
