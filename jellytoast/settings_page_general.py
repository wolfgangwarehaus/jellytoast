"""Settings dialog — the General page.

Split out of ``settings_dialog.py``: a mixin of ``SettingsDialog`` (it uses
the dialog's widgets, settings handle and shared helpers via ``self``).
Strings use ``QCoreApplication.translate("SettingsDialog", …)`` rather than
``self.tr`` so they keep the SettingsDialog translation context — lupdate
files a mixin's ``self.tr`` under the MIXIN's name, which never matches at
runtime.
"""

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QSizePolicy,
    QSpacerItem,
    QVBoxLayout,
    QWidget,
)

from jellytoast import autostart as _autostart
from jellytoast.design_tokens import (
    TYPE_BODY,
    TYPE_CAPTION,
    rad,
    type_qss,
)
from jellytoast.keep_above import (
    install_mini_player_rule,
    remove_mini_player_rule,
)
from jellytoast.keep_above import (
    is_supported as keep_above_supported,
)
from jellytoast.player_state import PlayerBus
from jellytoast.selector import (
    Selector as _Selector,  # noqa: E402  (deliberate post-class import for layout)
)
from jellytoast.settings_widgets import (
    HOME_DESTINATIONS,
    WrapCheckBox,
)
from jellytoast.ui_helpers import (
    TEXT,
    TEXT_FAINT,
)


