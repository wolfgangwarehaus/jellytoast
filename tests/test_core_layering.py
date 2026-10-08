"""The headless core must stay UI-free.

Server clients (providers / Jellyfin API), scrobbling and the smart-playlist
engine load only QtCore (+ QtNetwork) today — no QtWidgets / QtGui, no view
modules. Keeping it that way is the cheap groundwork for any future UI on
another platform (a touch / QML front end, Android, a native port reusing
the logic as its reference), so pin it: a stray `from jellytoast.ui_helpers
import …` in a provider would quietly couple the core to the widget UI.

Runs in a fresh interpreter so other tests' imports can't mask a leak.
"""

from __future__ import annotations

import json
import subprocess
import sys

CORE_MODULES = (
    "jellytoast.providers",
    "jellytoast.providers.jellyfin",
    "jellytoast.providers.subsonic",
    "jellytoast.providers.smart_rule_eval",
    "jellytoast.providers.smart_rule_schema",
    "jellytoast.jellyfin_api",
    "jellytoast.scrobble",
)

FORBIDDEN_QT = ("PySide6.QtWidgets", "PySide6.QtGui", "PySide6.QtQuick", "PySide6.QtQml")

# jellytoast modules the core may pull in (state + plumbing, not UI).
ALLOWED_JT_PREFIXES = (
    "jellytoast.providers",
    "jellytoast.scrobble",
    "jellytoast.jellyfin_api",
    "jellytoast.settings",
    "jellytoast.settings_migration",
    "jellytoast.credentials",
    "jellytoast.platform_compat",
    "jellytoast.player_state",
    "jellytoast.offline",
    "jellytoast.async_io",
    "jellytoast.i18n",
    "jellytoast._channel",
    "jellytoast.version",
    "jellytoast.sort_utils",
)


def _loaded_after_importing_core():
    code = (
        "import json, sys\n"
        + "".join(f"import {m}\n" for m in CORE_MODULES)
        + "print(json.dumps(sorted(sys.modules)))\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    ).stdout
    return json.loads(out.strip().splitlines()[-1])


def test_core_loads_no_gui_toolkit_and_no_ui_modules():
    loaded = _loaded_after_importing_core()
    gui = [m for m in loaded if m.startswith(FORBIDDEN_QT)]
    assert not gui, f"core pulled in GUI Qt modules: {gui}"
    ui = [
        m
        for m in loaded
        if m.startswith("jellytoast.")
        and m != "jellytoast"
        and not m.startswith(ALLOWED_JT_PREFIXES)
    ]
    assert not ui, f"core pulled in non-core jellytoast modules: {ui}"
