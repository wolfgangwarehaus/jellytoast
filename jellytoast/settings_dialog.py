"""
jellytoast settings dialog. Frameless + frosted to match the main window
and mini player. Sidebar nav on the left, page content on the right.

Sections:
- General  — startup destination, window/tray behavior
- Account  — server URL, sign-out
- Appearance — theme mode (dark/light, live-applied) + accent color
- Display  — font + UI scaling
- About    — version + description

Settings that need the host window to react (sign-out, server change)
are emitted as signals; the host listens and acts.
"""

import sys

from PySide6.QtCore import QPointF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QKeySequenceEdit,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpacerItem,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
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
        self.setToolTip("Pick a colour from the screen")

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
                self._fill
                if self._fill is not None
                else QColor(*ink_rgb(), int(0.06 * 255))
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
from jellytoast import autostart as _autostart
from jellytoast.design_tokens import (
    RADIUS_WINDOW,
    TYPE_BODY,
    TYPE_CAPTION,
    TYPE_MICRO,
    TYPE_SUBHEAD,
    TYPE_TITLE,
    font,
    rad,
    type_qss,
)
from jellytoast.icon_button import IconButton
from jellytoast.icons import icon
from jellytoast.keep_above import (
    ABOUT_DIALOG_WINDOW_TITLE,
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
from jellytoast.settings import get_settings
from jellytoast.theme import _hex_to_rgb, contrast_ink, ink_rgb
from jellytoast.ui_helpers import (
    ACCENT,
    BORDER,
    DISABLED_FG,
    ERROR_FG,
    POPUP_OPAQUE_FILL,
    TEXT,
    TEXT_DIM,
    TEXT_FAINT,
    WASH_HOVER,
    ink_alpha,
)
from jellytoast.version import __version__

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
            "Native desktop client for Jellyfin and Subsonic / Navidrome — "
            "all-PySide6 surfaces with bit-perfect mpv playback, MPRIS2, "
            "system tray, floating mini player, and Chromecast/AirPlay "
            "casting."
        )
        desc.setWordWrap(True)
        desc.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_BODY)}")
        v.addWidget(desc)
        self._lbl_desc = desc

        v.addSpacing(12)
        close_btn = QPushButton("Close")
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

        title = QLabel("About jellytoast")
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
        return (
            f"<tr><td style='white-space:nowrap;'>{tag}&nbsp;—&nbsp;</td>"
            f"<td>{body}</td></tr>"
        )

    def _table(*rows: str) -> str:
        return (
            "<table cellspacing='0' cellpadding='0'>" + "".join(rows) + "</table>"
        )

    if platform == "linux":
        # NB: no "turn on Exclusive output" advice here — mpv's PipeWire
        # exclusive mode failed every open on a real box (2026-06-11;
        # the audio-health watchdog sheds + persists it off when that
        # happens). On Linux, real exclusivity IS the direct ALSA path.
        on_auto = device == "auto" or not device
        on_alsa = device.startswith("alsa/") or (
            on_auto and resolved_family == "alsa"
        )
        # On Auto, name what Auto actually resolved to (mpv current-ao)
        # so the legend answers "where is my sound going right now".
        pw_word = "PipeWire"
        alsa_word = "ALSA"
        if on_auto and resolved_family:
            if on_alsa:
                alsa_word = "Auto → ALSA"
            elif resolved_family in ("pipewire", "pulse"):
                pw_word = (
                    "Auto → PipeWire"
                    if resolved_family == "pipewire"
                    else "Auto → Pulse"
                )
        pw_text = (
            "sample-rate config installed."
            if pw_conf_installed
            else "install the sample-rate config below."
        ) + (
            "<br>Sharing the playback device with other audio sources "
            "will degrade the bit-perfect path."
        )
        alsa_text = (
            "selecting an ALSA device claims it exclusively — other "
            "audio sources won't play."
            if has_alsa_direct
            else "no direct device detected on this system."
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
                    "Exclusive output on — bit-perfect end to end.",
                    True,
                )
            )
        return _table(
            _row(
                AUDIO_FAMILY_GREEN,
                "WASAPI",
                "turn on Exclusive output — it hands jellytoast the device "
                "directly, skipping the Windows mixer.",
                True,
            )
        )
    return ""


