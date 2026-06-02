"""
Native album library grid — Phase 4 of the native-UI pivot.

Replaces JF Web's Music → Albums browse view with a PySide6 grid of
album tiles. Each tile clicks two ways:

- Edge / cover / text → browse_requested(album_id): host swaps to
  NowPlayingPage in preview mode (current track keeps playing).
- Centered hover-revealed play overlay → play_requested(album_id):
  host installs the album as the live queue and starts from track 0.

The two-click split mirrors how Spotify / Apple Music / Plexamp tile
grids behave and matches the same pattern we use elsewhere — the
tile's "intent" is browse; play is the explicit secondary action.

Why this exists: replacing JF Web for browse views removes the entire
brittle bridge layer (URL interception, intent_detected, silence_jfweb,
queue_state attribution, AlbumId-uniformity heuristics, JS click
capture). A native tile's play button calls bus.queue_play_now
directly with the right QueueContext — no round-trip, no inference.
"""

from typing import Dict, List

from PySide6.QtCore import (
    QAbstractListModel,
    QEasingCurve,
    QEvent,
    QModelIndex,
    QPoint,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    Signal,
    Slot,
)
from PySide6.QtGui import (
    QColor,
    QFont,
    QFontMetrics,
    QPainter,
    QPainterPath,
    QPalette,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QGraphicsOpacityEffect,
    QHBoxLayout,
    QLabel,
    QListView,
    QPushButton,
    QSizePolicy,
    QStackedWidget,
    QStyle,
    QStyledItemDelegate,
    QVBoxLayout,
    QWidget,
)

from modules import disk_cache

# Hot-path import: paint loops read TEXT / ACCENT via this module ref
# so they pick up live-theme / live-accent changes without paying
# Python's full `from modules.ui_helpers import …` machinery on every
# tile paint. Attribute access (``_u.TEXT``) is a single sys.modules
# lookup; ``from X import Y`` inside a paint runs the IMPORT_NAME +
# IMPORT_FROM opcodes every call.
from modules import ui_helpers as _u
from modules.design_tokens import (
    SPACE_LG,
    SPACE_SM,
    SPACE_XL,
    TYPE_BODY,
    TYPE_CAPTION,
    type_qss,
)
from modules.icons import icon
from modules.library_paginator import _PaginatorMixin
from modules.providers import get_provider
from modules.sort_utils import (
    article_stripped_key,
    first_letter,
)
from modules.theme import ink_rgb
from modules.ui_helpers import (
    TEXT,
    TEXT_DIM,
    TEXT_FAINT,
    EmptyState,
    ink_alpha,
    install_autofade_scrollbars,
    load_image_async,
    overlay_disc_colors,
    overlay_disc_qcolor,
    scale_pixmap_for_dpr,
    screen_dpr,
)

# ── Eliding label (local copy — small enough not to share yet) ──────────


class _ElidingLabel(QLabel):
    """QLabel that elides overflow with '…' instead of growing the parent."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._full = text
        super().setText(text)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def setText(self, text: str):
        self._full = text or ""
        self._elide()

    def text(self) -> str:
        return self._full

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._elide()

    def _elide(self):
        fm = self.fontMetrics()
        avail = max(0, self.width() - 4)
        super().setText(fm.elidedText(self._full, Qt.TextElideMode.ElideRight, avail))

    def minimumSizeHint(self):
        return QSize(0, super().minimumSizeHint().height())

    def sizeHint(self):
        return self.minimumSizeHint()


class _ClickableElidingLabel(_ElidingLabel):
    """Eliding label that emits `clicked` on left-click and consumes
    the event so it doesn't bubble to the parent tile (which would
    otherwise fire its own browse-the-album signal)."""

    clicked = Signal()

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            e.accept()
            return
        super().mousePressEvent(e)


class _ClickableLabel(QLabel):
    """Plain QLabel that emits `clicked` on left-click. Used for the
    year line in album tiles — same swallow-the-event pattern as the
    eliding variant so the click doesn't bubble to the album-browse
    handler."""

    clicked = Signal()

    def __init__(self, text: str = "", parent=None):
        super().__init__(text, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            e.accept()
            return
        super().mousePressEvent(e)


# ── Tile ────────────────────────────────────────────────────────────────


class LibraryTile(QFrame):
    """One library item in the grid (album or playlist). Cover + title
    + subtitle; hover reveals a centered play button overlay that's a
    child of the cover container (so it floats above the artwork
    without disturbing layout). `kind` controls the subtitle field
    — album shows artist, playlist shows track count."""

    play_requested = Signal(str)  # item_id
    browse_requested = Signal(str)  # item_id
    # Album tiles only — clicking the artist subtitle routes to the
    # artist page, clicking the year routes to a year-filtered grid.
    # Empty payload means "no actionable target" (no artist id / no
    # year metadata) and the host should ignore it.
    artist_browse_requested = Signal(str)  # artist_id
    year_browse_requested = Signal(int)  # year

    COVER_SIZE = 180
    OVERLAY_SIZE = 56

    # Class-level flag toggled by the parent LibraryGrid while the scroll
    # bar is actively moving. When True, reveal() snaps to full opacity
    # instead of running the 180ms QGraphicsOpacityEffect fade — animating
    # many tiles concurrently through QGraphicsEffect produces half-
    # painted frames and a brief white flash on Wayland during scroll.
    # See feedback_qgraphicseffect_scroll memory for the underlying issue.
    SCROLL_BUSY: bool = False

    def __init__(
        self,
        item: Dict,
        kind: str = "album",
        show_subtitle: bool = True,
        show_year: bool = True,
        parent=None,
    ):
        super().__init__(parent)
        self._item = item
        self._kind = kind
        self._item_id = item.get("Id", "")
        self._show_subtitle = show_subtitle
        self._show_year = show_year
        # Artists hide the play overlay — "play an artist" has no
        # canonical meaning (their newest album? all tracks shuffled?).
        # The whole-tile click opens the artist page where the user
        # picks a specific album to play.
        self._show_play_overlay = kind != "artist"
        self.setObjectName("libraryTile")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # StrongFocus lets the tile receive keyboard focus via Tab and
        # programmatic setFocus() (e.g. SearchView's "down arrow → first
        # result"). The :focus stylesheet rule below paints a subtle
        # backdrop so users can see which tile is focused.
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedWidth(self.COVER_SIZE)
        self.setStyleSheet(f"""
            QFrame#libraryTile {{ background: transparent; border: none; }}
            QFrame#libraryTile:focus {{ background: {ink_alpha(0.06)}; }}
            QFrame#libraryTile QLabel {{ background: transparent; }}
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACE_SM)

        # Cover: a QFrame as a fixed-size container so we can position
        # the play overlay absolutely inside it. The QLabel inside paints
        # the artwork; the QPushButton sits on top.
        self._cover_box = QFrame(self)
        self._cover_box.setFixedSize(self.COVER_SIZE, self.COVER_SIZE)
        self._cover_box.setStyleSheet(f"""
            QFrame {{
                background: {ink_alpha(0.04)};
                border-radius: 8px;
            }}
        """)

        self._cover = QLabel(self._cover_box)
        self._cover.setFixedSize(self.COVER_SIZE, self.COVER_SIZE)
        self._cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cover.setStyleSheet("background: transparent;")

        self._play_overlay = QPushButton(self._cover_box)
        self._play_overlay.setIcon(icon("play"))
        self._play_overlay.setIconSize(QSize(28, 28))
        self._play_overlay.setFixedSize(self.OVERLAY_SIZE, self.OVERLAY_SIZE)
        self._play_overlay.setCursor(Qt.CursorShape.PointingHandCursor)
        self._play_overlay.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        _ov_normal, _ov_hover = overlay_disc_colors()
        # A single translucent disc, no rim — matches the tile's corner
        # buttons. The play glyph is theme-tinted via icon("play")
        # (black on a light theme, near-white on dark).
        self._play_overlay.setStyleSheet(f"""
            QPushButton {{
                background: {_ov_normal};
                border: none;
                border-radius: 28px;
            }}
            QPushButton:hover {{ background: {_ov_hover}; }}
            QPushButton:pressed {{ background: {_ov_hover}; }}
        """)
        # Center the overlay in the cover.
        self._play_overlay.move(
            (self.COVER_SIZE - self.OVERLAY_SIZE) // 2,
            (self.COVER_SIZE - self.OVERLAY_SIZE) // 2,
        )
        self._play_overlay.clicked.connect(self._on_play_clicked)
        self._play_overlay.hide()

        layout.addWidget(self._cover_box)

        # Title — bold body, single line, centered, eliding.
        # Parent at construction so the label never gets allocated
        # a top-level Wayland surface before addWidget reparents it
        # — the brief mapping otherwise surfaces as flashes of album
        # titles in the middle of the screen during chunked rendering.
        self._title = _ElidingLabel(item.get("Name", "Unknown"), parent=self)
        self._title.setStyleSheet(f"color: {TEXT}; {type_qss(TYPE_BODY)} font-weight: 600;")
        self._title.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        layout.addWidget(self._title)

        # Year line — albums only, sits between title and subtitle.
        # ProductionYear is a string like "2010"; falls back to the
        # PremiereDate's year prefix. Hidden when neither is present
        # so the tile collapses cleanly for unscored items.
        # Clickable for album tiles when a year exists — emits
        # year_browse_requested(year) so the host can swap to a
        # year-filtered grid.
        if self._kind == "album" and self._show_year:
            year_text = self._compute_year()
            year_int = int(year_text) if year_text.isdigit() else 0
            self._year = (
                _ClickableLabel(year_text, parent=self)
                if year_int
                else QLabel(year_text, parent=self)
            )
            self._year.setStyleSheet(f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)}")
            self._year.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            self._year.setVisible(bool(year_text))
            if year_int:
                self._year.clicked.connect(lambda y=year_int: self.year_browse_requested.emit(y))
            layout.addWidget(self._year)

        # Subtitle — kind-dependent. Albums show the artist; playlists
        # show track count; artists show first genre. Same caption
        # styling so the tile reads consistently across kinds. Hidden
        # via show_subtitle=False when the surrounding context already
        # makes the line redundant (e.g. all albums on an ArtistPage
        # share the same artist).
        # For album tiles with a known artist id, the subtitle is
        # clickable and routes to the artist page instead of opening
        # the album.
        artist_id = self._artist_id_for_album() if self._kind == "album" else ""
        if artist_id:
            self._subtitle = _ClickableElidingLabel(
                self._compute_subtitle(),
                parent=self,
            )
            self._subtitle.clicked.connect(
                lambda aid=artist_id: self.artist_browse_requested.emit(aid)
            )
        else:
            self._subtitle = _ElidingLabel(
                self._compute_subtitle(),
                parent=self,
            )
        self._subtitle.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}")
        self._subtitle.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        self._subtitle.setVisible(self._show_subtitle)
        layout.addWidget(self._subtitle)

        layout.addStretch(0)

        # Born hidden — the chunked grid render parents tiles to the
        # container *before* placing them in the QGridLayout, and a
        # parented-but-unplaced child is briefly mapped at default
        # position by Wayland (showing as a stack of mini-windows
        # cascading from the top-left). Callers must call show()
        # after they place the tile in their layout — addWidget /
        # insertWidget alone won't unhide it.
        self.setVisible(False)
        # Draw nothing until the cover lands. Showing the placeholder
        # rectangle + text first reads as a different view (genre
        # tiles) instead of "albums still loading"; making the tile
        # fully transparent until set_cover replaces it with the real
        # artwork keeps the boot read clean — blank space, then the
        # final view, no intermediate skeleton state.
        # Layout space is preserved (the widget is still visible for
        # geometry purposes) — only the painted output is suppressed.
        self._opacity = QGraphicsOpacityEffect(self)
        self._opacity.setOpacity(0.0)
        self.setGraphicsEffect(self._opacity)
        self._revealed = False
        # Set by LibraryGrid._clear_tiles before deleteLater so an
        # in-flight cover-load callback that lands after teardown
        # can early-return instead of touching the dead C++ side.
        self._dead = False

    def reveal(self):
        """Make the tile visible (cover + text). Called from set_cover
        when the artwork lands, or from the grid as a fallback for
        items that have no cover URL at all. Animates the opacity so
        tiles fade in over ~180ms instead of binary-popping — softens
        the staggered prefetch landing pattern, which would otherwise
        look like a flickering grid as covers fire ~30ms apart.

        Drops the QGraphicsOpacityEffect at animation end so subsequent
        scrolls go through Qt's fast paint path. Leaving the effect
        attached forces every partial-repaint during scroll to redraw
        the whole tile through the effect chain, which on Wayland +
        QScrollArea consistently leaves tiles half-painted until the
        scroll stops. Once the tile is fully opaque the effect adds
        nothing but cost, so we remove it."""
        if self._dead or self._revealed:
            return
        self._revealed = True
        # Snap when the parent grid is mid-scroll — animating dozens of
        # tiles through a graphics effect mid-scroll is what produces
        # the white-flash artifact on Wayland.
        if LibraryTile.SCROLL_BUSY:
            if self._opacity is not None:
                self._opacity.setOpacity(1.0)
            self._drop_opacity_effect()
            return
        anim = QPropertyAnimation(self._opacity, b"opacity")
        anim.setDuration(180)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        anim.finished.connect(self._drop_opacity_effect)
        # Pin the wrapper to the tile so PySide doesn't GC the Python
        # half mid-animation. Replaced on each call (guarded by
        # _revealed) so no leak.
        self._reveal_anim = anim
        anim.start()

    def _drop_opacity_effect(self):
        # Detach the opacity effect once the fade-in finishes so
        # subsequent scrolls don't re-render the tile through the
        # effect chain (see `reveal` for why that matters). Idempotent
        # and guarded so a second `reveal()` (which can't happen given
        # `_revealed`, but defensive) doesn't crash.
        if self._opacity is not None:
            self.setGraphicsEffect(None)
            self._opacity = None

    def _compute_year(self) -> str:
        # Only meaningful for album items. ProductionYear is the
        # canonical field; PremiereDate (ISO 8601) is the fallback.
        y = self._item.get("ProductionYear")
        if y:
            return str(y)
        pd = (self._item.get("PremiereDate") or "").strip()
        return pd[:4] if pd[:4].isdigit() else ""

    def _artist_id_for_album(self) -> str:
        """First album-artist id from the album item's metadata, if
        any. Both Jellyfin and SubsonicProvider's _adapt_album emit
        an `AlbumArtists` list of {Id, Name}; ArtistItems is a
        Jellyfin-only sibling field. Empty string when the album
        has no resolvable primary artist (rare but possible)."""
        for field in ("AlbumArtists", "ArtistItems"):
            arr = self._item.get(field) or []
            if arr and isinstance(arr, list):
                first = arr[0] or {}
                aid = first.get("Id") if isinstance(first, dict) else ""
                if aid:
                    return aid
        return ""

    def _compute_subtitle(self) -> str:
        if self._kind == "playlist":
            count = self._item.get("ChildCount") or 0
            return f"{count} tracks" if count != 1 else "1 track"
        if self._kind == "artist":
            # Genres array is the most useful one-line meta; drop
            # leading-empty entries Jellyfin sometimes returns.
            genres = [g for g in (self._item.get("Genres") or []) if g]
            return genres[0] if genres else ""
        # Default (album): artist line
        return (
            self._item.get("AlbumArtist")
            or ", ".join(self._item.get("AlbumArtists", []) or [])
            or ""
        )

    # ── Cover loader callback ──────────────────────────────────────────

    @Slot(object)
    def set_cover(self, pix: QPixmap):
        if self._dead or pix is None or pix.isNull():
            return
        # scale_pixmap_for_dpr scales to physical pixels (COVER_SIZE ×
        # dpr) and tags with setDevicePixelRatio so Qt paints at
        # COVER_SIZE *logical* points using the full-resolution texture.
        # Without it, on a 2× display the painter would downscale a
        # COVER_SIZE-pixel pixmap to COVER_SIZE *physical* pixels at
        # paint time — visibly soft.
        self._cover.setPixmap(scale_pixmap_for_dpr(pix, self.COVER_SIZE))
        # Cover landed → reveal the tile (cover + text become opaque
        # together so the user never sees the skeleton state).
        self.reveal()

    # ── Hover → reveal play overlay ────────────────────────────────────

    def enterEvent(self, e):
        super().enterEvent(e)
        if self._show_play_overlay:
            self._play_overlay.show()
            self._play_overlay.raise_()
        # Pre-warm album/playlist detail so the click is instant.
        # Browser-style hover preload — by the time the user actually
        # clicks, get_item + get_album_tracks have populated the api's
        # _meta_cache and load_preview just hits cache.
        self.prewarm_detail()
        # Same idea for the now-playing-bar cover slot — most clicks
        # pass through hover first, so by click-time the bar's L1/L2
        # is warm and `_on_started` resolves without a network hop.
        self.prewarm_npbar_cover()

    def leaveEvent(self, e):
        super().leaveEvent(e)
        self._play_overlay.hide()

    def prewarm_detail(self):
        """Background-fire the get_item + get_tracks fetches that
        load_preview will need, so a click on this tile resolves
        from the api's in-memory cache instead of waiting on the
        network. No-op for artists (they route to ArtistPage,
        which has its own preload path) and idempotent — repeat
        calls are short-circuited by the dedupe flag."""
        if self._kind not in ("album", "playlist"):
            return
        if getattr(self, "_prewarm_done", False):
            return
        if not self._item_id:
            return
        self._prewarm_done = True
        # Local import: async_io stays out of the module-load graph.
        # get_provider is at module top; no re-import needed here.
        from modules.async_io import run_async

        api = get_provider()
        fetch_tracks = api.get_playlist_items if self._kind == "playlist" else api.get_album_tracks
        run_async(
            api.get_item,
            self._item_id,
            on_result=lambda _r: None,
            on_error=lambda _e: None,
        )
        run_async(
            fetch_tracks,
            self._item_id,
            on_result=lambda _r: None,
            on_error=lambda _e: None,
        )

    def prewarm_npbar_cover(self):
        """Hover-prewarm the now-playing-bar's cache slot for this
        tile's image. For an album tile the tile id IS the AlbumId,
        which is also what the bar uses as its L2 semantic key — so
        the prewarm hits the same slot the bar's `_on_started` will
        request. For artists / playlists we skip: the bar's image_id
        on play-from-tile is the first track's AlbumId, which we'd
        need to fetch the track list to know."""
        if self._kind != "album":
            return
        if not self._item_id:
            return
        if getattr(self, "_cover_prewarm_done", False):
            return
        self._cover_prewarm_done = True
        api = get_provider()
        url = api.get_image_url(self._item_id, "Primary", 256)
        if not url:
            return
        # Discard callback — we're just populating the cache.
        load_image_async(
            f"{self._item_id}|npbar",
            url,
            256,
            256,
            lambda _pix: None,
            rounded_radius=0,
            on_error=lambda: None,
            priority="high",
        )

    # ── Click → browse (play overlay handles its own click) ────────────

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.browse_requested.emit(self._item_id)
        super().mousePressEvent(e)

    def keyPressEvent(self, e):
        # Enter on a focused tile = same primary action as a click —
        # browse the album/playlist/artist page. Mirrors mousePress.
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.browse_requested.emit(self._item_id)
            return
        super().keyPressEvent(e)

    @Slot()
    def _on_play_clicked(self):
        # Play button consumes the click so it doesn't bubble to the
        # tile's mousePressEvent (which would also emit browse_requested).
        self.play_requested.emit(self._item_id)