class _GeneralPageMixin:
    # ── Page: General ──────────────────────────────────────────────────
    def _build_general(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        # 12 px between groups — matches the rhythm we use on every
        # other settings page (Playback, Display, Hotkeys, Scrobbling)
        # so the dialog reads as one consistent surface.
        v.setSpacing(12)

        # ── Server (folded in from the old Account page) ───────────────
        # Most surveyed players (Supersonic, Apple Music) treat server
        # / account as a row in General rather than its own peer page.
        v.addWidget(self._section_header(QCoreApplication.translate("SettingsDialog", "SERVER")))
        username = self.s.username
        signed_in = bool(self.s.access_token)
        provider_kind = (self.s.provider_kind or "jellyfin").lower()
        provider_display = "Navidrome" if provider_kind == "subsonic" else "Jellyfin"
        if signed_in:
            if username:
                status_text = QCoreApplication.translate(
                    "SettingsDialog", "Signed in to {0} as {1}."
                ).format(provider_display, username)
            else:
                status_text = QCoreApplication.translate(
                    "SettingsDialog", "Signed in to {0}."
                ).format(provider_display)
        else:
            status_text = QCoreApplication.translate("SettingsDialog", "Not signed in.")
        status = QLabel(status_text)
        status.setStyleSheet(f"color: {TEXT}; {type_qss(TYPE_BODY)}")
        v.addWidget(status)

        # URL + connection status dot on the same row. The dot:
        # green = reachable, orange = degraded, red = unreachable,
        # gray = checking / not signed in. Hover for details.
        #
        # The URL is a single QLineEdit — readonly by default with
        # transparent border/background so it reads as text, switching
        # to the dialog's default chrome (1-px border + tinted fill)
        # when the user enters edit mode. One widget instead of a
        # label + hidden edit swap so the text doesn't jump size or
        # baseline when the box pops in. We used to bounce to a
        # QInputDialog popup, but a modal-over-modal hiding the very
        # field you're editing is worse than letting the row accept
        # text inline.
        url = self.s.server_url or QCoreApplication.translate("SettingsDialog", "Not configured")
        self._url_edit = QLineEdit(url)
        self._url_edit.setObjectName("serverUrlField")
        self._url_edit.setReadOnly(True)
        self._url_edit.setCursorPosition(0)
        self._url_edit.returnPressed.connect(self._commit_server_url_edit)
        # Size the field to its current text + chrome so the dot lands
        # at a fixed pixel position regardless of edit state — without
        # this, switching to setStyleSheet("") in edit mode lets Qt
        # re-compute sizeHint and the dot shifts. Use the line edit's
        # own font metrics (already inheriting the dialog font) so a
        # later theme change picks up the right size after we re-init.
        fm = self._url_edit.fontMetrics()
        # Dialog QSS at line ~3346: padding 6 12, border 1 — total
        # horizontal chrome is (12 + 12 + 1 + 1) = 26 px. Plus a
        # small 6-px slack: ``fm.horizontalAdvance`` returns the
        # advance width sum, which underestimates the actual
        # rendered run by a pixel or two on fonts with subpixel
        # anti-aliasing, and Qt's QLineEdit also reserves a tiny
        # cursor cell at end-of-text. Without the slack the final
        # digit of a tight URL gets clipped.
        url_chrome_px = 26
        text_px = fm.horizontalAdvance(url) if url else fm.horizontalAdvance("http://example.com")
        self._url_edit.setFixedWidth(text_px + url_chrome_px + 6)
        self._apply_url_edit_chrome(editing=False)

        self._conn_dot = QLabel()
        self._conn_dot.setFixedSize(8, 8)
        self._set_conn_dot_state("checking")

        url_row = QHBoxLayout()
        url_row.setContentsMargins(0, 0, 0, 0)
        url_row.setSpacing(0)
        url_row.addWidget(self._url_edit, 0, Qt.AlignmentFlag.AlignVCenter)
        # 6 px past the field's right border — the field itself
        # already carries 12 px of right padding inside the chrome,
        # so the total visual gap from the end of the URL text to
        # the dot is ~18 px (roughly two character widths) without
        # the dot looking glued to the border.
        self._url_dot_spacer = QSpacerItem(
            6,
            0,
            QSizePolicy.Policy.Fixed,
            QSizePolicy.Policy.Minimum,
        )
        url_row.addItem(self._url_dot_spacer)
        url_row.addWidget(self._conn_dot, 0, Qt.AlignmentFlag.AlignVCenter)
        # Trailing stretch consumes the rest of the row, pinning the
        # field + dot to the left so the dot's pixel position doesn't
        # drift with row width.
        url_row.addStretch(1)
        v.addLayout(url_row)

        if signed_in and self.s.server_url:
            from jellytoast.async_io import run_async
            from jellytoast.providers import get_provider as _gp

            _api = _gp()
            run_async(
                _api.probe,
                self.s.server_url,
                on_result=lambda info: self._on_connection_probe(info),
                on_error=lambda _e: self._on_connection_probe(None),
            )
        else:
            self._set_conn_dot_state("signed_out")

        # Explicit fixed height on the outlined controls — the dialog
        # QSS gives QComboBox a 22-px min-height that bumps it ~4 px
        # taller than a ghost QPushButton's natural size. Setting the
        # same fixedHeight on both buttons AND the Home page combo
        # below makes the three controls read as one row of matching
        # outlined chips. 36 px (was 34) — QComboBox's QStyleSheetStyle
        # renders its bottom border at a sub-pixel position when the
        # widget is sized to 34 (content + padding + border lands on
        # an odd half-pixel), leaving the bottom edge visibly thinner.
        # 36 gives the bottom border a clean integer row to land in;
        # buttons grow by 2 px too so the row stays visually matched.
        _CTRL_H = 36
        # Fixed width on the Change-server-URL ghost button so the
        # Home page combo below can match it exactly — without it the
        # button sized to its content while the combo had setFixedWidth,
        # and the two outlined chips read at different widths. Sized
        # snug to the text rather than wide-and-airy so the page reads
        # as a tidier column of controls.
        _CHANGE_BTN_W = 170
        # Fixed width on Sign out too — read by the URL row above to
        # land the connection dot at this column's horizontal centre,
        # so the dot reads as "the indicator for this server" lined up
        # over the action you'd take on that server.
        _SIGNOUT_BTN_W = 110
        _BTN_ROW_GAP = 8
        btn_row = QHBoxLayout()
        # Top margin trimmed from 8→4 — clawing back vertical budget
        # so the bottom Home-page combo isn't clipped on the
        # fixed-height (820×540) General page.
        btn_row.setContentsMargins(0, 4, 0, 0)
        btn_row.setSpacing(_BTN_ROW_GAP)
        self._change_btn = QPushButton(
            QCoreApplication.translate("SettingsDialog", "Change server URL…")
        )
        self._change_btn.setObjectName("ghost")
        self._change_btn.setFixedHeight(_CTRL_H)
        self._change_btn.setMinimumWidth(_CHANGE_BTN_W)
        self._change_btn.clicked.connect(self._on_change_server_clicked)
        btn_row.addWidget(self._change_btn)
        signout_btn = QPushButton(QCoreApplication.translate("SettingsDialog", "Sign out"))
        signout_btn.setObjectName("ghost")
        signout_btn.setFixedHeight(_CTRL_H)
        signout_btn.setMinimumWidth(_SIGNOUT_BTN_W)
        signout_btn.clicked.connect(self.sign_out_requested.emit)
        btn_row.addWidget(signout_btn)
        btn_row.addStretch(1)
        v.addLayout(btn_row)
        self._general_ctrl_h = _CTRL_H
        self._general_change_btn_w = _CHANGE_BTN_W

        # Section gaps below were 18 — trimmed to 12 to claw back
        # vertical budget on the fixed-height General page so the
        # Home-page combo at the bottom isn't clipped.
        v.addSpacing(12)

        # ── Startup ───────────────────────────────────────────────────
        # Boot-time behaviour — what happens when jellytoast launches.
        v.addWidget(self._section_header(QCoreApplication.translate("SettingsDialog", "STARTUP")))

        # Real state (the portal grant on Linux, else whether the
        # autostart .desktop file exists) wins over the persisted flag —
        # they can drift if the user nukes the entry from a file manager
        # or refuses the portal. Hidden where no backend can
        # fulfil the toggle (Windows/macOS) — a no-op checkbox reads
        # as a broken one.
        if _autostart.is_supported():
            self._autostart_check = WrapCheckBox(
                QCoreApplication.translate("SettingsDialog", "Launch jellytoast at login")
            )
            self._autostart_check.setChecked(_autostart.is_enabled())
            self._autostart_check.toggled.connect(self._on_autostart_toggled)
            v.addWidget(self._autostart_check)

        self._mini_check = WrapCheckBox(
            QCoreApplication.translate("SettingsDialog", "Show mini player on startup")
        )
        self._mini_check.setChecked(self.s.show_mini_on_start)
        self._mini_check.toggled.connect(lambda val: setattr(self.s, "show_mini_on_start", val))
        v.addWidget(self._mini_check)
        from jellytoast.platform_compat import is_mobile_shell

        # No mini player on a phone shell (see app._show_mini).
        self._mini_check.setVisible(not is_mobile_shell())

        v.addSpacing(12)

        # ── Window ────────────────────────────────────────────────────
        # Behaviour of the open windows themselves — stacking + close
        # semantics. Both toggles change WHERE jellytoast lives on the
        # desktop, not what it does at boot.
        window_header = self._section_header(QCoreApplication.translate("SettingsDialog", "WINDOW"))
        v.addWidget(window_header)

        # Wayland-only: xdg-shell forbids apps from setting their own
        # stacking, so Qt.WindowStaysOnTopHint is a no-op there. We
        # install a KWin window rule to do it compositor-side. The
        # toggle only appears on KDE Wayland — outside that the X11
        # hint already works and there's nothing for us to expose.
        # (On Windows / macOS this row will be replaced by the host-OS
        # equivalent when those backends land.)
        if keep_above_supported():
            self._keep_above_check = WrapCheckBox(
                QCoreApplication.translate("SettingsDialog", "Keep mini player on top")
            )
            self._keep_above_check.setChecked(self.s.mini_player_keep_above)
            self._keep_above_check.toggled.connect(self._on_keep_above_toggled)
            v.addWidget(self._keep_above_check)
            self._keep_above_check.setVisible(not is_mobile_shell())

        # Tray-on-close lives last — it's the most behavior-changing
        # toggle of the group (kills the close-X exit semantics) so it
        # gets the bottom slot where the eye naturally lands on a
        # "biggest commitment" row.
        self._tray_check = WrapCheckBox(
            QCoreApplication.translate(
                "SettingsDialog", "Hide to system tray when window is closed"
            )
        )
        self._tray_check.setChecked(self.s.minimize_to_tray)
        self._tray_check.toggled.connect(lambda val: setattr(self.s, "minimize_to_tray", val))
        v.addWidget(self._tray_check)
        # A mobile shell shows no tray; close keeps playing in the background
        # on its own there (see app._close_hides_window), so the toggle is moot.
        self._tray_check.setVisible(not is_mobile_shell())
        # Both WINDOW rows (mini on top, hide to tray) are desktop-only.
        window_header.setVisible(not is_mobile_shell())

        v.addSpacing(12)

        # ── Notifications ─────────────────────────────────────────────
        # Only offered where a notification backend is actually wired up
        # (notify-send on Linux, WinRT toasts on Windows) — hidden where
        # notify() would be a silent no-op anyway.
        from jellytoast import notifications as _notifications

        if _notifications.is_supported():
            v.addWidget(
                self._section_header(QCoreApplication.translate("SettingsDialog", "NOTIFICATIONS"))
            )
            self._track_notify_check = WrapCheckBox(
                QCoreApplication.translate(
                    "SettingsDialog", "Show a notification when the track changes"
                )
            )
            self._track_notify_check.setChecked(self.s.notify_on_track_change)
            self._track_notify_check.toggled.connect(
                lambda val: setattr(self.s, "notify_on_track_change", val)
            )
            v.addWidget(self._track_notify_check)

            v.addSpacing(12)

        # ── Home page ─────────────────────────────────────────────────
        # The view jellytoast lands on at launch. Section header alone
        # labels what the combo does, so no inline label needed.
        v.addWidget(self._section_header(QCoreApplication.translate("SettingsDialog", "HOME PAGE")))

        self._home_combo = _Selector()
        for label, key in HOME_DESTINATIONS:
            self._home_combo.addItem(label, key)
        self._select_combo_by_data(self._home_combo, self.s.home_destination)
        self._home_combo.currentIndexChanged.connect(
            lambda _: setattr(
                self.s, "home_destination", self._home_combo.currentData() or "albums"
            )
        )
        # Same fixedHeight as the ghost buttons above — see the
        # _CTRL_H comment in the button row. sizeHint() can't be
        # relied on here because nested children aren't polished
        # against the dialog QSS until they're shown.
        self._home_combo.setFixedHeight(self._general_ctrl_h)
        # Match the Change-server-URL button width exactly so the two
        # outlined chips read as a balanced visual pair across the
        # General page.
        self._home_combo.setMinimumWidth(self._general_change_btn_w)
        home_row = QHBoxLayout()
        home_row.setContentsMargins(0, 6, 0, 0)
        home_row.addWidget(self._home_combo)
        home_row.addStretch(1)
        v.addLayout(home_row)

        # ── Updates ───────────────────────────────────────────────────
        # In-app update check (updates.py). Shown only on MANUAL install
        # channels — the Store / Mac App Store / AUR builds update themselves,
        # so the section is hidden there (nothing to configure).
        from jellytoast import updates as _updates

        if not _updates.is_auto_update_channel():
            # A touch more than the 12px section rhythm elsewhere — this header
            # follows the Home-page combo BOX, which needs more air beneath it
            # than the plain checkboxes the other headers follow.
            v.addSpacing(16)
            v.addWidget(
                self._section_header(QCoreApplication.translate("SettingsDialog", "UPDATES"))
            )
            v.addSpacing(4)
            up_row = QHBoxLayout()
            up_row.setContentsMargins(0, 0, 0, 0)
            up_row.setSpacing(10)
            self._update_check = WrapCheckBox(
                QCoreApplication.translate("SettingsDialog", "Check for updates")
            )
            self._update_check.setChecked(self.s.check_for_updates_enabled)
            self._update_check.toggled.connect(
                lambda val: setattr(self.s, "check_for_updates_enabled", val)
            )
            up_row.addWidget(self._update_check)
            up_row.addWidget(
                self._info_button(
                    QCoreApplication.translate("SettingsDialog", "Check for updates"),
                    QCoreApplication.translate(
                        "SettingsDialog",
                        "Once a day, jellytoast quietly checks GitHub for a newer "
                        "release and shows a small chip in the top bar if one's out. "
                        "Manual installs only — the Microsoft Store / Mac App Store "
                        "builds update themselves. Uses GitHub's public API; no "
                        "account or personal data.",
                    ),
                )
            )
            up_row.addStretch(1)
            self._update_check_now = QPushButton(
                QCoreApplication.translate("SettingsDialog", "Check now")
            )
            self._update_check_now.clicked.connect(self._on_check_updates_now)
            v.addLayout(up_row)
            from jellytoast.platform_compat import is_mobile_shell

            if getattr(self, "_compact", False) or is_mobile_shell():
                # No room beside the checkbox at phone width — its own line.
                v.addSpacing(6)
                v.addWidget(self._update_check_now, 0, Qt.AlignmentFlag.AlignLeft)
            else:
                up_row.addWidget(self._update_check_now)

        # ── Diagnostics ───────────────────────────────────────────────
        # One-click support report (diagnostics.collect_report): versions,
        # session type, blur status, provider + offline state, cover-pipeline
        # counters, a secrets-excluded settings dump and the log tail. Lands on
        # the clipboard, ready to paste into an issue.
        v.addSpacing(16)
        v.addWidget(
            self._section_header(QCoreApplication.translate("SettingsDialog", "DIAGNOSTICS"))
        )
        v.addSpacing(4)
        diag_row = QHBoxLayout()
        diag_row.setContentsMargins(0, 0, 0, 0)
        diag_row.setSpacing(10)
        self._diagnostics_btn = QPushButton(
            QCoreApplication.translate("SettingsDialog", "Copy diagnostics")
        )
        self._diagnostics_btn.clicked.connect(self._on_copy_diagnostics)
        diag_row.addWidget(self._diagnostics_btn)
        diag_row.addWidget(
            self._info_button(
                QCoreApplication.translate("SettingsDialog", "Copy diagnostics"),
                QCoreApplication.translate(
                    "SettingsDialog",
                    "Copies a support report to your clipboard — app version, "
                    "system and desktop, audio and theme details, plus the tail "
                    "of the log — so you can paste it into a bug report. Your "
                    "password, access token and scrobbling keys are never "
                    "included, and the server address is trimmed to its host.",
                ),
            )
        )
        diag_row.addStretch(1)
        v.addLayout(diag_row)

        # Acknowledgement line for the button above — same faint caption idiom
        # the "(needs restart)" hints use. Empty (but laid out) until clicked,
        # so the section doesn't reflow when the text appears.
        self._diagnostics_note = QLabel("")
        self._diagnostics_note.setWordWrap(True)
        self._diagnostics_note.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)}")
        v.addWidget(self._diagnostics_note)

        v.addStretch(1)
        return page

    def _on_copy_diagnostics(self):
        """Put the support report on the clipboard and say so in the note line
        below the button — no extra dialog to dismiss. Never raises: the whole
        point of this button is the paths where something is already wrong."""
        from jellytoast import diagnostics

        try:
            ok = diagnostics.copy_to_clipboard()
        except Exception:
            ok = False
        self._diagnostics_note.setText(
            QCoreApplication.translate("SettingsDialog", "Diagnostics copied to clipboard.")
            if ok
            else QCoreApplication.translate("SettingsDialog", "Couldn't copy diagnostics.")
        )

    def _set_conn_dot_state(self, state: str):
        dot = getattr(self, "_conn_dot", None)
        if dot is None:
            return
        color, tip = self._CONN_DOT_STATES.get(state, self._CONN_DOT_STATES["checking"])
        # 8×8 with border-radius 4 → circle.
        dot.setStyleSheet(f"QLabel {{ background: {color}; border-radius: {rad(4)}px; }}")
        dot.setToolTip(tip)

    def _on_connection_probe(self, info):
        """Update the connection-status dot after the probe lands.
        info is the dict provider.probe returned on success, or None
        on any failure (network error, wrong port, non-backend
        response). The dot may have been destroyed if the user closed
        the dialog before the probe came back."""
        try:
            self._set_conn_dot_state("reachable" if info else "disconnected")
        except RuntimeError:
            # QLabel was destroyed (dialog closed) — drop silently.
            pass

    def _on_change_server_clicked(self):
        """First click drops the readonly flag on the URL field and
        paints in the input chrome (border + tinted fill) — the text
        position/size/colour don't move because the readonly style
        already reserves the same padding. Second click commits via
        the shared save path below. Pressing Enter in the field
        also commits."""
        if not self._url_edit.isReadOnly():
            self._commit_server_url_edit()
            return
        self._url_edit.setReadOnly(False)
        self._apply_url_edit_chrome(editing=True)
        self._url_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        # setCursorPosition (not selectAll) so the user can type/paste
        # at the end without first clearing the field.
        self._url_edit.setCursorPosition(len(self._url_edit.text()))
        self._change_btn.setText(QCoreApplication.translate("SettingsDialog", "Save URL"))

    def _commit_server_url_edit(self):
        """Apply the inline edit. Emits with the trimmed URL; the
        consumer (jellytoast main window) signs out + routes to the
        login view, which also closes this dialog. Empty / unchanged
        values silently restore readonly display without firing —
        same bail-out the old QInputDialog path had."""
        new_url = self._url_edit.text().strip().rstrip("/")
        current = (self.s.server_url or "").rstrip("/")
        if not new_url or new_url == current:
            self._exit_url_edit_mode()
            return
        # Down-stack: the main window slot reads this URL, persists
        # it, and runs sign-out which closes the dialog. No need to
        # restore display state here — the dialog is going away.
        self.server_change_requested.emit(new_url)

    def _exit_url_edit_mode(self):
        self._url_edit.setReadOnly(True)
        self._url_edit.setText(
            self.s.server_url or QCoreApplication.translate("SettingsDialog", "Not configured")
        )
        self._url_edit.setCursorPosition(0)
        self._apply_url_edit_chrome(editing=False)
        self._change_btn.setText(QCoreApplication.translate("SettingsDialog", "Change server URL…"))

    def _apply_url_edit_chrome(self, *, editing: bool) -> None:
        """Toggle the field's border + fill between "looks like text"
        (readonly) and "looks like an input box" (editing). Padding
        and font are constant in both modes — the box pops *around*
        the URL text rather than shifting it. Inline stylesheet
        overrides the dialog-wide QLineEdit QSS just for this widget
        via the ``serverUrlField`` objectName."""
        if editing:
            # Empty inline → fall back to the dialog's QLineEdit rule
            # (1-px solid border + ink_alpha(0.06) fill + 6/12 padding).
            self._url_edit.setStyleSheet("")
            return
        # Readonly: transparent border + transparent fill, keeping the
        # exact same padding (6 12) and border *width* (1 px) the
        # dialog QSS sets so the cursor's x-position is identical
        # between modes. Border-radius is inherited.
        self._url_edit.setStyleSheet(
            "QLineEdit#serverUrlField { "
            "background: transparent; "
            "border: 1px solid transparent; "
            "padding: 6px 12px; "
            f"color: {TEXT}; "
            "}"
        )

    def _on_keep_above_toggled(self, on: bool):
        # Persist first, then apply — if the rule write fails we still
        # remember the user's intent for the next launch / retry.
        self.s.mini_player_keep_above = on
        if on:
            install_mini_player_rule()
        else:
            remove_mini_player_rule()

    def _on_autostart_toggled(self, on: bool):
        # Persist user intent, then ask the backend to make it so. If that
        # fails (read-only home — or, on Linux, the desktop's Background
        # portal denying the request), the QSettings flag still records
        # what the user asked for so we can retry next time the dialog
        # opens. The portal may prompt, so this call can take a moment.
        self.s.autostart = on
        ok = _autostart.enable() if on else _autostart.disable()
        # If reality drifted (e.g. enable failed or was denied), reflect
        # that in the checkbox without re-firing the toggled signal.
        if on and not ok and not _autostart.is_enabled():
            self._autostart_check.blockSignals(True)
            self._autostart_check.setChecked(False)
            self._autostart_check.blockSignals(False)

    def _audio_device_choices(self) -> list:
        """mpv's enumerated outputs via the live MpvController on the
        host window. Empty before the deferred player init (the dialog
        then offers Auto + the persisted value only)."""
        ctrl = getattr(self.parent(), "mpv_ctrl", None)
        if ctrl is None:
            return []
        try:
            return ctrl.audio_device_choices()
        except Exception:
            return []

    def _on_audio_output_changed(self, _idx: int):
        dev = self._audio_out_combo.currentData() or "auto"
        self.s.audio_output_device = dev
        # Runtime push — PlayerBackend re-targets the live handle(s);
        # mpv applies it on the next audio-output (re)open (next track).
        PlayerBus.get().audio_output_device_changed.emit(dev)
        self._refresh_bp_path_hint()

    def _on_check_updates_now(self):
        """Manual "Check now" — force a check past the daily throttle + any
        dismissed version (the channel gate still applies, so it's a no-op on a
        self-updating build). An available update surfaces as the top-bar chip."""
        try:
            from jellytoast import updates

            updates.maybe_check(force=True)
        except Exception:
            pass
