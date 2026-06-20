"""
Full-window now-playing / queue page.

Two-pane layout:
- Left:  cover art + title + artist · album + lyrics (scrollable, lazy-
         fetched per track with a small LRU).
- Right: track list. Renders the queue's `original_items` for ALBUM /
         PLAYLIST contexts (so the user always sees the source's
         natural order, regardless of shuffle), and the play-order
         items for SHUFFLE / MANUAL / SEARCH / ARTIST / INSTANT_MIX
         contexts. The currently-playing track is highlighted; clicking
         a row jumps to it via `bus.track_jumped`.

The page is swapped in/out of the main window's content stack — it
covers the WebEngine but leaves the top bar and bottom now-playing bar
visible.
"""

from typing import Dict, List, Optional

from PySide6.QtCore import (
    QEasingCurve,
    QPoint,
    QPropertyAnimation,
    QSize,
    Qt,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QColor,
    QPalette,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QGraphicsDropShadowEffect,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from jellytoast import disk_cache
from jellytoast.async_io import run_async
from jellytoast.design_tokens import (
    BTN_PRIMARY,
    SPACE_LG,
    SPACE_MD,
    SPACE_SM,
    TYPE_BODY,
    TYPE_CAPTION,
    TYPE_MICRO,
    TYPE_TITLE,
    button_qss,
    font,
    type_qss,
)
from jellytoast.download_button import _DownloadButton
from jellytoast.icons import accent_icon, icon
from jellytoast.np_left_pane import _LeftPaneMixin
from jellytoast.np_lyrics import _LyricsCache, _LyricsMixin, _ScrollbarFader
from jellytoast.np_track_list import _TrackDelegate, _TracksListView, _TracksModel
from jellytoast.player_state import (
    NowPlaying,
    PlayerBus,
    QueueContext,
    QueueKind,
    get_now_playing,
)
from jellytoast.providers import get_provider
from jellytoast.ui_helpers import (
    ACCENT,
    IDLE_TEXT,
    CoverOverlayButton,
    EmptyState,
    dpr_bucket,
    ink_alpha,
    load_image_async,
    scale_pixmap_for_dpr,
    screen_dpr,
)

# Right-pane behavior per queue context kind. ALBUM/PLAYLIST want
# source order (so the user can see "track 1, 2, 3..."); everything
# else wants the actual play sequence.
_SOURCE_ORDER_KINDS = {QueueKind.ALBUM, QueueKind.PLAYLIST}


def _lyrics_caption_btn_qss() -> str:
    """QSS for the small faint caption buttons above the lyrics scroll
    (the "Hide lyrics" toggle + the "● Live" re-snap). Reads the live
    ``ui_helpers`` ink tokens so a dark↔light flip re-stamps to the
    new family — bare ``TEXT_FAINT``/``TEXT`` imports freeze at module
    load. Shared by construction and ``_reapply_theme``."""
    from jellytoast import ui_helpers as _u

    return f"""
        QPushButton {{
            background: transparent; color: {_u.TEXT_FAINT};
            border: none; padding: 4px 8px;
            {type_qss(TYPE_CAPTION)}
        }}
        QPushButton:hover {{ color: {_u.TEXT}; }}
    """


# ── Track list: model + delegate + view ─────────────────────────────────
#
# Replaces the widget-based _TrackRow / _QueueDropTarget / _DiscDivider
# stack with the same model/view/delegate scaffolding used by every
# other big list in the app (SongsView, LibraryGrid, GenresView,
# HorizontalRail). Drag-to-reorder uses Qt's InternalMove (default
# drop-indicator line) instead of the previous custom grabMouse-based
# animated row-shift; the polished animation is gone but the move
# semantics are identical and the rendering scaffolding now matches
# the rest of the surfaces. Multi-disc album dividers preserved via
# heterogeneous model rows ("track" vs "disc" entries).


class NowPlayingPage(_LeftPaneMixin, _LyricsMixin, QWidget):
    """Full-screen now-playing view. Owned by JellytoastWindow; swapped
    into the content stack when the user clicks the now-playing pill
    in the transport bar."""

    # Emitted when the user wants to dismiss the page (back button).
    dismiss_requested = Signal()
    # Emitted whenever the page enters / leaves preview mode. The host
    # uses this to keep the bottom-transport-bar's left cluster (cover +
    # title + artist + heart) visible while the user browses (so the
    # currently-playing track stays surfaced) and hide it again when
    # the page returns to live mode (the page itself displays the
    # active track in large).
    preview_changed = Signal(bool)  # True = entering preview, False = leaving
    # Internal — fires from the lyrics worker thread; the auto-routed
    # queued connection delivers it on the main thread so we can touch
    # widgets safely. Without this we'd be calling QTimer.singleShot
    # from a thread that has no event loop and the callback would never
    # fire.
    _lyrics_loaded = Signal(str, object)
    # Async preview-fetch results land on the GUI thread via these.
    _preview_meta_loaded = Signal(str, object)  # (preview_id, meta or None)
    _preview_tracks_loaded = Signal(str, object)  # (preview_id, list or None)

    # Panes split 50/50; cover sits at the top of the left pane and the
    # lyrics column owns the visual weight underneath. Apple Music's
    # macOS lyrics view is the reference — the cover anchors, lyrics
    # are the focal point.
    COVER_SIZE = 200  # square art

    def __init__(self, queue_mgr, parent=None):
        super().__init__(parent)
        self.bus = PlayerBus.get()
        self.api = get_provider()
        self.queue_mgr = queue_mgr
        self._lyrics_cache = _LyricsCache()
        self._lyrics_loading_for: str = ""  # in-flight item_id
        self._cover_orig: Optional[QPixmap] = None
        self._displayed_items_kind: str = ""  # "source" | "play"

        # Preview mode — when set, the page browses an album/playlist
        # without taking over the live queue. Click Play (or any track)
        # to install + play, which transitions back to live mode.
        # _preview_kind drives the right fetch endpoint and the
        # QueueKind installed when the user converts preview to live.
        self._preview_id: str = ""
        self._preview_kind: QueueKind = QueueKind.ALBUM
        self._preview_meta: Dict = {}
        self._preview_tracks: List[Dict] = []

        # Authoritative favourite state of the LIVE queue source
        # (the album / playlist the CTA favourites — NOT the active
        # track). _preview_meta is {} outside preview mode, so the
        # CTA can't read live-source fav state from it; this field is
        # the single source of truth in live mode. Seeded on every queue
        # source change by an async get_item fetch (_on_context_changed →
        # _apply_live_source_fav) so an already-favourited source loads
        # with a filled heart, and kept current by the favorite_toggled
        # bus signal (which the CTA itself emits) so external favourites
        # (phone, web) and our own toggle both land here.
        self._live_source_fav: bool = False

        # Left-pane mode — tri-state persisted via
        # ``settings.np_left_pane_mode``. ``cover`` shows just the
        # art + meta; ``lyrics`` shows the scrolling-lyrics pane
        # (the historical default); ``visualizer`` shows the
        # spectrum-bar widget. The legacy ``_show_lyrics`` bool used
        # to live here — we keep a derived view so existing call sites
        # like ``_update_lyrics_visibility`` don't have to be re-wired
        # all at once.
        try:
            from jellytoast.settings import get_settings

            self._np_left_pane_mode: str = get_settings().np_left_pane_mode
        except Exception:
            self._np_left_pane_mode = "lyrics"
        # Lazy-built visualizer widget — only constructed when the
        # user first switches into visualizer mode so the FFT bus
        # subscriptions don't fire for users who never look at it.
        # ``_visualizer_engine`` co-builds with the widget — see
        # ``_build_visualizer_widget``.
        self._visualizer_widget = None
        self._visualizer_engine = None

        # Auto-scroll vs user-scroll detection for the lyrics pane. The
        # "Live" pill button appears when the user has manually scrolled
        # away from the active line; clicking it re-snaps. The flag is
        # raised before each programmatic scroll and lowered when the
        # animation finishes — valueChanged callbacks check it to tell
        # which kind of scroll fired the signal.
        self._lyric_scroll_is_auto: bool = False
        self._user_off_live: bool = False

        # Synced lyrics state. `_lyrics_lines` parallels `_lyrics_widgets`
        # 1:1 — each entry is the line's start in *milliseconds* (0 for
        # unsynced lines). `_lyrics_starts_ms` is the same starts list,
        # cached because bisect over a list-of-tuples is awkward; we
        # search this and use the index into `_lyrics_widgets` to
        # highlight. `_active_line_idx` is the most recently highlighted
        # entry — the position-update handler bails early when the
        # active line hasn't changed, so 4Hz position pings don't cause
        # 4Hz repaints.
        self._lyrics_widgets: List[QLabel] = []
        self._lyrics_starts_ms: List[int] = []
        self._lyrics_synced: bool = False
        self._active_line_idx: int = -1

        self.setObjectName("npPage")
        # The host window paints its translucent body (with KWin blur
        # behind it) underneath; we want the frosted look to continue
        # all the way through this page. The descendant rule clears the
        # opaque QWidget background that GLOBAL_STYLE paints on every
        # QWidget so panes, scroll areas, labels, and frames let the
        # body show through. Per-widget styles on QPushButton / QSlider
        # / QScrollBar still take precedence because they're more
        # specific than this descendant selector.
        #
        # Scrollbar override: GLOBAL_STYLE colors the handle in the
        # accent (Jellyfin blue / purple, depending on theme). On this
        # page we want a quiet dim-white track that recedes when the
        # user isn't actively scrolling — the scrollbar isn't part of
        # the visual story here, it's a fallback affordance. Hover
        # state brightens it so it's still discoverable.
        self.setStyleSheet(f"""
            QWidget#npPage,
            QWidget#npPage QWidget,
            QWidget#npPage QFrame,
            QWidget#npPage QLabel,
            QWidget#npPage QScrollArea,
            QWidget#npPage QScrollArea > QWidget,
            QWidget#npPage QScrollArea > QWidget > QWidget {{
                background: transparent;
            }}
            QWidget#npPage QScrollBar:vertical {{
                background: transparent;
                width: 8px;
                margin: 4px 2px 4px 0;
                border: none;
            }}
            QWidget#npPage QScrollBar::handle:vertical {{
                background: {ink_alpha(0.12)};
                border-radius: 3px;
                min-height: 28px;
            }}
            QWidget#npPage QScrollBar::handle:vertical:hover,
            QWidget#npPage QScrollBar::handle:vertical:pressed {{
                background: {ink_alpha(0.32)};
            }}
            QWidget#npPage QScrollBar::add-line:vertical,
            QWidget#npPage QScrollBar::sub-line:vertical {{
                height: 0;
                background: transparent;
                border: none;
            }}
            QWidget#npPage QScrollBar::add-page:vertical,
            QWidget#npPage QScrollBar::sub-page:vertical {{
                background: transparent;
            }}
            QWidget#npPage QScrollBar:horizontal {{ height: 0; }}
        """)

        outer = QHBoxLayout(self)
        outer.setContentsMargins(20, 12, 20, 12)
        outer.setSpacing(20)

        # Equal stretch factors give the panes a 50/50 split. The cover
        # column gets pushed left and the track listing gets meaningfully
        # more horizontal room, so longer track titles stop truncating.
        outer.addWidget(self._build_left_pane(), 1)
        outer.addWidget(self._build_right_pane(), 1)

        self._connect_bus()
        # Render whatever's currently playing the first time the page
        # is shown — caller may already have a queue installed.
        self._refresh_now_playing(get_now_playing())
        self._refresh_track_list()
        # Initial chrome state: hide the lyrics toggle (no lyrics yet),
        # the Live button, and the preview-only Play CTA.
        self._update_lyrics_visibility()
        self._update_live_btn_visibility()
        self._update_cta_visibility()

        # Auto-hide scrollbars on both panes — they appear dim white on
        # scroll/hover and fade out after ~1s idle. The track-list
        # view (now a QListView, not a QScrollArea) uses the shared
        # install_autofade_scrollbars helper from ui_helpers instead
        # — same fade behavior, applied at view-construction time.
        self._lyrics_fader = _ScrollbarFader(self._lyrics_scroll)

    # ── Left pane (cover + metadata + lyrics) ───────────────────────────────

    # Index of the leading stretch item in the left pane's vbox (after
    # the 6px top spacer). Claimed only when there are no lyrics on
    # screen — see _update_lyrics_visibility.
    _LEFT_TOP_STRETCH_IDX = 1

    def _build_left_pane(self) -> QWidget:
        pane = QWidget()
        v = QVBoxLayout(pane)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)
        self._left_v = v
        # Lock the cover + header block at its natural minimum height so a
        # squeezed window can't collapse the title up into the album art.
        # Cover (COVER_SIZE) + spacing (20) + title row (~30) + subtitle
        # (~20) + meta (~16) + SPACE_MD + Play CTA (~36) + toggle/buffer
        # (~30). When the window goes below the resulting outer minimum
        # the bottom of the page tucks under the transport bar instead of
        # the inner widgets fighting for the same pixels.
        pane.setMinimumHeight(self.COVER_SIZE + 160)

        # No in-page back button — the top-bar's chrome back arrow is
        # the single source of "go back" and dismiss_requested is fired
        # via the top-bar nav stack (see jellytoast.app._dismiss_now_playing).

        # Small breathing room from the window chrome. The cover/header
        # block stays pinned to the top regardless of lyrics state — the
        # leading stretch (_LEFT_TOP_STRETCH_IDX) is a permanent 0 so the
        # block doesn't bounce when lyrics flip on/off or when preview
        # mode swaps in/out the Play CTA below. Lyrics scroll (stretch
        # 100) takes all the slack underneath.
        v.addSpacing(6)
        v.addStretch(0)

        # Cover — square, soft drop-shadow. The shadow is what reads
        # as "this is a real album object" against the frosted body;
        # without it the cover looks flat-pasted.
        self._cover = QLabel()
        self._cover.setFixedSize(self.COVER_SIZE, self.COVER_SIZE)
        self._cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        # No background / border-radius — the pixmap itself carries the
        # rounded corners from load_image_async, and a transparent label
        # means there's no visible frame around it on a fractional-DPR
        # screen where the pixmap can fall a hair short of COVER_SIZE.
        self._cover.setStyleSheet("background: transparent;")
        shadow = QGraphicsDropShadowEffect(self._cover)
        shadow.setBlurRadius(32)
        shadow.setColor(QColor(0, 0, 0, 115))  # ≈ rgba(0,0,0,0.45)
        shadow.setOffset(0, 12)
        self._cover.setGraphicsEffect(shadow)

        # Heart + download are hover-revealed corner overlays on the
        # cover itself — BR is the favorite heart, BL is the download
        # control (which auto-promotes to always-visible while a job
        # is in flight so the progress ring is legible without forcing
        # the user to hover). Matches the library tile pattern from
        # library_grid._paint_corner_button.
        self._fav_cta = CoverOverlayButton(
            self._cover,
            size=32,
            margin=10,
            bordered=False,
        )
        self._fav_cta.setIcon(icon("favorite_outline"))
        self._fav_cta.setIconSize(QSize(16, 16))
        self._fav_cta.setToolTip("Favorite")

        self._download_cta = _DownloadButton(self._cover)

        cover_row = QHBoxLayout()
        cover_row.setSpacing(SPACE_MD)
        cover_row.addStretch(1)
        cover_row.addWidget(self._cover)
        cover_row.addStretch(1)
        v.addLayout(cover_row)
        v.addSpacing(20)

        # Lyrics own the moment; title is the label.
        # Pin title and subtitle to their natural height — without this
        # QLabel's default Preferred vertical policy lets them grow into
        # any unclaimed space (e.g. when lyrics are hidden), pulling
        # them away from the cover and away from the CTAs below them.
        self._title = QLabel("Nothing Playing")
        self._title.setFont(font(TYPE_TITLE))
        # Idle styling — TEXT_DIM-equivalent so the placeholder reads
        # as inactive. _refresh_now_playing swaps to the bright
        # color when a real track lands.
        self._title.setStyleSheet(f"color: {IDLE_TEXT};")
        self._title.setWordWrap(True)
        self._title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._title.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        self._subtitle = QLabel("")
        self._subtitle.setFont(font(TYPE_CAPTION))
        self._subtitle.setStyleSheet(f"color: {ink_alpha(0.62)};")
        self._subtitle.setTextFormat(Qt.TextFormat.RichText)
        self._subtitle.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._subtitle.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

        # Tertiary line under the subtitle — track count + total
        # runtime, only shown in preview mode where the page
        # represents a whole album / playlist rather than a single
        # active track. Uses the MICRO tier with all-caps + a wider
        # letter-spacing so it reads as metadata rather than a
        # header.
        self._meta_line = QLabel("")
        self._meta_line.setFont(font(TYPE_MICRO))
        self._meta_line.setStyleSheet(f"color: {ink_alpha(0.42)}; letter-spacing: 0.6px;")
        self._meta_line.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._meta_line.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Fixed,
        )
        self._meta_line.setVisible(False)
        # Build the bare info column here so the CTA-row construction
        # below can flank it with download (left) + heart (right). Wiring
        # the flankers into the same row leaves the vertical band under
        # the cover for lyrics rather than a 3-button strip.
        info_col = QVBoxLayout()
        info_col.setContentsMargins(0, 0, 0, 0)
        info_col.setSpacing(0)
        info_col.addWidget(self._title)
        info_col.addSpacing(4)
        info_col.addWidget(self._subtitle)
        info_col.addWidget(self._meta_line)
        self._info_col = info_col

        # ── CTAs ────────────────────────────────────────────────────────
        # Heart and download flank the title/artist info row, freeing
        # the vertical strip for lyrics. Play button visible *only in
        # preview mode* — clicking it installs the previewed album as
        # the live queue and starts playback (the page transitions back
        # to live mode automatically on playback_started). In live mode
        # there's no Play here — the bottom transport bar already plays.
        self._play_cta = QPushButton(" Play")
        self._play_cta.setIcon(icon("play"))
        self._play_cta.setIconSize(QSize(16, 16))
        self._play_cta.setStyleSheet(button_qss(BTN_PRIMARY))
        self._play_cta.setCursor(Qt.CursorShape.PointingHandCursor)
        # StrongFocus so keyboard users who arrive at this page via
        # Enter-on-album-tile land here — pressing Enter again then
        # starts playback. button_qss(BTN_PRIMARY) renders a visible
        # focus state via Qt's default focus rect on top of the fill.
        self._play_cta.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._play_cta.clicked.connect(self._on_play_preview)
        self._play_cta.hide()  # shown by _update_cta_visibility in preview mode
        # Down arrow on the focused Play CTA dives into the track
        # list — keyboard parity for the visual layout (Play above
        # the track rows). Focus then sits on the first track and
        # arrow keys step row-to-row from there.
        self._play_cta.keyPressEvent = self._on_play_cta_key

        # Heart + download are constructed up by the cover row (they
        # flank the art there). Wire their click handlers here so the
        # ordering of slots stays grouped with the rest of the CTAs.
        self._fav_cta.clicked.connect(self._on_favorite_cta)
        self._download_cta.clicked.connect(self._on_download_cta)

        # Info row holds just the title/subtitle/meta column — heart and
        # download have moved up to flank the cover, so this row stays
        # naturally centered under the art.
        info_row = QHBoxLayout()
        info_row.setSpacing(SPACE_SM)
        info_row.setContentsMargins(0, 0, 0, 0)
        info_row.addStretch(1)
        info_row.addLayout(self._info_col)
        info_row.addStretch(1)
        v.addLayout(info_row)
        v.addSpacing(SPACE_MD)

        # Play CTA gets its own centered row below the info — only the
        # purple primary button lives here in preview mode, with no
        # flankers crowding it.
        cta_row = QHBoxLayout()
        cta_row.setSpacing(SPACE_MD)
        cta_row.setContentsMargins(0, 0, 0, 0)
        cta_row.addStretch(1)
        cta_row.addWidget(self._play_cta)
        cta_row.addStretch(1)
        v.addLayout(cta_row)
        v.addSpacing(SPACE_SM)

        # ── Lyrics toggle row ───────────────────────────────────────────
        # Small text button right-above the lyrics scroll. Hidden in
        # preview mode (lyrics aren't relevant when not listening) and
        # when the active track has no lyrics at all.
        toggle_row = QHBoxLayout()
        toggle_row.setContentsMargins(SPACE_LG, 0, SPACE_LG, 0)
        toggle_row.setSpacing(0)
        toggle_row.addStretch(1)
        self._lyrics_toggle_btn = QPushButton("Hide lyrics")
        self._lyrics_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._lyrics_toggle_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._lyrics_toggle_btn.setStyleSheet(_lyrics_caption_btn_qss())
        self._lyrics_toggle_btn.clicked.connect(self._toggle_lyrics)
        toggle_row.addWidget(self._lyrics_toggle_btn)
        v.addLayout(toggle_row)
        # Hover-only visibility — eligibility (live mode + has lyrics)
        # comes from _refresh_lyrics_visibility; this layer adds a hover
        # gate so the toggle stays out of the way when the user isn't
        # interacting with the lyrics area. Default state is hidden.
        self._lyrics_toggle_btn.hide()
        self._lyrics_toggle_eligible = False
        self._lyrics_toggle_hovered = False
        self._lyrics_toggle_hide_timer = QTimer(self)
        self._lyrics_toggle_hide_timer.setSingleShot(True)
        self._lyrics_toggle_hide_timer.setInterval(150)
        self._lyrics_toggle_hide_timer.timeout.connect(self._on_lyrics_hover_grace_done)

        # Live button row — sits just under the lyrics toggle, same
        # subtle styling so the two read as a stacked control cluster.
        # Visible only when the user has manually scrolled away from
        # the auto-tracked active line; click → re-snap.
        live_row = QHBoxLayout()
        live_row.setContentsMargins(SPACE_LG, 0, SPACE_LG, 0)
        live_row.setSpacing(0)
        live_row.addStretch(1)
        self._live_btn = QPushButton("● Live")
        self._live_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._live_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._live_btn.setStyleSheet(_lyrics_caption_btn_qss())
        self._live_btn.clicked.connect(self._resnap_to_live)
        self._live_btn.hide()
        live_row.addWidget(self._live_btn)
        v.addLayout(live_row)

        # Lyrics scroll area — fills the remaining vertical space.
        self._lyrics_scroll = QScrollArea(self)
        self._lyrics_scroll.setWidgetResizable(True)
        self._lyrics_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._lyrics_scroll.setStyleSheet("QScrollArea { background: transparent; border: none; }")
        # Flatten viewport first-paint — see feedback_wayland_flash_diagnostics.
        _vp = self._lyrics_scroll.viewport()
        _vp.setAutoFillBackground(False)
        _vp.setBackgroundRole(QPalette.ColorRole.NoRole)
        self._lyrics_container = QWidget()
        self._lyrics_container.setStyleSheet("background: transparent;")
        self._lyrics_layout = QVBoxLayout(self._lyrics_container)
        # Generous left padding so the active line breathes; we
        # left-align the lyrics like Apple Music macOS rather than
        # center, which reads better as verse on a wide pane.
        self._lyrics_layout.setContentsMargins(24, 8, 24, 24)
        self._lyrics_layout.setSpacing(0)
        self._lyrics_layout.addStretch(1)
        self._lyrics_scroll.setWidget(self._lyrics_container)
        # Hover gate for the toggle button + lyrics-area Enter/Leave —
        # mouse inside ANY left-pane content surface (scroll, cover,
        # visualizer when built) OR the toggle button itself counts as
        # hovered. The cover + visualizer wires break the chicken-and-
        # egg in cover/visualizer mode where the scroll is hidden and
        # the button starts invisible.
        self._lyrics_scroll.installEventFilter(self)
        self._lyrics_toggle_btn.installEventFilter(self)
        self._cover.installEventFilter(self)
        # High stretch so the lyrics scroll dominates available vertical
        # space when visible, plus a low-stretch trailing absorber that
        # claims the leftover when lyrics is hidden. This keeps the
        # widgets above (cover, title, subtitle, CTAs, toggle, live)
        # at stable y-positions across toggle — without the trailing
        # stretch, hiding the lyrics removes the only stretch claimer
        # and Qt redistributes the leftover space among the remaining
        # widgets, sliding everything around.
        v.addWidget(self._lyrics_scroll, 100)
        v.addStretch(1)

        # Smooth-scroll animation on the lyrics scrollbar — used by the
        # synced-lyrics auto-scroll. 300ms ease-out per the design pass.
        self._lyrics_anim = QPropertyAnimation(
            self._lyrics_scroll.verticalScrollBar(), b"value", self
        )
        self._lyrics_anim.setDuration(300)
        self._lyrics_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        # When the smooth scroll finishes, drop the auto-scroll flag so
        # the next valueChanged event is correctly attributed to the user.
        self._lyrics_anim.finished.connect(lambda: setattr(self, "_lyric_scroll_is_auto", False))
        # Watch the scrollbar to detect manual user scrolls — if the
        # user grabs the bar (or wheels in the viewport), we surface the
        # "Live" button so they can re-snap to the active line.
        self._lyrics_scroll.verticalScrollBar().valueChanged.connect(self._on_lyrics_scrolled)

        return pane

    # ── Right pane (track list / queue) ─────────────────────────────────────

    def _build_right_pane(self) -> QWidget:
        pane = QWidget()
        v = QVBoxLayout(pane)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(0)

        # Single small ALL-CAPS kicker. The left pane already carries
        # the title/artist; a redundant big "Album / 19" header on the
        # right just clutters. The kicker tells the user *what kind* of
        # context they're looking at and what its source is — see the
        # text built in _refresh_track_list().
        # type_qss(TYPE_MICRO) (rather than font(TYPE_MICRO)) so that the
        # kind/source-label concatenation in _refresh_track_list ("ALBUM ·
        # Currents") keeps its mixed casing — QFont's AllUppercase would
        # force-uppercase the source label too.
        self._right_kicker = QLabel("UP NEXT")
        # Bumped from TYPE_MICRO (11px) to 13px bold so the kicker
        # reads as a real heading at glance distance, brighter color
        # (0.55 → 0.78) so it doesn't disappear against the frosted
        # background. Letter-spacing stays out of QSS — Qt stylesheets
        # ignore that property; the all-caps source strings carry the
        # visual rhythm without it.
        self._right_kicker.setStyleSheet(
            f"color: {ink_alpha(0.78)}; {type_qss(TYPE_BODY)} font-weight: 700;"
        )
        # Left-align with the row's title column. _TrackRow's layout
        # is contentsMargins(12, 0, 12, 0) + 32 wide index + 14 spacing
        # = 58px from the row's left edge to the title text. Match that
        # so the kicker sits directly above where titles start, not
        # centered above the whole pane.
        self._right_kicker.setAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self._right_kicker.setContentsMargins(58, 4, 12, 0)
        v.addWidget(self._right_kicker)
        v.addSpacing(16)

        # Track list — model/view/delegate. The view's own scroll bar
        # replaces the legacy QScrollArea wrapper; install_autofade_
        # scrollbars handles the fade behavior. _list_container is
        # kept as an attribute name (== the view) so existing call
        # sites that reference it stay valid.
        from jellytoast.ui_helpers import install_autofade_scrollbars

        self._tracks_model = _TracksModel(self)
        self._tracks_delegate = _TrackDelegate(self)
        self._list_container = _TracksListView(
            self._tracks_model,
            self._tracks_delegate,
            self,
        )
        install_autofade_scrollbars(self._list_container)
        # Click on a row → jump to that play index.
        self._list_container.track_clicked.connect(self._on_row_clicked)
        # Drag start/end → flip the kicker to "QUEUE" during drag.
        self._list_container.drag_state_changed.connect(self._on_drag_state_changed)
        # Right-click → context menu (Play next / Add to queue /
        # Remove from queue).
        self._list_container.track_context_menu.connect(self._on_track_context_menu)
        # Drag-reorder → commit to the queue (the page maps source-order
        # display rows back to play-order by Id; see _on_reorder_requested).
        self._list_container.reorder_requested.connect(self._on_reorder_requested)
        # Live-accent: delegate re-reads ACCENT on every paint, so a
        # theme change just needs viewport().update().
        self.bus.theme_changed.connect(self._list_container.viewport().update)
        # Stack the track list with an empty-state surface so a queue
        # that resolves to zero tracks (no current playback yet,
        # cleared "Up Next") reads as "nothing queued — go browse"
        # instead of a silent blank.
        self._tracks_empty_state = EmptyState(
            glyph="♪",
            headline="Nothing queued",
            sub="Pick an album, playlist, or song to start the queue.",
            parent=self,
        )
        self._tracks_stack = QStackedWidget()
        self._tracks_stack.setStyleSheet("background: transparent;")
        self._tracks_stack.addWidget(self._list_container)
        self._tracks_stack.addWidget(self._tracks_empty_state)
        v.addWidget(self._tracks_stack, 1)

        return pane

    # ── Bus wiring ──────────────────────────────────────────────────────────

    def _connect_bus(self):
        self.bus.playback_started.connect(self._on_playback_started)
        self.bus.playback_stopped.connect(self._on_playback_stopped)
        # Settings → "Refresh album art" — re-fetch the current track's
        # cover so server-side art changes appear without a track skip.
        self.bus.image_cache_cleared.connect(
            self._on_image_cache_cleared,
        )
        self.bus.queue_changed.connect(self._on_queue_changed)
        self.bus.queue_context_changed.connect(self._on_context_changed)
        # Unified radio rendering — see jellytoast/radio_state.py. One
        # handler drives the whole page's radio render (cover, title,
        # subtitle, LIVE · station meta line) from a single dataclass.
        self.bus.radio_state_changed.connect(self._on_radio_state)
        self._is_radio: bool = False
        self._radio_station_name: str = ""
        # Seed from the current snapshot so a page constructed
        # mid-session (user opens NowPlayingPage after radio started)
        # picks up the in-flight state without waiting for the next
        # bus event.
        from jellytoast import radio_state as _radio_state

        seed = _radio_state.current()
        if seed is not None:
            self._on_radio_state(seed)
        self.bus.position_updated.connect(self._on_position_updated)
        self.bus.favorite_toggled.connect(self._on_favorite_toggled)
        self.bus.lyrics_font_size_changed.connect(self._on_lyrics_font_size_changed)
        # Download progress for the album-view download CTA — filtered
        # to the previewed item in the handler.
        self.bus.download_progress.connect(self._on_download_progress)
        # Cover-art prefetch for the next-up track — same pattern as
        # the bar / mini player. See feedback_now_playing_cover_pipeline.
        self.bus.queue_prefetch_request.connect(self._prefetch_cover)
        # Live-accent: walk every visible track row and refresh the
        # active-row tint, plus restamp the heart CTA from current
        # state. The track-row CSS staticmethods now re-read ACCENT
        # at call time so a fresh _reapply_styling() picks up the new
        # colour.
        self.bus.theme_changed.connect(self._reapply_theme)
        # Cross-DPR cover refresh — re-issue the main cover load when
        # the user moves the window to a different-scale monitor so
        # the result is sized for the new physical target.
        self.bus.dpr_changed.connect(self._on_dpr_changed)
        # Internal async-result signals — preview meta / tracks land on
        # the GUI thread via these. Wired here (one-time) so the slots
        # are connected before the first load_preview() can fire.
        self._lyrics_loaded.connect(self._on_lyrics_loaded)
        self._preview_meta_loaded.connect(self._on_preview_meta_loaded)
        self._preview_tracks_loaded.connect(self._on_preview_tracks_loaded)

    def _on_dpr_changed(self):
        # If we're showing a preview, re-fire the preview cover load;
        # otherwise re-fire the live now-playing cover load. Either
        # path goes through load_image_async at the new physical
        # target so the resulting pixmap is correctly sized.
        if self._preview_id:
            # Re-issue ONLY the preview cover at the new physical target.
            # Going back through load_preview() is dead here: its
            # early-return guard fires on the unchanged id+kind+meta, so
            # the cover never re-fetched at the new DPR (and it would also
            # incur wasted get_item/get_tracks round-trips). Replicate the
            # cover-load block from _on_preview_meta_loaded directly.
            if self._preview_meta:
                dpr = dpr_bucket(screen_dpr(self))
                target_phys = max(self.COVER_SIZE, int(round(self.COVER_SIZE * dpr)))
                radius_phys = int(round(12 * dpr))
                server_px = max(512, target_phys)
                cover_url = self.api.get_image_url(self._preview_id, "Primary", server_px)
                if cover_url:
                    load_image_async(
                        f"{self._preview_id}|nppage",
                        cover_url,
                        target_phys,
                        target_phys,
                        self._on_cover_loaded,
                        rounded_radius=radius_phys,
                        on_error=lambda: None,
                    )
            return
        np = get_now_playing()
        if np.item_id:
            self._refresh_now_playing(np)

    def _reapply_theme(self):
        """Full theme re-stamp on theme_changed.

        The track-list delegate re-reads tokens every paint, so a
        viewport invalidate (wired in _connect_bus) covers the rows.
        This handler covers the chrome the delegate doesn't: the
        favourite / play CTAs and the metadata text whose colour QSS
        is baked at construction."""
        from jellytoast import ui_helpers as _u

        # Favourite + play state — from preview meta if previewing,
        # otherwise the live SOURCE-collection fav state (the CTA
        # favourites the album/playlist, not the active track, so this
        # tracks _live_source_fav rather than np.is_favorite).
        if self._preview_id:
            cur_fav = bool((self._preview_meta.get("UserData") or {}).get("IsFavorite", False))
            has_track = True
        else:
            np = get_now_playing()
            cur_fav = self._live_source_fav
            has_track = bool(np.item_id)
        self._fav_cta.setIcon(
            accent_icon("favorite_filled") if cur_fav else icon("favorite_outline")
        )
        # Play CTA — re-issue the glyph in the new tint + re-stamp the
        # accent button QSS.
        self._play_cta.setIcon(icon("play"))
        self._play_cta.setStyleSheet(button_qss(BTN_PRIMARY))
        # Metadata text — colour QSS is baked at construction, so a
        # theme switch with no track change otherwise leaves it stale.
        self._subtitle.setStyleSheet(f"color: {ink_alpha(0.62)};")
        self._meta_line.setStyleSheet(
            f"color: {ink_alpha(0.42)}; letter-spacing: 0.6px;"
        )
        self._title.setStyleSheet(
            f"color: {ink_alpha(0.95) if has_track else _u.IDLE_TEXT};"
        )
        # Right-pane kicker ("ALBUM · 19") — colour QSS baked at
        # construction, same staleness as the metadata text above.
        self._right_kicker.setStyleSheet(
            f"color: {ink_alpha(0.78)}; {type_qss(TYPE_BODY)} font-weight: 700;"
        )
        # Lyrics caption buttons ("Hide lyrics" / "● Live") — faint-ink
        # QSS baked at construction, so re-stamp to the live family.
        self._lyrics_toggle_btn.setStyleSheet(_lyrics_caption_btn_qss())
        self._live_btn.setStyleSheet(_lyrics_caption_btn_qss())
        # Lyrics body — unsynced lines + the status fallback label bake
        # ink at build time (synced lines self-correct on the next
        # position tick). _LyricsMixin handles the re-stamp.
        self._restamp_lyrics_theme()

    @Slot(object)
    def _prefetch_cover(self, np):
        if np is None:
            return
        image_id = getattr(np, "image_id", "") or getattr(np, "item_id", "")
        if not image_id:
            return
        # HiDPI: target physical pixels for the cache + rounded radius
        # so the live load (which uses identical keying) lands on the
        # same slot. _on_cover_loaded tags the result with raw DPR.
        # dpr_bucket here so the disk-cache key doesn't fragment across
        # Wayland fractional-scale jitter.
        dpr = dpr_bucket(screen_dpr(self))
        target_phys = max(self.COVER_SIZE, int(round(self.COVER_SIZE * dpr)))
        radius_phys = int(round(12 * dpr))
        server_px = max(512, target_phys)
        url = self.api.get_image_url(image_id, "Primary", server_px)
        if not url:
            return
        load_image_async(
            f"{image_id}|nppage",
            url,
            target_phys,
            target_phys,
            lambda _pix: None,
            rounded_radius=radius_phys,
            on_error=lambda: None,
        )

    @Slot(object)
    def _on_playback_started(self, np: NowPlaying):
        # In preview mode the page is showing a different album — only
        # update the now-playing data when we're in live mode.
        if self._preview_id:
            return
        self._refresh_now_playing(np)

    def _on_image_cache_cleared(self):
        # Same logic as a playback-start: re-render the current track's
        # cover. Preview mode shows a different album, so its art
        # comes back on the next preview navigation rather than now.
        if self._preview_id:
            return
        np = get_now_playing()
        if np.item_id or np.image_id:
            self._refresh_now_playing(np)

    @Slot()
    def _on_playback_stopped(self):
        if self._preview_id:
            return
        from jellytoast import ui_helpers as _u

        self._title.setText("Nothing Playing")
        # Re-dim the title to the idle styling (the active-track
        # path in _refresh_now_playing brightens it back). Read the live
        # ui_helpers token so a dark↔light flip while idle re-stamps it.
        self._title.setStyleSheet(f"color: {_u.IDLE_TEXT};")
        self._subtitle.setText("")
        self._cover.clear()
        self._cover_orig = None
        self._set_lyrics_text("")

    def _on_drag_state_changed(self, dragging: bool):
        """Called by the track list view on begin/end drag. The right-
        pane kicker should read "QUEUE" the moment the user starts
        dragging — even before the drop completes — because a drag-in-
        progress conceptually breaks the source ordering. On drag end
        the regular logic in _refresh_track_list picks the correct
        label (ALBUM / PLAYLIST / QUEUE if modified)."""
        if dragging:
            self._right_kicker.setText("QUEUE")
        elif not self._preview_id:
            self._refresh_track_list()

    @Slot(list, int)
    def _on_queue_changed(self, _items: list, _index: int):
        # Preview mode is browsing a different list — ignore live-queue
        # mutations until the user exits preview.
        if self._preview_id:
            return
        # Defer if a drag is in flight. ``move_item`` runs synchronously
        # inside ``dropEvent`` and re-emits ``queue_changed`` before
        # the drag fully unwinds — re-rendering here would delete the
        # source row mid-drag, stranding our _ghost_row reference and
        # leaving the new rows in an inconsistent state.
        # ``_on_drag_state_changed`` re-renders unconditionally on drag end.
        if self._list_container.is_dragging():
            return
        self._refresh_track_list()

    @Slot(object)
    def _on_context_changed(self, ctx: QueueContext):
        # The live queue source changed — drop the cached source-fav
        # state so a new album/playlist doesn't inherit the previous
        # one's filled heart. QueueContext carries no fav flag, so the
        # conservative default is unfavourited; an external favourite
        # event (favorite_toggled) re-fills it if warranted. Done before
        # the preview/drag short-circuits because the live source's
        # identity changed regardless of which surface is on screen.
        self._live_source_fav = False
        # Fetch the new source's REAL favourite state so the heart is
        # correct on load — an already-favourited album/playlist must
        # show a filled heart without waiting for the user to interact or
        # for an external favorite_toggled event. The async result is
        # staleness-guarded in _apply_live_source_fav against a later
        # source change. Fires regardless of preview/drag (so the live
        # state is right the moment the user returns to live).
        src = ctx.source_id if ctx else ""
        if src:
            run_async(
                self.api.get_item,
                src,
                on_result=lambda meta, sid=src: self._apply_live_source_fav(sid, meta),
                on_error=lambda _e: None,
            )
        # The radio-specific rendering lives in _on_radio_state below;
        # this slot only needs to refresh the track list (which is
        # gated on preview mode + drag state).
        if self._preview_id:
            return
        if self._list_container.is_dragging():
            return
        self._refresh_track_list()

    def _apply_live_source_fav(self, source_id: str, meta: Optional[Dict]):
        """Apply a fetched live-source favourite state to the page.

        Staleness-guarded: a fetch for a source the user has since moved
        off (a new album/playlist became the live source while this was
        in flight) is dropped, so a slow reply can't clobber the current
        source's heart."""
        if source_id != self.queue_mgr.context.source_id:
            return
        self._live_source_fav = bool(
            ((meta or {}).get("UserData") or {}).get("IsFavorite", False)
        )
        self._refresh_fav_cta_icon()

    def _refresh_fav_cta_icon(self):
        """Re-stamp the favourite CTA glyph from the authoritative
        source-fav state — ``_preview_meta`` while previewing, otherwise
        ``_live_source_fav``. One reader so every entry point (theme
        reapply, context change, external toggle, preview exit) stays
        consistent."""
        if self._preview_id:
            cur_fav = bool((self._preview_meta.get("UserData") or {}).get("IsFavorite", False))
        else:
            cur_fav = self._live_source_fav
        self._fav_cta.setIcon(
            accent_icon("favorite_filled") if cur_fav else icon("favorite_outline")
        )

    @Slot(object)
    def _on_radio_state(self, state):
        """Unified radio renderer — receives a ``radio_state.RadioState``
        snapshot (or ``None`` to clear). Same render contract as the
        NP bar + mini player: title = song-or-station, subtitle =
        artist, meta-line = LIVE · station, cover = per-track art or
        station logo. Preview mode short-circuits since the user is
        browsing a different surface."""
        if state is None:
            self._is_radio = False
            self._radio_station_name = ""
            self._refresh_meta_line()
            return

        self._is_radio = True
        self._radio_station_name = state.station_name
        if self._preview_id:
            # Preview is browsing a different album — keep the radio
            # state internally (so a future toggle back to live mode
            # picks up correctly) but don't repaint over the preview.
            self._refresh_meta_line()
            return

        # Title + subtitle.
        self._title.setText(state.display_title or "Unknown")
        self._title.setStyleSheet(f"color: {ink_alpha(0.95)};")
        if state.display_subtitle:
            self._subtitle.setText(state.display_subtitle)
        else:
            self._subtitle.setText("")

        # Cover.
        cover_url = state.display_cover_url
        if cover_url:
            self._load_radio_cover(cover_url)

        # LIVE · station badge in the meta line.
        self._refresh_meta_line()

    def _load_radio_cover(self, url: str) -> None:
        if not url:
            return
        dpr = dpr_bucket(screen_dpr(self))
        target_phys = max(self.COVER_SIZE, int(round(self.COVER_SIZE * dpr)))
        radius_phys = int(round(12 * dpr))
        load_image_async(
            f"radio:{url}|nppage",
            url,
            target_phys,
            target_phys,
            self._on_cover_loaded,
            rounded_radius=radius_phys,
            on_error=lambda: None,
            priority="high",
        )

    @Slot(str, bool)
    def _on_favorite_toggled(self, item_id: str, fav: bool):
        # Sync the heart icon when the live queue's source (album /
        # playlist) is favorited from another client (a phone app,
        # Jellyfin Web in a browser, another machine) — or by our own
        # CTA, which re-emits through here so the live-source fav state
        # stays authoritative.
        live_source = self.queue_mgr.context.source_id
        if item_id and item_id == live_source:
            # Keep the live-source authority current even while a
            # preview is open, so toggling back to live reads right.
            self._live_source_fav = fav
        target = self._preview_id or live_source
        if item_id == target:
            self._fav_cta.setIcon(
                accent_icon("favorite_filled") if fav else icon("favorite_outline")
            )

    # ── Updaters ────────────────────────────────────────────────────────────

    def _refresh_now_playing(self, np: NowPlaying):
        if not np.item_id:
            return
        # Radio mode is owned entirely by ``_on_radio_state``. Skip the
        # rest of this method so a subsequent playback_started (which
        # carries the synthetic station id + np.title=station name)
        # can't clobber the song / artist / per-track art we just
        # rendered. The radio state's own emissions repaint when ICY
        # / cover-lookup events land.
        if getattr(self, "_is_radio", False):
            return
        self._title.setText(np.title or "Unknown")
        # Brighten the title — _on_playback_stopped dims it for the
        # "Nothing Playing" idle state; an active track needs the
        # full-weight color.
        self._title.setStyleSheet(f"color: {ink_alpha(0.95)};")
        bits = []
        if np.subtitle:
            bits.append(np.subtitle)
        if np.album:
            bits.append(np.album)
        # Render the bullet at lower opacity so the eye reads "Artist · Album"
        # as a single phrase. setTextFormat(RichText) is set in _build.
        if bits:
            sep = f'<span style="color: {ink_alpha(0.40)};"> · </span>'
            self._subtitle.setText(sep.join(bits))
        else:
            self._subtitle.setText("")

        image_id = np.image_id or np.item_id
        if image_id and not getattr(self, "_is_radio", False):
            # Build our own URL at the page's target size — see the
            # bar's _on_started for why we don't reuse np.thumb_url.
            # Radio mode skips this entirely — _on_context_changed
            # owns the cover (station logo + per-track MusicBrainz
            # art); the synthetic station id has no provider image.
            # 512 covers a 200-logical cover at 2× DPR with headroom;
            # at 3+× we bump the server request past 512 so the source
            # stays larger than the physical render target.
            dpr = dpr_bucket(screen_dpr(self))
            target_phys = max(self.COVER_SIZE, int(round(self.COVER_SIZE * dpr)))
            radius_phys = int(round(12 * dpr))
            server_px = max(512, target_phys)
            url = self.api.get_image_url(image_id, "Primary", server_px)
            load_image_async(
                f"{image_id}|nppage",
                url,
                target_phys,
                target_phys,
                self._on_cover_loaded,
                rounded_radius=radius_phys,
                on_error=lambda: None,
                priority="high",
            )
        self._fetch_lyrics(np.item_id)

    def _on_cover_loaded(self, pix: QPixmap):
        self._cover_orig = pix
        if pix.isNull():
            return
        # load_image_async fetched at the bucketed DPR (cache-friendly).
        # scale_pixmap_for_dpr re-scales to the *actual* screen DPR so
        # the pixmap fills COVER_SIZE logical points exactly — without
        # this, fractional-DPR screens would leave a few logical pixels
        # short and reveal whatever sits behind the cover.
        pix = scale_pixmap_for_dpr(pix, self.COVER_SIZE, screen_dpr(self))
        self._cover.setPixmap(pix)

    def _refresh_track_list(self):
        # Preview mode short-circuits the queue-driven path: we render
        # the previewed item's tracks in source order and only highlight
        # a row if it matches the live now-playing track (which can
        # happen when the user previews the same album they're listening
        # to).
        if self._preview_id:
            label = self._preview_meta.get("Name", "") or "Loading…"
            # Kind-specific kicker (ALBUM / PLAYLIST / ARTIST) — the
            # "browsing vs now-playing" distinction lives in the top
            # bar now, so the kicker focuses on *what kind of content*
            # the user is looking at.
            preview_kicker = {
                "album": "ALBUM",
                "playlist": "PLAYLIST",
                "artist": "ARTIST",
            }.get(self._preview_kind, "BROWSING")
            self._right_kicker.setText(f"{preview_kicker}  ·  {label}")
            self._displayed_items_kind = "source"
            highlight_index = self._preview_current_highlight_index()
            # Album previews are by-definition single-artist; playlists
            # only show artists if they actually span more than one.
            is_album = self._preview_kind == QueueKind.ALBUM
            self._populate_rows(
                self._preview_tracks,
                show_artist=(
                    False if is_album else self._items_span_multiple_artists(self._preview_tracks)
                ),
                highlight_index=highlight_index,
                multi_disc_enabled=is_album,
            )
            return

        ctx = self.queue_mgr.context
        # Single ALL-CAPS kicker. When there's a human-readable source
        # (album / playlist name) we append it after the kind label —
        # "ALBUM · 19", "PLAYLIST · Coffeehouse" — so the user has the
        # full context in one glance without a separate big title.
        # Once the queue diverges from its source (user added a track,
        # dragged a row, removed an item) the queue is no longer a
        # faithful reflection of the source — the kicker collapses to
        # "QUEUE" so the label can't lie.
        is_modified = getattr(self.queue_mgr._q, "is_modified", False)
        if is_modified:
            self._right_kicker.setText("QUEUE")
        else:
            kind_label, default_label = {
                QueueKind.ALBUM: ("ALBUM", "Album"),
                QueueKind.PLAYLIST: ("PLAYLIST", "Playlist"),
                QueueKind.ARTIST: ("ARTIST", "Artist"),
                QueueKind.SHUFFLE: ("LIBRARY SHUFFLE", "Library shuffle"),
                QueueKind.SEARCH: ("SEARCH RESULTS", "Search"),
                QueueKind.MANUAL: ("QUEUE", "Up next"),
                QueueKind.INSTANT_MIX: ("INSTANT MIX", "Instant mix"),
            }.get(ctx.kind, ("QUEUE", "Up next"))
            if ctx.source_label and ctx.source_label != default_label:
                self._right_kicker.setText(f"{kind_label}  ·  {ctx.source_label}")
            else:
                self._right_kicker.setText(kind_label)

        # Pick the right items list per the context's natural ordering.
        # Source-order rendering (album / playlist track list) is only
        # honored while the queue is *pristine* — once the user has
        # added a track, dragged a row, or removed an item the queue
        # has diverged from the source and we render in play-order so
        # the drag visibly takes effect.
        if ctx.kind in _SOURCE_ORDER_KINDS and not is_modified:
            items = self.queue_mgr.original_items
            self._displayed_items_kind = "source"
            # In source-order mode the highlighted row is the
            # original_items index of the currently-playing track.
            current_orig_idx = self._current_original_index()
            highlight_index = current_orig_idx
        else:
            items = self.queue_mgr.queue  # play-order
            self._displayed_items_kind = "play"
            highlight_index = self.queue_mgr.current_index

        # Show the artist sub-line only when the queue actually spans
        # more than one artist. Previously this flipped on any
        # modified queue (drag-reorder, add-to-queue), which lit up
        # the sub-line under every track even when the user was just
        # reordering tracks within a single-artist album. Data-driven
        # check fires only when the queue genuinely crosses artists.
        show_artist = self._items_span_multiple_artists(items)
        # Disc dividers only apply to a *pristine* ALBUM context — once
        # the queue is reordered they no longer correspond to discs.
        multi_disc_enabled = ctx.kind == QueueKind.ALBUM and not is_modified

        self._populate_rows(items, show_artist, highlight_index, multi_disc_enabled)

    @staticmethod
    def _items_span_multiple_artists(items: List[Dict]) -> bool:
        """True iff the given track list contains tracks attributed to
        more than one AlbumArtist. Used to decide whether to render
        the per-row artist sub-line — single-artist queues hide it as
        redundant chrome."""
        seen = set()
        for t in items:
            artist = t.get("AlbumArtist") or ""
            if not artist:
                artists = t.get("Artists") or []
                if artists:
                    artist = artists[0] or ""
            seen.add((artist or "").strip().lower())
            if len(seen) > 1:
                return True
        return False

    def _preview_current_highlight_index(self) -> int:
        """If the live now-playing track happens to be in the previewed
        item's track list, return that row's index so we can highlight
        it. -1 if the previewed item doesn't contain the live track."""
        np = get_now_playing()
        cur_id = (np.item_id or "").lower() if np else ""
        if not cur_id or not self._preview_tracks:
            return -1
        for i, t in enumerate(self._preview_tracks):
            if (t.get("Id") or "").lower() == cur_id:
                return i
        return -1

    def _current_original_index(self) -> int:
        """Index into `original_items` of the currently-playing track —
        what the right pane should highlight when it's rendering source
        order. -1 if nothing is playing."""
        cur = self.queue_mgr.current_item
        if not cur:
            return -1
        target = (cur.get("Id") or "").lower()
        for i, it in enumerate(self.queue_mgr.original_items):
            if (it.get("Id") or "").lower() == target:
                return i
        return -1

    def _populate_rows(
        self,
        items: List[Dict],
        show_artist: bool,
        highlight_index: int,
        multi_disc_enabled: bool = False,
    ):
        """Drop the previous rendering and rebuild the model with the
        new items. Disc dividers only show in pristine multi-disc
        ALBUM context (caller flips multi_disc_enabled accordingly)
        and only when more than one disc is actually represented in
        the items."""
        # Multi-disc detection — only meaningful for ALBUM contexts. In
        # PLAYLIST / SHUFFLE / SEARCH views every track comes from a
        # different album with its own ParentIndexNumber, so grouping
        # by disc would produce an absurd "Disc 1, Disc 2, Disc 1, …"
        # interleave. Caller flips multi_disc_enabled only for ALBUM.
        disc_numbers = {int(t.get("ParentIndexNumber") or 1) for t in items}
        multi_disc = multi_disc_enabled and (
            len(disc_numbers) > 1 or any(d > 1 for d in disc_numbers)
        )
        drag_enabled = not bool(self._preview_id)
        self._tracks_model.set_state(
            items,
            highlight_index,
            show_artist,
            drag_enabled,
            multi_disc=multi_disc,
        )
        # Flip the track-list ↔ empty-state surface. Preview mode
        # with no tracks reads as "this album has no tracks here yet"
        # (likely mid-fetch — we don't show empty until the load
        # actually returns nothing). Live mode with no tracks is the
        # "nothing queued" empty state.
        if not items:
            if self._preview_id:
                # Preview-mode empty: still loading or genuinely
                # empty source. Keep the list page so the rest of
                # the page (cover, lyrics) reads correctly while
                # tracks land.
                self._tracks_stack.setCurrentIndex(0)
            else:
                self._tracks_empty_state.set_state(
                    headline="Nothing queued",
                    sub="Pick an album, playlist, or song to start the queue.",
                )
                self._tracks_stack.setCurrentIndex(1)
        else:
            self._tracks_stack.setCurrentIndex(0)
        # Scroll the highlighted row into view (if any). Deferred a
        # tick so QListView has computed cell rects post-reset.
        if highlight_index >= 0:

            def _scroll_to_target(h=highlight_index):
                try:
                    row = self._tracks_model.row_for_play_index(h)
                    if row >= 0:
                        idx = self._tracks_model.index(row, 0)
                        self._list_container.scrollTo(
                            idx,
                            QAbstractItemView.ScrollHint.PositionAtCenter,
                        )
                except RuntimeError:
                    pass

            QTimer.singleShot(0, _scroll_to_target)

    @Slot(int)
    def _on_row_clicked(self, displayed_index: int):
        # Preview mode: clicking any row installs the previewed item as
        # the live queue and starts from that index. The page transitions
        # back to live mode automatically once playback_started fires.
        if self._preview_id:
            if not (0 <= displayed_index < len(self._preview_tracks)):
                return
            # Snapshot, then drop preview state *before* emitting so the
            # sync-fired playback_started / queue_changed handlers see
            # live mode (same race as _on_play_preview).
            tracks = list(self._preview_tracks)
            ctx = QueueContext(
                kind=self._preview_kind,
                source_id=self._preview_id,
                source_label=self._preview_meta.get("Name", ""),
            )
            self._preview_id = ""
            self._preview_meta = {}
            self._preview_tracks = []
            self._update_cta_visibility()
            self.preview_changed.emit(False)
            self.bus.queue_play_now.emit(tracks, displayed_index, ctx)
            return
        # The displayed index is into either `original_items` (source
        # order) or `queue` (play order). track_jumped wants a play-order
        # index, so map source → play when needed.
        if self._displayed_items_kind == "source":
            orig = self.queue_mgr.original_items
            if not (0 <= displayed_index < len(orig)):
                return
            target_id = (orig[displayed_index].get("Id") or "").lower()
            for play_idx, it in enumerate(self.queue_mgr.queue):
                if (it.get("Id") or "").lower() == target_id:
                    self.bus.track_jumped.emit(play_idx)
                    return
        else:
            self.bus.track_jumped.emit(displayed_index)

    @Slot(int, QPoint)
    def _on_track_context_menu(self, play_idx: int, global_pos: QPoint):
        """Right-click on a track row → Play next / Add to queue /
        Remove from queue. Resolve the item dict from the appropriate
        list given the current display mode (source-order vs play-order
        vs preview)."""
        # Resolve the item dict from the right source.
        if self._preview_id:
            tracks = self._preview_tracks
        elif self._displayed_items_kind == "source":
            tracks = self.queue_mgr.original_items
        else:
            tracks = self.queue_mgr.queue
        if not (0 <= play_idx < len(tracks)):
            return
        item = tracks[play_idx]
        # Remove from queue is only meaningful in live mode (the
        # queue is the displayed list); preview mode has no concept
        # of "remove" since the user isn't editing a queue.
        from jellytoast.ui_helpers import opaque_menu

        menu = opaque_menu(self._list_container)
        play_next = menu.addAction("Play next")
        add_end = menu.addAction("Add to queue")
        remove_act = None
        if not self._preview_id:
            menu.addSeparator()
            remove_act = menu.addAction("Remove from queue")
        chosen = menu.exec(global_pos)
        if chosen is play_next:
            self.bus.queue_add_next.emit([item])
        elif chosen is add_end:
            self.bus.queue_add_end.emit([item])
        elif chosen is not None and chosen is remove_act:
            # `play_idx` indexes the DISPLAYED list. In source-order display
            # it's an original_items index, but queue_remove_at →
            # QueueManager.remove_at expects a PLAY-ORDER index. A shuffled
            # album stays in source display (shuffle permutes play_order
            # without setting is_modified), so the two diverge — map by Id
            # like _on_row_clicked does, or the wrong track gets removed.
            remove_idx = play_idx
            if self._displayed_items_kind == "source":
                target_id = (item.get("Id") or "").lower()
                remove_idx = next(
                    (
                        i
                        for i, it in enumerate(self.queue_mgr.queue)
                        if (it.get("Id") or "").lower() == target_id
                    ),
                    -1,
                )
                if remove_idx < 0:
                    return
            self.bus.queue_remove_at.emit(remove_idx)

    def _on_reorder_requested(self, src_play_orig, dest_play, src_id, anchor_id):
        """Commit a drag-reorder to the queue.

        In PLAY-order display the view's play_index values are already the
        real play-order indices — pass them straight through (identical to
        the pre-fix behaviour, incl. its exact-index duplicate handling).

        In SOURCE-order display (a pristine shuffled album/playlist), the
        play_index values are SOURCE indices, so feeding them to
        ``QueueManager.move_item`` (which treats them as play-order) moved
        the WRONG track. Re-map by Id — same fix as the context-menu
        remove path: ``src`` by its own Id, and the destination by the
        track the drop landed AFTER (``anchor_id``; empty = dropped at the
        very top → play-order 0). move_item pops src then inserts, so to
        land src right after the anchor we use the anchor's index (when src
        was above it and shifts down on the pop) or anchor+1 (when below).
        """
        if self._displayed_items_kind != "source":
            src, dest = src_play_orig, dest_play
        else:
            play = self.queue_mgr.queue  # play-order item dicts

            def _pidx(item_id: str) -> int:
                t = (item_id or "").lower()
                return next(
                    (i for i, it in enumerate(play) if (it.get("Id") or "").lower() == t),
                    -1,
                )

            src = _pidx(src_id)
            if src < 0:
                return
            if not anchor_id:
                dest = 0
            else:
                anchor = _pidx(anchor_id)
                if anchor < 0:
                    return
                dest = anchor if src < anchor else anchor + 1
        if src == dest:
            return
        self.bus.queue_move_item.emit(src, dest)
        # Drop-at-top = play that track now.
        if dest == 0:
            self.bus.track_jumped.emit(0)

    # ── Heart + Play CTAs ──────────────────────────────────────────────

    def _update_cta_visibility(self):
        # Play CTA only shows in preview mode (live mode has Play in the
        # bottom transport bar). Heart is a hover overlay on the cover
        # itself — it manages its own visibility, no setVisible needed.
        in_preview = bool(self._preview_id)
        was_visible = self._play_cta.isVisible()
        self._play_cta.setVisible(in_preview)
        # First transition from "live → preview" auto-focuses the Play
        # button so a keyboard user who arrived here via Enter on an
        # album tile can tap Enter once more to start playback. Skip
        # if focus is already on a meaningful target (e.g. the track
        # list — user tabbed past the Play CTA on purpose) to avoid
        # yanking focus away.
        if in_preview and not was_visible:
            from PySide6.QtWidgets import QApplication

            focused = QApplication.focusWidget()
            if focused is None or not self.isAncestorOf(focused):
                self._play_cta.setFocus()

        # Download CTA — preview mode only (it acts on the previewed
        # album/playlist). Seed its state from the index; an in-flight
        # download corrects itself on the next download_progress tick.
        self._download_cta.set_enabled(in_preview)
        if in_preview:
            try:
                from jellytoast import offline

                downloaded = offline.is_downloaded(self._preview_id)
            except Exception:
                downloaded = False
            # Don't stomp a live "downloading" arc with a stale "idle".
            if self._download_cta.state() not in ("downloading", "pending"):
                self._download_cta.set_state("complete" if downloaded else "idle")

    def _on_download_cta(self):
        """Download / remove / cancel the previewed album. The action
        is read off the button's current state."""
        item_id = self._preview_id
        if not item_id:
            return
        from jellytoast import offline

        state = self._download_cta.state()
        if state == "complete":
            # Mirror the library grid's confirm for a cascade removal.
            from jellytoast.frosted_dialog import frosted_confirm

            name = self._preview_meta.get("Name") or "this album"
            if not frosted_confirm(
                self,
                "Remove download",
                f"Remove the downloaded copy of “{name}”?",
                confirm_text="Remove",
                destructive=True,
            ):
                return
            offline.remove(item_id)
            self._download_cta.set_state("idle")
        elif state in ("downloading", "pending"):
            # Click during a download cancels it (remove reaps the
            # partial node graph).
            offline.remove(item_id)
            self._download_cta.set_state("idle")
        else:  # idle / failed → start a download
            meta = dict(self._preview_meta or {})
            meta.setdefault("Id", item_id)
            offline.download(meta)
            self._download_cta.set_state("pending")

    @Slot(str, str, float)
    def _on_download_progress(self, item_id: str, state: str, fraction: float):
        """download_progress bus hook — only the previewed item drives
        the CTA. 'removed' falls back to idle."""
        if not self._preview_id or item_id != self._preview_id:
            return
        from jellytoast.offline import DownloadState as _DS

        # The CTA widget keeps its own UI-only "idle" state, distinct from
        # the DownloadState lifecycle; a "removed" lifecycle event maps to it.
        self._download_cta.set_state("idle" if state == _DS.REMOVED else state, fraction)

    def _on_play_cta_key(self, e):
        """Down arrow on the Play CTA hops focus into the track list,
        seeding its keyboard cursor on row 0 (or the active track if
        already playing). The keyboard parity for the visual layout:
        Play sits above the rows, so Down naturally walks into the
        first row from there.

        Enter/Return activates the button — a plain QPushButton only
        fires on Space (or Enter as a dialog default button), so
        without this an Enter press on the focused Play CTA does
        nothing. The keyboard user arrives here via Enter on an album
        tile and expects a second Enter to start the album."""
        if e.key() == Qt.Key.Key_Down and not e.modifiers():
            if self._list_container is not None:
                self._list_container.setFocus()
                e.accept()
                return
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not e.modifiers():
            self._play_cta.animateClick()
            e.accept()
            return
        QPushButton.keyPressEvent(self._play_cta, e)

    def _on_play_preview(self):
        if not self._preview_id or not self._preview_tracks:
            return
        # Snapshot before clearing — we drop preview state *before*
        # emitting queue_play_now so the synchronously-fired
        # playback_started / queue_changed handlers see live mode and
        # refresh the page (kicker, active-track highlight, lyrics).
        tracks = list(self._preview_tracks)
        ctx = QueueContext(
            kind=self._preview_kind,
            source_id=self._preview_id,
            source_label=self._preview_meta.get("Name", ""),
        )
        self._preview_id = ""
        self._preview_meta = {}
        self._preview_tracks = []
        self._update_cta_visibility()
        self.preview_changed.emit(False)
        self.bus.queue_play_now.emit(tracks, 0, ctx)

    def _on_favorite_cta(self):
        # Favorite the current source item (album/playlist), not the
        # active track — the bottom transport bar already favorites the
        # track. This CTA is for the broader collection.
        #
        # In preview mode the authoritative state is _preview_meta's
        # IsFavorite (the freshly-fetched album/playlist meta). In LIVE
        # mode _preview_meta is {}, so we read+write self._live_source_fav
        # — the page's source-collection fav state, kept current by the
        # favorite_toggled bus signal (incl. external clients).
        if self._preview_id:
            target_id = self._preview_id
            cur_fav = bool((self._preview_meta.get("UserData") or {}).get("IsFavorite", False))
        else:
            target_id = self.queue_mgr.context.source_id
            cur_fav = self._live_source_fav
        if not target_id:
            return
        new_state = not cur_fav
        run_async(self.api.toggle_favorite, target_id, new_state)
        # Persist the new state to the authoritative source so a
        # subsequent read flips correctly.
        if self._preview_id:
            self._preview_meta.setdefault("UserData", {})["IsFavorite"] = new_state
        else:
            self._live_source_fav = new_state
        # Broadcast so the other surfaces (and our own
        # _on_favorite_toggled) reflect the change — mirrors the
        # transport bar's _toggle_favorite contract.
        self.bus.favorite_toggled.emit(target_id, new_state)
        self._fav_cta.setIcon(
            accent_icon("favorite_filled") if new_state else icon("favorite_outline")
        )

    # ── Preview mode ───────────────────────────────────────────────────

    PREVIEW_CACHE_NAME = "preview"

    def load_preview(self, item_id: str, kind: str = "album"):
        """Show this album/playlist's tracks in preview mode without
        installing as the active queue. Click Play / a track to install.
        `kind` is "album" or "playlist" — controls the fetch endpoint
        and the QueueKind installed when preview becomes live.

        Two-phase: render from disk cache instantly if we've shown
        this item before, then refresh from the server in the
        background. New albums (no cache) still hit the network on
        first open, but every subsequent open of an already-seen
        album is instant — even across app launches."""
        if not item_id:
            return
        new_kind = QueueKind.PLAYLIST if kind == "playlist" else QueueKind.ALBUM
        if item_id == self._preview_id and new_kind == self._preview_kind and self._preview_meta:
            return  # already loaded
        # Preview target changed — drop the stale cover immediately so
        # the user doesn't see the previously-playing album's artwork
        # under the new album's "Loading…" text. The new cover lands
        # via _on_preview_meta_loaded → load_image_async once the
        # meta fetch resolves.
        self._cover.clear()
        self._cover_orig = None
        self._preview_id = item_id
        self._preview_kind = new_kind
        self._preview_meta = {}
        self._preview_tracks = []
        # Stop any active-track lyric chase while previewing.
        self._user_off_live = False
        self._update_lyrics_visibility()
        self._update_live_btn_visibility()
        self._update_cta_visibility()
        self.preview_changed.emit(True)
        # Try the disk cache for this album/playlist before showing
        # placeholders. A cache hit means the user has previewed this
        # item in a previous session — render meta + tracks
        # immediately and let the background refresh confirm.
        scope = {"kind": kind, "item_id": item_id}
        cached = disk_cache.load(self.PREVIEW_CACHE_NAME, scope)
        if cached and cached.get("meta") and cached.get("tracks") is not None:
            # Render the cached snapshot synchronously so the user
            # sees the album immediately. The fresh fetches still
            # fire below — server data wins on conflict.
            self._on_preview_meta_loaded(item_id, cached["meta"])
            self._on_preview_tracks_loaded(item_id, cached["tracks"])
        else:
            # Cold path — placeholders while we wait on the network.
            self._title.setText("Loading…")
            self._subtitle.setText("")
            self._refresh_track_list()
            self._refresh_meta_line()
        # Async fetches dispatch back to the GUI thread via signals.
        # Different endpoint per kind — playlists pull AlbumId per track
        # (cover art resolves per track, not per playlist).
        fetch_tracks = (
            self.api.get_playlist_items
            if new_kind == QueueKind.PLAYLIST
            else self.api.get_album_tracks
        )
        run_async(
            self.api.get_item,
            item_id,
            on_result=lambda meta, iid=item_id: self._preview_meta_loaded.emit(iid, meta),
            on_error=lambda _e, iid=item_id: self._preview_meta_loaded.emit(iid, None),
        )
        run_async(
            fetch_tracks,
            item_id,
            on_result=lambda tracks, iid=item_id: self._preview_tracks_loaded.emit(iid, tracks),
            on_error=lambda _e, iid=item_id: self._preview_tracks_loaded.emit(iid, []),
        )

    def keyPressEvent(self, e):
        """Esc on the NP page is a two-stage dismiss: in preview
        mode it backs out to the live queue; on the live view it
        dismisses the whole page back to the previous surface
        (the host wires dismiss_requested to navigate back)."""
        if e.key() == Qt.Key.Key_Escape and not e.modifiers():
            if self._preview_id:
                self.clear_preview()
                e.accept()
                return
            self.dismiss_requested.emit()
            e.accept()
            return
        super().keyPressEvent(e)

    def clear_preview(self):
        """Drop preview state — show the live queue + active track."""
        if not self._preview_id:
            return
        self._preview_id = ""
        self._preview_meta = {}
        self._preview_tracks = []
        self._refresh_now_playing(get_now_playing())
        self._refresh_track_list()
        self._refresh_meta_line()
        self._update_lyrics_visibility()
        self._update_cta_visibility()
        # Returning to live with no context change: re-stamp the heart
        # from the live-source fav state (preview may have left it
        # showing the previewed item's glyph).
        self._refresh_fav_cta_icon()
        self.preview_changed.emit(False)

    @Slot(str, object)
    def _on_preview_meta_loaded(self, item_id: str, meta: Optional[Dict]):
        # Stale callback if user has moved on to a different preview.
        if item_id != self._preview_id:
            return
        if meta is None:
            # Only show the "Couldn't load" placeholder if we don't
            # already have something on screen — a cached render
            # that's followed by a network failure should keep the
            # cached snapshot up.
            if not self._preview_meta:
                self._title.setText("Couldn't load")
            return
        self._preview_meta = meta
        # Render preview header — title is the album/playlist name,
        # subtitle is the artist (or curator for playlists).
        self._title.setText(meta.get("Name") or "Unknown")
        # AlbumArtists is a list of {Id, Name} dicts (both providers) — a
        # bare ", ".join would raise TypeError on dicts; extract Name.
        artist = meta.get("AlbumArtist") or ", ".join(
            a.get("Name", "")
            for a in (meta.get("AlbumArtists") or [])
            if isinstance(a, dict) and a.get("Name")
        )
        self._subtitle.setText(artist)
        # Cover load via the standard image URL helper. Match the
        # live-mode load size + DPR-scaling so this preview shares the
        # cache slot the live now-playing flow would populate for the
        # same album.
        dpr = dpr_bucket(screen_dpr(self))
        target_phys = max(self.COVER_SIZE, int(round(self.COVER_SIZE * dpr)))
        radius_phys = int(round(12 * dpr))
        server_px = max(512, target_phys)
        cover_url = self.api.get_image_url(item_id, "Primary", server_px)
        if cover_url:
            load_image_async(
                f"{item_id}|nppage",
                cover_url,
                target_phys,
                target_phys,
                self._on_cover_loaded,
                rounded_radius=radius_phys,
                on_error=lambda: None,
            )
        # Reflect favorited state in the heart icon.
        cur_fav = bool((meta.get("UserData") or {}).get("IsFavorite", False))
        self._fav_cta.setIcon(
            accent_icon("favorite_filled") if cur_fav else icon("favorite_outline")
        )
        self._maybe_save_preview_cache()

    @Slot(str, object)
    def _on_preview_tracks_loaded(self, item_id: str, tracks: Optional[List[Dict]]):
        if item_id != self._preview_id:
            return
        self._preview_tracks = tracks or []
        self._refresh_track_list()
        self._refresh_meta_line()
        self._maybe_save_preview_cache()

    def _refresh_meta_line(self):
        """Update the "12 tracks · 47 min" line under the subtitle.
        Three radio-mode variants + preview album-header + hidden:
          • playing radio → accent ● LIVE · station
          • paused radio  → dim PAUSED · station
          • stopped radio → dim {station} (queued but inactive)
          • preview mode w/ tracks → uppercase album-header style
          • otherwise → hidden"""
        # Radio takes priority — even in live mode it owns the meta line
        # so the user always sees what station's playing while ICY
        # metadata cycles through tracks above it.
        if getattr(self, "_is_radio", False) and not self._preview_id:
            from jellytoast import radio_state as _radio_state

            cur = _radio_state.current()
            station = (self._radio_station_name or "").strip()
            if cur is not None and cur.is_live:
                text = f"●  LIVE  ·  {station}" if station else "●  LIVE"
                color = ACCENT
            elif cur is not None and cur.playback_state == "paused":
                text = f"PAUSED  ·  {station}" if station else "PAUSED"
                color = ink_alpha(0.42)
            else:
                text = station
                color = ink_alpha(0.42)
            self._meta_line.setText(text)
            self._meta_line.setStyleSheet(
                f"color: {color}; letter-spacing: 1px; font-weight: 700;"
            )
            self._meta_line.setVisible(True)
            return
        # Restore preview-mode styling (in case we just exited radio).
        self._meta_line.setStyleSheet(
            f"color: {ink_alpha(0.42)}; letter-spacing: 0.6px;"
        )
        tracks = self._preview_tracks
        if not (self._preview_id and tracks):
            self._meta_line.setVisible(False)
            return
        count = len(tracks)
        total_ticks = sum(int(t.get("RunTimeTicks") or 0) for t in tracks)
        # Compose like Apple Music's album header: "12 SONGS · 47 MIN".
        # Use SONG / TRACKS depending on kind for accuracy.
        unit = "song" if self._preview_kind != QueueKind.PLAYLIST else "track"
        count_part = f"{count} {unit}{'s' if count != 1 else ''}"
        if total_ticks <= 0:
            self._meta_line.setText(count_part.upper())
        else:
            self._meta_line.setText(f"{count_part}  ·  {self._format_runtime(total_ticks)}".upper())
        self._meta_line.setVisible(True)

    @staticmethod
    def _format_runtime(ticks: int) -> str:
        """Album-runtime formatter: short and human. Sub-hour reads as
        minutes ("47 min"); hour-plus reads as hours+minutes ("1 hr
        23 min"). Matches the convention iTunes / Apple Music use in
        their album headers."""
        total_seconds = ticks // 10_000_000
        hours, rem = divmod(total_seconds, 3600)
        minutes = rem // 60
        if hours <= 0:
            # Round up to 1 min for any non-zero runtime so a
            # 12-second sample album doesn't read as "0 min".
            return f"{max(1, minutes)} min"
        if minutes == 0:
            return f"{hours} hr"
        return f"{hours} hr {minutes} min"

    def _maybe_save_preview_cache(self):
        """Persist the (meta, tracks) pair once both halves have landed
        from the server. Called from both _on_preview_*_loaded handlers
        — whichever fires second triggers the save. Subsequent opens
        of the same item across app launches render from this snapshot
        instantly while the fresh fetch verifies in the background."""
        if not (self._preview_id and self._preview_meta and self._preview_tracks):
            return
        kind = "playlist" if self._preview_kind == QueueKind.PLAYLIST else "album"
        scope = {"kind": kind, "item_id": self._preview_id}
        disk_cache.save(
            self.PREVIEW_CACHE_NAME,
            scope,
            {
                "meta": self._preview_meta,
                "tracks": self._preview_tracks,
            },
        )
