"""
Shared SVG icon registry. Used by JtTopBar, NowPlayingBar, and
FloatingMiniPlayer so every glyph across the app has the same stroke
weight, geometry, and color treatment.

Each icon is a 24×24 viewBox SVG using `currentColor`. _svg_pix() swaps
the color in at render time. icon() returns a 2-state QIcon that flips
to the bright pixmap on hover via QIcon.Mode.Active.
"""

import functools

from PySide6.QtCore import QByteArray, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer

# Stroke-width 2, line-cap round, fill=none unless explicitly noted.
_SVG = {
    # ── Navigation ─────────────────────────────────────────────────────
    "back": (
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<path d="M15 6 L9 12 L15 18" stroke="currentColor" stroke-width="2" '
        'fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>'
    ),
    "forward": (
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<path d="M9 6 L15 12 L9 18" stroke="currentColor" stroke-width="2" '
        'fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>'
    ),
    "home": (
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<path d="M3 11 L12 3 L21 11 L21 21 L15 21 L15 14 L9 14 L9 21 L3 21 Z" '
        'stroke="currentColor" stroke-width="2" fill="none" '
        'stroke-linecap="round" stroke-linejoin="round"/></svg>'
    ),
    "menu": (
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<path d="M4 7 H20 M4 12 H20 M4 17 H20" stroke="currentColor" '
        'stroke-width="2" stroke-linecap="round"/></svg>'
    ),
    "search": (
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<circle cx="11" cy="11" r="6" stroke="currentColor" stroke-width="2" fill="none"/>'
        '<line x1="20" y1="20" x2="15.5" y2="15.5" stroke="currentColor" '
        'stroke-width="2" stroke-linecap="round"/></svg>'
    ),
    "cast": (
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<path d="M21 5 H3 V9" stroke="currentColor" stroke-width="2" fill="none" '
        'stroke-linecap="round" stroke-linejoin="round"/>'
        '<path d="M21 5 V19 H10" stroke="currentColor" stroke-width="2" fill="none" '
        'stroke-linecap="round" stroke-linejoin="round"/>'
        '<path d="M3 12 a 6 6 0 0 1 6 6" stroke="currentColor" stroke-width="2" '
        'fill="none" stroke-linecap="round"/>'
        '<path d="M3 16 a 2 2 0 0 1 2 2" stroke="currentColor" stroke-width="2" '
        'fill="none" stroke-linecap="round"/>'
        '<circle cx="3.5" cy="19.5" r="1" fill="currentColor"/></svg>'
    ),
    # Classic AirPlay glyph — screen frame + upward-pointing triangle.
    # Used in the cast dialog so Chromecast and AirPlay rows are
    # visually distinguishable at a glance.
    "airplay": (
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<path d="M3 7 H21 V15 H15" stroke="currentColor" stroke-width="2" '
        'fill="none" stroke-linecap="round" stroke-linejoin="round"/>'
        '<path d="M3 7 V15 H9" stroke="currentColor" stroke-width="2" '
        'fill="none" stroke-linecap="round" stroke-linejoin="round"/>'
        '<path d="M12 13 L17 19 H7 Z" fill="currentColor"/></svg>'
    ),
    "user": (
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<circle cx="12" cy="8" r="4" stroke="currentColor" stroke-width="2" fill="none"/>'
        '<path d="M4 21 a 8 8 0 0 1 16 0" stroke="currentColor" stroke-width="2" '
        'fill="none" stroke-linecap="round"/></svg>'
    ),
    "chevron_down": (
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<path d="M6 9 L12 15 L18 9" stroke="currentColor" stroke-width="2" '
        'fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>'
    ),
    "check": (
        '<svg viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg">'
        '<path d="M3 8 L7 12 L13 4" stroke="currentColor" stroke-width="2.2" '
        'fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>'
    ),
    "info": (
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<circle cx="12" cy="12" r="9" stroke="currentColor" stroke-width="2" fill="none"/>'
        '<line x1="12" y1="11" x2="12" y2="17" stroke="currentColor" '
        'stroke-width="2" stroke-linecap="round"/>'
        '<circle cx="12" cy="7.5" r="1.2" fill="currentColor"/></svg>'
    ),
    "settings": (
        # Material-style outline gear, simplified for clean rendering at 20px.
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<circle cx="12" cy="12" r="3" stroke="currentColor" stroke-width="2" fill="none"/>'
        '<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 -2.83 2.83'
        " l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0"
        " v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83"
        " l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4"
        " h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83"
        " l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0"
        " v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83"
        " l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4"
        ' h-.09a1.65 1.65 0 0 0-1.51 1z" '
        'stroke="currentColor" stroke-width="2" fill="none" '
        'stroke-linecap="round" stroke-linejoin="round"/></svg>'
    ),
    # Crescent moon — sleep-timer affordance. Feather's `moon` path,
    # drawn as a filled glyph so it reads at 18px in the player bar.
    "moon": (
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<path d="M21 12.79 A 9 9 0 1 1 11.21 3 A 7 7 0 0 0 21 12.79 Z" '
        'stroke="currentColor" stroke-width="2" fill="currentColor" '
        'stroke-linecap="round" stroke-linejoin="round"/></svg>'
    ),
    # ── Transport ─────────────────────────────────────────────────────
    "play": (
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<path d="M7 5 L19 12 L7 19 Z" fill="currentColor"/></svg>'
    ),
    "pause": (
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<rect x="6" y="5" width="4" height="14" rx="1" fill="currentColor"/>'
        '<rect x="14" y="5" width="4" height="14" rx="1" fill="currentColor"/></svg>'
    ),
    "prev": (
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<rect x="5" y="5" width="2" height="14" rx="1" fill="currentColor"/>'
        '<path d="M19 5 L9 12 L19 19 Z" fill="currentColor"/></svg>'
    ),
    "next": (
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<rect x="17" y="5" width="2" height="14" rx="1" fill="currentColor"/>'
        '<path d="M5 5 L15 12 L5 19 Z" fill="currentColor"/></svg>'
    ),
    "shuffle": (
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<path d="M3 7 H7 L17 17 H21 M3 17 H7 L9 15 M15 9 L17 7 H21" '
        'stroke="currentColor" stroke-width="2" fill="none" '
        'stroke-linecap="round" stroke-linejoin="round"/>'
        '<path d="M18 4 L21 7 L18 10" stroke="currentColor" stroke-width="2" '
        'fill="none" stroke-linecap="round" stroke-linejoin="round"/>'
        '<path d="M18 14 L21 17 L18 20" stroke="currentColor" stroke-width="2" '
        'fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>'
    ),
    "repeat": (
        # Refined two-arrow loop — same family as before but with
        # cleaner V-shaped arrowheads (instead of single-stroke
        # hints), thinner 1.75 strokes, and a tighter vertical
        # bounding box (y=6..18 vs y=4..20). The closure-suggestion
        # stubs at the corners are dropped; the parallel arrows
        # carry the "loop" reading on their own.
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        # Top arrow: ──→
        '<path d="M6 8 H15 M13 6 L16 8 L13 10" stroke="currentColor" '
        'stroke-width="1.75" fill="none" stroke-linecap="round" '
        'stroke-linejoin="round"/>'
        # Bottom arrow: ←──  (mirrored)
        '<path d="M18 16 H9 M11 14 L8 16 L11 18" stroke="currentColor" '
        'stroke-width="1.75" fill="none" stroke-linecap="round" '
        'stroke-linejoin="round"/></svg>'
    ),
    "repeat_one": (
        # Same refined arrows as `repeat`, plus a bold VECTOR "1" (a drawn
        # path with flag + base serif, NOT an SVG <text> glyph — text
        # rasterizes tiny and soft via QSvgRenderer at icon sizes and read
        # as "barely different"). The digit sits in the gap between the two
        # horizontals (y=8..16), clear of both arrowheads.
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<path d="M6 8 H15 M13 6 L16 8 L13 10" stroke="currentColor" '
        'stroke-width="1.75" fill="none" stroke-linecap="round" '
        'stroke-linejoin="round"/>'
        '<path d="M18 16 H9 M11 14 L8 16 L11 18" stroke="currentColor" '
        'stroke-width="1.75" fill="none" stroke-linecap="round" '
        'stroke-linejoin="round"/>'
        '<path d="M10.7 10.6 L12.2 9.4 V14.6 M11.4 14.6 H13.6" '
        'stroke="currentColor" stroke-width="1.7" fill="none" '
        'stroke-linecap="round" stroke-linejoin="round"/></svg>'
    ),
    "stop": (
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<rect x="6" y="6" width="12" height="12" rx="1" fill="currentColor"/></svg>'
    ),
    # ── Volume / queue / favorite ─────────────────────────────────────
    "volume": (
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<path d="M3 9 H7 L12 5 V19 L7 15 H3 Z" stroke="currentColor" stroke-width="2" '
        'fill="currentColor" stroke-linejoin="round"/>'
        '<path d="M16 9 a 5 5 0 0 1 0 6" stroke="currentColor" stroke-width="2" '
        'fill="none" stroke-linecap="round"/>'
        '<path d="M19 6 a 9 9 0 0 1 0 12" stroke="currentColor" stroke-width="2" '
        'fill="none" stroke-linecap="round"/></svg>'
    ),
    "volume_muted": (
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<path d="M3 9 H7 L12 5 V19 L7 15 H3 Z" stroke="currentColor" stroke-width="2" '
        'fill="currentColor" stroke-linejoin="round"/>'
        '<path d="M16 9 L21 14 M21 9 L16 14" stroke="currentColor" stroke-width="2" '
        'fill="none" stroke-linecap="round"/></svg>'
    ),
    "queue": (
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<path d="M3 6 H21 M3 12 H15 M3 18 H15 M18 16 V21 L21 19 Z" '
        'stroke="currentColor" stroke-width="2" fill="currentColor" '
        'stroke-linecap="round" stroke-linejoin="round"/></svg>'
    ),
    # Picture-in-picture / pop-out mini player. Outer rounded frame with
    # a filled inset in the bottom-right corner — universal "pop the
    # player out into a floating window" affordance (YouTube, Spotify,
    # Apple Music all use this glyph).
    "miniplayer": (
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<rect x="3" y="5" width="18" height="14" rx="2" '
        'stroke="currentColor" stroke-width="2" fill="none"/>'
        '<rect x="12" y="12" width="7" height="5" rx="1" '
        'fill="currentColor"/></svg>'
    ),
    # Clean rounded square — the mini player's compact↔expanded size
    # toggle (the old "▢" glyph's intent as a real SVG). One simple
    # shape so it stays crisp at the 14px window-control size; the
    # earlier four-corner-bracket version was illegible that small.
    "expand": (
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<rect x="5" y="5" width="14" height="14" rx="2.5" '
        'stroke="currentColor" stroke-width="2" fill="none"/></svg>'
    ),
    # Mini player compact/expanded toggle — the glyph previews the
    # *target* shape: `view_tall` shows while compact (click → grow to
    # the tall album view), `view_flat` shows while expanded (click →
    # collapse to the flat bar). Weight-matched outlines, 2px stroke.
    "view_tall": (
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<rect x="7" y="4" width="10" height="16" rx="2" '
        'stroke="currentColor" stroke-width="2" fill="none"/></svg>'
    ),
    "view_flat": (
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<rect x="4" y="8" width="16" height="8" rx="2" '
        'stroke="currentColor" stroke-width="2" fill="none"/></svg>'
    ),
    # Diagonal arrow leaving the top-right — "open the main window".
    # Drawn on the same ~14-unit content grid as `expand` and `volume`
    # so the three mini-player window-controls read as one balanced set
    # (the earlier 8-unit version rendered visibly small beside them).
    "open_window": (
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<path d="M5 19 L19 5 M10 5 H19 V14" stroke="currentColor" '
        'stroke-width="2" fill="none" stroke-linecap="round" '
        'stroke-linejoin="round"/></svg>'
    ),
    # Window controls for the borderless main window's titlebar (the
    # blended JtTopBar). Shared 24-grid, 2px stroke, ~12-unit content.
    "win_minimize": (
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<path d="M6 12 H18" stroke="currentColor" stroke-width="2" '
        'fill="none" stroke-linecap="round"/></svg>'
    ),
    "win_maximize": (
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<rect x="6" y="6" width="12" height="12" rx="2" '
        'stroke="currentColor" stroke-width="2" fill="none"/></svg>'
    ),
    "win_close": (
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<path d="M7 7 L17 17 M17 7 L7 17" stroke="currentColor" '
        'stroke-width="2" fill="none" stroke-linecap="round"/></svg>'
    ),
    "favorite_outline": (
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<path d="M12 21 C 5 16 3 12 3 8.5 a 4.5 4.5 0 0 1 9 -1.5 a 4.5 4.5 0 0 1 9 1.5 '
        'C 21 12 19 16 12 21 Z" stroke="currentColor" stroke-width="2" '
        'fill="none" stroke-linejoin="round"/></svg>'
    ),
    "favorite_filled": (
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<path d="M12 21 C 5 16 3 12 3 8.5 a 4.5 4.5 0 0 1 9 -1.5 a 4.5 4.5 0 0 1 9 1.5 '
        'C 21 12 19 16 12 21 Z" fill="currentColor"/></svg>'
    ),
    # "Download" — down-arrow into a tray. Paired with `check_filled`
    # for the album-tile / NP-cover BL corner button: download when
    # the item isn't on disk, check when it is. Same 24×24 viewBox as
    # the favorite_* pair so the two corners read at matching weight.
    "download": (
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<path d="M12 4 L12 15 M7 10 L12 15 L17 10" '
        'stroke="currentColor" stroke-width="2" fill="none" '
        'stroke-linecap="round" stroke-linejoin="round"/>'
        '<path d="M4 19 L20 19" stroke="currentColor" stroke-width="2" '
        'stroke-linecap="round"/></svg>'
    ),
    # Larger-viewbox check, sized to match the heart glyph's optical
    # weight when rendered inside the same 28-px CoverOverlayButton.
    # The tighter 16-viewbox `check` above is kept for the small inline
    # uses (Settings rows, tray, etc).
    "check_filled": (
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<path d="M5 12 L10 17 L19 7" stroke="currentColor" stroke-width="2.4" '
        'fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>'
    ),
    # ── Library controls ───────────────────────────────────────────────
    "grid": (
        # 2×2 of rounded squares — universal "grid view" glyph.
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<rect x="4" y="4" width="7" height="7" rx="1.5" stroke="currentColor" stroke-width="2" fill="none"/>'
        '<rect x="13" y="4" width="7" height="7" rx="1.5" stroke="currentColor" stroke-width="2" fill="none"/>'
        '<rect x="4" y="13" width="7" height="7" rx="1.5" stroke="currentColor" stroke-width="2" fill="none"/>'
        '<rect x="13" y="13" width="7" height="7" rx="1.5" stroke="currentColor" stroke-width="2" fill="none"/>'
        "</svg>"
    ),
    "list": (
        # Bulleted rows — paired with grid for the view toggle.
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<circle cx="5" cy="6" r="1.5" fill="currentColor"/>'
        '<line x1="9" y1="6" x2="20" y2="6" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>'
        '<circle cx="5" cy="12" r="1.5" fill="currentColor"/>'
        '<line x1="9" y1="12" x2="20" y2="12" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>'
        '<circle cx="5" cy="18" r="1.5" fill="currentColor"/>'
        '<line x1="9" y1="18" x2="20" y2="18" stroke="currentColor" stroke-width="2" stroke-linecap="round"/>'
        "</svg>"
    ),
    "sort": (
        # A / Z stacked on the left, double-headed vertical arrow on the
        # right — reads as "sort by alphabetical order, either direction".
        # The actual asc/desc state lives in the menu, so the icon
        # represents the broader "sort" affordance, not current order.
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<text x="2" y="11" font-family="sans-serif" font-size="10" '
        'font-weight="700" fill="currentColor">A</text>'
        '<text x="2" y="21" font-family="sans-serif" font-size="10" '
        'font-weight="700" fill="currentColor">Z</text>'
        '<path d="M17 4 L17 20 M14 7 L17 4 L20 7 M14 17 L17 20 L20 17" '
        'stroke="currentColor" stroke-width="2" stroke-linecap="round" '
        'stroke-linejoin="round" fill="none"/>'
        "</svg>"
    ),
    # Padlock — body + U-shackle. Used by the volume popup to signal
    # the bit-perfect "volume locked at 100%" state.
    "lock": (
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<rect x="5" y="11" width="14" height="9" rx="2" stroke="currentColor" '
        'stroke-width="2" fill="none"/>'
        '<path d="M8 11 V8 a4 4 0 0 1 8 0 V11" stroke="currentColor" '
        'stroke-width="2" fill="none" stroke-linecap="round"/>'
        "</svg>"
    ),
    # Eyedropper — the "sample a colour from the screen" affordance next to the
    # accent swatches and in the custom Colors page. Diagonal, but with a clear
    # round squeeze-bulb at the top — the bulb is what stops it reading as a
    # pencil; the barrel tapers to a fine tip at bottom-left.
    "eyedropper": (
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<circle cx="16.5" cy="7.5" r="3.5" stroke="currentColor" '
        'stroke-width="2" fill="none"/>'
        '<path d="M14 10 L4.5 19.5 L4 21 L5.5 20.5 L15 11" '
        'stroke="currentColor" stroke-width="2" fill="none" '
        'stroke-linejoin="round" stroke-linecap="round"/>'
        "</svg>"
    ),
}


