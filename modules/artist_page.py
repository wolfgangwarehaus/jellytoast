"""
Native artist detail page.

Shown when the user clicks an artist tile in the native Artists grid.
Replaces JF Web's artist detail rendering for the music library.

Layout:
- Top: back button.
- Header band: artist photo (left), name + genre + counts (right).
- Body: grid of the artist's albums sorted by release year ascending
  (oldest → newest, "chronological release order"). Reuses LibraryTile
  for the album cells so the visual reads identically to the main
  Albums grid.

Click any album tile → existing browse path (preview in NowPlayingPage).
Play overlay → install that album as the live queue + start.
"""

from typing import Dict, List, Optional

from PySide6.QtCore import Qt, QSize, Signal, Slot
from PySide6.QtGui import QPalette, QPixmap
from PySide6.QtWidgets import (
    QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QGridLayout, QScrollArea,
)

from modules.async_io import run_async
from modules.providers import get_provider
from modules.ui_helpers import load_image_async, TEXT, TEXT_DIM, TEXT_FAINT, screen_dpr
from modules.icons import icon
from modules.design_tokens import (
    TYPE_DISPLAY, TYPE_BODY, TYPE_MICRO, apply_type, font, type_qss,
    SPACE_SM, SPACE_MD, SPACE_LG, SPACE_XL,
)
from modules.library_grid import LibraryTile


