"""
Floating mini player.
Two modes:
  - Compact: 360×96 — artwork + title + transport (great for audio)
  - Expanded: 320×420 — large artwork + full controls + queue peek

The mini player is frameless, always-on-top, and draggable.
"""

from PySide6.QtCore import QPoint, QSize, Qt, QTimer, Slot
from PySide6.QtGui import QColor, QFont, QPainter, QPainterPath, QPixmap
from PySide6.QtWidgets import (
    QApplication,
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from jellytoast.async_io import run_async
from jellytoast.design_tokens import RADIUS_WINDOW, TYPE_CAPTION, TYPE_TINY, type_qss
from jellytoast.icon_button import IconButton
from jellytoast.icons import accent_icon, icon
from jellytoast.player_state import NowPlaying, PlayerBus, get_now_playing
from jellytoast.providers import get_provider
from jellytoast.settings import get_settings
from jellytoast.ui_helpers import (
    IDLE_TEXT,
    TEXT,
    TEXT_DIM,
    WASH_HOVER,
    WASH_PRESSED,
    CoverOverlayButton,
    ScrubbableSlider,
    ink_alpha,
    load_image_async,
    overlay_disc_colors,
    screen_dpr,
    skip_taskbar_x11,
)
from jellytoast.ui_helpers import (
    MarqueeLabel as _MarqueeLabel,
)
from jellytoast.volume_button import VolumeButton

QWIDGETSIZE_MAX = 16777215
# Window-body corner radius — matched to the host OS (see RADIUS_WINDOW).
# Also rounds the mini player's left-edge album art so its corners sit
# flush with the body.
BODY_RADIUS = RADIUS_WINDOW


def _round_all_corners(pix: QPixmap, radius: int) -> QPixmap:
    """Return a copy of pix with all four corners rounded."""
    if pix.isNull() or radius <= 0:
        return pix
    out = QPixmap(pix.size())
    out.fill(Qt.GlobalColor.transparent)
    painter = QPainter(out)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0.0, 0.0, float(pix.width()), float(pix.height()), radius, radius)
    painter.setClipPath(path)
    painter.drawPixmap(0, 0, pix)
    painter.end()
    return out


def _render_cover_placeholder(
    size_logical: int,
    dpr: float,
    radius_logical: int,
) -> QPixmap:
    """Cover slot fallback for when nothing's playing or art failed to
    load. Fully transparent pixmap with only a centered music-note
    glyph — the mini player's body paint shows through, so the
    placeholder reads as "the cover slot is empty" rather than a
    distinct gradient pretending to be art. radius_logical is
    accepted for API symmetry with the real-art path but unused
    here since there's no fill to round."""
    del radius_logical  # transparent fill needs no clip path
    phys = max(1, int(round(size_logical * dpr)))
    out = QPixmap(phys, phys)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    try:
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        glyph_font = QFont()
        glyph_font.setPixelSize(max(1, int(phys * 0.378)))
        p.setFont(glyph_font)
        # Theme ink at low alpha — a faint note that reads on the body
        # in either theme (white on dark, near-black on light).
        from jellytoast.theme import ink_rgb as _ink_rgb

        p.setPen(QColor(*_ink_rgb(), 55))
        p.drawText(
            out.rect(),
            int(Qt.AlignmentFlag.AlignCenter),
            "♪",
        )
    finally:
        p.end()
    out.setDevicePixelRatio(dpr)
    return out


# ── Helpers ──────────────────────────────────────────────────────────────────


def _icon_btn_qss() -> str:
    """Transport-button QSS — bakes the WASH_* tokens, so rebuilt on a
    live theme switch (see `_apply_panel_theme`)."""
    return f"""
        QPushButton {{
            background: transparent; border: none; border-radius: 8px;
        }}
        QPushButton:hover {{ background: {WASH_HOVER}; }}
        QPushButton:pressed {{ background: {WASH_PRESSED}; }}
    """


def _panel_progress_qss() -> str:
    """Hairline progress-slider QSS for both mini-player panels —
    bakes ink_alpha(), so rebuilt on a live theme switch."""
    return f"""
        QSlider::groove:horizontal {{
            height: 1px; background: {ink_alpha(0.10)}; border-radius: 0px;
        }}
        QSlider::sub-page:horizontal {{
            background: {ink_alpha(0.55)}; border-radius: 0px;
        }}
        QSlider::handle:horizontal {{
            width: 0px; height: 0px; margin: 0; background: transparent;
            border: none;
        }}
    """


_CLOSE_BTN_SIZE = 28

# Fixed worst-case-DPR source size for the mini cover (320 logical × 3).
# Decouples the SERVER fetch width from raw screen_dpr() so the L2 raw
# cache (keyed by image id) stays one entry per album across launches —
# Wayland fractional-DPR drift would otherwise pin a fresh raw per session
# and make the server re-encode each time. The DPR-aware target_px below
# still drives the LOCAL decode/scale. Mirrors now_playing_bar._BAR_SOURCE_PX.
_MINI_SOURCE_PX = 960


def _close_btn_qss() -> str:
    """Mini-player close button QSS — matches CoverOverlayButton (the
    favourite heart): a translucent circle, no rim, with a theme-tinted
    ``win_close`` icon. The disc tone tracks the theme via
    overlay_disc_colors() (light disc on a light theme), so it's
    rebuilt on a live switch in `_reapply_theme`."""
    normal, hover = overlay_disc_colors()
    return f"""
        QPushButton {{
            background: {normal};
            border: none;
            border-radius: {_CLOSE_BTN_SIZE // 2}px;
        }}
        QPushButton:hover {{ background: {hover}; }}
    """


def _icon_button(
    name: str, size: int = 30, icon_size: int | None = None, accent: bool = False
) -> QPushButton:
    """Mini-player transport button. Uses the shared SVG icon registry so
    every player chrome (top bar, bottom bar, mini) shares glyph geometry.
    `accent=True` paints the icon in accent (use for the play button when
    you want a primary-action emphasis).

    The icon name + accent flag are stashed as ``_jt_icon`` /
    ``_jt_icon_accent`` properties so a live theme switch can re-issue
    the glyph in the new tint (see `FloatingMiniPlayer._reapply_theme`).

    (IconButton is NoFocus by default, so the button never picks up Qt's
    focus ring when focus snaps onto a transport button after a mode toggle.)"""
    btn = IconButton()
    btn.setIcon(accent_icon(name) if accent else icon(name))
    btn.setProperty("_jt_icon", name)
    btn.setProperty("_jt_icon_accent", accent)
    isz = icon_size if icon_size is not None else max(14, int(size * 0.55))
    btn.setIconSize(QSize(isz, isz))
    btn.setFixedSize(size, size)
    btn.setStyleSheet(_icon_btn_qss())
    return btn


def _apply_panel_theme(panel, playing: bool) -> None:
    """Re-stamp every theme-dependent style on a mini-player panel —
    the compact and expanded panels share widget attribute names, so
    one function covers both. ``playing`` picks the title colour
    (full-strength TEXT for a live track, dimmed IDLE_TEXT for the
    idle placeholder).

    Reads the live ``ui_helpers`` tokens (not the module-frozen
    ``from import`` copies) so a dark↔light flip actually recolors the
    text — bare ``TEXT``/``TEXT_DIM`` would re-stamp to the startup
    palette."""
    from jellytoast import ui_helpers as _uih

    panel.title.setStyleSheet(
        f"color: {_uih.TEXT if playing else _uih.IDLE_TEXT}; "
        f"{type_qss(TYPE_CAPTION)} font-weight: 500;"
    )
    panel.subtitle.setStyleSheet(f"color: {_uih.TEXT_DIM}; {type_qss(TYPE_TINY)}")
    panel.progress.setStyleSheet(_panel_progress_qss())
    for btn in (panel.prev_btn, panel.play_btn, panel.next_btn):
        btn.setStyleSheet(_icon_btn_qss())