@functools.lru_cache(maxsize=512)
def _svg_pix_cached(name: str, color: str, physical: int, dpr: float) -> QPixmap:
    """The actual rasterization, memoized on (name, color, physical-size, dpr).

    Theme/accent swaps re-issue the same handful of glyphs across every
    surface — and ``icon()`` renders 30 size×mode variants per name — so the
    same (name, color, physical, dpr) tuple was being re-rasterized hundreds
    of times per swap. Caching makes the 2nd+ render near-free. Keyed by dpr so
    a fractional-scale change still produces fresh pixmaps; bounded so a busy
    session can't grow it without limit. The returned QPixmap is shared
    (implicitly COW), and callers only read it, so sharing is safe."""
    if name not in _SVG:
        # Empty pixmap rather than crash — caller will get a transparent
        # button square they can debug from.
        pix = QPixmap(physical, physical)
        pix.fill(Qt.GlobalColor.transparent)
        pix.setDevicePixelRatio(dpr)
        return pix
    svg = _SVG[name].replace("currentColor", color)
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    pix = QPixmap(physical, physical)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    renderer.render(p)
    p.end()
    pix.setDevicePixelRatio(dpr)
    return pix


def _svg_pix(name: str, color: str, size: int = 20) -> QPixmap:
    """Render an icon as a single-color QPixmap at `size`×`size` (cached).

    HiDPI: render the backing pixmap at physical resolution
    (`size * devicePixelRatio`) and tag it via setDevicePixelRatio so Qt knows
    the logical size is still `size`. Without this, on a 2x display Qt scales a
    20×20 pixmap up to 40×40 with bilinear interpolation and strokes look
    blurry. (Sub-pixel POSITIONING softness at fractional scale is handled at
    paint time by IconButton's device-pixel snap, not here — supersampling at
    render time doesn't help, it only adds a downscale.)"""
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance()
    dpr = app.devicePixelRatio() if app is not None else 1.0
    physical = max(1, int(round(size * dpr)))
    return _svg_pix_cached(name, color, physical, dpr)


