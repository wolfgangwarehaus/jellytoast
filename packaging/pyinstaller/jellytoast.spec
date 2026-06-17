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
    # License + third-party notices (GPL §3 compliance — the Windows
    # bundle ships GPL libmpv/FFmpeg; ship the corresponding-source offer
    # next to the exe).
    (str(REPO_ROOT / "LICENSE"), "."),
    (str(REPO_ROOT / "packaging" / "THIRD-PARTY-NOTICES.md"), "."),
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
        # NB: do NOT exclude PySide6.QtMultimedia — the visualizer's
        # in-process QtDecodeTap imports QAudioDecoder/QAudioFormat from
        # it (jellytoast/visualizer.py). Excluding it silently breaks the
        # visualizer in every frozen build.
        "PySide6.QtPdf",
        "tkinter",
    ],
    noarchive=False,
)

# --- Linux: do NOT bundle libmpv's host-provided dependency closure --------
# The .deb declares `Depends: libmpv2 | libmpv1`, so the host always provides
# libmpv *and everything libmpv links* (ffmpeg, glib, libstdc++, libmount, …).
# PyInstaller's binary analysis nevertheless copies the build host's (Ubuntu
# 22.04) copies of those libraries into the bundle, and a frozen app searches
# its own dir first. At runtime python-mpv loads libmpv via
# `ctypes.CDLL(ctypes.util.find_library("mpv"))`: on the host that resolves to
# the host's libmpv.so.2, but the dynamic loader then satisfies libmpv's own
# deps from the bundle's STALE 22.04 copies. On any distro newer than the
# builder that fails hard — e.g. on Ubuntu 26.04:
#     OSError: <bundle>/_internal/libstdc++.so.6: version `GLIBCXX_3.4.32'
#         not found (required by /usr/lib/x86_64-linux-gnu/libavcodec.so.62)
# so `import mpv` raises -> MPV_AVAILABLE is False -> the app aborts at startup
# with a "Missing dependency" dialog. (CI's frozen smoke test never caught it:
# it runs on the same 22.04 where the bundled libs still match the system.)
#
# Fix: strip libmpv's dependency closure from the Linux bundle so those libs
# come from the host — exactly the "audio stack matches the host distro" intent
# in this file's header. The set is computed from the build host's libmpv via
# `ldd`; we add the C/C++ runtimes (linked by ~everything, so they'd otherwise
# stay) and the libmpv sonames themselves. Defensive: any failure leaves the
# binaries untouched (worst case = today's behavior).
if sys.platform.startswith("linux"):
    import ctypes.util
    import os
    import subprocess

    def _libmpv_dep_closure():
        soname = ctypes.util.find_library("mpv")
        if not soname:
            return set()
        cache = subprocess.run(
            ["ldconfig", "-p"], capture_output=True, text=True
        ).stdout
        lib_path = next(
            (
                line.split("=>")[-1].strip()
                for line in cache.splitlines()
                if soname in line and "=>" in line
            ),
            None,
        )
        if not lib_path or not os.path.exists(lib_path):
            return set()
        ldd = subprocess.run(
            ["ldd", lib_path], capture_output=True, text=True
        ).stdout
        names = {
            os.path.basename(line.split("=>")[0].strip())
            for line in ldd.splitlines()
            if "=>" in line
        }
        # The host always provides these (the .deb's Depends guarantees it);
        # the C/C++ runtimes in particular MUST come from the host so they are
        # >= what the host's libmpv/ffmpeg need.
        names |= {
            soname,
            "libmpv.so.1",
            "libmpv.so.2",
            "libstdc++.so.6",
            "libgcc_s.so.1",
        }
        return names

    try:
        _drop = _libmpv_dep_closure()
        if _drop:
            _before = len(a.binaries)
            a.binaries = [
                b for b in a.binaries if os.path.basename(b[0]) not in _drop
            ]
            print(
                "jellytoast.spec: excluded "
                f"{_before - len(a.binaries)} host-provided libmpv dependency "
                "libraries from the Linux bundle (use the host's, per Depends)"
            )
    except Exception as _exc:  # never break the build over this hardening
        print(
            f"jellytoast.spec: libmpv closure exclusion skipped: {_exc}",
            file=sys.stderr,
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
