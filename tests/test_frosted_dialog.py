"""Tests for modules.frosted_dialog — the app-styled replacement for
QMessageBox.warning used by the cast-failed alert (and reusable elsewhere).

Visual frosting is verified by screenshot; these pin the construction
contract: it builds without a native message box, carries the frosted
object name + a status-aware body colour, and surfaces the message text.
"""

from __future__ import annotations

from modules.frosted_dialog import FrostedMessageDialog, frosted_info, frosted_warning


def test_constructs_with_frosted_identity(qapp):
    dlg = FrostedMessageDialog(
        None, title="Cast failed", text="Could not cast to X.", icon_name="cast"
    )
    assert dlg.objectName() == "jtFrostedDialog"
    assert dlg.isModal() is True
    # Status-aware body colour is an RGBA 4-tuple (never see-through default).
    assert isinstance(dlg._dialog_body_color, tuple)
    assert len(dlg._dialog_body_color) == 4
    assert dlg._msg.text() == "Could not cast to X."


def test_translucent_background_attribute_set(qapp):
    from PySide6.QtCore import Qt

    dlg = FrostedMessageDialog(None, title="T", text="body")
    assert dlg.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground) is True


def test_esc_rejects(qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeyEvent

    dlg = FrostedMessageDialog(None, title="T", text="body")
    rejected = []
    dlg.rejected.connect(lambda: rejected.append(True))
    dlg.keyPressEvent(QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier))
    assert rejected == [True]


def test_helpers_are_callable_aliases():
    # frosted_info is the same surface as frosted_warning (different intent).
    assert frosted_info is frosted_warning
    assert callable(frosted_warning)


def test_no_icon_when_name_blank(qapp):
    # An empty icon_name must not raise (the glyph is simply omitted).
    dlg = FrostedMessageDialog(None, title="T", text="body", icon_name="")
    assert dlg.objectName() == "jtFrostedDialog"
