"""Settings dialog — the Scrobbling (ListenBrainz, Last.fm) page.

Split out of ``settings_dialog.py``: a mixin of ``SettingsDialog`` (it uses
the dialog's widgets, settings handle and shared helpers via ``self``).
Strings use ``QCoreApplication.translate("SettingsDialog", …)`` rather than
``self.tr`` so they keep the SettingsDialog translation context — lupdate
files a mixin's ``self.tr`` under the MIXIN's name, which never matches at
runtime.
"""

from PySide6.QtCore import QCoreApplication, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from jellytoast.design_tokens import (
    TYPE_BODY,
    TYPE_CAPTION,
    rad,
    type_qss,
)
from jellytoast.theme import _hex_to_rgb
from jellytoast.ui_helpers import (
    TEXT,
    TEXT_DIM,
    TEXT_FAINT,
    ink_alpha,
)


class _ScrobblingPageMixin:
    def _restamp_scrobble_banner(self) -> None:
        """(Re)apply the double-scrobble banner's stylesheet. The
        confirmatory ("your server is scrobbling for you") state is
        accent-tinted; the other two states use a neutral ink wash. The
        accent variant reads the LIVE accent so a swatch swap retints it —
        called both at build time and from ``_reapply_accent``. No-op
        before the banner exists."""
        warning = getattr(self, "_scrobble_banner", None)
        if warning is None:
            return
        if getattr(self, "_scrobble_banner_accent", False):
            from jellytoast.ui_helpers import ACCENT as _ACCENT_NOW

            ar, ag, ab = _hex_to_rgb(_ACCENT_NOW)
            border = f"rgba({ar},{ag},{ab},0.30)"
            bg = f"rgba({ar},{ag},{ab},0.08)"
        else:
            border = ink_alpha(0.12)
            bg = ink_alpha(0.03)
        warning.setStyleSheet(
            f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}"
            f"padding: 10px 12px; border: 1px solid {border};"
            f"border-radius: {rad(8)}px; background: {bg};"
        )

    # ── Page: Scrobbling ────────────────────────────────────────────────
    # ListenBrainz is fully live (token validate, server-scrobble
    # detection, offline queue). The Last.fm section is built but hidden
    # until an API key ships (lastfm.is_configured() gates it).
    def _build_scrobbling(self) -> QWidget:
        from jellytoast.scrobble import lastfm as _lastfm

        page = QWidget()
        page.setStyleSheet("background: transparent;")
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(14)

        # ── Double-scrobble guidance / detection banner ──────────────
        # Three states drive the banner copy + style:
        # 1. Server-side scrobbling DETECTED for at least one service:
        #    confirmatory tone — "your server is scrobbling for you,
        #    in-app is off". Per-service enable checkboxes are also
        #    disabled below to make the chosen state obvious.
        # 2. Server is Navidrome but detection hasn't confirmed a second
        #    scrobbler yet (no recent foreign listens, or LB unreachable):
        #    strong reminder phrased toward Navidrome specifically.
        # 3. Anything else: generic guidance — same tone as the original
        #    warning, just covering the manual-config case.
        srv_lf = self.s.server_scrobbles_lastfm
        srv_lb = self.s.server_scrobbles_listenbrainz
        is_navidrome = self.s.server_is_navidrome
        check_done = self.s.server_scrobble_check_done
        if srv_lf or srv_lb:
            services = []
            if srv_lf:
                services.append("Last.fm")
            if srv_lb:
                services.append("ListenBrainz")
            joined = QCoreApplication.translate("SettingsDialog", " and ").join(services)
            warning_text = QCoreApplication.translate(
                "SettingsDialog",
                "Your server is scrobbling {0} for you — "
                "the in-app option for that service is turned off so "
                "tracks aren't counted twice.",
            ).format(joined)
            # Confirmatory state → accent-tinted. Derived from the LIVE
            # accent (not the old hardcoded purple) + re-stamped on accent
            # change so it follows a swatch swap like every other tinted
            # surface.
            self._scrobble_banner_accent = True
        elif is_navidrome and not check_done:
            warning_text = QCoreApplication.translate(
                "SettingsDialog",
                "This is a Navidrome server. If you've already enabled "
                "Last.fm or ListenBrainz scrobbling there, leave these "
                "off — otherwise every track is counted twice.",
            )
            self._scrobble_banner_accent = False
        else:
            warning_text = QCoreApplication.translate(
                "SettingsDialog",
                "If your music server already scrobbles "
                "(e.g. Navidrome's built-in Last.fm / ListenBrainz "
                "integration), leave these off — otherwise every "
                "track is counted twice.",
            )
            self._scrobble_banner_accent = False
        warning = QLabel(warning_text)
        warning.setWordWrap(True)
        self._scrobble_banner = warning
        self._restamp_scrobble_banner()
        v.addWidget(warning)

        # ── Re-check button ───────────────────────────────────────────
        # Detection works by inspecting the submission_client of the user's
        # recent ListenBrainz listens (jellytoast stamps "jellytoast"; the
        # server stamps its own name). It runs on login + app start, but a
        # button lets the user re-run it on demand — e.g. right after they
        # set up server-side scrobbling, before there are enough foreign
        # listens for the boot check to catch.
        recheck_row = QHBoxLayout()
        recheck_row.setSpacing(8)
        self._scrobble_recheck_btn = QPushButton(
            QCoreApplication.translate("SettingsDialog", "Re-check server scrobbling")
        )
        self._scrobble_recheck_btn.setObjectName("ghost")
        self._scrobble_recheck_status = QLabel("")
        self._scrobble_recheck_status.setWordWrap(True)
        self._scrobble_recheck_status.setStyleSheet(
            f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)}"
        )

        def _scrobble_recheck():
            from jellytoast.scrobble import refresh_server_scrobble_flags

            self._scrobble_recheck_btn.setEnabled(False)
            self._scrobble_recheck_status.setText(
                QCoreApplication.translate(
                    "SettingsDialog", "Checking your recent ListenBrainz listens…"
                )
            )

            def _done(res):
                try:
                    self._scrobble_recheck_btn.setEnabled(True)
                    if not getattr(res, "checked", False):
                        self._scrobble_recheck_status.setText(
                            QCoreApplication.translate(
                                "SettingsDialog",
                                "Couldn't check — ListenBrainz isn't configured, "
                                "or the server was unreachable.",
                            )
                        )
                    elif getattr(res, "server_scrobbles", False):
                        who = ", ".join(res.foreign_clients) or QCoreApplication.translate(
                            "SettingsDialog", "another client"
                        )
                        self._scrobble_recheck_status.setText(
                            QCoreApplication.translate(
                                "SettingsDialog",
                                "Detected another scrobbler ({0}) on your account "
                                "— in-app ListenBrainz paused to avoid duplicates. "
                                "Reopen this page to refresh the controls.",
                            ).format(who)
                        )
                    else:
                        self._scrobble_recheck_status.setText(
                            QCoreApplication.translate(
                                "SettingsDialog",
                                "No second scrobbler found in your recent listens.",
                            )
                        )
                except RuntimeError:
                    pass  # dialog closed before the probe returned

            refresh_server_scrobble_flags(on_done=_done)

        self._scrobble_recheck_btn.clicked.connect(_scrobble_recheck)
        recheck_row.addWidget(self._scrobble_recheck_btn)
        recheck_row.addWidget(self._scrobble_recheck_status, 1)
        v.addLayout(recheck_row)

        # ── ListenBrainz ──────────────────────────────────────────────
        v.addWidget(self._section_header("ListenBrainz"))

        self._lb_enabled = QCheckBox(
            QCoreApplication.translate("SettingsDialog", "Enable ListenBrainz scrobbling")
        )
        self._lb_enabled.setChecked(self.s.listenbrainz_enabled)
        self._lb_enabled.toggled.connect(lambda v: setattr(self.s, "listenbrainz_enabled", v))
        v.addWidget(self._lb_enabled)
        if srv_lb:
            # Lock the in-app toggle off when a second scrobbler is
            # detected. Tooltip surfaces the *why* on hover.
            self._lb_enabled.setEnabled(False)
            self._lb_enabled.setToolTip(
                QCoreApplication.translate(
                    "SettingsDialog",
                    "A second scrobbler (your server) is already submitting "
                    "these listens to ListenBrainz. In-app scrobbling is paused "
                    "to avoid duplicates.",
                )
            )
            # Escape hatch: if the detected 'other' scrobbler is actually a
            # different app (not this server), the user can force jellytoast
            # to scrobble anyway (accepting duplicates).
            self._lb_override = QCheckBox(
                QCoreApplication.translate("SettingsDialog", "Scrobble from jellytoast anyway")
            )
            self._lb_override.setChecked(self.s.scrobble_in_app_anyway)
            self._lb_override.setToolTip(
                QCoreApplication.translate(
                    "SettingsDialog",
                    "Submit listens from jellytoast even though another scrobbler "
                    "was detected on your ListenBrainz account. Only use this if "
                    "that other scrobbler isn't your music server — otherwise "
                    "every track is counted twice.",
                )
            )
            self._lb_override.toggled.connect(
                lambda val: setattr(self.s, "scrobble_in_app_anyway", val)
            )
            v.addWidget(self._lb_override)

        token_row = QHBoxLayout()
        token_row.setSpacing(8)
        token_row.addWidget(
            self._field_label(QCoreApplication.translate("SettingsDialog", "User token"))
        )
        self._lb_token_edit = QLineEdit()
        self._lb_token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._lb_token_edit.setText(self.s.listenbrainz_token)
        self._lb_token_edit.setPlaceholderText(
            QCoreApplication.translate("SettingsDialog", "Paste from listenbrainz.org/profile/")
        )
        self._lb_token_edit.editingFinished.connect(self._on_lb_token_changed)
        # Cap the token + URL line edits at a sensible width — at full
        # row stretch (the previous setup) they read absurdly wide
        # for the short string they hold. A trailing stretch on the
        # row pushes the Validate button left of the page's right
        # edge to match.
        self._lb_token_edit.setMaximumWidth(260)
        token_row.addWidget(self._lb_token_edit)
        self._lb_validate_btn = QPushButton(
            QCoreApplication.translate("SettingsDialog", "Validate")
        )
        self._lb_validate_btn.setObjectName("ghost")
        self._lb_validate_btn.clicked.connect(self._on_lb_validate)
        token_row.addWidget(self._lb_validate_btn)
        token_row.addStretch(1)
        v.addLayout(token_row)

        # Status line: "Connected as <name>" or "Not validated yet".
        self._lb_status = QLabel(self._lb_status_text())
        self._lb_status.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)}")
        v.addWidget(self._lb_status)

        url_row = QHBoxLayout()
        url_row.setSpacing(8)
        url_row.addWidget(
            self._field_label(QCoreApplication.translate("SettingsDialog", "Server URL"))
        )
        self._lb_url_edit = QLineEdit()
        self._lb_url_edit.setText(self.s.listenbrainz_url)
        self._lb_url_edit.setPlaceholderText("https://api.listenbrainz.org")
        self._lb_url_edit.editingFinished.connect(
            lambda: setattr(
                self.s,
                "listenbrainz_url",
                self._lb_url_edit.text().strip() or "https://api.listenbrainz.org",
            )
        )
        self._lb_url_edit.setMaximumWidth(260)
        url_row.addWidget(self._lb_url_edit)
        url_row.addStretch(1)
        v.addLayout(url_row)

        # ── Last.fm ───────────────────────────────────────────────────
        # Deferred 2026-05-20 — the in-app API credentials aren't
        # registered (Last.fm's signup WAF kept blocking it), so the
        # whole section stays hidden rather than showing an apologetic
        # "coming soon" placeholder. ``is_configured()`` is False while
        # the API key is empty; populate the key to bring it back.
        # ListenBrainz above is the supported scrobbling path.
        if _lastfm.is_configured():
            v.addWidget(self._section_header("Last.fm"))
            self._lf_enabled = QCheckBox(
                QCoreApplication.translate("SettingsDialog", "Enable Last.fm scrobbling")
            )
            self._lf_enabled.setChecked(self.s.lastfm_enabled)
            self._lf_enabled.toggled.connect(lambda v: setattr(self.s, "lastfm_enabled", v))
            v.addWidget(self._lf_enabled)
            if srv_lf:
                self._lf_enabled.setEnabled(False)
                self._lf_enabled.setToolTip(
                    QCoreApplication.translate(
                        "SettingsDialog",
                        "Your Navidrome server is already scrobbling to "
                        "Last.fm. Disable it there to use jellytoast's "
                        "Last.fm client instead.",
                    )
                )

            lf_row = QHBoxLayout()
            lf_row.setSpacing(8)
            self._lf_status = QLabel(self._lf_status_text())
            self._lf_status.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)}")
            lf_row.addWidget(self._lf_status, 1)
            if self.s.lastfm_session_key:
                self._lf_action_btn = QPushButton(
                    QCoreApplication.translate("SettingsDialog", "Disconnect")
                )
                self._lf_action_btn.setObjectName("ghost")
                self._lf_action_btn.clicked.connect(self._on_lf_disconnect)
            else:
                self._lf_action_btn = QPushButton(
                    QCoreApplication.translate("SettingsDialog", "Connect to Last.fm…")
                )
                self._lf_action_btn.setObjectName("ghost")
                self._lf_action_btn.clicked.connect(self._on_lf_connect)
            lf_row.addWidget(self._lf_action_btn)
            v.addLayout(lf_row)

        v.addStretch(1)
        return page

    def _lb_status_text(self) -> str:
        name = self.s.listenbrainz_username
        if name:
            return QCoreApplication.translate("SettingsDialog", "Connected as {0}.").format(name)
        if self.s.listenbrainz_token:
            return QCoreApplication.translate(
                "SettingsDialog", "Token saved — click Validate to confirm."
            )
        return QCoreApplication.translate("SettingsDialog", "Not validated yet.")

    def _lf_status_text(self) -> str:
        name = self.s.lastfm_username
        if name:
            return QCoreApplication.translate("SettingsDialog", "Connected as {0}.").format(name)
        return QCoreApplication.translate("SettingsDialog", "Not connected.")

    def _on_lb_token_changed(self):
        new_token = (self._lb_token_edit.text() or "").strip()
        if new_token == self.s.listenbrainz_token:
            return
        self.s.listenbrainz_token = new_token
        # Username on file no longer matches the new token — clear it
        # so the status line goes back to "Token saved — click Validate".
        self.s.listenbrainz_username = ""
        self._lb_status.setText(self._lb_status_text())

    def _on_lb_validate(self):
        from jellytoast.async_io import run_async
        from jellytoast.scrobble import listenbrainz as _lb

        token = (self._lb_token_edit.text() or "").strip()
        url = (self._lb_url_edit.text() or "").strip() or _lb.DEFAULT_BASE_URL
        if not token:
            self._lb_status.setText(
                QCoreApplication.translate("SettingsDialog", "Enter a token, then click Validate.")
            )
            return
        # Persist whatever the user typed so a successful validate can
        # be acted on immediately by the manager.
        self.s.listenbrainz_token = token
        self.s.listenbrainz_url = url
        self._lb_status.setText(QCoreApplication.translate("SettingsDialog", "Validating…"))
        self._lb_validate_btn.setEnabled(False)
        run_async(
            _lb.validate_token,
            token,
            url,
            on_result=self._on_lb_validated,
            on_error=lambda _e: self._on_lb_validated(None),
        )

    def _on_lb_validated(self, username):
        self._lb_validate_btn.setEnabled(True)
        if username:
            self.s.listenbrainz_username = username
            self._lb_status.setText(
                QCoreApplication.translate("SettingsDialog", "Connected as {0}.").format(username)
            )
        else:
            self.s.listenbrainz_username = ""
            self._lb_status.setText(
                QCoreApplication.translate(
                    "SettingsDialog", "Couldn't validate — check the token and try again."
                )
            )

    def _on_lf_connect(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        from jellytoast.async_io import run_async
        from jellytoast.scrobble import lastfm as _lf

        # Step 1: get a request token. Step 2: open browser. Step 3:
        # poll auth.getSession every 3s until the user authorizes (or
        # cancels via the dialog's Close).
        self._lf_action_btn.setEnabled(False)
        self._lf_status.setText(QCoreApplication.translate("SettingsDialog", "Requesting token…"))

        def _on_token(token):
            if not token:
                self._lf_status.setText(
                    QCoreApplication.translate(
                        "SettingsDialog", "Couldn't reach Last.fm — try again later."
                    )
                )
                self._lf_action_btn.setEnabled(True)
                return
            QDesktopServices.openUrl(QUrl(_lf.auth_url(token)))
            self._lf_open_auth_modal(token)

        run_async(
            _lf.get_token,
            on_result=_on_token,
            on_error=lambda _e: _on_token(None),
        )

    def _lf_open_auth_modal(self, token: str):
        from jellytoast.async_io import run_async
        from jellytoast.scrobble import lastfm as _lf

        dlg = QDialog(self)
        dlg.setWindowTitle(QCoreApplication.translate("SettingsDialog", "Connect to Last.fm"))
        dlg.setFixedWidth(380)
        dlg.setModal(True)
        v = QVBoxLayout(dlg)
        v.setContentsMargins(20, 20, 20, 20)
        v.setSpacing(10)
        msg = QLabel(
            QCoreApplication.translate(
                "SettingsDialog",
                'We\'ve opened Last.fm in your browser. Click "Allow" '
                "there, then come back — jellytoast will detect the "
                "approval automatically.",
            )
        )
        msg.setWordWrap(True)
        msg.setStyleSheet(f"color: {TEXT}; {type_qss(TYPE_BODY)}")
        v.addWidget(msg)
        status = QLabel(QCoreApplication.translate("SettingsDialog", "Waiting for authorization…"))
        status.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)}")
        v.addWidget(status)
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel_btn = QPushButton(QCoreApplication.translate("SettingsDialog", "Cancel"))
        cancel_btn.setObjectName("ghost")
        cancel_btn.clicked.connect(dlg.reject)
        btn_row.addWidget(cancel_btn)
        v.addLayout(btn_row)

        # Poll auth.getSession every 3 seconds. Each tick spawns a
        # run_async; the dialog timer is what gets cancelled when the
        # user hits Cancel — pending workers just become no-ops by the
        # time their result lands (we re-check dlg.isVisible).
        timer = QTimer(dlg)
        timer.setInterval(3000)

        def _poll():
            if not dlg.isVisible():
                return

            def _on_session(sess):
                if not dlg.isVisible():
                    return
                if sess and sess.get("key"):
                    self.s.lastfm_session_key = sess["key"]
                    self.s.lastfm_username = sess.get("name", "")
                    self.s.lastfm_enabled = True
                    self._lf_action_btn.setText(
                        QCoreApplication.translate("SettingsDialog", "Disconnect")
                    )
                    try:
                        self._lf_action_btn.clicked.disconnect()
                    except Exception:
                        pass
                    self._lf_action_btn.clicked.connect(self._on_lf_disconnect)
                    self._lf_action_btn.setEnabled(True)
                    self._lf_status.setText(self._lf_status_text())
                    timer.stop()
                    dlg.accept()

            run_async(
                _lf.get_session,
                token,
                on_result=_on_session,
                on_error=lambda _e: None,
            )

        timer.timeout.connect(_poll)
        timer.start()
        dlg.exec()
        timer.stop()
        if not self.s.lastfm_session_key:
            self._lf_status.setText(
                QCoreApplication.translate("SettingsDialog", "Connection cancelled.")
            )
            self._lf_action_btn.setEnabled(True)

    def _on_lf_disconnect(self):
        self.s.lastfm_session_key = ""
        self.s.lastfm_username = ""
        self.s.lastfm_enabled = False
        self._lf_action_btn.setText(
            QCoreApplication.translate("SettingsDialog", "Connect to Last.fm…")
        )
        try:
            self._lf_action_btn.clicked.disconnect()
        except Exception:
            pass
        self._lf_action_btn.clicked.connect(self._on_lf_connect)
        self._lf_status.setText(self._lf_status_text())
