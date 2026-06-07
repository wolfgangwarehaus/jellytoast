"""Frosted, frameless message dialog — the app-styled replacement for
``QMessageBox.warning`` / ``information`` so transient alerts (e.g. "Cast
failed") match the main window + settings/cast dialogs instead of popping a
native system box.

Mirrors ``CastDialog``'s scaffold: frameless everywhere EXCEPT KDE Wayland,
where it stays a decorated ``Window`` stripped by the app-wide KWin
``noborder`` rule (KWin drops the blur effect on undecorated windows, so the
decorated+noborder route is what keeps it frosted). Body colour is
status-aware (``body_color_tuple`` — glass when blur is verified, near-opaque
otherwise) so it is never see-through; compositor blur is applied once the
surface is mapped, matching the other frosted surfaces.
"""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from modules.design_tokens import RADIUS_WINDOW


class FrostedMessageDialog(QDialog):
    """A frameless, frosted alert with a titlebar (icon + title + ✕), a
    word-wrapped message, and one accent OK button. Use the module helpers
    (:func:`frosted_warning` / :func:`frosted_info`) for the common case."""

    BODY_RADIUS = RADIUS_WINDOW

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        title: str = "",
        text: str = "",
        icon_name: str = "",
        ok_text: str = "OK",
    ) -> None:
        super().__init__(parent)
        from modules.platform_compat import is_kde_wayland
        from modules.ui_helpers import GLOBAL_STYLE, body_color_tuple

        # Frameless off KDE Wayland; on KDE the decorated window + the app-wide
        # KWin noborder rule keeps the blur effect (see module docstring).
        flags = Qt.WindowType.Window
        if not is_kde_wayland():
            flags |= Qt.WindowType.FramelessWindowHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setObjectName("jtFrostedDialog")
        self.setModal(True)
        self.setMinimumWidth(360)
        # Status-aware body: glass when blur is verified, near-opaque frosted
        # panel otherwise — never see-through. Shared with the main window +
        # the cast/settings dialogs via ui_helpers.body_color_tuple.
        self._dialog_body_color = body_color_tuple("dialog")
        self.setStyleSheet(GLOBAL_STYLE)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        outer.addWidget(self._build_titlebar(title, icon_name))

        from modules.design_tokens import TYPE_BODY, type_qss
        from modules.ui_helpers import TEXT

        body = QWidget()
        body.setStyleSheet("background: transparent;")
        bl = QVBoxLayout(body)
        bl.setContentsMargins(20, 4, 20, 18)
        bl.setSpacing(16)
        self._msg = QLabel(text)
        self._msg.setWordWrap(True)
        # Selectable so copy-paste-ready content (e.g. the Casting-page
        # firewall rule) can actually be copied out of the dialog.
        self._msg.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
            | Qt.TextInteractionFlag.TextSelectableByKeyboard
        )
        self._msg.setStyleSheet(
            f"color: {TEXT}; {type_qss(TYPE_BODY)} background: transparent;"
        )
        bl.addWidget(self._msg)

        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        ok = QPushButton(ok_text)
        ok.setObjectName("accent")
        ok.setCursor(Qt.CursorShape.PointingHandCursor)
        ok.setDefault(True)
        ok.clicked.connect(self.accept)
        btn_row.addWidget(ok)
        bl.addLayout(btn_row)
        outer.addWidget(body)

    def _build_titlebar(self, title: str, icon_name: str) -> QWidget:
        from modules.design_tokens import TYPE_CAPTION, TYPE_SUBHEAD, type_qss
        from modules.ui_helpers import TEXT, TEXT_DIM, WASH_HOVER

        tb = QWidget()
        tb.setFixedHeight(46)
        tb.setObjectName("jtFrostedTitle")
        tb.setStyleSheet(
            "QWidget#jtFrostedTitle { background: transparent; }"
            "QWidget#jtFrostedTitle QLabel { background: transparent; }"
        )
        h = QHBoxLayout(tb)
        h.setContentsMargins(20, 0, 8, 0)
        h.setSpacing(10)

        if icon_name:
            from modules.icons import icon

            glyph = QLabel()
            glyph.setPixmap(icon(icon_name).pixmap(QSize(18, 18)))
            h.addWidget(glyph)

        lbl = QLabel(title)
        lbl.setStyleSheet(f"color: {TEXT}; {type_qss(TYPE_SUBHEAD)}")
        h.addWidget(lbl)
        h.addStretch(1)

        close_btn = QPushButton("✕")
        close_btn.setFixedSize(36, 28)
        close_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        close_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; color: {TEXT_DIM}; "
            f"border: none; border-radius: 6px; {type_qss(TYPE_CAPTION)} }}"
            f"QPushButton:hover {{ background: {WASH_HOVER}; color: {TEXT}; }}"
        )
        close_btn.clicked.connect(self.reject)
        h.addWidget(close_btn)

        tb.mousePressEvent = self._titlebar_press
        return tb

    def _titlebar_press(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            handle = self.windowHandle()
            if handle is not None:
                handle.startSystemMove()

    def keyPressEvent(self, e):
        # Esc dismisses; the frameless + WA_TranslucentBackground combo on KDE
        # Wayland doesn't reliably route the key to QDialog's default handler.
        if e.key() == Qt.Key.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(e)

    def showEvent(self, e):
        super().showEvent(e)
        QTimer.singleShot(0, self._apply_blur)

    def _apply_blur(self):
        from modules import blur
        from modules.theme import get_active_theme

        blur.apply(self, get_active_theme().blur, corner_radius=self.BODY_RADIUS)

    def paintEvent(self, e):
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            # Source-replace clears the surface, then paint the rounded body so
            # the corners stay transparent for the compositor blur region.
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
            p.fillRect(self.rect(), Qt.GlobalColor.transparent)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
            path = QPainterPath()
            path.addRoundedRect(
                0.0,
                0.0,
                float(self.width()),
                float(self.height()),
                self.BODY_RADIUS,
                self.BODY_RADIUS,
            )
            p.setBrush(QColor(*self._dialog_body_color))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawPath(path)
        finally:
            p.end()


def frosted_warning(
    parent: Optional[QWidget],
    title: str,
    text: str,
    *,
    icon_name: str = "info",
    ok_text: str = "OK",
) -> None:
    """Show an app-styled (frosted, frameless) alert and block until dismissed
    — the drop-in for ``QMessageBox.warning`` where matching the app chrome
    matters."""
    FrostedMessageDialog(
        parent, title=title, text=text, icon_name=icon_name, ok_text=ok_text
    ).exec()


# Alias — same surface, different intent at the call site.
frosted_info = frosted_warning
