"""
Shared UI helpers: theme, async image loader, formatting, common widgets.
"""

import math
import shutil
import subprocess
import threading
from collections import OrderedDict
from typing import Callable, Optional
from PySide6.QtCore import Qt, QRectF, QUrl
from PySide6.QtGui import QPixmap, QImage, QColor, QPainter, QPainterPath, QFont
from PySide6.QtNetwork import QNetworkReply, QNetworkRequest
from PySide6.QtWidgets import QWidget, QSlider, QStyle

from modules.async_io import get_qnam


# ── Theme ────────────────────────────────────────────────────────────────────
# Palette + body fills come from the active Theme (modules/theme.py). The
# constants below are re-exported so existing `from modules.ui_helpers
# import TEXT, ACCENT, ...` callers don't have to change.

from modules.theme import get_active_theme

_THEME = get_active_theme()

ACCENT = _THEME.accent
ACCENT_DEEP = _THEME.accent_deep
BG = _THEME.bg
BG_PANEL = _THEME.bg_panel
BG_CARD = _THEME.bg_card
TEXT = _THEME.text
TEXT_DIM = _THEME.text_dim
TEXT_FAINT = _THEME.text_faint
BORDER = _THEME.border
BORDER_ACCENT = _THEME.border_accent

# Painted body colors — used as `QColor(*BODY_COLOR)` inside paintEvent.
# Three slots because the main window, mini player, and dialogs each
# paint their own surface and read at slightly different depths.
BODY_COLOR = _THEME.body_color
MINI_BODY_COLOR = _THEME.mini_body_color
DIALOG_BODY_COLOR = _THEME.dialog_body_color

GLOBAL_STYLE = f"""
* {{
    color: {TEXT};
    font-family: 'Inter', 'Segoe UI', 'Noto Sans', sans-serif;
}}
QMainWindow, QDialog, QWidget {{
    background: {BG};
}}
QScrollArea {{ background: transparent; border: none; }}
QScrollBar:vertical {{
    background: rgba(255,255,255,0.03); width: 8px; border-radius: 4px;
    margin: 2px;
}}
QScrollBar::handle:vertical {{
    background: rgba(167,139,250,0.4); border-radius: 4px; min-height: 24px;
}}
QScrollBar::handle:vertical:hover {{ background: {ACCENT}; }}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0; }}
QScrollBar:horizontal {{ height: 8px; background: transparent; }}
QScrollBar::handle:horizontal {{
    background: rgba(167,139,250,0.4); border-radius: 4px; min-width: 24px;
}}
QLineEdit {{
    background: rgba(255,255,255,0.05);
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 8px 12px;
    color: {TEXT};
    selection-background-color: {ACCENT_DEEP};
}}
QLineEdit:focus {{ border-color: {ACCENT}; background: rgba(255,255,255,0.07); }}
QPushButton {{
    background: rgba(255,255,255,0.05);
    color: {TEXT};
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 8px 14px;
}}
QPushButton:hover {{ background: rgba(167,139,250,0.15); border-color: {BORDER_ACCENT}; }}
QPushButton:pressed {{ background: rgba(167,139,250,0.3); }}
QPushButton#accent {{
    background: {ACCENT_DEEP}; border: 1px solid {ACCENT}; color: white;
}}
QPushButton#accent:hover {{ background: {ACCENT}; }}
QPushButton#ghost {{
    background: transparent; border: none;
}}
QPushButton#ghost:hover {{ background: rgba(255,255,255,0.06); }}
QComboBox {{
    background: rgba(255,255,255,0.05);
    border: 1px solid {BORDER};
    border-radius: 8px;
    padding: 6px 12px;
    min-height: 22px;
}}
QComboBox::drop-down {{ border: none; width: 20px; }}
QComboBox QAbstractItemView {{
    background: {BG_PANEL};
    border: 1px solid {BORDER_ACCENT};
    border-radius: 6px;
    selection-background-color: rgba(167,139,250,0.25);
    padding: 4px;
}}
QListWidget {{
    background: transparent;
    border: 1px solid {BORDER};
    border-radius: 8px;
    outline: none;
}}
QListWidget::item {{
    padding: 8px 10px; border-radius: 6px; margin: 1px 2px;
}}
QListWidget::item:selected {{ background: rgba(167,139,250,0.18); }}
QListWidget::item:hover {{ background: rgba(255,255,255,0.04); }}
QTabWidget::pane {{ border: none; background: transparent; }}
QTabBar::tab {{
    background: transparent;
    color: {TEXT_DIM};
    padding: 8px 18px;
    border: none;
    border-bottom: 2px solid transparent;
    font-weight: 500;
}}
QTabBar::tab:hover {{ color: {TEXT}; }}
QTabBar::tab:selected {{
    color: {ACCENT}; border-bottom: 2px solid {ACCENT};
}}
QSlider::groove:horizontal {{
    height: 3px; background: rgba(255,255,255,0.12); border-radius: 1px;
}}
QSlider::sub-page:horizontal {{ background: {ACCENT}; border-radius: 1px; }}
QSlider::handle:horizontal {{
    width: 12px; height: 12px; margin: -5px 0;
    background: white; border-radius: 6px;
}}
QSlider::handle:horizontal:hover {{ background: {ACCENT}; }}
QMenu {{
    background: {BG_PANEL};
    border: 1px solid {BORDER_ACCENT};
    border-radius: 8px;
    padding: 4px;
}}
QMenu::item {{
    padding: 8px 24px 8px 14px; border-radius: 4px;
}}
QMenu::item:selected {{ background: rgba(167,139,250,0.2); }}
QMenu::separator {{
    height: 1px; background: {BORDER}; margin: 4px 8px;
}}
QToolTip {{
    background: {BG_PANEL}; color: {TEXT};
    border: 1px solid {BORDER_ACCENT}; padding: 4px 8px; border-radius: 4px;
}}
"""


