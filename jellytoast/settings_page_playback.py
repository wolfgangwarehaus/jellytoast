"""Settings dialog — the Playback (crossfade, EQ, ReplayGain, bit-perfect, PipeWire) page.

Split out of ``settings_dialog.py``: a mixin of ``SettingsDialog`` (it uses
the dialog's widgets, settings handle and shared helpers via ``self``).
Strings use ``QCoreApplication.translate("SettingsDialog", …)`` rather than
``self.tr`` so they keep the SettingsDialog translation context — lupdate
files a mixin's ``self.tr`` under the MIXIN's name, which never matches at
runtime.
"""

from PySide6.QtCore import QCoreApplication, Qt
from PySide6.QtWidgets import (
    QFormLayout,
    QFrame,
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
    rad,
    type_qss,
)
from jellytoast.player_state import PlayerBus
from jellytoast.selector import (
    Selector as _Selector,  # noqa: E402  (deliberate post-class import for layout)
)
from jellytoast.settings_widgets import (
    AUDIO_QUALITIES,
    REPLAYGAIN_MODES,
    WrapCheckBox,
    audio_family_dot_color,
    bit_perfect_path_hint,
)
from jellytoast.ui_helpers import (
    DISABLED_FG,
    TEXT,
    TEXT_DIM,
    TEXT_FAINT,
    ink_alpha,
)


