"""Make frosted-glass blur work on KDE under a pip/pipx-installed PySide6.

A `pipx install` (or any pip wheel install) bundles its own copy of Qt
inside the PySide6 package. That bundled Qt's plugin search path does NOT
include the system KDE Qt plugins at ``/usr/lib/qt6/plugins/kf6/...``, so
``libKF6WindowSystem`` can't load its platform backend
(``kf.windowsystem: Could not find any platform plugin``),
``KWindowEffects::isEffectAvailable(BlurBehind)`` returns False, and
``jellytoast.blur`` reports ``UNSUPPORTED`` — the frosted themes then fall back
to the near-opaque body even though KWin blur is actually available. (That
fallback is the #46 safety net doing its job; the root cause is the install
method, not the blur code.)

The fix: when we detect that exact situation, append the system Qt6 plugin
directory to ``QT_PLUGIN_PATH`` before Qt loads. Appending (not prepending)
keeps the bundled plugins at higher priority — only the *missing* KDE plugins
(``kf6/kwindowsystem``) get filled in from the system dir. Qt's own plugin
version check safely refuses a system plugin built against a newer Qt minor,
so a wide ABI skew degrades back to the near-opaque fallback rather than
crashing.

A distro/system PySide6 (or a ``venv --system-site-packages`` over the
packaged ``pyside6``) already integrates with the system plugins, so this is
a no-op there. It's also a no-op off KDE, inside a Flatpak (the KDE runtime
ships the plugins), and when ``JT_NO_QT_PLUGIN_FIX=1`` is set.

``heal_qt_plugin_path()`` MUST run before PySide6 is imported / QApplication
is constructed — Qt reads ``QT_PLUGIN_PATH`` when it builds its library paths.
It is called from the env-bootstrap block at the top of ``jellytoast/app.py``,
alongside the ``LC_NUMERIC`` / ``PULSE_PROP_*`` setup.
"""

from __future__ import annotations

import os
import sys

# System Qt6 plugin directories across the distros we care about. Arch puts
# them under /usr/lib/qt6/plugins; some 64-bit distros use lib64; Debian/
# Ubuntu use the multiarch triplet path. First existing match wins.
_CANDIDATE_PLUGIN_DIRS = (
    "/usr/lib/qt6/plugins",
    "/usr/lib64/qt6/plugins",
    "/usr/lib/x86_64-linux-gnu/qt6/plugins",
)

# A PySide6 living under one of these prefixes is a distro/system install (or
# a --system-site-packages venv that resolves to it) and already sees the
# system Qt plugins — nothing to heal.
_SYSTEM_PREFIXES = ("/usr/lib", "/usr/lib64", "/usr/local/lib")


def _choose_plugin_dir(
    *,
    platform: str,
    desktop: str,
    flatpak: bool,
    disabled: bool,
    pyside_dir: str,
    existing_path: str,
    isdir,
) -> str | None:
    """Pure decision: the system Qt6 plugin dir to append to QT_PLUGIN_PATH,
    or None to leave the environment untouched.

    ``isdir`` is injected (defaults to ``os.path.isdir`` in the caller) so the
    full gate matrix is unit-testable without a real filesystem or a real
    PySide6 install.
    """
    if platform != "linux":
        return None
    if disabled:
        return None
    if flatpak:
        return None
    if "KDE" not in desktop.upper():
        return None
    if not pyside_dir:
        return None
    # Already a system/distro PySide6 → it sees the system plugins already.
    if pyside_dir.startswith(_SYSTEM_PREFIXES):
        return None
    present = [p for p in existing_path.split(os.pathsep) if p]
    for plug_dir in _CANDIDATE_PLUGIN_DIRS:
        if not isdir(os.path.join(plug_dir, "kf6", "kwindowsystem")):
            continue
        if plug_dir in present:
            return None  # user / earlier call already added it
        return plug_dir
    return None


def _pyside_location() -> str:
    """Filesystem dir of the installed PySide6 package WITHOUT importing it
    (importing would start Qt before we've had a chance to set the plugin
    path). Empty string if it can't be located."""
    import importlib.util

    try:
        spec = importlib.util.find_spec("PySide6")
    except Exception:
        return ""
    if spec is None or not spec.submodule_search_locations:
        return ""
    return spec.submodule_search_locations[0]


def heal_qt_plugin_path() -> str | None:
    """Append the system Qt6 plugin dir to ``QT_PLUGIN_PATH`` when running a
    bundled PySide6 on KDE so compositor blur works. Mutates ``os.environ``
    in place and returns the directory added, or None if no change was made.

    Must be called before importing PySide6 / building QApplication.
    """
    chosen = _choose_plugin_dir(
        platform=sys.platform,
        desktop=os.environ.get("XDG_CURRENT_DESKTOP", ""),
        flatpak=bool(os.environ.get("FLATPAK_ID")),
        disabled=os.environ.get("JT_NO_QT_PLUGIN_FIX") == "1",
        pyside_dir=_pyside_location(),
        existing_path=os.environ.get("QT_PLUGIN_PATH", ""),
        isdir=os.path.isdir,
    )
    if chosen:
        present = [p for p in os.environ.get("QT_PLUGIN_PATH", "").split(os.pathsep) if p]
        os.environ["QT_PLUGIN_PATH"] = os.pathsep.join([*present, chosen])
    return chosen
