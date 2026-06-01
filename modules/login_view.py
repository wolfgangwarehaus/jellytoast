"""Native sign-in surface — boot-time authentication entry point.

A centered card with server URL / username / password fields and a
Sign In button. On submit it calls the active provider's
``authenticate`` via ``run_async`` (the GUI thread can't block on a
10s POST timeout) and emits ``signed_in`` on success. The host shows
this view in the content stack whenever the API isn't authenticated;
on success the host swaps to the user's home destination."""

from PySide6.QtCore import Qt, Signal, Slot
from PySide6.QtGui import QKeyEvent, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from modules.async_io import run_async
from modules.design_tokens import (
    RADIUS_WINDOW,
    SPACE_LG,
    SPACE_MD,
    SPACE_SM,
    SPACE_XS,
    TYPE_BODY,
    TYPE_CAPTION,
    TYPE_DISPLAY,
    type_qss,
)
from modules.providers import get_provider, reset_provider
from modules.selector import Selector, selector_qss
from modules.settings import get_settings
from modules.ui_helpers import (
    ACCENT,
    BG,
    BORDER,
    ERROR_FG,
    TEXT,
    TEXT_DIM,
    TEXT_FAINT,
    ink_alpha,
)

CARD_WIDTH = 420


class _LoginCard(QFrame):
    """Login card whose body is painted in the same style the
    settings dialog uses for its window body: a rounded rect filled
    with the active theme's ``popup_paint_qcolor`` token (translucent
    on frosted themes, opaque on solid). No 1-px border — the border
    on top of a translucent fill reads as a hard chip against the
    blurred backdrop; the lifted fill alone is the separation. Same
    ``RADIUS_WINDOW`` corner radius the settings dialog uses, so the
    two surfaces feel like peers of one design language.

    Repaints itself on ``PlayerBus.theme_changed`` so an accent /
    theme swap re-pulls ``popup_paint_qcolor()`` and the card retints
    without rebuilding the LoginView.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        # WA_StyledBackground off so Qt's style engine doesn't paint
        # the QFrame's default background under us — we own the
        # whole surface.
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, False)
        self.setFrameShape(QFrame.Shape.NoFrame)
        try:
            from modules.player_state import PlayerBus

            PlayerBus.get().theme_changed.connect(self.update)
        except Exception:
            pass

    def paintEvent(self, _e):  # noqa: N802 — Qt naming
        from modules.ui_helpers import popup_paint_qcolor

        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            path = QPainterPath()
            path.addRoundedRect(
                0.0,
                0.0,
                float(self.width()),
                float(self.height()),
                RADIUS_WINDOW,
                RADIUS_WINDOW,
            )
            p.setBrush(popup_paint_qcolor())
            p.setPen(Qt.PenStyle.NoPen)
            p.drawPath(path)
        finally:
            p.end()


# Shared line-edit QSS for the alternate-URL dialog rows — a trimmed
# version of LoginView._build_field's style (no focus/submit wiring).
_DIALOG_FIELD_QSS = f"""
    QLineEdit {{
        background: {ink_alpha(0.06)};
        color: {TEXT};
        border: 1px solid {BORDER};
        border-radius: 6px;
        padding: 7px 10px;
        {type_qss(TYPE_CAPTION)}
    }}
    QLineEdit:focus {{ border-color: {ink_alpha(0.32)}; }}
