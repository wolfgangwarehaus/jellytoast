"""Windows taskbar overlay badge — playback state at a glance.

Paints a small play/pause glyph on the corner of the taskbar button so a
user can tell jellytoast's state without switching to it. Windows-only; a
no-op controller everywhere else.

Two Windows-isms handled here:
- ITaskbarList3 methods must NOT be called before the shell sends the
  registered ``TaskbarButtonCreated`` window message (and it re-fires if
  Explorer restarts). A ``QAbstractNativeEventFilter`` gates the first
  apply and re-applies on restart.
- Qt6 dropped QtWin's ``toHICON``, so the badge HICONs are built from the
  shared icon glyphs via a temp ``.ico`` + ``LoadImage`` (the same .ico
  path the Start-menu icon uses).

All Win32 / comtypes imports are lazy + guarded: the module imports on
every platform and ``start()`` degrades to a clean no-op without comtypes
or the taskbar API.
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QAbstractNativeEventFilter, QObject

logger = logging.getLogger(__name__)

# Module-cached comtypes interface + class id (built on first Windows use).
_ITaskbarList3 = None
_CLSID_TaskbarList = None


def _badge_png(kind: str) -> bytes:
    """Render a play/pause badge (dark disc + white glyph) to PNG bytes.
    Pure Qt — runs on any platform (lets the rendering be unit-tested)."""
    from PySide6.QtCore import QBuffer, QIODevice, QRectF, Qt
    from PySide6.QtGui import QColor, QImage, QPainter

    from jellytoast.icons import _svg_pix

    size = 32
    img = QImage(size, size, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    # Dark disc so the glyph reads on any taskbar accent / wallpaper.
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(20, 20, 20, 235))
    p.drawEllipse(QRectF(0.5, 0.5, size - 1, size - 1))
    glyph = _svg_pix(kind, "#ffffff", 20)
    glyph.setDevicePixelRatio(1.0)
    glyph = glyph.scaled(
        20,
        20,
        Qt.AspectRatioMode.KeepAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    )
    p.drawPixmap((size - glyph.width()) // 2, (size - glyph.height()) // 2, glyph)
    p.end()

    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    return bytes(buf.data())


def _badge_ico_path(kind: str):
    """Write (once) and return the .ico for a badge kind, beside the
    Start-menu icon. Reuses windows_shortcut's PNG→.ico wrapper."""
    from jellytoast.windows_shortcut import _icon_path, _png_to_ico_bytes

    path = _icon_path().parent / f"overlay_{kind}.ico"
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_png_to_ico_bytes(_badge_png(kind), 32))
    return path


