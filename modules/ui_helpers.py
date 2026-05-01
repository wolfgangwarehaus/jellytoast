"""
Shared UI helpers: theme, async image loader, formatting, common widgets.
"""

import shutil
import subprocess
import threading
import requests
from typing import Callable, Optional
from PyQt6.QtCore import Qt, QRectF, pyqtSignal, QObject
from PyQt6.QtGui import QPixmap, QImage, QColor, QPainter, QPainterPath, QFont
from PyQt6.QtWidgets import QWidget


# ── Theme ────────────────────────────────────────────────────────────────────

ACCENT = "#00a4dc"
ACCENT_DEEP = "#0085bd"
BG = "#101010"
BG_PANEL = "#202020"
BG_CARD = "rgba(255,255,255,0.04)"
TEXT = "#ffffff"
TEXT_DIM = "rgba(255,255,255,0.7)"
TEXT_FAINT = "rgba(255,255,255,0.4)"
BORDER = "rgba(255,255,255,0.08)"
BORDER_ACCENT = "rgba(0,164,220,0.35)"

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
    Silently no-ops if xprop is missing or we're on native Wayland.
    """
    global _XPROP_OK
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


# ── Async image loader ──────────────────────────────────────────────────────

class _ImageLoaderSignals(QObject):
    loaded = pyqtSignal(str, QPixmap)


_image_cache: dict[str, QPixmap] = {}


def load_image_async(key: str, url: str, target_w: int, target_h: int,
                     callback: Callable[[QPixmap], None],
                     rounded_radius: int = 0):
    """
    Fetch image off-thread, scale, optionally round corners, and invoke callback
    on the Qt main thread.
    """
    cache_key = f"{key}|{target_w}x{target_h}|r={rounded_radius}"
    if cache_key in _image_cache:
        callback(_image_cache[cache_key])
        return

    signals = _ImageLoaderSignals()
    signals.loaded.connect(lambda _k, p: callback(p))

    def _work():
        pix = _fetch_pixmap(url, target_w, target_h)
        if rounded_radius > 0:
            pix = _round_corners(pix, rounded_radius)
        _image_cache[cache_key] = pix
        signals.loaded.emit(cache_key, pix)

    threading.Thread(target=_work, daemon=True).start()


def _fetch_pixmap(url: str, w: int, h: int) -> QPixmap:
    try:
        r = requests.get(url, timeout=8)
        img = QImage()
        img.loadFromData(r.content)
        if img.isNull():
            raise ValueError("invalid image")
        return QPixmap.fromImage(img).scaled(
            w, h, Qt.AspectRatioMode.KeepAspectRatioByExpanding,
            Qt.TransformationMode.SmoothTransformation,
        )
    except Exception:
        pix = QPixmap(w, h)
        pix.fill(QColor("#1a1a2e"))
        # Subtle gradient placeholder
        p = QPainter(pix)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QColor(255, 255, 255, 30))
        p.setFont(QFont("Arial", min(w, h) // 4, QFont.Weight.Bold))
        p.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, "♪")
        p.end()
        return pix


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


def make_app_icon(size: int = 64) -> QPixmap:
    """JellyToast logo: a square slice of toast with a dollop of jelly
    and a pat of butter on top. Drawn with primitives so it scales from
    16px (tray) up to 128px+ without raster artifacts."""
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(Qt.PenStyle.NoPen)

    s = float(size)

    # ── Toast crust (outer rounded square) ──────────────────────────────
    crust = QColor("#9c6a3a")
    pad = max(1.0, s * 0.06)
    p.setBrush(crust)
    p.drawRoundedRect(
        QRectF(pad, pad, s - 2 * pad, s - 2 * pad),
        s * 0.16, s * 0.16,
    )

    # ── Toast interior (golden bread) ──────────────────────────────────
    bread = QColor("#e3ae65")
    inset = pad + max(1.0, s * 0.05)
    p.setBrush(bread)
    p.drawRoundedRect(
        QRectF(inset, inset, s - 2 * inset, s - 2 * inset),
        s * 0.10, s * 0.10,
    )

    # ── Jelly dollop — purple, lobed/blobby outline so it reads as a
    #    poured-out spoonful rather than a flat oval. Centered on the
    #    toast so the butter pat can sit dead-center on top of it. ─────
    cx, cy = s / 2.0, s * 0.55
    jw, jh = s * 0.50, s * 0.38
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
    # Concord-grape purple — desaturated enough to feel jammy, not neon.
    p.setBrush(QColor("#7b3a8f"))
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

    # ── Butter pat — small rounded square centered on the jelly. ──────
    butter = QColor("#f7d764")
    bw, bh = s * 0.22, s * 0.14
    bx = cx - bw / 2.0
    by = cy - bh / 2.0
    p.setBrush(butter)
    p.drawRoundedRect(QRectF(bx, by, bw, bh), bh * 0.25, bh * 0.25)

    # Butter highlight (top-left strip) — only visible at larger sizes
    # where the pat is big enough to read.
    if size >= 32:
        p.setBrush(QColor(255, 255, 255, 110))
        p.drawRoundedRect(
            QRectF(
                bx + bw * 0.15, by + bh * 0.18,
                bw * 0.45, bh * 0.22,
            ),
            bh * 0.15, bh * 0.15,
        )

    p.end()
    return pix
