"""Tests for jellytoast.frosted_dialog — the app-styled replacement for
QMessageBox.warning used by the cast-failed alert (and reusable elsewhere).

Visual frosting is verified by screenshot; these pin the construction
contract: it builds without a native message box, carries the frosted
object name + a status-aware body colour, and surfaces the message text.
"""

from __future__ import annotations

from jellytoast.frosted_dialog import (
    FrostedConfirmDialog,
    FrostedDialog,
    FrostedMessageDialog,
    frosted_confirm,
    frosted_info,
    frosted_warning,
)


def test_base_dialog_exposes_content_layout(qapp):
    # FrostedDialog is the reusable chrome — callers add widgets to
    # content_layout and inherit the frosted identity + body paint.
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QLabel

    dlg = FrostedDialog(None, title="Import", icon_name="settings")
    assert dlg.objectName() == "jtFrostedDialog"
    assert dlg.isModal() is True
    assert dlg.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground) is True
    assert isinstance(dlg._dialog_body_color, tuple) and len(dlg._dialog_body_color) == 4
    # The body host layout accepts arbitrary widgets.
    before = dlg.content_layout.count()
    dlg.content_layout.addWidget(QLabel("hi"))
    assert dlg.content_layout.count() == before + 1


def test_base_dialog_esc_rejects(qapp):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QKeyEvent

    dlg = FrostedDialog(None, title="T")
    rejected = []
    dlg.rejected.connect(lambda: rejected.append(True))
    dlg.keyPressEvent(
        QKeyEvent(QKeyEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
    )
    assert rejected == [True]


def test_message_dialog_is_a_frosted_dialog(qapp):
    # The message box is now a thin subclass of the reusable base.
    dlg = FrostedMessageDialog(None, title="T", text="body")
    assert isinstance(dlg, FrostedDialog)


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


# ── frosted_confirm — the QMessageBox.question replacement ───────────────────


def test_confirm_dialog_is_frosted_with_two_buttons(qapp):
    from PySide6.QtWidgets import QPushButton

    dlg = FrostedConfirmDialog(
        None, title="Remove?", text="Delete it?", confirm_text="Remove",
        cancel_text="Cancel",
    )
    assert isinstance(dlg, FrostedDialog)
    assert dlg._msg.text() == "Delete it?"
    labels = {b.text() for b in dlg.findChildren(QPushButton)}
    assert {"Remove", "Cancel"} <= labels
    names = {b.objectName() for b in dlg.findChildren(QPushButton)}
    assert {"accent", "ghost"} <= names


def test_frosted_confirm_returns_true_on_accept(qapp, monkeypatch):
    from PySide6.QtWidgets import QDialog

    monkeypatch.setattr(
        FrostedConfirmDialog, "exec", lambda self: QDialog.DialogCode.Accepted
    )
    assert frosted_confirm(None, "T", "body") is True


def test_frosted_confirm_returns_false_on_reject(qapp, monkeypatch):
    from PySide6.QtWidgets import QDialog

    monkeypatch.setattr(
        FrostedConfirmDialog, "exec", lambda self: QDialog.DialogCode.Rejected
    )
    assert frosted_confirm(None, "T", "body") is False


def test_destructive_confirm_defaults_to_cancel(qapp):
    from PySide6.QtWidgets import QPushButton

    # A destructive action focuses Cancel by default so a stray Enter is safe.
    dlg = FrostedConfirmDialog(
        None, title="Clear?", text="x", confirm_text="Clear all", destructive=True
    )
    by_name = {b.objectName(): b for b in dlg.findChildren(QPushButton)}
    assert by_name["ghost"].isDefault() is True
    assert by_name["accent"].isDefault() is False


def test_nondestructive_confirm_defaults_to_confirm(qapp):
    from PySide6.QtWidgets import QPushButton

    dlg = FrostedConfirmDialog(None, title="Apply?", text="x", confirm_text="Apply")
    by_name = {b.objectName(): b for b in dlg.findChildren(QPushButton)}
    assert by_name["accent"].isDefault() is True


def test_accent_default_button_owns_per_widget_style(qapp):
    # The accent default button must carry its OWN stylesheet (button_qss),
    # not lean solely on the app-level #accent object-name rule: under KDE
    # Breeze the native default-button paint hijacks #accent, leaving a white
    # fill + invisible label in light themes (the blank Save&Play / Delete
    # buttons). A non-empty per-widget stylesheet keeps QStyleSheetStyle in
    # full control so Breeze can't take over.
    from PySide6.QtWidgets import QPushButton

    dlg = FrostedConfirmDialog(None, title="T", text="x", confirm_text="Delete")
    accent = next(
        b for b in dlg.findChildren(QPushButton) if b.objectName() == "accent"
    )
    assert accent.isDefault() is True
    assert accent.styleSheet().strip() != ""