# ── Compact mode ─────────────────────────────────────────────────────────────


class _SubField:
    """Duck-typed setText forwarder used by _CompactBar so the parent panel
    can call panel.artist.setText(...) / panel.album.setText(...) the same
    way it does on the expanded panel — except here both feed a single
    joined "artist · album" subtitle label."""

    def __init__(self, owner, attr_name: str):
        self._owner = owner
        self._attr = attr_name

    def setText(self, text: str):
        setattr(self._owner, self._attr, text or "")
        self._owner._refresh_subtitle()


class _CompactBar(QWidget):
    """Super-compact view: tiny square album art on the left, three stacked
    rows on the right — title (own row, full width so it rarely needs to
    marquee), artist · album subtitle, and a progress bar with three
    transport buttons. Shuffle / repeat live only in the expanded view."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.bus = PlayerBus.get()
        layout = QHBoxLayout(self)
        # No bezel — cover fills the left side edge-to-edge.
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Album art — square, full body height. Resized by
        # FloatingMiniPlayer.resizeEvent. All four corners are rounded with
        # BODY_RADIUS so the left corners match the body and the right
        # corners look like a rounded card edge against the right strip.
        self.thumb = QLabel()
        self.thumb.setFixedSize(96, 96)
        self.thumb.setStyleSheet("background: transparent;")
        self.thumb.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cover_orig: QPixmap | None = None
        # Heart overlay — only visible while hovering the cover. Wires
        # into the same favorite_toggled bus signal as the bottom bar
        # so any surface (bar / mini compact / mini expanded /
        # now-playing page) reflects the current state.
        self.fav_btn = CoverOverlayButton(self.thumb, size=24, margin=6, bordered=False)
        self.fav_btn.setIcon(icon("favorite_outline"))
        self.fav_btn.setIconSize(QSize(13, 13))
        self.fav_btn.setToolTip("Favorite")
        self.fav_btn.clicked.connect(self._toggle_favorite)
        self.bus.favorite_toggled.connect(self._on_favorite_toggled)
        layout.addWidget(self.thumb)

        right = QVBoxLayout()
        right.setContentsMargins(12, 6, 12, 6)
        right.setSpacing(2)

        # Row 1: title — its own row, centered in the right strip so the
        # whole right side reads as a tidy card. Marquees only when the title
        # is too long for the strip.
        self.title = _MarqueeLabel()
        self.title.setText("Nothing Playing")
        # TYPE_CAPTION carries the 12px size; the 500-weight override
        # gives the title a slight emphasis over the subtitle without
        # promoting it to TYPE_BODY (which would push to 13px). Idle
        # color matches the inactive icon color (icons.ICON_DIM =
        # #a8a8a8) so "Nothing Playing" visually pairs with the
        # transport buttons; FloatingMiniPlayer._on_started swaps to
        # TEXT when a real track lands.
        self.title.setStyleSheet(f"color: {IDLE_TEXT}; {type_qss(TYPE_CAPTION)} font-weight: 500;")
        self.title.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)

        # Subtitle holds artist + album joined with a bullet. The parent
        # writes via panel.artist.setText / panel.album.setText, so we
        # expose two duck-typed forwarders that update the joined label.
        # _MarqueeLabel scrolls slowly when artist + album exceeds the
        # narrow right strip's width (common for long album titles).
        self.subtitle = _MarqueeLabel()
        self.subtitle.setText("")
        self.subtitle.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_TINY)}")
        self.subtitle.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
        self._artist_text = ""
        self._album_text = ""
        self.artist = _SubField(self, "_artist_text")
        self.album = _SubField(self, "_album_text")

        # Title + artist · album hug as one "now playing" text block, then a
        # wider gap (2× the gap below the progress) separates them from the
        # progress bar so the info reads grouped rather than evenly scattered.
        right.addWidget(self.title)
        right.addWidget(self.subtitle)
        right.addStretch(2)

        # Row 3: minimal progress bar — hairline groove, no visible handle.
        # Stretches to ~75% of the strip width (1:6:1) and stays centered, so
        # it reads as tied to the track info above instead of a short floating
        # island. Still draggable — clicking the groove jumps the value.
        progress_row = QHBoxLayout()
        progress_row.setContentsMargins(0, 0, 0, 0)
        progress_row.setSpacing(0)
        self.progress = ScrubbableSlider(Qt.Orientation.Horizontal)
        self.progress.setFixedHeight(2)
        self.progress.setRange(0, 1000)
        self.progress.setStyleSheet(_panel_progress_qss())
        self.progress.sliderMoved.connect(self._on_seek)
        progress_row.addStretch(1)
        progress_row.addWidget(self.progress, 6)
        progress_row.addStretch(1)
        right.addLayout(progress_row)
        right.addStretch(1)

        # Row 4: transport buttons, centered under the progress bar. Force
        # vertical centering so prev/next align on the play button's centerline
        # (their natural cell anchors at the top otherwise).
        controls_row = QHBoxLayout()
        controls_row.setSpacing(2)
        self.prev_btn = _icon_button("prev", 30)
        self.play_btn = _icon_button("play", 36, icon_size=18)
        self.next_btn = _icon_button("next", 30)
        self.prev_btn.clicked.connect(lambda: self.bus.prev_track.emit())
        self.play_btn.clicked.connect(lambda: self.bus.pause_toggled.emit())
        self.next_btn.clicked.connect(lambda: self.bus.next_track.emit())
        controls_row.addStretch()
        controls_row.addWidget(self.prev_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        controls_row.addWidget(self.play_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        controls_row.addWidget(self.next_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        controls_row.addStretch()
        right.addLayout(controls_row)

        layout.addLayout(right, 1)

        # Seed the cover slot with the placeholder so a cold-start
        # mini player reads complete before the first track loads.
        self.refresh_cover()

    def _refresh_subtitle(self):
        a = (self._artist_text or "").strip()
        b = (self._album_text or "").strip()
        if a and b:
            self.subtitle.setText(f"{a}  ·  {b}")
        else:
            self.subtitle.setText(a or b)

    def _on_seek(self, value: int):
        np = get_now_playing()
        if np.duration > 0:
            self.bus.seek_requested.emit(int(value / 1000 * np.duration))

    def set_cover_pixmap(self, pix: QPixmap):
        self._cover_orig = pix
        self.refresh_cover()

    def refresh_cover(self):
        s = self.thumb.size()
        if s.width() <= 0 or s.height() <= 0:
            return
        dpr = screen_dpr(self)
        if self._cover_orig is None or self._cover_orig.isNull():
            # Nothing playing / cover missing — render the placeholder
            # instead of leaving the slot empty.
            self.thumb.setPixmap(
                _render_cover_placeholder(
                    min(s.width(), s.height()),
                    dpr,
                    BODY_RADIUS,
                )
            )
            return
        # HiDPI: render at physical pixels and DPR-tag the result so
        # Qt paints at logical size with a full-resolution texture.
        phys_w = max(s.width(), int(round(s.width() * dpr)))
        phys_h = max(s.height(), int(round(s.height() * dpr)))
        scaled = self._cover_orig.scaled(
            phys_w,
            phys_h,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        # Non-square source art comes back oversized after Expanding —
        # center-crop to the physical target so the rounded corners we
        # draw below land at the visible edges instead of getting
        # clipped off.
        if scaled.width() != phys_w or scaled.height() != phys_h:
            x = (scaled.width() - phys_w) // 2
            y = (scaled.height() - phys_h) // 2
            scaled = scaled.copy(x, y, phys_w, phys_h)
        scaled = _round_all_corners(scaled, int(round(BODY_RADIUS * dpr)))
        scaled.setDevicePixelRatio(dpr)
        self.thumb.setPixmap(scaled)

    def _toggle_favorite(self):
        np = get_now_playing()
        if not np.item_id:
            return
        new_state = not np.is_favorite
        run_async(get_provider().toggle_favorite, np.item_id, new_state)
        np.is_favorite = new_state
        self.bus.favorite_toggled.emit(np.item_id, new_state)

    @Slot(str, bool)
    def _on_favorite_toggled(self, item_id: str, fav: bool):
        np = get_now_playing()
        if np.item_id != item_id:
            return
        self.fav_btn.setIcon(accent_icon("favorite_filled") if fav else icon("favorite_outline"))


# ── Expanded mode ────────────────────────────────────────────────────────────

# Bar height shared by both modes. In compact the bar fills the right side
# (full window height). In expanded the bar sits below the cover with this
# height, and the window's total height is body_w + BAR_HEIGHT.
_BAR_HEIGHT = 96


class _ExpandedPanel(QWidget):
    """Cover on top, then a bar that mirrors the compact view's right
    strip exactly — title (marquee), subtitle (marquee), hairline
    scrubbable progress, three transport buttons (prev/play/next).

    No shuffle/repeat: that's intentional symmetry with the compact
    view, so toggling between modes only changes where the cover lives
    (left vs top), not the bar's grammar. The bar instance itself isn't
    shared across modes — it's a duplicated layout — but FloatingMini-
    Player's toggle anchors the window so the bar appears stationary
    during the swap."""

    BAR_HEIGHT = _BAR_HEIGHT

    def __init__(self, parent=None):
        super().__init__(parent)
        self.bus = PlayerBus.get()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # Album art — full body width, square. FloatingMiniPlayer.
        # resizeEvent sets the fixed size to body_w × body_w on every
        # resize. The source pixmap is kept at high res so rescaling
        # stays crisp as the player grows.
        self.cover = QLabel()
        self.cover.setFixedSize(320, 320)
        self.cover.setStyleSheet("background: transparent;")
        self.cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cover_orig: QPixmap | None = None
        # Heart overlay — only visible while hovering the cover. Larger
        # touch target (32px) than the compact 24px since the expanded
        # cover is much larger and the user has more room to land
        # precisely.
        self.fav_btn = CoverOverlayButton(self.cover, size=28, margin=10, bordered=False)
        self.fav_btn.setIcon(icon("favorite_outline"))
        self.fav_btn.setIconSize(QSize(16, 16))
        self.fav_btn.setToolTip("Favorite")
        self.fav_btn.clicked.connect(self._toggle_favorite)
        self.bus.favorite_toggled.connect(self._on_favorite_toggled)
        layout.addWidget(self.cover)

        # Bar — visually identical to compact's right strip.
        bar = QWidget()
        bar.setFixedHeight(self.BAR_HEIGHT)
        bar.setStyleSheet("background: transparent;")
        right = QVBoxLayout(bar)
        right.setContentsMargins(12, 6, 12, 6)
        right.setSpacing(2)

        self.title = _MarqueeLabel()
        self.title.setText("Nothing Playing")
        # Mirrors the compact panel's idle styling — matches the
        # inactive icon color (#a8a8a8) so the placeholder pairs
        # visually with the transport buttons. FloatingMiniPlayer
        # flips back to TEXT on playback start.
        self.title.setStyleSheet(f"color: {IDLE_TEXT}; {type_qss(TYPE_CAPTION)} font-weight: 500;")
        self.title.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)

        # Joined "artist · album" subtitle. Same _SubField forwarder
        # pattern as the compact view so the parent's panel.artist /
        # panel.album setters work uniformly.
        self.subtitle = _MarqueeLabel()
        self.subtitle.setText("")
        self.subtitle.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_TINY)}")
        self.subtitle.setAlignment(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter)
        self._artist_text = ""
        self._album_text = ""
        self.artist = _SubField(self, "_artist_text")
        self.album = _SubField(self, "_album_text")

        # Title + artist · album hug as one text block; a wider gap (2×)
        # separates them from the progress bar (matches the compact panel).
        right.addWidget(self.title)
        right.addWidget(self.subtitle)
        right.addStretch(2)

        # Progress — hairline 1px groove, fixed-width centered (matches
        # the compact bar's progress styling exactly).
        progress_row = QHBoxLayout()
        progress_row.setContentsMargins(0, 0, 0, 0)
        progress_row.setSpacing(0)
        self.progress = ScrubbableSlider(Qt.Orientation.Horizontal)
        self.progress.setFixedHeight(2)
        self.progress.setRange(0, 1000)
        self.progress.setStyleSheet(_panel_progress_qss())
        self.progress.sliderMoved.connect(self._on_seek)
        progress_row.addStretch(1)
        progress_row.addWidget(self.progress, 6)
        progress_row.addStretch(1)
        right.addLayout(progress_row)
        right.addStretch(1)

        # Transport — prev / play / next, matching compact.
        controls_row = QHBoxLayout()
        controls_row.setSpacing(2)
        self.prev_btn = _icon_button("prev", 30)
        self.play_btn = _icon_button("play", 36, icon_size=18)
        self.next_btn = _icon_button("next", 30)
        self.prev_btn.clicked.connect(lambda: self.bus.prev_track.emit())
        self.play_btn.clicked.connect(lambda: self.bus.pause_toggled.emit())
        self.next_btn.clicked.connect(lambda: self.bus.next_track.emit())
        controls_row.addStretch()
        controls_row.addWidget(self.prev_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        controls_row.addWidget(self.play_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        controls_row.addWidget(self.next_btn, 0, Qt.AlignmentFlag.AlignVCenter)
        controls_row.addStretch()
        right.addLayout(controls_row)

        layout.addWidget(bar)

        # Seed the cover with the placeholder so a cold-start mini
        # player has a complete-looking cover slot before any track.
        self.refresh_cover()

    def _refresh_subtitle(self):
        a = (self._artist_text or "").strip()
        b = (self._album_text or "").strip()
        if a and b:
            self.subtitle.setText(f"{a}  ·  {b}")
        else:
            self.subtitle.setText(a or b)

    def _on_seek(self, value: int):
        np = get_now_playing()
        if np.duration > 0:
            self.bus.seek_requested.emit(int(value / 1000 * np.duration))

    def set_cover_pixmap(self, pix: QPixmap):
        self._cover_orig = pix
        self.refresh_cover()

    def refresh_cover(self):
        s = self.cover.size()
        if s.width() <= 0 or s.height() <= 0:
            return
        dpr = screen_dpr(self)
        if self._cover_orig is None or self._cover_orig.isNull():
            self.cover.setPixmap(
                _render_cover_placeholder(
                    min(s.width(), s.height()),
                    dpr,
                    BODY_RADIUS,
                )
            )
            return
        phys_w = max(s.width(), int(round(s.width() * dpr)))
        phys_h = max(s.height(), int(round(s.height() * dpr)))
        scaled = self._cover_orig.scaled(
            phys_w,
            phys_h,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        if scaled.width() != phys_w or scaled.height() != phys_h:
            x = (scaled.width() - phys_w) // 2
            y = (scaled.height() - phys_h) // 2
            scaled = scaled.copy(x, y, phys_w, phys_h)
        scaled = _round_all_corners(scaled, int(round(BODY_RADIUS * dpr)))
        scaled.setDevicePixelRatio(dpr)
        self.cover.setPixmap(scaled)

    def _toggle_favorite(self):
        np = get_now_playing()
        if not np.item_id:
            return
        new_state = not np.is_favorite
        run_async(get_provider().toggle_favorite, np.item_id, new_state)
        np.is_favorite = new_state
        self.bus.favorite_toggled.emit(np.item_id, new_state)

    @Slot(str, bool)
    def _on_favorite_toggled(self, item_id: str, fav: bool):
        np = get_now_playing()
        if np.item_id != item_id:
            return
        self.fav_btn.setIcon(accent_icon("favorite_filled") if fav else icon("favorite_outline"))


# ── The mini player itself ──────────────────────────────────────────────────


class FloatingMiniPlayer(QWidget):
    # Expanded geometry: H = W + EXPANDED_BOTTOM_DELTA, where the delta
    # is exactly the bar height — the cover takes the top W×W, the bar
    # takes W×_BAR_HEIGHT below it. Same bar height as compact's full
    # window height, so toggling only changes where the cover lives.
    EXPANDED_BOTTOM_DELTA = _BAR_HEIGHT
    # Min width is set so the popup buttons (anchored bottom-right of the
    # bar) clear the rightmost transport button at the narrowest size.
    # Math at min: bar inner = W − 24 (padding); transport = 100; right
    # stretch = (inner − 100) / 2 must exceed popup width + edge margin.
    EXPANDED_MIN_WIDTH = 300
    # Hard upper bound — beyond this the player is no longer
    # "mini" and a drag overshoot can land it outside the screen
    # before the user can react. 600 cover + bar fits any modern
    # display while still being usefully large for now-playing.
    EXPANDED_MAX_WIDTH = 600
    COMPACT_SIZE = (384, _BAR_HEIGHT)
    RESIZE_HIT = 20  # px square in bottom-left corner that triggers resize

    def __init__(self, parent=None):
        super().__init__(parent)
        self.bus = PlayerBus.get()
        # Provider ref is cached at construction so cover URLs can be
        # built in _on_started / _prefetch. The host pushes a fresh ref
        # via refresh_provider() on sign-out / provider-kind switch
        # (see feedback_provider_singleton_refs).
        self.api = get_provider()
        self._mode = "compact"  # "compact" or "expanded"
        # Recursion guard for the aspect-ratio enforcement in
        # resizeEvent — calling self.resize() inside resizeEvent
        # re-enters resizeEvent, which would loop without this.
        self._aspect_adjust = False
        # Last expanded body width — always seeded to EXPANDED_MIN_WIDTH
        # on construction. The album-art view should always launch at
        # its smallest size; the user can drag-resize during a
        # session and the new width takes effect for any compact→
        # expanded toggle in that session, but a fresh app start
        # resets to the smallest size. The persisted setting from
        # earlier iterations stays around for compat but is ignored
        # here.
        self._last_expanded_width = self.EXPANDED_MIN_WIDTH
        self.setMouseTracking(True)

        # Distinct window title so the keep-above backend's KWin rule
        # can scope-match the mini player without catching the main
        # window. Frameless so the user never sees it. Keep in sync
        # with jellytoast.keep_above.MINI_PLAYER_WINDOW_TITLE.
        from jellytoast.keep_above import MINI_PLAYER_WINDOW_TITLE

        self.setWindowTitle(MINI_PLAYER_WINDOW_TITLE)

        # Top-level window, always on top. Pager/taskbar-skip strategy
        # is platform-split:
        #  - X11: set _NET_WM_STATE_SKIP_TASKBAR/PAGER via xprop in
        #    showEvent (skip_taskbar_x11). Plain Qt.Tool here on X11 +
        #    KDE leaves a ghost strip in some themes.
        #  - Wayland: no xprop equivalent. Qt.Tool is the standard way
        #    to ask the compositor to keep this surface out of the
        #    taskbar; KWin Wayland honors it cleanly.
        # Note: WindowStaysOnTopHint is a no-op on Wayland — xdg-shell
        # forbids apps from controlling z-order. The "Keep mini player
        # on top (Wayland)" setting goes through jellytoast.keep_above,
        # which installs a KWin rule on KDE Wayland and is a no-op
        # everywhere else (where the Qt flag already works).
        #
        # Decoration: frameless everywhere EXCEPT KDE Wayland, where the
        # window is server-side-decorated and a KWin `noborder` rule
        # (jellytoast.keep_above.install_noborder_rules) strips the visible
        # chrome so it still looks frameless. Reason: KWin's blur effect
        # drops blur for *undecorated* windows while they move, so a
        # frameless mini player flickers on every drag — a decorated +
        # noborder window keeps its blur. Verified on KWin 6.6.
        from jellytoast.platform_compat import is_kde_wayland, is_wayland

        flags = Qt.WindowType.WindowStaysOnTopHint
        if not is_kde_wayland():
            flags |= Qt.WindowType.FramelessWindowHint
        if is_wayland():
            flags |= Qt.WindowType.Tool
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)

        # Container is a transparent layout host. The body shape is painted in
        # this widget's paintEvent so we can honor an alpha channel — Qt's
        # QSS alpha doesn't reliably reach a child QFrame.
        self.container = QFrame(self)
        self.container.setObjectName("miniContainer")
        self.container.setStyleSheet("""
            QFrame#miniContainer { background: transparent; border: none; }
            QFrame#miniContainer > QWidget,
            QFrame#miniContainer QStackedWidget,
            QFrame#miniContainer QStackedWidget > QWidget {
                background: transparent;
            }
            /* Labels otherwise inherit the app-global QWidget {background: BG}
               rule — an opaque near-black box behind every line of text.
               Invisible on the solid/frosted bodies, but the Transparent
               theme exposed it. Descendant selector so it reaches the
               title / subtitle labels in both panels. */
            QFrame#miniContainer QLabel { background: transparent; }
        """)

        # No drop shadow — the body fills the entire window. Translucent body
        # alone reads better over both dark and bright wallpapers.
        self.outer_layout = QVBoxLayout(self)
        self.outer_layout.setContentsMargins(0, 0, 0, 0)
        self.outer_layout.addWidget(self.container)

        self.inner_layout = QVBoxLayout(self.container)
        self.inner_layout.setContentsMargins(0, 0, 0, 0)
        self.inner_layout.setSpacing(0)

        # Stacked: compact / expanded. Parent each child to its
        # eventual container *at construction time* — on Wayland a
        # parentless QWidget gets a top-level surface allocated for
        # an instant before addWidget reparents it, which surfaces
        # as a small rectangle flashing in the middle of the screen
        # during boot.
        self.stack = QStackedWidget(self.container)
        self.compact = _CompactBar(self.stack)
        self.expanded = _ExpandedPanel(self.stack)
        self.stack.addWidget(self.compact)
        self.stack.addWidget(self.expanded)
        self.inner_layout.addWidget(self.stack)

        # Floating window-controls overlay — anchored to the bottom-right of
        # the body and only visible while the cursor is over the mini player.
        self.window_controls = QFrame(self.container)
        self.window_controls.setObjectName("winControls")
        self.window_controls.setStyleSheet("""
            QFrame#winControls { background: transparent; }
        """)
        wc_layout = QHBoxLayout(self.window_controls)
        wc_layout.setContentsMargins(0, 0, 0, 0)
        wc_layout.setSpacing(2)

        # SVG icons, not unicode glyphs — the old "▢" / "⛶" chars
        # depend on a font that happens to carry those codepoints, and
        # on many systems they render blank. _icon_button uses the
        # shared SVG registry (same path as the transport buttons).
        # Icon previews the target shape — _apply_mode_size keeps it in
        # sync; "view_tall" is the compact-mode default (click to grow).
        self.toggle_btn = _icon_button("view_tall", 20, icon_size=14)
        self.toggle_btn.setToolTip("Toggle compact / expanded")
        self.toggle_btn.clicked.connect(self.toggle_mode)

        self.open_btn = _icon_button("open_window", 20, icon_size=14)
        self.open_btn.setToolTip("Open main window")
        self.open_btn.clicked.connect(lambda: self.bus.open_main_window.emit())

        # Volume — small variant of the now-playing bar's button. The popup is
        # the integrated right-edge slot (``_BAR_HEIGHT`` tall, flush with the
        # player's right edge) — now drawn as a top-level frosted-glass ToolTip
        # window so it rides real KWin blur like everywhere else (the slot look,
        # but real frosted glass instead of the old child-panel software blur).
        self.volume_btn = VolumeButton(
            self.bus,
            parent=self.window_controls,
            size=20,
            popup_height=_BAR_HEIGHT,
            popup_align="right",
        )
        self.volume_btn.setIconSize(QSize(14, 14))

        wc_layout.addWidget(self.toggle_btn)
        wc_layout.addWidget(self.open_btn)
        wc_layout.addWidget(self.volume_btn)
        self.window_controls.adjustSize()
        self.window_controls.hide()

        # Top-right close overlay — same hover lifecycle as the
        # bottom-right control row. Single-button frame so the
        # positioning math stays trivial.
        self.close_overlay = QFrame(self.container)
        self.close_overlay.setObjectName("winClose")
        self.close_overlay.setStyleSheet("""
            QFrame#winClose { background: transparent; }
        """)
        co_layout = QHBoxLayout(self.close_overlay)
        co_layout.setContentsMargins(0, 0, 0, 0)
        co_layout.setSpacing(0)
        self.close_btn = IconButton()
        self.close_btn.setIcon(icon("win_close"))
        self.close_btn.setIconSize(QSize(12, 12))
        self.close_btn.setFixedSize(_CLOSE_BTN_SIZE, _CLOSE_BTN_SIZE)
        # Tagged so _reapply_theme re-issues the glyph in the new tint.
        self.close_btn.setProperty("_jt_icon", "win_close")
        self.close_btn.setStyleSheet(_close_btn_qss())
        self.close_btn.clicked.connect(self.hide)
        co_layout.addWidget(self.close_btn)
        self.close_overlay.adjustSize()
        self.close_overlay.hide()

        # Restore saved mode before applying size — compact's
        # setFixedSize would otherwise pin the window before
        # restoreGeometry could enlarge it back to the user's
        # last-used expanded size.
        saved_mode = get_settings().mini_player_mode
        if saved_mode == "expanded":
            self._mode = "expanded"
        self._apply_mode_size()
        self._connect_signals()

        # Debounced save — moveEvent / resizeEvent fire constantly during
        # drags. 400ms after the last event we flush the geometry blob to
        # QSettings.
        self._save_geom_timer = QTimer(self)
        self._save_geom_timer.setSingleShot(True)
        self._save_geom_timer.setInterval(400)
        self._save_geom_timer.timeout.connect(self._save_geometry_now)

        # Restore saved geometry. On Wayland, KWin ignores the position
        # part (xdg-shell forbids client-set absolute positions) but the
        # size still restores. Falls back to bottom-right of the primary
        # screen on first launch / corrupt blob.
        saved_geom = get_settings().mini_player_geometry
        restored = False
        if saved_geom:
            restored = self.restoreGeometry(saved_geom)
        if not restored:
            screen = QApplication.primaryScreen().availableGeometry()
            self.move(
                screen.right() - self.width() - 24,
                screen.bottom() - self.height() - 24,
            )
        elif self._mode == "expanded":
            # restoreGeometry can carry over a stale-larger size from
            # an older session whose blob predates the dedicated
            # mini_player_expanded_width setting. Snap the size back
            # to the authoritative persisted width so the album-art
            # view always opens at exactly what the user last had
            # it sized to — keeps the restored position untouched.
            target_w = self._last_expanded_width
            target_h = target_w + self.EXPANDED_BOTTOM_DELTA
            if (self.width(), self.height()) != (target_w, target_h):
                self.resize(target_w, target_h)

    def set_cast_manager(self, cm):
        """Forward the CastManager to the volume button so its popup can
        switch to the per-speaker variant when casting to a group."""
        self.volume_btn.set_cast_manager(cm)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)

        # Hard-clear the alpha buffer first. WA_TranslucentBackground sets
        # WA_NoSystemBackground, so Qt won't auto-fill — without this clear,
        # stale frames or compositor artifacts can ghost above the body.
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        p.fillRect(self.rect(), Qt.GlobalColor.transparent)
        p.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)

        rect = self.container.geometry()

        # Body — translucent rounded rect; alpha lets the wallpaper show through.
        body_path = QPainterPath()
        body_path.addRoundedRect(
            float(rect.x()),
            float(rect.y()),
            float(rect.width()),
            float(rect.height()),
            BODY_RADIUS,
            BODY_RADIUS,
        )
        # Read the body fill live from ui_helpers.body_color_tuple — it
        # resolves the active theme AND the verified blur status, so a
        # theme-mode switch (body opacity) AND a no-blur box (glass →
        # near-opaque fallback, never see-through) both land here without a
        # cache to invalidate. Mirrors the main window's body resolution.
        from jellytoast import ui_helpers as _uih

        p.setBrush(QColor(*_uih.body_color_tuple("mini")))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawPath(body_path)

        # Resize hit area is invisible — bottom-left corner of the body
        # accepts a drag to resize, no glyph needed.

    def showEvent(self, event):
        super().showEvent(event)
        # KWin needs a real X11 winId before it honors EWMH state atoms.
        QTimer.singleShot(0, lambda: skip_taskbar_x11(self))
        # Repaint stored covers — when a load lands while the mini is
        # hidden, ``set_cover_pixmap`` stores _cover_orig but
        # ``refresh_cover`` bails because thumb.size() is 0×0. When the
        # user opens the mini the resizeEvent only refreshes the active
        # stacked panel; this hook covers both so a recently-stored
        # radio cover paints reliably on first show.
        QTimer.singleShot(0, lambda: (self.compact.refresh_cover(), self.expanded.refresh_cover()))
        # Compositor blur behind the body, once the surface is mapped.
        QTimer.singleShot(0, self._apply_blur)

    def _apply_blur(self):
        """Blur behind the mini player when the active theme is frosted.
        Region shaped to the BODY_RADIUS rounded rect. On KDE Wayland
        the window is server-side-decorated (see the flag block in
        __init__) so KWin keeps the blur alive while it's dragged —
        frameless windows lose it mid-move. Silent no-op where the
        compositor has no blur support."""
        from jellytoast import blur
        from jellytoast.theme import get_active_theme

        blur.apply(self, get_active_theme().blur, corner_radius=BODY_RADIUS)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._mode == "expanded":
            # Re-enforce the aspect ratio after the compositor resizes
            # us. With startSystemResize the WM controls geometry and
            # gives us whatever width/height the user dragged to;
            # without this the cover image would either crop or float
            # in empty space when the height drifted from W + DELTA.
            expected_h = self.width() + self.EXPANDED_BOTTOM_DELTA
            if self.height() != expected_h and not self._aspect_adjust:
                self._aspect_adjust = True
                try:
                    self.resize(self.width(), expected_h)
                finally:
                    self._aspect_adjust = False
            body_w = max(1, self.width())
            self.expanded.cover.setFixedSize(body_w, body_w)
            self.expanded.refresh_cover()
            # Remember the last user-set width so we restore it on the
            # next compact→expanded toggle.
            self._last_expanded_width = body_w
        else:
            # Compact: cover is square at the body's full height (no bezel).
            body_h = max(1, self.height())
            self.compact.thumb.setFixedSize(body_h, body_h)
            self.compact.refresh_cover()
        self._position_window_controls()
        # Keep the rounded blur region sized to the body as it resizes.
        self._apply_blur()
        # Debounce — drag-resize generates a flurry of events; we only
        # need the final size.
        if hasattr(self, "_save_geom_timer") and not self._aspect_adjust:
            self._save_geom_timer.start()

    def moveEvent(self, event):
        super().moveEvent(event)
        if hasattr(self, "_save_geom_timer"):
            self._save_geom_timer.start()

    def hideEvent(self, event):
        # Catches both the close-button → hide() path and the implicit
        # hide during app quit, so we always have the latest geometry
        # persisted before the window goes away.
        self._save_geometry_now()
        super().hideEvent(event)

    def enterEvent(self, event):
        super().enterEvent(event)
        self.window_controls.show()
        self.window_controls.raise_()
        self.close_overlay.show()
        self.close_overlay.raise_()
        self._position_window_controls()

    def leaveEvent(self, event):
        super().leaveEvent(event)
        self.window_controls.hide()
        self.close_overlay.hide()
        # The right-edge volume popup fills most of the player's right
        # slice; it relies on its own leaveEvent to start the hide-grace
        # timer, but Wayland sometimes doesn't deliver the popup-side
        # leave when the cursor exits the top-level surface (children
        # of a translucent top-level can have flaky enter/leave ordering
        # at the window boundary). Force-start the hide timer here too
        # so the popup always dismisses when the cursor leaves the mini
        # player entirely.
        if hasattr(self, "volume_btn"):
            timer = getattr(self.volume_btn, "_hide_timer", None)
            if timer is not None:
                timer.start()

    def _is_resize_corner(self, pos: QPoint) -> bool:
        if self._mode != "expanded":
            return False
        return pos.x() <= self.RESIZE_HIT and pos.y() >= self.height() - self.RESIZE_HIT

    def _position_window_controls(self):
        # Bottom-right of the bar in both modes. Since toggle_mode pins
        # the window's bottom-right to a fixed screen pivot, anchoring
        # the popup here means it stays at the *exact same screen point*
        # across the compact/expanded swap — only the cover transforms
        # around it. EXPANDED_MIN_WIDTH guarantees there's enough right
        # stretch beside the centered 3-button transport so the popup
        # never collides with the next button.
        self.window_controls.adjustSize()
        cw = self.window_controls.width()
        ch = self.window_controls.height()
        x = self.container.width() - cw - 10
        # Vertically centre the cluster on the transport row's play
        # button so the two read as one line. A fixed bottom inset left
        # it sitting a few px low against the bar-centred transport
        # buttons. Falls back to the inset if the page isn't laid out
        # yet (an early pre-show call) — a later call re-aligns it.
        page = self.compact if self._mode == "compact" else self.expanded
        play = page.play_btn
        if play.height() > 0:
            cy = play.mapTo(self.container, play.rect().center()).y()
            y = cy - ch // 2
        else:
            y = self.container.height() - ch - 10
        self.window_controls.move(x, y)

        # Close button: top-right corner, horizontally centred on the
        # volume button in the bottom cluster so the X and the speaker
        # icon read as one vertical column.
        self.close_overlay.adjustSize()
        vol_center_x = self.volume_btn.mapTo(
            self.container, self.volume_btn.rect().center()
        ).x()
        clx = vol_center_x - self.close_overlay.width() // 2
        cly = 6  # a pinch higher — it sat a touch low at the bar's top inset
        self.close_overlay.move(clx, cly)

    # ── Mode switching ──────────────────────────────────────────────────────

    def toggle_mode(self):
        """Swap between compact and expanded. Default anchor is
        bottom-right so the bar stays put and the window grows UP
        out of compact; if the player is docked near the top of the
        desk and growing up would push it off-screen, the anchor
        flips to top-right and the window grows DOWN instead."""
        # KDE Wayland silently ignores ANY runtime position change
        # to a mapped xdg-toplevel (move, setGeometry, etc.) and the
        # "remap via hide/show/destroy" workarounds trigger KWin's
        # default placement policy, which centers the window — so
        # we can't actually move the window on toggle. The best we
        # can do is keep the top-left fixed and let the window
        # grow down (or shrink up). That looks correct when docked
        # at the top edge and broken when docked at the bottom —
        # a known constraint of xdg-shell, not something we can
        # fix from the client side.
        pivot = self.geometry().bottomRight()
        if self._mode == "expanded":
            self._last_expanded_width = self.width()
        self._mode = "expanded" if self._mode == "compact" else "compact"
        self._apply_mode_size()
        new_geom = self.geometry()
        new_geom.moveBottomRight(pivot)
        self.move(new_geom.topLeft())
        self._position_window_controls()
        self._save_geometry_now()
        # A click that resizes the window can leave the toggle button
        # stuck in its :hover / :pressed paint — the reflow desyncs Qt's
        # under-mouse tracking and no Leave is delivered. Clear both so
        # it repaints idle; the next real mouse-move re-asserts hover.
        self.toggle_btn.setDown(False)
        self.toggle_btn.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, False)
        self.toggle_btn.update()

    def _save_geometry_now(self):
        """Persist the current size/position + mode to QSettings so the
        next launch reopens the mini player where the user left it.
        Called from move/resize (debounced), toggle_mode, and hideEvent
        — saveGeometry is cheap and works regardless of visibility."""
        try:
            s = get_settings()
            s.mini_player_geometry = bytes(self.saveGeometry())
            s.mini_player_mode = self._mode
            # Track expanded width separately — saveGeometry() above
            # only captures the size of whichever mode is currently
            # active, so a session that ends in compact would
            # otherwise forget the expanded width.
            s.mini_player_expanded_width = self._last_expanded_width
        except Exception:
            pass

    def _apply_mode_size(self):
        if self._mode == "compact":
            # Fixed size — bar fills the entire window.
            self.setMinimumSize(0, 0)
            self.setMaximumSize(QWIDGETSIZE_MAX, QWIDGETSIZE_MAX)
            self.setFixedSize(*self.COMPACT_SIZE)
            self.stack.setCurrentIndex(0)
            # Compact now → the toggle grows to the tall album view.
            self.toggle_btn.setIcon(icon("view_tall"))
            # Track the current glyph so _reapply_theme's restamp loop
            # re-issues the RIGHT mode icon on a live theme switch
            # (without this the property stays stale and the glyph flips).
            self.toggle_btn.setProperty("_jt_icon", "view_tall")
            # Compact: plain close button — it sits over the flat bar,
            # not album art, so no circular disc.
            self.close_btn.setStyleSheet(_icon_btn_qss())
        else:
            # Aspect locked: H = W + bar height. Capped by
            # EXPANDED_MAX_WIDTH so a drag overshoot can't push the
            # window past screen bounds (the WM honors maximumSize).
            self.setMinimumSize(
                self.EXPANDED_MIN_WIDTH,
                self.EXPANDED_MIN_WIDTH + self.EXPANDED_BOTTOM_DELTA,
            )
            self.setMaximumSize(
                self.EXPANDED_MAX_WIDTH,
                self.EXPANDED_MAX_WIDTH + self.EXPANDED_BOTTOM_DELTA,
            )
            w = max(
                self.EXPANDED_MIN_WIDTH, min(self._last_expanded_width, self.EXPANDED_MAX_WIDTH)
            )
            self.resize(w, w + self.EXPANDED_BOTTOM_DELTA)
            self.stack.setCurrentIndex(1)
            # Expanded now → the toggle collapses to the flat bar.
            self.toggle_btn.setIcon(icon("view_flat"))
            self.toggle_btn.setProperty("_jt_icon", "view_flat")
            # Expanded: circular disc — the X floats over the cover.
            self.close_btn.setStyleSheet(_close_btn_qss())
        self._position_window_controls()

    # ── Bus signals ─────────────────────────────────────────────────────────

    def _connect_signals(self):
        self.bus.playback_started.connect(self._on_started)
        self.bus.playback_stopped.connect(self._on_stopped)
        # Settings → "Refresh album art" — re-fetch the current track's
        # cover against the now-cleared caches.
        self.bus.image_cache_cleared.connect(
            self._on_image_cache_cleared,
        )
        self.bus.playback_paused.connect(self._on_paused)
        self.bus.playback_resumed.connect(self._on_resumed)
        # Launch-time resume: render the restored track (paused) instead of
        # leaving the mini on "Nothing Playing" until the user presses play.
        self.bus.playback_restored.connect(self._on_restored)
        self.bus.position_updated.connect(self._on_position)
        self.bus.duration_set.connect(self._on_duration)
        # Live-accent: refresh the heart icons + play button (when
        # idle) on Settings → Display accent change so the QIcon
        # objects we cached at construction don't keep the old
        # colour. Once playback has started the play button is
        # already on the regular `icon("pause")` / `icon("play")`
        # which doesn't carry accent, so we only restamp it from
        # the idle state.
        self.bus.theme_changed.connect(self._reapply_theme)
        # Cross-DPR cover refresh — re-issue the cover load at the new
        # physical target when the user moves the parent window to a
        # different-scale monitor. The mini player's own window can be
        # on a different monitor than the main window; we still listen
        # because the bus signal fires when ANY top-level changes DPR.
        self.bus.dpr_changed.connect(self._on_dpr_changed)
        # Cover-art prefetch for the next-up track — warms the cache
        # slot so a track advance is a memory-cache hit, not a network
        # fetch. Belongs HERE in _connect_signals, not _reapply_theme
        # where each theme change would stack another duplicate
        # connection (see [[signal-connects-belong-in-init-not-reapply-accent]]).
        self.bus.queue_prefetch_request.connect(self._prefetch_cover)
        # Unified radio rendering — one handler driven by the
        # ``jellytoast.radio_state`` snapshots. See radio_state.py for the
        # parse + cover-lookup pipeline.
        self.bus.radio_state_changed.connect(self._on_radio_state)
        self._is_radio: bool = False
        self._radio_station_name: str = ""
        # Seed from the current snapshot so a mini constructed
        # mid-session picks up the in-flight radio state immediately.
        from jellytoast import radio_state as _radio_state

        seed = _radio_state.current()
        if seed is not None:
            self._on_radio_state(seed)

    @Slot(object)
    def _on_radio_state(self, state):
        """Unified radio renderer — applies a ``radio_state.RadioState``
        snapshot (or clears radio mode when ``state is None``). Shares
        the render contract with the NP bar + page so all three
        surfaces stay in lockstep."""
        if state is None:
            if self._is_radio:
                self._is_radio = False
                self._radio_station_name = ""
                # Returning to album/playlist — restore the subtitle
                # styling so the next track's "artist · album" reads
                # as normal metadata, not a radio badge.
                for panel in (self.compact, self.expanded):
                    self._reset_panel_subtitle_style(panel)
            return

        self._is_radio = True
        self._radio_station_name = state.station_name

        display_title = state.display_title
        display_artist = state.display_subtitle
        for panel in (self.compact, self.expanded):
            panel.title.setText(display_title)
            panel.title.setStyleSheet(
                f"color: {TEXT}; {type_qss(TYPE_CAPTION)} font-weight: 500;"
            )
            panel.artist.setText(display_artist)
            self._stamp_radio_album(panel, state)

        cover_url = state.display_cover_url
        if cover_url:
            self._load_radio_cover(cover_url)

    def _stamp_radio_album(self, panel, state) -> None:
        """Set the panel's subtitle line to a radio status badge.
        Three flavours, gated on ``state.playback_state``:

          * playing → accent "● LIVE · {station}"
          * paused  → dim   "PAUSED · {station}"
          * stopped → dim   "{station}" (radio queued but inactive)

        Both panels (compact + expanded) use a ``_SubField`` proxy
        that feeds a single ``panel.subtitle`` QLabel; we write the
        badge text via the proxy and tint the underlying QLabel
        directly since proxies don't carry styling."""
        from jellytoast import ui_helpers as _uih

        station = (state.station_name or "").strip()
        if state.is_live:
            badge = f"● LIVE · {station}" if station else "● LIVE"
            tint = _uih.ACCENT
        elif state.playback_state == "paused":
            badge = f"PAUSED · {station}" if station else "PAUSED"
            tint = _uih.TEXT_DIM
        else:
            badge = station
            tint = _uih.TEXT_DIM
        panel.album.setText(badge)
        panel.subtitle.setStyleSheet(
            f"color: {tint}; {type_qss(TYPE_TINY)} font-weight: 700;"
            " letter-spacing: 1px;"
        )

    def _reset_panel_subtitle_style(self, panel) -> None:
        """Restore the default TEXT_DIM subtitle style on a panel. Used
        when leaving radio mode so the next album's subtitle reads as
        normal track metadata, not as a status badge."""
        from jellytoast import ui_helpers as _uih

        panel.subtitle.setStyleSheet(
            f"color: {_uih.TEXT_DIM}; {type_qss(TYPE_TINY)}"
        )

    def _load_radio_cover(self, url: str) -> None:
        """Load ``url`` into both compact + expanded panels. Same DPR-
        aware target as the album-art path so the mini player reads
        crisp on Retina."""
        if not url:
            return
        target_px = max(800, int(round(320 * screen_dpr(self))))
        load_image_async(
            f"radio:{url}",
            url,
            target_px,
            target_px,
            self._set_cover_both_panels,
            rounded_radius=0,
            on_error=lambda: None,
            priority="high",
        )

    def _on_dpr_changed(self):
        np = get_now_playing()
        if np.item_id:
            self._on_started(np)

    def _on_image_cache_cleared(self):
        np = get_now_playing()
        if np.item_id or np.image_id:
            self._on_started(np)

    def _reapply_theme(self):
        """Full theme re-stamp on theme_changed — every text colour,
        icon tint and control QSS, so a live light↔dark switch lands
        uniformly. Accent-only changes route here too; the extra work
        is cheap and keeps one handler."""
        np = get_now_playing()
        playing = bool(np.item_id)

        # 1. Panel text colours, progress + transport-button QSS.
        for panel in (self.compact, self.expanded):
            _apply_panel_theme(panel, playing)

        # 1b. Radio "● LIVE · station" badge — _apply_panel_theme just
        #     forced both subtitles to plain TEXT_DIM, which clobbers the
        #     accent badge. Re-stamp it from the current radio state so a
        #     theme flip while streaming keeps the LIVE accent identity
        #     (mirrors the _on_started radio guard).
        if self._is_radio:
            from jellytoast import radio_state as _radio_state

            cur = _radio_state.current()
            if cur is not None:
                for panel in (self.compact, self.expanded):
                    self._stamp_radio_album(panel, cur)

        # 2. Re-issue stable icon-button glyphs in the fresh tint —
        #    everything tagged by `_icon_button` except play / fav,
        #    which are state-driven and re-stamped in step 3.
        state_btns = {
            self.compact.play_btn, self.expanded.play_btn,
            self.compact.fav_btn, self.expanded.fav_btn,
        }
        for btn in self.findChildren(QPushButton):
            name = btn.property("_jt_icon")
            if not name or btn in state_btns:
                continue
            accent = bool(btn.property("_jt_icon_accent"))
            btn.setIcon(accent_icon(name) if accent else icon(name))

        # 3. Play + favorite — state-driven glyphs, re-issued in tint.
        #    Play shows accent only in the never-played-yet state.
        for panel in (self.compact, self.expanded):
            if not playing:
                panel.play_btn.setIcon(accent_icon("play"))
            else:
                panel.play_btn.setIcon(icon("play" if np.is_paused else "pause"))
            panel.fav_btn.setIcon(
                accent_icon("favorite_filled") if np.is_favorite
                else icon("favorite_outline")
            )

        # 4. Close button — rebuild its QSS for the current mode
        #    (expanded = theme-aware disc; compact = plain pill).
        self.close_btn.setStyleSheet(
            _close_btn_qss() if self._mode == "expanded" else _icon_btn_qss()
        )

        # 5. Repaint the body (MINI_BODY_COLOR opacity differs per
        #    theme — read live at paint time) and re-blur for frosted.
        self.update()
        self._apply_blur()

    @Slot(object)
    def _prefetch_cover(self, np):
        if np is None:
            return
        image_id = getattr(np, "image_id", "") or getattr(np, "item_id", "")
        if not image_id:
            return
        target_px = max(800, int(round(320 * screen_dpr(self))))
        url = self.api.get_image_url(image_id, "Primary", _MINI_SOURCE_PX)
        if not url:
            return
        load_image_async(
            f"{image_id}|mini",
            url,
            target_px,
            target_px,
            lambda _pix: None,
            rounded_radius=0,
            on_error=lambda: None,
        )

    @Slot(object)
    def _on_started(self, np: NowPlaying):
        for panel in (self.compact, self.expanded):
            # Re-stamp title style to TEXT so the active track reads
            # at full brightness (idle was dimmed to TEXT_DIM). Live
            # token read so a track starting after a theme flip lands
            # in the current palette.
            from jellytoast import ui_helpers as _uih

            panel.title.setStyleSheet(
                f"color: {_uih.TEXT}; {type_qss(TYPE_CAPTION)} font-weight: 500;"
            )
            if not self._is_radio:
                # Radio title/artist are owned by _on_radio_state; a replayed
                # _on_started must NOT clobber them with the raw np stream
                # fields (often empty/wrong for a live stream).
                panel.title.setText(np.title)
                panel.artist.setText(np.subtitle or np.year)
            if self._is_radio:
                # Album slot is owned by the LIVE · station badge while
                # streaming — don't let np.album="" wipe it. We re-pull
                # the current radio state instead of caching one,
                # so the badge respects whatever the LATEST playback
                # state is (in case _on_started fires after a pause).
                from jellytoast import radio_state as _radio_state

                cur = _radio_state.current()
                if cur is not None:
                    self._stamp_radio_album(panel, cur)
            else:
                # ``panel.album`` is a ``_SubField`` proxy — only
                # setText is supported; the underlying QLabel is
                # ``panel.subtitle`` and that's where the per-mode
                # styling lives.
                self._reset_panel_subtitle_style(panel)
                panel.album.setText(np.album)
            # State-aware: _on_started can be replayed for the same track on
            # a cache-clear / dpr-change while paused, so an unconditional
            # "pause" glyph would lie about the state (mirrors _reapply_theme).
            panel.play_btn.setIcon(icon("play" if np.is_paused else "pause"))
            # Seed the heart icon state for the new track. The
            # favorite_toggled bus signal handles subsequent flips.
            panel.fav_btn.setIcon(
                accent_icon("favorite_filled") if np.is_favorite else icon("favorite_outline")
            )

        image_id = np.image_id or np.item_id
        if image_id and not self._is_radio:
            # Build our own URL at the mini's target size — see the
            # bar's _on_started for why we don't reuse np.thumb_url.
            # Radio mode skips this entirely; _on_queue_context_changed
            # owns the cover (station logo + per-track MusicBrainz
            # art) and the synthetic station id has no provider image.
            # 800 covers the expanded panel's max width (~640 physical
            # at 2× DPR on a 320-logical panel); on 3+× the dpr
            # multiplier takes over so a 4K Retina user still gets a
            # crisp source. Compact mode (96 logical) downscales from
            # this without upscaling artifacts.
            target_px = max(800, int(round(320 * screen_dpr(self))))
            url = self.api.get_image_url(image_id, "Primary", _MINI_SOURCE_PX)
            load_image_async(
                f"{image_id}|mini",
                url,
                target_px,
                target_px,
                self._set_cover_both_panels,
                rounded_radius=0,
                on_error=lambda: None,
                priority="high",
            )

    @Slot(object)
    def _on_restored(self, np: NowPlaying):
        """Render the launch-time resume state: track + saved position +
        duration, paused. Same UI as _on_started but the play icon stays
        'play' (mpv hasn't loaded yet) — mirrors now_playing_bar._on_restored.
        Without this the mini shows 'Nothing Playing' for a resumed session
        until the user presses play."""
        self._on_started(np)
        for panel in (self.compact, self.expanded):
            panel.play_btn.setIcon(icon("play"))
        self._on_duration(np.duration)
        self._on_position(np.position)

    def _set_cover_both_panels(self, pix: QPixmap):
        self.compact.set_cover_pixmap(pix)
        self.expanded.set_cover_pixmap(pix)

    @Slot()
    def _on_stopped(self):
        for panel in (self.compact, self.expanded):
            panel.title.setText("Nothing Playing")
            # Idle state — match inactive icon color (#a8a8a8) so
            # the title pairs with the transport buttons next to it.
            panel.title.setStyleSheet(f"color: {IDLE_TEXT}; {type_qss(TYPE_CAPTION)} font-weight: 500;")
            panel.artist.setText("")
            panel.album.setText("")
            panel.play_btn.setIcon(icon("play"))
            panel.progress.setValue(0)
            # Revert the cover to the placeholder so the "nothing
            # playing" state reads consistently across cover + title.
            panel.set_cover_pixmap(QPixmap())

    @Slot()
    def _on_paused(self):
        for p in (self.compact, self.expanded):
            p.play_btn.setIcon(icon("play"))

    @Slot()
    def _on_resumed(self):
        for p in (self.compact, self.expanded):
            p.play_btn.setIcon(icon("pause"))

    @Slot(int)
    def _on_position(self, ms: int):
        np = get_now_playing()
        for panel in (self.compact, self.expanded):
            if np.duration > 0 and not panel.progress.isSliderDown():
                panel.progress.setValue(int(ms / np.duration * 1000))

    @Slot(int)
    def _on_duration(self, ms: int):
        # No time labels in either compact or expanded — progress bar only.
        pass

    # ── Drag / resize support ───────────────────────────────────────────────
    #
    # Both drag and corner-resize are delegated to the window manager via
    # QWindow.startSystemMove() / startSystemResize(). Works identically
    # on X11 and Wayland — and on Wayland it's the only way that works,
    # since QWidget.move() / setGeometry() on top-level windows is a
    # protocol no-op. The aspect ratio (H = W + EXPANDED_BOTTOM_DELTA in
    # expanded mode) is re-enforced in resizeEvent because the compositor
    # resizes freeform.

    def mousePressEvent(self, e):
        if e.button() != Qt.MouseButton.LeftButton:
            return
        handle = self.windowHandle()
        if handle is None:
            return
        if self._is_resize_corner(e.position().toPoint()):
            # Bottom-left grip — top-right corner stays anchored when the
            # left and bottom edges follow the cursor.
            handle.startSystemResize(Qt.Edge.LeftEdge | Qt.Edge.BottomEdge)
        else:
            handle.startSystemMove()
