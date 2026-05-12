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
    Qt, QSize, QTimer, Signal, Slot,
    QPropertyAnimation, QEasingCurve,
    QAbstractListModel, QModelIndex, QPoint, QRect, QRectF,
)
from PySide6.QtGui import (
    QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPalette, QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QWidget, QFrame, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QSizePolicy, QGraphicsOpacityEffect,
    QAbstractItemView, QListView, QStyle, QStyledItemDelegate,
)

from modules import disk_cache
from modules.async_io import run_async
from modules.providers import get_provider
from modules.sort_utils import (
    article_stripped_key, first_letter,
)
from modules.ui_helpers import (
    load_image_async, install_autofade_scrollbars,
    screen_dpr, scale_pixmap_for_dpr,
    TEXT, TEXT_DIM, TEXT_FAINT,
)
from modules.icons import icon
from modules.design_tokens import (
    TYPE_BODY, TYPE_CAPTION, type_qss,
    SPACE_XS, SPACE_SM, SPACE_LG, SPACE_XL,
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

    play_requested = Signal(str)    # item_id
    browse_requested = Signal(str)  # item_id
    # Album tiles only — clicking the artist subtitle routes to the
    # artist page, clicking the year routes to a year-filtered grid.
    # Empty payload means "no actionable target" (no artist id / no
    # year metadata) and the host should ignore it.
    artist_browse_requested = Signal(str)  # artist_id
    year_browse_requested = Signal(int)    # year

    COVER_SIZE = 180
    OVERLAY_SIZE = 56

    # Class-level flag toggled by the parent LibraryGrid while the scroll
    # bar is actively moving. When True, reveal() snaps to full opacity
    # instead of running the 180ms QGraphicsOpacityEffect fade — animating
    # many tiles concurrently through QGraphicsEffect produces half-
    # painted frames and a brief white flash on Wayland during scroll.
    # See feedback_qgraphicseffect_scroll memory for the underlying issue.
    SCROLL_BUSY: bool = False

    def __init__(self, item: Dict, kind: str = "album",
                 show_subtitle: bool = True, show_year: bool = True,
                 parent=None):
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
        self._show_play_overlay = (kind != "artist")
        self.setObjectName("libraryTile")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        # StrongFocus lets the tile receive keyboard focus via Tab and
        # programmatic setFocus() (e.g. SearchView's "down arrow → first
        # result"). The :focus stylesheet rule below paints a subtle
        # backdrop so users can see which tile is focused.
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setFixedWidth(self.COVER_SIZE)
        self.setStyleSheet("""
            QFrame#libraryTile { background: transparent; border: none; }
            QFrame#libraryTile:focus { background: rgba(255, 255, 255, 0.06); }
            QFrame#libraryTile QLabel { background: transparent; }
        """)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(SPACE_SM)

        # Cover: a QFrame as a fixed-size container so we can position
        # the play overlay absolutely inside it. The QLabel inside paints
        # the artwork; the QPushButton sits on top.
        self._cover_box = QFrame(self)
        self._cover_box.setFixedSize(self.COVER_SIZE, self.COVER_SIZE)
        self._cover_box.setStyleSheet("""
            QFrame {
                background: rgba(255, 255, 255, 0.04);
                border-radius: 8px;
            }
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
        self._play_overlay.setStyleSheet("""
            QPushButton {
                background: rgba(0, 0, 0, 0.65);
                color: white;
                border: 2px solid rgba(255, 255, 255, 0.85);
                border-radius: 28px;
            }
            QPushButton:hover { background: rgba(0, 0, 0, 0.85); }
            QPushButton:pressed { background: rgba(0, 0, 0, 0.95); }
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
        self._title.setStyleSheet(
            f"color: {TEXT}; {type_qss(TYPE_BODY)} font-weight: 600;"
        )
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
                _ClickableLabel(year_text, parent=self) if year_int
                else QLabel(year_text, parent=self)
            )
            self._year.setStyleSheet(
                f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)}"
            )
            self._year.setAlignment(Qt.AlignmentFlag.AlignHCenter)
            self._year.setVisible(bool(year_text))
            if year_int:
                self._year.clicked.connect(
                    lambda y=year_int: self.year_browse_requested.emit(y)
                )
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
                self._compute_subtitle(), parent=self,
            )
            self._subtitle.clicked.connect(
                lambda aid=artist_id:
                self.artist_browse_requested.emit(aid)
            )
        else:
            self._subtitle = _ElidingLabel(
                self._compute_subtitle(), parent=self,
            )
        self._subtitle.setStyleSheet(
            f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}"
        )
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
        # Honor the user's `library_tile_fade` preference. When off,
        # snap to fully-opaque immediately and detach the effect on
        # the same path the animation finish would take. The 180ms
        # fade is a polish touch but not load-bearing — instant reveal
        # is fine and slightly cheaper.
        from modules.settings import get_settings as _gs
        # Snap when the user has the fade off, or when the parent grid
        # is mid-scroll (animating dozens of tiles through a graphics
        # effect mid-scroll is what produces the white-flash artifact).
        if not _gs().library_tile_fade or LibraryTile.SCROLL_BUSY:
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
        return self._item.get("AlbumArtist") or ", ".join(
            self._item.get("AlbumArtists", []) or []
        ) or ""

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
        # Local imports keep tile construction lightweight when many
        # tiles are created in a chunk burst.
        from modules.async_io import run_async
        from modules.providers import get_provider
        api = get_provider()
        fetch_tracks = (
            api.get_playlist_items if self._kind == "playlist"
            else api.get_album_tracks
        )
        run_async(
            api.get_item, self._item_id,
            on_result=lambda _r: None, on_error=lambda _e: None,
        )
        run_async(
            fetch_tracks, self._item_id,
            on_result=lambda _r: None, on_error=lambda _e: None,
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
        from modules.providers import get_provider
        api = get_provider()
        url = api.get_image_url(self._item_id, "Primary", 256)
        if not url:
            return
        # Discard callback — we're just populating the cache.
        load_image_async(
            f"{self._item_id}|npbar", url, 256, 256,
            lambda _pix: None, rounded_radius=0,
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


# ── Row ─────────────────────────────────────────────────────────────────

class LibraryRow(QFrame):
    """List-mode counterpart to LibraryTile. Mirrors the same signal
    surface and `set_cover` / `reveal` contract so LibraryGrid can
    swap widget types per `view_mode` without changing its scaffolding.
    Single horizontal row: cover · title + subtitle · year (album).

    Subtitle (album-artist) and year are clickable when actionable —
    same `artist_browse_requested` / `year_browse_requested` semantics
    as the tile. Whole-row click → `browse_requested`."""

    play_requested = Signal(str)
    browse_requested = Signal(str)
    artist_browse_requested = Signal(str)
    year_browse_requested = Signal(int)

    COVER_SIZE = 36
    ROW_HEIGHT = 48

    def __init__(self, item: Dict, kind: str = "album",
                 show_subtitle: bool = True, parent=None):
        super().__init__(parent)
        self._item = item
        self._kind = kind
        self._item_id = item.get("Id", "")
        self._show_subtitle = show_subtitle

        self.setObjectName("libraryRow")
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(self.ROW_HEIGHT)
        self.setStyleSheet("""
            QFrame#libraryRow {
                background: transparent;
                border: none;
                border-radius: 4px;
            }
            QFrame#libraryRow:hover {
                background: rgba(255, 255, 255, 0.05);
            }
            QFrame#libraryRow QLabel { background: transparent; }
        """)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(SPACE_SM, SPACE_XS, SPACE_SM, SPACE_XS)
        layout.setSpacing(SPACE_SM)

        # Cover slot — small square, painted by `set_cover` (or left
        # as the placeholder background when the item has no art).
        self._cover_box = QFrame(self)
        self._cover_box.setFixedSize(self.COVER_SIZE, self.COVER_SIZE)
        self._cover_box.setStyleSheet("""
            QFrame {
                background: rgba(255, 255, 255, 0.04);
                border-radius: 4px;
            }
        """)
        self._cover = QLabel(self._cover_box)
        self._cover.setFixedSize(self.COVER_SIZE, self.COVER_SIZE)
        self._cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cover.setStyleSheet("background: transparent;")
        layout.addWidget(self._cover_box)

        # Text column: title + subtitle stacked, takes remaining width.
        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(2)
        self._title = _ElidingLabel(item.get("Name", "Unknown"), parent=self)
        self._title.setStyleSheet(
            f"color: {TEXT}; {type_qss(TYPE_BODY)} font-weight: 600;"
        )
        text_col.addWidget(self._title)

        artist_id = self._artist_id_for_album() if kind == "album" else ""
        sub_text = self._compute_subtitle()
        if artist_id and sub_text:
            self._subtitle = _ClickableElidingLabel(sub_text, parent=self)
            self._subtitle.clicked.connect(
                lambda aid=artist_id:
                self.artist_browse_requested.emit(aid)
            )
        else:
            self._subtitle = _ElidingLabel(sub_text, parent=self)
        self._subtitle.setStyleSheet(
            f"color: {TEXT_DIM}; {type_qss(TYPE_CAPTION)}"
        )
        self._subtitle.setVisible(self._show_subtitle and bool(sub_text))
        text_col.addWidget(self._subtitle)
        layout.addLayout(text_col, 1)

        # Year — albums only; clickable when present.
        if kind == "album":
            year_text = self._compute_year()
            year_int = int(year_text) if year_text.isdigit() else 0
            cls = _ClickableLabel if year_int else QLabel
            self._year = cls(year_text, parent=self)
            self._year.setAlignment(
                Qt.AlignmentFlag.AlignVCenter
                | Qt.AlignmentFlag.AlignRight
            )
            self._year.setStyleSheet(
                f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)}"
            )
            self._year.setFixedWidth(60)
            self._year.setVisible(bool(year_text))
            if year_int:
                self._year.clicked.connect(
                    lambda y=year_int: self.year_browse_requested.emit(y)
                )
            layout.addWidget(self._year)

        # Born hidden + opacity-effect — same fade-in semantics as
        # LibraryTile so a freshly-loaded list reveals as covers
        # land instead of popping in unstyled.
        self.setVisible(False)
        self._opacity = QGraphicsOpacityEffect(self)
        self._opacity.setOpacity(0.0)
        self.setGraphicsEffect(self._opacity)
        self._revealed = False
        # Set by LibraryGrid._clear_tiles before deleteLater so an
        # in-flight cover-load callback that lands after teardown
        # can early-return instead of touching the dead C++ side.
        self._dead = False

    # The compute helpers + reveal/prewarm/click-handlers are the same
    # shape as LibraryTile's. Duplicated rather than refactored into a
    # base class because the layouts diverge enough that a shared
    # ancestor would still need overrides for almost every method.

    def _artist_id_for_album(self) -> str:
        for field in ("AlbumArtists", "ArtistItems"):
            arr = self._item.get(field) or []
            if arr and isinstance(arr, list):
                first = arr[0] or {}
                aid = first.get("Id") if isinstance(first, dict) else ""
                if aid:
                    return aid
        return ""

    def _compute_year(self) -> str:
        y = self._item.get("ProductionYear")
        if y:
            return str(y)
        pd = (self._item.get("PremiereDate") or "").strip()
        return pd[:4] if pd[:4].isdigit() else ""

    def _compute_subtitle(self) -> str:
        if self._kind == "playlist":
            count = self._item.get("ChildCount") or 0
            return f"{count} tracks" if count != 1 else "1 track"
        if self._kind == "artist":
            genres = [g for g in (self._item.get("Genres") or []) if g]
            return genres[0] if genres else ""
        return self._item.get("AlbumArtist") or ", ".join(
            self._item.get("AlbumArtists", []) or []
        ) or ""

    def reveal(self):
        if self._dead or self._revealed:
            return
        self._revealed = True
        from modules.settings import get_settings as _gs
        # Match LibraryTile: snap during active scroll so the row's
        # opacity effect doesn't half-paint mid-scroll.
        if not _gs().library_tile_fade or LibraryTile.SCROLL_BUSY:
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
        self._reveal_anim = anim
        anim.start()

    def _drop_opacity_effect(self):
        if self._opacity is not None:
            self.setGraphicsEffect(None)
            self._opacity = None

    @Slot(object)
    def set_cover(self, pix: QPixmap):
        if self._dead or pix is None or pix.isNull():
            return
        # Cache hands us a tile-sized (180-px) pixmap; downscale to
        # row size with HiDPI awareness so list mode reuses the same
        # cache entries as grid mode (no extra network on toggle).
        self._cover.setPixmap(scale_pixmap_for_dpr(pix, self.COVER_SIZE))
        self.reveal()

    def enterEvent(self, e):
        super().enterEvent(e)
        self.prewarm_detail()

    def prewarm_detail(self):
        if self._kind not in ("album", "playlist"):
            return
        if getattr(self, "_prewarm_done", False):
            return
        if not self._item_id:
            return
        self._prewarm_done = True
        from modules.async_io import run_async
        from modules.providers import get_provider
        api = get_provider()
        fetch_tracks = (
            api.get_album_tracks if self._kind == "album"
            else api.get_playlist_items
        )
        run_async(
            api.get_item, self._item_id,
            on_result=lambda _r: None, on_error=lambda _e: None,
        )
        run_async(
            fetch_tracks, self._item_id,
            on_result=lambda _r: None, on_error=lambda _e: None,
        )

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.browse_requested.emit(self._item_id)
        super().mousePressEvent(e)


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
            btn.clicked.connect(
                lambda _checked=False, c=ch: self.jump_requested.emit(c)
            )
            layout.addWidget(btn, 1)
            self._buttons[ch] = btn

    @staticmethod
    def _btn_style(active: bool) -> str:
        if active:
            return (
                f"QPushButton {{ background: transparent; color: {TEXT}; "
                "border: none; padding: 0; font-size: 9px; font-weight: 700; }}"
                "QPushButton:hover { color: white; }"
            )
        return (
            "QPushButton { background: transparent; color: rgba(255,255,255,0.30); "
            "border: none; padding: 0; font-size: 9px; }"
            "QPushButton:hover { color: white; }"
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
    Delegates paint from two custom roles — ItemRole returns the source
    dict, CoverRole returns the loaded pixmap (None until it lands).
    Single-shot ``set_items`` replaces the chunked widget-build the old
    implementation needed; ``append_items`` powers paginated tails."""

    ItemRole = Qt.ItemDataRole.UserRole + 1
    CoverRole = Qt.ItemDataRole.UserRole + 2

    def __init__(self, parent=None):
        super().__init__(parent)
        self._items: List[Dict] = []
        self._covers: Dict[int, QPixmap] = {}

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
        return None

    def items(self) -> List[Dict]:
        return self._items

    def set_items(self, items: List[Dict]):
        self.beginResetModel()
        self._items = list(items)
        self._covers = {}
        self.endResetModel()

    def append_items(self, new_items: List[Dict]):
        if not new_items:
            return
        first = len(self._items)
        last = first + len(new_items) - 1
        self.beginInsertRows(QModelIndex(), first, last)
        self._items.extend(new_items)
        self.endInsertRows()

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
    CELL_W = 196   # COVER_SIZE + 16 horizontal gap
    CELL_H = 264   # cover + 8 + title 22 + 4 + year 18 + 4 + subtitle 18 + ~12 bottom margin
    COVER_RADIUS = 8

    def __init__(self, kind: str, parent=None):
        super().__init__(parent)
        self._kind = kind
        self._show_play_overlay = (kind != "artist")

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
        # live-theme changes flow through without per-delegate caches.
        from modules.ui_helpers import TEXT as _TEXT

        # Center the 180px content column inside the (196px) cell.
        content_x = rect.x() + (rect.width() - self.COVER_SIZE) // 2
        cover_rect = QRect(content_x, rect.y(),
                           self.COVER_SIZE, self.COVER_SIZE)

        # Cover paint — rounded square. Placeholder rect for items that
        # haven't loaded artwork yet, or have no artwork available.
        if cover is not None and not cover.isNull():
            path = QPainterPath()
            path.addRoundedRect(QRectF(cover_rect),
                                self.COVER_RADIUS, self.COVER_RADIUS)
            painter.save()
            painter.setClipPath(path)
            painter.drawPixmap(cover_rect, cover)
            painter.restore()
        else:
            path = QPainterPath()
            path.addRoundedRect(QRectF(cover_rect),
                                self.COVER_RADIUS, self.COVER_RADIUS)
            painter.fillPath(path, QColor(255, 255, 255, 10))

        # Hover overlay: dark circle with the play glyph. Same look
        # as the legacy QPushButton overlay — 65% black fill, 2px
        # white border (85% alpha), centered triangle glyph. Skipped
        # for artists ("play an artist" has no canonical meaning).
        if (self._show_play_overlay
                and option.state & QStyle.StateFlag.State_MouseOver):
            ov_rect = self.overlay_rect_for(rect)
            painter.save()
            # Dark fill.
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(0, 0, 0, 165))
            painter.drawEllipse(ov_rect)
            # 2px white border. Inset by 1px so the stroke draws
            # inside the circle rather than half-outside.
            border = QPen(QColor(255, 255, 255, 217))
            border.setWidth(2)
            painter.setPen(border)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawEllipse(ov_rect.adjusted(1, 1, -1, -1))
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
            painter.setBrush(QColor(255, 255, 255, 240))
            painter.drawPath(tri)
            painter.restore()

        # Title — bold body, centered, eliding.
        title_y = cover_rect.bottom() + SPACE_SM + 1
        title_h = 22
        title_rect = QRect(rect.x(), title_y, rect.width(), title_h)
        title_font = QFont(painter.font())
        title_font.setPixelSize(TYPE_BODY.size_px)
        title_font.setBold(True)
        painter.setFont(title_font)
        fm_title = QFontMetrics(title_font)
        painter.setPen(QColor(_TEXT))
        title = item.get("Name") or "Unknown"
        painter.drawText(
            title_rect,
            int(Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter),
            fm_title.elidedText(title, Qt.TextElideMode.ElideRight,
                                title_rect.width() - 8),
        )

        # Caption font for year + subtitle.
        caption_font = QFont(painter.font())
        caption_font.setPixelSize(TYPE_CAPTION.size_px)
        caption_font.setBold(False)
        painter.setFont(caption_font)
        fm_cap = QFontMetrics(caption_font)

        # Year — albums only, sits between title and subtitle.
        year_y = title_rect.bottom() + 2
        year_h = 18
        year_text = ""
        if self._kind == "album":
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
                int(Qt.AlignmentFlag.AlignHCenter
                    | Qt.AlignmentFlag.AlignVCenter),
                year_text,
            )
            subtitle_y = year_rect.bottom() + 2
        else:
            subtitle_y = year_y

        # Subtitle — kind-dependent. Albums show the artist; playlists
        # show track count; artists show first genre.
        subtitle = _compute_subtitle(item, self._kind)
        if subtitle:
            subtitle_rect = QRect(rect.x(), subtitle_y,
                                  rect.width(), year_h)
            painter.setPen(QColor(_TEXT))
            painter.drawText(
                subtitle_rect,
                int(Qt.AlignmentFlag.AlignHCenter
                    | Qt.AlignmentFlag.AlignVCenter),
                fm_cap.elidedText(subtitle, Qt.TextElideMode.ElideRight,
                                  subtitle_rect.width() - 8),
            )

        painter.restore()

    def overlay_rect_for(self, cell_rect: QRect) -> QRect:
        """Center the play overlay over the cover's center."""
        content_x = cell_rect.x() + (cell_rect.width() - self.COVER_SIZE) // 2
        cover_cx = content_x + self.COVER_SIZE // 2
        cover_cy = cell_rect.y() + self.COVER_SIZE // 2
        return QRect(
            cover_cx - self.OVERLAY_SIZE // 2,
            cover_cy - self.OVERLAY_SIZE // 2,
            self.OVERLAY_SIZE, self.OVERLAY_SIZE,
        )

    def cover_rect_for(self, cell_rect: QRect) -> QRect:
        content_x = cell_rect.x() + (cell_rect.width() - self.COVER_SIZE) // 2
        return QRect(content_x, cell_rect.y(),
                     self.COVER_SIZE, self.COVER_SIZE)

    def subtitle_rect_for(self, cell_rect: QRect, item: Dict) -> QRect:
        """Sub-rect of the subtitle line. Mirrors :meth:`paint`'s
        vertical math so the view can hit-test artist clicks against
        this rect."""
        title_y = cell_rect.y() + self.COVER_SIZE + SPACE_SM + 1
        title_bottom = title_y + 22
        year_text = ""
        if self._kind == "album":
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
        title_y = cell_rect.y() + self.COVER_SIZE + SPACE_SM + 1
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

    def sizeHint(self, option, index):
        w = option.rect.width() if option.rect.width() > 0 else 200
        return QSize(w, self.ROW_HEIGHT)

    def paint(self, painter, option, index):
        item = index.data(_LibraryItemsModel.ItemRole)
        if item is None:
            return
        cover = index.data(_LibraryItemsModel.CoverRole)
        rect = option.rect

        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        from modules.ui_helpers import TEXT as _TEXT

        # Hover backdrop — faint highlight so the row reads as
        # interactive without committing to a heavy selection chip.
        if option.state & QStyle.StateFlag.State_MouseOver:
            inset = rect.adjusted(2, 2, -2, -2)
            path = QPainterPath()
            path.addRoundedRect(QRectF(inset), 4, 4)
            painter.fillPath(path, QColor(255, 255, 255, 13))

        # Thumb cell — centered vertically inside the row.
        thumb_y = rect.y() + (rect.height() - self.THUMB_SIZE) // 2
        thumb_rect = QRect(
            rect.x() + self.LEFT_PAD, thumb_y,
            self.THUMB_SIZE, self.THUMB_SIZE,
        )
        if cover is not None and not cover.isNull():
            scaled = scale_pixmap_for_dpr(cover, self.THUMB_SIZE)
            path = QPainterPath()
            path.addRoundedRect(QRectF(thumb_rect),
                                self.THUMB_RADIUS, self.THUMB_RADIUS)
            painter.save()
            painter.setClipPath(path)
            painter.drawPixmap(thumb_rect, scaled)
            painter.restore()
        else:
            path = QPainterPath()
            path.addRoundedRect(QRectF(thumb_rect),
                                self.THUMB_RADIUS, self.THUMB_RADIUS)
            painter.fillPath(path, QColor(255, 255, 255, 10))

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
            text_right -= (self.YEAR_W + self.GAP)
        text_w = max(0, text_right - text_x)

        title_font = QFont(painter.font())
        title_font.setPixelSize(TYPE_BODY.size_px)
        title_font.setBold(True)
        fm_title = QFontMetrics(title_font)

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
            fm_title.elidedText(title, Qt.TextElideMode.ElideRight,
                                title_rect.width()),
        )

        if subtitle:
            sub_font = QFont(painter.font())
            sub_font.setPixelSize(TYPE_CAPTION.size_px)
            sub_font.setBold(False)
            painter.setFont(sub_font)
            fm_sub = QFontMetrics(sub_font)
            painter.setPen(QColor(_TEXT))
            painter.drawText(
                sub_rect,
                int(Qt.AlignmentFlag.AlignLeft
                    | Qt.AlignmentFlag.AlignVCenter),
                fm_sub.elidedText(subtitle, Qt.TextElideMode.ElideRight,
                                  sub_rect.width()),
            )

        if year_text:
            year_font = QFont(painter.font())
            year_font.setPixelSize(TYPE_CAPTION.size_px)
            year_font.setBold(False)
            painter.setFont(year_font)
            painter.setPen(QColor(_TEXT))
            year_rect = QRect(
                rect.right() - self.RIGHT_PAD - self.YEAR_W,
                rect.y(), self.YEAR_W, rect.height(),
            )
            painter.drawText(
                year_rect,
                int(Qt.AlignmentFlag.AlignRight
                    | Qt.AlignmentFlag.AlignVCenter),
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
            text_right -= (self.YEAR_W + self.GAP)
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
            cell_rect.y(), self.YEAR_W, cell_rect.height(),
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
    return item.get("AlbumArtist") or ", ".join(
        item.get("AlbumArtists", []) or []
    ) or ""


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
    link) each route to their own signal."""

    play_requested = Signal(str)             # item_id
    browse_requested = Signal(str)           # item_id
    artist_browse_requested = Signal(str)    # artist_id
    year_browse_requested = Signal(int)      # year

    def __init__(self, tile_delegate: _TileDelegate,
                 row_delegate: _RowDelegate, parent=None):
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
        self.setStyleSheet(
            "QListView { background: transparent; border: none; }"
        )
        # Hover → prewarm: matches the old LibraryTile.enterEvent
        # path. Mouse moves over an item → fire background fetches
        # for get_item + get_album_tracks so that a subsequent click
        # resolves from cache instead of waiting on the network.
        # Idempotent — each item is prewarmed at most once per view
        # lifetime via the _prewarmed set.
        self._prewarmed: set = set()
        self.entered.connect(self._on_entered)

    def set_mode(self, mode: str):
        if mode == self._mode:
            return
        self._mode = mode
        if mode == "list":
            self.setItemDelegate(self._row_delegate)
            self.setViewMode(QListView.ViewMode.ListMode)
            self.setFlow(QListView.Flow.TopToBottom)
            self.setWrapping(False)
        else:
            self.setItemDelegate(self._tile_delegate)
            self.setViewMode(QListView.ViewMode.IconMode)
            self.setFlow(QListView.Flow.LeftToRight)
            self.setWrapping(True)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        # Force a re-layout so uniform item sizes pick up the new
        # delegate's sizeHint immediately. Without this, the first
        # paint after a mode switch can use the stale cell metrics.
        self.scheduleDelayedItemsLayout()

    def mousePressEvent(self, e):
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
            # Hit-test order: overlay → year → subtitle → fall through.
            ov_rect = self._tile_delegate.overlay_rect_for(cell)
            if (self._tile_delegate._show_play_overlay
                    and ov_rect.contains(pos) and item_id):
                self.play_requested.emit(item_id)
                e.accept()
                return
            year_rect = self._tile_delegate.year_rect_for(cell, item)
            if year_rect.contains(pos):
                y = item.get("ProductionYear")
                year_int = int(y) if isinstance(y, int) else (
                    int(y) if isinstance(y, str) and y.isdigit() else 0
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
                year_int = int(y) if isinstance(y, int) else (
                    int(y) if isinstance(y, str) and y.isdigit() else 0
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
        from modules.providers import get_provider
        api = get_provider()
        fetch_tracks = (
            api.get_playlist_items if kind == "playlist"
            else api.get_album_tracks
        )
        run_async(
            api.get_item, item_id,
            on_result=lambda _r: None, on_error=lambda _e: None,
        )
        run_async(
            fetch_tracks, item_id,
            on_result=lambda _r: None, on_error=lambda _e: None,
        )
        # Album tiles also prewarm the now-playing-bar cover slot
        # so a click → play resolves the bar's cover from cache.
        if kind == "album":
            url = api.get_image_url(item_id, "Primary", 256)
            if url:
                from modules.ui_helpers import load_image_async
                load_image_async(
                    f"{item_id}|npbar", url, 256, 256,
                    lambda _pix: None, rounded_radius=0,
                    on_error=lambda: None, priority="high",
                )


# ── Grid ────────────────────────────────────────────────────────────────

class LibraryGrid(QWidget):
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

    _ITEM_TYPE = {"album": "MusicAlbum", "playlist": "Playlist",
                  "artist": "MusicArtist"}
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

        # Cover-loading bookkeeping.
        self._covers_loaded: set = set()
        self._prefetch_idx: int = 0

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
        self._tile_delegate = _TileDelegate(kind, self)
        self._row_delegate = _RowDelegate(kind, self)
        self._view = _LibraryListView(
            self._tile_delegate, self._row_delegate, self,
        )
        self._view.setModel(self._model)
        # Outer padding so the first row of tiles doesn't crowd the
        # window chrome and the last row doesn't slam into the bottom
        # bar. Matches the old grid's SPACE_XL horizontal padding.
        self._view.setViewportMargins(SPACE_XL, 0, SPACE_XL, SPACE_XL)
        install_autofade_scrollbars(self._view)

        # Restore the persisted view mode now that the view exists.
        if self._view_mode == "list":
            self._view.set_mode("list")

        # Signal forwarding from the view to host-level signals.
        self._view.play_requested.connect(self.play_requested.emit)
        self._view.browse_requested.connect(self.browse_requested.emit)
        self._view.artist_browse_requested.connect(
            self.artist_browse_requested.emit
        )
        self._view.year_browse_requested.connect(
            self.year_browse_requested.emit
        )

        # Body row: view + alphabet index sit side-by-side.
        body = QHBoxLayout()
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(0)
        body.addWidget(self._view, 1)
        self._alphabet = _AlphabetIndex()
        self._alphabet.jump_requested.connect(self._on_alphabet_jump)
        body.addWidget(self._alphabet)
        outer.addLayout(body, 1)

        # "Loading more…" footer surfaces while a paginated next-page
        # fetch is in flight. Centered + caption-tier so it reads as
        # status info, not a tile. Hidden by default.
        self._loading_more_label = QLabel("Loading more…", self)
        self._loading_more_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._loading_more_label.setStyleSheet(
            f"color: {TEXT_FAINT}; {type_qss(TYPE_CAPTION)} "
            f"padding: {SPACE_SM}px 0;"
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
        self._view.verticalScrollBar().valueChanged.connect(
            self._on_scroll_raw
        )

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
            if event.type() == event.Type.Resize:
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
        self._prefetch_idx = 0
        self._load_visible_covers()
        if not self._prefetch_timer.isActive():
            self._prefetch_timer.start()

    # ── Public API ────────────────────────────────────────────────────

    def load_items(self, parent_id: str = "", genre_id: str = "",
                   year: str = ""):
        """Async-fetch items of this grid's ``kind``. Two-phase: if a
        disk cache matches the current scope, render from it instantly
        and verify against the server in the background. On a true
        cold load, fire the regular fetch and persist on success."""
        self._parent_id = parent_id
        self._genre_id = genre_id
        self._year = year
        from modules.settings import get_settings as _gs
        ps = _gs().library_page_size
        if ps <= 0:
            self.PAGE_SIZE = 500
            self._auto_paginate = True
        else:
            self.PAGE_SIZE = ps
            self._auto_paginate = False
        item_type = self._ITEM_TYPE.get(self.kind, "")
        sort_by = self._sort_for_kind(self._sort_by, self.kind)
        scope = {
            "kind": self.kind,
            "parent_id": parent_id,
            "genre_id": genre_id,
            "year": year,
            "sort_by": sort_by,
            "sort_order": self._sort_order,
        }
        self._refresh_scope = scope
        cached = disk_cache.load(self._cache_name, scope)
        if cached:
            # Cache payload is either the legacy bare list (page 1
            # only — written by old versions) or the new envelope dict
            # ``{"items": [...], "complete": bool}`` that stores the
            # full multi-page browse. Handle both for forward + back
            # compat across this rewrite.
            if isinstance(cached, dict):
                cached_items = cached.get("items") or []
                cached_complete = bool(cached.get("complete"))
            else:
                cached_items = cached
                cached_complete = False
            # Pass the complete flag through the resp envelope so
            # `_on_items_loaded` can avoid the (otherwise default)
            # `_has_more = len(items) >= PAGE_SIZE` heuristic. With
            # 290 items cached and PAGE_SIZE=200, the heuristic would
            # set `_has_more = True` and trigger an auto-paginate tick
            # before any override here could land — which is exactly
            # why "still loading page by page" persisted across the
            # earlier fix attempts.
            self._items_loaded.emit({
                "Items": cached_items,
                "_complete": cached_complete,
            })
            # Background refresh of page 1 catches any mutations
            # since the cache was written. Tail pages are still
            # trusted from cache unless the user scrolls past them
            # and pagination kicks back in.
            run_async(
                self.api.get_items, parent_id, item_type,
                self.PAGE_SIZE, 0,
                sort_by, self._sort_order, True, genre_id,
                years=year,
                on_result=lambda resp: self._refresh_loaded.emit(resp),
                on_error=lambda _e: None,
            )
            return
        self._clear()
        run_async(
            self.api.get_items, parent_id, item_type,
            self.PAGE_SIZE, 0,
            sort_by, self._sort_order, True, genre_id,
            years=year,
            on_result=lambda resp: self._on_cold_fetch(resp),
            on_error=lambda _e: self._items_loaded.emit({"Items": []}),
        )

    def _on_cold_fetch(self, resp):
        items = (resp or {}).get("Items") or []
        # Render first — pagination state lands in _on_items_loaded,
        # which we read below to mark "complete" if the library fits
        # in a single page.
        self._items_loaded.emit(resp)
        if items and self._refresh_scope:
            complete = len(items) < self.PAGE_SIZE
            self._save_cache_async(items, complete)

    def _save_cache_async(self, items: List[Dict], complete: bool):
        """Persist the cache off the GUI thread. A multi-page cache
        (hundreds to thousands of items) serializes to a non-trivial
        JSON blob, and doing it on the GUI thread between page-load
        appends is what makes scroll hitch right after a new page
        lands."""
        scope = dict(self._refresh_scope)
        payload = {"items": list(items), "complete": complete}
        run_async(
            disk_cache.save, self._cache_name, scope, payload,
            on_result=lambda _r: None, on_error=lambda _e: None,
        )

    def set_sort(self, sort_by: str, sort_order: str):
        self._sort_by = sort_by or "SortName"
        self._sort_order = (
            "Descending" if sort_order == "descending" else "Ascending"
        )
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

    # ── Pagination ────────────────────────────────────────────────────

    @Slot(int)
    def _maybe_load_more(self, value: int):
        if self._loading_more or not self._has_more:
            return
        bar = self._view.verticalScrollBar()
        if bar.maximum() <= 0:
            return
        if value >= bar.maximum() * self.SCROLL_NEAR_BOTTOM:
            self._load_next_page()

    def _load_next_page(self):
        self._loading_more = True
        self._loading_more_label.setVisible(True)
        item_type = self._ITEM_TYPE.get(self.kind, "")
        sort_by = self._sort_for_kind(self._sort_by, self.kind)
        run_async(
            self.api.get_items, self._parent_id, item_type,
            self.PAGE_SIZE, self._loaded_count,
            sort_by, self._sort_order, True, self._genre_id,
            years=self._year,
            on_result=lambda resp: self._on_page_loaded(resp),
            on_error=lambda _e: self._on_page_error(),
        )

    def _on_page_loaded(self, resp):
        items = (resp or {}).get("Items") or []
        self._loading_more = False
        if len(items) < self.PAGE_SIZE:
            self._has_more = False
        if not items:
            self._loading_more_label.setVisible(False)
            # Even with no new items, persist the "complete" flag if
            # we've just hit the tail — next launch can skip
            # pagination entirely.
            if self._refresh_scope and not self._has_more:
                self._save_cache_async(self._model.items(), True)
            return
        items = self._resort_items_by_article(items)
        # Augment the alphabet map for the new tail.
        base = self._model.rowCount()
        for i, item in enumerate(items):
            letter = self._index_letter_for(item)
            if (letter and letter.isalpha()
                    and letter not in self._letter_to_row):
                self._letter_to_row[letter] = base + i
        self._loaded_count += len(items)
        self._model.append_items(items)
        # Hide the footer when there's no more loading queued. In
        # auto-paginate ("load all") mode keep it visible through the
        # 50ms tick gap so the user sees one continuous "Loading more…"
        # indicator rather than a pulsing one.
        if not (self._auto_paginate and self._has_more):
            self._loading_more_label.setVisible(False)
        # Extend the disk cache with the accumulated items so the
        # next launch renders the full library without paging
        # through it again. Off the GUI thread to avoid stutter
        # right after the append (especially for 1000+ item caches).
        if self._refresh_scope:
            self._save_cache_async(
                self._model.items(), not self._has_more,
            )
        self._load_visible_covers()
        if self._auto_paginate and self._has_more and not self._loading_more:
            QTimer.singleShot(50, self._load_next_page)

    def _on_page_error(self):
        self._loading_more = False
        self._loading_more_label.setVisible(False)

    @staticmethod
    def _sort_for_kind(sort_by: str, kind: str) -> str:
        if not sort_by:
            return "SortName"
        first_key = sort_by.split(",", 1)[0]
        if kind in ("playlist", "artist") and first_key in (
            "AlbumArtist", "PremiereDate"
        ):
            return "SortName"
        return sort_by

    # ── Async result handlers ─────────────────────────────────────────

    @Slot(object)
    def _on_items_loaded(self, resp):
        items = (resp or {}).get("Items") or []
        # `_complete` is a private envelope key set by the load_items
        # cache-hit path. When True we know the cache holds the full
        # multi-page browse and there's nothing more to fetch — short-
        # circuit the length-based `_has_more` heuristic and skip the
        # auto-paginate tick below.
        complete = bool((resp or {}).get("_complete"))
        items = self._resort_items_by_article(items)
        # Alphabet map — letter → first-matching row index.
        self._letter_to_row = {}
        for i, item in enumerate(items):
            letter = self._index_letter_for(item)
            if (letter and letter.isalpha()
                    and letter not in self._letter_to_row):
                self._letter_to_row[letter] = i
        self._alphabet.setVisible(
            self._alphabet_field_for_sort(self._sort_by) is not None
        )
        self._model.set_items(items)
        self._covers_loaded.clear()
        self._prefetch_idx = 0
        self._loaded_count = len(items)
        self._has_more = (not complete) and (len(items) >= self.PAGE_SIZE)
        if not items:
            return
        if self._alphabet.isVisible():
            letter = self._index_letter_for(items[0])
            if letter:
                self._alphabet.set_current_letter(letter)
        self._load_visible_covers()
        from modules.settings import get_settings as _gs
        if _gs().library_cover_prefetch:
            if not self._prefetch_timer.isActive():
                self._prefetch_timer.start()
        if (self._auto_paginate and self._has_more
                and not self._loading_more):
            QTimer.singleShot(50, self._load_next_page)

    @Slot(object)
    def _on_refresh_loaded(self, resp):
        items = (resp or {}).get("Items") or []
        rendered_first_page = self._model.items()[:self.PAGE_SIZE]
        if (self._items_signature(items)
                == self._items_signature(rendered_first_page)):
            # No change — preserve the existing multi-page cache.
            # (The old behavior unconditionally re-wrote the cache as
            # a bare page-1 list here, which clobbered the
            # {"items", "complete"} envelope and forced re-paging on
            # every launch.)
            return
        # Library changed — drop everything and re-render. The
        # subsequent pagination + `_on_page_loaded` calls will rewrite
        # the cache as the full multi-page browse lands.
        self._clear()
        self._on_items_loaded({"Items": items})

    @staticmethod
    def _items_signature(items):
        return tuple(it.get("Id", "") for it in items)

    def _clear(self):
        self._model.set_items([])
        self._covers_loaded.clear()
        self._prefetch_timer.stop()
        self._prefetch_idx = 0
        self._loaded_count = 0
        self._has_more = False
        self._loading_more = False
        self._letter_to_row = {}

    # ── Cover loading ─────────────────────────────────────────────────

    def _visible_row_range(self) -> "tuple[int, int]":
        """Inclusive-exclusive [first, last) row indices currently in
        (or near) the viewport. A small over-fetch buffer means a
        moderate kinetic-scroll fling stays ahead of the loader."""
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
            return
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
        cover_url = self.api.get_image_url(cover_id, "Primary", target)
        if not cover_url:
            self._covers_loaded.add(row)
            return
        self._covers_loaded.add(row)

        def _on_pix(pix, r=row):
            self._model.set_cover(r, pix)

        def _on_err(r=row):
            # Drop the index from the loaded set so the next viewport
            # change retries. Mirrors the old _on_cover_failed path.
            self._covers_loaded.discard(r)

        load_image_async(
            f"{cover_id}|{self.kind}tile",
            cover_url, target, target,
            _on_pix, rounded_radius=radius_phys,
            on_error=_on_err, priority=priority,
        )

    @Slot()
    def _prefetch_tick(self):
        rc = self._model.rowCount()
        if rc == 0:
            self._prefetch_timer.stop()
            return
        while (self._prefetch_idx < rc
               and self._prefetch_idx in self._covers_loaded):
            self._prefetch_idx += 1
        if self._prefetch_idx >= rc:
            self._prefetch_timer.stop()
            return
        i = self._prefetch_idx
        self._prefetch_idx += 1
        self._fire_cover_load(i, priority="low")

    # ── Alphabet jump + scroll-driven highlight ───────────────────────

    def _resort_items_by_article(self, items: "List[Dict]") -> "List[Dict]":
        first_key = (self._sort_by or "").split(",", 1)[0]
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
        field = self._alphabet_field_for_sort(self._sort_by)
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
        self._view.scrollTo(idx, QAbstractItemView.ScrollHint.PositionAtTop)

    def _update_alphabet_highlight(self):
        if not self._alphabet.isVisible():
            return
        if self._model.rowCount() == 0:
            return
        idx = self._view.indexAt(QPoint(4, 0))
        if not idx.isValid():
            return
        item = idx.data(_LibraryItemsModel.ItemRole)
        if not item:
            return
        letter = self._index_letter_for(item)
        if letter:
            self._alphabet.set_current_letter(letter)