# Default tones used across every player chrome. Mirrored from the
# canonical text tokens in ui_helpers (IDLE_TEXT / TEXT) so the color
# editor's edits to those tokens flow through to icons on the next
# refresh_theme() — keeping these as separate constants in this
# module would mean two places to keep in sync for the same value.
def _resolve_icon_default(token_name: str, fallback: str) -> str:
    try:
        from modules import ui_helpers as _u

        return getattr(_u, token_name, fallback)
    except Exception:
        return fallback


ICON_DIM = _resolve_icon_default("IDLE_TEXT", "#a8a8a8")
ICON_BRIGHT = _resolve_icon_default("TEXT", "#ffffff")


# Pulled from the active theme so an accent override (Settings →
# Display → Accent) flows through to accent-state icons (filled
# heart, active shuffle / repeat).
def _resolve_icon_accent() -> str:
    try:
        from modules.theme import get_active_theme

        return get_active_theme().accent
    except Exception:
        return "#967de1"


ICON_ACCENT = _resolve_icon_accent()


def refresh_theme() -> None:
    """Refresh ICON_ACCENT, ICON_DIM, and ICON_BRIGHT after a
    theme/accent/color-token change. Idempotent. Existing QIcon
    objects callers hold are not retroactively updated — they were
    built with the old colors baked in. Callers that want live-
    updating icons must re-call ``icon(name)`` /
    ``accent_icon(name)`` on the ``PlayerBus.theme_changed`` signal."""
    global ICON_ACCENT, ICON_DIM, ICON_BRIGHT
    ICON_ACCENT = _resolve_icon_accent()
    ICON_DIM = _resolve_icon_default("IDLE_TEXT", "#a8a8a8")
    ICON_BRIGHT = _resolve_icon_default("TEXT", "#ffffff")