class ArtistPage(QWidget):
    """Artist detail surface — header + chronological album grid."""

    dismiss_requested = Signal()
    # Forwarded from each album tile so the host can open the preview /
    # install-and-play path without ArtistPage knowing about either.
    album_browse_requested = Signal(str)
    album_play_requested = Signal(str)

    HEADER_COVER = 180

    # Async fetch results land on the GUI thread via these.
    _meta_loaded = Signal(str, object)    # (artist_id, meta or None)
    _albums_loaded = Signal(str, object)  # (artist_id, list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.api = get_provider()
        self._artist_id: str = ""
        self._artist_meta: Dict = {}
        self._album_tiles: List[LibraryTile] = []
        self._current_cols = 0

        self.setObjectName("artistPage")
        self.setStyleSheet("""
            QWidget#artistPage,
            QWidget#artistPage QWidget,
            QWidget#artistPage QLabel,
            QWidget#artistPage QFrame,
            QWidget#artistPage QScrollArea,
            QWidget#artistPage QScrollArea > QWidget,
            QWidget#artistPage QScrollArea > QWidget > QWidget {
                background: transparent;
            }
            QWidget#artistPage QScrollBar:vertical {
                background: transparent; width: 8px;
                margin: 4px 2px 4px 0; border: none;
            }
            QWidget#artistPage QScrollBar::handle:vertical {
                background: rgba(255,255,255,0.18);
                border-radius: 3px; min-height: 28px;
            }
            QWidget#artistPage QScrollBar::handle:vertical:hover {
                background: rgba(255,255,255,0.32);
            }
            QWidget#artistPage QScrollBar::add-line:vertical,
            QWidget#artistPage QScrollBar::sub-line:vertical { height: 0; }
        """)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # Back button row.
        top_row = QHBoxLayout()
        top_row.setContentsMargins(SPACE_MD, SPACE_SM, SPACE_MD, 0)
        top_row.setSpacing(0)
        self._back_btn = QPushButton()
        self._back_btn.setIcon(icon("back"))
        self._back_btn.setIconSize(QSize(18, 18))
        self._back_btn.setFixedSize(36, 32)
        self._back_btn.setToolTip("Back")
        self._back_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._back_btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._back_btn.setStyleSheet("""
            QPushButton {
                background: transparent; border: none; border-radius: 6px;
            }
            QPushButton:hover { background: rgba(255,255,255,0.08); }
        """)
        self._back_btn.clicked.connect(self.dismiss_requested.emit)
        top_row.addWidget(self._back_btn)
        top_row.addStretch(1)
        outer.addLayout(top_row)

        # Header: artist photo + meta block.
        header = QHBoxLayout()
        header.setContentsMargins(SPACE_XL, SPACE_LG, SPACE_XL, SPACE_LG)
        header.setSpacing(SPACE_XL)

        self._cover = QLabel()
        self._cover.setFixedSize(self.HEADER_COVER, self.HEADER_COVER)
        self._cover.setStyleSheet(
            "background: rgba(255,255,255,0.04); border-radius: 90px;"
        )
        self._cover.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._cover_orig: Optional[QPixmap] = None
        header.addWidget(self._cover, 0, Qt.AlignmentFlag.AlignTop)

        meta = QVBoxLayout()
        meta.setContentsMargins(0, SPACE_SM, 0, 0)
        meta.setSpacing(SPACE_SM)

        self._kicker = QLabel("ARTIST")
        self._kicker.setStyleSheet(
            f"color: {TEXT_FAINT}; {type_qss(TYPE_MICRO)}"
        )
        apply_type(self._kicker, TYPE_MICRO)
        meta.addWidget(self._kicker)

        self._name = QLabel("Loading…")
        self._name.setFont(font(TYPE_DISPLAY))
        self._name.setStyleSheet(f"color: {TEXT};")
        self._name.setWordWrap(True)
        meta.addWidget(self._name)

        self._info = QLabel("")
        self._info.setStyleSheet(f"color: {TEXT_DIM}; {type_qss(TYPE_BODY)}")
        meta.addWidget(self._info)

        meta.addStretch(1)
        meta_widget = QWidget(self)
        meta_widget.setLayout(meta)
        header.addWidget(meta_widget, 1)

        outer.addLayout(header)

        # Album grid scroll area.
        self._scroll = QScrollArea(self)
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        # Flatten viewport first-paint — see feedback_wayland_flash_diagnostics.
        _vp = self._scroll.viewport()
        _vp.setAutoFillBackground(False)
        _vp.setBackgroundRole(QPalette.ColorRole.NoRole)
        self._container = QWidget(self._scroll)
        self._grid_layout = QGridLayout(self._container)
        self._grid_layout.setContentsMargins(
            SPACE_XL, 0, SPACE_XL, SPACE_XL,
        )
        self._grid_layout.setHorizontalSpacing(SPACE_LG)
        self._grid_layout.setVerticalSpacing(SPACE_LG + SPACE_SM)
        self._grid_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._scroll.setWidget(self._container)
        outer.addWidget(self._scroll, 1)

        self._meta_loaded.connect(self._on_meta_loaded)
        self._albums_loaded.connect(self._on_albums_loaded)

    # ── Public API ─────────────────────────────────────────────────────

    def load_artist(self, artist_id: str):
        """Async-fetch artist metadata + their albums. Populates the
        page when the result lands. No-op if the same artist is
        already loaded (idempotent re-show)."""
        if not artist_id:
            return
        if artist_id == self._artist_id and self._artist_meta:
            return
        self._artist_id = artist_id
        self._artist_meta = {}
        self._clear_album_tiles()
        self._name.setText("Loading…")
        self._info.setText("")
        run_async(
            self.api.get_item, artist_id,
            on_result=lambda meta, aid=artist_id:
                self._meta_loaded.emit(aid, meta),
            on_error=lambda _e, aid=artist_id:
                self._meta_loaded.emit(aid, None),
        )
        run_async(
            self.api.get_artist_albums, artist_id,
            on_result=lambda albums, aid=artist_id:
                self._albums_loaded.emit(aid, albums),
            on_error=lambda _e, aid=artist_id:
                self._albums_loaded.emit(aid, []),
        )

    # ── Async handlers ─────────────────────────────────────────────────

    @Slot(str, object)
    def _on_meta_loaded(self, artist_id: str, meta: Optional[Dict]):
        if artist_id != self._artist_id:
            return  # stale; user moved on
        if meta is None:
            self._name.setText("Couldn't load artist")
            return
        self._artist_meta = meta
        self._name.setText(meta.get("Name") or "Unknown")
        # Info line: drop pieces that are missing rather than stub
        # them. Pattern matches NowPlayingPage's album header.
        bits = []
        genres = [g for g in (meta.get("Genres") or []) if g]
        if genres:
            bits.append(genres[0])
        # Album count comes from _on_albums_loaded — re-merged into
        # the info line there once both async fetches resolve.
        self._info.setText("  ·  ".join(bits))
        # Cover / artist photo. HiDPI: target physical pixels and the
        # DPR-multiplied radius so the cached slot matches what the
        # label paints; _on_cover_loaded just tags + sets.
        dpr = screen_dpr(self)
        target_phys = max(self.HEADER_COVER, int(round(self.HEADER_COVER * dpr)))
        radius_phys = int(round(90 * dpr))
        server_px = max(360, target_phys)
        url = self.api.get_image_url(artist_id, "Primary", server_px)
        if url:
            load_image_async(
                f"{artist_id}|artistphoto", url, target_phys, target_phys,
                self._on_cover_loaded, rounded_radius=radius_phys,
            )

    @Slot(object)
    def _on_cover_loaded(self, pix: QPixmap):
        if pix is None or pix.isNull():
            return
        self._cover_orig = pix
        dpr = screen_dpr(self)
        if dpr != 1.0:
            pix.setDevicePixelRatio(dpr)
        self._cover.setPixmap(pix)

    @Slot(str, object)
    def _on_albums_loaded(self, artist_id: str, albums: Optional[List[Dict]]):
        if artist_id != self._artist_id:
            return
        albums = albums or []
        # Sort chronologically — oldest first. Jellyfin's
        # get_artist_albums returns descending; sort client-side so we
        # don't have to fork the API helper. ProductionYear is the
        # most reliable field for music; PremiereDate falls back to
        # something parseable.
        def _year(item):
            y = item.get("ProductionYear")
            if y:
                return int(y)
            # PremiereDate is ISO 8601 — first 4 chars are the year.
            pd = (item.get("PremiereDate") or "").strip()
            if pd[:4].isdigit():
                return int(pd[:4])
            return 9999  # unknown years sort last
        albums = sorted(albums, key=_year)

        # Update the info line now that we know the album count.
        bits = []
        meta_genres = [g for g in (self._artist_meta.get("Genres") or []) if g]
        if meta_genres:
            bits.append(meta_genres[0])
        bits.append(
            f"{len(albums)} albums" if len(albums) != 1 else "1 album"
        )
        self._info.setText("  ·  ".join(bits))

        # Build tiles + populate grid.
        self._clear_album_tiles()
        for album in albums:
            # show_subtitle=False — every album on this page has the
            # same artist (the page's subject), so repeating the
            # artist line under every cover would be noise. The year
            # line stays.
            tile = LibraryTile(
                album, kind="album", show_subtitle=False,
                parent=self._container,
            )
            tile.browse_requested.connect(self.album_browse_requested.emit)
            tile.play_requested.connect(self.album_play_requested.emit)
            self._album_tiles.append(tile)
            # Match LibraryTile's DPR-aware request size so this load
            # populates the same cache slot LibraryGrid uses for an
            # album tile; without this the same album would re-fetch
            # under a different cache key when the user navigates from
            # ArtistPage to the album-grid view.
            dpr = screen_dpr(self)
            target_phys = max(LibraryTile.COVER_SIZE, int(round(LibraryTile.COVER_SIZE * dpr)))
            radius_phys = int(round(8 * dpr))
            server_px = max(360, target_phys)
            cover_url = self.api.get_image_url(
                album.get("Id", ""), "Primary", server_px,
            )
            if cover_url:
                load_image_async(
                    f"{album.get('Id')}|artistalbumtile",
                    cover_url, target_phys, target_phys,
                    tile.set_cover, rounded_radius=radius_phys,
                )
        self._current_cols = 0
        self._reflow_grid()

    # ── Layout helpers ─────────────────────────────────────────────────

    def _clear_album_tiles(self):
        for tile in self._album_tiles:
            self._grid_layout.removeWidget(tile)
            tile.setParent(None)
            tile.deleteLater()
        self._album_tiles = []

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._reflow_grid()

    def _reflow_grid(self):
        if not self._album_tiles:
            return
        viewport = self._scroll.viewport()
        avail = (viewport.width() if viewport is not None else self.width()) \
                - 2 * SPACE_XL
        per_tile = LibraryTile.COVER_SIZE + SPACE_LG
        cols = max(1, (avail + SPACE_LG) // per_tile)
        if cols == self._current_cols:
            return
        self._current_cols = cols
        for tile in self._album_tiles:
            self._grid_layout.removeWidget(tile)
        for i, tile in enumerate(self._album_tiles):
            row, col = divmod(i, cols)
            self._grid_layout.addWidget(
                tile, row, col, Qt.AlignmentFlag.AlignHCenter,
            )
            tile.show()
        for col in range(cols):
            self._grid_layout.setColumnStretch(col, 1)
        for col in range(cols, cols + 16):
            self._grid_layout.setColumnStretch(col, 0)