# ── Alphabet index ──────────────────────────────────────────────────────


class _AlphabetIndex(QWidget):
    """Vertical A–Z strip on the right edge of the grid. Letters are
    subtle by default; the current letter (first character of the
    top-most visible album) renders bright. Clicking a letter emits
    jump_requested(letter) — the grid scrolls the first matching tile
    into view.

    Mirrors the iOS Music app / Jellyfin Web pattern. Inert until the
    grid wires its scroll bar + jump handlers."""

    LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

    jump_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(20)
        self.setStyleSheet("background: transparent;")
        self._current = ""
        self._buttons = {}

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, SPACE_LG, 4, SPACE_LG)
        layout.setSpacing(0)
        for ch in self.LETTERS:
            btn = QPushButton(ch)
            btn.setFlat(True)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.setStyleSheet(self._btn_style(active=False))
            # stretch=1 so the 26 letters distribute evenly across the
            # available height — keeps the strip readable on tall and
            # short windows alike, no fixed per-letter height needed.
            btn.clicked.connect(lambda _checked=False, c=ch: self.jump_requested.emit(c))
            layout.addWidget(btn, 1)
            self._buttons[ch] = btn

    @staticmethod
    def _btn_style(active: bool) -> str:
        if active:
            return (
                f"QPushButton {{ background: transparent; color: {TEXT}; "
                f"border: none; padding: 0; font-size: 9px; font-weight: 700; }}"
                f"QPushButton:hover {{ color: {TEXT}; }}"
            )
        return (
            f"QPushButton {{ background: transparent; color: {ink_alpha(0.30)}; "
            f"border: none; padding: 0; font-size: 9px; }}"
            f"QPushButton:hover {{ color: {TEXT}; }}"
        )

    def set_current_letter(self, letter: str):
        letter = (letter or "").upper()
        if letter == self._current:
            return
        if self._current and self._current in self._buttons:
            self._buttons[self._current].setStyleSheet(self._btn_style(active=False))
        self._current = letter
        if letter and letter in self._buttons:
            self._buttons[letter].setStyleSheet(self._btn_style(active=True))


# ── Model ────────────────────────────────────────────────────────────────


class _LibraryItemsModel(QAbstractListModel):
    """Items + sparse cover cache for the album/playlist/artist grid.
    Delegates paint from four custom roles — ItemRole returns the source
    dict, CoverRole returns the loaded pixmap (None until it lands),
    DownloadedRole returns True for items whose id is in the downloads
    index in state ``complete``, and IsFavoriteRole reads the item's
    ``UserData.IsFavorite``. Single-shot ``set_items`` replaces the
    chunked widget-build the old implementation needed; ``append_items``
    powers paginated tails."""

    ItemRole = Qt.ItemDataRole.UserRole + 1
    CoverRole = Qt.ItemDataRole.UserRole + 2
    DownloadedRole = Qt.ItemDataRole.UserRole + 3
    IsFavoriteRole = Qt.ItemDataRole.UserRole + 4
    # Returns -1.0 when the item isn't downloading; 0.0..<1.0 when it
    # is. Used by _TileDelegate to swap the BL hover-button for an
    # always-visible determinate progress ring.
    DownloadFractionRole = Qt.ItemDataRole.UserRole + 5

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: List[Dict] = []
        self._covers: Dict[int, QPixmap] = {}
        # Item ids whose download is in state ``complete``. Seeded from
        # offline.downloaded_item_ids() on set/append and patched off
        # the bus's download_progress signal so badges flip live without
        # a per-paint DB hit.
        self._downloaded: set = set()
        # item_id → fraction in [0, 1) for items currently downloading
        # (either a single track or a cascade root). Mirrors the
        # ``"downloading"`` / ``"pending"`` events on the bus; entries
        # are dropped on ``complete`` / ``failed`` / ``removed``.
        self._progress: Dict[str, float] = {}
        # Bus subscription: the model lives on the GUI thread and
        # download_progress is emitted via Qt's queued connection, so
        # the slot fires on the GUI thread regardless of the emitter.
        try:
            from modules.player_state import PlayerBus

            bus = PlayerBus.get()
            bus.download_progress.connect(self._on_download_progress)
            # Mirror favorite flips emitted by the NP bar / mini player /
            # tile-corner heart so an item favorited anywhere repaints
            # everywhere it's showing.
            bus.favorite_toggled.connect(self._on_favorite_toggled)
        except Exception:
            pass

    def rowCount(self, parent=QModelIndex()):
        if parent.isValid():
            return 0
        return len(self._items)

    def data(self, index, role=Qt.ItemDataRole.DisplayRole):
        if not index.isValid():
            return None
        row = index.row()
        if not (0 <= row < len(self._items)):
            return None
        if role == self.ItemRole:
            return self._items[row]
        if role == self.CoverRole:
            return self._covers.get(row)
        if role == self.DownloadedRole:
            item_id = self._items[row].get("Id") or ""
            return bool(item_id) and item_id in self._downloaded
        if role == self.IsFavoriteRole:
            ud = self._items[row].get("UserData") or {}
            return bool(ud.get("IsFavorite", False))
        if role == self.DownloadFractionRole:
            item_id = self._items[row].get("Id") or ""
            if not item_id:
                return -1.0
            return float(self._progress.get(item_id, -1.0))
        return None

    def items(self) -> List[Dict]:
        return self._items

    def set_items(self, items: List[Dict]):
        self.beginResetModel()
        self._items = list(items)
        self._covers = {}
        self._reseed_downloaded()
        self.endResetModel()

    def append_items(self, new_items: List[Dict]):
        if not new_items:
            return
        first = len(self._items)
        last = first + len(new_items) - 1
        self.beginInsertRows(QModelIndex(), first, last)
        self._items.extend(new_items)
        self.endInsertRows()
        # Pull in any rows that flipped while paging was in flight.
        self._reseed_downloaded(emit=True)

    # ── Downloaded-badge cache ────────────────────────────────────────

    def _reseed_downloaded(self, emit: bool = False) -> None:
        """Re-read the full ``complete`` set from the offline index.
        Called on every set_items/append_items so a row's badge is
        accurate the moment it lays out. With ``emit=True`` also emits
        dataChanged for any row whose downloaded-state actually flipped,
        so paginated appends pick up changes that arrived mid-fetch."""
        try:
            from modules import offline

            new = offline.downloaded_item_ids()
        except Exception:
            new = set()
        if not emit:
            self._downloaded = new
            return
        flipped = new ^ self._downloaded
        self._downloaded = new
        if not flipped or not self._items:
            return
        for row, item in enumerate(self._items):
            if (item.get("Id") or "") in flipped:
                idx = self.index(row, 0)
                self.dataChanged.emit(idx, idx, [self.DownloadedRole])

    def _on_download_progress(self, item_id: str, state: str, fraction: float):
        """Bus slot — maintain the downloaded-id set and the in-flight
        progress map so tiles can paint a check (complete), a download
        icon (idle), or a determinate progress ring (in-flight)."""
        if not item_id:
            return
        from modules.offline import DownloadState as _DS

        roles: "list" = []
        if state in (_DS.DOWNLOADING, _DS.PENDING):
            self._progress[item_id] = max(0.0, min(0.999, float(fraction)))
            roles.append(self.DownloadFractionRole)
            # Mid-flight items are not "complete" yet — make sure the
            # downloaded-set doesn't shadow the ring (paint priority is
            # ring > check > download, but we still want a clean state).
            if item_id in self._downloaded:
                self._downloaded.discard(item_id)
                roles.append(self.DownloadedRole)
        elif state == _DS.COMPLETE:
            if item_id in self._progress:
                self._progress.pop(item_id, None)
                roles.append(self.DownloadFractionRole)
            if item_id not in self._downloaded:
                self._downloaded.add(item_id)
                roles.append(self.DownloadedRole)
        elif state == _DS.REMOVED:
            if item_id in self._progress:
                self._progress.pop(item_id, None)
                roles.append(self.DownloadFractionRole)
            if item_id in self._downloaded:
                self._downloaded.discard(item_id)
                roles.append(self.DownloadedRole)
        elif state == _DS.FAILED:
            if item_id in self._progress:
                self._progress.pop(item_id, None)
                roles.append(self.DownloadFractionRole)
        else:
            return
        if not roles:
            return
        for row, item in enumerate(self._items):
            if (item.get("Id") or "") == item_id:
                idx = self.index(row, 0)
                self.dataChanged.emit(idx, idx, roles)

    def _on_favorite_toggled(self, item_id: str, fav: bool):
        """Bus slot — patch the matching row's ``UserData.IsFavorite``
        in place and refresh. Mirrors the NP bar / mini player's
        optimistic flip pattern: the toggle was already applied on the
        server (or queued for it); our job here is to keep every
        surface that's showing the item visually in sync."""
        if not item_id:
            return
        for row, item in enumerate(self._items):
            if (item.get("Id") or "") != item_id:
                continue
            ud = item.get("UserData")
            if not isinstance(ud, dict):
                ud = {}
                item["UserData"] = ud
            if bool(ud.get("IsFavorite", False)) == bool(fav):
                continue
            ud["IsFavorite"] = bool(fav)
            idx = self.index(row, 0)
            self.dataChanged.emit(idx, idx, [self.IsFavoriteRole])

    def set_cover(self, row: int, pix: QPixmap):
        if not (0 <= row < len(self._items)):
            return
        if pix is None or pix.isNull():
            return
        self._covers[row] = pix
        idx = self.index(row, 0)
        self.dataChanged.emit(idx, idx, [self.CoverRole])

    def clear_covers(self):
        if not self._covers:
            return
        self._covers = {}
        if self._items:
            top = self.index(0, 0)
            bot = self.index(len(self._items) - 1, 0)
            self.dataChanged.emit(top, bot, [self.CoverRole])


