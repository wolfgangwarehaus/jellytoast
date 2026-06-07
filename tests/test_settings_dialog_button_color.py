"""Settings dialog plain-button text stays theme-aware (finding, 2026-06-07).

The dialog sets its own stylesheet, and Qt does not reliably merge the
app-global ``QPushButton {{ color: TEXT }}`` into a widget that already has
its own — so the dialog's plain QPushButtons (e.g. "Customize" and the
PipeWire conf installer, whose live label is "Remove PipeWire bit-perfect
config") kept stale near-white text after switching to a light theme.
``_build_and_apply_dialog_stylesheet`` now defines a base QPushButton colour
rule (rebuilt on every theme_changed) tied to the theme TEXT token.

`qapp` (conftest.py) provides the QApplication the dialog needs.
"""

import re

from modules import ui_helpers


def test_dialog_stylesheet_defines_theme_aware_base_button_color(qapp):
    from modules.settings_dialog import SettingsDialog

    dlg = SettingsDialog()
    try:
        # Strip CSS comments first — they can contain brace examples that
        # would otherwise false-match the rule regex.
        qss = re.sub(r"/\*.*?\*/", "", dlg.styleSheet(), flags=re.DOTALL)
        # Base QPushButton rule (no #objectName / pseudo-state before the brace).
        m = re.search(r"QPushButton\s*\{([^}]*)\}", qss)
        assert m, "dialog must define a base QPushButton rule"
        assert f"color: {ui_helpers.TEXT}" in m.group(1), (
            "base QPushButton colour must be the theme TEXT token so plain "
            "buttons flip with the theme (regression: rule missing → stale "
            "white text on the Customize / PipeWire buttons in light theme)"
        )
    finally:
        dlg.deleteLater()