# ── KDE Plasma window-manager hints ─────────────────────────────────────────

_XPROP_OK: Optional[bool] = None


def skip_taskbar_x11(widget: QWidget):
    """
    Tell EWMH-aware window managers (KWin/Mutter/i3/etc.) to keep `widget` out
    of the taskbar and pager. Uses xprop to set _NET_WM_STATE atoms.
    Silently no-ops if xprop is missing or we're on native Wayland (the
    xprop subprocess can't address Wayland surfaces; mini_player.py uses
    the Qt.Tool window flag on Wayland instead).
    """
    global _XPROP_OK
    # Wayland: bail before subprocessing — `winId()` is a Wayland surface
    # id, not an X11 window id; xprop will fail noisily.
    try:
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None and app.platformName() == "wayland":
            return
    except Exception:
        pass
    if _XPROP_OK is False:
        return
    if _XPROP_OK is None:
        _XPROP_OK = shutil.which("xprop") is not None
        if not _XPROP_OK:
            return

    try:
        wid = int(widget.winId())
    except Exception:
        return
    if wid <= 0:
        return

    def _run():
        try:
            subprocess.run(
                ["xprop", "-id", str(wid),
                 "-f", "_NET_WM_STATE", "32a",
                 "-set", "_NET_WM_STATE",
                 "_NET_WM_STATE_SKIP_TASKBAR,_NET_WM_STATE_SKIP_PAGER,_NET_WM_STATE_ABOVE"],
                check=False, timeout=2,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()


def enable_kde_blur(widget: QWidget):
    """
    Ask KWin's Blur effect to blur the desktop behind `widget`. Sets the
    `_KDE_NET_WM_BLUR_BEHIND_REGION` X11 atom; KWin reads it and applies
    the configured blur radius to whatever shows through the window's
    translucent regions.

    No-ops on:
      - native Wayland — Qt's `winId()` is a Wayland surface id, not an
        X11 window id. The Wayland equivalent (`org_kde_kwin_blur`) has
        no Python bindings available, so blur is an X11/XWayland-only
        feature for now.
      - non-KDE compositors — the atom is KDE-specific; other WMs ignore
        it harmlessly.
      - environments without `xprop` on PATH.

    Setting the property to a single cardinal `0` (instead of a proper
    region tuple) is the conventional empirical signal for "blur the
    whole window" — KWin's blur effect treats a non-quadruple-aligned
    array as a request to use the window's full geometry.
    """
    global _XPROP_OK
    try:
        from PySide6.QtWidgets import QApplication
        app = QApplication.instance()
        if app is not None and app.platformName() == "wayland":
            return
    except Exception:
        pass
    if _XPROP_OK is False:
        return
    if _XPROP_OK is None:
        _XPROP_OK = shutil.which("xprop") is not None
        if not _XPROP_OK:
            return

    try:
        wid = int(widget.winId())
    except Exception:
        return
    if wid <= 0:
        return

    def _run():
        try:
            subprocess.run(
                ["xprop", "-id", str(wid),
                 "-f", "_KDE_NET_WM_BLUR_BEHIND_REGION", "32c",
                 "-set", "_KDE_NET_WM_BLUR_BEHIND_REGION", "0"],
                check=False, timeout=2,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()


# ── Async image loader ──────────────────────────────────────────────────────

# LRU bound on the decoded-pixmap cache. QPixmaps are GPU-side textures
# (~30-60kB each at typical 200x200 cover sizes plus rounded-corner
# variants), so an unbounded dict balloons VRAM on big libraries.
# 256 is generous enough that a typical browse never repeats a fetch
# but caps growth to single-digit megabytes.
_IMAGE_CACHE_MAX = 256
_image_cache: "OrderedDict[str, QPixmap]" = OrderedDict()

# In-flight QNetworkReply objects keyed to the load context the slot
# needs. Qt deletes reply objects whose Python refs are dropped, so we
# must hold them across the async hop — the slot pops the entry and
# calls `reply.deleteLater()` once decoding is done.
_pending_replies: dict = {}


def load_image_async(key: str, url: str, target_w: int, target_h: int,
                     callback: Callable[[QPixmap], None],
                     rounded_radius: int = 0):
    """
    Fetch + scale image asynchronously via Qt's network stack, decoding
    on the GUI thread once the reply lands. No raw threads, no `requests`,
    no cross-thread QObject GC pinning — QNAM owns connection pooling
    and per-host parallelism, and the entire pipeline runs on the Qt
    event loop.
    """
    cache_key = f"{key}|{target_w}x{target_h}|r={rounded_radius}"
    cached = _image_cache.get(cache_key)
    if cached is not None:
        _image_cache.move_to_end(cache_key)
        callback(cached)
        return

    req = QNetworkRequest(QUrl(url))
    # Match the old `requests.get(timeout=8)` budget so a hung Jellyfin
    # image endpoint doesn't pile up replies forever.
    req.setTransferTimeout(8000)
    reply = get_qnam().get(req)
    _pending_replies[reply] = (
        cache_key, target_w, target_h, rounded_radius, callback,
    )
    reply.finished.connect(lambda r=reply: _on_image_reply_finished(r))


def _on_image_reply_finished(reply: QNetworkReply):
    ctx = _pending_replies.pop(reply, None)
    if ctx is None:
        reply.deleteLater()
        return
    cache_key, target_w, target_h, radius, callback = ctx
    try:
        if reply.error() != QNetworkReply.NetworkError.NoError:
            img = _placeholder_image(target_w, target_h)
        else:
            data = bytes(reply.readAll())
            img = QImage()
            if not img.loadFromData(data) or img.isNull():
                img = _placeholder_image(target_w, target_h)
            else:
                img = img.scaled(
                    target_w, target_h,
                    Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                    Qt.TransformationMode.SmoothTransformation,
                )
        pix = QPixmap.fromImage(img)
        if radius > 0:
            pix = _round_corners(pix, radius)
        _image_cache[cache_key] = pix
        _image_cache.move_to_end(cache_key)
        while len(_image_cache) > _IMAGE_CACHE_MAX:
            _image_cache.popitem(last=False)
        callback(pix)
    finally:
        reply.deleteLater()


def _placeholder_image(w: int, h: int) -> QImage:
    """Flat tinted fallback when the network fetch or decode fails. Host
    widgets show their own placeholder over this most of the time; the
    tint just prevents a transparent gap if they don't."""
    img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(QColor("#1a1a2e"))
    return img


def _round_corners(pix: QPixmap, radius: int) -> QPixmap:
    out = QPixmap(pix.size())
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    path = QPainterPath()
    path.addRoundedRect(0, 0, pix.width(), pix.height(), radius, radius)
    p.setClipPath(path)
    p.drawPixmap(0, 0, pix)
    p.end()
    return out


# ── Formatting ──────────────────────────────────────────────────────────────

def fmt_time(ms: int) -> str:
    if ms < 0:
        ms = 0
    s = ms // 1000
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_duration_ticks(ticks: int) -> str:
    return fmt_time(ticks // 10_000)


def _rounded_polygon(points: list[tuple[float, float]],
                     radius: float) -> QPainterPath:
    """Build a closed QPainterPath through `points` with each corner softened
    by `radius`. Walks the polygon, drawing line segments that stop `radius`
    short of each vertex and using a quadratic bezier through the vertex to
    reach the next inset point. Used for icon shapes that should read as
    "soft" (butter, jelly, etc.) without looking pillowy."""
    n = len(points)
    if n < 3 or radius <= 0:
        path = QPainterPath()
        path.moveTo(*points[0])
        for pt in points[1:]:
            path.lineTo(*pt)
        path.closeSubpath()
        return path
    insets: list[tuple[tuple[float, float], tuple[float, float]]] = []
    for i in range(n):
        px, py = points[i]
        prev_x, prev_y = points[(i - 1) % n]
        nxt_x, nxt_y = points[(i + 1) % n]
        # Stay inside each adjacent edge, capped at half its length so we
        # don't overshoot on tight angles or short sides.
        d_prev = math.hypot(prev_x - px, prev_y - py) or 1.0
        d_next = math.hypot(nxt_x - px, nxt_y - py) or 1.0
        r_prev = min(radius, d_prev * 0.5)
        r_next = min(radius, d_next * 0.5)
        ip_in = (px + (prev_x - px) / d_prev * r_prev,
                 py + (prev_y - py) / d_prev * r_prev)
        ip_out = (px + (nxt_x - px) / d_next * r_next,
                  py + (nxt_y - py) / d_next * r_next)
        insets.append((ip_in, ip_out))
    path = QPainterPath()
    path.moveTo(*insets[0][0])
    for i in range(n):
        ip_in, ip_out = insets[i]
        path.lineTo(*ip_in)
        path.quadTo(points[i][0], points[i][1], ip_out[0], ip_out[1])
    path.closeSubpath()
    return path


_APP_ICON_CACHE: dict[int, QPixmap] = {}


def make_app_icon(size: int = 64) -> QPixmap:
    """JellyToast logo: a domed slice of bread with a dollop of jelly
    and a butter play-triangle on top. Drawn with primitives so it
    scales from 16px (tray) up to 512px without raster artifacts.
    Cached per requested size — the icon is requested 3+ times during
    launch (QApplication, JellyToastWindow, TrayController) and the
    pixmap is immutable, so re-rasterizing each time is pure waste."""
    cached = _APP_ICON_CACHE.get(size)
    if cached is not None:
        return cached
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)

    s = float(size)

    # Classic sandwich-bread silhouette: flat bottom with small rounded
    # corners, tall sides, generously rounded top "shoulders", and a
    # gentle arch peaking between the shoulders.
    def slice_path(rect: QRectF) -> QPainterPath:
        x, y, w, h = rect.x(), rect.y(), rect.width(), rect.height()
        br = h * 0.08                  # bottom corner radius — tight
        sr = h * 0.22                  # shoulder radius — chunky
        arch = h * 0.06                # arch height above shoulder line
        sy = y + arch                  # y of the shoulder line
        path = QPainterPath()
        path.moveTo(x + br, y + h)
        path.lineTo(x + w - br, y + h)
        path.quadTo(x + w, y + h, x + w, y + h - br)
        path.lineTo(x + w, sy + sr)
        path.quadTo(x + w, sy, x + w - sr, sy)
        path.cubicTo(
            x + w * 0.70, y,
            x + w * 0.30, y,
            x + sr, sy,
        )
        path.quadTo(x, sy, x, sy + sr)
        path.lineTo(x, y + h - br)
        path.quadTo(x, y + h, x + br, y + h)
        path.closeSubpath()
        return path

    # ── Toast crust (outer slice silhouette) ────────────────────────────
    crust = QColor("#5e2e0d")          # deep, browner crust
    pad = max(1.0, s * 0.025)          # bigger overall — fills more canvas
    p.setBrush(crust)
    p.drawPath(slice_path(QRectF(pad, pad, s - 2 * pad, s - 2 * pad)))

    # ── Toast interior (light, near-white bread) ────────────────────────
    bread = QColor("#fbe9c8")          # whiter, milkier crumb
    inset = pad + max(1.0, s * 0.07)   # thicker crust band for contrast
    p.setBrush(bread)
    p.drawPath(slice_path(QRectF(inset, inset, s - 2 * inset, s - 2 * inset)))

    # ── Jelly dollop — purple, lobed/blobby outline so it reads as a
    #    poured-out spoonful rather than a flat oval. Centered on the
    #    toast so the butter pat can sit dead-center on top of it. ─────
    cx, cy = s / 2.0, s * 0.55
    jw, jh = s * 0.58, s * 0.44
    jelly_path = QPainterPath()
    # Eight control-point pairs around the perimeter create three small
    # lobes per side — the cubic spans pulled outward make the silhouette
    # bulge, so the outline reads as wobbly jam rather than a smooth oval.
    L = cx - jw / 2.0   # left
    R = cx + jw / 2.0   # right
    T = cy - jh / 2.0   # top
    B = cy + jh / 2.0   # bottom
    # Start mid-left, sweep up-and-over the top with two lobes, down the
    # right side with one lobe, across the bottom with two lobes, up the
    # left with one lobe. Asymmetric controls give the irregular feel.
    jelly_path.moveTo(L, cy + jh * 0.05)
    jelly_path.cubicTo(L - jw * 0.04, T + jh * 0.10, cx - jw * 0.18, T - jh * 0.18, cx - jw * 0.05, T - jh * 0.02)
    jelly_path.cubicTo(cx + jw * 0.08, T - jh * 0.20, R + jw * 0.05, T + jh * 0.06, R, cy - jh * 0.02)
    jelly_path.cubicTo(R + jw * 0.10, cy + jh * 0.30, cx + jw * 0.18, B + jh * 0.18, cx + jw * 0.04, B - jh * 0.02)
    jelly_path.cubicTo(cx - jw * 0.10, B + jh * 0.20, L - jw * 0.08, B - jh * 0.04, L, cy + jh * 0.05)
    jelly_path.closeSubpath()
    # Concord-grape purple — deeper for stronger contrast against the bread.
    p.setBrush(QColor("#6a2680"))
    p.drawPath(jelly_path)

    # Glossy highlight on the jelly's upper-left so it reads as wet.
    if size >= 24:
        p.setBrush(QColor(255, 255, 255, 70))
        p.drawEllipse(
            QRectF(
                cx - jw * 0.28, cy - jh * 0.45,
                jw * 0.30, jh * 0.18,
            )
        )

    # ── Butter "play" triangle — pat-shaped pat replaced by the universal
    #    play glyph. Same butter color so it still reads as a topping; the
    #    silhouette doubles as a media-player cue. Corners are gently
    #    rounded so it reads as butter (slightly soft) rather than a sharp
    #    icon stamp. Centered on the jelly. ───────────────────────────────
    butter = QColor("#ffd633")         # punchier, sunnier yellow
    tw = s * 0.22                      # horizontal extent (base → tip)
    th = s * 0.24                      # vertical extent (top → bottom of base)
    tx = cx - tw / 2.0
    ty = cy - th / 2.0
    p.setBrush(butter)
    p.drawPath(_rounded_polygon(
        [(tx, ty), (tx, ty + th), (tx + tw, cy)],
        radius=s * 0.022,
    ))

    # Highlight on the triangle's upper-left interior so it reads as a
    # buttery, slightly glossy slab. Same rounding so the curves match. ──
    if size >= 32:
        p.setBrush(QColor(255, 255, 255, 120))
        p.drawPath(_rounded_polygon(
            [
                (tx + tw * 0.10, ty + th * 0.18),
                (tx + tw * 0.10, ty + th * 0.62),
                (tx + tw * 0.42, ty + th * 0.40),
            ],
            radius=s * 0.012,
        ))

    p.end()
    _APP_ICON_CACHE[size] = pix
    return pix


# ── Scrubbable slider ──────────────────────────────────────────────────────
# Used by every slider that should "feel like a music player slider":
# clicking anywhere in the groove jumps to that value, dragging continues
# to scrub. Stock QSlider only page-steps when you click off the handle,
# which is the wrong default for progress / volume / seek bars.
#
# Also kills the focus rectangle — Qt's default focus indicator paints
# blue notches at the slider edges that read as "brackets" against a
# hairline groove. NoFocus removes them and removes the slider from the
# tab order (transport sliders are mouse-only by design).


class ScrubbableSlider(QSlider):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)

    def _value_at(self, pos: int) -> int:
        # Horizontal sliders read from x; vertical from y. Pick whichever
        # axis matches the current orientation so this class works for
        # both without a separate subclass.
        if self.orientation() == Qt.Orientation.Horizontal:
            span = max(1, self.width())
        else:
            span = max(1, self.height())
        return QStyle.sliderValueFromPosition(
            self.minimum(), self.maximum(), pos, span,
        )

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            pos = (int(e.position().x())
                   if self.orientation() == Qt.Orientation.Horizontal
                   else int(e.position().y()))
            v = self._value_at(pos)
            # setSliderDown so consumer-side position-update slots that
            # gate on isSliderDown() pause their writes during the scrub
            # — otherwise the playback timer fights the user's drag.
            self.setSliderDown(True)
            self.setValue(v)
            self.sliderMoved.emit(v)
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e):
        if e.buttons() & Qt.MouseButton.LeftButton and self.isSliderDown():
            pos = (int(e.position().x())
                   if self.orientation() == Qt.Orientation.Horizontal
                   else int(e.position().y()))
            v = self._value_at(pos)
            self.setValue(v)
            self.sliderMoved.emit(v)
            e.accept()
            return
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton and self.isSliderDown():
            self.setSliderDown(False)
            e.accept()
            return
        super().mouseReleaseEvent(e)