class _PlaybackPageMixin:
    # ── Page: Playback ─────────────────────────────────────────────────
    def _build_playback(self) -> QWidget:
        # Trimmed 2026-05-11 — section headers ("Streaming",
        # "ReplayGain", "Behavior") and the long descriptive paragraphs
        # were removed in favour of a single tidy form. ReplayGain is
        # surfaced as "Normalization" for users who don't know the
        # tag-spec name. Gapless playback and smart shuffle were once
        # toggleable here; both are now always-on (no setting), so the
        # page is one form + a couple of behaviour rows.
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        # Tighter rhythm than the other pages (10 px vs 12) — the
        # Playback page packs four sections (form / crossfade / EQ /
        # shuffle) into the dialog's fixed height. Section headers
        # already give visual chunking; the extra 2 px between
        # siblings wasn't load-bearing.
        v.setSpacing(10)

        # ── Audio output ─────────────────────────────────────────────
        # Output-device pin (mpv --audio-device): Auto by default, or
        # any device mpv enumerates — PipeWire/Pulse sinks, WASAPI
        # endpoints, raw ALSA hw: for the direct audiophile path. The
        # list comes from the LIVE player handle; before playback init
        # (rare — the dialog opens post-boot) the picker offers Auto +
        # the persisted value. See docs/research/audio_output_routing.md.
        v.addWidget(
            self._section_header(QCoreApplication.translate("SettingsDialog", "AUDIO OUTPUT"))
        )
        out_row = QHBoxLayout()
        out_row.setContentsMargins(0, 0, 0, 0)
        out_row.setSpacing(10)
        self._audio_out_combo = _Selector()
        self._audio_out_combo.addItem(
            QCoreApplication.translate("SettingsDialog", "Auto (recommended)"), "auto"
        )
        current_out = self.s.audio_output_device
        listed = {"auto"}
        for name, desc in self._audio_device_choices():
            if name in listed or name == "auto":
                continue
            listed.add(name)
            self._audio_out_combo.addItem(
                desc or name, name, dot_color=audio_family_dot_color(name)
            )
        if current_out not in listed:
            # Persisted device that isn't enumerable right now (USB DAC
            # unplugged, or no live handle yet) — keep it selectable so
            # opening the dialog never silently rewrites the choice.
            self._audio_out_combo.addItem(
                QCoreApplication.translate("SettingsDialog", "{0} (not connected)").format(
                    current_out
                ),
                current_out,
                dot_color=audio_family_dot_color(current_out),
            )
        self._select_combo_by_data(self._audio_out_combo, current_out)
        self._audio_out_combo.currentIndexChanged.connect(self._on_audio_output_changed)
        out_row.addWidget(self._audio_out_combo)
        out_row.addWidget(
            self._info_button(
                QCoreApplication.translate("SettingsDialog", "Audio output"),
                QCoreApplication.translate(
                    "SettingsDialog",
                    "Where audio goes. Auto follows the system default. Direct "
                    "ALSA devices bypass the PipeWire mixer — the bit-perfect "
                    "path — but hold the device exclusively. Applies on the "
                    "next track; falls back to Auto if the device won't open.",
                ),
            )
        )
        out_row.addStretch(1)
        v.addLayout(out_row)
        # (No standalone ALSA-consequences box here — the BIT-PERFECT
        # legend's ALSA line owns the exclusivity message, and the
        # visualizer widget shows its own caption when it has no stream
        # to tap. Removed 2026-06-11 at august's request.)

        # ── Bit-perfect mode ─────────────────────────────────────────
        # Master "no DSP, no resample, no attenuation" lock. When on:
        # Normalization, EQ, Crossfade, and the volume slider are all
        # gated to their bit-perfect-safe values. Sits at the top of
        # the page because it overrides the controls below it; the
        # surfaces it gates listen to PlayerBus.bit_perfect_changed.
        # See `docs/research/bit_perfect_playback.md` §7 (T2) and the
        # user-facing `docs/bit_perfect.md` for the PipeWire side.
        v.addWidget(
            self._section_header(QCoreApplication.translate("SettingsDialog", "BIT-PERFECT"))
        )
        bp_row = QHBoxLayout()
        bp_row.setContentsMargins(0, 0, 0, 0)
        bp_row.setSpacing(10)
        self._bit_perfect_check = WrapCheckBox(
            QCoreApplication.translate("SettingsDialog", "Bit-perfect mode")
        )
        self._bit_perfect_check.setChecked(self.s.bit_perfect_mode)
        self._bit_perfect_check.toggled.connect(self._on_bit_perfect_toggled)
        bp_row.addWidget(self._bit_perfect_check)
        bp_row.addWidget(
            self._info_button(
                QCoreApplication.translate("SettingsDialog", "Bit-perfect mode"),
                QCoreApplication.translate(
                    "SettingsDialog",
                    "Plays the file's exact bits: locks volume at 100% and turns "
                    "off Normalization, Equalizer, and Crossfade. The hint below "
                    "covers the OS side of the path.",
                ),
            )
        )
        bp_row.addStretch(1)
        v.addLayout(bp_row)

        # Exclusive output — T3 sub-toggle. Indented (a sub-option of
        # bit-perfect) and only meaningful when the parent is on. The
        # parent gate's _refresh_bit_perfect_gating handler enables /
        # disables this row in lockstep.
        #
        # HIDDEN ON LINUX (2026-06-11): mpv's PipeWire exclusive mode
        # failed every AO open on a real box — with the flag persisted
        # on, even plain Auto playback was dead on arrival. The alsa AO
        # ignores the flag entirely (ALSA-direct devices are exclusive
        # by nature), so on Linux the checkbox had no working backend —
        # only a trap. The persisted setting is force-cleared here so a
        # previously-armed config can't keep poisoning opens invisibly.
        from jellytoast.platform_compat import IS_LINUX as _IS_LINUX

        if _IS_LINUX:
            if self.s.audio_exclusive:
                self.s.audio_exclusive = False
                PlayerBus.get().audio_exclusive_changed.emit(False)
        else:
            excl_row = QHBoxLayout()
            excl_row.setContentsMargins(0, 0, 0, 0)  # aligned under Bit-perfect mode
            excl_row.setSpacing(10)
            self._audio_exclusive_check = WrapCheckBox(
                QCoreApplication.translate("SettingsDialog", "Exclusive output")
            )
            self._audio_exclusive_check.setChecked(self.s.audio_exclusive)
            self._audio_exclusive_check.toggled.connect(self._on_audio_exclusive_toggled)
            excl_row.addWidget(self._audio_exclusive_check)
            excl_row.addWidget(
                self._info_button(
                    QCoreApplication.translate("SettingsDialog", "Exclusive output"),
                    QCoreApplication.translate(
                        "SettingsDialog",
                        "Hands jellytoast the device (WASAPI Exclusive · "
                        "CoreAudio HogMode) — other apps go silent. Applies on "
                        "the next track; falls back to shared if the device "
                        "refuses.",
                    ),
                )
            )
            excl_row.addStretch(1)
            v.addLayout(excl_row)

        # Adaptive path hint — the streamlined "what's my next step"
        # line. State comes from bit_perfect_path_hint(); refreshed by
        # the bit-perfect gate, the output picker, the exclusive toggle,
        # and the PipeWire-config button.
        self._bp_path_hint = QLabel("")
        self._bp_path_hint.setWordWrap(True)
        self._bp_path_hint.setTextFormat(Qt.TextFormat.RichText)
        self._bp_path_hint.setStyleSheet(
            f"color: {TEXT_DIM}; "
            f"background: {ink_alpha(0.05)}; "
            f"border-radius: {rad(6)}px; "
            f"padding: 8px 12px; "
            f"{type_qss(TYPE_CAPTION)}"
        )
        self._bp_path_hint.setVisible(False)
        v.addWidget(self._bp_path_hint)

        # T4 — PipeWire conf installer. Linux-only; on other platforms
        # the whole row stays hidden. Drops a small conf file that lets
        # PipeWire follow the source sample rate (most CD-quality
        # material stops resampling 44.1 → 48 kHz). See
        # ``jellytoast.pipewire_setup`` for the helper and
        # ``docs/research/bit_perfect_playback.md`` §4.2 / §7 (T4).
        from jellytoast import pipewire_setup as _pws

        if _pws.is_supported():
            pw_row = QHBoxLayout()
            pw_row.setContentsMargins(0, 6, 0, 0)
            pw_row.setSpacing(10)
            self._pw_install_btn = QPushButton()
            self._pw_install_btn.clicked.connect(self._on_pipewire_install_clicked)
            pw_row.addWidget(self._pw_install_btn)
            pw_row.addWidget(
                self._info_button(
                    QCoreApplication.translate("SettingsDialog", "PipeWire sample-rate config"),
                    QCoreApplication.translate(
                        "SettingsDialog",
                        "Lets PipeWire follow the source sample rate so 44.1 kHz "
                        "files stop resampling to 48 kHz. Takes effect after "
                        "PipeWire restarts (or next login). Not needed on a "
                        "direct ALSA output.",
                    ),
                )
            )
            pw_row.addStretch(1)
            v.addLayout(pw_row)
            # Small inline status flash — populated by the click
            # handler after each install / remove so the user sees the
            # action took (without a modal interrupting their flow).
            self._pw_status_label = QLabel("")
            self._pw_status_label.setWordWrap(True)
            self._pw_status_label.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}")
            self._pw_status_label.setVisible(False)
            v.addWidget(self._pw_status_label)
            self._refresh_pipewire_button_label()

        # ── Audio ─────────────────────────────────────────────────────
        # Quality (transcode tier) + Normalization (ReplayGain mode)
        # belong together: both decide what the SOURCE stream looks like
        # before it hits the decoder. Header gives the page a consistent
        # rhythm with the Crossfade / Equalizer blocks below.
        v.addWidget(self._section_header(QCoreApplication.translate("SettingsDialog", "AUDIO")))

        # Single shared form so Quality and Normalization labels +
        # combo boxes column-align cleanly.
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        # Left-justify the whole form. macOS QFormLayout defaults to a CENTERED
        # Aqua form (Linux/Windows already default left), which floats Quality /
        # Normalization mid-dialog instead of at the content margin like the
        # section headers above them.
        form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(8)
        # Keep the combos at their size hint (grow with the font, don't stretch
        # to fill the field column) — same as the Display page's scaling_form.
        # Their setMinimumWidth floor then holds the tuned default-font width.
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)

        # Fixed combo width — both AUDIO combos match each other AND
        # the EQ Preset combo's fixed 180-px width below, so the three
        # combos read as one tidy stacked column of outlined chips
        # rather than two wide ones over a narrow one.
        _AUDIO_COMBO_W = 120
        # Fixed label-column width shared between the AUDIO form and
        # the EQ Preset row, so all three combos (Quality, Normalization,
        # Preset) align in one vertical column. Tuned to fit
        # "Normalization:" at TYPE_BODY with a few px of breathing room.
        # Stashed on self so _build_eq_section can reuse it.
        self._playback_label_w = 130

        q_label = self._field_label(QCoreApplication.translate("SettingsDialog", "Quality:"))
        q_label.setMinimumWidth(self._playback_label_w)
        self._quality_combo = _Selector()
        for label, key in AUDIO_QUALITIES:
            self._quality_combo.addItem(label, key)
        self._select_combo_by_data(self._quality_combo, self.s.audio_quality or "original")

        def _on_quality_changed():
            new_q = self._quality_combo.currentData() or "original"
            self.s.audio_quality = new_q
            # PlayerBackend listens to recompute the runtime bit-perfect
            # contract — switching to a transcoded tier breaks it
            # before the codec arrives on the next track.
            PlayerBus.get().audio_quality_changed.emit(new_q)

        self._quality_combo.currentIndexChanged.connect(lambda _: _on_quality_changed())
        self._quality_combo.setMinimumWidth(_AUDIO_COMBO_W)
        form.addRow(q_label, self._quality_combo)

        # ReplayGain is the canonical tag spec, but the user-facing
        # label reads as "Normalization" since that's the effect users
        # recognise (and what other players call it).
        n_label = self._field_label(QCoreApplication.translate("SettingsDialog", "Normalization:"))
        n_label.setMinimumWidth(self._playback_label_w)
        self._rg_combo = _Selector()
        for label, key in REPLAYGAIN_MODES:
            self._rg_combo.addItem(label, key)
        self._select_combo_by_data(self._rg_combo, self.s.replaygain)
        self._rg_combo.currentIndexChanged.connect(self._on_replaygain_changed)
        self._rg_combo.setMinimumWidth(_AUDIO_COMBO_W)
        # Short labels lose context; tooltips spell out the longer
        # description we used to bake into the label.
        self._rg_combo.setToolTip(
            QCoreApplication.translate(
                "SettingsDialog",
                "Normalization mode:\n"
                "  Off — play untouched.\n"
                "  Track — match each song to a common loudness target.\n"
                "  Album — preserve relative loudness within an album.",
            )
        )
        form.addRow(n_label, self._rg_combo)

        v.addLayout(form)
        v.addWidget(self._build_crossfade_section())

        # ── Shuffle ───────────────────────────────────────────────────
        # Lives on Playback rather than Library because it shapes
        # playback behaviour, not what the grid looks like. Single
        # combo — the queue-size cap — kept tight to match the page's
        # other label/combo rows. Sits between Crossfade and EQ so
        # the heaviest block (EQ — 11 sliders + header row) anchors
        # the bottom of the page.
        v.addWidget(self._section_header(QCoreApplication.translate("SettingsDialog", "SHUFFLE")))
        shuffle_form = QFormLayout()
        shuffle_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        shuffle_form.setFormAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop)
        shuffle_form.setHorizontalSpacing(16)
        shuffle_form.setVerticalSpacing(8)
        shuffle_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint)
        sq_label = self._field_label(QCoreApplication.translate("SettingsDialog", "Queue size:"))
        sq_label.setMinimumWidth(self._playback_label_w)
        self._shuffle_size_combo = _Selector()
        for label, key in (
            (QCoreApplication.translate("SettingsDialog", "50 tracks"), 50),
            (QCoreApplication.translate("SettingsDialog", "100 tracks"), 100),
            (QCoreApplication.translate("SettingsDialog", "250 tracks"), 250),
            (QCoreApplication.translate("SettingsDialog", "500 tracks"), 500),
            (QCoreApplication.translate("SettingsDialog", "1000 tracks"), 1000),
        ):
            self._shuffle_size_combo.addItem(label, key)
        self._select_combo_by_data(
            self._shuffle_size_combo,
            self.s.shuffle_queue_size,
        )
        self._shuffle_size_combo.currentIndexChanged.connect(
            lambda _: setattr(
                self.s,
                "shuffle_queue_size",
                int(self._shuffle_size_combo.currentData() or 100),
            )
        )
        self._shuffle_size_combo.setMinimumWidth(120)
        shuffle_form.addRow(sq_label, self._shuffle_size_combo)
        v.addLayout(shuffle_form)

        # Extra air before Equalizer — the EQ block is the visually
        # heaviest section on the page (11 sliders + a header row),
        # and a tighter sibling gap made it sit on top of whatever
        # is above. The form section above already has the default
        # 10 px from the page-level setSpacing.
        v.addSpacing(8)
        v.addWidget(self._build_eq_section())

        # ── Media keys / OS integration ───────────────────────────────
        # Toggle whether jellytoast registers with the desktop's media
        # controls (MPRIS widget + media keys on Linux; SMTC flyout +
        # hardware keys on Windows). The service spawns a dbus/asyncio
        # thread that isn't built for live restart, so it's honoured at
        # launch — hence the restart hint.
        v.addSpacing(8)
        v.addWidget(
            self._section_header(QCoreApplication.translate("SettingsDialog", "MEDIA KEYS"))
        )
        v.addSpacing(4)
        mk_row = QHBoxLayout()
        mk_row.setContentsMargins(0, 0, 0, 0)
        mk_row.setSpacing(10)
        self._media_integration_check = WrapCheckBox(
            QCoreApplication.translate("SettingsDialog", "OS media integration")
        )
        self._media_integration_check.setChecked(self.s.media_integration_enabled)
        self._media_integration_check.toggled.connect(
            lambda val: setattr(self.s, "media_integration_enabled", val)
        )
        mk_row.addWidget(self._media_integration_check)
        mk_row.addWidget(
            self._info_button(
                QCoreApplication.translate("SettingsDialog", "OS media integration"),
                QCoreApplication.translate(
                    "SettingsDialog",
                    "Exposes jellytoast to your desktop's media controls — the "
                    "MPRIS widget and media keys on Linux, the SMTC volume-flyout "
                    "and hardware media keys on Windows. Turn off to keep "
                    "jellytoast out of the OS media UI. Takes effect on restart.",
                ),
            )
        )
        mk_row.addStretch(1)
        v.addLayout(mk_row)

        v.addStretch(1)
        # Initial gating pass — if bit_perfect_mode was persisted as on,
        # apply the disabled state to every dependent now that they all
        # exist (EQ + Crossfade build inside this function so they were
        # nil when the section header was added at the top of the page).
        self._refresh_bit_perfect_gating()
        return page

    def _build_crossfade_section(self) -> QWidget:
        """Crossfade controls — enable toggle, fade-duration slider, and
        the same-album continuity escape hatch. Cast-greyed like the EQ
        section: crossfade ping-pongs two local mpv handles, so it has
        no effect on a cast device's own decoder."""
        wrap = QFrame()
        wrap.setObjectName("jtCrossfadeSection")
        wrap.setStyleSheet("QFrame#jtCrossfadeSection { background: transparent; border: none; }")
        wv = QVBoxLayout(wrap)
        wv.setContentsMargins(0, 0, 0, 0)
        wv.setSpacing(10)

        wv.addWidget(
            self._section_header(QCoreApplication.translate("SettingsDialog", "CROSSFADE"))
        )
        # Breathing room under the section header before the toggles —
        # without it, the small-caps header sits visually attached to
        # the first checkbox.
        wv.addSpacing(4)

        # Both toggles on one row: parent "Crossfade between tracks"
        # left, "Skip on albums" sibling right. Duration sits beneath
        # both as a sub-option of the parent (only meaningful when
        # crossfade is on; greyed via _refresh_xf_enabled_state).
        from jellytoast.platform_compat import is_mobile_shell as _mobile

        # Phone: stacked — side by side they need ~330 px in a wide font
        # (DejaVu), past a 360 px phone's page.
        toggle_row = QVBoxLayout() if _mobile() else QHBoxLayout()
        # 16→28: extra breathing room between the parent "Crossfade
        # between tracks" toggle and its "Skip on albums" sibling, so
        # the sub-option reads as a distinct choice rather than
        # crowding the parent label.
        toggle_row.setSpacing(6 if _mobile() else 28)
        self._xf_enabled = WrapCheckBox(
            QCoreApplication.translate("SettingsDialog", "Crossfade between tracks")
        )
        self._xf_enabled.setChecked(self.s.crossfade_enabled)
        self._xf_enabled.toggled.connect(self._on_xf_enabled_toggled)
        toggle_row.addWidget(self._xf_enabled)

        self._xf_smart_album = WrapCheckBox(
            QCoreApplication.translate("SettingsDialog", "Skip on albums")
        )
        self._xf_smart_album.setToolTip(
            QCoreApplication.translate(
                "SettingsDialog",
                "Skip the crossfade between tracks on the same album so "
                "albums that flow as one piece (live sets, concept records) "
                "stay seamless.",
            )
        )
        self._xf_smart_album.setChecked(self.s.crossfade_smart_album_continuity)
        self._xf_smart_album.toggled.connect(
            lambda val: setattr(self.s, "crossfade_smart_album_continuity", val)
        )
        toggle_row.addWidget(self._xf_smart_album)
        toggle_row.addStretch(1)
        wv.addLayout(toggle_row)
        # Air between the toggles and the duration slider — the
        # slider's visual weight (the accent rail) needs separation
        # from the checkboxes above or the row reads as one cluster.
        wv.addSpacing(8)

        # Duration — stored in ms (1000–10000), shown to the user in
        # seconds. The setting getter/setter clamp, so the slider range
        # only needs to match for a tidy UI.
        # Layout: label sits in the same column as Quality / Normalization
        # / Preset (min-width matches `_playback_label_w`, spacing 16
        # matches the AUDIO form's setHorizontalSpacing) so all four
        # labels and all four field-column starts line up vertically.
        # Right margin caps the slider at the same column as the EQ
        # band area below. With dialog 720 and nav 128, content
        # width ≈ 516. EQ grid 16k right edge sits ~482 px in; a
        # 46-px trailing pad leaves a 12-px breath inside the 16k
        # boundary and mirrors what the preset row below uses.
        from jellytoast.platform_compat import is_mobile_shell

        # mirrors preset_row's trailing pad (none on a phone or narrow window)
        narrow = getattr(self, "_compact", False) or is_mobile_shell()
        _CONTENT_RIGHT_PAD = 0 if narrow else 46
        dur_row = QHBoxLayout()
        dur_row.setContentsMargins(0, 0, _CONTENT_RIGHT_PAD, 0)
        dur_row.setSpacing(16)
        self._xf_duration_lbl = self._field_label(
            QCoreApplication.translate("SettingsDialog", "Duration:")
        )
        self._xf_duration_lbl.setMinimumWidth(self._playback_label_w)
        dur_row.addWidget(self._xf_duration_lbl)
        self._xf_duration = QSlider(Qt.Orientation.Horizontal)
        self._xf_duration.setRange(1000, 10000)
        self._xf_duration.setSingleStep(500)
        self._xf_duration.setPageStep(1000)
        self._xf_duration.setValue(self.s.crossfade_duration_ms)
        self._xf_duration.valueChanged.connect(self._on_xf_duration_changed)
        # Explicit slider QSS — without this the dialog falls back to
        # the native QStyle for the handle (a blue Fusion dot) instead
        # of the white-on-accent treatment GLOBAL_STYLE defines for
        # every other horizontal slider. Rebuilt on accent change via
        # _reapply_dialog_accent_styling.
        self._xf_duration.setStyleSheet(self._horiz_slider_qss())
        dur_row.addWidget(self._xf_duration, 1)
        self._xf_duration_label = QLabel()
        self._xf_duration_label.setStyleSheet(
            f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)} min-width: 42px;"
        )
        dur_row.addWidget(self._xf_duration_label)
        wv.addLayout(dur_row)

        self._xf_caption = QLabel("")
        self._xf_caption.setWordWrap(True)
        self._xf_caption.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)}")
        # Hidden by default — same logic as the EQ caption. Only shows
        # while a cast is active; reclaims the row's line height when
        # the message is empty.
        self._xf_caption.setVisible(False)
        wv.addWidget(self._xf_caption)

        # Cast-greying — crossfade is local-playback only.
        try:
            bus = PlayerBus.get()
            bus.cast_started.connect(self._on_xf_cast_active)
            bus.cast_stopped.connect(self._on_xf_cast_cleared)
        except Exception:
            pass

        self._update_xf_duration_label()
        self._refresh_xf_enabled_state()
        return wrap

    def _on_xf_enabled_toggled(self, on: bool):
        self.s.crossfade_enabled = on
        self._refresh_xf_enabled_state()
        PlayerBus.get().crossfade_changed.emit(bool(on))

    def _on_xf_duration_changed(self, ms: int):
        self.s.crossfade_duration_ms = ms
        self._update_xf_duration_label()

    def _update_xf_duration_label(self):
        self._xf_duration_label.setText(
            QCoreApplication.translate("SettingsDialog", "{0:.1f} s").format(
                self.s.crossfade_duration_ms / 1000.0
            )
        )

    def _refresh_xf_enabled_state(self):
        """Gate the duration slider + same-album toggle on the enable
        checkbox, and hard-disable the whole section while casting."""
        on = bool(self.s.crossfade_enabled)
        casting = getattr(self, "_xf_cast_blocking", False)
        active = on and not casting
        self._xf_duration.setEnabled(active)
        self._xf_smart_album.setEnabled(active)
        self._xf_enabled.setEnabled(not casting)
        # Duration label + value-readout aren't widgets Qt natively
        # greys (they're QLabels with explicit QSS colour overriding
        # the palette), so restyle them in lockstep so the row reads
        # as inactive when crossfade is off.
        dim_colour = DISABLED_FG if not active else TEXT_DIM
        self._xf_duration_lbl.setStyleSheet(f"color: {dim_colour}; {type_qss(TYPE_BODY)}")
        self._xf_duration_label.setStyleSheet(
            f"color: {dim_colour}; {type_qss(TYPE_CAPTION)} min-width: 42px;"
        )
        if casting:
            self._xf_caption.setText(
                QCoreApplication.translate(
                    "SettingsDialog",
                    "Casting — crossfade applies to local playback only and is inactive now.",
                )
            )
            self._xf_caption.setVisible(True)
        else:
            self._xf_caption.setText("")
            self._xf_caption.setVisible(False)

    def _on_xf_cast_active(self, *_args):
        self._xf_cast_blocking = True
        self._refresh_xf_enabled_state()

    def _on_xf_cast_cleared(self, *_args):
        self._xf_cast_blocking = False
        self._refresh_xf_enabled_state()

    def _build_eq_section(self) -> QWidget:
        """Build the EQ section as a standalone ``EqSettingsPage``.

        The EQ cluster (~1k lines) moved to ``jellytoast/settings_eq_page``
        to shrink this dialog. The bit-perfect gating and accent
        re-stamp reach the EQ enable / linear-phase checks and the
        refresh hook by name, so re-expose those three on the dialog
        (pointing at the page) to keep those cross-section paths working
        unchanged; the accent path calls ``self._eq_page.reapply_accent``.
        """
        from jellytoast.settings_eq_page import EqSettingsPage

        self._eq_page = EqSettingsPage(
            self.s,
            label_col_w=self._playback_label_w,
            narrow=getattr(self, "_compact", False),
        )
        self._eq_enabled_check = self._eq_page._eq_enabled_check
        self._eq_linear_phase_check = self._eq_page._eq_linear_phase_check
        self._eq_advanced_check = self._eq_page._eq_advanced_check
        self._refresh_eq_enabled_state = self._eq_page._refresh_eq_enabled_state
        return self._eq_page

    def _horiz_slider_qss(self) -> str:
        """Horizontal slider QSS — same accent-on-track + white-handle
        treatment GLOBAL_STYLE defines for the app, inlined here so
        sliders inside the dialog don't fall back to the native
        QStyle's blue Fusion handle (the dialog stylesheet's presence
        seems to suppress the app-level QSlider cascade on Wayland).
        Reads ACCENT live so a theme change rebuilds it on the next
        dialog open / theme_changed."""
        from jellytoast.ui_helpers import ACCENT as _ACCENT_NOW

        return f"""
            QSlider::groove:horizontal {{
                height: 3px; background: {ink_alpha(0.12)}; border-radius: {rad(1)}px;
            }}
            QSlider::sub-page:horizontal {{
                background: {_ACCENT_NOW}; border-radius: {rad(1)}px;
            }}
            QSlider::add-page:horizontal {{
                background: transparent;
            }}
            QSlider::handle:horizontal {{
                width: 12px; height: 12px; margin: -5px 0;
                background: {TEXT}; border-radius: 6px;
            }}
            QSlider::handle:horizontal:hover {{
                background: {_ACCENT_NOW};
            }}
            /* Disabled state — when the parent crossfade toggle is
               off, strip the accent fill and dim the handle so the
               slider obviously reads as inactive. Matches the EQ
               slider's :disabled treatment. */
            QSlider::sub-page:horizontal:disabled {{
                background: {ink_alpha(0.15)};
            }}
            QSlider::handle:horizontal:disabled {{
                background: {ink_alpha(0.25)};
            }}
            QSlider::groove:horizontal:disabled {{
                background: {ink_alpha(0.05)};
            }}
        """

    def _on_replaygain_changed(self):
        mode = self._rg_combo.currentData() or "no"
        PlayerBus.get().replaygain_changed.emit(mode)

    def _on_bit_perfect_toggled(self, on: bool):
        """Master gate. On enable: force-persist the safe values for
        every dependent setting and broadcast the corresponding signals
        so live surfaces (volume slider, mpv replaygain, EQ filter
        chain, crossfade engine) catch up immediately. On disable: just
        flip the flag — dependents stay at their safe values until the
        user re-enables them explicitly. See
        ``docs/research/bit_perfect_playback.md`` §7 (T2)."""
        self.s.bit_perfect_mode = bool(on)
        bus = PlayerBus.get()
        if on:
            # Volume → 100. set_volume's own bit-perfect guard would
            # clamp future calls regardless, but pushing 100 now keeps
            # the slider widget + volume_state observers in sync.
            # Snapshot the pre-toggle volume so the OFF leg can restore
            # it — avoids the "vol stuck at 100 after toggling off"
            # surprise loudness.
            self.s.volume_pre_bit_perfect = int(self.s.volume)
            # Skip the push when casting — set_volume routes the value
            # to the cast device, which has its own independent volume
            # we don't want to blast. PlayerBackend's
            # ``_refresh_bit_perfect_active`` covers the local pipeline
            # whenever the cast ends and the contract takes hold.
            if not bus.cast_active:
                bus.volume_changed.emit(100)
            # ReplayGain → 'no'. Settings setter doesn't fire the bus
            # signal; mpv listens to replaygain_changed via
            # PlayerBackend.set_replaygain, which also persists.
            # Snapshot the prior mode so the OFF leg can restore it —
            # symmetric with the EQ remember-and-restore.
            self.s.replaygain_pre_bit_perfect = self.s.replaygain
            # Same shape for audio_quality. We DON'T force "original"
            # on enable (that would silently change the next stream's
            # URL — a big FLAC over a slow link is not what every user
            # wants). The combo is greyed by ``_refresh_bit_perfect_gating``
            # so the user can't pick a transcoded tier while the mode
            # is on; the snapshot exists so a user who deliberately
            # moved to "original" pre-toggle keeps that choice, and a
            # user who did NOT can rebound to whatever they had.
            self.s.audio_quality_pre_bit_perfect = self.s.audio_quality or "original"
            if self.s.replaygain != "no":
                bus.replaygain_changed.emit("no")
                self._select_combo_by_data(self._rg_combo, "no")
            # EQ → off. eq_changed payload carries (enabled, bands);
            # bands stay the user's curve so re-enabling later replays
            # it. PlayerBackend.apply_eq clears the filter on enabled=
            # False. Snapshot the prior ``eq_enabled`` so the OFF leg
            # of this toggle can restore the user's previous choice —
            # a user who lives with their EQ shouldn't have to re-tick
            # the box every time they flip the master gate.
            self.s.eq_enabled_pre_bit_perfect = bool(self.s.eq_enabled)
            if self.s.eq_enabled:
                self.s.eq_enabled = False
                bus.eq_changed.emit(False, list(self.s.eq_bands))
                if hasattr(self, "_eq_enabled_check"):
                    self._eq_enabled_check.blockSignals(True)
                    self._eq_enabled_check.setChecked(False)
                    self._eq_enabled_check.blockSignals(False)
                    self._refresh_eq_enabled_state()
            # Crossfade → off. crossfade.Crossfader observes
            # crossfade_enabled via the settings property directly each
            # gapless-handoff tick; the bus signal is for the now-playing
            # badge's live re-render. Snapshot the prior state symmetric
            # with EQ + ReplayGain.
            self.s.crossfade_enabled_pre_bit_perfect = bool(self.s.crossfade_enabled)
            if self.s.crossfade_enabled:
                self.s.crossfade_enabled = False
                bus.crossfade_changed.emit(False)
                if hasattr(self, "_xf_enabled"):
                    self._xf_enabled.blockSignals(True)
                    self._xf_enabled.setChecked(False)
                    self._xf_enabled.blockSignals(False)
                    self._refresh_xf_enabled_state()
        else:
            # OFF leg — emit ``bit_perfect_changed`` BEFORE any restore
            # emits. The backend's ``_on_bit_perfect_setting_changed``
            # listener refreshes ``bus.bit_perfect_active`` to False
            # synchronously, which is what ``set_volume``'s guard reads.
            # Without this ordering, the volume restore below would
            # route through ``bus.volume_changed`` → ``set_volume``,
            # find the still-True runtime flag, and clamp the prior
            # value back to 100 — leaving the user stuck at full
            # volume after they disabled the mode. The other restores
            # (EQ / RG / Crossfade / quality) don't have an equivalent
            # guard, so the ordering only matters for volume; emit
            # early so the whole leg sees a consistent state.
            bus.bit_perfect_changed.emit(False)

            # Restore the pre-bit-perfect DSP state so a user who runs
            # with EQ / ReplayGain / Crossfade enabled doesn't have to
            # re-tick three boxes every time they cycle the master
            # gate. We only restore when the snapshot says "was
            # active"; an unset / OFF / "no" snapshot means "leave
            # alone" so a user who deliberately turned a DSP off
            # before enabling bit-perfect isn't surprised by it coming
            # back. Each snapshot is cleared once consumed so the next
            # ON leg captures fresh state.
            if self.s.eq_enabled_pre_bit_perfect and not self.s.eq_enabled:
                self.s.eq_enabled = True
                bus.eq_changed.emit(True, list(self.s.eq_bands))
                if hasattr(self, "_eq_enabled_check"):
                    self._eq_enabled_check.blockSignals(True)
                    self._eq_enabled_check.setChecked(True)
                    self._eq_enabled_check.blockSignals(False)
                    self._refresh_eq_enabled_state()
            self.s.eq_enabled_pre_bit_perfect = False

            prior_rg = self.s.replaygain_pre_bit_perfect
            if prior_rg != "no" and self.s.replaygain == "no":
                bus.replaygain_changed.emit(prior_rg)
                self._select_combo_by_data(self._rg_combo, prior_rg)
            self.s.replaygain_pre_bit_perfect = "no"

            prior_q = self.s.audio_quality_pre_bit_perfect
            if prior_q and prior_q != (self.s.audio_quality or ""):
                self.s.audio_quality = prior_q
                bus.audio_quality_changed.emit(prior_q)
                self._select_combo_by_data(self._quality_combo, prior_q)
            self.s.audio_quality_pre_bit_perfect = ""

            if self.s.crossfade_enabled_pre_bit_perfect and not self.s.crossfade_enabled:
                self.s.crossfade_enabled = True
                bus.crossfade_changed.emit(True)
                if hasattr(self, "_xf_enabled"):
                    self._xf_enabled.blockSignals(True)
                    self._xf_enabled.setChecked(True)
                    self._xf_enabled.blockSignals(False)
                    self._refresh_xf_enabled_state()
            self.s.crossfade_enabled_pre_bit_perfect = False

            # Restore the pre-toggle volume — set_volume can now honour
            # values < 100 again. -1 sentinel means "no snapshot
            # active" (first-ever toggle, or already consumed).
            # Same cast guard as the ON leg: emitting while a cast is
            # live would route the value to the cast device's volume,
            # which we deliberately didn't touch. settings.volume
            # stayed at its pre-toggle value the whole time (cast's
            # set_volume early-returns without persisting), so mpv
            # already resumes correctly when the cast ends.
            prior_vol = self.s.volume_pre_bit_perfect
            if prior_vol >= 0 and prior_vol != self.s.volume and not bus.cast_active:
                bus.volume_changed.emit(prior_vol)
            self.s.volume_pre_bit_perfect = -1
        self._refresh_bit_perfect_gating()
        # ON leg: emit AFTER snapshots so the snapshot setters in the
        # ON branch capture pre-toggle values. OFF leg already emitted
        # at the top of the branch so the volume-restore guard sees
        # the refreshed runtime flag.
        if on:
            bus.bit_perfect_changed.emit(True)

    def _refresh_bit_perfect_gating(self):
        """Grey out (or un-grey) every Playback control the bit-perfect
        toggle gates. Idempotent — safe to call from the toggle handler
        and from page-rebuild paths (live theme switch).

        The Exclusive sub-toggle is gated the inverse way to the rest:
        it's a SUB-option of Bit-perfect, so it's only enabled when
        Bit-perfect itself is on AND the source isn't transcoded —
        ``_make_mpv_handle`` requires ``audio_quality=="original"`` for
        exclusive to actually apply, so enabling the checkbox on a
        transcoded tier would just be a flag that does nothing."""
        on = bool(self.s.bit_perfect_mode)
        # ``_quality_combo`` is included alongside the DSP gates: the
        # bit-perfect contract requires ``audio_quality=="original"``
        # so a transcoded tier silently breaks it. Greying the combo
        # keeps the user from flipping it mid-session and ending up
        # in the "setting says Bit Perfect but the runtime flag is
        # off" zombie state. Sub-options (``_quality_*_combo`` for
        # the per-tier bitrate dropdowns, if any) ride the combo's
        # own enabled-state via their existing layout.
        for w in (
            "_quality_combo",
            "_rg_combo",
            "_xf_enabled",
            "_xf_smart_album",
            "_xf_duration",
            "_eq_enabled_check",
            "_eq_linear_phase_check",
            "_eq_advanced_check",
        ):
            widget = getattr(self, w, None)
            if widget is not None:
                widget.setEnabled(not on)
        quality_is_original = (self.s.audio_quality or "").strip().lower() == "original"
        if hasattr(self, "_audio_exclusive_check"):
            # Defence in depth: even with bit-perfect on, a stale
            # persisted audio_quality="low" (upgrade path, manual
            # QSettings edit) would let the user tick exclusive while
            # the underlying ``_make_mpv_handle`` gate refuses to honour
            # it. Gate the widget here too so the UI and the runtime
            # stay aligned.
            self._audio_exclusive_check.setEnabled(on and quality_is_original)
        if on:
            tip = QCoreApplication.translate(
                "SettingsDialog", "Disabled while Bit-perfect mode is on."
            )
        else:
            tip = ""
        for w in ("_quality_combo", "_rg_combo", "_xf_enabled", "_eq_enabled_check"):
            widget = getattr(self, w, None)
            if widget is not None and on:
                widget.setToolTip(tip)
        if hasattr(self, "_audio_exclusive_check"):
            if not on:
                self._audio_exclusive_check.setToolTip(
                    QCoreApplication.translate(
                        "SettingsDialog", "Enable Bit-perfect mode to use exclusive output."
                    )
                )
            elif not quality_is_original:
                self._audio_exclusive_check.setToolTip(
                    QCoreApplication.translate(
                        "SettingsDialog",
                        "Exclusive output requires audio quality 'Original' — "
                        "the server can't transcode and still ship "
                        "bit-identical bits.",
                    )
                )
            else:
                self._audio_exclusive_check.setToolTip("")
        # ReplayGain's existing long-form tooltip is informative; only
        # overwrite it while bit-perfect is on, restore on toggle-off
        # via a separate path (page rebuild + the tooltip restore below).
        if not on and hasattr(self, "_rg_combo"):
            self._rg_combo.setToolTip(
                QCoreApplication.translate(
                    "SettingsDialog",
                    "Normalization mode:\n"
                    "  Off — play untouched.\n"
                    "  Track — match each song to a common loudness target.\n"
                    "  Album — preserve relative loudness within an album.",
                )
            )
        self._refresh_bp_path_hint()

    def _refresh_bp_path_hint(self):
        """Re-evaluate the adaptive BIT-PERFECT path hint from live
        state. Cheap; called by the bit-perfect gate, the output picker,
        the exclusive toggle, and the PipeWire-config button."""
        label = getattr(self, "_bp_path_hint", None)
        if label is None:
            return
        from jellytoast.platform_compat import IS_LINUX, IS_WINDOWS

        platform = "linux" if IS_LINUX else ("windows" if IS_WINDOWS else "")
        pw_installed = False
        if IS_LINUX:
            try:
                from jellytoast import pipewire_setup as _pws

                pw_installed = _pws.is_supported() and _pws.is_installed()
            except Exception:
                pw_installed = False
        try:
            has_alsa = any(n.startswith("alsa/") for n, _ in self._audio_device_choices())
        except Exception:
            has_alsa = False
        from jellytoast import ui_helpers as _uih

        resolved = ""
        try:
            ctrl = getattr(self.parent(), "mpv_ctrl", None)
            if ctrl is not None:
                resolved = ctrl.current_output_family()
        except Exception:
            resolved = ""
        text = bit_perfect_path_hint(
            bp_on=bool(self.s.bit_perfect_mode),
            device=self.s.audio_output_device or "auto",
            has_alsa_direct=has_alsa,
            pw_conf_installed=pw_installed,
            exclusive_on=bool(self.s.audio_exclusive),
            platform=platform,
            faint_color=_uih.TEXT_FAINT,
            resolved_family=resolved,
        )
        label.setText(text)
        label.setVisible(bool(text))

    def _on_audio_exclusive_toggled(self, on: bool):
        """Persist + push to the live mpv handle. The new value takes
        effect on the next track (mpv's audio output is opened per
        play()). The shared-mode fallback in ``_make_mpv_handle`` covers
        DACs that refuse exclusive open (Windows WASAPI #11600/#11733).
        See ``docs/research/bit_perfect_playback.md`` §7 (T3)."""
        self.s.audio_exclusive = bool(on)
        PlayerBus.get().audio_exclusive_changed.emit(bool(on))
        self._refresh_bp_path_hint()

    def _refresh_pipewire_button_label(self):
        """Reflect current install state in the button label + tooltip.
        Idempotent — safe to call from the click handler and from the
        page-build path. No-op on non-Linux (the row doesn't exist)."""
        if not hasattr(self, "_pw_install_btn"):
            return
        from jellytoast import pipewire_setup as _pws

        # Narrow Settings: the full label (wider still in Spanish) can't fit
        # 360 px; the BIT-PERFECT header and the tooltip carry the rest.
        narrow = getattr(self, "_compact", False)
        if _pws.is_installed():
            self._pw_install_btn.setText(
                QCoreApplication.translate("SettingsDialog", "Remove config")
                if narrow
                else QCoreApplication.translate(
                    "SettingsDialog", "Remove PipeWire bit-perfect config"
                )
            )
            self._pw_install_btn.setToolTip(
                QCoreApplication.translate(
                    "SettingsDialog", "Removes {0}. Restart pipewire to revert."
                ).format(_pws.conf_path())
            )
        else:
            self._pw_install_btn.setText(
                QCoreApplication.translate("SettingsDialog", "Install config")
                if narrow
                else QCoreApplication.translate(
                    "SettingsDialog", "Install PipeWire bit-perfect config"
                )
            )
            self._pw_install_btn.setToolTip(
                QCoreApplication.translate(
                    "SettingsDialog", "Writes {0} with rate-matching properties."
                ).format(_pws.conf_path())
            )

    def _on_pipewire_install_clicked(self):
        """Toggle the conf file on disk. The label flips between
        Install / Remove on each click; a small inline status line
        confirms the action. T4 of
        ``docs/research/bit_perfect_playback.md`` §7."""
        from jellytoast import pipewire_setup as _pws

        if _pws.is_installed():
            removed = _pws.uninstall()
            if removed:
                msg = QCoreApplication.translate(
                    "SettingsDialog",
                    "Removed. Restart pipewire to revert the rate-matching properties.",
                )
            else:
                # uninstall() returns False if the file lacks our
                # ID-stamp header — surface that honestly rather than
                # claiming success.
                msg = QCoreApplication.translate(
                    "SettingsDialog",
                    "Did not remove — the file at that path wasn't "
                    "installed by jellytoast. Edit it by hand if you "
                    "want to revert.",
                )
        else:
            try:
                _pws.install()
                msg = QCoreApplication.translate(
                    "SettingsDialog",
                    "Installed. Restart pipewire (or log out / in) for "
                    "the rate-matching properties to take effect.",
                )
            except OSError as e:
                msg = QCoreApplication.translate("SettingsDialog", "Install failed: {0}").format(e)
        self._pw_status_label.setText(msg)
        self._pw_status_label.setVisible(True)
        self._refresh_pipewire_button_label()
        self._refresh_bp_path_hint()