"""


class _UrlRow(QWidget):
    """One alternate-URL entry — a URL field, an optional label field,
    and a remove button. Owned by ``_AlternateUrlsDialog``."""

    def __init__(self, on_remove, url: str = "", label: str = "", parent=None):
        super().__init__(parent)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(SPACE_XS)

        self.url_edit = QLineEdit(url)
        self.url_edit.setPlaceholderText("http://alt.address:8096")
        self.url_edit.setStyleSheet(_DIALOG_FIELD_QSS)
        self.label_edit = QLineEdit(label)
        self.label_edit.setPlaceholderText("Label (optional)")
        self.label_edit.setMaximumWidth(150)
        self.label_edit.setStyleSheet(_DIALOG_FIELD_QSS)

        remove = QPushButton("×")
        remove.setFixedSize(28, 28)
        remove.setCursor(Qt.CursorShape.PointingHandCursor)
        remove.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; "
            f"color: {TEXT_DIM}; font-size: 18px; }} "
            f"QPushButton:hover {{ color: {TEXT}; }}"
        )
        remove.clicked.connect(lambda: on_remove(self))

        row.addWidget(self.url_edit, 1)
        row.addWidget(self.label_edit)
        row.addWidget(remove)


class _AlternateUrlsDialog(QDialog):
    """Manage ``settings.server_hostnames`` — alternate addresses for
    the *same* account that the connectivity engine fails over to (in
    list order) when the primary URL goes unreachable."""

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self._settings = settings
        self.setWindowTitle("Alternate server URLs")
        self.setModal(True)
        self.setMinimumWidth(480)
        self.setStyleSheet(f"QDialog {{ background: {BG}; }}")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(SPACE_LG, SPACE_LG, SPACE_LG, SPACE_LG)
        outer.setSpacing(SPACE_SM)

        intro = QLabel(
            "Extra addresses for the same account — handy for a Tailscale "
            "name vs. a LAN IP. jellytoast probes these in order when the "
            "main Server URL stops responding, and switches back when it "
            "recovers."
        )
        intro.setWordWrap(True)
        intro.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}")
        outer.addWidget(intro)
        outer.addSpacing(SPACE_XS)

        self._rows_box = QVBoxLayout()
        self._rows_box.setSpacing(SPACE_XS)
        outer.addLayout(self._rows_box)
        self._rows: list[_UrlRow] = []

        add_btn = QPushButton("+ Add URL")
        add_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        add_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; "
            f"color: {ACCENT}; {type_qss(TYPE_CAPTION)} text-align: left; }} "
            f"QPushButton:hover {{ color: {TEXT}; }}"
        )
        add_btn.clicked.connect(lambda: self._add_row())
        outer.addWidget(add_btn, 0, Qt.AlignmentFlag.AlignLeft)
        outer.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

        for entry in self._settings.server_hostnames:
            self._add_row(entry.get("url", ""), entry.get("label", ""))

    def _add_row(self, url: str = "", label: str = "") -> None:
        row = _UrlRow(self._remove_row, url, label)
        self._rows.append(row)
        self._rows_box.addWidget(row)

    def _remove_row(self, row: _UrlRow) -> None:
        if row in self._rows:
            self._rows.remove(row)
            self._rows_box.removeWidget(row)
            row.deleteLater()

    def _on_accept(self) -> None:
        # Priority = list position; the connectivity engine walks the
        # list in this order. Blank URL rows are dropped. The settings
        # setter re-validates / normalizes, so this just gathers.
        cleaned = []
        for row in self._rows:
            url = row.url_edit.text().strip()
            if not url:
                continue
            cleaned.append(
                {
                    "url": url,
                    "label": row.label_edit.text().strip(),
                    "priority": len(cleaned),
                }
            )
        self._settings.server_hostnames = cleaned
        self.accept()


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
        self._refresh_loginview_qss()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.addStretch(1)

        # Center column: card flanked by horizontal stretches so it
        # stays centered regardless of window width.
        center_row = QHBoxLayout()
        center_row.addStretch(1)

        self._card = _LoginCard()
        self._card.setObjectName("loginCard")
        self._card.setFixedWidth(CARD_WIDTH)
        # Background + corner radius now live on _LoginCard.paintEvent
        # so the card matches the settings dialog body byte-for-byte.

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
        # Selector (modules/selector.py) is the same dropdown surface
        # the settings dialog uses — self-styling QPushButton + QMenu
        # popup, accent-aware, theme_changed-aware. Drops in 100 lines
        # of QSS + QStyledItemDelegate + popup-view restyling that the
        # old QComboBox path needed to escape KDE Breeze's hard-coded
        # selection colour. See docs (or settings_dialog history) for
        # the QComboBox rabbit hole.
        self._kind_combo = Selector()
        self._kind_combo.addItem("Jellyfin", "jellyfin")
        self._kind_combo.addItem("Subsonic / Navidrome", "subsonic")
        saved_kind = (self._settings.provider_kind or "jellyfin").lower()
        idx = self._kind_combo.findData(saved_kind)
        if idx >= 0:
            self._kind_combo.setCurrentIndex(idx)
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

        def _add_field(label: str, field: QWidget) -> None:
            cap = QLabel(label.upper())
            cap.setStyleSheet(
                f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)} letter-spacing: 0.6px;"
            )
            card_layout.addWidget(cap)
            card_layout.addWidget(field)
            card_layout.addSpacing(SPACE_XS)

        _add_field("Server URL", self._server_field)

        # Alternate-URL affordance — sits right under Server URL since
        # it's the same concept (where to reach the server). Opens the
        # failover-list manager; the label reflects how many are set.
        self._alt_urls_btn = QPushButton(self._alt_urls_label())
        self._alt_urls_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._alt_urls_btn.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; "
            f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)} text-align: left; }} "
            f"QPushButton:hover {{ color: {ACCENT}; }}"
        )
        self._alt_urls_btn.clicked.connect(self._open_alt_urls)
        card_layout.addWidget(self._alt_urls_btn, 0, Qt.AlignmentFlag.AlignLeft)
        card_layout.addSpacing(SPACE_XS)

        _add_field("Username", self._username_field)
        _add_field("Password", self._password_field)

        # Error message — hidden until a sign-in attempt fails.
        self._error_label = QLabel("")
        self._error_label.setStyleSheet(f"color: {ERROR_FG}; {type_qss(TYPE_CAPTION)}")
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
                background: {ink_alpha(0.10)};
                color: {ink_alpha(0.50)};
            }}
        """

    def _refresh_loginview_qss(self) -> None:
        """LoginView-level stylesheet: the descendant transparency
        sweep (so the card sits cleanly over the body color) PLUS
        the Selector rule for ``#jtSelector``. Both live here so the
        specificity of the Selector rule matches the transparency
        sweep — passing ``"QWidget#loginView"`` as the host selector
        to ``selector_qss`` gives the Selector rule the same parent-
        id weight so its ``background: ink_alpha(0.06)`` wins over
        the transparency sweep. Without that match, the descendant
        rule beats the bare ``QPushButton#jtSelector`` selector and
        leaves the dropdown see-through.

        Called on init and re-called on ``PlayerBus.theme_changed``
        so accent / theme swaps re-stamp the Selector's border tint
        + chevron colour live."""
        self.setStyleSheet(
            """
            QWidget#loginView,
            QWidget#loginView QWidget {
                background: transparent;
            }
            """
            + selector_qss("QWidget#loginView")
        )

    def _reapply_accent(self):
        """Re-stamp every surface in this view whose stylesheet baked
        the accent at construction. Wired to PlayerBus.theme_changed.

        The login card repaints itself via paintEvent reading the live
        ``popup_paint_qcolor`` token. The Selector rule lives in
        LoginView's parent stylesheet (see ``_refresh_loginview_qss``)
        so theme changes re-stamp it from here. The Sign in button
        bakes the accent into its own widget-level QSS."""
        self._refresh_loginview_qss()
        if hasattr(self, "_submit_btn"):
            self._submit_btn.setStyleSheet(self._submit_btn_qss())

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
                background: {ink_alpha(0.06)};
                color: {TEXT};
                border: 1px solid {BORDER};
                border-radius: 8px;
                padding: 10px 14px;
                {type_qss(TYPE_BODY)}
                selection-background-color: {ink_alpha(0.20)};
            }}
            QLineEdit:focus {{
                border-color: {ink_alpha(0.32)};
                background: {ink_alpha(0.08)};
            }}
        """)
        # Submit on Return from any field — the typical "fill out the
        # form, hit Enter" UX.
        edit.returnPressed.connect(self._submit)
        return edit

    def _alt_urls_label(self) -> str:
        """Affordance text — a count when alternates exist, an invite
        to add one when the list is empty."""
        count = len(self._settings.server_hostnames)
        return f"Alternate URLs · {count}" if count else "+ Add alternate URL"

    def _open_alt_urls(self):
        dlg = _AlternateUrlsDialog(self._settings, self)
        dlg.exec()
        self._alt_urls_btn.setText(self._alt_urls_label())

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
        """Record whether the server looks like Navidrome (for the badge
        wording) and re-run double-scrobble detection.

        The detection no longer reads the server config — Navidrome simply
        doesn't expose per-user scrobble linkage (its ``/api/user/{id}``
        record carries no LB/Last.fm field, and the Subsonic API has nothing
        either). Instead it inspects the user's own recent ListenBrainz
        listens for a non-jellytoast ``submission_client`` (the server stamps
        its own name). Best-effort + async; see
        ``modules.scrobble.refresh_server_scrobble_flags`` /
        ``server_scrobble_detect``. The manager gates its in-app scrobbling on
        the resulting ``server_scrobbles_listenbrainz`` flag, so no auto-flip
        of ``listenbrainz_enabled`` here — the user's intent stays intact and
        the override (``scrobble_in_app_anyway``) can re-enable it."""
        self._settings.server_is_navidrome = "navidrome" in (product_name or "").lower()
        from modules.scrobble import refresh_server_scrobble_flags

        refresh_server_scrobble_flags()

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
