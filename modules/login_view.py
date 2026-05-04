"""Native sign-in surface — replaces Jellyfin Web's login page as the
boot-time authentication entry point.

A centered card with server URL / username / password fields and a
Sign In button. On submit it calls ``JellyfinAPI.authenticate`` via
``run_async`` (the GUI thread can't block on a 10s POST timeout) and
emits ``signed_in`` on success. The host shows this view in the
content stack whenever the API isn't authenticated; on success the
host swaps to the user's home destination.

JF Web still loads in the background for now — its embed is needed
for the Account / preferences page until that's natively replaced —
but it sits hidden behind this surface until the user authenticates."""

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QWidget, QFrame, QLabel, QLineEdit, QPushButton, QVBoxLayout, QHBoxLayout,
)

from modules.async_io import run_async
from modules.jellyfin_api import get_api
from modules.ui_helpers import BORDER, TEXT, TEXT_DIM, TEXT_FAINT, ACCENT
from modules.design_tokens import (
    TYPE_DISPLAY, TYPE_BODY, TYPE_CAPTION, type_qss,
    SPACE_SM, SPACE_MD, SPACE_LG, SPACE_XL,
)


CARD_WIDTH = 420


class LoginView(QWidget):
    """Centered sign-in card. Emits ``signed_in`` on successful
    ``api.authenticate`` round-trip; host listens to swap to the
    music landing surface."""

    signed_in = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.api = get_api()
        self._submitting = False

        self.setObjectName("loginView")
        # Sweep transparency across descendants so the card sits over
        # the body color cleanly (matches the pattern other native
        # views use to defeat GLOBAL_STYLE's QWidget background).
        self.setStyleSheet("""
            QWidget#loginView,
            QWidget#loginView QWidget {
                background: transparent;
            }
        """)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addStretch(1)

        # Center column: card flanked by horizontal stretches so it
        # stays centered regardless of window width.
        center_row = QHBoxLayout()
        center_row.addStretch(1)

        self._card = QFrame()
        self._card.setObjectName("loginCard")
        self._card.setFixedWidth(CARD_WIDTH)
        self._card.setStyleSheet(f"""
            QFrame#loginCard {{
                background: rgba(20, 22, 26, 0.92);
                border: 1px solid {BORDER};
                border-radius: 14px;
            }}
        """)

        card_layout = QVBoxLayout(self._card)
        card_layout.setContentsMargins(SPACE_XL + 4, SPACE_XL + 4,
                                       SPACE_XL + 4, SPACE_XL + 4)
        card_layout.setSpacing(SPACE_MD)

        title = QLabel("JellyToast")
        title.setStyleSheet(
            f"color: {TEXT}; {type_qss(TYPE_DISPLAY)} font-weight: 600;"
        )
        card_layout.addWidget(title)

        subtitle = QLabel("Sign in to your Jellyfin server")
        subtitle.setStyleSheet(
            f"color: {TEXT_DIM}; {type_qss(TYPE_BODY)}"
        )
        card_layout.addWidget(subtitle)
        card_layout.addSpacing(SPACE_LG)

        # Field labels are small captions above the inputs — Material-
        # style "floating label" would need extra widget code; this
        # is simpler and just as clear.
        self._server_field = self._build_field(
            "Server URL", "http://your.server:8096",
            initial=self.api.settings.server_url,
        )
        self._username_field = self._build_field(
            "Username", "",
            initial=self.api.settings.username,
        )
        self._password_field = self._build_field(
            "Password", "", password=True,
        )

        for label, field in (("Server URL", self._server_field),
                              ("Username", self._username_field),
                              ("Password", self._password_field)):
            cap = QLabel(label.upper())
            cap.setStyleSheet(
                f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)} "
                "letter-spacing: 0.6px;"
            )
            card_layout.addWidget(cap)
            card_layout.addWidget(field)
            card_layout.addSpacing(SPACE_SM)

        # Error message — hidden until a sign-in attempt fails.
        self._error_label = QLabel("")
        self._error_label.setStyleSheet(
            f"color: #f87171; {type_qss(TYPE_CAPTION)}"
        )
        self._error_label.setWordWrap(True)
        self._error_label.setVisible(False)
        card_layout.addWidget(self._error_label)
        card_layout.addSpacing(SPACE_MD)

        self._submit_btn = QPushButton("Sign in")
        self._submit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._submit_btn.setFixedHeight(40)
        self._submit_btn.setStyleSheet(f"""
            QPushButton {{
                background: {ACCENT};
                color: white;
                border: none;
                border-radius: 8px;
                {type_qss(TYPE_BODY)}
                font-weight: 600;
            }}
            QPushButton:hover {{ background: {ACCENT}; opacity: 0.92; }}
            QPushButton:pressed {{ background: {ACCENT}; }}
            QPushButton:disabled {{
                background: rgba(255,255,255,0.10);
                color: rgba(255,255,255,0.50);
            }}
        """)
        self._submit_btn.clicked.connect(self._submit)
        card_layout.addWidget(self._submit_btn)

        center_row.addWidget(self._card)
        center_row.addStretch(1)
        outer.addLayout(center_row)
        outer.addStretch(1)

        # Initial focus: password if username is already filled in,
        # username if not, server URL if neither — first empty field.
        if not self._server_field.text():
            self._server_field.setFocus()
        elif not self._username_field.text():
            self._username_field.setFocus()
        else:
            self._password_field.setFocus()

    def _build_field(self, label: str, placeholder: str,
                     initial: str = "", password: bool = False) -> QLineEdit:
        edit = QLineEdit()
        edit.setPlaceholderText(placeholder)
        edit.setText(initial or "")
        if password:
            edit.setEchoMode(QLineEdit.EchoMode.Password)
        edit.setStyleSheet(f"""
            QLineEdit {{
                background: rgba(255,255,255,0.06);
                color: {TEXT};
                border: 1px solid {BORDER};
                border-radius: 8px;
                padding: 10px 14px;
                {type_qss(TYPE_BODY)}
                selection-background-color: rgba(255,255,255,0.20);
            }}
            QLineEdit:focus {{
                border-color: rgba(255,255,255,0.32);
                background: rgba(255,255,255,0.08);
            }}
        """)
        # Submit on Return from any field — the typical "fill out the
        # form, hit Enter" UX.
        edit.returnPressed.connect(self._submit)
        return edit

    @Slot()
    def _submit(self):
        if self._submitting:
            return
        server = self._server_field.text().strip()
        username = self._username_field.text().strip()
        password = self._password_field.text()
        if not server or not username:
            self._show_error("Please fill in the server URL and username.")
            return
        # Server URL must include a scheme — Jellyfin's handlers will
        # 404 on a bare host.
        if "://" not in server:
            server = "http://" + server
            self._server_field.setText(server)

        self._set_submitting(True)
        self._error_label.setVisible(False)
        # Two-phase: probe /System/Info/Public first to validate the
        # URL is actually a Jellyfin server BEFORE the password is
        # sent over the wire. Catches typos / wrong-port errors with
        # a clear message and lets us capture ServerId for the
        # credential record. On probe success we authenticate.
        run_async(
            self.api.server_info, server,
            on_result=lambda info: self._on_probe_ok(
                server, username, password, info,
            ),
            on_error=lambda e: self._on_probe_err(e),
        )

    def _on_probe_ok(self, server: str, username: str, password: str, info):
        # Sanity-check the response shape — a non-Jellyfin server at
        # the URL might return 200 with arbitrary JSON. Real
        # /System/Info/Public always returns at minimum Id +
        # ProductName.
        if not info or "Id" not in info or "ProductName" not in info:
            self._set_submitting(False)
            self._show_error(
                "That URL responded but doesn't look like a Jellyfin server."
            )
            return
        # URL is a real Jellyfin server. Go ahead and authenticate.
        run_async(
            self.api.authenticate, server, username, password,
            on_result=lambda _data: self._on_auth_ok(),
            on_error=lambda e: self._on_auth_err(e),
        )

    def _on_probe_err(self, err: Exception):
        self._set_submitting(False)
        msg = str(err) or err.__class__.__name__
        if "Connection" in msg or "Max retries" in msg or "timed out" in msg:
            msg = "Couldn't reach the server. Check the URL and your network."
        elif "404" in msg or "Not Found" in msg:
            msg = "That URL doesn't look like a Jellyfin server."
        else:
            msg = f"Couldn't reach the server: {msg}"
        self._show_error(msg)

    def _on_auth_ok(self):
        self._set_submitting(False)
        # Clear the password field on success so it doesn't sit in the
        # form if the user signs out and lands here again.
        self._password_field.clear()
        self.signed_in.emit()

    def _on_auth_err(self, err: Exception):
        self._set_submitting(False)
        msg = str(err) or err.__class__.__name__
        # Common case: HTTPError 401 from a wrong password. The full
        # error string from requests is verbose; surface a friendly
        # message and tuck the technical detail underneath.
        if "401" in msg or "Unauthorized" in msg:
            msg = "Wrong username or password."
        elif "404" in msg or "Not Found" in msg:
            msg = "Server not found at that URL."
        elif "Connection" in msg or "Max retries" in msg:
            msg = "Couldn't reach the server. Check the URL and your network."
        self._show_error(msg)

    def _show_error(self, msg: str):
        self._error_label.setText(msg)
        self._error_label.setVisible(True)

    def _set_submitting(self, submitting: bool):
        self._submitting = submitting
        self._submit_btn.setText("Signing in…" if submitting else "Sign in")
        self._submit_btn.setEnabled(not submitting)
        for f in (self._server_field, self._username_field, self._password_field):
            f.setEnabled(not submitting)

    def keyPressEvent(self, event: QKeyEvent):
        # Esc on the login surface is a no-op — there's nothing to
        # dismiss to without authentication.
        if event.key() == Qt.Key.Key_Escape:
            return
        super().keyPressEvent(event)
