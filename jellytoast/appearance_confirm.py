"""A "keep this look, or auto-revert" safety prompt for live appearance
changes (font family / scale).

When the user picks an appearance setting that we apply LIVE, we show this
non-modal prompt above the main window with a countdown: press **Keep** to
commit, **Revert** (or Esc / ✕ / let it time out) to undo — the same pattern a
monitor's resolution change uses. The load-bearing constraint: the prompt must
stay READABLE even if the just-applied font is illegible, so its own text is
pinned to a guaranteed-safe font that defeats BOTH the app-wide
``* { font-family }`` QSS rule AND ``app.setFont`` — otherwise a broken pick
would leave the user unable to read the button that undoes it.
"""

from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QHBoxLayout, QLabel, QPushButton, QWidget

from jellytoast.frosted_dialog import FrostedDialog

# A family stack that excludes any user choice, plus a hard 14px size (NOT a
# FONT_SCALE-derived token) so a broken family or scale can't make the prompt
# itself unreadable or oversized.
_SAFE_STACK = "'Inter', 'Segoe UI', 'Noto Sans', 'DejaVu Sans', sans-serif"


def _safe_qfont(px: int = 14, bold: bool = False) -> QFont:
    """The OS default UI font — immune to app.setFont — at a fixed size."""
    f = QFontDatabase.systemFont(QFontDatabase.SystemFont.GeneralFont)
    f.setPixelSize(px)
    f.setBold(bold)
    return f


class AppearanceRevertDialog(FrostedDialog):
    def __init__(
        self,
        parent: Optional[QWidget],
        *,
        on_revert: Callable[[], None],
        seconds: int = 10,
    ) -> None:
        super().__init__(parent, title="Keep this look?", icon_name="info", min_width=380)
        # Non-modal + .show() (NOT .exec()) so the countdown runs under the
        # normal event loop and the user can see the app behind the prompt.
        self.setModal(False)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        self._on_revert = on_revert
        self._kept = False
        self._finished = False
        self._remaining = int(seconds)

        from jellytoast.design_tokens import BTN_PRIMARY, button_qss
        from jellytoast.ui_helpers import TEXT, TEXT_DIM

        msg = QLabel("Applied your new look. Keep it, or revert to the previous one?")
        msg.setWordWrap(True)
        msg.setFont(_safe_qfont())
        msg.setStyleSheet(
            f"color: {TEXT}; font-family: {_SAFE_STACK}; font-size: 14px; background: transparent;"
        )
        self.content_layout.addWidget(msg)

        self._count = QLabel(self._count_text())
        self._count.setFont(_safe_qfont(px=12))
        self._count.setStyleSheet(
            f"color: {TEXT_DIM}; font-family: {_SAFE_STACK}; font-size: 12px; background: transparent;"
        )
        self.content_layout.addWidget(self._count)

        row = QHBoxLayout()
        row.addStretch(1)
        revert = QPushButton("Revert now")
        revert.setObjectName("ghost")
        revert.setFont(_safe_qfont())
        revert.setStyleSheet(f"QPushButton {{ font-family: {_SAFE_STACK}; font-size: 14px; }}")
        revert.setCursor(Qt.CursorShape.PointingHandCursor)
        revert.clicked.connect(self.reject)  # not kept → _finish reverts
        row.addWidget(revert)

        keep = QPushButton("Keep")
        keep.setObjectName("accent")
        keep.setFont(_safe_qfont(bold=True))
        # button_qss for the accent look (Breeze default-button footgun), then a
        # safe font-family override appended so it wins over the app font.
        keep.setStyleSheet(
            button_qss(BTN_PRIMARY)
            + f"QPushButton {{ font-family: {_SAFE_STACK}; font-size: 14px; }}"
        )
        keep.setCursor(Qt.CursorShape.PointingHandCursor)
        keep.setDefault(True)
        keep.clicked.connect(self._do_keep)
        row.addWidget(keep)
        self.content_layout.addLayout(row)

        self._timer = QTimer(self)  # owned by this GUI-thread widget → affinity OK
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    def _count_text(self) -> str:
        return f"Reverting automatically in {self._remaining}s…"

    def _tick(self) -> None:
        self._remaining -= 1
        if self._remaining <= 0:
            self._finish()  # not kept → reverts
            return
        self._count.setText(self._count_text())

    def _do_keep(self) -> None:
        self._kept = True
        self._finish()

    def reject(self) -> None:  # Esc / ✕ / Revert-now (FrostedDialog wires Esc+✕)
        self._finish()

    def _finish(self) -> None:
        """The single, idempotent teardown chokepoint. Keep / Revert / Esc / ✕ /
        timeout all route here: revert unless Keep was pressed, then tear down
        with hide() + deleteLater().

        Critically it does NOT call self.close(). ``QDialog.closeEvent`` calls
        ``reject()``, and our ``reject()`` routes here — if teardown went through
        close() that would re-enter closeEvent → reject() → close() … a mutual
        recursion that RecursionError'd and FROZE the prompt (Keep did nothing,
        every button dead). hide() + deleteLater() sidesteps closeEvent."""
        if self._finished:
            return
        self._finished = True
        self._timer.stop()
        if not self._kept:
            try:
                self._on_revert()
            except Exception:
                pass
        self.hide()
        self.deleteLater()

    def closeEvent(self, e) -> None:  # noqa: N802 — WM / programmatic close()
        # Route a stray close() through the same chokepoint, but do NOT chain to
        # QDialog.closeEvent (which calls reject() → would recurse). _finish is
        # idempotent, so if Keep/Revert already ran, this is a no-op.
        self._finish()
        e.accept()


# Module-level ref keeps the dialog alive even if it ends up parentless (tests);
# with a real parent window Qt's ownership already keeps it alive.
_active: Optional[AppearanceRevertDialog] = None


def show_appearance_revert(
    on_revert: Callable[[], None], *, seconds: int = 10
) -> AppearanceRevertDialog:
    """Apply-and-confirm: the caller has ALREADY applied the change live; this
    shows the keep/revert prompt above the main window and calls ``on_revert``
    if the user doesn't keep it within ``seconds``. Hosted on the main window
    (not the Settings dialog, which the user may close mid-countdown)."""
    global _active
    main = None
    for w in QApplication.topLevelWidgets():
        if type(w).__name__ == "JellytoastWindow":
            main = w
            break
    dlg = AppearanceRevertDialog(main, on_revert=on_revert, seconds=seconds)
    _active = dlg
    dlg.show()
    dlg.raise_()
    return dlg