def _load_hicon(path) -> int:
    import ctypes
    from ctypes import wintypes

    IMAGE_ICON = 1
    LR_LOADFROMFILE = 0x00000010
    LR_DEFAULTSIZE = 0x00000040
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    user32.LoadImageW.restype = wintypes.HANDLE
    user32.LoadImageW.argtypes = [
        wintypes.HINSTANCE,
        wintypes.LPCWSTR,
        wintypes.UINT,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    return user32.LoadImageW(
        None, str(path), IMAGE_ICON, 0, 0, LR_LOADFROMFILE | LR_DEFAULTSIZE
    )


def _make_taskbar():
    """CoCreate an ITaskbarList3, defining the interface on first use.
    Only the vtable up to SetOverlayIcon (slot 16) is declared."""
    global _ITaskbarList3, _CLSID_TaskbarList
    from ctypes import c_int, c_uint, c_ulonglong, c_void_p, c_wchar_p
    from ctypes.wintypes import HWND

    import comtypes
    from comtypes import COMMETHOD, GUID, HRESULT, CoCreateInstance, IUnknown

    if _ITaskbarList3 is None:

        class ITaskbarList3(IUnknown):
            _iid_ = GUID("{ea1afb91-9e28-4b86-90e9-9e9f8a5eefaf}")
            _methods_ = [
                COMMETHOD([], HRESULT, "HrInit"),
                COMMETHOD([], HRESULT, "AddTab", (["in"], HWND, "hwnd")),
                COMMETHOD([], HRESULT, "DeleteTab", (["in"], HWND, "hwnd")),
                COMMETHOD([], HRESULT, "ActivateTab", (["in"], HWND, "hwnd")),
                COMMETHOD([], HRESULT, "SetActiveAlt", (["in"], HWND, "hwnd")),
                COMMETHOD(
                    [], HRESULT, "MarkFullscreenWindow",
                    (["in"], HWND, "hwnd"), (["in"], c_int, "fFullscreen"),
                ),
                COMMETHOD(
                    [], HRESULT, "SetProgressValue",
                    (["in"], HWND, "hwnd"),
                    (["in"], c_ulonglong, "ullCompleted"),
                    (["in"], c_ulonglong, "ullTotal"),
                ),
                COMMETHOD(
                    [], HRESULT, "SetProgressState",
                    (["in"], HWND, "hwnd"), (["in"], c_int, "tbpFlags"),
                ),
                COMMETHOD(
                    [], HRESULT, "RegisterTab",
                    (["in"], HWND, "hwndTab"), (["in"], HWND, "hwndMDI"),
                ),
                COMMETHOD([], HRESULT, "UnregisterTab", (["in"], HWND, "hwndTab")),
                COMMETHOD(
                    [], HRESULT, "SetTabOrder",
                    (["in"], HWND, "hwndTab"), (["in"], HWND, "hwndInsertBefore"),
                ),
                COMMETHOD(
                    [], HRESULT, "SetTabActive",
                    (["in"], HWND, "hwndTab"), (["in"], HWND, "hwndMDI"),
                    (["in"], c_uint, "dwReserved"),
                ),
                COMMETHOD(
                    [], HRESULT, "ThumbBarAddButtons",
                    (["in"], HWND, "hwnd"), (["in"], c_uint, "cButtons"),
                    (["in"], c_void_p, "pButton"),
                ),
                COMMETHOD(
                    [], HRESULT, "ThumbBarUpdateButtons",
                    (["in"], HWND, "hwnd"), (["in"], c_uint, "cButtons"),
                    (["in"], c_void_p, "pButton"),
                ),
                COMMETHOD(
                    [], HRESULT, "ThumbBarSetImageList",
                    (["in"], HWND, "hwnd"), (["in"], c_void_p, "himl"),
                ),
                COMMETHOD(
                    [], HRESULT, "SetOverlayIcon",
                    (["in"], HWND, "hwnd"), (["in"], c_void_p, "hIcon"),
                    (["in"], c_wchar_p, "pszDescription"),
                ),
            ]

        _ITaskbarList3 = ITaskbarList3
        _CLSID_TaskbarList = GUID("{56fdf344-fd6d-11d0-958a-006097c9a090}")

    try:
        comtypes.CoInitialize()
    except Exception:
        pass
    tb = CoCreateInstance(_CLSID_TaskbarList, interface=_ITaskbarList3)
    tb.HrInit()
    return tb


class _ButtonCreatedFilter(QAbstractNativeEventFilter):
    """Fires ``on_created`` when the shell sends ``TaskbarButtonCreated``."""

    def __init__(self, on_created):
        super().__init__()
        self._on_created = on_created
        import ctypes

        self._msg_id = ctypes.windll.user32.RegisterWindowMessageW(  # type: ignore[attr-defined]
            "TaskbarButtonCreated"
        )

    def nativeEventFilter(self, event_type, message):
        try:
            if event_type == b"windows_generic_MSG":
                from ctypes import wintypes

                msg = wintypes.MSG.from_address(int(message))
                if msg.message == self._msg_id:
                    self._on_created()
        except Exception as e:  # pragma: no cover — Windows-only
            logger.debug("taskbar native event: %s", e)
        return False, 0


class TaskbarOverlay(QObject):
    """Shows a play/pause overlay badge reflecting playback state.

    ``start(window)`` wires it up; the badge updates on PlayerBus state
    changes. No-op off Windows and degrades cleanly without comtypes.
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self._hwnd = 0
        self._taskbar = None
        self._filter = None
        self._ready = False
        self._desired = None  # "play" | "pause" | None
        self._hicons: dict[str, int] = {}

    def start(self, window):
        from jellytoast.platform_compat import IS_WINDOWS

        if not IS_WINDOWS:
            return
        try:
            self._hwnd = int(window.winId())
            from PySide6.QtWidgets import QApplication

            self._filter = _ButtonCreatedFilter(self._on_button_created)
            QApplication.instance().installNativeEventFilter(self._filter)
            self._connect_bus()
        except Exception as e:
            logger.info("taskbar overlay unavailable: %s", e)

    def _connect_bus(self):
        from jellytoast.player_state import PlayerBus

        bus = PlayerBus.get()
        bus.playback_started.connect(lambda *_: self._set_state("play"))
        bus.playback_resumed.connect(lambda *_: self._set_state("play"))
        bus.playback_paused.connect(lambda *_: self._set_state("pause"))
        bus.playback_stopped.connect(lambda *_: self._set_state(None))
        bus.playback_ended.connect(lambda *_: self._set_state(None))

    def _on_button_created(self):
        # Shell created (or recreated, after an Explorer restart) our
        # taskbar button — ITaskbarList3 calls are valid now.
        self._ready = True
        try:
            if self._taskbar is None:
                self._taskbar = _make_taskbar()
            self._apply()
        except Exception as e:  # pragma: no cover — Windows-only
            logger.debug("taskbar init failed: %s", e)
            self._taskbar = None

    def _set_state(self, state):
        if state == self._desired:
            return
        self._desired = state
        self._apply()

    def _apply(self):
        if not self._ready or self._taskbar is None or not self._hwnd:
            return
        try:
            if self._desired is None:
                self._taskbar.SetOverlayIcon(self._hwnd, None, "")
            else:
                self._taskbar.SetOverlayIcon(
                    self._hwnd, self._hicon(self._desired), self._desired
                )
        except Exception as e:  # pragma: no cover — Windows-only
            logger.debug("SetOverlayIcon failed: %s", e)

    def _hicon(self, kind: str) -> int:
        if kind not in self._hicons:
            self._hicons[kind] = _load_hicon(_badge_ico_path(kind))
        return self._hicons[kind]

    def stop(self):
        if self._taskbar is None or not self._hwnd:
            return
        try:
            self._taskbar.SetOverlayIcon(self._hwnd, None, "")
        except Exception:  # pragma: no cover — Windows-only
            pass
