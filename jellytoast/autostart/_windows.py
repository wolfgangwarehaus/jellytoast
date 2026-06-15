"""Windows launch-on-login backend. Manages a value under the per-user
Run key (``HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\Run``) —
no elevation needed, honored by Explorer at login, and surfaced to the
user in Task Manager's Startup tab and Settings → Apps → Startup.

The command targets the gui-script launcher exe when one exists (pip /
pipx install) so login launches without a console window. A source
checkout falls back to ``pythonw.exe -m jellytoast`` (then plain
``python.exe`` if pythonw is missing).
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

try:
    import winreg
except ImportError:  # pragma: no cover — non-Windows; tests patch this attr
    winreg = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_VALUE_NAME = "jellytoast"


def _launch_command() -> str:
    """The command line to register: the launcher exe if we have one,
    else a ``-m jellytoast`` invocation, preferring pythonw so a login
    launch doesn't flash a console."""
    from jellytoast.windows_shortcut import _launcher_exe

    exe = _launcher_exe()
    if exe is not None:
        return f'"{exe}"'
    interp = Path(sys.executable or "python")
    pythonw = interp.with_name("pythonw.exe")
    if pythonw.is_file():
        interp = pythonw
    return f'"{interp}" -m jellytoast'


def is_supported() -> bool:
    return winreg is not None


def is_enabled() -> bool:
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_READ
        ) as key:
            value, _kind = winreg.QueryValueEx(key, _VALUE_NAME)
        return bool(value)
    except FileNotFoundError:
        return False
    except Exception as e:
        logger.debug("autostart is_enabled read failed: %s", e)
        return False


def enable() -> bool:
    if winreg is None:
        return False
    try:
        with winreg.CreateKeyEx(
            winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.SetValueEx(key, _VALUE_NAME, 0, winreg.REG_SZ, _launch_command())
        return True
    except Exception as e:
        logger.debug("autostart enable failed: %s", e)
        return False


def disable() -> bool:
    """Remove the Run-key value. Returns True if a value was removed,
    False if there was nothing to do (or removal failed)."""
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER, _RUN_KEY, 0, winreg.KEY_SET_VALUE
        ) as key:
            winreg.DeleteValue(key, _VALUE_NAME)
        return True
    except FileNotFoundError:
        return False
    except Exception as e:
        logger.debug("autostart disable failed: %s", e)
        return False
