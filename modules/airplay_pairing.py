"""
AirPlay 2 pairing dialog.

Modal flow that drives ``modules.airplay2.pair_begin_sync`` /
``pair_finish_sync`` end-to-end:

  1. ``begin``  — worker thread starts the handshake; receiver shows
                  a 4-digit PIN on-screen. Dialog spins.
  2. ``prompt`` — user types the PIN; clicking Pair fires
                  ``pair_finish_sync`` on the worker.
  3. ``done``   — credentials persisted via ``store_credentials``;
                  ``accept()`` so callers see ``QDialog.Accepted`` and
                  can read the freshly-stored creds back from settings.

Errors at any stage drop into ``error`` state with a one-button retry
that re-enters ``begin``.

Both pairing primitives block, so they run via ``modules.async_io.run_async``
to keep the GUI thread responsive. The state machine itself lives in
``_State``; UI is rebuilt on transition rather than imperatively
toggling visibility, which keeps the dialog easy to reason about
(every state shows a coherent set of widgets).
"""

from __future__ import annotations
from enum import Enum
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QDialog,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QHBoxLayout,
    QWidget,
)

from modules.airplay2 import (
    AirPlay2Device,
    _PairingHandle,
    pair_begin_sync,
    pair_finish_sync,
    store_credentials,
)
from modules.async_io import run_async
from modules.design_tokens import (
    TYPE_DISPLAY,
    TYPE_TITLE,
    TYPE_BODY,
    TYPE_CAPTION,
    type_qss,
)
from modules.ui_helpers import (
    BORDER,
    TEXT,
    TEXT_DIM,
    TEXT_FAINT,
    ink_alpha,
)


class _State(Enum):
    BEGIN = "begin"  # spinner — pair_begin_sync running
    PROMPT = "prompt"  # PIN entry — pair_finish_sync waiting on user
    FINISH = "finish"  # spinner — pair_finish_sync running
    ERROR = "error"  # message + retry button


