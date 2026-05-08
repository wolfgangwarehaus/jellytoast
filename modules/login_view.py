"""Native sign-in surface — boot-time authentication entry point.

A centered card with server URL / username / password fields and a
Sign In button. On submit it calls the active provider's
``authenticate`` via ``run_async`` (the GUI thread can't block on a
10s POST timeout) and emits ``signed_in`` on success. The host shows
this view in the content stack whenever the API isn't authenticated;
on success the host swaps to the user's home destination."""

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QWidget, QFrame, QLabel, QLineEdit, QPushButton, QVBoxLayout, QHBoxLayout,
    QComboBox,
)

from modules.async_io import run_async
from modules.providers import get_provider, reset_provider
from modules.settings import get_settings
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
        # Talk to the backend through the provider abstraction so a
        # future Subsonic / Navidrome provider can plug in here
        # without LoginView changes.
        self.provider = get_provider()
        self._settings = get_settings()
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

        self._subtitle = QLabel("Sign in to your music server")
        self._subtitle.setStyleSheet(
            f"color: {TEXT_DIM}; {type_qss(TYPE_BODY)}"
        )
        card_layout.addWidget(self._subtitle)
        card_layout.addSpacing(SPACE_LG)

        # Server-type picker. The user picks which backend protocol
        # they're signing in to BEFORE typing credentials so the
        # probe + authenticate calls go through the right provider.
        # Default to whatever was used last (persisted in
        # provider_kind) so re-login is one-click.
        kind_cap = QLabel("SERVER TYPE")
        kind_cap.setStyleSheet(
            f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)} "
            "letter-spacing: 0.6px;"
        )
        card_layout.addWidget(kind_cap)
        self._kind_combo = QComboBox()
        self._kind_combo.addItem("Jellyfin", "jellyfin")
        self._kind_combo.addItem("Subsonic / Navidrome", "subsonic")
        saved_kind = (self._settings.provider_kind or "jellyfin").lower()
        for i in range(self._kind_combo.count()):
            if self._kind_combo.itemData(i) == saved_kind:
                self._kind_combo.setCurrentIndex(i)
                break
        self._kind_combo.setStyleSheet(f"""
            QComboBox {{
                background: rgba(255,255,255,0.06);
                color: {TEXT};
                border: 1px solid {BORDER};
                border-radius: 8px;
                padding: 8px 14px;
                {type_qss(TYPE_BODY)}
            }}
            QComboBox:focus {{
                border-color: rgba(255,255,255,0.32);
                background: rgba(255,255,255,0.08);
            }}
            QComboBox::drop-down {{ border: none; width: 24px; }}
        """)
        self._kind_combo.currentIndexChanged.connect(self._on_kind_changed)
        card_layout.addWidget(self._kind_combo)
        card_layout.addSpacing(SPACE_SM)

        # Field labels are small captions above the inputs — Material-
        # style "floating label" would need extra widget code; this
        # is simpler and just as clear.
        self._server_field = self._build_field(
            "Server URL", "http://your.server:8096",
            initial=self._settings.server_url,
        )
        self._username_field = self._build_field(
            "Username", "",
            initial=self._settings.username,
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
        # Server URL must include a scheme — both Jellyfin and
        # Subsonic 404 on bare hosts.
        if "://" not in server:
            server = "http://" + server
            self._server_field.setText(server)

        # Make sure the active provider matches the dropdown
        # selection. The user might have signed out as a Jellyfin user
        # and is now signing in to Subsonic (or vice-versa); the
        # provider singleton was built from the persisted
        # provider_kind which is now stale.
        chosen_kind = self._kind_combo.currentData() or "jellyfin"
        if chosen_kind != self.provider.kind:
            self._settings.provider_kind = chosen_kind
            reset_provider()
            self.provider = get_provider()

        self._set_submitting(True)
        self._error_label.setVisible(False)
        # Two-phase: probe the URL first to validate it's actually a
        # backend of this kind BEFORE the password is sent over the
        # wire. provider.probe returns None on any failure (network
        # error, wrong port, non-backend response) so we don't have
        # to translate exceptions in this path. On success we
        # authenticate.
        run_async(
            self.provider.probe, server,
            on_result=lambda info: self._on_probe_ok(
                server, username, password, info,
            ),
            on_error=lambda e: self._on_probe_err(e),
        )

    def _on_kind_changed(self, _idx: int):
        kind = self._kind_combo.currentData() or "jellyfin"
        if kind == "subsonic":
            self._subtitle.setText(
                "Sign in to your Subsonic / Navidrome server"
            )
            self._server_field.setPlaceholderText(
                "http://your.server:4533"
            )
        else:
            self._subtitle.setText("Sign in to your Jellyfin server")
            self._server_field.setPlaceholderText(
                "http://your.server:8096"
            )

    def _on_probe_ok(self, server: str, username: str, password: str, info):
        if info is None:
            self._set_submitting(False)
            self._show_error(
                "That URL responded but doesn't look like a "
                f"{self.provider.kind.capitalize()} server."
            )
            return
        # URL probe succeeded. Authenticate.
        run_async(
            self.provider.authenticate, server, username, password,
            on_result=lambda _result: self._on_auth_ok(),
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
