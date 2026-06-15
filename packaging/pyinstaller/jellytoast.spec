# PyInstaller spec — one file for both frozen targets:
#   Linux  → onedir bundle that the .deb stages under /opt/jellytoast
#            (libmpv intentionally NOT bundled — python-mpv dlopens the
#            system libmpv.so.2, which the .deb declares as a Depends, so
#            the audio stack matches the host distro's ffmpeg)
#   Windows → onedir windowed app wrapped by Inno Setup / the portable
#            zip, with libmpv-2.dll BUNDLED (there is no system libmpv on
#            Windows; python-mpv resolves the DLL from the app dir)
#
# Build (from the repo root):
#   pyinstaller packaging/pyinstaller/jellytoast.spec --noconfirm
#
# On Windows, CI drops the pinned libmpv-2.dll at
# packaging/windows/libmpv/libmpv-2.dll before building (see
# .github/workflows/release.yml); the spec picks it up when present.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

REPO_ROOT = Path(SPECPATH).resolve().parent.parent

datas = [
    # Brand SVG + KWin drag-repaint effect — the two in-package data
    # sets pyproject's [tool.setuptools.package-data] ships in the wheel.
    # collect_data_files keeps them importlib.resources-resolvable in
    # the frozen app.
    *collect_data_files("jellytoast"),
]

hiddenimports = [
    # ctypes / soft-imported modules PyInstaller's static analysis
    # can't see from jellytoast's lazy import style.
    "mpv",
    *collect_submodules("keyring.backends"),
    *collect_submodules("pychromecast"),
    *collect_submodules("zeroconf"),
    *collect_submodules("soco"),
    *collect_submodules("snapcast"),
    *collect_submodules("async_upnp_client"),
]
if sys.platform.startswith("linux"):
    hiddenimports += ["dbus_next", "Xlib"]

binaries = []
if sys.platform == "win32":
    _libmpv = REPO_ROOT / "packaging" / "windows" / "libmpv" / "libmpv-2.dll"
    if _libmpv.is_file():
        # "." = the app root dir, where python-mpv's module-dir fallback
        # (and a plain LoadLibrary) finds it.
        binaries.append((str(_libmpv), "."))
    else:
        print(
            "WARNING: packaging/windows/libmpv/libmpv-2.dll not found — "
            "the frozen app will fail at 'import mpv' unless libmpv is "
            "on PATH. CI must download it first.",
            file=sys.stderr,
        )

a = Analysis(
    [str(REPO_ROOT / "packaging" / "pyinstaller" / "launch.py")],
    pathex=[str(REPO_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        # Qt modules jellytoast never imports — keeps the bundle lean.
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtQuick",
        "PySide6.QtQml",
        "PySide6.Qt3DCore",
        "PySide6.QtCharts",
        "PySide6.QtDataVisualization",
        "PySide6.QtMultimedia",
        "PySide6.QtPdf",
        "tkinter",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="jellytoast",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,  # GUI app — no console window on Windows
    icon=str(REPO_ROOT / "packaging" / "windows" / "jellytoast.ico")
    if sys.platform == "win32"
    else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="jellytoast",
)
