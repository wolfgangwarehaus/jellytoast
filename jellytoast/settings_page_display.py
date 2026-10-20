"""Settings dialog — the Display (theme, glass, accent, theme import, fonts, language) page.

Split out of ``settings_dialog.py``: a mixin of ``SettingsDialog`` (it uses
the dialog's widgets, settings handle and shared helpers via ``self``).
Strings use ``QCoreApplication.translate("SettingsDialog", …)`` rather than
``self.tr`` so they keep the SettingsDialog translation context — lupdate
files a mixin's ``self.tr`` under the MIXIN's name, which never matches at
runtime.
"""

import sys

from PySide6.QtCore import QCoreApplication, QSize, Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSlider,
    QVBoxLayout,
    QWidget,
)

from jellytoast.design_tokens import (
    TYPE_BODY,
    TYPE_CAPTION,
    font,
    rad,
    type_qss,
)
from jellytoast.player_state import PlayerBus
from jellytoast.selector import (
    Selector as _Selector,  # noqa: E402  (deliberate post-class import for layout)
)
from jellytoast.settings_widgets import (
    _THEME_MODE_CHOICES,
    LYRICS_FONT_SIZES,
    _AccentSwatch,
    _EyedropperSwatch,
)
from jellytoast.theme import _hex_to_rgb, ink_rgb
from jellytoast.ui_helpers import (
    ACCENT,
    TEXT,
    TEXT_DIM,
    TEXT_FAINT,
    ink_alpha,
)