# ── Tile delegate (IconMode) ─────────────────────────────────────────────


# Hover-revealed corner button: 28-px circle anchored to a corner of
# the cover, with a 16-px SVG glyph centred inside. Constants live at
# module scope so the view's hit-tester can compute the same rects
# without instantiating a delegate.
_CORNER_BTN_SIZE = 28
_CORNER_BTN_GLYPH = 16
_CORNER_BTN_MARGIN = 8


def _corner_rect(cover_rect: QRect, corner: str) -> QRect:
    """Geometry for one of the two corner buttons. ``corner`` is "br"
    (heart) or "bl" (download/check). Returned as an integer QRect so
    the view's mousePressEvent can hit-test it with ``contains(pos)``."""
    by = cover_rect.bottom() - _CORNER_BTN_SIZE - _CORNER_BTN_MARGIN
    if corner == "br":
        bx = cover_rect.right() - _CORNER_BTN_SIZE - _CORNER_BTN_MARGIN
    else:
        bx = cover_rect.left() + _CORNER_BTN_MARGIN
    return QRect(bx, by, _CORNER_BTN_SIZE, _CORNER_BTN_SIZE)


def _paint_progress_ring(
    painter: QPainter,
    cover_rect: QRect,
    fraction: float,
) -> None:
    """Paint a determinate accent-coloured progress ring in the cover's
    bottom-left corner. Replaces the BL download button while a job is
    in flight so the user gets at-a-glance progress without hovering.

    Visual: same dark circular backdrop as the corner buttons (so the
    transition reads as the same control state-changing, not a new
    widget appearing), a dim 2-px track ring, and an accent-coloured
    arc that sweeps clockwise from 12-o'clock as fraction climbs."""
    from modules.icons import ICON_ACCENT

    btn = _corner_rect(cover_rect, "bl")
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    # Backdrop — matches _paint_corner_button so the swap reads as a
    # state change of the same control, not a new widget. Theme-aware:
    # light disc on a light theme.
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(overlay_disc_qcolor())
    painter.drawEllipse(QRectF(btn))

    # Ring geometry — inset so the stroke sits fully inside the
    # backdrop circle. Width tuned so the track + arc read at small
    # sizes without looking heavy.
    stroke_w = 2.4
    inset = stroke_w / 2.0 + 3.0
    ring_rect = QRectF(btn).adjusted(inset, inset, -inset, -inset)

    # Dim track underneath — ink-toned so it reads on the disc in
    # either theme (dark track on a light disc, light on a dark one).
    pen = QPen(QColor(*ink_rgb(), 64))
    pen.setWidthF(stroke_w)
    pen.setCapStyle(Qt.PenCapStyle.FlatCap)
    painter.setPen(pen)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawEllipse(ring_rect)

    # Accent arc — Qt's drawArc takes 16ths of a degree, 0° is at 3
    # o'clock and positive sweeps counter-clockwise. We want 12 o'clock
    # start (= 90°) and a clockwise sweep, hence the negative span.
    frac = max(0.0, min(1.0, float(fraction)))
    span_deg = -360.0 * frac
    pen = QPen(QColor(ICON_ACCENT))
    pen.setWidthF(stroke_w)
    pen.setCapStyle(Qt.PenCapStyle.RoundCap)
    painter.setPen(pen)
    painter.drawArc(ring_rect, int(90 * 16), int(span_deg * 16))
    painter.restore()


def _paint_corner_button(
    painter: QPainter,
    cover_rect: QRect,
    corner: str,
    *,
    filled: bool,
    filled_glyph: str,
    outline_glyph: str,
) -> None:
    """Paint a hover-revealed corner button: dark circular backdrop and
    a centred SVG glyph. ``filled`` switches the glyph (filled name
    when True) and tints it accent; ``filled=False`` paints the outline
    glyph in bright white. Mirrors ``CoverOverlayButton`` in style so
    the tile and NP-bar surfaces read as the same control."""
    from modules.icons import ICON_ACCENT, ICON_BRIGHT, _svg_pix

    btn = _corner_rect(cover_rect, corner)
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    # Backdrop — matches CoverOverlayButton's disc so the two surfaces
    # look identical. Theme-aware: light disc on a light theme. No rim:
    # the cover supplies the border.
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(overlay_disc_qcolor())
    painter.drawEllipse(QRectF(btn))
    painter.restore()

    name = filled_glyph if filled else outline_glyph
    color = ICON_ACCENT if filled else ICON_BRIGHT
    pix = _svg_pix(name, color, _CORNER_BTN_GLYPH)
    gx = btn.center().x() - _CORNER_BTN_GLYPH // 2 + 1
    gy = btn.center().y() - _CORNER_BTN_GLYPH // 2 + 1
    painter.drawPixmap(gx, gy, pix)


