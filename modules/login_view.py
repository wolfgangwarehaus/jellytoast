"""Native sign-in surface — boot-time authentication entry point.

A centered card with server URL / username / password fields and a
Sign In button. On submit it calls the active provider's
``authenticate`` via ``run_async`` (the GUI thread can't block on a
10s POST timeout) and emits ``signed_in`` on success. The host shows
this view in the content stack whenever the API isn't authenticated;
on success the host swaps to the user's home destination."""

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QKeyEvent, QPalette, QColor
from PySide6.QtWidgets import (
    QWidget,
    QFrame,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QHBoxLayout,
    QComboBox,
    QStyledItemDelegate,
    QStyle,
    QStyleOptionViewItem,
)

from modules.async_io import run_async
from modules.providers import get_provider, reset_provider
from modules.settings import get_settings
from modules.ui_helpers import BORDER, TEXT, TEXT_DIM, TEXT_FAINT, ACCENT
from modules.design_tokens import (
    TYPE_DISPLAY,
    TYPE_BODY,
    TYPE_CAPTION,
    type_qss,
    SPACE_XS,
    SPACE_SM,
    SPACE_MD,
    SPACE_LG,
)


CARD_WIDTH = 420


class _AccentItemDelegate(QStyledItemDelegate):
    """Paint combo-popup item backgrounds ourselves so the highlight
    color is actually the user's accent (purple by default), not
    whatever the platform style hard-codes for "selected" / "current".
    KDE Plasma's Breeze style paints these states with a system teal
    that ignores both QSS selection-background-color AND
    QPalette.Highlight — neither route reaches its native painting
    path. Owning the paint cycle is the only reliable workaround."""

    def __init__(self, accent_rgb: "tuple[int, int, int]", parent=None):
        super().__init__(parent)
        self._r, self._g, self._b = accent_rgb

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        is_selected = bool(opt.state & QStyle.StateFlag.State_Selected)
        is_hover = bool(opt.state & QStyle.StateFlag.State_MouseOver)
        # Strip every style hint that triggers native background
        # painting — we'll draw the fill ourselves below, then let
        # the default paint handle text rendering.
        opt.state &= ~QStyle.StateFlag.State_Selected
        opt.state &= ~QStyle.StateFlag.State_MouseOver
        opt.state &= ~QStyle.StateFlag.State_HasFocus
        if is_selected and is_hover:
            alpha = int(0.40 * 255)
        elif is_selected:
            alpha = int(0.30 * 255)
        elif is_hover:
            alpha = int(0.20 * 255)
        else:
            alpha = 0
        if alpha:
            painter.fillRect(
                option.rect,
                QColor(self._r, self._g, self._b, alpha),
            )
        # Force text colors via the option's palette so the default
        # paint draws white text regardless of style group.
        opt.palette.setColor(
            QPalette.ColorRole.Text,
            QColor("#ffffff"),
        )
        opt.palette.setColor(
            QPalette.ColorRole.HighlightedText,
            QColor("#ffffff"),
        )
        super().paint(painter, opt, index)


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
        # Tightened padding/spacing — the card was sized like a
        # full-bleed onboarding panel; this brings it closer to a
        # standard sign-in card so the form doesn't dominate the
        # window on smaller screens.
        card_layout.setContentsMargins(
            SPACE_LG + 4,
            SPACE_LG + 4,
            SPACE_LG + 4,
            SPACE_LG + 4,
        )
        card_layout.setSpacing(SPACE_SM)

        title = QLabel("jellytoast")
        title.setStyleSheet(f"color: {TEXT}; {type_qss(TYPE_DISPLAY)} font-weight: 600;")
        card_layout.addWidget(title)

        self._subtitle = QLabel("Sign in to your music server")
        self._subtitle.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_BODY)}")
        card_layout.addWidget(self._subtitle)
        card_layout.addSpacing(SPACE_MD)

        # Server-type picker. The user picks which backend protocol
        # they're signing in to BEFORE typing credentials so the
        # probe + authenticate calls go through the right provider.
        # Default to whatever was used last (persisted in
        # provider_kind) so re-login is one-click.
        kind_cap = QLabel("SERVER TYPE")
        kind_cap.setStyleSheet(
            f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)} letter-spacing: 0.6px;"
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
        # Materialize the chevron SVG to a cache file so QSS can
        # reference it via `image: url(...)`. Without an explicit
        # arrow image the platform style draws a tiny near-black
        # caret that's invisible against the dark card background.
        # NOTE: must be a hex color, not rgba — Qt's QSvgRenderer
        # (SVG 1.1) silently fails on stroke="rgba(...)" and leaves
        # the arrow invisible, which is what was happening when this
        # was passed TEXT_DIM.
        from modules.icons import icon_svg_path
        from modules.theme import _hex_to_rgb as _h2r

        chevron_path = icon_svg_path("chevron_down", "#c8c8c8")
        # `\` would break QSS — Qt expects forward slashes in url()
        # paths even on Windows.
        chevron_url = chevron_path.replace("\\", "/")
        # ACCENT is a hex string; split it into RGB so we can tint
        # the focus border + popup item highlight at low alpha
        # without showing the platform-default blue.
        _ar, _ag, _ab = _h2r(ACCENT)
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
                /* Accent-tinted border on focus instead of the
                   platform-default blue ring. */
                border-color: rgba({_ar},{_ag},{_ab},0.65);
                background: rgba(255,255,255,0.08);
                outline: none;
            }}
            QComboBox:hover {{
                border-color: rgba({_ar},{_ag},{_ab},0.40);
            }}
            QComboBox::drop-down {{
                border: none;
                width: 28px;
                subcontrol-origin: padding;
                subcontrol-position: right center;
            }}
            QComboBox::down-arrow {{
                image: url({chevron_url});
                width: 12px;
                height: 12px;
            }}
            /* The popup list. Without an explicit opaque background
               the menu inherits the card's translucent surface and
               reads as a ghosted overlay over the URL field below. */
            QComboBox QAbstractItemView {{
                background: rgb(20, 22, 26);
                color: {TEXT};
                border: 1px solid {BORDER};
                border-radius: 8px;
                padding: 4px 0px;
                outline: 0;
                /* Accent-tinted selection so the highlighted item
                   doesn't fall back to platform blue. */
                selection-background-color: rgba({_ar},{_ag},{_ab},0.30);
                selection-color: {TEXT};
            }}
            QComboBox QAbstractItemView::item {{
                padding: 8px 14px;
                min-height: 22px;
            }}
            QComboBox QAbstractItemView::item:hover {{
                background: rgba({_ar},{_ag},{_ab},0.20);
            }}
        """)
        # Install a custom item delegate so the popup paints its own
        # backgrounds with our accent color. QSS + QPalette + a
        # forced Fusion style all failed to override KDE Breeze's
        # native selected-item painting; owning the paint loop via
        # a delegate is the one path that's style-independent.
        try:
            self._kind_combo.setItemDelegate(
                _AccentItemDelegate((_ar, _ag, _ab), self._kind_combo),
            )
            view = self._kind_combo.view()
            view.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            view.setStyleSheet(f"""
                QAbstractItemView {{
                    background: rgb(20, 22, 26);
                    color: {TEXT};
                    border: 1px solid {BORDER};
                    border-radius: 8px;
                    padding: 4px 0px;
                    outline: 0;
                }}
                QAbstractItemView::item {{
                    padding: 8px 14px;
                    min-height: 22px;
                    border: none;
                }}
            """)
        except Exception:
            pass

        self._kind_combo.currentIndexChanged.connect(self._on_kind_changed)
        card_layout.addWidget(self._kind_combo)
        card_layout.addSpacing(SPACE_XS)

        # Field labels are small captions above the inputs — Material-
        # style "floating label" would need extra widget code; this
        # is simpler and just as clear.
        self._server_field = self._build_field(
            "Server URL",
            "http://your.server:8096",
            initial=self._settings.server_url,
        )
        self._username_field = self._build_field(
            "Username",
            "",
            initial=self._settings.username,
        )
        self._password_field = self._build_field(
            "Password",
            "",
            password=True,
        )

        for label, field in (
            ("Server URL", self._server_field),
            ("Username", self._username_field),
            ("Password", self._password_field),
        ):
            cap = QLabel(label.upper())
            cap.setStyleSheet(
                f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)} letter-spacing: 0.6px;"
            )
            card_layout.addWidget(cap)
            card_layout.addWidget(field)
            card_layout.addSpacing(SPACE_XS)

        # Error message — hidden until a sign-in attempt fails.
        self._error_label = QLabel("")
        self._error_label.setStyleSheet(f"color: #f87171; {type_qss(TYPE_CAPTION)}")
        self._error_label.setWordWrap(True)
        self._error_label.setVisible(False)
        card_layout.addWidget(self._error_label)
        card_layout.addSpacing(SPACE_SM)

        self._submit_btn = QPushButton("Sign in")
        self._submit_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._submit_btn.setFixedHeight(40)
        self._submit_btn.setStyleSheet(self._submit_btn_qss())
        self._submit_btn.clicked.connect(self._submit)
        card_layout.addWidget(self._submit_btn)

        # Live-accent: re-stamp the submit-button QSS + combo
        # accents on PlayerBus.theme_changed. The form bakes both
        # at construction; without this, picking a new accent
        # leaves the Sign in button stuck on the previous color.
        from modules.player_state import PlayerBus

        PlayerBus.get().theme_changed.connect(self._reapply_accent)

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

    def _submit_btn_qss(self) -> str:
        """QSS for the Sign in button, built from the CURRENT accent
        so a fresh accent pick in Settings re-stamps cleanly. Was
        baked at construction time; _reapply_accent calls this on
        theme_changed to refresh."""
        from modules.ui_helpers import ACCENT as _ACCENT

        return f"""
            QPushButton {{
                background: {_ACCENT};
                color: white;
                border: none;
                border-radius: 8px;
                {type_qss(TYPE_BODY)}
                font-weight: 600;
            }}
            QPushButton:hover {{ background: {_ACCENT}; opacity: 0.92; }}
            QPushButton:pressed {{ background: {_ACCENT}; }}
            QPushButton:disabled {{
                background: rgba(255,255,255,0.10);
                color: rgba(255,255,255,0.50);
            }}
        """

    def _reapply_accent(self):
        """Re-stamp every surface in this view whose stylesheet baked
        the accent at construction. Wired to PlayerBus.theme_changed."""
        from modules.ui_helpers import ACCENT as _ACCENT
        from modules.theme import _hex_to_rgb as _h2r

        try:
            _ar, _ag, _ab = _h2r(_ACCENT)
        except Exception:
            return
        # Submit button — solid accent fill.
        if hasattr(self, "_submit_btn"):
            self._submit_btn.setStyleSheet(self._submit_btn_qss())
        # Combo focus/hover borders + popup hover/selected tints +
        # delegate accent triplet. The chevron icon stays as a hex
        # gray (#c8c8c8) so it doesn't need restamping.
        if hasattr(self, "_kind_combo"):
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
                    border-color: rgba({_ar},{_ag},{_ab},0.65);
                    background: rgba(255,255,255,0.08);
                    outline: none;
                }}
                QComboBox:hover {{
                    border-color: rgba({_ar},{_ag},{_ab},0.40);
                }}
            """)
            # Re-install the custom item delegate with the new
            # accent triplet so popup item highlights track too.
            try:
                self._kind_combo.setItemDelegate(
                    _AccentItemDelegate(
                        (_ar, _ag, _ab),
                        self._kind_combo,
                    ),
                )
            except Exception:
                pass

    def _build_field(
        self, label: str, placeholder: str, initial: str = "", password: bool = False
    ) -> QLineEdit:
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
            self.provider.probe,
            server,
            on_result=lambda info: self._on_probe_ok(
                server,
                username,
                password,
                info,
            ),
            on_error=lambda e: self._on_probe_err(e),
        )

    def _on_kind_changed(self, _idx: int):
        kind = self._kind_combo.currentData() or "jellyfin"
        if kind == "subsonic":
            self._subtitle.setText("Sign in to your Subsonic / Navidrome server")
            self._server_field.setPlaceholderText("http://your.server:4533")
        else:
            self._subtitle.setText("Sign in to your Jellyfin server")
            self._server_field.setPlaceholderText("http://your.server:8096")

    def _on_probe_ok(self, server: str, username: str, password: str, info):
        if info is None:
            self._set_submitting(False)
            self._show_error(
                "That URL responded but doesn't look like a "
                f"{self.provider.kind.capitalize()} server."
            )
            return
        # URL probe succeeded. Authenticate. The product_name from the
        # probe (e.g. "Navidrome") is threaded through so _on_auth_ok
        # can decide whether to run the server-side scrobble detection
        # — we have the password in scope here, but not after auth.
        product_name = (getattr(info, "product_name", "") or "").strip()
        run_async(
            self.provider.authenticate,
            server,
            username,
            password,
            on_result=lambda _result: self._on_auth_ok(
                server,
                username,
                password,
                product_name,
            ),
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

    def _on_auth_ok(
        self, server: str = "", username: str = "", password: str = "", product_name: str = ""
    ):
        self._set_submitting(False)
        # Clear the password field on success so it doesn't sit in the
        # form if the user signs out and lands here again.
        self._password_field.clear()
        # Server-side scrobble detection — only on Subsonic logins, and
        # only when ping reported a Navidrome server. Non-Navidrome
        # Subsonic-compatible servers (Airsonic, Gonic, etc.) don't
        # expose the native API we'd need, so we just clear the flags
        # so a stale "server is scrobbling" banner from an older login
        # to a different server doesn't carry over.
        self._sync_server_scrobble_flags(server, username, password, product_name)
        self.signed_in.emit()

    def _sync_server_scrobble_flags(
        self, server: str, username: str, password: str, product_name: str
    ):
        """Probe Navidrome's native API for per-user scrobble linkage
        and set ``settings.server_scrobbles_*`` accordingly. Best effort
        — any failure leaves us at "couldn't tell" and the settings
        page falls back to the warning banner.

        On every Subsonic login we first reset the flags so a sign-in
        to a different server doesn't inherit the previous server's
        state. Then if the new server looks like Navidrome we fire the
        detector. Non-Navidrome servers exit after the reset.
        """
        if self.provider.kind != "subsonic":
            self._settings.server_is_navidrome = False
            self._settings.server_scrobbles_lastfm = False
            self._settings.server_scrobbles_listenbrainz = False
            self._settings.server_scrobble_check_done = False
            return
        is_navidrome = "navidrome" in (product_name or "").lower()
        self._settings.server_is_navidrome = is_navidrome
        self._settings.server_scrobbles_lastfm = False
        self._settings.server_scrobbles_listenbrainz = False
        self._settings.server_scrobble_check_done = False
        if not is_navidrome or not server or not username or not password:
            return
        from modules.scrobble.navidrome_detect import detect

        def _on_detect(result):
            # ``result`` is a navidrome_detect.Result.
            if not result or not result.detected:
                # Couldn't read the user record — leave flags False and
                # let the settings UI surface the warning banner.
                return
            self._settings.server_scrobble_check_done = True
            self._settings.server_scrobbles_lastfm = bool(result.server_lastfm)
            self._settings.server_scrobbles_listenbrainz = bool(result.server_listenbrainz)
            # Auto-disable in-app scrobblers when the server is
            # already covering them — the whole point of the detect.
            if result.server_lastfm:
                self._settings.lastfm_enabled = False
            if result.server_listenbrainz:
                self._settings.listenbrainz_enabled = False

        run_async(
            detect,
            server,
            username,
            password,
            on_result=_on_detect,
            on_error=lambda _e: None,
        )

    def _on_auth_err(self, err: Exception):
        self._set_submitting(False)
        msg = str(err) or err.__class__.__name__
        unauthorized = "401" in msg or "Unauthorized" in msg
        # Common case: HTTPError 401 from a wrong password. The full
        # error string from requests is verbose; surface a friendly
        # message and tuck the technical detail underneath.
        if unauthorized:
            msg = "Wrong username or password."
        elif "404" in msg or "Not Found" in msg:
            msg = "Server not found at that URL."
        elif "Connection" in msg or "Max retries" in msg:
            msg = "Couldn't reach the server. Check the URL and your network."
        self._show_error(msg)
        # On a 401 the user almost certainly mistyped the password;
        # clear it + return focus + select-all so the next keystroke
        # replaces the bad input without a tab.
        if unauthorized:
            self._password_field.clear()
            self._password_field.setFocus(Qt.FocusReason.OtherFocusReason)

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
