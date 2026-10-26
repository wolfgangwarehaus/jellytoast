"""Widgets + helpers shared by the Settings dialog and its page modules
(split out of ``settings_dialog.py``; re-exported there for old imports)."""

from PySide6.QtCore import QCoreApplication, QPointF, QRect, QSize, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPalette
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QStyle,
    QStyleOptionButton,
    QStylePainter,
    QVBoxLayout,
    QWidget,
)

from jellytoast.design_tokens import (
    RADIUS_WINDOW,
    TYPE_BODY,
    TYPE_CAPTION,
    TYPE_TITLE,
    rad,
    type_qss,
)
from jellytoast.icon_button import IconButton
from jellytoast.icons import icon
from jellytoast.keep_above import (
    ABOUT_DIALOG_WINDOW_TITLE,
)
from jellytoast.player_state import PlayerBus
from jellytoast.theme import contrast_ink, ink_rgb
from jellytoast.ui_helpers import (
    TEXT,
    TEXT_DIM,
)
from jellytoast.version import __version__


class WrapCheckBox(QCheckBox):
    """A QCheckBox whose label wraps onto more lines when the dialog is
    narrow. Qt's own draws the label on one line, so at 360 px Settings
    clipped "Hide to system tray when windo". Paints the stylesheet's
    indicator itself (beside the first line) and the label word-wrapped
    after it; the whole row stays clickable."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        sp = self.sizePolicy()
        sp.setHorizontalPolicy(QSizePolicy.Policy.Preferred)
        # Not QCheckBox's Fixed: a fixed height makes the layout ignore
        # heightForWidth, and the wrapped second line was cut off.
        sp.setVerticalPolicy(QSizePolicy.Policy.Minimum)
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)

    def _option(self) -> QStyleOptionButton:
        opt = QStyleOptionButton()
        self.initStyleOption(opt)
        return opt

    def _text_x(self) -> int:
        opt = self._option()
        style = self.style()
        ind = style.subElementRect(QStyle.SubElement.SE_CheckBoxIndicator, opt, self)
        gap = style.pixelMetric(QStyle.PixelMetric.PM_CheckBoxLabelSpacing, opt, self)
        return ind.right() + 1 + gap

    def _line_height(self) -> int:
        opt = self._option()
        ind = self.style().subElementRect(QStyle.SubElement.SE_CheckBoxIndicator, opt, self)
        return max(self.fontMetrics().height(), ind.height()) + 4

    def _text_height(self, width: int) -> int:
        box = QRect(0, 0, max(1, width - self._text_x()), 100000)
        return self.fontMetrics().boundingRect(box, Qt.TextFlag.TextWordWrap, self.text()).height()

    def hasHeightForWidth(self) -> bool:  # noqa: N802 — Qt naming
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 — Qt naming
        line = self._line_height()
        return max(line, self._text_height(width) + (line - self.fontMetrics().height()))

    def minimumSizeHint(self) -> QSize:  # noqa: N802 — Qt naming
        fm = self.fontMetrics()
        longest = max((fm.horizontalAdvance(w) for w in self.text().split()), default=0)
        return QSize(self._text_x() + longest, self._line_height())

    def hitButton(self, pos) -> bool:  # noqa: N802 — Qt naming
        return self.rect().contains(pos)

    def paintEvent(self, _e):  # noqa: N802 — Qt naming
        p = QStylePainter(self)
        opt = self._option()
        opt.text = ""
        line = self._line_height()
        opt.rect = QRect(0, 0, self.width(), line)
        p.drawControl(QStyle.ControlElement.CE_CheckBox, opt)
        top = (line - self.fontMetrics().height()) // 2
        text_rect = QRect(self._text_x(), top, self.width() - self._text_x(), self.height() - top)
        self.style().drawItemText(
            p,
            text_rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap),
            self.palette(),
            self.isEnabled(),
            self.text(),
            QPalette.ColorRole.WindowText,
        )


class _AccentSwatch(QPushButton):
    """Circular accent-picker swatch painted by hand.

    QSS ``border-radius`` on a small QPushButton has a long-standing
    aliasing bug — Qt's stylesheet painter doesn't antialias the
    rounded edge, leaving visible ``dots`` / stair-stepping at each
    45° point on the perimeter. Custom-painting with QPainter +
    RenderHint.Antialiasing produces a smooth circle.

    The swatch carries its own fill colour and selection state; the
    parent dialog calls :meth:`set_accent_state` after the user picks
    a new accent so every swatch refreshes its ring without rebuilding
    the row."""

    def __init__(self, hex_color: str, parent=None):
        super().__init__(parent)
        self._fill = QColor(hex_color)
        self._hovered = False
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedSize(28, 28)

    def set_accent_state(self, hex_color: str, checked: bool) -> None:
        """Refresh fill + checked from the live picker. Hex doesn't
        actually change today (presets are fixed), but accepting it
        keeps the call symmetric with the QSS-builder it replaces and
        leaves room for future user-custom accents."""
        self._fill = QColor(hex_color)
        self.setChecked(checked)
        self.update()

    def enterEvent(self, e):
        self._hovered = True
        self.update()
        super().enterEvent(e)

    def leaveEvent(self, e):
        self._hovered = False
        self.update()
        super().leaveEvent(e)

    def paintEvent(self, _e):
        from PySide6.QtGui import QPen

        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            # Fixed inset for every state — the fill stays the same
            # diameter whether the swatch is idle, hovered, or selected.
            # The earlier version inset by half the stroke width, so a
            # 2-px selected ring shrank the visible fill by 1 px relative
            # to the 1-px idle ring; the selected swatch read as smaller
            # than its siblings. Now the ring sits over the fill edge
            # instead of biting into the inset.
            r = self.rect()
            cx = r.center().x() + 0.5
            cy = r.center().y() + 0.5
            radius = min(r.width(), r.height()) / 2.0 - 1.5

            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(self._fill)
            p.drawEllipse(QPointF(cx, cy), radius, radius)

            # Selection / hover ring — thin in every state so the fill
            # dominates the visual. Selected uses near-opaque ink as a
            # crisp outline; hover is slightly stronger than idle. Theme-
            # aware ink (white on dark, near-black on light) keeps the ring
            # visible on the dark default theme — a hardcoded black ring
            # vanished against the dark dialog body.
            ink = ink_rgb()
            if self.isChecked():
                ring_color = QColor(*ink, int(0.85 * 255))
                ring_w = 1.5
            elif self._hovered:
                ring_color = QColor(*ink, int(0.55 * 255))
                ring_w = 1.0
            else:
                ring_color = QColor(*ink, int(0.18 * 255))
                ring_w = 1.0
            pen = QPen(ring_color)
            pen.setWidthF(ring_w)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(cx, cy), radius, radius)
        finally:
            p.end()


class _EyedropperSwatch(_AccentSwatch):
    """Accent-picker swatch that triggers the screen eyedropper. Sits in line
    with the preset swatches. Shows the eyedropper glyph on an empty disc until
    the user samples a colour; afterwards the disc fills with that colour (the
    glyph stays, tinted to contrast, so it stays the re-pickable dropper)."""

    def __init__(self, parent=None):
        super().__init__("#000000", parent)
        self._fill = None  # None = empty (not yet used)
        self.setToolTip(self.tr("Pick a colour from the screen"))

    def set_picked(self, hex_color, checked: bool = True) -> None:
        """Load a sampled colour into the swatch (or clear with None)."""
        self._fill = QColor(hex_color) if hex_color else None
        self.setChecked(bool(checked and self._fill is not None))
        self.update()

    def paintEvent(self, _e):
        from PySide6.QtGui import QPen

        from jellytoast.icons import _svg_pix

        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            r = self.rect()
            cx = r.center().x() + 0.5
            cy = r.center().y() + 0.5
            radius = min(r.width(), r.height()) / 2.0 - 1.5

            # Disc: the sampled colour, or a faint empty disc before first use.
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(
                self._fill if self._fill is not None else QColor(*ink_rgb(), int(0.06 * 255))
            )
            p.drawEllipse(QPointF(cx, cy), radius, radius)

            # Eyedropper glyph, centred. Theme ink on the empty disc; a
            # contrast-picked ink (white/near-black, WCAG luminance) once a
            # colour is loaded so it stays legible on any sampled colour.
            if self._fill is not None:
                glyph_hex = contrast_ink(self._fill).name()
            else:
                glyph_hex = QColor(*ink_rgb()).name()
            gsz = 14
            pix = _svg_pix("eyedropper", glyph_hex, gsz)
            p.drawPixmap(
                round((r.width() - gsz) / 2.0),
                round((r.height() - gsz) / 2.0),
                pix,
            )

            # Selection / hover ring — identical treatment to _AccentSwatch.
            ink = ink_rgb()
            if self.isChecked():
                ring_color = QColor(*ink, int(0.85 * 255))
                ring_w = 1.5
            elif self._hovered:
                ring_color = QColor(*ink, int(0.55 * 255))
                ring_w = 1.0
            else:
                ring_color = QColor(*ink, int(0.18 * 255))
                ring_w = 1.0
            pen = QPen(ring_color)
            pen.setWidthF(ring_w)
            p.setPen(pen)
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QPointF(cx, cy), radius, radius)
        finally:
            p.end()


# Selector ("dropdown") used to live here as ``_Selector``; it moved
# to jellytoast/selector.py so the login view (and any future surfaces)
# can reuse it. Imported below as ``_Selector`` so the local
# call-sites in this file keep working without a rename pass.

# Native music surfaces the top-bar Home button can route to. Mirrors
# the keys consumed by JellytoastWindow._route_home. The same setting
# also drives the launch landing — jellytoast always boots into the
# user's chosen Home surface.
HOME_DESTINATIONS = [
    ("Albums", "albums"),
    ("Playlists", "playlists"),
    ("Artists", "artists"),
    ("Songs", "songs"),
    ("Genres", "genres"),
    ("Suggestions", "suggestions"),
]

# The luminance-mode picker shown for a family that has both a dark and a light
# member (jellytoast, Catppuccin, Gruvbox). Order: follow-system first, then the
# explicit choices — mirrors how the old theme dropdown led with Auto.
_THEME_MODE_CHOICES = [
    ("Follow system", "auto"),
    ("Dark", "dark"),
    ("Light", "light"),
]

LYRICS_FONT_SIZES = [
    ("Smaller", "small"),
    ("Default", "default"),
    ("Larger", "large"),
    ("Largest", "largest"),
]

# (visible label, mpv "replaygain" property value)
# Labels kept short for combo-box rhythm; the longer parentheticals
# ("per song" / "preserve relative loudness") moved to combo tooltips
# so the box width matches the AUDIO_QUALITIES combo without losing
# the explanatory copy.
REPLAYGAIN_MODES = [
    ("Off", "no"),
    ("Track", "track"),
    ("Album", "album"),
]

# (visible label, audio_quality setting value)
# "original" maps to DirectStream (Jellyfin) / format=raw (Subsonic).
# Numeric values are kbps and trigger server-side transcode.
AUDIO_QUALITIES = [
    ("Original", "original"),
    ("320 kbps", "320"),
    ("256 kbps", "256"),
    ("192 kbps", "192"),
    ("128 kbps", "128"),
    ("96 kbps", "96"),
]

# (visible label, cast_stream_routing setting value)
# Controls how a cast device reaches the media stream. "auto" casts
# direct when the server is a private LAN IP and relays through this
# machine otherwise (Tailscale / remote / self-signed host).
CAST_ROUTING_MODES = [
    # (radio label, faint hint suffix or None, setting value)
    ("Auto", None, "auto"),
    ("Network cast", "(normal mode)", "direct"),
    ("Route locally", "(offline, special case)", "proxy"),
]


class _AboutDialog(QDialog):
    """Borderless frosted "About jellytoast" popup. Custom titlebar
    (drag + close button), rounded translucent body painted with the
    lifted tone (``popup_paint_qcolor`` — same colour as tooltips and
    the volume slider popup) so it reads as one of the elevated
    surface family instead of an opaque dialog. On KDE Wayland the
    KWin noborder rule (jellytoast.keep_above, scoped by
    ``ABOUT_DIALOG_WINDOW_TITLE``) strips the server decoration; off
    KDE Wayland ``FramelessWindowHint`` does the same."""

    BODY_RADIUS = RADIUS_WINDOW

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(ABOUT_DIALOG_WINDOW_TITLE)
        self.setFixedWidth(380)
        self.setModal(True)

        from jellytoast.platform_compat import is_kde_wayland

        # Same flag dance as SettingsDialog: KDE Wayland keeps the
        # server-side decoration so KWin blur survives drags, and a
        # noborder rule hides the chrome. Everywhere else we go
        # plain frameless.
        flags = Qt.WindowType.Dialog
        if not is_kde_wayland():
            flags |= Qt.WindowType.FramelessWindowHint
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setObjectName("jtAboutDialog")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_titlebar())

        body = QWidget()
        body.setStyleSheet("background: transparent;")
        v = QVBoxLayout(body)
        v.setContentsMargins(20, 8, 20, 18)
        v.setSpacing(8)

        title = QLabel("jellytoast")
        title.setStyleSheet(f"color: {TEXT}; {type_qss(TYPE_TITLE)}")
        v.addWidget(title)
        self._lbl_title = title

        version = QLabel(f"v{__version__}")
        version.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_BODY)}")
        v.addWidget(version)
        self._lbl_version = version

        v.addSpacing(8)

        desc = QLabel(
            self.tr(
                "Native desktop client for Jellyfin and Subsonic / Navidrome — "
                "all-PySide6 surfaces with bit-perfect mpv playback, MPRIS2, "
                "system tray, floating mini player, and Chromecast/AirPlay "
                "casting."
            )
        )
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_BODY)}")
        v.addWidget(desc)
        self._lbl_desc = desc

        v.addSpacing(12)
        close_btn = QPushButton(self.tr("Close"))
        close_btn.setObjectName("ghost")
        close_btn.clicked.connect(self.accept)
        v.addWidget(close_btn, 0, Qt.AlignmentFlag.AlignRight)

        outer.addWidget(body, 1)

        # Live theme re-stamp — the label colours + titlebar close-hover
        # are baked here; an OS-driven dark↔light flip while the (modal)
        # dialog is open would otherwise leave them in the old palette.
        # Bound-method slot auto-disconnects when the dialog is destroyed.
        PlayerBus.get().theme_changed.connect(self._reapply_theme)

    def _build_titlebar(self) -> QWidget:
        tb = QWidget()
        tb.setFixedHeight(40)
        tb.setObjectName("jtAboutTitle")
        tb.setStyleSheet("""
            QWidget#jtAboutTitle { background: transparent; }
            QWidget#jtAboutTitle QLabel { background: transparent; }
        """)
        h = QHBoxLayout(tb)
        h.setContentsMargins(16, 0, 8, 0)
        h.setSpacing(10)

        title = QLabel(self.tr("About jellytoast"))
        title.setStyleSheet(f"color: {TEXT}; {type_qss(TYPE_CAPTION)}")
        h.addWidget(title)
        self._tb_title = title
        h.addStretch(1)

        # Icon-based close button — matches the main-window top-bar
        # win_close glyph + WASH_HOVER pill, so all close buttons in
        # the app read identically (text-glyph ✕ buttons on
        # KDE/Plasma pick up the default-button outline and read
        # heavier than icon buttons). autoDefault off so Qt doesn't
        # treat it as the dialog's default action and paint a
        # focus-ring frame even when unhovered.
        close_btn = IconButton()
        close_btn.setIcon(icon("win_close"))
        close_btn.setIconSize(QSize(16, 16))
        close_btn.setFixedSize(32, 28)
        close_btn.setAutoDefault(False)
        close_btn.setDefault(False)
        close_btn.setFlat(True)
        close_btn.setStyleSheet(self._tb_close_qss())
        close_btn.clicked.connect(self.reject)
        h.addWidget(close_btn)
        self._tb_close = close_btn

        tb.mousePressEvent = self._titlebar_press
        return tb

    @staticmethod
    def _tb_close_qss() -> str:
        from jellytoast import ui_helpers as _u

        return f"""
            QPushButton {{
                background: transparent;
                border: none;
                border-radius: {rad(6)}px;
            }}
            QPushButton:hover {{ background: {_u.WASH_HOVER}; }}
        """

    def _reapply_theme(self) -> None:
        """Re-stamp the baked label colours + close-hover on a live
        theme flip. Reads live ``ui_helpers`` ink tokens."""
        from jellytoast import ui_helpers as _u

        self._lbl_title.setStyleSheet(f"color: {_u.TEXT}; {type_qss(TYPE_TITLE)}")
        self._lbl_version.setStyleSheet(f"color: {_u.TEXT_DIM}; {type_qss(TYPE_BODY)}")
        self._lbl_desc.setStyleSheet(f"color: {_u.TEXT_DIM}; {type_qss(TYPE_BODY)}")
        self._tb_title.setStyleSheet(f"color: {_u.TEXT}; {type_qss(TYPE_CAPTION)}")
        self._tb_close.setStyleSheet(self._tb_close_qss())

    def _titlebar_press(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            handle = self.windowHandle()
            if handle is not None:
                handle.startSystemMove()

    def keyPressEvent(self, e):  # noqa: N802 — Qt naming
        if e.key() == Qt.Key.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(e)

    def paintEvent(self, _e):  # noqa: N802 — Qt naming
        """Paint the dialog body as a rounded rect filled with the
        active theme's ``popup_opaque_fill`` token (translucent on
        frosted themes, opaque on solid). Pairs with the KWin
        noborder rule + compositor blur to read as one of the
        elevated frosted surfaces."""
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.RenderHint.Antialiasing)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
            p.fillRect(self.rect(), Qt.GlobalColor.transparent)
            p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

            from jellytoast.ui_helpers import popup_paint_qcolor

            path = QPainterPath()
            path.addRoundedRect(
                0.0,
                0.0,
                float(self.width()),
                float(self.height()),
                self.BODY_RADIUS,
                self.BODY_RADIUS,
            )
            p.setBrush(popup_paint_qcolor())
            p.setPen(Qt.PenStyle.NoPen)
            p.drawPath(path)
        finally:
            p.end()

    def showEvent(self, e):  # noqa: N802 — Qt naming
        super().showEvent(e)
        # Install compositor blur on frosted themes so the translucent
        # popup_paint_qcolor fill composites over a blurred wallpaper
        # backdrop — the same lifted-glass look the tooltip uses.
        try:
            from jellytoast import blur as _blur
            from jellytoast.theme import get_active_theme

            if get_active_theme().blur:
                _blur.apply(self, True, corner_radius=self.BODY_RADIUS)
        except Exception:
            pass


# Output-family tag colors — semantic (NOT the user accent): green =
# mixer-managed sink server (PipeWire / Pulse / WASAPI / CoreAudio),
# purple = direct ALSA hardware. The picker rows carry a dot in these
# colors; the bit-perfect legend uses the same colors for its keywords.
AUDIO_FAMILY_GREEN = "#2fbe8a"
AUDIO_FAMILY_PURPLE = "#967de1"


def audio_family_dot_color(device_name: str) -> str:
    """Tag color for an output-picker entry, '' for none (auto)."""
    family = device_name.partition("/")[0]
    if family == "alsa" and "/" in device_name:
        return AUDIO_FAMILY_PURPLE
    if family in ("pipewire", "pulse", "wasapi", "coreaudio"):
        return AUDIO_FAMILY_GREEN
    return ""


def bit_perfect_path_hint(
    *,
    bp_on: bool,
    device: str,
    has_alsa_direct: bool,
    pw_conf_installed: bool,
    exclusive_on: bool,
    platform: str,
    faint_color: str = "#7a7a7a",
    resolved_family: str = "",
) -> str:
    """The color-keyed legend under the BIT-PERFECT section (rich text;
    '' hides the row). One line per output family, keyed by the same
    colors the picker's row dots use; the family the current device
    belongs to renders at full strength, the other's body text dims to
    ``faint_color`` (Qt rich text has no opacity). Pure function so
    every state is unit-testable.

    The streamlining contract (2026-06-11): the page should ASSEMBLE the
    bit-perfect puzzle for the user — app layer (the toggle's gating),
    then OS layer per family — instead of leaving three controls and a
    doc link to cross-reference."""
    if not bp_on:
        return ""

    def _row(color: str, word: str, text: str, active: bool) -> str:
        # Two-column row: the colored family word gutters left, the
        # description (which may carry its own <br> line breaks) wraps
        # in its own column — so a continuation line starts under the
        # description text, not under the family word.
        if active:
            tag = f"<b><span style='color:{color};'>{word}</span></b>"
            body = text
        else:
            tag = f"<span style='color:{color};'>{word}</span>"
            body = f"<span style='color:{faint_color};'>{text}</span>"
        return f"<tr><td style='white-space:nowrap;'>{tag}&nbsp;—&nbsp;</td><td>{body}</td></tr>"

    def _table(*rows: str) -> str:
        return "<table cellspacing='0' cellpadding='0'>" + "".join(rows) + "</table>"

    if platform == "linux":
        # NB: no "turn on Exclusive output" advice here — mpv's PipeWire
        # exclusive mode failed every open on a real box (2026-06-11;
        # the audio-health watchdog sheds + persists it off when that
        # happens). On Linux, real exclusivity IS the direct ALSA path.
        on_auto = device == "auto" or not device
        on_alsa = device.startswith("alsa/") or (on_auto and resolved_family == "alsa")
        # On Auto, name what Auto actually resolved to (mpv current-ao)
        # so the legend answers "where is my sound going right now".
        pw_word = "PipeWire"
        alsa_word = "ALSA"
        if on_auto and resolved_family:
            if on_alsa:
                alsa_word = QCoreApplication.translate("SettingsDialog", "Auto → ALSA")
            elif resolved_family in ("pipewire", "pulse"):
                pw_word = (
                    QCoreApplication.translate("SettingsDialog", "Auto → PipeWire")
                    if resolved_family == "pipewire"
                    else QCoreApplication.translate("SettingsDialog", "Auto → Pulse")
                )
        pw_text = (
            QCoreApplication.translate("SettingsDialog", "sample-rate config installed.")
            if pw_conf_installed
            else QCoreApplication.translate(
                "SettingsDialog", "install the sample-rate config below."
            )
        ) + QCoreApplication.translate(
            "SettingsDialog",
            "<br>Sharing the playback device with other audio sources "
            "will degrade the bit-perfect path.",
        )
        alsa_text = (
            QCoreApplication.translate(
                "SettingsDialog",
                "selecting an ALSA device claims it exclusively — other audio sources won't play.",
            )
            if has_alsa_direct
            else QCoreApplication.translate(
                "SettingsDialog", "no direct device detected on this system."
            )
        )
        return _table(
            _row(AUDIO_FAMILY_GREEN, pw_word, pw_text, not on_alsa),
            _row(AUDIO_FAMILY_PURPLE, alsa_word, alsa_text, on_alsa),
        )
    if platform == "windows":
        if exclusive_on:
            return _table(
                _row(
                    AUDIO_FAMILY_GREEN,
                    "WASAPI",
                    QCoreApplication.translate(
                        "SettingsDialog",
                        "Exclusive output on — bit-perfect end to end.",
                    ),
                    True,
                )
            )
        return _table(
            _row(
                AUDIO_FAMILY_GREEN,
                "WASAPI",
                QCoreApplication.translate(
                    "SettingsDialog",
                    "turn on Exclusive output — it hands jellytoast the device "
                    "directly, skipping the Windows mixer.",
                ),
                True,
            )
        )
    return ""