class _TileDelegate(QStyledItemDelegate):
    """Paints one library item in IconMode: cover + title + (year for
    albums) + subtitle. All draw operations — no child widgets, no per-
    row stylesheets. Hover state comes from option.state (the view's
    mouseTracking feeds State_MouseOver automatically); the play
    overlay paints on hover and is hit-tested via :meth:`overlay_rect_for`
    in the view's mousePressEvent. Album-tile subtitle (artist line)
    and year line each have their own click hit-test sub-rect."""

    COVER_SIZE = 180
    OVERLAY_SIZE = 56
    CELL_W = 196  # COVER_SIZE + 16 horizontal gap
    COVER_RADIUS = 8
    # Cell-height presets — picked based on which text lines the
    # delegate is configured to paint. `show_year=True` adds 22px for
    # the year row; `show_subtitle=True` adds 22px for the artist /
    # track-count row. Bottom margin (~12px) baked into the base.
    _CELL_H_BASE = 224  # cover + title + bottom margin
    _CELL_H_YEAR = 22
    _CELL_H_SUBTITLE = 22

    def __init__(self, kind: str, parent=None, show_year: bool = True, show_subtitle: bool = True):
        super().__init__(parent)
        self._kind = kind
        self._show_play_overlay = kind != "artist"
        # Year line only meaningful for albums; flag is effectively
        # always False for playlists / artists.
        self._show_year = show_year and (kind == "album")
        self._show_subtitle = show_subtitle
        # Pre-scaled cover cache keyed by (cover.cacheKey, logical
        # size, dpr). The cover bitmap is rescaled here once per
        # discrete (cover_size, dpr) pair instead of every paint —
        # otherwise drawPixmap(rect, cover) does an internal scale
        # on every paint, which is the visible shimmer during a
        # drag-resize. Cap keeps memory bounded; eviction is FIFO.
        self._scaled_cover_cache: Dict[tuple, "QPixmap"] = {}
        self._scaled_cover_cache_cap = 256
        self._build_fonts()
        # Theme/font-scale changes can in principle shift TYPE_BODY.size_px
        # at runtime — in jellytoast today font scale needs a restart but
        # wiring this signal future-proofs the cache and matches the
        # contract in [[architecture_live_accent]].
        from modules.player_state import PlayerBus

        PlayerBus.get().theme_changed.connect(self._build_fonts)

    def _build_fonts(self):
        title_font = QFont()
        title_font.setPixelSize(TYPE_BODY.size_px)
        title_font.setBold(True)
        self._title_font = title_font
        self._fm_title = QFontMetrics(title_font)
        caption_font = QFont()
        caption_font.setPixelSize(TYPE_CAPTION.size_px)
        caption_font.setBold(False)
        self._caption_font = caption_font
        self._fm_caption = QFontMetrics(caption_font)

    @property
    def CELL_H(self) -> int:
        h = self._CELL_H_BASE
        if self._show_year:
            h += self._CELL_H_YEAR
        if self._show_subtitle:
            h += self._CELL_H_SUBTITLE
        return h

    def sizeHint(self, option, index):
        return QSize(self.CELL_W, self.CELL_H)

    def paint(self, painter, option, index):
        item = index.data(_LibraryItemsModel.ItemRole)
        if item is None:
            return
        cover = index.data(_LibraryItemsModel.CoverRole)
        rect = option.rect

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        # Re-read theme constants on every paint so live-accent /
        # live-theme changes flow through without per-delegate caches —
        # via the module ref (single attribute lookup) rather than
        # re-running the import machinery per tile.
        _TEXT = _u.TEXT

        # Cover size adapts to the cell — see _effective_cover_size.
        cover_size = self._effective_cover_size(rect)
        content_x = rect.x() + (rect.width() - cover_size) // 2
        cover_rect = QRect(content_x, rect.y(), cover_size, cover_size)

        # Cover paint — rounded square. Placeholder rect for items that
        # haven't loaded artwork yet, or have no artwork available.
        if cover is not None and not cover.isNull():
            scaled = self._scaled_cover(cover, cover_size)
            path = QPainterPath()
            path.addRoundedRect(QRectF(cover_rect), self.COVER_RADIUS, self.COVER_RADIUS)
            painter.save()
            painter.setClipPath(path)
            # drawPixmap(point, pixmap) — no rescale on the paint
            # thread; the bitmap was pre-scaled into the cache.
            painter.drawPixmap(content_x, rect.y(), scaled)
            painter.restore()
        else:
            path = QPainterPath()
            path.addRoundedRect(QRectF(cover_rect), self.COVER_RADIUS, self.COVER_RADIUS)
            painter.fillPath(path, QColor(*ink_rgb(), 10))

        # Keyboard-focus ring — accent-colored 2 px stroke around the
        # cover. Painted ONLY when the owning view's _keyboard_mode
        # flag is set (i.e. focus arrived via Tab / Shortcut, not a
        # mouse click). Qt's State_HasFocus + view.hasFocus() alone
        # weren't enough — clicks on a tile leave focus on the view
        # with currentIndex set, which without this gate would paint
        # the ring as a click feedback (not what we want).
        view_widget = getattr(option, "widget", None)
        kb_mode = bool(getattr(view_widget, "_keyboard_mode", False))
        if (option.state & QStyle.StateFlag.State_HasFocus) and kb_mode:
            ring = QColor(_u.ACCENT)
            ring.setAlpha(220)
            pen = QPen(ring)
            pen.setWidth(2)
            painter.save()
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            # Inset by 1 so the 2 px stroke draws fully inside the
            # cover's bounding rect — otherwise the outer half of
            # the line lands beyond the cover and clips against the
            # cell's edge in tight grids.
            painter.drawRoundedRect(
                QRectF(cover_rect).adjusted(1, 1, -1, -1),
                self.COVER_RADIUS,
                self.COVER_RADIUS,
            )
            painter.restore()

        # Hover overlay: a single translucent disc with the play
        # glyph — matches the tile's corner buttons (no rim). The disc
        # is theme-aware (light on a light theme, dark on dark) and the
        # glyph is theme-ink. Skipped for artists ("play an artist"
        # has no canonical meaning).
        if self._show_play_overlay and option.state & QStyle.StateFlag.State_MouseOver:
            ov_rect = self.overlay_rect_for(rect)
            painter.save()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(overlay_disc_qcolor())
            painter.drawEllipse(ov_rect)
            # Play triangle — drawn directly with a QPainterPath so we
            # don't depend on QIcon.pixmap() (which intermittently
            # returns a null pixmap when the icon was registered at a
            # different size than requested). Geometry is hand-tuned
            # to look balanced inside the 56px circle.
            cx = ov_rect.center().x()
            cy = ov_rect.center().y()
            tri_w = 14
            tri_h = 16
            # Optical-center nudge — the triangle's visual mass sits
            # slightly left of its bounding-box center, so shift right.
            left = cx - tri_w // 2 + 2
            top = cy - tri_h // 2
            tri = QPainterPath()
            tri.moveTo(left, top)
            tri.lineTo(left + tri_w, cy)
            tri.lineTo(left, top + tri_h)
            tri.closeSubpath()
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(*ink_rgb()))
            painter.drawPath(tri)
            painter.restore()

        # Bottom-left state machine:
        #   • mid-download → always-visible determinate progress ring
        #   • else, on hover → check (downloaded) or download (idle)
        # Bottom-right (heart) is hover-only regardless of state.
        dl_fraction = float(index.data(_LibraryItemsModel.DownloadFractionRole) or -1.0)
        is_hovered = bool(option.state & QStyle.StateFlag.State_MouseOver)
        if dl_fraction >= 0.0:
            _paint_progress_ring(painter, cover_rect, dl_fraction)
        elif is_hovered:
            _paint_corner_button(
                painter,
                cover_rect,
                "bl",
                filled=bool(index.data(_LibraryItemsModel.DownloadedRole)),
                filled_glyph="check_filled",
                outline_glyph="download",
            )
        if is_hovered:
            _paint_corner_button(
                painter,
                cover_rect,
                "br",
                filled=bool(index.data(_LibraryItemsModel.IsFavoriteRole)),
                filled_glyph="favorite_filled",
                outline_glyph="favorite_outline",
            )

        # Title — bold body, centered, eliding. Font + metrics are
        # cached on the delegate via ``_build_fonts()`` and rebound on
        # ``PlayerBus.theme_changed`` — see ``__init__`` for the wiring.
        title_y = cover_rect.bottom() + SPACE_SM + 1
        title_h = 22
        title_rect = QRect(rect.x(), title_y, rect.width(), title_h)
        painter.setFont(self._title_font)
        fm_title = self._fm_title
        painter.setPen(QColor(_TEXT))
        title = item.get("Name") or "Unknown"
        painter.drawText(
            title_rect,
            int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
            fm_title.elidedText(title, Qt.TextElideMode.ElideRight, title_rect.width() - 8),
        )

        # Caption font for year + subtitle.
        painter.setFont(self._caption_font)
        fm_cap = self._fm_caption

        # Year — albums only, sits between title and subtitle.
        year_y = title_rect.bottom() + 2
        year_h = 18
        year_text = ""
        if self._show_year:
            y = item.get("ProductionYear")
            if y:
                year_text = str(y)
            else:
                pd = (item.get("PremiereDate") or "").strip()
                if pd[:4].isdigit():
                    year_text = pd[:4]
        if year_text:
            year_rect = QRect(rect.x(), year_y, rect.width(), year_h)
            painter.setPen(QColor(_TEXT))
            painter.drawText(
                year_rect,
                int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                year_text,
            )
            subtitle_y = year_rect.bottom() + 2
        else:
            subtitle_y = year_y

        # Subtitle — kind-dependent. Albums show the artist; playlists
        # show track count; artists show first genre.
        subtitle = _compute_subtitle(item, self._kind) if self._show_subtitle else ""
        if subtitle:
            subtitle_rect = QRect(rect.x(), subtitle_y, rect.width(), year_h)
            painter.setPen(QColor(_TEXT))
            painter.drawText(
                subtitle_rect,
                int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
                fm_cap.elidedText(subtitle, Qt.TextElideMode.ElideRight, subtitle_rect.width() - 8),
            )

        painter.restore()

    def _effective_cover_size(self, cell_rect: QRect) -> int:
        """Cell width minus 8 px breathing room, capped at the
        delegate's natural COVER_SIZE. Used in paint + hit-tests
        so the cover and its overlay scale together when the
        view's adaptive grid produces narrow cells.

        Quantized to 12-px steps in the shrink range so the cover
        bitmap doesn't re-scale on every pixel of resize — the
        scaled-cover cache stays warm across small width deltas
        and the only thing that changes per pixel is the cover's
        center position within the cell."""
        max_cover = max(48, cell_rect.width() - 8)
        if max_cover >= self.COVER_SIZE:
            return self.COVER_SIZE
        return max(48, (max_cover // 12) * 12)

    def _scaled_cover(self, cover, target_logical: int):
        """Return `cover` scaled to `target_logical` logical pixels,
        from a per-delegate cache. drawPixmap(point, scaled) blits
        without any rescale on the paint thread — that's what
        makes resize smooth instead of shimmery."""
        from PySide6.QtGui import QPixmap  # local: keep top-of-file lean

        try:
            dpr = cover.devicePixelRatioF() or 1.0
        except Exception:
            dpr = 1.0
        # Round dpr so float jitter doesn't bust the cache key.
        dpr_key = round(dpr * 100)
        key = (cover.cacheKey(), target_logical, dpr_key)
        cached = self._scaled_cover_cache.get(key)
        if cached is not None:
            return cached
        target_phys = max(1, int(round(target_logical * dpr)))
        scaled: QPixmap = cover.scaled(
            target_phys,
            target_phys,
            Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
        scaled.setDevicePixelRatio(dpr)
        self._scaled_cover_cache[key] = scaled
        if len(self._scaled_cover_cache) > self._scaled_cover_cache_cap:
            # Drop the oldest quarter — dict preserves insertion order.
            drop_n = self._scaled_cover_cache_cap // 4
            for k in list(self._scaled_cover_cache.keys())[:drop_n]:
                del self._scaled_cover_cache[k]
        return scaled

    def overlay_rect_for(self, cell_rect: QRect) -> QRect:
        """Center the play overlay over the cover's center."""
        cover_size = self._effective_cover_size(cell_rect)
        content_x = cell_rect.x() + (cell_rect.width() - cover_size) // 2
        cover_cx = content_x + cover_size // 2
        cover_cy = cell_rect.y() + cover_size // 2
        return QRect(
            cover_cx - self.OVERLAY_SIZE // 2,
            cover_cy - self.OVERLAY_SIZE // 2,
            self.OVERLAY_SIZE,
            self.OVERLAY_SIZE,
        )

    def _cover_rect_for(self, cell_rect: QRect) -> QRect:
        """Geometry of the cover square inside the cell. Shared helper
        for the corner-button hit-tests so the view doesn't recompute
        the cover layout."""
        cover_size = self._effective_cover_size(cell_rect)
        content_x = cell_rect.x() + (cell_rect.width() - cover_size) // 2
        return QRect(content_x, cell_rect.y(), cover_size, cover_size)

    def heart_rect_for(self, cell_rect: QRect) -> QRect:
        """Hit-test rect for the bottom-right favorite corner button."""
        return _corner_rect(self._cover_rect_for(cell_rect), "br")

    def download_rect_for(self, cell_rect: QRect) -> QRect:
        """Hit-test rect for the bottom-left download/check corner."""
        return _corner_rect(self._cover_rect_for(cell_rect), "bl")

    def subtitle_rect_for(self, cell_rect: QRect, item: Dict) -> QRect:
        """Sub-rect of the subtitle line. Mirrors :meth:`paint`'s
        vertical math so the view can hit-test artist clicks against
        this rect. Returns empty rect when subtitle is suppressed."""
        if not self._show_subtitle:
            return QRect()
        cover_size = self._effective_cover_size(cell_rect)
        title_y = cell_rect.y() + cover_size + SPACE_SM + 1
        title_bottom = title_y + 22
        year_text = ""
        if self._show_year:
            y = item.get("ProductionYear")
            if y:
                year_text = str(y)
            else:
                pd = (item.get("PremiereDate") or "").strip()
                if pd[:4].isdigit():
                    year_text = pd[:4]
        if year_text:
            year_y = title_bottom + 2
            subtitle_y = year_y + 18 + 2
        else:
            subtitle_y = title_bottom + 2
        return QRect(cell_rect.x(), subtitle_y, cell_rect.width(), 18)

    def year_rect_for(self, cell_rect: QRect, item: Dict) -> QRect:
        if not self._show_year:
            return QRect()
        cover_size = self._effective_cover_size(cell_rect)
        title_y = cell_rect.y() + cover_size + SPACE_SM + 1
        title_bottom = title_y + 22
        year_y = title_bottom + 2
        return QRect(cell_rect.x(), year_y, cell_rect.width(), 18)


# ── Row delegate (ListMode) ──────────────────────────────────────────────


class _RowDelegate(QStyledItemDelegate):
    """Paints one library item as a single horizontal row: thumb +
    title + subtitle stack + right-aligned year (album only). Shares
    the cover cache with _TileDelegate via _LibraryItemsModel — the
    180×180 pixmap downscales to a 36×36 thumb at paint via
    scale_pixmap_for_dpr so list mode reuses grid-mode cache slots."""

    THUMB_SIZE = 36
    ROW_HEIGHT = 48
    LEFT_PAD = SPACE_SM
    RIGHT_PAD = SPACE_SM
    GAP = SPACE_SM
    YEAR_W = 60
    THUMB_RADIUS = 4

    def __init__(self, kind: str, parent=None):
        super().__init__(parent)
        self._kind = kind
        # Pre-scaled thumb cache keyed by (cover.cacheKey, logical size,
        # dpr). Mirrors _TileDelegate._scaled_cover_cache: the 180×180
        # cover bitmap is rescaled (+ centre-cropped) to the 36px thumb
        # once per discrete (size, dpr) pair instead of every paint —
        # scale_pixmap_for_dpr ran a SmoothTransformation downscale plus
        # a .copy() crop on each repaint otherwise. Cap keeps memory
        # bounded; eviction is FIFO via insertion order.
        self._scaled_cover_cache: Dict[tuple, "QPixmap"] = {}
        self._scaled_cover_cache_cap = 256
        self._build_fonts()
        from modules.player_state import PlayerBus

        PlayerBus.get().theme_changed.connect(self._build_fonts)

    def _build_fonts(self):
        title_font = QFont()
        title_font.setPixelSize(TYPE_BODY.size_px)
        title_font.setBold(True)
        self._title_font = title_font
        self._fm_title = QFontMetrics(title_font)
        sub_font = QFont()
        sub_font.setPixelSize(TYPE_CAPTION.size_px)
        sub_font.setBold(False)
        self._sub_font = sub_font
        self._fm_sub = QFontMetrics(sub_font)
        # Year column uses the same caption tier as the subtitle but
        # doesn't elide — own reference for clarity at paint time.
        self._year_font = sub_font

    def sizeHint(self, option, index):
        w = option.rect.width() if option.rect.width() > 0 else 200
        return QSize(w, self.ROW_HEIGHT)

    def _scaled_thumb(self, cover, target_logical: int):
        """Return `cover` downscaled + centre-cropped to a `target_logical`
        square thumb, from a per-delegate cache. Wraps
        scale_pixmap_for_dpr so visual output is byte-identical to the
        un-cached path, but the SmoothTransformation scale + crop runs
        once per (cacheKey, size, dpr) instead of every paint."""
        dpr = screen_dpr()
        # Round dpr so float jitter doesn't bust the cache key.
        dpr_key = round(dpr * 100)
        key = (cover.cacheKey(), target_logical, dpr_key)
        cached = self._scaled_cover_cache.get(key)
        if cached is not None:
            return cached
        scaled = scale_pixmap_for_dpr(cover, target_logical, dpr)
        self._scaled_cover_cache[key] = scaled
        if len(self._scaled_cover_cache) > self._scaled_cover_cache_cap:
            # Drop the oldest quarter — dict preserves insertion order.
            drop_n = self._scaled_cover_cache_cap // 4
            for k in list(self._scaled_cover_cache.keys())[:drop_n]:
                del self._scaled_cover_cache[k]
        return scaled

    def paint(self, painter, option, index):
        item = index.data(_LibraryItemsModel.ItemRole)
        if item is None:
            return
        cover = index.data(_LibraryItemsModel.CoverRole)
        rect = option.rect

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        _TEXT = _u.TEXT

        # Hover backdrop — faint highlight so the row reads as
        # interactive without committing to a heavy selection chip.
        # Keyboard-focus wash a touch heavier than hover so arrow-
        # key users can see which row Enter would activate.
        if option.state & QStyle.StateFlag.State_HasFocus:
            inset = rect.adjusted(2, 2, -2, -2)
            path = QPainterPath()
            path.addRoundedRect(QRectF(inset), 4, 4)
            painter.fillPath(path, QColor(*ink_rgb(), 22))
        elif option.state & QStyle.StateFlag.State_MouseOver:
            inset = rect.adjusted(2, 2, -2, -2)
            path = QPainterPath()
            path.addRoundedRect(QRectF(inset), 4, 4)
            painter.fillPath(path, QColor(*ink_rgb(), 13))

        # Thumb cell — centered vertically inside the row.
        thumb_y = rect.y() + (rect.height() - self.THUMB_SIZE) // 2
        thumb_rect = QRect(
            rect.x() + self.LEFT_PAD,
            thumb_y,
            self.THUMB_SIZE,
            self.THUMB_SIZE,
        )
        if cover is not None and not cover.isNull():
            scaled = self._scaled_thumb(cover, self.THUMB_SIZE)
            path = QPainterPath()
            path.addRoundedRect(QRectF(thumb_rect), self.THUMB_RADIUS, self.THUMB_RADIUS)
            painter.save()
            painter.setClipPath(path)
            painter.drawPixmap(thumb_rect, scaled)
            painter.restore()
        else:
            path = QPainterPath()
            path.addRoundedRect(QRectF(thumb_rect), self.THUMB_RADIUS, self.THUMB_RADIUS)
            painter.fillPath(path, QColor(*ink_rgb(), 10))

        # Text columns — title + subtitle stacked, year right-aligned.
        year_text = ""
        if self._kind == "album":
            y = item.get("ProductionYear")
            if y:
                year_text = str(y)
            else:
                pd = (item.get("PremiereDate") or "").strip()
                if pd[:4].isdigit():
                    year_text = pd[:4]

        text_x = thumb_rect.right() + self.GAP
        text_right = rect.right() - self.RIGHT_PAD
        if year_text:
            text_right -= self.YEAR_W + self.GAP
        text_w = max(0, text_right - text_x)

        # Font + metrics are cached on the delegate (`_build_fonts`)
        # and refreshed on `PlayerBus.theme_changed`.
        title_font = self._title_font
        fm_title = self._fm_title

        subtitle = _compute_subtitle(item, self._kind)
        if subtitle:
            title_h = 20
            sub_h = 16
            title_y = rect.y() + (rect.height() - (title_h + sub_h)) // 2
            sub_y = title_y + title_h
            title_rect = QRect(text_x, title_y, text_w, title_h)
            sub_rect = QRect(text_x, sub_y, text_w, sub_h)
        else:
            title_rect = QRect(text_x, rect.y(), text_w, rect.height())
            sub_rect = QRect(text_x, rect.y(), 0, 0)

        painter.setFont(title_font)
        painter.setPen(QColor(_TEXT))
        title = item.get("Name") or "Unknown"
        painter.drawText(
            title_rect,
            int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
            fm_title.elidedText(title, Qt.TextElideMode.ElideRight, title_rect.width()),
        )

        if subtitle:
            painter.setFont(self._sub_font)
            painter.setPen(QColor(_TEXT))
            painter.drawText(
                sub_rect,
                int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                self._fm_sub.elidedText(subtitle, Qt.TextElideMode.ElideRight, sub_rect.width()),
            )

        if year_text:
            painter.setFont(self._year_font)
            painter.setPen(QColor(_TEXT))
            year_rect = QRect(
                rect.right() - self.RIGHT_PAD - self.YEAR_W,
                rect.y(),
                self.YEAR_W,
                rect.height(),
            )
            painter.drawText(
                year_rect,
                int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                year_text,
            )

        painter.restore()

    def subtitle_rect_for(self, cell_rect: QRect, item: Dict) -> QRect:
        """Sub-rect of the subtitle line. Mirrors :meth:`paint`'s
        vertical math so the view can hit-test artist clicks here."""
        subtitle = _compute_subtitle(item, self._kind)
        if not subtitle:
            return QRect()
        year_text = ""
        if self._kind == "album":
            y = item.get("ProductionYear")
            if y:
                year_text = str(y)
            else:
                pd = (item.get("PremiereDate") or "").strip()
                if pd[:4].isdigit():
                    year_text = pd[:4]
        text_x = cell_rect.x() + self.LEFT_PAD + self.THUMB_SIZE + self.GAP
        text_right = cell_rect.right() - self.RIGHT_PAD
        if year_text:
            text_right -= self.YEAR_W + self.GAP
        text_w = max(0, text_right - text_x)
        title_h = 20
        sub_h = 16
        title_y = cell_rect.y() + (cell_rect.height() - (title_h + sub_h)) // 2
        sub_y = title_y + title_h
        return QRect(text_x, sub_y, text_w, sub_h)

    def year_rect_for(self, cell_rect: QRect, item: Dict) -> QRect:
        if self._kind != "album":
            return QRect()
        y = item.get("ProductionYear")
        year_text = str(y) if y else ""
        if not year_text:
            pd = (item.get("PremiereDate") or "").strip()
            if pd[:4].isdigit():
                year_text = pd[:4]
        if not year_text:
            return QRect()
        return QRect(
            cell_rect.right() - self.RIGHT_PAD - self.YEAR_W,
            cell_rect.y(),
            self.YEAR_W,
            cell_rect.height(),
        )


# ── Helpers shared by both delegates ─────────────────────────────────────


def _compute_subtitle(item: Dict, kind: str) -> str:
    if kind == "playlist":
        count = item.get("ChildCount") or 0
        return f"{count} tracks" if count != 1 else "1 track"
    if kind == "artist":
        genres = [g for g in (item.get("Genres") or []) if g]
        return genres[0] if genres else ""
    # Default (album): artist line
    return item.get("AlbumArtist") or ", ".join(item.get("AlbumArtists", []) or []) or ""


def _artist_id_for_album(item: Dict) -> str:
    """First album-artist id from the album item's metadata, if any.
    Both Jellyfin and SubsonicProvider's _adapt_album emit an
    ``AlbumArtists`` list of {Id, Name}; ``ArtistItems`` is a
    Jellyfin-only sibling field."""
    for field in ("AlbumArtists", "ArtistItems"):
        arr = item.get(field) or []
        if arr and isinstance(arr, list):
            first = arr[0] or {}
            aid = first.get("Id") if isinstance(first, dict) else ""
            if aid:
                return aid
    return ""


# ── View ─────────────────────────────────────────────────────────────────


class _LibraryListView(QListView):
    """QListView tuned for the library grid surface. Swaps between
    IconMode (multi-column tile grid) and ListMode (single-column row
    stack) via :meth:`set_mode`. Hit-tests the delegate sub-rects so
    the play overlay, subtitle (artist link), and year (year-filter
    link) each route to their own signal.

    Grid layout uses a FIXED cell size (set once via setGridSize on
    construction and on mode switch). Qt's setResizeMode(Adjust)
    handles the column count automatically as the window resizes —
    items wrap to a new row whenever they cross the viewport edge,
    no client-side recompute required. The result: dragging the
    window edge within a column band is completely free (no
    relayouts, no repaints), and crossing into a new band reflows
    once via Qt's built-in path."""

    play_requested = Signal(str)  # item_id
    browse_requested = Signal(str)  # item_id
    artist_browse_requested = Signal(str)  # artist_id
    year_browse_requested = Signal(int)  # year

    def __init__(self, tile_delegate: _TileDelegate, row_delegate: _RowDelegate, parent=None):
        super().__init__(parent)
        self._tile_delegate = tile_delegate
        self._row_delegate = row_delegate
        self._mode = "grid"
        self.setItemDelegate(tile_delegate)
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setFlow(QListView.Flow.LeftToRight)
        self.setWrapping(True)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setMovement(QListView.Movement.Static)
        self.setUniformItemSizes(True)
        self.setMouseTracking(True)
        self.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOn)
        self.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setSpacing(0)
        # Viewport transparency — same trick as library_grid's old
        # scroll area: prevents the default-palette base colour from
        # flashing under the body QSS cascade on first show.
        vp = self.viewport()
        vp.setAutoFillBackground(False)
        vp.setBackgroundRole(QPalette.ColorRole.NoRole)
        self.setStyleSheet("QListView { background: transparent; border: none; }")
        # Hover → prewarm: matches the old LibraryTile.enterEvent
        # path. Mouse moves over an item → fire background fetches
        # for get_item + get_album_tracks so that a subsequent click
        # resolves from cache instead of waiting on the network.
        # Idempotent — each item is prewarmed at most once per view
        # lifetime via the _prewarmed set.
        self._prewarmed: set = set()
        self.entered.connect(self._on_entered)
        # Tracks whether focus arrived via keyboard (Tab / Shortcut)
        # or mouse. The delegate paints the focus ring ONLY in
        # keyboard mode so a click doesn't leave a lingering accent
        # ring on the clicked tile.
        self._keyboard_mode = False
        # Layout is recomputed once at construction and then again
        # only after the user pauses resizing (settle timer below).
        # During an active drag, setGridSize / setViewportMargins
        # are NOT called — items stay rock-still while the right
        # edge briefly opens a gap, then reflow once the drag
        # ends. This is what makes the resize visually smooth:
        # items aren't shifting per pixel of viewport width.
        self._last_grid_size = QSize()
        # Tracked alongside _last_grid_size because cell_w can repeat
        # across a column-count band boundary (735//3 == 980//4 == 245)
        # — a pure size-equality cache would then skip the relayout.
        self._last_cols = -1
        self._settle_timer = QTimer(self)
        self._settle_timer.setSingleShot(True)
        self._settle_timer.setInterval(120)
        self._settle_timer.timeout.connect(self._apply_grid_size)
        self._apply_grid_size()

    # Spotify-style grid: column count comes from breakpoints below
    # (so 3-col density holds across a wide span of widths instead
    # of flipping every ~200 px) and the cells grow to fill the
    # viewport. The cover stays at COVER_SIZE (180 px) whenever
    # the cell is wide enough; below that, _effective_cover_size
    # shrinks the cover to fit — but it quantizes to coarse steps
    # so the cover bitmap only re-scales at those discrete steps,
    # not every pixel of resize. Combined with the delegate's
    # pre-scaled pixmap cache, this is what eliminates the shimmer.
    MIN_TILE_WIDTH = 110
    _BASE_HMARGIN = 24

    # (avail_upper_exclusive_logical_px, col_count). Tuned so a
    # narrow ~450-px viewport still holds 3 columns; default
    # 920-px window opens at 4 columns; wider windows step up
    # one column at each band edge.
    _COL_BANDS = (
        (200, 1),
        (400, 2),
        (760, 3),
        (1100, 4),
        (1460, 5),
        (1840, 6),
        (2240, 7),
        (2660, 8),
    )

    def _cols_for_width(self, available: int) -> int:
        for upper, cols in self._COL_BANDS:
            if available < upper:
                return cols
        return self._COL_BANDS[-1][1]

    def _available_width(self) -> int:
        """Logical px left for tiles after the scrollbar + side margins
        — the input to both the column-band lookup and cell sizing."""
        sb = self.verticalScrollBar()
        sb_w = sb.sizeHint().width() if sb is not None else 0
        return max(
            self.MIN_TILE_WIDTH,
            self.width() - sb_w - 2 * self._BASE_HMARGIN,
        )

    def _apply_grid_size(self):
        """Cells fill the viewport; column count comes from the
        breakpoint table. setGridSize fires only when the cell size
        OR the column count changed, but the cover bitmap is quantized
        + pre-scaled by the delegate so the shimmer is avoided."""
        if self._mode != "grid":
            return
        cell_h = self._tile_delegate.CELL_H
        available = self._available_width()
        cols = self._cols_for_width(available)
        # Guard against an exact divide: `available // cols` can make
        # `cols * cell_w == available` exactly, which leaves QListView's
        # IconMode wrap logic zero headroom — it then fits one fewer
        # column and wraps the last tile. Shaving ~2 px per column keeps
        # the cells filling the viewport (the gap is sub-1%) while
        # guaranteeing the intended column count always fits.
        cell_w = max(self.MIN_TILE_WIDTH, (available - 2 * cols) // cols)
        new_grid = QSize(cell_w, cell_h)
        # Re-apply when either the cell size or the column count changed.
        # The column-count guard matters because cell_w can repeat across
        # a band boundary — a pure size cache would skip the relayout.
        if new_grid == self._last_grid_size and cols == self._last_cols:
            return
        self._last_grid_size = new_grid
        self._last_cols = cols
        self.setGridSize(new_grid)
        self.setViewportMargins(self._BASE_HMARGIN, 0, self._BASE_HMARGIN, 0)
        # A column-count change is a structural reflow; force it so the
        # view never sits at a stale density after a coincidental
        # cell_w match.
        self.scheduleDelayedItemsLayout()

    def showEvent(self, event):
        super().showEvent(event)
        # __init__'s _apply_grid_size ran against the pre-layout default
        # width (~100 px), so the grid opens at 1 column until something
        # corrects it. On a quiet launch the first resizeEvent's settle
        # timer handles that — but a boot-time race (deferred show, a
        # screen/DPR re-evaluation right after map) can leave the grid
        # stuck at the stale density. Re-apply once geometry is real;
        # singleShot(0) defers past the current event batch so
        # self.width() reflects the laid-out size.
        if self._mode == "grid":
            QTimer.singleShot(0, self._apply_grid_size)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._mode != "grid":
            return
        # A column-count change is a coarse, intentional band-boundary
        # event — apply it promptly so the grid never sits at the wrong
        # density (the launch / screen-change race). Sub-band width
        # changes still defer to the settle timer so drag-resize stays
        # smooth: items hold their last-settled positions, a right-edge
        # gap briefly opens, then they reflow once the user pauses.
        if self._cols_for_width(self._available_width()) != self._last_cols:
            self._apply_grid_size()
        else:
            self._settle_timer.start()

    def set_mode(self, mode: str):
        if mode == self._mode:
            return
        self._mode = mode
        if mode == "list":
            self.setItemDelegate(self._row_delegate)
            self.setViewMode(QListView.ViewMode.ListMode)
            self.setFlow(QListView.Flow.TopToBottom)
            self.setWrapping(False)
            self.setViewportMargins(
                self._BASE_HMARGIN,
                0,
                self._BASE_HMARGIN,
                0,
            )
            # Drop the grid-size override so list rows use the
            # row-delegate's natural sizeHint. Invalidate the cache
            # too — next switch back to grid must re-apply.
            self.setGridSize(QSize())
            self._last_grid_size = QSize()
            self._last_cols = -1
        else:
            self.setItemDelegate(self._tile_delegate)
            self.setViewMode(QListView.ViewMode.IconMode)
            self.setFlow(QListView.Flow.LeftToRight)
            self.setWrapping(True)
            self._apply_grid_size()
        self.setResizeMode(QListView.ResizeMode.Adjust)
        # Force a re-layout so uniform item sizes pick up the new
        # delegate's sizeHint immediately. Without this, the first
        # paint after a mode switch can use the stale cell metrics.
        self.scheduleDelayedItemsLayout()


    def mousePressEvent(self, e):
        # Mouse interaction drops keyboard mode so the focus ring
        # stops painting on whichever tile was last keyboard-focused.
        if self._keyboard_mode:
            self._keyboard_mode = False
            self.viewport().update()
        if e.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(e)
            return
        pos = e.position().toPoint()
        idx = self.indexAt(pos)
        if not idx.isValid():
            super().mousePressEvent(e)
            return
        item = idx.data(_LibraryItemsModel.ItemRole) or {}
        item_id = item.get("Id", "")
        cell = self.visualRect(idx)

        if self._mode == "grid":
            # Hit-test order: heart → download → overlay → year → subtitle
            # → fall through. Corner buttons sit on top of the cover
            # so they outrank the play overlay; they're hover-revealed
            # but Qt's underMouse state implies hover anyway.
            heart_rect = self._tile_delegate.heart_rect_for(cell)
            if heart_rect.contains(pos) and item_id:
                self._toggle_favorite(item, item_id)
                e.accept()
                return
            dl_rect = self._tile_delegate.download_rect_for(cell)
            if dl_rect.contains(pos) and item_id:
                # During an in-flight download the BL slot is the
                # progress ring — clicks fall through to the normal
                # browse so the user doesn't accidentally cancel by
                # poking a moving target.
                dl_fraction = float(idx.data(_LibraryItemsModel.DownloadFractionRole) or -1.0)
                if dl_fraction < 0.0:
                    self._toggle_download(item, item_id)
                    e.accept()
                    return
            ov_rect = self._tile_delegate.overlay_rect_for(cell)
            if self._tile_delegate._show_play_overlay and ov_rect.contains(pos) and item_id:
                self.play_requested.emit(item_id)
                e.accept()
                return
            year_rect = self._tile_delegate.year_rect_for(cell, item)
            if year_rect.contains(pos):
                y = item.get("ProductionYear")
                year_int = (
                    int(y)
                    if isinstance(y, int)
                    else (int(y) if isinstance(y, str) and y.isdigit() else 0)
                )
                if year_int:
                    self.year_browse_requested.emit(year_int)
                    e.accept()
                    return
            sub_rect = self._tile_delegate.subtitle_rect_for(cell, item)
            if sub_rect.contains(pos):
                aid = _artist_id_for_album(item) if self._kind_of(item) == "album" else ""
                if aid:
                    self.artist_browse_requested.emit(aid)
                    e.accept()
                    return
        else:
            year_rect = self._row_delegate.year_rect_for(cell, item)
            if year_rect.contains(pos):
                y = item.get("ProductionYear")
                year_int = (
                    int(y)
                    if isinstance(y, int)
                    else (int(y) if isinstance(y, str) and y.isdigit() else 0)
                )
                if year_int:
                    self.year_browse_requested.emit(year_int)
                    e.accept()
                    return
            sub_rect = self._row_delegate.subtitle_rect_for(cell, item)
            if sub_rect.contains(pos):
                aid = _artist_id_for_album(item) if self._kind_of(item) == "album" else ""
                if aid:
                    self.artist_browse_requested.emit(aid)
                    e.accept()
                    return

        # Fall through — primary click on the cell is a browse.
        if item_id:
            self.browse_requested.emit(item_id)
            e.accept()
            return
        super().mousePressEvent(e)

    # ── Corner-button click handlers ────────────────────────────────────
    #
    # Both follow the same optimistic pattern as the NP bar / mini player:
    # we mutate the item dict in place, fire the bus signal so other
    # surfaces (mini player, bar) re-paint, and dispatch the actual
    # server / offline call asynchronously. The model's
    # ``favorite_toggled`` / ``download_progress`` subscriptions also
    # listen to the signal so the tile updates without us touching the
    # model directly.

    def _toggle_favorite(self, item: Dict, item_id: str) -> None:
        from modules.async_io import run_async
        from modules.player_state import PlayerBus

        ud = item.get("UserData")
        if not isinstance(ud, dict):
            ud = {}
            item["UserData"] = ud
        new_state = not bool(ud.get("IsFavorite", False))
        ud["IsFavorite"] = new_state
        try:
            api = get_provider()
            run_async(api.toggle_favorite, item_id, new_state)
        except Exception:
            pass
        PlayerBus.get().favorite_toggled.emit(item_id, new_state)

    def _toggle_download(self, item: Dict, item_id: str) -> None:
        """BL-corner click: download if idle, remove (with cascade
        confirmation for parents) if already on disk."""
        from modules import offline

        if not offline.is_downloaded(item_id):
            offline.download(item)
            return

        # Already downloaded — same cascade-confirm flow as the
        # right-click menu so a stray click doesn't nuke an album's
        # worth of files.
        from PySide6.QtWidgets import QMessageBox

        kind = self._tile_delegate._kind
        cascade_kinds = ("album", "playlist", "artist")
        if kind in cascade_kinds:
            name = item.get("Name") or f"this {kind}"
            confirm = QMessageBox.question(
                self,
                "Remove download",
                f"Remove the downloaded files for “{name}”?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return
        offline.remove(item_id)

    def contextMenuEvent(self, e):
        """Right-click a tile → radio / smart playlist / download. This
        is the only entry point for cascade downloads of albums /
        playlists / artists; track rows get theirs from
        ``SongsView._on_context_menu``. Removal of a parent is confirmed
        first — it cascades (design doc §5.7)."""
        idx = self.indexAt(e.pos())
        if not idx.isValid():
            super().contextMenuEvent(e)
            return
        item = idx.data(_LibraryItemsModel.ItemRole) or {}
        item_id = item.get("Id", "")
        if not item_id:
            super().contextMenuEvent(e)
            return

        from modules import offline
        from modules.ui_helpers import (
            opaque_menu,
            open_create_smart_playlist,
            start_seed_radio,
        )

        downloaded = offline.is_downloaded(item_id)
        kind = self._tile_delegate._kind
        item_name = item.get("Name") or ""

        menu = opaque_menu(self)
        # Album / artist tiles seed an INSTANT_MIX radio queue; the
        # RadioFeeder auto-extends from the stamped seed_kind. Playlist
        # tiles have no radio entry point (a playlist is already a
        # curated set).
        radio_act = None
        if kind == "album":
            radio_act = menu.addAction("Start album radio")
        elif kind == "artist":
            radio_act = menu.addAction("Start artist radio")
        # Create smart playlist from this album / artist — pre-fills the
        # editor with a from_album / from_artist recipe. Naming follows
        # the short-suffix idiom: "More like X" / "Deep Cuts: X".
        sp_act = None
        if item_name and kind in ("album", "artist"):
            sp_act = menu.addAction(
                f"Create smart playlist: More like {item_name}"
                if kind == "album"
                else f"Create smart playlist: Deep Cuts: {item_name}"
            )
        if radio_act is not None or sp_act is not None:
            menu.addSeparator()
        act = menu.addAction("Remove download" if downloaded else "Download")
        chosen = menu.exec(e.globalPos())
        if radio_act is not None and chosen is radio_act:
            seed_kind = "album" if kind == "album" else "artist"
            start_seed_radio(seed_kind, item_id, item_name)
            return
        if sp_act is not None and chosen is sp_act:
            # Pass the full item dict so from_album can read Genres +
            # ProductionYear for the era-vibe recipe. Falls back to
            # name-only gracefully if metadata is missing.
            open_create_smart_playlist(self, kind, item_name, item=item)
            return
        if chosen is not act:
            return

        if not downloaded:
            offline.download(item)
            return

        # Removing a parent cascades to its tracks — confirm first.
        from PySide6.QtWidgets import QMessageBox

        name = item.get("Name") or f"this {kind}"
        confirm = QMessageBox.question(
            self,
            "Remove download",
            f"Remove the downloaded files for “{name}”?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm == QMessageBox.StandardButton.Yes:
            offline.remove(item_id)

    def focusInEvent(self, e):
        """Flip into "keyboard mode" when focus arrives via Tab /
        Shortcut / programmatic setFocus; stay out of it when focus
        came from a mouse click. Mode toggles whether the delegate
        paints the accent focus ring — the ring is a keyboard
        affordance, not a click feedback."""
        keyboard_reasons = (
            Qt.FocusReason.TabFocusReason,
            Qt.FocusReason.BacktabFocusReason,
            Qt.FocusReason.ShortcutFocusReason,
            Qt.FocusReason.OtherFocusReason,
        )
        if e.reason() in keyboard_reasons:
            self._keyboard_mode = True
            if (
                not self.currentIndex().isValid()
                and self.model() is not None
                and self.model().rowCount() > 0
            ):
                self.setCurrentIndex(self.model().index(0, 0))
            self.viewport().update()
        super().focusInEvent(e)

    def focusOutEvent(self, e):
        self._keyboard_mode = False
        self.viewport().update()
        super().focusOutEvent(e)

    def keyPressEvent(self, e):
        """Arrow keys engage keyboard mode + seed the focus cursor
        to the first visible tile if nothing's selected yet. Enter
        opens the focused tile (browse, not play — keyboard users
        commit twice to start playback)."""
        arrow_keys = (
            Qt.Key.Key_Down,
            Qt.Key.Key_Up,
            Qt.Key.Key_Left,
            Qt.Key.Key_Right,
        )
        if e.key() in arrow_keys:
            # Engage keyboard mode so the focus ring paints, and
            # seed currentIndex to the top-left visible tile if
            # nothing's selected (otherwise Qt's default Down just
            # scrolls the viewport rather than snapping to a tile).
            need_seed = (
                not self.currentIndex().isValid()
                and self.model() is not None
                and self.model().rowCount() > 0
            )
            if not self._keyboard_mode:
                self._keyboard_mode = True
                self.viewport().update()
            if need_seed:
                seed = self.indexAt(self.viewport().rect().topLeft())
                if not seed.isValid():
                    seed = self.model().index(0, 0)
                self.setCurrentIndex(seed)
                e.accept()
                return
        if e.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            idx = self.currentIndex()
            if idx.isValid():
                item = idx.data(_LibraryItemsModel.ItemRole) or {}
                item_id = item.get("Id", "")
                if item_id:
                    self.browse_requested.emit(item_id)
                    e.accept()
                    return
        super().keyPressEvent(e)

    def _kind_of(self, item: Dict) -> str:
        # Trust the active delegate's kind — the model only holds one
        # kind at a time (the grid switches model on kind change).
        return self._tile_delegate._kind

    @Slot(QModelIndex)
    def _on_entered(self, idx: QModelIndex):
        """Hover prewarm: fire the get_item + get_tracks fetches that
        a subsequent click on this item will need, so the click
        resolves from the api's in-memory cache instead of waiting on
        the network. Mirrors LibraryTile.enterEvent's prewarm_detail
        for the widget-based path. Skipped for artists (they route
        to ArtistPage, which has its own preload). Idempotent — each
        item is warmed once per view lifetime."""
        if not idx.isValid():
            return
        item = idx.data(_LibraryItemsModel.ItemRole) or {}
        item_id = item.get("Id", "")
        if not item_id or item_id in self._prewarmed:
            return
        kind = self._kind_of(item)
        if kind == "artist":
            self._prewarmed.add(item_id)
            return
        self._prewarmed.add(item_id)
        from modules.async_io import run_async

        api = get_provider()
        fetch_tracks = api.get_playlist_items if kind == "playlist" else api.get_album_tracks
        run_async(
            api.get_item,
            item_id,
            on_result=lambda _r: None,
            on_error=lambda _e: None,
        )
        run_async(
            fetch_tracks,
            item_id,
            on_result=lambda _r: None,
            on_error=lambda _e: None,
        )
        # Album tiles also prewarm the now-playing-bar cover slot
        # so a click → play resolves the bar's cover from cache.
        if kind == "album":
            url = api.get_image_url(item_id, "Primary", 256)
            if url:
                from modules.ui_helpers import load_image_async

                load_image_async(
                    f"{item_id}|npbar",
                    url,
                    256,
                    256,
                    lambda _pix: None,
                    rounded_radius=0,
                    on_error=lambda: None,
                    priority="high",
                )


# ── Grid ────────────────────────────────────────────────────────────────


class LibraryGrid(_PaginatorMixin, QWidget):
    """Responsive grid of library items. QListView + QAbstractListModel
    + QStyledItemDelegate under the hood — no per-row widgets, so a
    1000-album browse renders in a single model reset and cover-loading
    + scroll stay on the cheap delegate-paint path.

    ``kind`` controls what's fetched and how each item is rendered:
      "album"    → IncludeItemTypes=MusicAlbum, subtitle = artist
      "playlist" → IncludeItemTypes=Playlist,   subtitle = track count
      "artist"   → IncludeItemTypes=MusicArtist, subtitle = first genre
    """

    play_requested = Signal(str)
    browse_requested = Signal(str)
    artist_browse_requested = Signal(str)
    year_browse_requested = Signal(int)

    _items_loaded = Signal(object)
    _refresh_loaded = Signal(object)

    TILE_WIDTH = LibraryTile.COVER_SIZE
    PAGE_SIZE = 200
    SCROLL_NEAR_BOTTOM = 0.8
    # A cover load that errors (cold/slow server right after login,
    # QNAM concurrency cap, transient timeout) is retried by the idle
    # prefetch pass rather than waiting for the user to scroll it back
    # into view. Capped so a genuinely offline grid stops eventually.
    COVER_RETRY_LIMIT = 4

    # On a first cold load, fire cover requests for the top-of-grid
    # rows immediately (before the QListView has computed its visible
    # range). The main window pairs this with a short reveal delay so
    # the first paint shows tiles already populated instead of a flash
    # of empty cells. 16 ≈ 4 cols × 4 rows, the typical above-the-fold
    # surface on a default-size window.
    _INITIAL_PRELOAD_ROWS = 16

    # Covers are fetched from the server at one fixed physical size,
    # independent of the current display DPR. The cache identity in
    # load_image_async's L2 raw tier is keyed by semantic id (the
    # album id), so a fixed source size means a cover fetched once
    # serves every DPR forever — the tile delegate rescales to the
    # exact DPR-correct size at paint time (_scaled_cover) anyway.
    # Baking the live DPR into the request instead (the old
    # `max(COVER_SIZE, COVER_SIZE * dpr)`) fragmented the cache:
    # every distinct DPR the app ever ran at — 1.25, 1.8, 2.0 all
    # show up on a fractional-scaled Wayland session — got its own
    # cache slot, so scrolling a library that was "fully loaded"
    # under a different DPR re-hit the network. 540 = COVER_SIZE×3
    # covers up to a 3× display sharply; higher just upscales a
    # thumbnail at paint, imperceptible.
    _COVER_SOURCE_PX = _TileDelegate.COVER_SIZE * 3

    _ITEM_TYPE = {"album": "MusicAlbum", "playlist": "Playlist", "artist": "MusicArtist"}
    # Per-kind cache file. Sharing a single "library.json" across all
    # kinds means every swap (Albums → Playlists → Artists) invalidates
    # the previous one's cache; per-kind files let each kind retain its
    # cached browse independently.
    CACHE_NAME = "library"

    def __init__(self, kind: str = "album", parent=None):
        super().__init__(parent)
        self.api = get_provider()
        self.kind = kind
        # Per-instance cache name so album / playlist / artist grids
        # each persist to their own file.
        self._cache_name = f"{self.CACHE_NAME}_{kind}"
        from modules.settings import get_settings

        s = get_settings()
        self._parent_id: str = ""
        self._genre_id: str = ""
        self._year: str = ""
        self._sort_by: str = s.library_sort_by or "SortName"
        self._sort_order: str = (
            "Descending" if s.library_sort_order == "descending" else "Ascending"
        )
        self._view_mode: str = s.library_view_mode

        # Pagination state.
        self._loaded_count: int = 0
        self._has_more: bool = False
        self._loading_more: bool = False
        self._auto_paginate: bool = False
        # Set by the cache-hit path when the cache was partial
        # (`complete: False`). Triggers a background page-by-page
        # pagination to fill out the rest of the library, so the next
        # cold launch renders everything from cache without paging.
        # Cleared when the tail is reached (_has_more flips False).
        self._completing_partial_cache: bool = False
        # Set by the cache-hit path to whether the loaded disk cache was
        # a *complete* multi-page browse. Gates the tail-growth probe in
        # _on_refresh_loaded: a complete cache that hasn't grown needs no
        # rebuild, but a complete cache can hide an album added PAST the
        # first page (the page-1 refresh signature can't see it), so we
        # probe the tail. Partial caches already self-heal via the
        # buffered backfill, so they don't need the probe.
        self._cache_was_complete: bool = False
        # Silent backfill buffer — when a cache hit lands a partial
        # cache we treat it as complete for the UI (no scroll
        # pagination, no "Loading more…" footer) and fetch the
        # remaining pages here. Items accumulate WITHOUT touching
        # the rendered model, so the user just sees their cached
        # items. When the tail is reached we save the combined
        # payload back to disk with complete=True — next launch
        # renders everything in one paint.
        self._partial_cache_buffer: List[Dict] = []
        # True while a silent fill/rebuild is in flight. Gates the
        # two paths so a cache-hit-triggered top-up doesn't try to
        # share the buffer with a refresh-triggered full rebuild.
        self._silent_fetch_in_flight: bool = False
        # Monotonic load-generation token. Every load_items() bumps it;
        # async result handlers + the auto-paginate cascade capture the
        # value live at dispatch and bail when it no longer matches, so a
        # second load_items() (e.g. _route_home AND _retry_empty_native_views
        # both firing on sign-in) cleanly supersedes the first instead of
        # running two concurrent pagination cascades that double-append every
        # page and corrupt the shared pagination offset (which both doubled
        # albums AND truncated the grid mid-library). See
        # session_active_dup_albums_bug memory.
        self._load_gen: int = 0

        # Cover-loading bookkeeping.
        self._covers_loaded: set = set()
        self._prefetch_idx: int = 0
        # Per-row cover-load failure count, so a flaky cover gets a
        # bounded number of idle-prefetch retries (COVER_RETRY_LIMIT)
        # before we give up on it.
        self._cover_retries: dict = {}
        # Rows whose cover load was DEFERRED because we were (auto-)
        # offline when it fired — the image gate short-circuits to a
        # synchronous error without touching the network. Kept marked
        # "loaded" so the normal passes skip them, but remembered here so
        # the offline→online transition re-fetches them instead of
        # leaving the tile permanently blank after a connectivity flap.
        self._cover_failed: set = set()

        # Refresh scope tracked across the cache hit → background
        # refresh round-trip so the refresh callback knows what scope
        # to validate against and persist to.
        self._refresh_scope: dict = {}

        # Letter → first-matching-row map for the alphabet jump.
        self._letter_to_row: Dict[str, int] = {}

        self.setObjectName("libraryGrid")
        self.setStyleSheet("""
            QWidget#libraryGrid,
            QWidget#libraryGrid QWidget,
            QWidget#libraryGrid QListView {
                background: transparent;
                border: none;
            }
        """)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, SPACE_LG, 0, 0)
        outer.setSpacing(0)

        # Model + delegates + view.
        self._model = _LibraryItemsModel(self)
        # Year line stays off for the main Albums browse — keeps
        # the grid quieter; artist-page still passes show_year=True
        # on its own _TileDelegate instance so the year is visible
        # there where it adds context.
        self._tile_delegate = _TileDelegate(
            kind,
            self,
            show_year=False,
        )
        self._row_delegate = _RowDelegate(kind, self)
        self._view = _LibraryListView(
            self._tile_delegate,
            self._row_delegate,
            self,
        )
        self._view.setModel(self._model)
        # Outer padding so the first row of tiles doesn't crowd the
        # window chrome and the last row doesn't slam into the bottom
        # bar. Matches the old grid's SPACE_XL horizontal padding.
        self._view.setViewportMargins(SPACE_XL, 0, SPACE_XL, SPACE_XL)
        install_autofade_scrollbars(self._view)
        # Focus proxy so Tab cycling into the content-section anchor
        # (the LibraryGrid itself) lands on the inner QListView —
        # which is where arrow nav + Enter + the focus ring live.
        self.setFocusProxy(self._view)

        # Restore the persisted view mode now that the view exists.
        if self._view_mode == "list":
            self._view.set_mode("list")

        # Signal forwarding from the view to host-level signals.
        self._view.play_requested.connect(self.play_requested.emit)
        self._view.browse_requested.connect(self.browse_requested.emit)
        self._view.artist_browse_requested.connect(self.artist_browse_requested.emit)
        self._view.year_browse_requested.connect(self.year_browse_requested.emit)

        # Body row: view + alphabet index sit side-by-side. Wrapped
        # in a QStackedWidget so the empty-state view can take over
        # the same slot when the load resolves to zero items.
        body_widget = QWidget()
        body_widget.setStyleSheet("background: transparent;")
        body = QHBoxLayout(body_widget)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._view, 1)
        self._alphabet = _AlphabetIndex()
        self._alphabet.jump_requested.connect(self._on_alphabet_jump)
        body.addWidget(self._alphabet)

        # Empty-state surface — shown whenever the model is empty
        # AFTER an initial load has completed. Copy is refreshed per
        # scope in _show_empty_state so genre/year filters get a
        # specific "no albums in this genre" headline instead of the
        # generic "library is empty" one.
        self._empty_state = EmptyState(
            glyph="♪",
            headline=self._empty_default_headline(),
            sub="",
            action_label="Refresh",
            parent=self,
        )
        self._empty_state.action_clicked.connect(
            self._on_empty_state_refresh,
        )
        self._initial_load_complete = False
        # Marks the grid dirty when offline mode flips while we're not
        # the currently-visible kind. ``showEvent`` consumes the flag
        # on next navigation so we refresh exactly once at view time.
        self._refresh_after_offline_toggle: bool = False

        self._content_stack = QStackedWidget(self)
        self._content_stack.setStyleSheet("background: transparent;")
        self._content_stack.addWidget(body_widget)
        self._content_stack.addWidget(self._empty_state)
        outer.addWidget(self._content_stack, 1)

        # "Loading more…" footer surfaces while a paginated next-page
        # fetch is in flight. Centered + caption-tier so it reads as
        # status info, not a tile. Hidden by default.
        self._loading_more_label = QLabel("Loading more…", self)
        self._loading_more_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._loading_more_label.setStyleSheet(
            f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)} padding: {SPACE_SM}px 0;"
        )
        self._loading_more_label.setVisible(False)
        outer.addWidget(self._loading_more_label)

        # Background prefetch trickle — fills covers outside the
        # viewport once the visible-cover pass is done.
        self._prefetch_timer = QTimer(self)
        self._prefetch_timer.setInterval(30)
        self._prefetch_timer.timeout.connect(self._prefetch_tick)

        # Single coalesced scroll handler. Trailing-edge debounce at
        # 16ms (~60Hz) so a kinetic scroll's 60-120Hz valueChanged
        # firehose doesn't fan out into three per-pixel handlers
        # (alphabet highlight, pagination, visible-cover load).
        self._scroll_coalesce = QTimer(self)
        self._scroll_coalesce.setSingleShot(True)
        self._scroll_coalesce.setInterval(16)
        self._scroll_coalesce.timeout.connect(self._on_scroll_coalesced)
        self._view.verticalScrollBar().valueChanged.connect(self._on_scroll_raw)

        # First viewport resize kicks an initial cover load — the
        # viewport's height is 0 until first show, and without this
        # the initial batch lands to an empty visible range.
        self._view.viewport().installEventFilter(self)

        # Async result handlers — items land on the GUI thread.
        self._items_loaded.connect(self._on_items_loaded)
        self._refresh_loaded.connect(self._on_refresh_loaded)

        # Live-accent: delegates re-read TEXT on every paint, so a
        # theme change just needs to invalidate the viewport.
        from modules.player_state import PlayerBus

        PlayerBus.get().theme_changed.connect(self._view.viewport().update)
        # Cross-DPR refresh — clear cover cache and rerun visible load.
        PlayerBus.get().dpr_changed.connect(self._on_dpr_changed)
        # Re-render when offline mode flips — the grid swaps between
        # server-backed and downloads-only sources. QueuedConnection so
        # the heavy refetch lands on the next event-loop tick instead
        # of stalling the GUI thread inside the bus emit chain.
        PlayerBus.get().offline_mode_changed.connect(
            self._on_offline_mode_changed,
            Qt.ConnectionType.QueuedConnection,
        )
        # Settings → "Refresh album art" → drop the per-row loaded set
        # so visible tiles re-issue their fetches against the (now
        # cleared) caches and pick up server-side art changes.
        PlayerBus.get().image_cache_cleared.connect(
            self._on_image_cache_cleared,
        )

    # ── Backwards-compatible accessors ────────────────────────────────

    @property
    def _tiles(self) -> List[Dict]:
        """jellytoast.py reads this to gate re-loads on emptiness.
        Old implementation exposed a list of tile widgets; the new
        one returns the model's item dicts. Same shape (a list,
        truthy when loaded) — call sites only check truthiness."""
        return self._model.items()

    # ── Scroll plumbing ───────────────────────────────────────────────

    def eventFilter(self, obj, event):
        if obj is self._view.viewport():
            if event.type() == QEvent.Type.Resize:
                if not self._scroll_coalesce.isActive():
                    self._scroll_coalesce.start()
        return super().eventFilter(obj, event)

    def _on_scroll_raw(self, *_):
        if not self._scroll_coalesce.isActive():
            self._scroll_coalesce.start()

    def _on_scroll_coalesced(self):
        bar = self._view.verticalScrollBar()
        self._update_alphabet_highlight()
        self._maybe_load_more(bar.value())
        self._load_visible_covers()

    def _on_dpr_changed(self):
        if self._model.rowCount() == 0:
            return
        self._model.clear_covers()
        self._covers_loaded.clear()
        self._cover_retries.clear()
        self._cover_failed.clear()
        self._prefetch_idx = 0
        self._load_visible_covers()
        if not self._prefetch_timer.isActive():
            self._prefetch_timer.start()

    def _on_image_cache_cleared(self):
        # Mirror the DPR-change reset: drop our per-row "already
        # loaded" tracking + the model's cached pixmaps, then re-issue
        # the visible-window fetches against the now-cleared caches.
        if self._model.rowCount() == 0:
            return
        self._model.clear_covers()
        self._covers_loaded.clear()
        self._cover_retries.clear()
        self._cover_failed.clear()
        self._prefetch_idx = 0
        self._load_visible_covers()
        if not self._prefetch_timer.isActive():
            self._prefetch_timer.start()

    def focus_first_item(self):
        """Drop keyboard focus on the inner view's first visible
        tile and engage keyboard mode. Called by the app-level
        Down filter when the user presses Down with focus on
        chrome (Home button, etc.) — keyboard parity with the
        suggestions surface's same-named method."""
        if self._view is None or self._model.rowCount() == 0:
            return
        seed = self._view.indexAt(self._view.viewport().rect().topLeft())
        if not seed.isValid():
            seed = self._model.index(0, 0)
        self._view._keyboard_mode = True
        self._view.setCurrentIndex(seed)
        self._view.setFocus(Qt.FocusReason.OtherFocusReason)
        self._view.viewport().update()

    def _rearm_failed_covers(self):
        """Re-arm covers deferred during an offline window (see
        ``_on_err``). Mirrors the ``_on_dpr_changed`` /
        ``_on_image_cache_cleared`` reset, but only for the rows that
        were stranded: drop them back to eligible, clear their retry
        tally, and re-issue the visible-window fetches; the idle prefetch
        fills the rest."""
        if not self._cover_failed:
            return
        for r in self._cover_failed:
            self._covers_loaded.discard(r)
            self._cover_retries.pop(r, None)
        self._cover_failed.clear()
        if self._model.rowCount() == 0:
            return
        self._prefetch_idx = 0
        self._load_visible_covers()
        if not self._prefetch_timer.isActive():
            self._prefetch_timer.start()

    def showEvent(self, event):
        """Drain the deferred offline-mode refresh on first navigation
        back to a hidden grid. The inner ``_LibraryListView`` has its
        own showEvent for layout work; this one runs on the outer
        widget where the dirty flag lives."""
        super().showEvent(event)
        if self._refresh_after_offline_toggle:
            self._refresh_after_offline_toggle = False
            self.load_items(self._parent_id, self._genre_id, self._year)

    def set_view_mode(self, mode: str):
        """Switch between "grid" (multi-column tile grid) and "list"
        (single-column row stack). Persists the choice and rerenders
        the currently-loaded items in the new mode without re-fetching
        from the server. No-op when the mode is unchanged."""
        if mode not in ("grid", "list") or mode == self._view_mode:
            return
        self._view_mode = mode
        from modules.settings import get_settings as _gs

        _gs().library_view_mode = mode
        self._view.set_mode(mode)
        # Visible-row count changes with the new cell height — recompute
        # the cover-load window.
        if self._model.rowCount() > 0:
            QTimer.singleShot(0, self._load_visible_covers)

    # ── Empty-state surface ───────────────────────────────────────────

    def _empty_default_headline(self) -> str:
        if self.kind == "playlist":
            return "No playlists yet"
        if self.kind == "artist":
            return "No artists yet"
        return "No albums yet"

    def _empty_copy_for_scope(self) -> "tuple[str, str]":
        """Headline + sub-line tuned to the current filter scope. A
        genre/year browse with no matches needs a different framing
        ('try another genre') than the root browse ('library is
        empty')."""
        kind = self.kind
        if self._genre_id:
            return (
                f"No {kind}s in this genre",
                "Try a different genre or refresh the library.",
            )
        if self._year:
            return (
                f"No {kind}s from {self._year}",
                "Try a different year or refresh the library.",
            )
        return (
            self._empty_default_headline(),
            "Your library may still be loading, or it's empty. Refresh to retry.",
        )

    def _show_empty_state(self):
        headline, sub = self._empty_copy_for_scope()
        self._empty_state.set_state(headline=headline, sub=sub)
        self._content_stack.setCurrentIndex(1)
        self._alphabet.setVisible(False)
        self._loading_more_label.setVisible(False)

    def _on_empty_state_refresh(self):
        """User clicked Refresh on the empty-state — drop the disk
        cache for this kind and re-trigger the load with the current
        scope. Clearing the cache means a legitimately-empty cached
        result can't lock us out of a retry."""
        try:
            disk_cache.clear(self._cache_name)
        except Exception:
            pass
        self._initial_load_complete = False
        self._content_stack.setCurrentIndex(0)
        self.load_items(self._parent_id, self._genre_id, self._year)

    # ── Cover loading ─────────────────────────────────────────────────

    def _visible_row_range(self) -> "tuple[int, int]":
        """Inclusive-exclusive [first, last) row indices currently in
        (or near) the viewport. A small over-fetch buffer means a
        moderate kinetic-scroll fling stays ahead of the loader.

        Returns ``(0, 0)`` (empty) when the layout hasn't computed yet —
        both viewport-corner ``indexAt`` probes return invalid right
        after a ``set_items`` model reset, before the QListView has
        laid out new cells. The OLD fallback there was ``(0, rc)``
        (treat unknown as "all rows"), which made ``_load_visible_covers``
        fire a cover load for every item in the library — 292 fires
        ×~25 ms each = ~6 s of GUI-thread blocking on every
        offline-mode toggle. Empty-range now means "try again later";
        the caller re-schedules and the prefetch timer fills any gaps."""
        rc = self._model.rowCount()
        if rc == 0:
            return 0, 0
        vp = self._view.viewport()
        h = vp.height()
        w = vp.width()
        if h <= 0 or w <= 0:
            return 0, 0
        # Sample the corners of the viewport. With uniform item sizes
        # and a left-to-right top-to-bottom flow, every item between
        # top-left and bottom-right (in model order) is visible.
        top_left = self._view.indexAt(QPoint(4, 0))
        bot_right = self._view.indexAt(QPoint(w - 4, h - 1))
        # Layout not yet computed — neither probe lands on a real row.
        # Bail with empty range so the caller can retry once the view
        # has had a chance to lay items out.
        if not top_left.isValid() and not bot_right.isValid():
            return 0, 0
        first = top_left.row() if top_left.isValid() else 0
        last = bot_right.row() if bot_right.isValid() else rc - 1
        if first < 0:
            first = 0
        if last < 0:
            last = rc - 1
        # Buffer rows on each side so off-screen tiles warm just
        # before they reach the viewport.
        buf = 6 if self._view_mode == "list" else 12
        first = max(0, first - buf)
        last = min(rc - 1, last + buf)
        if first > last:
            return 0, 0
        return first, last + 1

    @Slot()
    def _load_visible_covers(self):
        rc = self._model.rowCount()
        if rc == 0:
            return
        first, last = self._visible_row_range()
        if first >= last:
            # Layout not ready (just after a model reset) — retry at
            # a slightly larger delay so QListView has time to compute
            # cell positions. Bounded so we don't loop forever if the
            # view stays empty for some reason; the prefetch timer is
            # the safety net once we give up.
            tries = getattr(self, "_visible_retry_tries", 0)
            if tries < 4:
                self._visible_retry_tries = tries + 1
                QTimer.singleShot(50, self._load_visible_covers)
            return
        self._visible_retry_tries = 0
        for row in range(first, last):
            if row in self._covers_loaded:
                continue
            self._fire_cover_load(row)

    def _fire_cover_load(self, row: int, priority: str = "normal"):
        items = self._model.items()
        if not (0 <= row < len(items)):
            return
        item = items[row]
        cover_id = item.get("Id", "")
        if not cover_id:
            self._covers_loaded.add(row)
            return
        dpr = screen_dpr(self)
        target = max(
            _TileDelegate.COVER_SIZE,
            int(round(_TileDelegate.COVER_SIZE * dpr)),
        )
        radius_phys = int(round(_TileDelegate.COVER_RADIUS * dpr))
        # Fetch at the fixed DPR-independent source size — see
        # _COVER_SOURCE_PX. load_image_async still renders the
        # pixmap at the DPR-correct `target`, but the raw source it
        # caches (keyed by album id, DPR-independent) is now big
        # enough to derive any DPR's variant locally, so a DPR
        # change never re-hits the network.
        cover_url = self.api.get_image_url(
            cover_id,
            "Primary",
            self._COVER_SOURCE_PX,
        )
        if not cover_url:
            self._covers_loaded.add(row)
            return
        self._covers_loaded.add(row)

        def _on_pix(pix, r=row):
            self._model.set_cover(r, pix)

        def _on_err(r=row):
            # A cover load failed. Distinguish two cases:
            #   • We're (auto-)offline → the image gate short-circuited
            #     synchronously without hitting the network. This isn't a
            #     real failure, just a deferral, so DON'T spend the retry
            #     budget (a single connectivity flap fires this gate for
            #     every uncached cover and would otherwise burn all the
            #     retries in one tick). Remember it for re-fetch when we
            #     come back online; leave it marked loaded for now so the
            #     normal passes don't spin on it.
            #   • We're online → a genuine fetch failure. Up to
            #     COVER_RETRY_LIMIT times, drop the index from the loaded
            #     set and nudge the idle prefetch pass to revisit it — so
            #     a grid sitting open with blank covers (cold server right
            #     after login) keeps filling in without the user having to
            #     scroll. Past the cap we leave it marked loaded and stop.
            from modules import offline as _offline

            if _offline.is_offline_mode():
                self._cover_failed.add(r)
                return
            tries = self._cover_retries.get(r, 0) + 1
            self._cover_retries[r] = tries
            if tries >= self.COVER_RETRY_LIMIT:
                return
            self._covers_loaded.discard(r)
            self._prefetch_idx = min(self._prefetch_idx, r)
            if not self._prefetch_timer.isActive():
                self._prefetch_timer.start()

        load_image_async(
            f"{cover_id}|{self.kind}tile",
            cover_url,
            target,
            target,
            _on_pix,
            rounded_radius=radius_phys,
            on_error=_on_err,
            priority=priority,
        )

    @Slot()
    def _prefetch_tick(self):
        rc = self._model.rowCount()
        if rc == 0:
            self._prefetch_timer.stop()
            return
        while self._prefetch_idx < rc and self._prefetch_idx in self._covers_loaded:
            self._prefetch_idx += 1
        if self._prefetch_idx >= rc:
            self._prefetch_timer.stop()
            return
        i = self._prefetch_idx
        self._prefetch_idx += 1
        self._fire_cover_load(i, priority="low")

    # ── Alphabet jump + scroll-driven highlight ───────────────────────

    def _resort_items_by_article(self, items: "List[Dict]") -> "List[Dict]":
        first_key = (self._effective_sort() or "").split(",", 1)[0]
        descending = self._sort_order == "Descending"
        if first_key == "AlbumArtist":

            def key(it: dict) -> str:
                v = it.get("AlbumArtist", "") or ""
                if isinstance(v, list):
                    v = v[0] if v else ""
                return article_stripped_key(v)

            return sorted(items, key=key, reverse=descending)
        if first_key == "SortName":

            def key2(it: dict) -> str:
                v = it.get("SortName") or it.get("Name") or ""
                return article_stripped_key(v)

            return sorted(items, key=key2, reverse=descending)
        return items

    @staticmethod
    def _alphabet_field_for_sort(sort_by: str):
        first_key = (sort_by or "").split(",", 1)[0]
        if first_key == "SortName":
            return ""
        if first_key == "AlbumArtist":
            return "AlbumArtist"
        return None

    def _index_letter_for(self, item: dict) -> str:
        field = self._alphabet_field_for_sort(self._effective_sort())
        if field is None:
            return ""
        if field:
            val = item.get(field, "") or ""
            if isinstance(val, list):
                val = val[0] if val else ""
        else:
            val = item.get("SortName") or item.get("Name") or ""
        return first_letter(val)

    @Slot(str)
    def _on_alphabet_jump(self, letter: str):
        alphabet = _AlphabetIndex.LETTERS
        target = (letter or "").upper()
        if target not in alphabet:
            return
        row = None
        for i in range(alphabet.index(target), -1, -1):
            cand = self._letter_to_row.get(alphabet[i])
            if cand is not None and 0 <= cand < self._model.rowCount():
                row = cand
                break
        if row is None:
            return
        idx = self._model.index(row, 0)
        # Set the scrollbar value directly via cell-math rather than
        # going through scrollTo(). In IconMode + Wayland, scrollTo can
        # leave the scrollbar value lagging the visual position, and the
        # app-level `SmoothScrollFilter` reads `bar.value()` as its
        # source of truth — direct setValue keeps them in lockstep.
        # Also invalidate any in-flight wheel animation: the filter
        # caches its current animation target per-bar and computes new
        # wheel targets relative to it, so a stale target would animate
        # the view straight back to the pre-click position on the next
        # wheel notch.
        sb = self._view.verticalScrollBar()
        cols = max(1, getattr(self._view, "_last_cols", 1) or 1)
        if self._view_mode == "list":
            grid_size = getattr(self._view, "_last_grid_size", None)
            cell_h = grid_size.height() if grid_size and not grid_size.isEmpty() else 0
        else:
            cell_h = self._view._tile_delegate.CELL_H
        if cell_h > 0:
            target_y = min((row // cols) * cell_h, sb.maximum())
            sb.setValue(target_y)
        else:
            self._view.scrollTo(idx, QAbstractItemView.ScrollHint.PositionAtTop)
        # Clear any cached wheel-animation target on this bar so the
        # next wheel notch computes its delta from the new bar value.
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        sf = getattr(app, "_smooth_scroll", None)
        if sf is not None:
            sf.invalidate(sb)

    def _update_alphabet_highlight(self):
        """Highlight the rail letter for the top-visible item.

        We compute the top row from cell-math (scrollbar.value() //
        cell_h × cols), NOT from QListView.indexAt(). Reason:
        indexAt() in IconMode + Wrapping + UniformItemSizes is
        unreliable on Wayland Qt 6 — it can return invalid for points
        that visually fall inside a tile, especially in the centre
        column. Cell-math is also the inverse of what
        `_on_alphabet_jump` uses to convert (row, cols, cell_h) → y,
        so the two stay in lockstep by construction."""
        if not self._alphabet.isVisible():
            return
        if self._model.rowCount() == 0:
            return
        sb = self._view.verticalScrollBar()
        y = sb.value()
        cols = max(1, getattr(self._view, "_last_cols", 1) or 1)
        if self._view_mode == "list":
            grid_size = getattr(self._view, "_last_grid_size", None)
            cell_h = grid_size.height() if grid_size and not grid_size.isEmpty() else 0
        else:
            cell_h = self._view._tile_delegate.CELL_H
        if cell_h <= 0:
            return
        top_row_idx = (y // cell_h) * cols
        if top_row_idx >= self._model.rowCount():
            return
        item = self._model._items[top_row_idx] if top_row_idx < len(self._model._items) else None
        if not item:
            return
        letter = self._index_letter_for(item)
        if letter:
            self._alphabet.set_current_letter(letter)