class PairingDialog(QDialog):
    """Modal AirPlay 2 pairing dialog. Use the ``run`` classmethod to
    drive it end-to-end and pull the resulting credentials blob:

        creds = PairingDialog.run(parent, device)
        if creds:
            # successfully paired; airplay2.store_credentials was
            # already called inside the dialog before accept(), so the
            # caller usually doesn't need to re-store.
            ...
    """

    BODY_RADIUS = 12
    _FIXED_W = 380
    _FIXED_H = 260

    # Emitted after a successful pair so the host can immediately
    # retry whatever cast triggered the dialog. The payload is the
    # credentials blob (same as ``result_credentials``).
    paired = Signal(str)

    def __init__(self, device: AirPlay2Device, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._device = device
        self._state: _State = _State.BEGIN
        self._handle: Optional[_PairingHandle] = None
        self._error_text: str = ""
        self.result_credentials: str = ""

        self.setWindowTitle("Pair AirPlay 2 device")
        self.setFixedSize(self._FIXED_W, self._FIXED_H)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Dialog)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setObjectName("jtPairingDialog")
        self.setModal(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 18)
        outer.setSpacing(12)

        # Header — title + device name. Stays the same across every
        # state, so it's built once in __init__ rather than rebuilt
        # on transitions.
        title = QLabel("Pair AirPlay 2 device")
        title.setStyleSheet(f"color: {TEXT}; {type_qss(TYPE_TITLE)}")
        outer.addWidget(title)

        device_lbl = QLabel(self._device.name)
        device_lbl.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_BODY)}")
        outer.addWidget(device_lbl)

        # Body container — replaced on every state transition. Holds
        # the spinner / PIN field / error message + its action buttons.
        self._body_host = QWidget()
        self._body_host.setStyleSheet("background: transparent;")
        self._body_layout = QVBoxLayout(self._body_host)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(12)
        outer.addWidget(self._body_host, 1)

        self._render_state()
        # Kick off pairing as soon as the dialog opens — the user
        # picked a device to pair, no point making them confirm again.
        self._start_begin()

    # ── State rendering ─────────────────────────────────────────────────

    def _render_state(self):
        """Tear down the current body and rebuild for ``self._state``.
        Every state owns its own widget set + button row; rebuilding
        rather than toggling visibility keeps the code linear (no
        cross-state widget references to forget about)."""
        while self._body_layout.count():
            item = self._body_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
            else:
                # Layout (the button row's QHBoxLayout) — clear and
                # mark for GC. Children deleteLater themselves.
                sub = item.layout()
                if sub is not None:
                    while sub.count():
                        sub_item = sub.takeAt(0)
                        sw = sub_item.widget()
                        if sw is not None:
                            sw.deleteLater()

        if self._state == _State.BEGIN:
            self._render_begin()
        elif self._state == _State.PROMPT:
            self._render_prompt()
        elif self._state == _State.FINISH:
            self._render_finish()
        elif self._state == _State.ERROR:
            self._render_error()

    def _render_begin(self):
        msg = QLabel(
            "Asking the receiver for a PIN. Look at the device's "
            "screen — a 4-digit code should appear shortly."
        )
        msg.setWordWrap(True)
        msg.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)} padding-top: 6px;")
        self._body_layout.addWidget(msg)
        self._body_layout.addStretch(1)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch(1)
        cancel = self._ghost_button("Cancel")
        cancel.clicked.connect(self.reject)
        btn_row.addWidget(cancel)
        self._body_layout.addLayout(btn_row)

    def _render_prompt(self):
        instr = QLabel("Enter the 4-digit PIN shown on the device:")
        instr.setStyleSheet(f"color: {TEXT}; {type_qss(TYPE_BODY)}")
        self._body_layout.addWidget(instr)

        self._pin_input = QLineEdit()
        self._pin_input.setMaxLength(4)
        self._pin_input.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._pin_input.setInputMethodHints(Qt.InputMethodHint.ImhDigitsOnly)
        self._pin_input.setPlaceholderText("0000")
        # font-size from TYPE_DISPLAY (22px base) — PIN entry visually
        # ranks alongside dialog headings. letter-spacing kept as a raw
        # value: it's a visual property of monospaced digit entry, not
        # a typography-tier concern.
        self._pin_input.setStyleSheet(f"""
            QLineEdit {{
                background: {ink_alpha(0.06)};
                color: {TEXT};
                border: 1px solid {BORDER};
                border-radius: 6px;
                padding: 10px 12px;
                font-size: {TYPE_DISPLAY.size_px}px;
                font-weight: 600;
                letter-spacing: 8px;
            }}
            QLineEdit:focus {{
                border-color: {ink_alpha(0.32)};
            }}
        """)
        # Enter submits — common-sense PIN entry UX. Pair button stays
        # too so a user without a keyboard (touchscreen) can finish.
        self._pin_input.returnPressed.connect(self._submit_pin)
        self._body_layout.addWidget(self._pin_input)
        self._pin_input.setFocus()

        self._body_layout.addStretch(1)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch(1)
        cancel = self._ghost_button("Cancel")
        cancel.clicked.connect(self.reject)
        btn_row.addWidget(cancel)
        pair_btn = self._primary_button("Pair")
        pair_btn.clicked.connect(self._submit_pin)
        btn_row.addWidget(pair_btn)
        self._body_layout.addLayout(btn_row)

    def _render_finish(self):
        msg = QLabel("Finalising pairing…")
        msg.setStyleSheet(f"color: {TEXT}; {type_qss(TYPE_BODY)}")
        self._body_layout.addWidget(msg)
        self._body_layout.addStretch(1)

    def _render_error(self):
        # Distinguish "device asked for credentials we already had but
        # they're stale" from "user typed the wrong PIN" via the
        # message — both end up here, but the user-facing fix differs.
        msg = QLabel(self._error_text or "Pairing failed. Please try again.")
        msg.setWordWrap(True)
        msg.setStyleSheet(
            f"color: {TEXT}; {type_qss(TYPE_BODY)} "
            f"background: rgba(239,68,68,0.10); "
            f"border-radius: 6px; padding: 10px 14px;"
        )
        self._body_layout.addWidget(msg)
        self._body_layout.addStretch(1)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch(1)
        cancel = self._ghost_button("Cancel")
        cancel.clicked.connect(self.reject)
        btn_row.addWidget(cancel)
        retry = self._primary_button("Try again")
        retry.clicked.connect(self._start_begin)
        btn_row.addWidget(retry)
        self._body_layout.addLayout(btn_row)

    # ── State transitions ──────────────────────────────────────────────

    def _set_state(self, state: _State):
        self._state = state
        self._render_state()

    def _start_begin(self):
        """Fire pair_begin_sync on a worker thread. The receiver
        should respond with its on-screen PIN within a few seconds;
        once it does, the worker returns the handle and we move to
        PROMPT for the user to type the PIN."""
        self._handle = None
        self._set_state(_State.BEGIN)

        def _go() -> _PairingHandle:
            return pair_begin_sync(self._device)

        def _on_result(handle: _PairingHandle):
            print("[ap2-dbg] dialog: _start_begin success → PROMPT", flush=True)
            self._handle = handle
            self._set_state(_State.PROMPT)

        def _on_error(err: Exception):
            print(f"[ap2-dbg] dialog: _start_begin error: {type(err).__name__}: {err}", flush=True)
            self._error_text = (
                f"Couldn't start pairing: {err}\n\n"
                "Make sure the receiver is reachable and not paired "
                "to another device right now."
            )
            self._set_state(_State.ERROR)

        run_async(_go, on_result=_on_result, on_error=_on_error)

    def _submit_pin(self):
        if self._handle is None:
            return
        pin = (self._pin_input.text() or "").strip()
        if len(pin) < 4:
            # Don't even hit the network with an obviously-incomplete
            # PIN — pyatv would reject it with a less helpful message.
            self._pin_input.setFocus()
            self._pin_input.selectAll()
            return
        handle = self._handle
        self._set_state(_State.FINISH)

        def _go() -> str:
            return pair_finish_sync(handle, pin)

        def _on_result(creds: str):
            print(f"[ap2-dbg] dialog: _submit_pin success creds_len={len(creds)}", flush=True)
            if not creds:
                self._error_text = (
                    "The receiver accepted the handshake but didn't "
                    "return credentials. Try pairing again."
                )
                self._set_state(_State.ERROR)
                return
            # Persist immediately so a host that retries the cast
            # after this dialog accepts can read them back without
            # plumbing the blob through return values.
            store_credentials(self._device.identifier, creds)
            print(f"[ap2-dbg] dialog: stored creds for {self._device.identifier!r}", flush=True)
            self.result_credentials = creds
            self.paired.emit(creds)
            self.accept()

        def _on_error(err: Exception):
            print(f"[ap2-dbg] dialog: _submit_pin error: {type(err).__name__}: {err}", flush=True)
            self._error_text = (
                f"Pairing failed: {err}\n\n"
                "Double-check the PIN — common cause is a typo or a "
                "stale code (the receiver rotates them after a few "
                "minutes)."
            )
            # Reset the handle so Try-again re-runs begin from scratch
            # rather than reusing the (now-dead) handler.
            self._handle = None
            self._set_state(_State.ERROR)

        run_async(_go, on_result=_on_result, on_error=_on_error)

    # ── Helpers ────────────────────────────────────────────────────────

    def _ghost_button(self, text: str) -> QPushButton:
        """Borderless / faint button — used for Cancel everywhere.
        Object name "ghost" picks up the dialog's accent-bordered
        styling cascade if the dialog inherits one (we don't here, but
        the consistency is cheap)."""
        btn = QPushButton(text)
        btn.setStyleSheet(f"""
            QPushButton {{
                background: {ink_alpha(0.04)};
                color: {TEXT};
                border: 1px solid {BORDER};
                border-radius: 6px;
                padding: 6px 14px;
                {type_qss(TYPE_BODY)}
            }}
            QPushButton:hover {{
                background: {ink_alpha(0.08)};
                border-color: {ink_alpha(0.20)};
            }}
        """)
        return btn

    def _primary_button(self, text: str) -> QPushButton:
        """Accent-bordered primary action (Pair / Try again). Pulls
        the accent at call time so a runtime accent change tints this
        button correctly on dialog reopen."""
        from modules.ui_helpers import ACCENT as _A
        from modules.theme import _hex_to_rgb

        r, g, b = _hex_to_rgb(_A)
        btn = QPushButton(text)
        btn.setStyleSheet(f"""
            QPushButton {{
                background: rgba({r},{g},{b},0.18);
                color: {TEXT};
                border: 1px solid rgba({r},{g},{b},0.65);
                border-radius: 6px;
                padding: 6px 14px;
                {type_qss(TYPE_BODY)}
            }}
            QPushButton:hover {{
                background: rgba({r},{g},{b},0.30);
                border-color: rgba({r},{g},{b},0.85);
            }}
            QPushButton:pressed {{
                background: rgba({r},{g},{b},0.42);
            }}
        """)
        return btn

    def paintEvent(self, e):
        # Rounded card body matching SettingsDialog / CastDialog.
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
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
            # Live read — refresh_theme() rebinds the module attribute;
            # the import-time binding would freeze the body opacity.
            from modules import ui_helpers as _uih

            p.setBrush(QColor(*_uih.DIALOG_BODY_COLOR))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawPath(path)
        finally:
            p.end()

    # ── Public API ─────────────────────────────────────────────────────

    @classmethod
    def run(cls, parent, device: AirPlay2Device) -> str:
        """Open the dialog, drive pairing to completion or cancel,
        return the credentials blob on success or "" otherwise.
        Credentials are also persisted inside the dialog before it
        accepts — the return is for callers that want to react
        immediately (e.g. retry a cast that triggered the pair)."""
        dlg = cls(device, parent=parent)
        result = dlg.exec()
        if result == QDialog.DialogCode.Accepted:
            return dlg.result_credentials
        return ""
