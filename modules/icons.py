"""
Shared SVG icon registry. Used by JtTopBar, NowPlayingBar, and
FloatingMiniPlayer so every glyph across the app has the same stroke
weight, geometry, and color treatment.

Each icon is a 24×24 viewBox SVG using `currentColor`. _svg_pix() swaps
the color in at render time. icon() returns a 2-state QIcon that flips
to the bright pixmap on hover via QIcon.Mode.Active.
"""

from PySide6.QtCore import Qt, QByteArray
from PySide6.QtGui import QIcon, QPixmap, QPainter
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
    "settings": (
        # Material-style outline gear, simplified for clean rendering at 20px.
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<circle cx="12" cy="12" r="3" stroke="currentColor" stroke-width="2" fill="none"/>'
        '<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 -2.83 2.83'
        ' l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0'
        ' v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83'
        ' l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4'
        ' h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83'
        ' l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0'
        ' v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83'
        ' l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4'
        ' h-.09a1.65 1.65 0 0 0-1.51 1z" '
        'stroke="currentColor" stroke-width="2" fill="none" '
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
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<path d="M7 7 H17 L14 4 M17 7 V11" stroke="currentColor" stroke-width="2" '
        'fill="none" stroke-linecap="round" stroke-linejoin="round"/>'
        '<path d="M17 17 H7 L10 20 M7 17 V13" stroke="currentColor" stroke-width="2" '
        'fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg>'
    ),
    "repeat_one": (
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<path d="M7 7 H17 L14 4 M17 7 V11" stroke="currentColor" stroke-width="2" '
        'fill="none" stroke-linecap="round" stroke-linejoin="round"/>'
        '<path d="M17 17 H7 L10 20 M7 17 V13" stroke="currentColor" stroke-width="2" '
        'fill="none" stroke-linecap="round" stroke-linejoin="round"/>'
        '<text x="12" y="14" font-size="6" font-weight="700" fill="currentColor" '
        'text-anchor="middle" font-family="sans-serif">1</text></svg>'
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
}


def _svg_pix(name: str, color: str, size: int = 20) -> QPixmap:
    """Render an icon as a single-color QPixmap at `size`×`size`."""
    if name not in _SVG:
        # Empty pixmap rather than crash — caller will get a transparent
        # button square they can debug from.
        pix = QPixmap(size, size)
        pix.fill(Qt.GlobalColor.transparent)
        return pix
    svg = _SVG[name].replace("currentColor", color)
    renderer = QSvgRenderer(QByteArray(svg.encode("utf-8")))
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
    renderer.render(p)
    p.end()
    return pix


# Default tones used across every player chrome — keeping them in one
# place means a future palette tweak is a one-line change.
ICON_DIM = "#a8a8a8"
ICON_BRIGHT = "#ffffff"
ICON_ACCENT = "#00a4dc"


def icon(name: str, dim: str = ICON_DIM, bright: str = ICON_BRIGHT,
         size: int = 20) -> QIcon:
    """Two-state QIcon — Normal=dim, Active/Selected=bright. Qt swaps
    to Active on hover when the button is enabled."""
    ic = QIcon()
    ic.addPixmap(_svg_pix(name, dim, size), QIcon.Mode.Normal)
    ic.addPixmap(_svg_pix(name, bright, size), QIcon.Mode.Active)
    ic.addPixmap(_svg_pix(name, bright, size), QIcon.Mode.Selected)
    return ic


def accent_icon(name: str, size: int = 20) -> QIcon:
    """Icon that's accent-colored in both states — used for toggled-on
    state of shuffle/repeat/favorite."""
    return icon(name, dim=ICON_ACCENT, bright=ICON_ACCENT, size=size)