class _DisplayPageMixin:
    # ── Page: Display ──────────────────────────────────────────────────
    # Unified page covering everything that affects how the UI looks:
    # theme mode (was Appearance), lyrics text size (was Lyrics), and
    # the placeholder scaling sliders. Three sections separated by
    # section headers + blank space, so the visual hierarchy reads as
    # one settings page rather than three pages stitched together.
    def _build_display(self) -> QWidget:
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)

        # Restart notice — page-level alert pinned to the top so it
        # doesn't shove itself between controls when the user picks
        # a new theme / accent. Visible only after a dirty change.
        _ar2, _ag2, _ab2 = _hex_to_rgb(ACCENT)
        self._theme_restart_notice = QLabel(
            QCoreApplication.translate(
                "SettingsDialog", "Restart jellytoast to apply your display changes."
            )
        )
        self._theme_restart_notice.setWordWrap(True)
        self._theme_restart_notice.setStyleSheet(
            f"color: {TEXT}; "
            f"background: rgba({_ar2},{_ag2},{_ab2},0.18); "
            f"border-radius: {rad(6)}px; "
            f"padding: 10px 14px; "
            f"{type_qss(TYPE_CAPTION)}"
        )
        self._theme_restart_notice.setMinimumHeight(36)
        self._theme_restart_notice.hide()
        v.addWidget(self._theme_restart_notice)

        # ── Theme ──────────────────────────────────────────────────────
        # One dropdown of theme families (jellytoast + the preset brands) + an
        # Import button; an orthogonal Dark/Light/Follow-system Mode control
        # (only when the family ships both) and an always-present Frosted/Opaque
        # switch. Then the accent row (jellytoast) OR a base16 palette preview
        # (presets), chosen by the active family. Built by _build_theme_section
        # so the whole block re-evaluates on the page rebuild a family/mode
        # change triggers.
        v.addWidget(self._section_header(QCoreApplication.translate("SettingsDialog", "Theme")))
        v.addLayout(self._build_theme_section())

        # Blur-availability hint — when a Frosted theme is active but this
        # machine can't produce real compositor/OS blur, explain why the body
        # reads near-opaque rather than glass (it is never see-through). Shown
        # only in that case; the page rebuilds on every theme change so it
        # re-evaluates without a manual reconnect.
        self._blur_hint = QLabel()
        self._blur_hint.setWordWrap(True)
        self._blur_hint.setStyleSheet(
            f"color: {TEXT_DIM}; "
            f"background: {ink_alpha(0.05)}; "
            f"border-radius: {rad(6)}px; "
            f"padding: 8px 12px; "
            f"{type_qss(TYPE_CAPTION)}"
        )
        self._blur_hint.setMinimumHeight(32)
        self._blur_hint.hide()
        v.addWidget(self._blur_hint)
        self._update_blur_hint()

        # ── Scaling ────────────────────────────────────────────────────
        # Font size scales every design-token font size + button
        # geometry via `jellytoast.design_tokens` (restart required).
        # Lyrics font size lives here too since it's a type-size knob
        # in spirit; it applies live via PlayerBus. UI scale is NOT
        # exposed — KDE Plasma's system-wide scale slider already
        # handles it via Qt 6's `wp_fractional_scale_v1` integration,
        # so adding a duplicate app-level knob would just diverge.
        v.addWidget(self._section_header(QCoreApplication.translate("SettingsDialog", "Scaling")))

        scaling_form = QFormLayout()
        scaling_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        scaling_form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        scaling_form.setHorizontalSpacing(16)
        scaling_form.setVerticalSpacing(10)
        # QFormLayout's field-growth policy is only a STYLE HINT: Windows'
        # native style ignored FieldsStayAtSizeHint and grew the field cell to
        # the full page width, so the selectors stretched despite their fixed
        # width (the Theme combo escaped it by living in a box layout). A box
        # layout DOES honor a child's fixed width, so wrap each selector in an
        # HBox [selector, stretch]: the stretch eats the stretched cell and the
        # selector sits at its fixed width, left-aligned — matching the Theme
        # combo on every platform.
        scaling_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)

        def _left(widget):
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.addWidget(widget)
            row.addStretch(1)
            return row

        # Font size — app-wide font scale. Writes to settings.font_scale
        # and triggers the restart notice; design_tokens reads the
        # setting at module import and bakes the multiplier into every
        # TypeTier / ButtonTier.
        self._font_size_combo = _Selector()
        for label, key in LYRICS_FONT_SIZES:
            self._font_size_combo.addItem(label, key)
        self._initial_font_scale = self.s.font_scale
        # Currently-APPLIED scale — updated on each live apply / revert.
        self._active_font_scale = self._initial_font_scale
        self._select_combo_by_data(self._font_size_combo, self._initial_font_scale)
        self._font_size_combo.currentIndexChanged.connect(self._on_font_scale_changed)
        self._font_size_combo.setMinimumWidth(185)  # 281 left → 466 (eyedropper right)
        scaling_form.addRow(
            self._field_label(QCoreApplication.translate("SettingsDialog", "Font size:")),
            _left(self._font_size_combo),
        )

        # Font family — app-wide UI text font. Lists installed families that
        # can render Latin text (drops private + symbol/icon fonts so the user
        # can't pick one that renders all text as tofu). "System default" maps
        # to '' = the built-in Inter stack. Writes settings.font_family and
        # triggers the restart notice; applied via the global QSS font stack +
        # app.setFont at boot. Icons are SVG, so they're never affected.
        from PySide6.QtGui import QFont, QFontDatabase

        self._font_family_combo = _Selector()
        self._font_family_combo.addItem(
            QCoreApplication.translate("SettingsDialog", "System default"), ""
        )
        _latin = QFontDatabase.WritingSystem.Latin
        for _fam in QFontDatabase.families():
            if QFontDatabase.isPrivateFamily(_fam):
                continue
            if _latin not in QFontDatabase.writingSystems(_fam):
                continue
            # Preview each family in its own typeface in the dropdown.
            self._font_family_combo.addItem(_fam, _fam, font=QFont(_fam))
        self._initial_font_family = self.s.font_family
        # Currently-APPLIED family — updated on each live apply / revert so a
        # subsequent pick reverts to the right previous value.
        self._active_font_family = self._initial_font_family
        self._select_combo_by_data(self._font_family_combo, self._initial_font_family)
        self._font_family_combo.currentIndexChanged.connect(self._on_font_family_changed)
        self._font_family_combo.setMinimumWidth(185)
        scaling_form.addRow(
            self._field_label(QCoreApplication.translate("SettingsDialog", "Font:")),
            _left(self._font_family_combo),
        )

        # Lyrics font size — wires live to PlayerBus, no restart needed.
        self._lyrics_size_combo = _Selector()
        for label, key in LYRICS_FONT_SIZES:
            self._lyrics_size_combo.addItem(label, key)
        self._select_combo_by_data(self._lyrics_size_combo, self.s.lyrics_font_size)
        self._lyrics_size_combo.currentIndexChanged.connect(self._on_lyrics_size_changed)
        self._lyrics_size_combo.setMinimumWidth(185)  # 281 left → 466 (eyedropper right)
        scaling_form.addRow(
            self._field_label(QCoreApplication.translate("SettingsDialog", "Lyrics font size:")),
            _left(self._lyrics_size_combo),
        )

        v.addLayout(scaling_form)

        # ── Now-playing info ────────────────────────────────────────────
        # Per-segment toggles for the streaming-info line that sits
        # above the transport row in the now-playing bar
        # ("Streaming · Bit Perfect · FLAC · 1411 kbps" etc.). Defaults
        # match typical listener expectations — codec + bitrate + the
        # active DSP that actually matters (Bit Perfect, EQ) on,
        # ReplayGain + Crossfade off because the people who use them
        # already know they're using them.
        v.addSpacing(4)
        v.addWidget(
            self._section_header(QCoreApplication.translate("SettingsDialog", "Now-playing info"))
        )

        def _badge_toggle(attr: str, label: str) -> QCheckBox:
            cb = QCheckBox(label)
            cb.setChecked(bool(getattr(self.s, attr)))

            def _on(val, name=attr):
                setattr(self.s, name, bool(val))
                PlayerBus.get().streaming_info_badges_changed.emit()

            cb.toggled.connect(_on)
            return cb

        self._badge_bit_perfect = _badge_toggle(
            "streaming_info_show_bit_perfect",
            QCoreApplication.translate("SettingsDialog", "Bit Perfect"),
        )
        self._badge_eq = _badge_toggle("streaming_info_show_eq", "EQ")
        self._badge_replaygain = _badge_toggle(
            "streaming_info_show_replaygain",
            QCoreApplication.translate("SettingsDialog", "Normalized"),
        )
        self._badge_crossfade = _badge_toggle(
            "streaming_info_show_crossfade",
            QCoreApplication.translate("SettingsDialog", "Crossfade"),
        )
        self._badge_codec = _badge_toggle(
            "streaming_info_show_codec",
            QCoreApplication.translate("SettingsDialog", "Codec (FLAC, MP3, …)"),
        )
        self._badge_bitrate = _badge_toggle(
            "streaming_info_show_bitrate",
            QCoreApplication.translate("SettingsDialog", "Bitrate (kbps)"),
        )
        # Two columns (3 + 3) so this section stays compact vertically and
        # the Display page fits without the badges pushing it tall.
        _np_cols = QHBoxLayout()
        _np_cols.setContentsMargins(0, 0, 0, 0)
        _np_cols.setSpacing(48)
        _np_left = QVBoxLayout()
        _np_left.setContentsMargins(0, 0, 0, 0)
        _np_left.setSpacing(10)
        _np_right = QVBoxLayout()
        _np_right.setContentsMargins(0, 0, 0, 0)
        _np_right.setSpacing(10)
        for w in (self._badge_bit_perfect, self._badge_eq, self._badge_replaygain):
            _np_left.addWidget(w)
        for w in (self._badge_crossfade, self._badge_codec, self._badge_bitrate):
            _np_right.addWidget(w)
        _np_cols.addLayout(_np_left)
        _np_cols.addLayout(_np_right)
        _np_cols.addStretch(1)  # keep both columns left-packed
        v.addLayout(_np_cols)

        # ── Interface ──────────────────────────────────────────────────
        v.addSpacing(4)
        v.addWidget(self._section_header(QCoreApplication.translate("SettingsDialog", "Interface")))

        # Language — the UI language override (#232). "System default" ('')
        # follows the OS locale; explicit picks are bare codes ("es").
        # Translators install in main() before any widget exists, so this is
        # restart-required like Square corners. Options come from
        # i18n.SHIPPED_LANGUAGES so adding a catalog auto-extends the menu.
        from jellytoast.i18n import SHIPPED_LANGUAGES

        lang_row = QHBoxLayout()
        lang_row.setContentsMargins(0, 0, 0, 0)
        lang_row.setSpacing(10)
        self._language_combo = _Selector()
        self._language_combo.addItem(
            QCoreApplication.translate("SettingsDialog", "System default"), ""
        )
        self._language_combo.addItem("English", "en")
        for _code, _en_name, _native in SHIPPED_LANGUAGES:
            self._language_combo.addItem(_native, _code)
        self._initial_language = self.s.language
        self._select_combo_by_data(self._language_combo, self._initial_language)
        self._language_combo.currentIndexChanged.connect(self._on_language_changed)
        self._language_combo.setMinimumWidth(185)
        lang_row.addWidget(
            self._field_label(QCoreApplication.translate("SettingsDialog", "Language:"))
        )
        lang_row.addWidget(self._language_combo)
        lang_hint = QLabel(QCoreApplication.translate("SettingsDialog", "(needs restart)"))
        lang_hint.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)}")
        from jellytoast.platform_compat import is_mobile_shell

        if is_mobile_shell():
            # No room beside the combo at phone width — the hint goes below.
            lang_row.addStretch(1)
            v.addLayout(lang_row)
            v.addWidget(lang_hint)
        else:
            lang_row.addWidget(lang_hint)
            lang_row.addStretch(1)
            v.addLayout(lang_row)

        self._tooltips_check = QCheckBox(
            QCoreApplication.translate("SettingsDialog", "Show hover tooltips")
        )
        self._tooltips_check.setChecked(self.s.show_tooltips)
        self._tooltips_check.toggled.connect(lambda val: setattr(self.s, "show_tooltips", val))
        # Nest in a left-packed HBox so it shares the x of the NOW-PLAYING INFO
        # checkbox columns above (which are layout-nested → x=172). A bare
        # addWidget'd checkbox sits at x=170 and reads 2px left of them; AlignLeft
        # doesn't move it (it only sizes), so the row has to be nested.
        _tt_row = QHBoxLayout()
        _tt_row.setContentsMargins(0, 0, 0, 0)
        _tt_row.addWidget(self._tooltips_check)
        _tt_row.addStretch(1)
        v.addLayout(_tt_row)

        # Borderless main window — any Linux Wayland session (KDE strips the
        # decoration with a KWin rule; GNOME / wlroots go Qt-frameless / CSD).
        # Toggling this on restores the desktop's native titlebar. Decoration
        # is decided when the window is constructed, so this is restart-
        # required; the toggle just persists the intent. The "(needs restart)"
        # suffix is a separate dim QLabel laid out next to the checkbox so we
        # can style it independently — QCheckBox doesn't render rich text.
        from jellytoast.platform_compat import is_linux_wayland

        if is_linux_wayland():
            nb_row = QHBoxLayout()
            nb_row.setContentsMargins(0, 0, 0, 0)
            nb_row.setSpacing(10)
            self._native_border_check = QCheckBox(
                QCoreApplication.translate("SettingsDialog", "Use native window border")
            )
            self._native_border_check.setChecked(self.s.native_window_border)
            self._native_border_check.toggled.connect(
                lambda val: setattr(self.s, "native_window_border", val)
            )
            nb_row.addWidget(self._native_border_check)
            nb_hint = QLabel(QCoreApplication.translate("SettingsDialog", "(needs restart)"))
            nb_hint.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)}")
            nb_row.addWidget(nb_hint)
            nb_row.addStretch(1)
            v.addLayout(nb_row)

        # Square corners — zero every rounded corner in the UI (windows, album
        # art, tiles, dialogs, players, buttons, popups); genuinely circular
        # controls (round icon buttons, the slider handle) stay round.
        # design_tokens bakes the radii at module import, so this is restart-
        # required; the toggle persists the intent and shows the notice.
        sc_row = QHBoxLayout()
        sc_row.setContentsMargins(0, 0, 0, 0)
        sc_row.setSpacing(10)
        self._square_corners_check = QCheckBox(
            QCoreApplication.translate("SettingsDialog", "Square corners")
        )
        self._initial_square_corners = self.s.square_corners
        self._square_corners_check.setChecked(self._initial_square_corners)
        self._square_corners_check.toggled.connect(self._on_square_corners_changed)
        sc_row.addWidget(self._square_corners_check)
        sc_hint = QLabel(QCoreApplication.translate("SettingsDialog", "(needs restart)"))
        sc_hint.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)}")
        sc_row.addWidget(sc_hint)
        sc_row.addStretch(1)
        v.addLayout(sc_row)

        # NB: no "Opaque background" toggle. A frosted theme that can't get
        # real compositor blur already falls back to a near-opaque body
        # automatically (see jellytoast.blur.status() + theme.body_color_for),
        # which is what users actually want here — the manual toggle was
        # redundant and, by dropping WA_TranslucentBackground, broke the
        # window's rounded corners. JT_OPAQUE=1 stays as a dev-only env
        # diagnostic (streaming-flicker repro); it's intentionally not a
        # user setting.

        # macOS reserves ~4px less in a QCheckBox's sizeHint than it actually
        # renders, so the page's last control (Show hover tooltips) clips at the
        # content-widget's bottom edge. A little tail spacing grows the page's
        # sizeHint past that shortfall; harmless elsewhere (the stretch eats it).
        v.addSpacing(8)
        v.addStretch(1)
        return page

    def _on_lyrics_size_changed(self):
        key = self._lyrics_size_combo.currentData() or "default"
        self.s.lyrics_font_size = key
        PlayerBus.get().lyrics_font_size_changed.emit(key)

    def _on_font_scale_changed(self):
        # LIVE-apply the font scale (no restart) + keep/auto-revert prompt.
        new = self._font_size_combo.currentData() or "default"
        old = self._active_font_scale
        if new == old:
            return
        from PySide6.QtCore import QTimer

        from jellytoast.appearance_confirm import show_appearance_revert
        from jellytoast.ui_helpers import apply_font_settings_live

        def _apply(scale):
            self.s.font_scale = scale
            apply_font_settings_live()  # recomputes + propagates the size tokens
            self._active_font_scale = scale
            self._apply_dialog_size()  # the dialog scales too
            # Rebuild this dialog's pages so its OWN labels re-stamp at the new
            # size — they bake type_qss at construction and don't otherwise
            # re-run. Deferred: the combo that fired this lives on the page we
            # tear down. The rebuilt combo re-selects the current scale, and
            # its connect happens after selection so it won't re-fire.
            QTimer.singleShot(0, self._rebuild_pages_for_theme)

        _apply(new)
        show_appearance_revert(lambda: _apply(old))

    def _on_font_family_changed(self):
        # LIVE-apply the font family (no restart) and show a keep/auto-revert
        # safety prompt so a broken/illegible pick undoes itself in 10s.
        new = self._font_family_combo.currentData() or ""
        old = self._active_font_family
        if new == old:
            return
        from PySide6.QtCore import QTimer

        from jellytoast.appearance_confirm import show_appearance_revert
        from jellytoast.ui_helpers import apply_font_settings_live

        self.s.font_family = new
        apply_font_settings_live()  # reads settings.font_family live
        self._active_font_family = new
        # A new family changes the sidebar labels' width — re-widen the column
        # to fit (deferred so fontMetrics reflect the propagated family) rather
        # than letting a wider font clip against the fixed 128px start.
        QTimer.singleShot(0, self._size_nav_to_content)

        def _revert(old=old):
            self.s.font_family = old
            apply_font_settings_live()
            self._active_font_family = old
            QTimer.singleShot(0, self._size_nav_to_content)
            # Reset the combo without re-firing this handler.
            self._font_family_combo.blockSignals(True)
            self._select_combo_by_data(self._font_family_combo, old)
            self._font_family_combo.blockSignals(False)

        show_appearance_revert(_revert)

    def _on_square_corners_changed(self, on: bool):
        # Persist + keep the in-memory design_tokens flag in sync, then show
        # the restart notice — the radius tokens are baked at import, so the
        # squared/rounded look fully applies on the next launch.
        self.s.square_corners = bool(on)
        from jellytoast.design_tokens import set_square_corners

        set_square_corners(bool(on))
        self._refresh_restart_notice_visibility()

    def _on_language_changed(self):
        # Persist the pick; translators only install at boot (main() runs
        # i18n.install before any widget exists), so show the restart notice.
        self.s.language = self._language_combo.currentData() or ""
        self._refresh_restart_notice_visibility()

    def _refresh_restart_notice_visibility(self):
        """Show the restart banner if a setting that bakes into module
        state differs from the value loaded when the dialog opened.

        ``square_corners`` bakes into ``design_tokens`` at import time, and
        ``language`` installs translators before any widget exists — both need
        a restart. Font family + font scale now live-apply
        (``apply_font_settings_live`` — the tokens are recomputed + propagated
        at runtime), and theme mode / accent live-apply via
        ``refresh_theme()`` + ``PlayerBus.theme_changed`` — so none of those are
        part of the dirty check."""
        dirty = self.s.square_corners != getattr(
            self, "_initial_square_corners", self.s.square_corners
        ) or self.s.language != getattr(self, "_initial_language", self.s.language)
        self._theme_restart_notice.setVisible(dirty)

    def _update_blur_hint(self):
        """Show a 'why is Frosted near-opaque' note when the active theme is
        frosted but this machine can't produce verified compositor/OS blur.
        Hidden otherwise (Opaque theme, or blur ACTIVE)."""
        hint = getattr(self, "_blur_hint", None)
        if hint is None:
            return
        from jellytoast import blur
        from jellytoast.platform_compat import IS_MACOS
        from jellytoast.theme import get_active_theme

        # macOS: the faux-frost body is the INTENDED frosted look there (native
        # vibrancy is disabled by design — see blur/_macos.py), not a
        # can't-get-blur degradation, so don't nag about it. `blur` on the
        # resolved theme tracks the Frosted/Opaque switch (frosted_* → True).
        theme = get_active_theme()
        if (
            theme is not None
            and theme.blur
            and not IS_MACOS
            and blur.status() is not blur.BlurStatus.ACTIVE
        ):
            hint.setText(
                QCoreApplication.translate(
                    "SettingsDialog", "Frosted glass needs compositor blur — {0}."
                ).format(blur.reason())
            )
            hint.show()
        else:
            hint.hide()

    def _on_external_theme_changed(self):
        """theme_changed fired from OUTSIDE the dialog (the OS-driven "auto"
        follow, or another surface). If the resolved theme NAME changed it's a
        mode flip, so rebuild the pages (deferred — rebuilding tears down live
        widgets) to re-stamp baked text. Accent-only changes keep the same
        name and are skipped here (the accent restamp handles them)."""
        from jellytoast.theme import get_active_theme

        name = get_active_theme().name
        if name != self._last_theme_name:
            self._last_theme_name = name
            QTimer.singleShot(0, self._rebuild_pages_for_theme)

    # ── Theme family / mode / frosted — the unified theme controls ──────
    def _build_theme_section(self) -> QVBoxLayout:
        """The Theme block: family dropdown + Import, an orthogonal Dark/Light/
        Follow-system Mode control (only when the family has both), an
        always-present Frosted/Opaque switch, and then the accent row
        (jellytoast) or a base16 palette preview (a preset). Rebuilt whole on
        each family/mode change via ``_rebuild_pages_for_theme``."""
        from jellytoast.theme_presets import family_has_both

        fam_key = self.s.theme_family or "jellytoast"
        if fam_key == "imported" and self.s.imported_scheme_path:
            # The active import came from the watched themes folder — select
            # its folder row so the pick sticks visually across rebuilds.
            fam_key = f"file:{self.s.imported_scheme_path}"

        box = QVBoxLayout()
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(10)

        # Family dropdown (fixed width so its right edge is stable across family
        # names) + Import button. The glass "Default" button reuses these widths
        # so it right-aligns with Import (same slider width + same button width).
        fam_w = self._THEME_COMBO_W
        self._theme_btn_w = self._theme_button_width()
        top = QHBoxLayout()
        top.setContentsMargins(0, 0, 0, 0)
        top.setSpacing(10)
        self._family_combo = _Selector()
        self._family_combo.setFixedHeight(34)
        self._family_combo.setIconSize(QSize(52, 16))
        self._family_combo.setFixedWidth(fam_w)
        for label, key, ic in self._family_items():
            if ic is not None:
                self._family_combo.addItem(label, key, icon=ic)
            else:
                self._family_combo.addItem(label, key)
        self._select_combo_by_data(self._family_combo, fam_key)
        self._family_combo.currentIndexChanged.connect(lambda _=0: self._on_family_changed())
        top.addWidget(self._family_combo)
        imp = QPushButton(QCoreApplication.translate("SettingsDialog", "Import…"))
        imp.setObjectName("ghost")
        imp.setFixedWidth(self._theme_btn_w)
        imp.setToolTip(
            QCoreApplication.translate(
                "SettingsDialog", "Import a base16 .yaml colour scheme (≈250 community themes)"
            )
        )
        imp.clicked.connect(self._on_import_theme)
        top.addWidget(imp)
        top.addStretch(1)
        box.addLayout(top)

        # Mode (luminance) — only for a family that ships both dark + light. The
        # combo is sized so its RIGHT edge lines up with the family dropdown's:
        # combo_w = family_w − label_w − spacing (both rows share the left edge).
        if family_has_both(fam_key):
            self._mode_combo = _Selector()
            for label, key in _THEME_MODE_CHOICES:
                self._mode_combo.addItem(label, key)
            self._select_combo_by_data(self._mode_combo, self.s.theme_mode)
            self._mode_combo.currentIndexChanged.connect(lambda _=0: self._on_mode_changed())
            mode_row = QHBoxLayout()
            mode_row.setContentsMargins(0, 0, 0, 0)
            mode_row.setSpacing(10)
            mode_label = self._field_label(QCoreApplication.translate("SettingsDialog", "Mode:"))
            mode_row.addWidget(mode_label)
            mode_row.addWidget(self._mode_combo)
            mode_row.addStretch(1)
            self._mode_combo.setFixedWidth(max(120, fam_w - mode_label.sizeHint().width() - 10))
            box.addLayout(mode_row)
        else:
            self._mode_combo = None

        # Frosted / Opaque — always present.
        self._frosted_check = QCheckBox(
            QCoreApplication.translate("SettingsDialog", "Frosted glass")
        )
        self._frosted_check.setChecked(self.s.frosted)
        self._frosted_check.setToolTip(
            QCoreApplication.translate(
                "SettingsDialog", "Translucent, blurred body (Frosted) vs a solid opaque body."
            )
        )
        self._frosted_check.toggled.connect(self._on_frosted_toggled)
        box.addWidget(self._frosted_check)

        is_preset = fam_key not in ("", "jellytoast")
        # Glass opacity sits right under Frosted for ANY family (jellytoast +
        # presets), only while Frosted is on — an opaque body is fixed at solid.
        if self.s.frosted:
            box.addLayout(self._build_glass_opacity_row())

        if not is_preset:
            # Accent row + follow-system-accent (jellytoast owns its accent).
            box.addSpacing(2)
            box.addWidget(
                self._section_header(QCoreApplication.translate("SettingsDialog", "Accent color"))
            )
            box.addLayout(self._build_accent_row())
            self._follow_accent_check = QCheckBox(
                QCoreApplication.translate("SettingsDialog", "Follow system accent")
            )
            self._follow_accent_check.setChecked(self.s.follow_system_accent)
            self._follow_accent_check.setToolTip(
                QCoreApplication.translate(
                    "SettingsDialog",
                    "Adopt your desktop's accent colour "
                    "(KDE Plasma / GNOME 47+ / Windows / macOS).",
                )
            )
            self._follow_accent_check.toggled.connect(self._on_follow_system_accent_toggled)
            box.addSpacing(4)
            box.addWidget(self._follow_accent_check)
        else:
            # base16 palette preview — the scheme designates its own accent, so
            # no swatch row; show the 16-colour palette instead.
            base16 = self._current_member_base16()
            if base16:
                box.addSpacing(2)
                box.addWidget(
                    self._section_header(QCoreApplication.translate("SettingsDialog", "Palette"))
                )
                box.addWidget(self._base16_grid(base16), 0, Qt.AlignmentFlag.AlignLeft)

        # Follow pywal / wallust — Linux ricing loop (any family: following
        # re-themes onto the imported family on the next wallpaper change). The
        # ⓘ carries the "what is this" so the label stays terse.
        if sys.platform.startswith("linux"):
            self._follow_pywal_check = QCheckBox(
                QCoreApplication.translate("SettingsDialog", "Follow pywal / wallust")
            )
            self._follow_pywal_check.setChecked(self.s.follow_pywal)
            self._follow_pywal_check.toggled.connect(self._on_follow_pywal_toggled)
            pywal_row = QHBoxLayout()
            pywal_row.setContentsMargins(0, 0, 0, 0)
            pywal_row.setSpacing(10)
            pywal_row.addWidget(self._follow_pywal_check)
            pywal_row.addWidget(
                self._info_button(
                    QCoreApplication.translate("SettingsDialog", "Follow pywal / wallust"),
                    QCoreApplication.translate(
                        "SettingsDialog",
                        "pywal / wallust are tools that build themes from your wallpaper.",
                    ),
                )
            )
            pywal_row.addStretch(1)
            box.addSpacing(4)
            box.addLayout(pywal_row)
        else:
            self._follow_pywal_check = None

        return box

    def _theme_button_width(self) -> int:
        """One width for the Import + glass Default ghost buttons so they right-
        align (both sit at family_w + spacing from the shared left edge)."""
        from PySide6.QtGui import QFontMetrics

        fm = QFontMetrics(font(TYPE_BODY))
        text = max(
            fm.horizontalAdvance(QCoreApplication.translate("SettingsDialog", "Import…")),
            fm.horizontalAdvance(QCoreApplication.translate("SettingsDialog", "Default")),
        )
        return text + 2 * 14 + 2  # ghost padding (6px 14px) + 1px border each side

    def _glass_is_preset(self) -> bool:
        return self.s.theme_family not in ("", "jellytoast")

    def _build_glass_opacity_row(self) -> QVBoxLayout:
        """The 'Glass opacity' slider for the active frosted family — deeper reads
        truer to the theme's colour, airier lets more wallpaper/blur through. A
        Default button restores the family's own default (jellytoast airier,
        presets deeper). Live-previews on drag (bodies repaint from
        body_color_tuple) and commits a full re-stamp when the drag settles."""
        from jellytoast.theme import _glass_alpha, get_active_theme

        is_preset = self._glass_is_preset()
        dark = get_active_theme().dark
        override = self.s.preset_glass_alpha if is_preset else self.s.jellytoast_glass_alpha
        eff = _glass_alpha(dark, is_preset, override)

        wrap = QVBoxLayout()
        wrap.setContentsMargins(0, 0, 0, 0)
        wrap.setSpacing(4)

        # Header with the % readout right next to the label (not pushed right).
        head = QHBoxLayout()
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        head.addWidget(
            self._section_header(QCoreApplication.translate("SettingsDialog", "Glass opacity"))
        )
        self._glass_readout = QLabel()
        self._glass_readout.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)}")
        head.addWidget(self._glass_readout)
        head.addStretch(1)
        wrap.addLayout(head)

        # Settle timer coalesces the heavy full re-stamp to ~once after the drag
        # stops (mouse OR keyboard); the drag itself only cheap-repaints bodies.
        self._glass_settle = QTimer(self)
        self._glass_settle.setSingleShot(True)
        self._glass_settle.setInterval(120)
        self._glass_settle.timeout.connect(self._commit_glass_alpha)

        self._glass_slider = QSlider(Qt.Orientation.Horizontal)
        self._glass_slider.setRange(self._GLASS_MIN, self._GLASS_MAX)
        self._glass_slider.setValue(max(self._GLASS_MIN, min(self._GLASS_MAX, eff)))
        self._glass_slider.setStyleSheet(self._horiz_slider_qss())
        self._glass_slider.valueChanged.connect(self._on_glass_alpha_changed)
        # Slider == family-combo width and the Default button == Import width, so
        # Default right-aligns with Import (a pinch of gap sits before it).
        self._glass_slider.setFixedWidth(self._THEME_COMBO_W)
        srow = QHBoxLayout()
        srow.setContentsMargins(0, 0, 0, 0)
        srow.setSpacing(10)
        srow.addWidget(self._glass_slider)
        reset_btn = QPushButton(QCoreApplication.translate("SettingsDialog", "Default"))
        reset_btn.setObjectName("ghost")
        reset_btn.setFixedWidth(getattr(self, "_theme_btn_w", self._theme_button_width()))
        reset_btn.setToolTip(
            QCoreApplication.translate(
                "SettingsDialog", "Reset glass opacity to this theme's default"
            )
        )
        reset_btn.clicked.connect(self._reset_glass_alpha)
        srow.addWidget(reset_btn)
        srow.addStretch(1)
        wrap.addLayout(srow)

        self._update_glass_readout(self._glass_slider.value())
        return wrap

    def _update_glass_readout(self, val: int) -> None:
        # The true body opacity (alpha / 255), which reads intuitively —
        # 172 → 67 %, 205 → 80 %, 140 → 55 %.
        if hasattr(self, "_glass_readout"):
            self._glass_readout.setText(f"{round(val / 255 * 100)}%")

    def _on_glass_alpha_changed(self, val: int) -> None:
        # Persist to the active family's own key + live-preview: the painted
        # bodies read body_color_tuple() live in paintEvent, so a plain repaint of
        # the top-levels shows the new alpha without the cost of a full token
        # re-stamp. The heavy re-stamp (for any surface that caches its body
        # colour) fires once the drag settles.
        from PySide6.QtWidgets import QApplication

        if self._glass_is_preset():
            self.s.preset_glass_alpha = int(val)
        else:
            self.s.jellytoast_glass_alpha = int(val)
        self._update_glass_readout(val)
        app = QApplication.instance()
        if app is not None:
            for w in app.topLevelWidgets():
                w.update()
        self._glass_settle.start()

    def _reset_glass_alpha(self) -> None:
        # Clear the active family's override (→ its built-in default) and snap the
        # slider there without re-firing the handler, then commit a full re-stamp.
        from jellytoast.theme import _glass_alpha, get_active_theme

        is_preset = self._glass_is_preset()
        if is_preset:
            self.s.preset_glass_alpha = 0
        else:
            self.s.jellytoast_glass_alpha = 0
        default = _glass_alpha(get_active_theme().dark, is_preset, 0)
        if hasattr(self, "_glass_slider"):
            self._glass_slider.blockSignals(True)
            self._glass_slider.setValue(max(self._GLASS_MIN, min(self._GLASS_MAX, default)))
            self._glass_slider.blockSignals(False)
            self._update_glass_readout(self._glass_slider.value())
        self._commit_glass_alpha()

    def _flush_pending_glass_settle(self) -> None:
        """If a glass-opacity settle is still pending at dialog close, run the
        re-stamp now (the persisted alpha is already correct; this just refreshes
        the surfaces that cache their body colour)."""
        settle = getattr(self, "_glass_settle", None)
        if settle is not None and settle.isActive():
            settle.stop()
            self._commit_glass_alpha()

    def _commit_glass_alpha(self) -> None:
        from jellytoast import icons as _icons
        from jellytoast import ui_helpers as _uih

        with _uih.theme_swap_guard():
            _uih.refresh_theme()
            _icons.refresh_theme()
            PlayerBus.get().theme_changed.emit()

    def _family_items(self) -> list[tuple[str, str, object]]:
        """(label, family-key, palette-strip icon) for the family dropdown —
        jellytoast first, then the preset families, plus the active imported
        scheme (if any) as a trailing entry."""
        from jellytoast.external_theme import list_folder_schemes
        from jellytoast.theme_presets import (
            FAMILY_ORDER,
            family_label,
            imported_preset_from_settings,
        )

        items: list[tuple[str, str, object]] = [
            ("jellytoast", "jellytoast", self._family_strip_icon("jellytoast"))
        ]
        for key in FAMILY_ORDER:
            items.append((family_label(key), key, self._family_strip_icon(key)))
        # Schemes dropped into the watched themes folder ride along after the
        # curated families, keyed by their file path.
        folder_paths = set()
        for path, preset in list_folder_schemes():
            folder_paths.add(path)
            items.append((preset.name, f"file:{path}", self._palette_icon(preset)))
        if self.s.theme_family == "imported" and (self.s.imported_scheme_path not in folder_paths):
            # One-off import (or pywal) — not represented by a folder row.
            imp = imported_preset_from_settings()
            label = (
                imp.name if imp else QCoreApplication.translate("SettingsDialog", "Imported scheme")
            )
            ic = self._palette_icon(imp) if imp else None
            items.append((label, "imported", ic))
        return items

    def _family_strip_icon(self, key: str):
        """A little colour strip previewing a family in the dropdown. jellytoast
        uses the built-in dark theme + current accent; a preset family uses its
        representative (dark) member's base16 spread."""
        if key in ("", "jellytoast"):
            from jellytoast.theme import FROSTED_DARK
            from jellytoast.ui_helpers import ACCENT as _ACCENT_NOW

            return self._strip_icon([FROSTED_DARK.bg, _ACCENT_NOW, "#7c66d0", "#ffffff", "#a8a8a8"])
        from jellytoast.theme_presets import THEME_FAMILIES

        fam = THEME_FAMILIES.get(key)
        member = fam.member_for("dark") if fam else None
        if member is None:
            return None
        return self._palette_icon(member)

    def _current_member_base16(self) -> dict | None:
        """The base16 palette of the currently-resolved preset member (for the
        preview grid), or None for jellytoast / a missing import."""
        fam_key = self.s.theme_family
        if fam_key in ("", "jellytoast"):
            return None
        if fam_key == "imported":
            from jellytoast.theme_presets import imported_preset_from_settings

            imp = imported_preset_from_settings()
            return imp.base16 if imp else None
        from jellytoast.theme import os_color_scheme
        from jellytoast.theme_presets import THEME_FAMILIES

        fam = THEME_FAMILIES.get(fam_key)
        if fam is None:
            return None
        lum = os_color_scheme() if self.s.theme_mode == "auto" else self.s.theme_mode
        member = fam.member_for(lum)
        return member.base16 if member else None

    def _base16_grid(self, base16: dict) -> QWidget:
        """A 2×8 grid of the 16 base16 swatches — the scheme's palette preview."""
        from PySide6.QtGui import QColor, QPainter, QPainterPath

        slots = [f"base0{c}" for c in "0123456789ABCDEF"]
        cols = [base16.get(s, "#000000") for s in slots]

        class _Grid(QWidget):
            CELL = 24
            GAP = 4
            COLS = 8

            def __init__(self, colours):
                super().__init__()
                self._cols = colours
                rows = 2
                self.setFixedSize(
                    self.COLS * self.CELL + (self.COLS - 1) * self.GAP,
                    rows * self.CELL + (rows - 1) * self.GAP,
                )

            def paintEvent(self, _e):
                p = QPainter(self)
                p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
                for i, hexc in enumerate(self._cols):
                    r, c = divmod(i, self.COLS)
                    x = c * (self.CELL + self.GAP)
                    y = r * (self.CELL + self.GAP)
                    path = QPainterPath()
                    path.addRoundedRect(x, y, self.CELL, self.CELL, rad(5), rad(5))
                    p.fillPath(path, QColor(hexc))
                    p.setPen(QColor(*ink_rgb(), 40))
                    p.drawPath(path)

        return _Grid(cols)

    def _on_family_changed(self) -> None:
        key = self._family_combo.currentData() or "jellytoast"
        active = self.s.theme_family or "jellytoast"
        if active == "imported" and self.s.imported_scheme_path:
            active = f"file:{self.s.imported_scheme_path}"
        if key == active:
            return
        if key.startswith("file:"):
            self._apply_folder_scheme(key[len("file:") :])
            return
        from jellytoast.theme_presets import family_has_both

        # A dark-only family can't honour light/auto — pin it to dark; otherwise
        # carry the current luminance intent into the new family.
        if key not in ("", "jellytoast") and not family_has_both(key):
            new_mode = "dark"
        else:
            new_mode = self.s.theme_mode
        # Applying a preset imports a whole palette (illegibility risk) → guard
        # with the keep/revert prompt; switching to jellytoast just resets.
        self._apply_axes_live(key, new_mode, self.s.frosted, prompt=key not in ("", "jellytoast"))
        # Returning to the built-in family with Follow-system-accent on: the
        # live watcher only fires on OS-side changes, so re-read + re-apply the
        # OS accent now instead of lingering on the preset's accent until the
        # next launch. (apply_axes_live left accent_color as the user's, which
        # for a preset was the preset's — resync overwrites it.)
        if key in ("", "jellytoast") and self.s.follow_system_accent:
            from jellytoast.system_accent import resync_system_accent

            resync_system_accent()

    def _on_mode_changed(self) -> None:
        combo = getattr(self, "_mode_combo", None)
        if combo is None:
            return
        new_mode = combo.currentData() or "dark"
        if new_mode == self.s.theme_mode:
            return
        fam = self.s.theme_family
        # On a preset a mode change swaps the member palette (revert-guarded);
        # on jellytoast it just recomposes the base theme (no prompt).
        self._apply_axes_live(fam, new_mode, self.s.frosted, prompt=fam not in ("", "jellytoast"))

    def _on_frosted_toggled(self, on: bool) -> None:
        on = bool(on)
        if on == self.s.frosted:
            return
        # Frosted/Opaque changes only the body opacity (base theme name), not the
        # luminance or which controls show — restamp live, no rebuild, no prompt.
        from jellytoast.theme import _resolve_base_name
        from jellytoast.theme_presets import apply_theme_family

        self._last_theme_name = _resolve_base_name(self.s.theme_mode, on, self.s.theme_family)
        apply_theme_family(self.s.theme_family, self.s.theme_mode, on)
        self._update_blur_hint()
        # Any family gains / loses the Glass opacity slider with Frosted → rebuild
        # so its visibility re-evaluates.
        QTimer.singleShot(0, self._rebuild_pages_for_theme)

    def _apply_axes_live(self, family: str, mode: str, frosted: bool, *, prompt: bool) -> None:
        """Apply a family/mode/frosted change live, back up every axis for a
        clean 10s keep/revert (when ``prompt``), and rebuild the page so the
        conditional controls (Mode / accent row / palette preview) re-evaluate."""
        from jellytoast import color_tokens as ct
        from jellytoast.theme import _resolve_base_name
        from jellytoast.theme_presets import apply_theme_family

        backup = (
            self.s.theme_family,
            self.s.theme_mode,
            self.s.frosted,
            self.s.accent_color,
            self.s.last_preset_name,
            ct.export_palette(),
            self.s.imported_scheme_path,
        )
        # An explicit family/mode pick detaches the pywal follow AND the
        # followed folder file — otherwise the next wallpaper change / file
        # edit silently clobbers the user's choice.
        self.s.follow_pywal = False
        self.s.imported_scheme_path = ""
        # Pre-set the resolved name so the external-change watcher treats this
        # in-dialog pick as handled and doesn't schedule a second rebuild.
        self._last_theme_name = _resolve_base_name(mode, frosted, family)
        apply_theme_family(family, mode, frosted)
        if prompt:
            from jellytoast.appearance_confirm import show_appearance_revert

            show_appearance_revert(lambda b=backup: self._revert_theme_axes(b), seconds=10)
        QTimer.singleShot(0, self._rebuild_pages_for_theme)

    def _revert_theme_axes(self, backup) -> None:
        """Restore every theme axis + the full token palette captured before an
        apply. Reused by the 10s keep/revert prompt."""
        from jellytoast import color_tokens as ct
        from jellytoast import icons as _icons
        from jellytoast import ui_helpers as _uih
        from jellytoast.theme import _resolve_base_name

        family, mode, frosted, accent, lpn, palette, scheme_path = backup
        self.s.theme_family = family
        self.s.imported_scheme_path = scheme_path
        self.s.theme_mode = mode
        self.s.frosted = frosted
        self.s.accent_color = accent
        self.s.last_preset_name = lpn
        if family in ("", "jellytoast"):
            ct.reset_all()  # no preset overrides — accent_color drives the accent
        else:
            ct.import_palette(palette)
        self._last_theme_name = _resolve_base_name(mode, frosted, family)
        with _uih.theme_swap_guard():
            _uih.refresh_theme()
            _icons.refresh_theme()
            PlayerBus.get().theme_changed.emit()
        QTimer.singleShot(0, self._rebuild_pages_for_theme)

    def _palette_icon(self, preset, w: int = 52, h: int = 16):
        """A little rounded colour strip previewing a scheme — background, its
        accent, then a spread of its base16 hues — shown beside the name in the
        family dropdown (both the row and the closed button)."""
        return self._strip_icon(
            [
                preset.base16["base00"],
                preset.base16[preset.accent_slot],
                preset.base16["base08"],
                preset.base16["base0B"],
                preset.base16["base0D"],
            ],
            w,
            h,
        )

    def _strip_icon(self, cols: list[str], w: int = 52, h: int = 16):
        """A rounded horizontal strip of colour bands (one per hex in ``cols``),
        returned as a QIcon — the preview shown beside a family/preset name."""
        from PySide6.QtCore import QRectF
        from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPixmap

        pm = QPixmap(w, h)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            path_rect = QRectF(0.5, 0.5, w - 1, h - 1)
            clip = QPainterPath()
            r = rad(3)  # honour the square-corners toggle like every other chrome
            clip.addRoundedRect(path_rect, r, r)
            p.setClipPath(clip)
            n = max(1, len(cols))
            bw = w / n
            for i, c in enumerate(cols):
                p.fillRect(QRectF(i * bw, 0, bw + 1, h), QColor(c))
        finally:
            p.end()
        return QIcon(pm)

    def _on_import_theme(self) -> None:
        """Import a base16 .yaml scheme and apply it live (keep/revert-guarded).
        Uses the scheme's conventional accent (base0D); persisted as an
        ``imported`` family so its preview grid + body tint survive a restart."""
        from PySide6.QtWidgets import QFileDialog, QMessageBox

        from jellytoast import color_tokens as ct
        from jellytoast.appearance_confirm import show_appearance_revert
        from jellytoast.external_theme import Base16ParseError, parse_base16_yaml
        from jellytoast.theme import _resolve_base_name
        from jellytoast.theme_presets import apply_imported_preset

        path, _ = QFileDialog.getOpenFileName(
            self,
            QCoreApplication.translate("SettingsDialog", "Import a base16 theme"),
            "",
            QCoreApplication.translate(
                "SettingsDialog", "base16 schemes (*.yaml *.yml);;All files (*)"
            ),
        )
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                preset = parse_base16_yaml(f.read())
        except (OSError, UnicodeDecodeError, Base16ParseError) as exc:
            QMessageBox.warning(
                self,
                QCoreApplication.translate("SettingsDialog", "Import failed"),
                QCoreApplication.translate(
                    "SettingsDialog", "Couldn't read that as a base16 colour scheme:\n\n{0}"
                ).format(exc),
            )
            return

        backup = (
            self.s.theme_family,
            self.s.theme_mode,
            self.s.frosted,
            self.s.accent_color,
            self.s.last_preset_name,
            ct.export_palette(),
            self.s.imported_scheme_path,
        )
        variant = "light" if preset.variant == "light" else "dark"
        self.s.follow_pywal = False  # a hand-picked import detaches the follow
        self.s.imported_scheme_path = ""  # one-off import: nothing to follow
        self._last_theme_name = _resolve_base_name(variant, self.s.frosted, "imported")
        apply_imported_preset(preset, self.s.frosted)
        show_appearance_revert(lambda b=backup: self._revert_theme_axes(b), seconds=10)
        QTimer.singleShot(0, self._rebuild_pages_for_theme)

    def _apply_folder_scheme(self, path: str) -> None:
        """Apply a scheme picked from the watched themes folder (a ``file:``
        row in the family dropdown) — like a hand import, but the source path
        is remembered so the folder watcher re-applies edits live."""
        from jellytoast import color_tokens as ct
        from jellytoast.appearance_confirm import show_appearance_revert
        from jellytoast.external_theme import (
            Base16ParseError,
            ensure_readable,
            parse_base16_yaml,
        )
        from jellytoast.theme import _resolve_base_name
        from jellytoast.theme_presets import apply_imported_preset

        try:
            with open(path, encoding="utf-8") as f:
                preset = ensure_readable(parse_base16_yaml(f.read()))
        except (OSError, UnicodeDecodeError, Base16ParseError) as exc:
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.warning(
                self,
                QCoreApplication.translate("SettingsDialog", "Couldn't apply that theme"),
                QCoreApplication.translate(
                    "SettingsDialog", "Couldn't read {0} as a base16 colour scheme:\n\n{1}"
                ).format(path, exc),
            )
            QTimer.singleShot(0, self._rebuild_pages_for_theme)  # reset the combo
            return

        backup = (
            self.s.theme_family,
            self.s.theme_mode,
            self.s.frosted,
            self.s.accent_color,
            self.s.last_preset_name,
            ct.export_palette(),
            self.s.imported_scheme_path,
        )
        variant = "light" if preset.variant == "light" else "dark"
        self.s.follow_pywal = False  # an explicit pick detaches the pywal follow
        self._last_theme_name = _resolve_base_name(variant, self.s.frosted, "imported")
        apply_imported_preset(preset, self.s.frosted)
        self.s.imported_scheme_path = path  # after apply — it's path-agnostic
        show_appearance_revert(lambda b=backup: self._revert_theme_axes(b), seconds=10)
        QTimer.singleShot(0, self._rebuild_pages_for_theme)

    def _on_follow_pywal_toggled(self, on: bool) -> None:
        """Persist the toggle; when enabled, apply the current pywal palette
        right away (the watcher handles subsequent wallpaper changes). If no
        colors.json exists, tell the user and flip the toggle back."""
        self.s.follow_pywal = bool(on)
        if not on:
            return
        from jellytoast.theme_watcher import pywal_apply_once

        def _missing(msg: str) -> None:
            import shiboken6

            self.s.follow_pywal = False
            if not shiboken6.isValid(self):
                return
            chk = getattr(self, "_follow_pywal_check", None)
            if chk is not None and shiboken6.isValid(chk):
                chk.blockSignals(True)
                chk.setChecked(False)
                chk.blockSignals(False)
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.information(
                self,
                QCoreApplication.translate("SettingsDialog", "pywal palette not found"),
                QCoreApplication.translate(
                    "SettingsDialog",
                    "{0}\n\nRun pywal (or wallust) once so it generates a "
                    "palette, then turn this back on.",
                ).format(msg),
            )

        pywal_apply_once(on_missing=_missing)

    def _on_follow_system_accent_toggled(self, on: bool) -> None:
        """Persist the toggle; when enabled, read the desktop accent off a worker
        (portal D-Bus) and apply it via the accent path. If the DE reports none,
        surface a hint and switch the toggle back off."""
        self.s.follow_system_accent = bool(on)
        if not on:
            return
        from jellytoast.async_io import run_async
        from jellytoast.system_accent import read_system_accent

        def _got(hex_color):  # on the GUI thread (run_async pins the signaler)
            if hex_color:
                self._on_accent_picked(hex_color)  # applies + persists + drops any preset
                return
            from PySide6.QtWidgets import QMessageBox

            QMessageBox.information(
                self,
                QCoreApplication.translate("SettingsDialog", "No system accent"),
                QCoreApplication.translate(
                    "SettingsDialog",
                    "Your desktop didn't report an accent colour — this works "
                    "on KDE Plasma and GNOME 47+.",
                ),
            )
            self._follow_accent_check.blockSignals(True)
            self._follow_accent_check.setChecked(False)
            self._follow_accent_check.blockSignals(False)
            self.s.follow_system_accent = False

        run_async(read_system_accent, on_result=_got, on_error=lambda _e: None)

    def _build_accent_row(self) -> QHBoxLayout:
        """Row of color swatches matching ACCENT_PRESETS. Selecting one
        writes the hex into ``settings.accent_color`` and surfaces the
        same restart-required notice the theme dropdown uses (theme +
        accent are baked at module-import time today).
        """
        from jellytoast.theme import ACCENT_PRESETS as _PRESETS

        row = QHBoxLayout()
        row.setContentsMargins(0, 4, 0, 0)
        row.setSpacing(10)
        self._accent_buttons: list[tuple[str, QPushButton]] = []
        current = (self.s.accent_color or "").strip().lower()
        for label, hex_value in _PRESETS:
            btn = _AccentSwatch(hex_value)
            btn.setToolTip(label)
            btn.setChecked(hex_value.lower() == current)
            btn.clicked.connect(lambda _=False, h=hex_value: self._on_accent_picked(h))
            self._accent_buttons.append((hex_value, btn))
            row.addWidget(btn, 0, Qt.AlignmentFlag.AlignLeft)
        # Eyedropper swatch — a circle in line with the preset swatches that
        # samples any colour on screen as a custom accent. Empty until used,
        # then it holds the sampled colour. Applies via the same
        # _on_accent_picked path (so ACCENT_DEEP + BORDER_ACCENT derive from it).
        self._accent_dropper = _EyedropperSwatch()
        self._accent_dropper.clicked.connect(self._pick_accent_from_screen)
        # If the live accent isn't one of the presets, it's a custom colour —
        # show it loaded + selected in the dropper swatch.
        if current and not any(current == h.lower() for h, _ in self._accent_buttons):
            self._accent_dropper.set_picked(current, checked=True)
        row.addWidget(self._accent_dropper, 0, Qt.AlignmentFlag.AlignLeft)
        row.addStretch(1)
        return row

    def _pick_accent_from_screen(self):
        """Eyedropper: sample a colour from anywhere on screen and apply it as
        the accent — same path as picking a preset swatch, so ACCENT_DEEP +
        BORDER_ACCENT derive from the sampled colour (theme.refresh_theme)."""
        from jellytoast.color_picker import pick_screen_color

        pick_screen_color(self._on_accent_picked, parent=self)

    def _on_accent_picked(self, hex_value: str):
        # The accent row only shows for the jellytoast family, so an accent pick
        # is always applied on the built-in theme — no preset to drop here.
        # 1. Persist the pick.
        self.s.accent_color = hex_value
        # 1a. Clear every accent-derived override set via Settings →
        #     Colors. ACCENT_DEEP (checkbox fills) + BORDER_ACCENT
        #     (focus rings) are conceptually downstream of ACCENT —
        #     picking a fresh preset means "I want this preset's whole
        #     accent family". Without this, the user picks purple but
        #     checkbox fills + focus rings stay at the previously-
        #     saved override (green) because refresh_theme's
        #     load_persisted_overlays re-applies them.
        from jellytoast.settings_migration import open_qsettings

        _qs = open_qsettings()
        for _tok in ("ACCENT", "ACCENT_DEEP", "BORDER_ACCENT"):
            _qs.remove(f"debug/colors/{_tok}")
        # 2. Refresh module-level theme constants (the ACCENT family) +
        #    rebuild GLOBAL_STYLE + clear the ICON_ACCENT cache, so every later
        #    read of `ui_helpers.ACCENT` / `accent_icon(name)` — and the
        #    theme_changed handlers below — see the new colour.
        from jellytoast import icons as _icons
        from jellytoast import ui_helpers as _uih

        _uih.refresh_theme()
        _icons.refresh_theme()
        # 3. Reflect the pick in the swatch ring / eyedropper.
        self._refresh_accent_swatches(hex_value)
        # 4. Broadcast — and let the SINGLE theme_changed path do all the live
        #    work: the main window's `_cascade_global_style` re-stamps the app
        #    (GLOBAL_STYLE + palette + a deferred, visible-only checkbox
        #    repolish) and this dialog's `_reapply_dialog_accent_styling`
        #    (connected to the same signal) rebuilds its accent-baked QSS.
        #    An earlier inline `app.setStyleSheet` + a *synchronous*
        #    `allWidgets()` checkbox walk + an explicit dialog restyle ran here
        #    too, fully DUPLICATING the signal handlers — ~3 whole-app
        #    re-polishes per pick, one of them blocking. Dropping them is the
        #    "make accent switching instant" cut.
        PlayerBus.get().theme_changed.emit()
        # 7. Theme + font_scale are still bake-at-import-time changes;
        #    keep the restart notice visible if any of those differ
        #    from the initial values when the dialog was opened.
        self._refresh_restart_notice_visibility()

    def _refresh_accent_swatches(self, hex_value: str) -> None:
        """Sync the accent preset rings + the eyedropper swatch to ``hex_value``:
        the matching preset (if any) reads selected, else the eyedropper shows
        the custom colour — so it acts as a live indicator of the active accent,
        including a "Follow system accent" update that lands from OUTSIDE the
        dialog. No-op when the accent row isn't built (non-jellytoast family, or
        the Display page hasn't been visited yet)."""
        import shiboken6

        buttons = getattr(self, "_accent_buttons", None)
        if not buttons:
            return
        cur = (hex_value or "").strip().lower()
        matched = False
        for h, btn in buttons:
            # theme_changed can fire after a page rebuild tore these swatches
            # down (e.g. a family switch), leaving _accent_buttons pointing at
            # deleted C++ objects — touching one raises RuntimeError. Skip them.
            if not shiboken6.isValid(btn):
                continue
            m = h.lower() == cur
            btn.set_accent_state(h, m)
            matched = matched or m
        dropper = getattr(self, "_accent_dropper", None)
        if dropper is not None and shiboken6.isValid(dropper):
            if matched or not cur:
                dropper.setChecked(False)
                dropper.update()
            else:
                dropper.set_picked(cur, checked=True)