class SettingsDialog(QDialog):
    # Matched to the host-OS window-corner radius (see RADIUS_WINDOW).
    BODY_RADIUS = RADIUS_WINDOW

    sign_out_requested = Signal()
    # Carries the new URL the user committed inline. Main window
    # reads it directly rather than re-prompting — the field above
    # the button IS the prompt.
    server_change_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        # Delete the C++ object when the dialog finishes. It's rebuilt
        # fresh on every open (jellytoast.app._open_settings drops its ref on
        # finished) and several pages connect bound-method slots to the
        # PlayerBus singleton; without this the singleton keeps every
        # closed dialog alive forever (one leaked dialog per open) and its
        # stale slots keep firing. WA_DeleteOnClose covers a window-X
        # close; finished→deleteLater covers the accept()/reject() button
        # paths (done() hides without a close event), so both are needed.
        self.setAttribute(Qt.WA_DeleteOnClose)
        # A glass-opacity drag that settles within 120ms of the dialog closing
        # would otherwise lose its full re-stamp (the settle QTimer dies with
        # the dialog), leaving cached-body surfaces stale until the next theme
        # event. Flush any pending settle synchronously before teardown.
        self.finished.connect(self._flush_pending_glass_settle)
        self.finished.connect(self.deleteLater)
        self.s = get_settings()
        # Distinct title so the KWin `noborder` rule (jellytoast.keep_above)
        # can scope-match this dialog without catching the main window.
        from jellytoast.keep_above import SETTINGS_WINDOW_TITLE

        self.setWindowTitle(SETTINGS_WINDOW_TITLE)
        # 820→720: pulls a chunk of empty real-estate off the right
        # side of the dialog. Playback is the widest page (EQ band
        # grid + trailing Save/Delete buttons); the rest of the
        # pages adapt to the tighter content width page by page.
        # Height 540→620: lets the Playback page (Audio / Crossfade /
        # EQ / Shuffle) sit fully visible without invoking the page
        # scroller.
        #
        # 720×620 is the tuned baseline for the default font (Inter @ default
        # scale). It's scaled proportionally with the active font so a larger
        # font scale or a wide/monospace family gets a proportionally bigger
        # dialog (and a small scale a smaller one) instead of clipping — the
        # pages scroll for any residual overflow. See _adaptive_dialog_size.
        self.setFixedSize(*self._adaptive_dialog_size())

        # Independent top-level window (not ``Qt.Dialog``): KWin treats
        # ``Qt.Dialog`` as transient-for-parent, which pins it above
        # the main window and renders the main window as a faded
        # "background" — fine for a modal dialog, wrong for a side-
        # by-side configuration surface where the user wants to tweak
        # colours and watch the main UI react in real time. With
        # ``Qt.Window`` + ``NonModal`` the user can raise either
        # window, alt-tab between them, and the main UI keeps its
        # active-window styling. Mini player is also a separate
        # top-level so all three coexist as peers.
        # Frameless everywhere EXCEPT KDE Wayland, where the dialog is
        # server-side-decorated and a KWin `noborder` rule
        # (jellytoast.keep_above.install_noborder_rules) strips the chrome.
        # KWin's blur effect drops blur for *undecorated* windows while
        # they move, so a frameless dialog flickers when dragged — a
        # decorated + noborder window keeps its blur.
        from jellytoast.platform_compat import is_kde_wayland

        _dlg_flags = Qt.WindowType.Window
        if not is_kde_wayland():
            _dlg_flags |= Qt.WindowType.FramelessWindowHint
        self.setWindowFlags(_dlg_flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setObjectName("jtSettingsDialog")
        self.setWindowModality(Qt.WindowModality.NonModal)

        # Dialog-level styling for QComboBox + its popup. The dialog
        # uses WA_TranslucentBackground for the rounded card look, and
        # without explicit popup styling the Theme combo / ReplayGain
        # combo / etc. inherit that translucency — the popup ghosts
        # over the field below it. An opaque background and a clear
        # down-arrow icon (Qt's default arrow renders invisible against
        # the dark surface here) fix both issues for every combo in
        # the dialog without per-combo styling.
        self._build_and_apply_dialog_stylesheet()

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        outer.addWidget(self._build_titlebar())

        body = QWidget()
        body.setStyleSheet("background: transparent;")
        body_h = QHBoxLayout(body)
        body_h.setContentsMargins(10, 0, 14, 14)
        # 2→12 gap between nav column and the page stack: gives the
        # right pane visible room rather than touching the sidebar's
        # selected-row chip.
        body_h.setSpacing(12)

        self.nav = QListWidget()
        # Never show a horizontal scrollbar on the sidebar — if a wide/large
        # font needs more room we widen the column instead (see
        # _size_nav_to_content, re-run on live font changes), which reads far
        # better than a scrollbar under the nav labels.
        self.nav.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Initial width; _size_nav_to_content overrides it once the rows exist.
        self.nav.setFixedWidth(128)
        # Keyboard-navigable sidebar: Tab reaches it, Up/Down switch pages
        # (currentRowChanged rebuilds+shows). It was NoFocus, which made the
        # whole dialog unreachable by keyboard past the first control.
        self.nav.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.nav.setStyleSheet(self._nav_qss())
        body_h.addWidget(self.nav)

        self.stack = QStackedWidget()
        self.stack.setStyleSheet("background: transparent;")
        body_h.addWidget(self.stack, 1)

        outer.addWidget(body, 1)

        # Pages build LAZILY — `_add_page` stores the builder callable;
        # the content widget isn't constructed until the user first
        # navigates to that page. Building all nine up front is ~900
        # widgets, and since the theme combo lives on the Display page
        # the dialog is always open during a live theme switch — so
        # every switch would otherwise re-style those 900 widgets
        # (~150ms). Lazy pages keep a switch closer to ~15ms and also
        # make the dialog itself open faster.
        #
        # Account is folded into General as a Server section; About
        # moved to the titlebar info button. Downloads / Colors expand
        # to fill the page (they hold scrolling lists); Colors is the
        # power-user colour editor, last so day-to-day pages sit first.
        self._page_builders: list = []
        # Indices whose content widget has been built. A light↔dark
        # theme switch must tear these down + rebuild (see
        # _rebuild_pages_for_theme); unbuilt pages stay lazy.
        self._built_pages: set[int] = set()
        # General is scrollable like the other content pages. It used to be
        # expand=True (unscrolled) to dodge a hair-trigger scrollbar that
        # "jiggled" at the old fixed 820×540 size — but with the adaptive size
        # + live fonts, a wide/large font makes it overflow and CLIP the bottom
        # (the Updates row + "Check now" got cut off). The scroll wrapper now
        # uses an autofade scrollbar (hidden when idle), so it scrolls cleanly
        # on overflow with no persistent-scrollbar jiggle.
        self._add_page("General", self._build_general)
        self._add_page("Playback", self._build_playback)
        self._add_page("Casting", self._build_casting)
        self._add_page("Downloads", self._build_downloads, expand=True)
        self._add_page("Display", self._build_display)
        self._add_page("Hotkeys", self._build_hotkeys)
        self._add_page("Scrobbling", self._build_scrobbling)
        # The per-token "Colors" editor (jellytoast.settings_colors_page) is
        # intentionally NOT registered as a page — it's a hidden power-user
        # subsystem with no UI entry point. Its persisted overrides still apply
        # at boot via color_tokens.load_persisted_overrides(); the accent
        # picker + screen eyedropper on the Display page remain the only colour
        # controls users see.

        # Width the sidebar to its longest label in the ACTIVE font now that
        # every page (and thus every nav row) has been registered — a fixed
        # 128px clipped "Scrobbling"/"Downloads" in a wide/monospace font.
        self._size_nav_to_content()

        self.nav.currentRowChanged.connect(self._on_nav_changed)
        self.nav.setCurrentRow(0)  # fires _on_nav_changed → builds page 0
        # Seed keyboard focus on the sidebar so the dialog opens ready for
        # Up/Down page-switching.
        self.nav.setFocus(Qt.FocusReason.OtherFocusReason)

        # Live-apply: when the accent picker / eyedropper fires
        # theme_changed, re-stamp every accent-baked surface in the
        # dialog — combo borders, ghost-button outlines, restart
        # notice banner, EQ slider handles.
        try:
            PlayerBus.get().theme_changed.connect(
                self._reapply_dialog_accent_styling
            )
        except Exception:
            pass
        # An OS-driven "Auto (follow OS)" swap fires theme_changed but — unlike
        # the in-dialog combo path — doesn't rebuild this dialog's pages, so
        # baked text (nav labels, section headers) keeps its old colour and
        # reads wrong after a light↔dark flip. Track the resolved theme name
        # and rebuild the pages (deferred) whenever it changes from OUTSIDE the
        # dialog. _on_theme_changed updates _last_theme_name itself, so an
        # in-dialog pick won't double-rebuild.
        from jellytoast.theme import get_active_theme as _gat_init

        self._last_theme_name = _gat_init().name
        try:
            PlayerBus.get().theme_changed.connect(self._on_external_theme_changed)
        except Exception:
            pass

    def _adaptive_dialog_size(self) -> tuple[int, int]:
        """The dialog size for the active font. Keeps the tuned 720×620
        balance but scales it: by the font-size scale in BOTH dimensions
        (larger scale → bigger, small → smaller), and by a WIDTH-only factor
        for wide/monospace families so their extra character width doesn't
        overflow. Capped to the screen; residual overflow scrolls per-page."""
        from PySide6.QtGui import QFontInfo
        from PySide6.QtWidgets import QApplication

        from jellytoast.design_tokens import FONT_SCALE

        base_w, base_h = 720, 620
        # HEIGHT scales fully with the font-size scale — bigger text genuinely
        # needs proportionally more vertical room (and the pages scroll anyway).
        h = round(base_h * FONT_SCALE)
        # WIDTH scales only PARTIALLY. The widest text line grows with the font,
        # but the layout has horizontal slack (left-aligned content, right-
        # hugging buttons), so scaling width by the full factor sprawls the
        # dialog and leaves dead space on the right at larger sizes. Half the
        # scale delta keeps it comfortably wide without pushing everything apart.
        # Default scale (1.0) still lands exactly on the tuned 720 ("where we
        # started"). A monospace family packs wider glyphs → a small width-only
        # bump (nothing changes for proportional fonts).
        width_scale = 1.0 + (FONT_SCALE - 1.0) * 0.15
        width_family = 1.06 if QFontInfo(QApplication.font()).fixedPitch() else 1.0
        w = round(base_w * width_scale * width_family)
        scr = self.screen() or QApplication.primaryScreen()
        if scr is not None:
            avail = scr.availableGeometry()
            w = min(w, avail.width() - 64)
            h = min(h, avail.height() - 64)
        return int(w), int(h)

    def _size_nav_to_content(self) -> None:
        """Width the sidebar to its longest label in the active font + the
        item/list padding, so a wide/monospace font never clips a nav row
        (it was a fixed 128px tuned for Inter)."""
        fm = self.nav.fontMetrics()
        text_w = max(
            (fm.horizontalAdvance(self.nav.item(i).text()) for i in range(self.nav.count())),
            default=96,
        )
        # item h-padding (14+14) + list padding (4+4) + generous breathing so a
        # font whose rendered advance slightly exceeds fontMetrics still fits
        # (the horizontal scrollbar is off, so the column must be wide enough).
        self.nav.setFixedWidth(int(text_w) + 28 + 8 + 16)

    def _nav_qss(self) -> str:
        """Sidebar nav QSS — bakes TEXT / TEXT_DIM / ink_alpha(), so
        rebuilt on a live theme switch (see `_rebuild_pages_for_theme`,
        which re-applies it — the nav isn't a page)."""
        return f"""
            QListWidget {{
                background: transparent;
                border: none;
                outline-style: none;
                padding: 4px;
            }}
            QListWidget::item {{
                color: {TEXT_DIM};
                padding: 9px 14px;
                border-radius: {rad(8)}px;
                margin: 2px 0;
            }}
            QListWidget::item:hover {{
                background: {ink_alpha(0.05)};
                color: {TEXT};
            }}
            QListWidget::item:selected {{
                background: {ink_alpha(0.10)};
                color: {TEXT};
            }}
        """

    def _add_page(self, title: str, builder, expand: bool = False):
        """Register a settings page. ``builder`` is a zero-arg callable
        returning the page's content widget — invoked lazily on first
        navigation (``_on_nav_changed``), not here. An empty wrapper is
        added to the stack now so nav indices line up.

        Non-``expand`` pages are wrapped in a QScrollArea so a page
        taller than the fixed dialog (Playback's EQ stack, the Casting
        device list) scrolls instead of clipping off the bottom edge.
        ``expand`` pages manage their own vertical space (they hold
        their own scrolling lists) and are added to the stack as-is."""
        QListWidgetItem(title, self.nav)
        wrap = QWidget()
        wrap.setStyleSheet("background: transparent;")
        v = QVBoxLayout(wrap)
        # Top = 15 to line the first section header (e.g. SERVER) up
        # with the nav's first item text (e.g. "General"). The math:
        # QListWidget QSS sets padding 4 + per-item margin 2 +
        # per-item padding-top 9 = 15 from the body's top edge down
        # to the nav text. Matching that here makes the two columns
        # read as peer rows starting at the same baseline rather
        # than the right pane drifting up or down.
        v.setContentsMargins(20, 15, 20, 14)
        v.setSpacing(0)
        if expand:
            self.stack.addWidget(wrap)
        else:
            scroller = QScrollArea()
            scroller.setWidget(wrap)
            scroller.setWidgetResizable(True)
            scroller.setFrameShape(QFrame.Shape.NoFrame)
            scroller.setHorizontalScrollBarPolicy(
                Qt.ScrollBarPolicy.ScrollBarAlwaysOff
            )
            scroller.setStyleSheet("background: transparent;")
            scroller.viewport().setStyleSheet("background: transparent;")
            # Auto-fading scrollbar — fades out when not actively scrolling,
            # same treatment as the radio / downloads / cast lists.
            from jellytoast.ui_helpers import install_autofade_scrollbars

            install_autofade_scrollbars(scroller)
            self.stack.addWidget(scroller)
        # (builder, expand, wrapper-layout) — wrapper-layout is where the
        # built content is added; the scroll wrapping is transparent to
        # _on_nav_changed / _rebuild_pages_for_theme.
        self._page_builders.append((builder, expand, v))

    def show_page(self, title: str) -> bool:
        """Select the nav page whose title matches ``title`` (lazy-builds
        it via the row-change handler). Returns False if no such page —
        used to deep-link into Settings (e.g. the cast button → Casting).
        """
        for i in range(self.nav.count()):
            if self.nav.item(i).text() == title:
                self.nav.setCurrentRow(i)
                return True
        return False

    def _on_nav_changed(self, idx: int):
        """Build the page's content on first visit, then show it."""
        if 0 <= idx < len(self._page_builders) and idx not in self._built_pages:
            builder, expand, layout = self._page_builders[idx]
            content = builder()
            if expand:
                # Page owns its vertical space (a scrolling list) —
                # let it fill rather than pinning it to the top.
                layout.addWidget(content, 1)
            else:
                layout.addWidget(content)
                layout.addStretch(1)
            self._built_pages.add(idx)
            # Force Qt to polish the lazy-built widgets against the
            # dialog's stylesheet immediately. Without this, combos
            # constructed AFTER the dialog was shown sometimes don't
            # get the dialog-level QSS pass on first show — the user
            # symptom was Playback dropdowns "not working" on first
            # click until you navigated away and back, which is just
            # Qt's natural polish-on-hide-show kicking in.
            content.ensurePolished()
            for child in content.findChildren(QComboBox):
                child.ensurePolished()
        self.stack.setCurrentIndex(idx)

    def _rebuild_pages_for_theme(self):
        """Tear down + rebuild every built page after a light↔dark
        theme switch.

        Pages bake the text-token colours (TEXT / TEXT_DIM /
        TEXT_FAINT / ERROR_FG) into label stylesheets at build time.
        This module imported those names by value, so `refresh_theme()`
        updating `ui_helpers` doesn't reach them and already-built
        pages keep stale colours — invisible until now because the
        three dark themes shared one text palette. Rebind the module
        constants from the refreshed `ui_helpers`, drop the built
        pages, and rebuild the visible one; the rest stay lazy.

        Safe to call from the theme combo's own signal handler only
        when deferred (QTimer.singleShot) — the combo lives on the
        Display page this tears down.
        """
        import shiboken6

        if not shiboken6.isValid(self):
            # The deferral window means a theme change landing as the dialog
            # closes (OS auto-mode flip, accent watcher, keep/revert timeout)
            # can fire this on an already-deleted dialog — a RuntimeError
            # traceback on stderr. Nothing to rebuild; bail.
            return
        global TEXT, TEXT_DIM, TEXT_FAINT, ERROR_FG, BORDER, POPUP_OPAQUE_FILL
        global WASH_HOVER
        from jellytoast import ui_helpers as _u

        TEXT, TEXT_DIM, TEXT_FAINT = _u.TEXT, _u.TEXT_DIM, _u.TEXT_FAINT
        ERROR_FG, BORDER = _u.ERROR_FG, _u.BORDER
        POPUP_OPAQUE_FILL = _u.POPUP_OPAQUE_FILL
        WASH_HOVER = _u.WASH_HOVER

        current = self.stack.currentIndex()
        for idx in list(self._built_pages):
            _builder, _expand, layout = self._page_builders[idx]
            while layout.count():
                item = layout.takeAt(0)
                w = item.widget()
                if w is not None:
                    w.setParent(None)
                    w.deleteLater()
        self._built_pages.clear()
        # The sidebar nav isn't a page — re-stamp its QSS directly, then
        # re-widen it to the labels in the (possibly rescaled) active font.
        self.nav.setStyleSheet(self._nav_qss())
        self._size_nav_to_content()
        # The titlebar is built once and never rebuilt — re-stamp the
        # "Settings" heading and re-issue the cog glyph in the new tint.
        self._title_label.setStyleSheet(f"color: {TEXT}; {type_qss(TYPE_SUBHEAD)}")
        self._title_cog.setPixmap(icon("settings").pixmap(QSize(18, 18)))
        # The info (ⓘ) + close (✕) buttons bake colour-tokens / themed
        # icons too — re-issue the info glyph and re-stamp both QSS blocks
        # so they recolour live instead of only after a dialog reopen.
        self._about_btn.setIcon(icon("info"))
        self._about_btn.setStyleSheet(self._title_about_qss())
        self._title_close_btn.setStyleSheet(self._title_close_qss())
        # Rebuild the page in view now; the rest rebuild lazily on
        # their next visit, which keeps the switch cheap.
        self._on_nav_changed(current)

    def _build_downloads(self) -> QWidget:
        """The Downloads page — the offline-downloads management screen.
        Lazy import keeps the offline package (and its SQLite handle)
        off the settings dialog's construction path until this page is
        actually built."""
        from jellytoast.downloads_view import DownloadsView

        return DownloadsView()

    # ── Title bar ──────────────────────────────────────────────────────
    def _build_titlebar(self) -> QWidget:
        tb = QWidget()
        tb.setFixedHeight(46)
        tb.setObjectName("jtSettingsTitle")
        tb.setStyleSheet("""
            QWidget#jtSettingsTitle { background: transparent; }
            QWidget#jtSettingsTitle QLabel { background: transparent; }
        """)
        h = QHBoxLayout(tb)
        h.setContentsMargins(20, 0, 8, 0)
        h.setSpacing(10)

        cog = QLabel()
        cog.setPixmap(icon("settings").pixmap(QSize(18, 18)))
        h.addWidget(cog)
        self._title_cog = cog

        title = QLabel("Settings")
        title.setStyleSheet(f"color: {TEXT}; {type_qss(TYPE_SUBHEAD)}")
        h.addWidget(title)
        self._title_label = title
        h.addStretch(1)

        # About button (info circle) — opens a small overlay with the
        # name + version + blurb. Replaces the prior About settings page.
        about_btn = IconButton()
        about_btn.setFocusPolicy(Qt.FocusPolicy.TabFocus)  # keyboard-reachable
        about_btn.setIcon(icon("info"))
        about_btn.setIconSize(QSize(18, 18))
        about_btn.setFixedSize(32, 28)
        about_btn.setToolTip("About jellytoast")
        about_btn.setStyleSheet(self._title_about_qss())
        about_btn.clicked.connect(self._show_about)
        h.addWidget(about_btn)
        self._about_btn = about_btn

        close_btn = QPushButton("✕")
        close_btn.setFixedSize(36, 28)
        close_btn.setStyleSheet(self._title_close_qss())
        close_btn.clicked.connect(self.reject)
        h.addWidget(close_btn)
        self._title_close_btn = close_btn

        # Make titlebar draggable via KWin (matches the main window).
        tb.mousePressEvent = self._titlebar_press
        return tb

    @staticmethod
    def _title_about_qss() -> str:
        """QSS for the titlebar info (ⓘ) button. ink_alpha() reads the
        live theme, so re-calling this after a theme switch yields the
        correct hover wash."""
        from jellytoast.ui_helpers import ACCENT

        return f"""
            QPushButton {{ background: transparent; border: 1px solid transparent; border-radius: {rad(6)}px; }}
            QPushButton:hover {{ background: {ink_alpha(0.08)}; border-radius: {rad(6)}px; }}
            QPushButton:focus {{ background: {ink_alpha(0.08)}; border: 1px solid {ACCENT}; border-radius: {rad(6)}px; outline: none; }}
        """

    @staticmethod
    def _title_close_qss() -> str:
        """QSS for the titlebar close (✕) button. Bakes TEXT_DIM / TEXT /
        WASH_HOVER, so it must be re-stamped on a light↔dark switch."""
        return f"""
            QPushButton {{
                background: transparent; color: {TEXT_DIM};
                border: none; border-radius: {rad(6)}px; {type_qss(TYPE_CAPTION)}
            }}
            QPushButton:hover {{ background: {WASH_HOVER}; color: {TEXT}; }}
        """

    def _titlebar_press(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            handle = self.windowHandle()
            if handle is not None:
                handle.startSystemMove()

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
        v.addWidget(self._section_header("SERVER"))
        username = self.s.username
        signed_in = bool(self.s.access_token)
        provider_kind = (self.s.provider_kind or "jellyfin").lower()
        provider_display = "Navidrome" if provider_kind == "subsonic" else "Jellyfin"
        if signed_in:
            if username:
                status_text = f"Signed in to {provider_display} as {username}."
            else:
                status_text = f"Signed in to {provider_display}."
        else:
            status_text = "Not signed in."
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
        url = self.s.server_url or "Not configured"
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
            6, 0,
            QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Minimum,
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
        self._change_btn = QPushButton("Change server URL…")
        self._change_btn.setObjectName("ghost")
        self._change_btn.setFixedHeight(_CTRL_H)
        self._change_btn.setMinimumWidth(_CHANGE_BTN_W)
        self._change_btn.clicked.connect(self._on_change_server_clicked)
        btn_row.addWidget(self._change_btn)
        signout_btn = QPushButton("Sign out")
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
        v.addWidget(self._section_header("STARTUP"))

        # Disk truth (whether the autostart .desktop file exists) wins
        # over the persisted flag — they can drift if the user nukes
        # the file from a file manager. Hidden where no backend can
        # fulfil the toggle (Windows/macOS) — a no-op checkbox reads
        # as a broken one.
        if _autostart.is_supported():
            self._autostart_check = QCheckBox("Launch jellytoast at login")
            self._autostart_check.setChecked(_autostart.is_enabled())
            self._autostart_check.toggled.connect(self._on_autostart_toggled)
            v.addWidget(self._autostart_check)

        self._mini_check = QCheckBox("Show mini player on startup")
        self._mini_check.setChecked(self.s.show_mini_on_start)
        self._mini_check.toggled.connect(lambda val: setattr(self.s, "show_mini_on_start", val))
        v.addWidget(self._mini_check)

        v.addSpacing(12)

        # ── Window ────────────────────────────────────────────────────
        # Behaviour of the open windows themselves — stacking + close
        # semantics. Both toggles change WHERE jellytoast lives on the
        # desktop, not what it does at boot.
        v.addWidget(self._section_header("WINDOW"))

        # Wayland-only: xdg-shell forbids apps from setting their own
        # stacking, so Qt.WindowStaysOnTopHint is a no-op there. We
        # install a KWin window rule to do it compositor-side. The
        # toggle only appears on KDE Wayland — outside that the X11
        # hint already works and there's nothing for us to expose.
        # (On Windows / macOS this row will be replaced by the host-OS
        # equivalent when those backends land.)
        if keep_above_supported():
            self._keep_above_check = QCheckBox("Keep mini player on top")
            self._keep_above_check.setChecked(self.s.mini_player_keep_above)
            self._keep_above_check.toggled.connect(self._on_keep_above_toggled)
            v.addWidget(self._keep_above_check)

        # Tray-on-close lives last — it's the most behavior-changing
        # toggle of the group (kills the close-X exit semantics) so it
        # gets the bottom slot where the eye naturally lands on a
        # "biggest commitment" row.
        self._tray_check = QCheckBox("Hide to system tray when window is closed")
        self._tray_check.setChecked(self.s.minimize_to_tray)
        self._tray_check.toggled.connect(lambda val: setattr(self.s, "minimize_to_tray", val))
        v.addWidget(self._tray_check)

        v.addSpacing(12)

        # ── Notifications ─────────────────────────────────────────────
        # Only offered where a notification backend is actually wired up
        # (notify-send on Linux, WinRT toasts on Windows) — hidden where
        # notify() would be a silent no-op anyway.
        from jellytoast import notifications as _notifications

        if _notifications.is_supported():
            v.addWidget(self._section_header("NOTIFICATIONS"))
            self._track_notify_check = QCheckBox(
                "Show a notification when the track changes"
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
        v.addWidget(self._section_header("HOME PAGE"))

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
            v.addWidget(self._section_header("UPDATES"))
            v.addSpacing(4)
            up_row = QHBoxLayout()
            up_row.setContentsMargins(0, 0, 0, 0)
            up_row.setSpacing(10)
            self._update_check = QCheckBox("Check for updates")
            self._update_check.setChecked(self.s.check_for_updates_enabled)
            self._update_check.toggled.connect(
                lambda val: setattr(self.s, "check_for_updates_enabled", val)
            )
            up_row.addWidget(self._update_check)
            up_row.addWidget(
                self._info_button(
                    "Check for updates",
                    "Once a day, jellytoast quietly checks GitHub for a newer "
                    "release and shows a small chip in the top bar if one's out. "
                    "Manual installs only — the Microsoft Store / Mac App Store "
                    "builds update themselves. Uses GitHub's public API; no "
                    "account or personal data.",
                )
            )
            up_row.addStretch(1)
            self._update_check_now = QPushButton("Check now")
            self._update_check_now.clicked.connect(self._on_check_updates_now)
            up_row.addWidget(self._update_check_now)
            v.addLayout(up_row)

        v.addStretch(1)
        return page

    # Status-dot palette — kept here so a future probe enrichment
    # (orange for partial connectivity, slow response, auth-degraded)
    # has a single place to extend. Tooltip text is what the user sees
    # on hover, so it should be a complete sentence, not a token name.
    # Slightly translucent fills — the dot reads as a status pip, not
    # a primary control, so dropping alpha keeps it from competing
    # with the URL text next to it. Red holds higher alpha since it's
    # the only state the user needs to act on; checking / signed_out
    # sit faintest because they're transient / inactive.
    _CONN_DOT_STATES = {
        "checking": ("rgba(136, 136, 136, 0.65)", "Checking connection…"),
        "reachable": ("rgba(47, 190, 138, 0.75)", "Connected — server is reachable."),
        "issue": ("rgba(224, 115, 92, 0.80)", "Connection issue — degraded response from server."),
        "disconnected": ("rgba(220, 38, 38, 0.85)", "Disconnected — server unreachable. Check URL or network."),
        "signed_out": ("rgba(102, 102, 102, 0.55)", "Not signed in."),
    }

    def _set_conn_dot_state(self, state: str):
        dot = getattr(self, "_conn_dot", None)
        if dot is None:
            return
        color, tip = self._CONN_DOT_STATES.get(state, self._CONN_DOT_STATES["checking"])
        # 8×8 with border-radius 4 → circle.
        dot.setStyleSheet(
            f"QLabel {{ background: {color}; border-radius: {rad(4)}px; }}"
        )
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
        self._change_btn.setText("Save URL")

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
        self._url_edit.setText(self.s.server_url or "Not configured")
        self._url_edit.setCursorPosition(0)
        self._apply_url_edit_chrome(editing=False)
        self._change_btn.setText("Change server URL…")

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
        # Persist user intent, then mutate the filesystem. If the
        # filesystem op fails (e.g. read-only home), the QSettings flag
        # still records what the user asked for so we can retry next
        # time the dialog opens.
        self.s.autostart = on
        ok = _autostart.enable() if on else _autostart.disable()
        # If reality drifted (e.g. enable failed), reflect that in the
        # checkbox without re-firing the toggled signal.
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
        v.addWidget(self._section_header("AUDIO OUTPUT"))
        out_row = QHBoxLayout()
        out_row.setContentsMargins(0, 0, 0, 0)
        out_row.setSpacing(10)
        self._audio_out_combo = _Selector()
        self._audio_out_combo.addItem("Auto (recommended)", "auto")
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
                f"{current_out} (not connected)",
                current_out,
                dot_color=audio_family_dot_color(current_out),
            )
        self._select_combo_by_data(self._audio_out_combo, current_out)
        self._audio_out_combo.currentIndexChanged.connect(self._on_audio_output_changed)
        out_row.addWidget(self._audio_out_combo)
        out_row.addWidget(
            self._info_button(
                "Audio output",
                "Where audio goes. Auto follows the system default. Direct "
                "ALSA devices bypass the PipeWire mixer — the bit-perfect "
                "path — but hold the device exclusively. Applies on the "
                "next track; falls back to Auto if the device won't open.",
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
        v.addWidget(self._section_header("BIT-PERFECT"))
        bp_row = QHBoxLayout()
        bp_row.setContentsMargins(0, 0, 0, 0)
        bp_row.setSpacing(10)
        self._bit_perfect_check = QCheckBox("Bit-perfect mode")
        self._bit_perfect_check.setChecked(self.s.bit_perfect_mode)
        self._bit_perfect_check.toggled.connect(self._on_bit_perfect_toggled)
        bp_row.addWidget(self._bit_perfect_check)
        bp_row.addWidget(
            self._info_button(
                "Bit-perfect mode",
                "Plays the file's exact bits: locks volume at 100% and turns "
                "off Normalization, Equalizer, and Crossfade. The hint below "
                "covers the OS side of the path.",
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
            self._audio_exclusive_check = QCheckBox("Exclusive output")
            self._audio_exclusive_check.setChecked(self.s.audio_exclusive)
            self._audio_exclusive_check.toggled.connect(
                self._on_audio_exclusive_toggled
            )
            excl_row.addWidget(self._audio_exclusive_check)
            excl_row.addWidget(
                self._info_button(
                    "Exclusive output",
                    "Hands jellytoast the device (WASAPI Exclusive · "
                    "CoreAudio HogMode) — other apps go silent. Applies on "
                    "the next track; falls back to shared if the device "
                    "refuses.",
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
                    "PipeWire sample-rate config",
                    "Lets PipeWire follow the source sample rate so 44.1 kHz "
                    "files stop resampling to 48 kHz. Takes effect after "
                    "PipeWire restarts (or next login). Not needed on a "
                    "direct ALSA output.",
                )
            )
            pw_row.addStretch(1)
            v.addLayout(pw_row)
            # Small inline status flash — populated by the click
            # handler after each install / remove so the user sees the
            # action took (without a modal interrupting their flow).
            self._pw_status_label = QLabel("")
            self._pw_status_label.setWordWrap(True)
            self._pw_status_label.setStyleSheet(
                f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}"
            )
            self._pw_status_label.setVisible(False)
            v.addWidget(self._pw_status_label)
            self._refresh_pipewire_button_label()

        # ── Audio ─────────────────────────────────────────────────────
        # Quality (transcode tier) + Normalization (ReplayGain mode)
        # belong together: both decide what the SOURCE stream looks like
        # before it hits the decoder. Header gives the page a consistent
        # rhythm with the Crossfade / Equalizer blocks below.
        v.addWidget(self._section_header("AUDIO"))

        # Single shared form so Quality and Normalization labels +
        # combo boxes column-align cleanly.
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        # Left-justify the whole form. macOS QFormLayout defaults to a CENTERED
        # Aqua form (Linux/Windows already default left), which floats Quality /
        # Normalization mid-dialog instead of at the content margin like the
        # section headers above them.
        form.setFormAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        form.setHorizontalSpacing(16)
        form.setVerticalSpacing(8)
        # Keep the combos at their size hint (grow with the font, don't stretch
        # to fill the field column) — same as the Display page's scaling_form.
        # Their setMinimumWidth floor then holds the tuned default-font width.
        form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint
        )

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

        q_label = self._field_label("Quality:")
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
        n_label = self._field_label("Normalization:")
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
            "Normalization mode:\n"
            "  Off — play untouched.\n"
            "  Track — match each song to a common loudness target.\n"
            "  Album — preserve relative loudness within an album."
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
        v.addWidget(self._section_header("SHUFFLE"))
        shuffle_form = QFormLayout()
        shuffle_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        shuffle_form.setFormAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
        shuffle_form.setHorizontalSpacing(16)
        shuffle_form.setVerticalSpacing(8)
        shuffle_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint
        )
        sq_label = self._field_label("Queue size:")
        sq_label.setMinimumWidth(self._playback_label_w)
        self._shuffle_size_combo = _Selector()
        for label, key in (
            ("50 tracks", 50),
            ("100 tracks", 100),
            ("250 tracks", 250),
            ("500 tracks", 500),
            ("1000 tracks", 1000),
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
        v.addWidget(self._section_header("MEDIA KEYS"))
        v.addSpacing(4)
        mk_row = QHBoxLayout()
        mk_row.setContentsMargins(0, 0, 0, 0)
        mk_row.setSpacing(10)
        self._media_integration_check = QCheckBox("OS media integration")
        self._media_integration_check.setChecked(self.s.media_integration_enabled)
        self._media_integration_check.toggled.connect(
            lambda val: setattr(self.s, "media_integration_enabled", val)
        )
        mk_row.addWidget(self._media_integration_check)
        mk_row.addWidget(
            self._info_button(
                "OS media integration",
                "Exposes jellytoast to your desktop's media controls — the "
                "MPRIS widget and media keys on Linux, the SMTC volume-flyout "
                "and hardware media keys on Windows. Turn off to keep "
                "jellytoast out of the OS media UI. Takes effect on restart.",
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
        wrap.setStyleSheet(
            "QFrame#jtCrossfadeSection { background: transparent; border: none; }"
        )
        wv = QVBoxLayout(wrap)
        wv.setContentsMargins(0, 0, 0, 0)
        wv.setSpacing(10)

        wv.addWidget(self._section_header("CROSSFADE"))
        # Breathing room under the section header before the toggles —
        # without it, the small-caps header sits visually attached to
        # the first checkbox.
        wv.addSpacing(4)

        # Both toggles on one row: parent "Crossfade between tracks"
        # left, "Skip on albums" sibling right. Duration sits beneath
        # both as a sub-option of the parent (only meaningful when
        # crossfade is on; greyed via _refresh_xf_enabled_state).
        toggle_row = QHBoxLayout()
        # 16→28: extra breathing room between the parent "Crossfade
        # between tracks" toggle and its "Skip on albums" sibling, so
        # the sub-option reads as a distinct choice rather than
        # crowding the parent label.
        toggle_row.setSpacing(28)
        self._xf_enabled = QCheckBox("Crossfade between tracks")
        self._xf_enabled.setChecked(self.s.crossfade_enabled)
        self._xf_enabled.toggled.connect(self._on_xf_enabled_toggled)
        toggle_row.addWidget(self._xf_enabled)

        self._xf_smart_album = QCheckBox("Skip on albums")
        self._xf_smart_album.setToolTip(
            "Skip the crossfade between tracks on the same album so "
            "albums that flow as one piece (live sets, concept records) "
            "stay seamless."
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
        _CONTENT_RIGHT_PAD = 46  # mirrors preset_row's trailing pad
        dur_row = QHBoxLayout()
        dur_row.setContentsMargins(0, 0, _CONTENT_RIGHT_PAD, 0)
        dur_row.setSpacing(16)
        self._xf_duration_lbl = self._field_label("Duration:")
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

    # ── Crossfade helpers ───────────────────────────────────────────────────

    def _on_xf_enabled_toggled(self, on: bool):
        self.s.crossfade_enabled = on
        self._refresh_xf_enabled_state()
        PlayerBus.get().crossfade_changed.emit(bool(on))

    def _on_xf_duration_changed(self, ms: int):
        self.s.crossfade_duration_ms = ms
        self._update_xf_duration_label()

    def _update_xf_duration_label(self):
        self._xf_duration_label.setText(f"{self.s.crossfade_duration_ms / 1000.0:.1f} s")

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
        self._xf_duration_lbl.setStyleSheet(
            f"color: {dim_colour}; {type_qss(TYPE_BODY)}"
        )
        self._xf_duration_label.setStyleSheet(
            f"color: {dim_colour}; {type_qss(TYPE_CAPTION)} min-width: 42px;"
        )
        if casting:
            self._xf_caption.setText(
                "Casting — crossfade applies to local playback only and is inactive now."
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

        self._eq_page = EqSettingsPage(self.s, label_col_w=self._playback_label_w)
        self._eq_enabled_check = self._eq_page._eq_enabled_check
        self._eq_linear_phase_check = self._eq_page._eq_linear_phase_check
        self._eq_advanced_check = self._eq_page._eq_advanced_check
        self._refresh_eq_enabled_state = self._eq_page._refresh_eq_enabled_state
        return self._eq_page


    # ── Slider helpers ──────────────────────────────────────────────────────

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



    # ── Page: Casting ──────────────────────────────────────────────────
    def _build_casting(self) -> QWidget:
        # Dedicated page (moved out of Playback 2026-05-16). Covers:
        # per-protocol discovery toggles, when discovery runs, and the
        # stream-routing escape hatch a cast device behind a private
        # network needs to reach the server. Cast protocols are OFF by
        # default (opt-in) — nothing scans the network until the user
        # turns a type on here; the cast button routes to this page when
        # none are enabled.
        page = QWidget()
        page.setStyleSheet("background: transparent;")
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(12)

        # ── Device types ───────────────────────────────────────────────
        # ⓘ holds firewall guidance: on a firewalled host the discovery query
        # leaves fine but the device's reply is dropped, so AirPlay/DLNA never
        # appear (Chromecast usually survives). The hint hands the user a
        # copy-paste allow rule pre-filled with their own LAN subnet.
        dt_row = QHBoxLayout()
        dt_row.setContentsMargins(0, 0, 0, 0)
        dt_row.setSpacing(10)
        dt_row.addWidget(self._section_header("Device types"))
        dt_row.addWidget(
            self._info_button(
                "Cast devices not showing?",
                self._firewall_help_text(),
                tooltip="Firewall help — why AirPlay/DLNA might not appear",
            )
        )
        dt_row.addStretch(1)
        v.addLayout(dt_row)

        dt_caption = QLabel(
            "Casting is off until you turn it on — nothing scans your "
            "network until you enable a protocol below."
        )
        dt_caption.setWordWrap(True)
        dt_caption.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)}")
        v.addWidget(dt_caption)

        # Per-protocol toggle rows. All four backends ship: Chromecast,
        # AirPlay, and DLNA are hardware-verified; Sonos is implemented
        # (deps bundled) and just awaits a final hardware verify.
        # (visible label, attribute name on Settings)
        cast_types = [
            ("Chromecast", "cast_chromecast_enabled"),
            ("AirPlay", "cast_airplay_enabled"),
            ("DLNA / UPnP", "cast_dlna_enabled"),
            ("Sonos", "cast_sonos_enabled"),
        ]
        # One grid for all four rows: every checkbox sits in column 0 so they
        # share a single left edge with even row spacing, and the Sonos ⓘ rides
        # column 1. A grid (rather than per-row addWidget / an HBox just for
        # Sonos) keeps the column flush — Qt offsets a bare addWidget'd control
        # ~2px from a layout-nested one, which made Sonos read 2px right of its
        # neighbours and the rows space unevenly.
        dt_grid = QGridLayout()
        dt_grid.setContentsMargins(0, 0, 0, 0)
        dt_grid.setHorizontalSpacing(10)  # gap before the ⓘ
        dt_grid.setVerticalSpacing(0)
        dt_grid.setColumnStretch(2, 1)
        self._cast_type_checks: dict = {}
        for r, (label, attr) in enumerate(cast_types):
            cb = QCheckBox(label)
            cb.setChecked(bool(getattr(self.s, attr)))
            cb.toggled.connect(lambda val, a=attr: setattr(self.s, a, val))
            self._cast_type_checks[attr] = cb
            # Uniform row height so the ⓘ row doesn't run taller than the rest
            # (even spacing) while the checkbox stays comfortably un-cramped.
            dt_grid.setRowMinimumHeight(r, 26)
            if attr == "cast_sonos_enabled":
                # Sonos carries the transport caveat in an ⓘ right next to it.
                _sonos_info = self._info_button(
                    "Sonos transport",
                    "Mid-cast play/pause, volume, and seek (including the "
                    "skip ± buttons, both directions) are implemented for "
                    "Sonos — but Sonos transport hasn't been tested against "
                    "real hardware yet.",
                    tooltip="Sonos transport: implemented, not yet "
                    "hardware-tested",
                )
                # ⓘ in column 1, tight to "Sonos": every OTHER row spans all
                # three columns (below) so it doesn't widen column 0 — that keeps
                # column 0 sized to the short "Sonos" label, so its ⓘ sits right
                # after it instead of after the widest checkbox. The checkbox is
                # still an addWidget in column 0, so it holds the column-0 edge.
                dt_grid.addWidget(cb, r, 0)
                dt_grid.addWidget(
                    _sonos_info, r, 1, Qt.AlignmentFlag.AlignVCenter
                )
            else:
                dt_grid.addWidget(cb, r, 0, 1, 3)
        v.addLayout(dt_grid)

        v.addSpacing(8)

        # ── Discovery timing ───────────────────────────────────────────
        v.addWidget(self._section_header("Discovery timing"))

        self._discover_at_startup_radio = QRadioButton("Discover at startup")
        self._discover_on_demand_radio = QRadioButton("Discover on cast")
        timing_group = QButtonGroup(self)
        timing_group.addButton(self._discover_at_startup_radio)
        timing_group.addButton(self._discover_on_demand_radio)
        # Keep the group alive on self so it doesn't get GC'd; without
        # it the radios behave as independent checkboxes.
        self._discover_timing_group = timing_group
        current_timing = self.s.cast_discovery_timing
        self._discover_at_startup_radio.setChecked(current_timing == "startup")
        self._discover_on_demand_radio.setChecked(current_timing != "startup")

        def _on_timing_changed(_checked: bool):
            v_ = "startup" if self._discover_at_startup_radio.isChecked() else "on_demand"
            self.s.cast_discovery_timing = v_

        self._discover_at_startup_radio.toggled.connect(_on_timing_changed)
        # Grid (column 0) so the two radios share the device-types column edge
        # and the same even row spacing.
        dtim_grid = QGridLayout()
        dtim_grid.setContentsMargins(0, 0, 0, 0)
        dtim_grid.setVerticalSpacing(0)
        dtim_grid.setColumnStretch(1, 1)
        dtim_grid.addWidget(self._discover_at_startup_radio, 0, 0)
        dtim_grid.addWidget(self._discover_on_demand_radio, 1, 0)
        dtim_grid.setRowMinimumHeight(0, 26)
        dtim_grid.setRowMinimumHeight(1, 26)
        v.addLayout(dtim_grid)

        v.addSpacing(8)

        # ── Stream routing ─────────────────────────────────────────────
        # A cast device can only fetch a URL it can route to. "Network
        # cast" lets the device fetch from the server directly (normal
        # mode); "Route locally" relays the stream through a small
        # local HTTP server on this machine, the escape hatch for
        # offline / Tailscale / self-signed-cert / cross-VLAN setups
        # where the cast device can't reach the server itself. "Auto"
        # picks per-server based on URL reachability. Three-radio form
        # matches the Discovery timing section above so the page reads
        # as a consistent stack of single-pick groups.
        v.addWidget(self._section_header("Stream routing"))

        self._cast_routing_radios: dict = {}
        routing_group = QButtonGroup(self)
        current_routing = self.s.cast_stream_routing or "auto"
        # Grid: radios flush in column 0 (in line under each other + the device /
        # discovery columns), faint hint suffixes in column 1 so "(normal mode)"
        # / "(offline, special case)" align in their own column. The grid also
        # gives even row spacing — the old per-row HBoxes ran the hinted rows
        # taller, so Route locally sat further from Network than Network from Auto.
        routing_grid = QGridLayout()
        routing_grid.setContentsMargins(0, 0, 0, 0)
        routing_grid.setHorizontalSpacing(16)
        routing_grid.setVerticalSpacing(0)
        routing_grid.setColumnStretch(2, 1)
        for r, (label, hint, key) in enumerate(CAST_ROUTING_MODES):
            rb = QRadioButton(label)
            rb.setChecked(key == current_routing)
            routing_group.addButton(rb)
            routing_grid.addWidget(rb, r, 0)
            routing_grid.setRowMinimumHeight(r, 26)  # even rows incl. the hinted ones
            if hint:
                # Faint parenthetical as its own QLabel (QRadioButton's label
                # doesn't render rich text) so it reads dim grey.
                hint_lbl = QLabel(hint)
                hint_lbl.setStyleSheet(
                    f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)}"
                )
                routing_grid.addWidget(
                    hint_lbl, r, 1, Qt.AlignmentFlag.AlignVCenter
                )
            self._cast_routing_radios[key] = rb
        v.addLayout(routing_grid)
        # Keep the group alive on self so it doesn't get GC'd — see
        # the timing_group note above for the same trap.
        self._cast_routing_group = routing_group

        def _on_routing_changed(_checked: bool):
            for key, rb in self._cast_routing_radios.items():
                if rb.isChecked():
                    self.s.cast_stream_routing = key
                    return

        for rb in self._cast_routing_radios.values():
            rb.toggled.connect(_on_routing_changed)

        v.addStretch(1)
        return page

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
            if (
                prior_vol >= 0
                and prior_vol != self.s.volume
                and not bus.cast_active
            ):
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
        for w in ("_quality_combo", "_rg_combo", "_xf_enabled",
                  "_xf_smart_album", "_xf_duration", "_eq_enabled_check",
                  "_eq_linear_phase_check", "_eq_advanced_check"):
            widget = getattr(self, w, None)
            if widget is not None:
                widget.setEnabled(not on)
        quality_is_original = (
            (self.s.audio_quality or "").strip().lower() == "original"
        )
        if hasattr(self, "_audio_exclusive_check"):
            # Defence in depth: even with bit-perfect on, a stale
            # persisted audio_quality="low" (upgrade path, manual
            # QSettings edit) would let the user tick exclusive while
            # the underlying ``_make_mpv_handle`` gate refuses to honour
            # it. Gate the widget here too so the UI and the runtime
            # stay aligned.
            self._audio_exclusive_check.setEnabled(on and quality_is_original)
        if on:
            tip = "Disabled while Bit-perfect mode is on."
        else:
            tip = ""
        for w in ("_quality_combo", "_rg_combo", "_xf_enabled", "_eq_enabled_check"):
            widget = getattr(self, w, None)
            if widget is not None and on:
                widget.setToolTip(tip)
        if hasattr(self, "_audio_exclusive_check"):
            if not on:
                self._audio_exclusive_check.setToolTip(
                    "Enable Bit-perfect mode to use exclusive output."
                )
            elif not quality_is_original:
                self._audio_exclusive_check.setToolTip(
                    "Exclusive output requires audio quality 'Original' — "
                    "the server can't transcode and still ship bit-identical bits."
                )
            else:
                self._audio_exclusive_check.setToolTip("")
        # ReplayGain's existing long-form tooltip is informative; only
        # overwrite it while bit-perfect is on, restore on toggle-off
        # via a separate path (page rebuild + the tooltip restore below).
        if not on and hasattr(self, "_rg_combo"):
            self._rg_combo.setToolTip(
                "Normalization mode:\n"
                "  Off — play untouched.\n"
                "  Track — match each song to a common loudness target.\n"
                "  Album — preserve relative loudness within an album."
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
            has_alsa = any(
                n.startswith("alsa/") for n, _ in self._audio_device_choices()
            )
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

        if _pws.is_installed():
            self._pw_install_btn.setText("Remove PipeWire bit-perfect config")
            self._pw_install_btn.setToolTip(
                f"Removes {_pws.conf_path()}. Restart pipewire to revert."
            )
        else:
            self._pw_install_btn.setText("Install PipeWire bit-perfect config")
            self._pw_install_btn.setToolTip(
                f"Writes {_pws.conf_path()} with rate-matching properties."
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
                msg = "Removed. Restart pipewire to revert the rate-matching properties."
            else:
                # uninstall() returns False if the file lacks our
                # ID-stamp header — surface that honestly rather than
                # claiming success.
                msg = (
                    "Did not remove — the file at that path wasn't "
                    "installed by jellytoast. Edit it by hand if you "
                    "want to revert."
                )
        else:
            try:
                _pws.install()
                msg = (
                    "Installed. Restart pipewire (or log out / in) for "
                    "the rate-matching properties to take effect."
                )
            except OSError as e:
                msg = f"Install failed: {e}"
        self._pw_status_label.setText(msg)
        self._pw_status_label.setVisible(True)
        self._refresh_pipewire_button_label()
        self._refresh_bp_path_hint()

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
            "Restart jellytoast to apply your display changes."
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
        v.addWidget(self._section_header("Theme"))
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
        v.addWidget(self._section_header("Scaling"))

        scaling_form = QFormLayout()
        scaling_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        scaling_form.setFormAlignment(
            Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignTop
        )
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
        scaling_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.FieldsStayAtSizeHint
        )

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
            self._field_label("Font size:"), _left(self._font_size_combo)
        )

        # Font family — app-wide UI text font. Lists installed families that
        # can render Latin text (drops private + symbol/icon fonts so the user
        # can't pick one that renders all text as tofu). "System default" maps
        # to '' = the built-in Inter stack. Writes settings.font_family and
        # triggers the restart notice; applied via the global QSS font stack +
        # app.setFont at boot. Icons are SVG, so they're never affected.
        from PySide6.QtGui import QFont, QFontDatabase

        self._font_family_combo = _Selector()
        self._font_family_combo.addItem("System default", "")
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
        scaling_form.addRow(self._field_label("Font:"), _left(self._font_family_combo))

        # Lyrics font size — wires live to PlayerBus, no restart needed.
        self._lyrics_size_combo = _Selector()
        for label, key in LYRICS_FONT_SIZES:
            self._lyrics_size_combo.addItem(label, key)
        self._select_combo_by_data(self._lyrics_size_combo, self.s.lyrics_font_size)
        self._lyrics_size_combo.currentIndexChanged.connect(self._on_lyrics_size_changed)
        self._lyrics_size_combo.setMinimumWidth(185)  # 281 left → 466 (eyedropper right)
        scaling_form.addRow(
            self._field_label("Lyrics font size:"), _left(self._lyrics_size_combo)
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
        v.addWidget(self._section_header("Now-playing info"))

        def _badge_toggle(attr: str, label: str) -> QCheckBox:
            cb = QCheckBox(label)
            cb.setChecked(bool(getattr(self.s, attr)))

            def _on(val, name=attr):
                setattr(self.s, name, bool(val))
                PlayerBus.get().streaming_info_badges_changed.emit()

            cb.toggled.connect(_on)
            return cb

        self._badge_bit_perfect = _badge_toggle(
            "streaming_info_show_bit_perfect", "Bit Perfect"
        )
        self._badge_eq = _badge_toggle("streaming_info_show_eq", "EQ")
        self._badge_replaygain = _badge_toggle(
            "streaming_info_show_replaygain", "Normalized"
        )
        self._badge_crossfade = _badge_toggle(
            "streaming_info_show_crossfade", "Crossfade"
        )
        self._badge_codec = _badge_toggle(
            "streaming_info_show_codec", "Codec (FLAC, MP3, …)"
        )
        self._badge_bitrate = _badge_toggle(
            "streaming_info_show_bitrate", "Bitrate (kbps)"
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
        v.addWidget(self._section_header("Interface"))

        self._tooltips_check = QCheckBox("Show hover tooltips")
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
            self._native_border_check = QCheckBox("Use native window border")
            self._native_border_check.setChecked(self.s.native_window_border)
            self._native_border_check.toggled.connect(
                lambda val: setattr(self.s, "native_window_border", val)
            )
            nb_row.addWidget(self._native_border_check)
            nb_hint = QLabel("(needs restart)")
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
        self._square_corners_check = QCheckBox("Square corners")
        self._initial_square_corners = self.s.square_corners
        self._square_corners_check.setChecked(self._initial_square_corners)
        self._square_corners_check.toggled.connect(self._on_square_corners_changed)
        sc_row.addWidget(self._square_corners_check)
        sc_hint = QLabel("(needs restart)")
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
            self.setFixedSize(*self._adaptive_dialog_size())  # the dialog scales too
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

    def _refresh_restart_notice_visibility(self):
        """Show the restart banner if a setting that bakes into module
        state differs from the value loaded when the dialog opened.

        ``square_corners`` bakes into ``design_tokens`` at import time, so it's
        the only setting that still needs a restart. Font family + font scale
        now live-apply (``apply_font_settings_live`` — the tokens are recomputed
        + propagated at runtime), and theme mode / accent live-apply via
        ``refresh_theme()`` + ``PlayerBus.theme_changed`` — so none of those are
        part of the dirty check."""
        dirty = self.s.square_corners != getattr(
            self, "_initial_square_corners", self.s.square_corners
        )
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
            hint.setText(f"Frosted glass needs compositor blur — {blur.reason()}.")
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
        self._family_combo.currentIndexChanged.connect(
            lambda _=0: self._on_family_changed()
        )
        top.addWidget(self._family_combo)
        imp = QPushButton("Import…")
        imp.setObjectName("ghost")
        imp.setFixedWidth(self._theme_btn_w)
        imp.setToolTip("Import a base16 .yaml colour scheme (≈250 community themes)")
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
            self._mode_combo.currentIndexChanged.connect(
                lambda _=0: self._on_mode_changed()
            )
            mode_row = QHBoxLayout()
            mode_row.setContentsMargins(0, 0, 0, 0)
            mode_row.setSpacing(10)
            mode_label = self._field_label("Mode:")
            mode_row.addWidget(mode_label)
            mode_row.addWidget(self._mode_combo)
            mode_row.addStretch(1)
            self._mode_combo.setFixedWidth(
                max(120, fam_w - mode_label.sizeHint().width() - 10)
            )
            box.addLayout(mode_row)
        else:
            self._mode_combo = None

        # Frosted / Opaque — always present.
        self._frosted_check = QCheckBox("Frosted glass")
        self._frosted_check.setChecked(self.s.frosted)
        self._frosted_check.setToolTip(
            "Translucent, blurred body (Frosted) vs a solid opaque body."
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
            box.addWidget(self._section_header("Accent color"))
            box.addLayout(self._build_accent_row())
            self._follow_accent_check = QCheckBox("Follow system accent")
            self._follow_accent_check.setChecked(self.s.follow_system_accent)
            self._follow_accent_check.setToolTip(
                "Adopt your desktop's accent colour "
                "(KDE Plasma / GNOME 47+ / Windows / macOS)."
            )
            self._follow_accent_check.toggled.connect(
                self._on_follow_system_accent_toggled
            )
            box.addSpacing(4)
            box.addWidget(self._follow_accent_check)
        else:
            # base16 palette preview — the scheme designates its own accent, so
            # no swatch row; show the 16-colour palette instead.
            base16 = self._current_member_base16()
            if base16:
                box.addSpacing(2)
                box.addWidget(self._section_header("Palette"))
                box.addWidget(self._base16_grid(base16), 0, Qt.AlignmentFlag.AlignLeft)

        # Follow pywal / wallust — Linux ricing loop (any family: following
        # re-themes onto the imported family on the next wallpaper change). The
        # ⓘ carries the "what is this" so the label stays terse.
        if sys.platform.startswith("linux"):
            self._follow_pywal_check = QCheckBox("Follow pywal / wallust")
            self._follow_pywal_check.setChecked(self.s.follow_pywal)
            self._follow_pywal_check.toggled.connect(self._on_follow_pywal_toggled)
            pywal_row = QHBoxLayout()
            pywal_row.setContentsMargins(0, 0, 0, 0)
            pywal_row.setSpacing(10)
            pywal_row.addWidget(self._follow_pywal_check)
            pywal_row.addWidget(
                self._info_button(
                    "Follow pywal / wallust",
                    "pywal / wallust are tools that build themes from your "
                    "wallpaper.",
                )
            )
            pywal_row.addStretch(1)
            box.addSpacing(4)
            box.addLayout(pywal_row)
        else:
            self._follow_pywal_check = None

        return box

    # Glass-opacity slider range — spans the airy jellytoast light default (140)
    # up to near-opaque. Wider than the preset-only band so every family's
    # default is reachable/displayable.
    _GLASS_MIN = 120
    _GLASS_MAX = 245
    # Fixed width for the family dropdown (stable right edge). The Mode combo and
    # the glass slider match it so their right edges line up, and Import + the
    # glass "Default" share one button width so those right-align too.
    _THEME_COMBO_W = 250

    def _theme_button_width(self) -> int:
        """One width for the Import + glass Default ghost buttons so they right-
        align (both sit at family_w + spacing from the shared left edge)."""
        from PySide6.QtGui import QFontMetrics

        fm = QFontMetrics(font(TYPE_BODY))
        text = max(fm.horizontalAdvance("Import…"), fm.horizontalAdvance("Default"))
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
        head.addWidget(self._section_header("Glass opacity"))
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
        reset_btn = QPushButton("Default")
        reset_btn.setObjectName("ghost")
        reset_btn.setFixedWidth(getattr(self, "_theme_btn_w", self._theme_button_width()))
        reset_btn.setToolTip("Reset glass opacity to this theme's default")
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
            self._glass_slider.setValue(
                max(self._GLASS_MIN, min(self._GLASS_MAX, default))
            )
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
        if self.s.theme_family == "imported" and (
            self.s.imported_scheme_path not in folder_paths
        ):
            # One-off import (or pywal) — not represented by a folder row.
            imp = imported_preset_from_settings()
            label = imp.name if imp else "Imported scheme"
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

            return self._strip_icon(
                [FROSTED_DARK.bg, _ACCENT_NOW, "#7c66d0", "#ffffff", "#a8a8a8"]
            )
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
            self._apply_folder_scheme(key[len("file:"):])
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
        self._apply_axes_live(
            key, new_mode, self.s.frosted, prompt=key not in ("", "jellytoast")
        )
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
        self._apply_axes_live(
            fam, new_mode, self.s.frosted, prompt=fam not in ("", "jellytoast")
        )

    def _on_frosted_toggled(self, on: bool) -> None:
        on = bool(on)
        if on == self.s.frosted:
            return
        # Frosted/Opaque changes only the body opacity (base theme name), not the
        # luminance or which controls show — restamp live, no rebuild, no prompt.
        from jellytoast.theme import _resolve_base_name
        from jellytoast.theme_presets import apply_theme_family

        self._last_theme_name = _resolve_base_name(
            self.s.theme_mode, on, self.s.theme_family
        )
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

    # ── Page: Hotkeys ──────────────────────────────────────────────────
    # Every rebindable keyboard shortcut, one live QKeySequenceEdit per
    # row with a per-row Reset and a conflict warning. The bindings
    # themselves are wired in JellytoastWindow.__init__ via
    # jellytoast.hotkeys — Ctrl+F, /, Ctrl+Shift+L, etc.
    def _build_hotkeys(self) -> QWidget:
        from jellytoast import hotkeys as _hotkeys

        page = QWidget()
        page.setStyleSheet("background: transparent;")
        v = QVBoxLayout(page)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)

        # action_id -> (QKeySequenceEdit, warning QLabel, default_seq)
        self._hotkey_edits: dict = {}
        # action_id -> human label, for the conflict message.
        self._hotkey_labels = {
            e["action_id"]: e["label"] for e in _hotkeys.registry_actions()
        }

        v.addWidget(self._section_header("In-app shortcuts"))
        for entry in _hotkeys.registry_actions():
            v.addWidget(self._editable_hotkey_row(entry))

        reset_all = QPushButton("Reset all to defaults")
        reset_all.setObjectName("ghost")
        reset_all.clicked.connect(self._on_hotkeys_reset_all)
        v.addWidget(reset_all, 0, Qt.AlignmentFlag.AlignLeft)

        v.addSpacing(8)
        v.addWidget(self._section_header("Media keys"))
        v.addLayout(self._hotkey_row("Play / Pause", "Media Play"))
        v.addLayout(self._hotkey_row("Next track", "Media Next"))
        v.addLayout(self._hotkey_row("Previous track", "Media Previous"))
        note = QLabel(
            "Media keys route through MPRIS and your desktop's own "
            "shortcut settings — they can't be rebound here."
        )
        note.setWordWrap(True)
        # Wrap to the page's content width (no maxWidth): capping the render
        # width while the layout sizes the label's HEIGHT for the wider column
        # under-allocated it, so the extra wrapped lines overflowed into the row
        # above. The narrower dialog keeps this from running awkwardly wide.
        note.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)}")
        v.addSpacing(6)
        v.addWidget(note)
        v.addStretch(1)
        return page

    def _editable_hotkey_row(self, entry: dict) -> QWidget:
        """One rebindable shortcut — label + a QKeySequenceEdit capture
        field + a per-row Reset, with a hidden inline conflict warning
        below the row."""
        from jellytoast import hotkeys as _hotkeys

        action_id = entry["action_id"]
        wrap = QWidget()
        col = QVBoxLayout(wrap)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)

        row = QHBoxLayout()
        # Trailing right margin pulls the QKeySequenceEdit + Reset
        # button leftward so they don't sit hard against the page's
        # right edge — leaves the right column of the page reading
        # as a column rather than a strip of edge-aligned widgets.
        row.setContentsMargins(0, 0, 140, 0)
        row.setSpacing(8)
        name = QLabel(entry["label"])
        name.setStyleSheet(f"color: {TEXT}; {type_qss(TYPE_BODY)}")
        row.addWidget(name)
        row.addStretch(1)

        edit = QKeySequenceEdit()
        try:
            # One chord per action — no multi-step sequences.
            edit.setMaximumSequenceLength(1)
        except (AttributeError, TypeError):
            pass  # Qt < 6.5 — multi-chord accepted, harmless
        edit.setKeySequence(_hotkeys.get_sequence(action_id, entry["default_seq"]))
        edit.setMaximumWidth(170)
        edit.editingFinished.connect(
            lambda aid=action_id: self._on_hotkey_edited(aid)
        )
        row.addWidget(edit)

        reset_btn = QPushButton("Reset")
        reset_btn.setObjectName("ghost")
        reset_btn.clicked.connect(
            lambda _=False, aid=action_id: self._on_hotkey_reset(aid)
        )
        row.addWidget(reset_btn)
        col.addLayout(row)

        warn = QLabel("")
        warn.setStyleSheet(f"color: {ERROR_FG}; {type_qss(TYPE_CAPTION)}")
        warn.setWordWrap(True)
        warn.setVisible(False)
        col.addWidget(warn)

        self._hotkey_edits[action_id] = (edit, warn, entry["default_seq"])
        return wrap

    # ── Hotkey helpers ──────────────────────────────────────────────────────

    def _hotkey_snapshot(self) -> dict:
        """Live page state — every row's *current* sequence, including
        edits not yet persisted — so the conflict check sees what the
        user is about to have, not just the saved set."""
        return {
            aid: edit.keySequence()
            for aid, (edit, _warn, _default) in self._hotkey_edits.items()
        }

    def _on_hotkey_edited(self, action_id: str):
        from jellytoast import hotkeys as _hotkeys
        from jellytoast.player_state import PlayerBus

        edit, warn, default_seq = self._hotkey_edits[action_id]
        seq = edit.keySequence()
        if seq.isEmpty():
            # Cleared the field — treat as "reset to default".
            self._on_hotkey_reset(action_id)
            return
        conflict = _hotkeys.find_conflict(
            action_id, seq, bindings=self._hotkey_snapshot()
        )
        if conflict:
            other = self._hotkey_labels.get(conflict, conflict)
            warn.setText(f"Already used by “{other}” — pick another.")
            warn.setVisible(True)
            # Snap the field back to the persisted binding.
            edit.blockSignals(True)
            edit.setKeySequence(_hotkeys.get_sequence(action_id, default_seq))
            edit.blockSignals(False)
            return
        warn.setVisible(False)
        _hotkeys.set_sequence(action_id, seq)
        PlayerBus.get().hotkeys_changed.emit()

    def _on_hotkey_reset(self, action_id: str):
        from jellytoast import hotkeys as _hotkeys
        from jellytoast.player_state import PlayerBus

        edit, warn, default_seq = self._hotkey_edits[action_id]
        _hotkeys.reset(action_id)
        edit.blockSignals(True)
        edit.setKeySequence(_hotkeys.get_sequence(action_id, default_seq))
        edit.blockSignals(False)
        warn.setVisible(False)
        PlayerBus.get().hotkeys_changed.emit()

    def _on_hotkeys_reset_all(self):
        from jellytoast import hotkeys as _hotkeys
        from jellytoast.player_state import PlayerBus

        _hotkeys.reset_all()
        for aid, (edit, warn, default_seq) in self._hotkey_edits.items():
            edit.blockSignals(True)
            edit.setKeySequence(_hotkeys.get_sequence(aid, default_seq))
            edit.blockSignals(False)
            warn.setVisible(False)
        PlayerBus.get().hotkeys_changed.emit()

    def _hotkey_row(self, label: str, keys: str) -> QHBoxLayout:
        row = QHBoxLayout()
        # Trailing right margin matches `_editable_hotkey_row` so the
        # binding chips line up under the Reset buttons above rather
        # than drifting all the way to the page's right edge.
        row.setContentsMargins(0, 0, 140, 0)
        name = QLabel(label)
        name.setStyleSheet(f"color: {TEXT}; {type_qss(TYPE_BODY)}")
        row.addWidget(name)
        row.addStretch(1)
        binding = QLabel(keys)
        binding.setStyleSheet(
            f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}"
            f"background: {ink_alpha(0.06)}; padding: 3px 9px; "
            f"border-radius: {rad(5)}px;"
        )
        row.addWidget(binding)
        return row

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
            joined = " and ".join(services)
            warning_text = (
                f"Your server is scrobbling {joined} for you — "
                "the in-app option for that service is turned off so "
                "tracks aren't counted twice."
            )
            # Confirmatory state → accent-tinted. Derived from the LIVE
            # accent (not the old hardcoded purple) + re-stamped on accent
            # change so it follows a swatch swap like every other tinted
            # surface.
            self._scrobble_banner_accent = True
        elif is_navidrome and not check_done:
            warning_text = (
                "This is a Navidrome server. If you've already enabled "
                "Last.fm or ListenBrainz scrobbling there, leave these "
                "off — otherwise every track is counted twice."
            )
            self._scrobble_banner_accent = False
        else:
            warning_text = (
                "If your music server already scrobbles "
                "(e.g. Navidrome's built-in Last.fm / ListenBrainz "
                "integration), leave these off — otherwise every "
                "track is counted twice."
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
        self._scrobble_recheck_btn = QPushButton("Re-check server scrobbling")
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
                "Checking your recent ListenBrainz listens…"
            )

            def _done(res):
                try:
                    self._scrobble_recheck_btn.setEnabled(True)
                    if not getattr(res, "checked", False):
                        self._scrobble_recheck_status.setText(
                            "Couldn't check — ListenBrainz isn't configured, or "
                            "the server was unreachable."
                        )
                    elif getattr(res, "server_scrobbles", False):
                        who = ", ".join(res.foreign_clients) or "another client"
                        self._scrobble_recheck_status.setText(
                            f"Detected another scrobbler ({who}) on your account "
                            "— in-app ListenBrainz paused to avoid duplicates. "
                            "Reopen this page to refresh the controls."
                        )
                    else:
                        self._scrobble_recheck_status.setText(
                            "No second scrobbler found in your recent listens."
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

        self._lb_enabled = QCheckBox("Enable ListenBrainz scrobbling")
        self._lb_enabled.setChecked(self.s.listenbrainz_enabled)
        self._lb_enabled.toggled.connect(lambda v: setattr(self.s, "listenbrainz_enabled", v))
        v.addWidget(self._lb_enabled)
        if srv_lb:
            # Lock the in-app toggle off when a second scrobbler is
            # detected. Tooltip surfaces the *why* on hover.
            self._lb_enabled.setEnabled(False)
            self._lb_enabled.setToolTip(
                "A second scrobbler (your server) is already submitting "
                "these listens to ListenBrainz. In-app scrobbling is paused "
                "to avoid duplicates."
            )
            # Escape hatch: if the detected 'other' scrobbler is actually a
            # different app (not this server), the user can force jellytoast
            # to scrobble anyway (accepting duplicates).
            self._lb_override = QCheckBox("Scrobble from jellytoast anyway")
            self._lb_override.setChecked(self.s.scrobble_in_app_anyway)
            self._lb_override.setToolTip(
                "Submit listens from jellytoast even though another scrobbler "
                "was detected on your ListenBrainz account. Only use this if "
                "that other scrobbler isn't your music server — otherwise "
                "every track is counted twice."
            )
            self._lb_override.toggled.connect(
                lambda val: setattr(self.s, "scrobble_in_app_anyway", val)
            )
            v.addWidget(self._lb_override)

        token_row = QHBoxLayout()
        token_row.setSpacing(8)
        token_row.addWidget(self._field_label("User token"))
        self._lb_token_edit = QLineEdit()
        self._lb_token_edit.setEchoMode(QLineEdit.EchoMode.Password)
        self._lb_token_edit.setText(self.s.listenbrainz_token)
        self._lb_token_edit.setPlaceholderText("Paste from listenbrainz.org/profile/")
        self._lb_token_edit.editingFinished.connect(self._on_lb_token_changed)
        # Cap the token + URL line edits at a sensible width — at full
        # row stretch (the previous setup) they read absurdly wide
        # for the short string they hold. A trailing stretch on the
        # row pushes the Validate button left of the page's right
        # edge to match.
        self._lb_token_edit.setMaximumWidth(260)
        token_row.addWidget(self._lb_token_edit)
        self._lb_validate_btn = QPushButton("Validate")
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
        url_row.addWidget(self._field_label("Server URL"))
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
            self._lf_enabled = QCheckBox("Enable Last.fm scrobbling")
            self._lf_enabled.setChecked(self.s.lastfm_enabled)
            self._lf_enabled.toggled.connect(lambda v: setattr(self.s, "lastfm_enabled", v))
            v.addWidget(self._lf_enabled)
            if srv_lf:
                self._lf_enabled.setEnabled(False)
                self._lf_enabled.setToolTip(
                    "Your Navidrome server is already scrobbling to "
                    "Last.fm. Disable it there to use jellytoast's "
                    "Last.fm client instead."
                )

            lf_row = QHBoxLayout()
            lf_row.setSpacing(8)
            self._lf_status = QLabel(self._lf_status_text())
            self._lf_status.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)}")
            lf_row.addWidget(self._lf_status, 1)
            if self.s.lastfm_session_key:
                self._lf_action_btn = QPushButton("Disconnect")
                self._lf_action_btn.setObjectName("ghost")
                self._lf_action_btn.clicked.connect(self._on_lf_disconnect)
            else:
                self._lf_action_btn = QPushButton("Connect to Last.fm…")
                self._lf_action_btn.setObjectName("ghost")
                self._lf_action_btn.clicked.connect(self._on_lf_connect)
            lf_row.addWidget(self._lf_action_btn)
            v.addLayout(lf_row)

        v.addStretch(1)
        return page

    # ── Scrobbling helpers ─────────────────────────────────────────────

    def _lb_status_text(self) -> str:
        name = self.s.listenbrainz_username
        if name:
            return f"Connected as {name}."
        if self.s.listenbrainz_token:
            return "Token saved — click Validate to confirm."
        return "Not validated yet."

    def _lf_status_text(self) -> str:
        name = self.s.lastfm_username
        if name:
            return f"Connected as {name}."
        return "Not connected."

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
            self._lb_status.setText("Enter a token, then click Validate.")
            return
        # Persist whatever the user typed so a successful validate can
        # be acted on immediately by the manager.
        self.s.listenbrainz_token = token
        self.s.listenbrainz_url = url
        self._lb_status.setText("Validating…")
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
            self._lb_status.setText(f"Connected as {username}.")
        else:
            self.s.listenbrainz_username = ""
            self._lb_status.setText("Couldn't validate — check the token and try again.")

    def _on_lf_connect(self):
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices

        from jellytoast.async_io import run_async
        from jellytoast.scrobble import lastfm as _lf

        # Step 1: get a request token. Step 2: open browser. Step 3:
        # poll auth.getSession every 3s until the user authorizes (or
        # cancels via the dialog's Close).
        self._lf_action_btn.setEnabled(False)
        self._lf_status.setText("Requesting token…")

        def _on_token(token):
            if not token:
                self._lf_status.setText("Couldn't reach Last.fm — try again later.")
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
        dlg.setWindowTitle("Connect to Last.fm")
        dlg.setFixedWidth(380)
        dlg.setModal(True)
        v = QVBoxLayout(dlg)
        v.setContentsMargins(20, 20, 20, 20)
        v.setSpacing(10)
        msg = QLabel(
            'We\'ve opened Last.fm in your browser. Click "Allow" '
            "there, then come back — jellytoast will detect the "
            "approval automatically."
        )
        msg.setWordWrap(True)
        msg.setStyleSheet(f"color: {TEXT}; {type_qss(TYPE_BODY)}")
        v.addWidget(msg)
        status = QLabel("Waiting for authorization…")
        status.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)}")
        v.addWidget(status)
        btn_row = QHBoxLayout()
        btn_row.addStretch(1)
        cancel_btn = QPushButton("Cancel")
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
                    self._lf_action_btn.setText("Disconnect")
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
            self._lf_status.setText("Connection cancelled.")
            self._lf_action_btn.setEnabled(True)

    def _on_lf_disconnect(self):
        self.s.lastfm_session_key = ""
        self.s.lastfm_username = ""
        self.s.lastfm_enabled = False
        self._lf_action_btn.setText("Connect to Last.fm…")
        try:
            self._lf_action_btn.clicked.disconnect()
        except Exception:
            pass
        self._lf_action_btn.clicked.connect(self._on_lf_connect)
        self._lf_status.setText(self._lf_status_text())

    # ── About overlay (replaces the old About page) ────────────────────
    def _show_about(self):
        """Modal info dialog launched from the titlebar info button.
        Lighter than a full settings page since the content is just
        version + blurb — most surveyed music players (Strawberry,
        Supersonic, Feishin, Plexamp) put About in a small dialog
        rather than a settings tab.

        Renders as a borderless frosted pill matching the tooltip /
        volume-popup lift tone (see ``popup_paint_qcolor``). On KDE
        Wayland a noborder KWin rule (jellytoast.keep_above) strips the
        decoration so the custom titlebar reads as the only chrome;
        on other platforms ``FramelessWindowHint`` does the same.
        """
        dlg = _AboutDialog(self)
        dlg.exec()

    # ── helpers ────────────────────────────────────────────────────────
    def _section_header(self, text: str) -> QLabel:
        # font(TYPE_MICRO) handles uppercase + letter-spacing via QFont,
        # so we pass mixed-case text here — Qt's QSS doesn't actually
        # honor text-transform/letter-spacing, only QFont does.
        label = QLabel(text)
        label.setFont(font(TYPE_MICRO))
        label.setStyleSheet(f"color: {TEXT_FAINT};")
        return label

    def _field_label(self, text: str) -> QLabel:
        label = QLabel(text)
        label.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_BODY)}")
        return label

    def _info_button(
        self, title: str, text: str, tooltip: str | None = None
    ) -> QToolButton:
        """A small ⓘ button that holds a setting's descriptive text instead of
        an inline caption — keeps dense sections compact. Hover shows the text
        (tooltip); click opens it in the app-styled frosted dialog (so it works
        even when hover-tooltips are off). ``tooltip`` overrides the hover text
        when ``text`` is too long to read as a tooltip (e.g. multi-line help)."""
        from jellytoast.icons import icon

        btn = IconButton()
        # Keyboard-reachable (IconButton is NoFocus by default) so the help
        # text isn't mouse-only; Space/Enter opens it.
        btn.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        btn.setIcon(icon("info", size=15))
        btn.setIconSize(QSize(15, 15))
        btn.setFixedSize(20, 20)
        btn.setToolTip(text if tooltip is None else tooltip)
        btn.setAccessibleName(f"About {title}")
        from jellytoast.ui_helpers import ACCENT

        btn.setStyleSheet(
            "QPushButton { border: 1px solid transparent; background: transparent; padding: 0; border-radius: {rad(5)}px; }"
            f"QPushButton:focus {{ border: 1px solid {ACCENT}; outline: none; }}"
        )

        def _show():
            from jellytoast.frosted_dialog import frosted_info

            frosted_info(self, title, text)

        btn.clicked.connect(_show)
        return btn

    def _firewall_help_text(self) -> str:
        """Body for the Casting-page firewall ⓘ. A firewall can let the
        discovery query out but block the device's reply, so AirPlay/DLNA never
        appear (Chromecast usually still works — the tell). Pre-fills the host's
        real LAN subnet so the allow rule is copy-paste ready, and tailors the
        steps to the running OS."""
        from jellytoast.cast_manager import _lan_cidrs
        from jellytoast.platform_compat import IS_MACOS, IS_WINDOWS

        try:
            from jellytoast.cast_proxy import _PROXY_PORT
        except Exception:
            _PROXY_PORT = 8943

        cidrs = _lan_cidrs()
        subnet = cidrs[0] if cidrs else "your local subnet"

        intro = (
            "AirPlay and DLNA devices are found by sending discovery requests "
            "on your local network. A firewall can allow the request out but "
            "block the reply, so those devices never appear. If Chromecast "
            "works but AirPlay/DLNA don't, a firewall is the likely cause.\n\n"
        )
        if IS_WINDOWS:
            body = (
                "Windows\n"
                "When Windows asks on first launch, click “Allow access” "
                "for private networks. To change it later: Windows Security → "
                "Firewall & network protection → Allow an app through firewall "
                "→ enable jellytoast on Private networks."
            )
        elif IS_MACOS:
            body = (
                "macOS\n"
                "System Settings → Network → Firewall → Options → "
                "allow incoming connections for jellytoast (or click "
                "“Allow” if prompted on launch)."
            )
        else:
            body = (
                f"Linux — ufw\n    sudo ufw allow from {subnet}\n\n"
                "Linux — firewalld\n"
                "    sudo firewall-cmd --permanent --add-rich-rule="
                f"'rule family=ipv4 source address={subnet} accept'\n"
                "    sudo firewall-cmd --reload"
            )
        ports = (
            "\n\nPorts used: UDP 5353 (mDNS / AirPlay), UDP 1900 (SSDP / DLNA), "
            f"TCP {_PROXY_PORT} (cast proxy)."
        )
        return intro + body + ports

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
            "Import a base16 theme",
            "",
            "base16 schemes (*.yaml *.yml);;All files (*)",
        )
        if not path:
            return
        try:
            with open(path, encoding="utf-8") as f:
                preset = parse_base16_yaml(f.read())
        except (OSError, UnicodeDecodeError, Base16ParseError) as exc:
            QMessageBox.warning(
                self,
                "Import failed",
                f"Couldn't read that as a base16 colour scheme:\n\n{exc}",
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
        show_appearance_revert(
            lambda b=backup: self._revert_theme_axes(b), seconds=10
        )
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
                "Couldn't apply that theme",
                f"Couldn't read {path} as a base16 colour scheme:\n\n{exc}",
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
        show_appearance_revert(
            lambda b=backup: self._revert_theme_axes(b), seconds=10
        )
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
                "pywal palette not found",
                f"{msg}\n\nRun pywal (or wallust) once so it generates a "
                "palette, then turn this back on.",
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
                "No system accent",
                "Your desktop didn't report an accent colour — this works on "
                "KDE Plasma and GNOME 47+.",
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

    def _build_and_apply_dialog_stylesheet(self):
        """Compose the dialog-level QSS that styles every QComboBox
        and QPushButton#ghost inside the dialog. Pulled into its own
        method so we can rebuild it from `_on_accent_picked` to
        live-apply a new accent — re-reads `ACCENT` from
        `jellytoast.ui_helpers` at call time, so the refresh_theme()
        path that ran just before this sees its new value."""
        from jellytoast.icons import icon_svg_path
        from jellytoast.ui_helpers import ACCENT as _ACCENT_NOW
        from jellytoast.ui_helpers import check_url_for_accent

        # Brighter chevron (TEXT, not TEXT_DIM) so the dropdown affordance
        # actually reads as a dropdown — the dim version was too easy to
        # miss against the frosted background.
        chevron_path = icon_svg_path("chevron_down", TEXT)
        chevron_url = chevron_path.replace("\\", "/")
        # Accent-tinted check mark. GLOBAL_STYLE styles QCheckBox, but
        # it's set on the main window and this dialog is a separate
        # top-level — without the rules below the dialog's checkboxes
        # fall back to the native OS rendering (a blue check on Windows,
        # never the accent).
        check_url = check_url_for_accent()
        _ar, _ag, _ab = _hex_to_rgb(_ACCENT_NOW)
        self.setStyleSheet(f"""
            QComboBox {{
                background: {ink_alpha(0.06)};
                color: {TEXT};
                /* Per-side border declarations rather than the shorthand
                   ``border:`` — Qt's QStyleSheetStyle renders QComboBox
                   bottom border at a sub-pixel position when the
                   shorthand drives all four sides, leaving the bottom
                   visibly thinner than the top/sides. Per-side rules
                   force consistent 1-px strokes on every edge. */
                border-top: 1px solid rgba({_ar},{_ag},{_ab},0.45);
                border-bottom: 1px solid rgba({_ar},{_ag},{_ab},0.45);
                border-left: 1px solid rgba({_ar},{_ag},{_ab},0.45);
                border-right: 1px solid rgba({_ar},{_ag},{_ab},0.45);
                border-radius: {rad(6)}px;
                padding: 6px 12px;
                {type_qss(TYPE_BODY)}
                /* No min-height — when paired with the General page's
                   setFixedHeight(34) the combined min-height 22 +
                   padding 12 + border 2 = 36 px natural exceeds the
                   34 px frame, so Qt clipped the bottom row of pixels
                   and the bottom border appeared half-missing.
                   setFixedHeight on callers that want a specific size,
                   natural sizing for the rest. */
                /* Suppress Qt's native focus / selection rectangle —
                   without this the combo paints a faint extra line at
                   the bottom (the focus indicator) that reads as a
                   double-border against our accent QSS border. */
                outline: 0;
                selection-background-color: transparent;
                selection-color: {TEXT};
            }}
            QComboBox:hover {{
                border-color: rgba({_ar},{_ag},{_ab},0.65);
            }}
            QComboBox:focus {{
                border-color: rgba({_ar},{_ag},{_ab},0.85);
            }}
            QComboBox:disabled {{
                color: {TEXT_FAINT};
            }}
            QComboBox::drop-down {{
                border: none;
                width: 28px;
                subcontrol-origin: padding;
                subcontrol-position: right center;
            }}
            QComboBox::down-arrow {{
                image: url({chevron_url});
                width: 14px;
                height: 14px;
            }}
            /* Base text colour for the dialog's plain QPushButtons — e.g.
               "Customize" and the PipeWire conf installer. The dialog sets
               its own stylesheet, and Qt does NOT reliably merge the
               app-global base button-colour rule into a widget that already
               has one, so these kept stale (near-white) text after switching
               to a light theme. Rebuilt on every theme_changed, so it flips
               with the theme; the object-name rules below (#ghost,
               #jtSelector) and per-button stylesheets are more specific and
               still win. */
            QPushButton {{
                color: {TEXT};
            }}
            /* _Selector — QPushButton + QMenu replacement for combos.
               Visual treatment matches QComboBox above; the trailing
               chevron is a QSS background-image pinned to the right
               edge. Text-align left so the value reads from the left
               edge like a combo. Padding-right reserves space for the
               chevron. */
            QPushButton#jtSelector {{
                background: {ink_alpha(0.06)};
                color: {TEXT};
                border: 1px solid rgba({_ar},{_ag},{_ab},0.45);
                border-radius: {rad(6)}px;
                padding: 6px 32px 6px 12px;
                {type_qss(TYPE_BODY)}
                text-align: left;
                outline: 0;
            }}
            QPushButton#jtSelector:hover {{
                border-color: rgba({_ar},{_ag},{_ab},0.65);
            }}
            QPushButton#jtSelector:focus {{
                border-color: rgba({_ar},{_ag},{_ab},0.85);
            }}
            QPushButton#jtSelector:disabled {{
                color: {TEXT_FAINT};
            }}
            QComboBox QAbstractItemView {{
                background: {POPUP_OPAQUE_FILL};
                color: {TEXT};
                border: none;
                border-radius: {rad(8)}px;
                padding: 4px 0px;
                outline: 0;
                selection-background-color: rgba({_ar},{_ag},{_ab},0.85);
                selection-color: white;
            }}
            QComboBox QAbstractItemView::item {{
                padding: 7px 14px;
                min-height: 22px;
                border-radius: {rad(4)}px;
                margin: 1px 4px;
            }}
            QComboBox QAbstractItemView::item:hover {{
                background: rgba({_ar},{_ag},{_ab},0.22);
                color: {TEXT};
            }}
            QComboBox QAbstractItemView::item:selected {{
                background: rgba({_ar},{_ag},{_ab},0.85);
                color: white;
            }}
            QPushButton#ghost {{
                background: {ink_alpha(0.04)};
                color: {TEXT};
                border: 1px solid rgba({_ar},{_ag},{_ab},0.45);
                border-radius: {rad(6)}px;
                padding: 6px 14px;
                {type_qss(TYPE_BODY)}
            }}
            QPushButton#ghost:hover {{
                background: rgba({_ar},{_ag},{_ab},0.10);
                border-color: rgba({_ar},{_ag},{_ab},0.65);
            }}
            QPushButton#ghost:pressed {{
                background: rgba({_ar},{_ag},{_ab},0.18);
                border-color: rgba({_ar},{_ag},{_ab},0.85);
            }}
            QPushButton#ghost:disabled {{
                background: {ink_alpha(0.02)};
                color: {DISABLED_FG};
                border-color: {ink_alpha(0.10)};
            }}
            /* Plain (non-ghost) QPushButton — Save / Delete on the EQ
               row use this. :disabled greys background, text, and
               border so the buttons obviously read as inactive when
               the parent toggle (Equalizer Enable) is off. */
            QPushButton:disabled {{
                background: {ink_alpha(0.02)};
                color: {DISABLED_FG};
                border: 1px solid {ink_alpha(0.08)};
            }}
            QLineEdit, QKeySequenceEdit {{
                background: {ink_alpha(0.06)};
                color: {TEXT};
                border: 1px solid {BORDER};
                border-radius: {rad(6)}px;
                padding: 6px 12px;
                min-height: 22px;
            }}
            QLineEdit:focus, QKeySequenceEdit:focus {{
                border-color: rgba({_ar},{_ag},{_ab},0.85);
            }}
            QCheckBox {{
                color: {TEXT}; spacing: 8px; background: transparent;
            }}
            QCheckBox::indicator {{
                width: 16px; height: 16px;
                border: 1px solid {BORDER}; border-radius: {rad(3)}px;
                background: {ink_alpha(0.04)};
            }}
            QCheckBox::indicator:hover {{
                border-color: {ink_alpha(0.30)};
            }}
            QCheckBox::indicator:checked {{
                background: rgba({_ar},{_ag},{_ab},0.15);
                border: 1px solid rgba({_ar},{_ag},{_ab},0.45);
                image: url({check_url});
            }}
            QCheckBox::indicator:checked:hover {{
                background: rgba({_ar},{_ag},{_ab},0.28);
                border-color: rgba({_ar},{_ag},{_ab},0.65);
            }}
            QCheckBox::indicator:disabled {{
                border-color: {ink_alpha(0.10)};
                background: {ink_alpha(0.02)};
            }}
            /* Checked + disabled — strip the accent fill so a checked
               "Skip on albums" doesn't read at full accent purple when
               its parent crossfade toggle is off. */
            QCheckBox::indicator:checked:disabled {{
                background: {ink_alpha(0.04)};
                border: 1px solid {ink_alpha(0.15)};
                image: none;
            }}
            QCheckBox:disabled {{
                color: {DISABLED_FG};
            }}
            QRadioButton {{
                color: {TEXT}; spacing: 8px; background: transparent;
            }}
            QRadioButton::indicator {{
                width: 16px; height: 16px;
                border: 1px solid {BORDER}; border-radius: 9px;
                background: {ink_alpha(0.04)};
            }}
            QRadioButton::indicator:hover {{
                border-color: {ink_alpha(0.30)};
            }}
            QRadioButton::indicator:checked {{
                background: rgba({_ar},{_ag},{_ab},0.85);
                border: 1px solid rgba({_ar},{_ag},{_ab},0.85);
            }}
            QScrollArea {{ background: transparent; border: none; }}
            QScrollBar:vertical {{
                background: {ink_alpha(0.03)}; width: 8px;
                border-radius: {rad(4)}px; margin: 2px;
            }}
            QScrollBar::handle:vertical {{
                background: rgba({_ar},{_ag},{_ab},0.4);
                border-radius: {rad(4)}px; min-height: 24px;
            }}
            QScrollBar::handle:vertical:hover {{
                background: rgba({_ar},{_ag},{_ab},0.85);
            }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
                height: 0;
            }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
                background: none;
            }}
        """)

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

    def _reapply_dialog_accent_styling(self, accent_only: bool = False):
        """Rebuild every QSS string in the dialog that bakes ACCENT.
        Called on accent change so the dialog reflects the new colour
        immediately instead of waiting for a reopen.

        ``accent_only=True`` is the fast path: the user picked a new
        accent but the theme mode + body fill haven't changed, so we
        can skip the compositor blur toggle (no-op for accent) and the
        body-paint refresh — both of which only matter when
        DIALOG_BODY_COLOR's opacity changes (theme-mode flip)."""
        # Reapply the top-level dialog stylesheet (QComboBox borders,
        # ghost button outlines, popup item styling). The stylesheet
        # is built once in __init__ from `ACCENT` literals; rebuilding
        # means re-running the same code with the now-refreshed
        # module-level constant.
        self._build_and_apply_dialog_stylesheet()
        # Keep the accent preset ring + eyedropper swatch in sync with the live
        # accent, so a "Follow system accent" update (which lands from outside the
        # dialog) shows the current system colour in the eyedropper. No-op when
        # the accent row isn't built.
        self._refresh_accent_swatches(self.s.accent_color)
        # Restart notice fill / left bar use the active accent. Rebuild
        # its inline stylesheet so its colour matches the new accent.
        # hasattr-guarded — the Display page (which owns the notice) is
        # built lazily, so this slot can fire before it exists (e.g. a
        # Colors-page slider drag while Display was never opened).
        if hasattr(self, "_theme_restart_notice"):
            from jellytoast.ui_helpers import ACCENT as _ACCENT_NOW

            _ar2, _ag2, _ab2 = _hex_to_rgb(_ACCENT_NOW)
            self._theme_restart_notice.setStyleSheet(
                f"color: {TEXT}; "
                f"background: rgba({_ar2},{_ag2},{_ab2},0.18); "
                f"border-radius: {rad(6)}px; "
                f"padding: 10px 14px; "
                f"{type_qss(TYPE_CAPTION)}"
            )
        # Scrobbling page's "your server is scrobbling for you" banner is
        # accent-tinted in its confirmatory state — re-stamp so it follows
        # a live accent swap. Guarded inside the helper (no-op before the
        # Scrobbling page is built).
        self._restamp_scrobble_banner()
        # _Selector builds its menu fresh on every open via opaque_menu(),
        # which re-reads the current accent each call — so a theme
        # change is picked up automatically. No cache to reset.
        # EQ slider handles bake ACCENT into their per-slider QSS at
        # construction time — re-stamp so the dot changes colour
        # live with the accent. No-op if the EQ section hasn't been
        # built yet (e.g. the user hasn't visited the Playback page).
        if hasattr(self, "_eq_page"):
            self._eq_page.reapply_accent()
        # Crossfade duration slider bakes ACCENT too — re-stamp so its
        # filled track + hover-handle colour follows live.
        if hasattr(self, "_xf_duration"):
            self._xf_duration.setStyleSheet(self._horiz_slider_qss())
        # Re-polish every QCheckBox / QRadioButton inside the dialog.
        # The setStyleSheet call above on `self` invalidates child
        # styling and Qt won't re-evaluate the QApplication-level
        # `QCheckBox::indicator:checked { background: ACCENT_DEEP }`
        # rule without an explicit polish kick. Without this, the
        # main window's _cascade_global_style does run + repolish all
        # checkboxes app-wide, but the dialog's setStyleSheet (which
        # fires AFTER on theme_changed via this method's subscribe)
        # undoes that work — checkboxes stick at the old accent.
        #
        # Skip invisible widgets — on a theme-mode change the deferred
        # _rebuild_pages_for_theme is about to destroy every checkbox
        # in lazy-built non-current pages, so polishing them now is
        # work on dead widgets. On an accent change those pages aren't
        # rebuilt, but their checkboxes will repolish via Qt's
        # showEvent chain the next time the user visits the page.
        for cb in self.findChildren(QCheckBox):
            if not cb.isVisible():
                continue
            cb.style().unpolish(cb)
            cb.style().polish(cb)
            cb.update()
        for rb in self.findChildren(QRadioButton):
            if not rb.isVisible():
                continue
            rb.style().unpolish(rb)
            rb.style().polish(rb)
            rb.update()
        # Repaint the dialog's own body — paintEvent fills
        # DIALOG_BODY_COLOR, which differs across theme modes (frosted
        # vs solid vs transparent). A QSS re-stamp alone won't trigger
        # the custom paintEvent. Skip on the accent-only fast path —
        # accent doesn't change DIALOG_BODY_COLOR, so the existing
        # paint is still correct, and skipping saves a full repaint.
        if not accent_only:
            self.update()
            # Frosted blurs behind the dialog; Transparent / Solid
            # don't. Defer so the paintEvent triggered by self.update()
            # above has already refreshed the body fill, and toggle
            # off→on so KWin re-installs the blur even when the previous
            # theme also had blur (re-issuing the same enableBlurBehind
            # call is otherwise deduped and the live-switch lands
            # without visible blur). Accent picks don't touch blur, so
            # the toggle is a wasted compositor round-trip there.
            QTimer.singleShot(0, lambda: self._apply_blur(force_refresh=True))

    def _apply_blur(self, force_refresh: bool = False):
        """Blur behind the dialog when the active theme is frosted.
        Region shaped to the BODY_RADIUS rounded rect so the blur
        doesn't bleed past the dialog's transparent corners. Silent
        no-op where the compositor has no blur support.

        ``force_refresh=True`` toggles blur off then on — used on live
        theme switches where the compositor would otherwise dedupe the
        re-issued enable call and leave the dialog unblurred."""
        from jellytoast import blur
        from jellytoast.theme import get_active_theme

        want_blur = get_active_theme().blur
        if force_refresh and want_blur:
            blur.apply(self, False, corner_radius=self.BODY_RADIUS)
        blur.apply(self, want_blur, corner_radius=self.BODY_RADIUS)

    def showEvent(self, e):
        super().showEvent(e)
        # Apply compositor blur once the dialog has a mapped surface.
        QTimer.singleShot(0, self._apply_blur)
        QTimer.singleShot(0, self._polish_current_page)
        QTimer.singleShot(0, self.activateWindow)
        QTimer.singleShot(0, self.raise_)

    def _polish_current_page(self):
        current = self.stack.currentWidget()
        if current is None:
            return
        current.ensurePolished()
        for child in current.findChildren(QComboBox):
            child.ensurePolished()

    def _select_combo_by_data(self, combo: QComboBox, key: str):
        for i in range(combo.count()):
            if combo.itemData(i) == key:
                combo.setCurrentIndex(i)
                return
        combo.setCurrentIndex(0)

    def keyPressEvent(self, e):
        # Esc closes the dialog. QDialog wires this by default, but the
        # frameless + WA_TranslucentBackground combo on KDE Wayland
        # doesn't reliably route the key event to QDialog's own
        # handler, so make the binding explicit. An open combo popup
        # still eats Esc first (closing just the popup), which is the
        # behaviour we want.
        if e.key() == Qt.Key.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(e)

    def paintEvent(self, e):
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
            # Read live from ui_helpers — the `from … import` binding is
            # frozen at import, but refresh_theme() rebinds the module
            # attribute on a theme-mode switch (body opacity differs
            # across modes).
            from jellytoast import ui_helpers as _uih

            p.setBrush(QColor(*_uih.body_color_tuple("dialog")))
            p.setPen(Qt.PenStyle.NoPen)
            p.drawPath(path)
        finally:
            p.end()