# Pixmap sizes baked into every QIcon returned by `icon()` so Qt's
# QIcon engine can pick a sharp pre-rendered pixmap for whatever
# ``setIconSize()`` the caller used. Covers every iconSize present in
# the codebase today (12 / 13 / 14 / 16 / 18 / 20 / 22 / 28). Without
# this, callers that set an iconSize different from the default render
# size get a bilinearly-scaled pixmap and the result reads as visibly
# blurry — most obvious on dense glyphs like the grid / sort icons in
# the top bar.
_ICON_BAKED_SIZES = (12, 13, 14, 16, 18, 20, 22, 24, 28, 32)


def icon(name: str, dim: str = "", bright: str = "", size: int = 20) -> QIcon:
    """Two-state QIcon — Normal=dim, Active/Selected=bright. Qt swaps
    to Active on hover when the button is enabled.

    ``dim`` / ``bright`` default to the live ICON_DIM / ICON_BRIGHT
    module globals — resolved HERE, per call, not as default-argument
    values (those would freeze at import time and a theme switch via
    ``refresh_theme()`` would never reach them). Callers that re-issue
    icon() on ``theme_changed`` get the current tint.

    Renders pixmaps at every size in ``_ICON_BAKED_SIZES`` (plus the
    explicitly-requested ``size`` if it isn't in that set) so Qt picks
    a sharp pre-rendered pixmap for any caller-set iconSize. ``size``
    is kept on the signature for back-compat but isn't load-bearing
    anymore — the baked set covers every iconSize in the app today."""
    dim = dim or ICON_DIM
    bright = bright or ICON_BRIGHT
    ic = QIcon()
    sizes = set(_ICON_BAKED_SIZES)
    sizes.add(size)
    for s in sorted(sizes):
        ic.addPixmap(_svg_pix(name, dim, s), QIcon.Mode.Normal)
        ic.addPixmap(_svg_pix(name, bright, s), QIcon.Mode.Active)
        ic.addPixmap(_svg_pix(name, bright, s), QIcon.Mode.Selected)
    return ic


def accent_icon(name: str, size: int = 20) -> QIcon:
    """Icon that's accent-colored in both states — used for toggled-on
    state of shuffle/repeat/favorite."""
    return icon(name, dim=ICON_ACCENT, bright=ICON_ACCENT, size=size)


def icon_svg_path(name: str, color: str = "#ffffff") -> str:
    """Materialize the named icon SVG to a cache file (with the given
    stroke color baked in) and return its absolute path. Useful when
    a Qt stylesheet needs `image: url(...)` — Qt's QSS doesn't support
    data URIs and most SVG icons in this app live as in-memory
    strings rather than files. Cached by (name, color) so repeat
    calls just return the existing path."""
    if name not in _SVG:
        return ""
    import hashlib
    from pathlib import Path

    from PySide6.QtCore import QStandardPaths

    h = hashlib.sha1(f"{name}|{color}".encode()).hexdigest()
    cache_dir = (
        Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.CacheLocation))
        / "qss_icons"
    )
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return ""
    path = cache_dir / f"{h}.svg"
    if not path.exists():
        svg = _SVG[name].replace("currentColor", color)
        try:
            path.write_text(svg, encoding="utf-8")
        except OSError:
            return ""
    return str(path)
