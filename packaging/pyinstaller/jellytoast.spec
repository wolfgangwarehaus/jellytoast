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

import os
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
    # GPLv3 text: the project source is GPL-2.0-or-later (LICENSE), but the frozen
    # bundle (PySide6/Qt + a GPL build of libmpv/FFmpeg) is conveyed under GPL-3.0,
    # so the package must also ship the v3 text (GPLv3: "give all recipients a copy
    # of this License along with the Program").
    (str(REPO_ROOT / "COPYING"), "."),
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
    *collect_submodules("async_upnp_client"),
]
if sys.platform.startswith("linux"):
    hiddenimports += ["dbus_next", "Xlib"]

# Linux bundles (AppImage / deb) carry Qt Quick for the phone interface
# (jellytoast/quick — opt-in on Plasma Mobile). Windows / macOS bundles
# keep pruning it: no phone shell there, and it's ~20 MB.
QUICK_UI = sys.platform.startswith("linux")
if QUICK_UI:
    hiddenimports += ["PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtQuickControls2"]

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
elif sys.platform == "darwin":
    # macOS: there is no system libmpv on a clean Mac, so BUNDLE it (like the
    # Windows DLL — and UNLIKE the Linux .deb path, which strips it to use the
    # host's). CI stages the Homebrew libmpv at packaging/macos/libmpv/ first
    # (see packaging/macos/get_libmpv.sh). "." = the COLLECT root, which is
    # sys._MEIPASS at run time; player_backend.py redirects find_library there.
    # PyInstaller's macholib analysis follows the dylib's load commands and
    # pulls libmpv's own dep closure (FFmpeg, libass, …) into the bundle,
    # rewriting their install names — so the whole audio stack travels with the
    # .app.
    _libmpv_dir = REPO_ROOT / "packaging" / "macos" / "libmpv"
    _dylibs = sorted(_libmpv_dir.glob("libmpv*.dylib")) if _libmpv_dir.is_dir() else []
    if _dylibs:
        for _dylib in _dylibs:
            binaries.append((str(_dylib), "."))
    else:
        print(
            "WARNING: no packaging/macos/libmpv/libmpv*.dylib found — the "
            "frozen .app will fail at 'import mpv' unless libmpv is on the "
            "system. CI must stage it first (packaging/macos/get_libmpv.sh).",
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
        *([] if QUICK_UI else ["PySide6.QtQuick", "PySide6.QtQml"]),
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

# --- macOS: drop Intel-only stragglers so the two arch trees stay symmetric --
# The 0.2.0 universal .dmg lipo-merges the arm64 and x86_64 .app trees file by
# file, and merge_universal.sh FAILS CLOSED if the trees' Mach-O sets differ.
# Two collections happen on the Intel leg only (verified against real CI trees,
# run 29157012071):
#   • libmpv.dylib — PyInstaller's ctypes hook resolves python-mpv's
#     find_library("mpv") at BUILD time. Intel Homebrew lives under /usr/local
#     (on dyld's default search path) so it resolves — via the keg's
#     unversioned symlink, collecting a DUPLICATE libmpv under that name.
#     arm64 brew is /opt/homebrew (not searched), so nothing is collected.
#     The real libmpv.2.dylib is staged explicitly (binaries=... above) on
#     both legs and player_backend redirects find_library into the bundle,
#     so this copy is 4.5 MB of dead weight even on a pure Intel build.
#   • ossl-modules/legacy.dylib — the Intel leg builds cryptography from
#     source (no x86_64 wheel on 49+), linking Homebrew OpenSSL dynamically;
#     PyInstaller tags the keg's provider-module dir along. The arm64 wheel is
#     statically linked, so no ossl-modules there. Our only cryptography use
#     is AES-GCM (credentials.py), which never loads the legacy provider, and
#     CPython's own _ssl (TLS for requests) bundles its own libssl/libcrypto
#     on BOTH legs — dropping the provider dir costs nothing.
if sys.platform == "darwin":
    _mac_drop = [
        b
        for b in a.binaries
        if b[0] == "libmpv.dylib" or b[0].startswith("ossl-modules")
    ]
    if _mac_drop:
        a.binaries = [b for b in a.binaries if b not in _mac_drop]
        print(
            "jellytoast.spec: excluded "
            f"{len(_mac_drop)} Intel-only macOS collection(s) for universal-"
            f"merge symmetry: {sorted(b[0] for b in _mac_drop)}"
        )

# --- Prune Qt payload PyInstaller collects but jellytoast never uses -------
# The `excludes=` list above is a PYTHON-import filter: it stops
# `import PySide6.QtQuick` from pulling the module in, but it cannot stop
# PyInstaller's *binary* collector from copying Qt plugins and the shared
# libraries they drag behind them. Measured on the 0.2.2 AppImage (501 MB
# unpacked), that shipped a chain nothing in this app touches:
#
#   platforminputcontexts/libqtvirtualkeyboardplugin.so   (an on-screen
#       touch keyboard) → libQt6VirtualKeyboard → libQt6Quick → QmlMeta →
#       QmlModels → libQt6Qml  ..................................  ~17 MB
#   platformthemes/libqgtk3.so (GTK3 platform theme) → libgtk-3 + the
#       gdk-pixbuf/rsvg stack behind it  .......................  ~8 MB+
#   libQt6Pdf  (QtPdf is in `excludes` already; the lib still came)  ~4 MB
#   Qt's own .qm catalogs for 100+ languages we don't ship  ......  ~6 MB
#
# What is deliberately NOT pruned:
#   * the OTHER platforminputcontexts plugins (ibus / fcitx / compose) —
#     those are real input methods, and dropping them would break CJK and
#     dead-key entry. Only the virtual keyboard goes.
#   * wayland-shell-integration — load-bearing on KDE Wayland.
#   * QtMultimedia and its ffmpeg backend — the visualizer's QAudioDecoder
#     needs them (see the excludes note above).
# Losing libqgtk3 means GNOME users get Qt's own file dialog rather than
# the GTK one; jellytoast draws its whole UI with QSS regardless.
_QT_DROP_BASENAMES = {
    "libqtvirtualkeyboardplugin.so",
    "qtvirtualkeyboardplugin.dll",
    "libqgtk3.so",
}
_QT_DROP_STEMS = (
    "libQt6VirtualKeyboard",
    "Qt6VirtualKeyboard",
    *(() if QUICK_UI else ("libQt6Quick", "Qt6Quick", "libQt6Qml", "Qt6Qml")),
    "libQt6Pdf",
    "Qt6Pdf",
    "libgtk-3",
    "libgdk-3",
)


def _qt_prunable(dest: str) -> bool:
    base = os.path.basename(dest)
    if base in _QT_DROP_BASENAMES:
        return True
    return any(base.startswith(stem) for stem in _QT_DROP_STEMS)


_qt_dropped = [b for b in a.binaries if _qt_prunable(b[0])]
if _qt_dropped:
    a.binaries = [b for b in a.binaries if not _qt_prunable(b[0])]
    print(
        f"jellytoast.spec: pruned {len(_qt_dropped)} unused Qt binaries "
        "(virtual keyboard + its QML chain, GTK3 theme, QtPdf)"
    )

# --- Linux: keep only the QML the phone interface imports ------------------
# PySide6's QtQml hook collects the whole qml/ tree — Quick3D, WebEngine,
# Charts, Location, every Controls style … and the Qt libraries those
# plugins link. jellytoast/quick/qml imports QtQuick, QtQuick.Controls (Basic
# style), QtQuick.Layouts and QtQuick.Effects; keep those (+ what they load:
# Templates, Controls/impl, Window, QtQml) and drop the rest.
if QUICK_UI:
    def _qml_rel(dest: str):
        dest = dest.replace(os.sep, "/")
        marker = "PySide6/Qt/qml/"
        i = dest.find(marker)
        return dest[i + len(marker):].split("/") if i >= 0 else None

    def _qml_keep(parts) -> bool:
        if parts[0] == "QtQml":
            return len(parts) < 3 or parts[1] != "StateMachine"
        if parts[0] != "QtQuick":
            return False
        if len(parts) == 2:  # QtQuick's own qmldir / plugin
            return True
        if parts[1] in ("Layouts", "Effects", "Templates", "Window"):
            return True
        if parts[1] == "Controls":
            return len(parts) == 3 or parts[2] in ("Basic", "impl")
        return False

    def _qml_prunable(dest: str) -> bool:
        parts = _qml_rel(dest)
        return parts is not None and not _qml_keep(parts)

    # Qt libraries only the pruned QML modules use.
    _QML_ONLY_LIB_STEMS = tuple(
        f"libQt6{m}"
        for m in (
            "Quick3D", "Charts", "Graphs", "DataVisualization", "Location",
            "WebEngine", "WebView", "WebChannel", "WebSockets", "Positioning",
            "Sensors", "Scxml", "StateMachine", "RemoteObjects", "TextToSpeech",
            "3D", "QuickTest", "SpatialAudio", "Core5Compat", "QuickParticles",
            "QuickTimeline", "QuickVectorImage", "QuickDialogs2", "QmlLocalStorage",
            "QmlXmlListModel", "MultimediaQuick", "QuickControls2Material",
            "QuickControls2Universal", "QuickControls2Imagine", "QuickControls2Fusion",
            "QuickControls2FluentWinUI3", "QuickControls2Material", "LabsAnimation",
            "LabsFolderListModel", "LabsQmlModels", "LabsSettings", "LabsSharedImage",
            "LabsWavefrontMesh", "ShaderTools", "QmlDesignSupport", "QmlCompiler",
        )
    )

    def _lib_prunable(dest: str) -> bool:
        return os.path.basename(dest).startswith(_QML_ONLY_LIB_STEMS)

    _n_before = len(a.binaries) + len(a.datas)
    a.binaries = [b for b in a.binaries if not (_qml_prunable(b[0]) or _lib_prunable(b[0]))]
    a.datas = [d for d in a.datas if not _qml_prunable(d[0])]
    print(
        f"jellytoast.spec: pruned {_n_before - len(a.binaries) - len(a.datas)} QML files "
        "/ libraries the phone interface doesn't use"
    )

# Qt ships a .qm per module per language. Keep only the languages jellytoast
# actually offers — derived from i18n.SHIPPED_LANGUAGES so adding a language
# needs no spec edit — plus English. A pruned language simply falls back to
# Qt's built-in English strings, which is what an unshipped locale already did.
_keep_langs = {"en"}
try:
    sys.path.insert(0, str(REPO_ROOT))
    from jellytoast.i18n import SHIPPED_LANGUAGES as _SHIPPED

    _keep_langs |= {code for code, _en, _native in _SHIPPED}
except Exception as exc:  # pragma: no cover - build-time best effort
    print(f"jellytoast.spec: could not read SHIPPED_LANGUAGES ({exc}); keeping en only")


def _stale_qt_translation(dest: str) -> bool:
    d = dest.replace("\\", "/")
    if "/Qt/translations/" not in d or not d.endswith(".qm"):
        return False
    # Filenames are `<module>_<locale>.qm`, and BOTH halves can contain
    # underscores (`qt_help_ja`, `qtbase_es_MX`). Rather than parse that
    # ambiguity, test the last two segments and keep the file if either is
    # a language we ship — errs toward keeping, which is the safe direction.
    parts = os.path.basename(d)[: -len(".qm")].split("_")
    candidates = {parts[-1]}
    if len(parts) >= 2:
        candidates.add(parts[-2])
    return not (candidates & _keep_langs)


_tr_dropped = [d for d in a.datas if _stale_qt_translation(d[0])]
if _tr_dropped:
    a.datas = [d for d in a.datas if not _stale_qt_translation(d[0])]
    print(
        f"jellytoast.spec: pruned {len(_tr_dropped)} Qt translation catalogs; "
        f"kept {sorted(_keep_langs)}"
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

# --- macOS: wrap the COLLECT tree into a real .app bundle ------------------
# BUNDLE produces dist/jellytoast.app with a proper Info.plist so the app gets
# a Dock icon, Retina rendering, and the metadata Gatekeeper / the .dmg expect.
# Signing + notarization happen AFTER the build (codesign --options runtime
# with packaging/macos/entitlements.plist, then notarytool + stapler — see
# packaging/macos/sign_app.sh / notarize.sh). The entitlements are applied at
# sign time, not here.
if sys.platform == "darwin":
    # Read the version straight from pyproject so the bundle's CFBundleVersion
    # matches the release. Defensive: fall back to 0.0.0 if anything goes wrong
    # (a missing version must never break the build).
    try:
        import tomllib

        with open(REPO_ROOT / "pyproject.toml", "rb") as _f:
            _version = tomllib.load(_f)["project"]["version"]
    except Exception:
        _version = "0.0.0"

    # CFBundleVersion = a monotonic build number. Prefer the CI run number
    # (GITHUB_RUN_NUMBER): it increments on EVERY workflow run, so re-uploads of
    # the same marketing version are always accepted (ASC rejects a repeat build
    # number for a given CFBundleShortVersionString). The git commit count is a
    # local fallback — but CI's shallow checkout caps it at 1, which is exactly
    # why the run number has to win.
    import os as _os

    _build = _os.environ.get("GITHUB_RUN_NUMBER", "").strip()
    if not _build:
        try:
            import subprocess

            _build = subprocess.run(
                ["git", "rev-list", "--count", "HEAD"], cwd=str(REPO_ROOT),
                capture_output=True, text=True, check=True,
            ).stdout.strip()
        except Exception:
            _build = ""
    _build = _build or _version

    _icns = REPO_ROOT / "packaging" / "macos" / "jellytoast.icns"

    app = BUNDLE(
        coll,
        name="jellytoast.app",
        icon=str(_icns) if _icns.is_file() else None,
        bundle_identifier="io.github.wolfgangwarehaus.jellytoast",
        version=_version,
        info_plist={
            "CFBundleName": "jellytoast",
            "CFBundleDisplayName": "jellytoast",
            "CFBundleVersion": _build,
            "CFBundleShortVersionString": _version,
            "NSHighResolutionCapable": True,
            # Default floor for a local / MAS build (MAS validated at 12.0). The
            # Developer-ID .dmg legs OVERWRITE this per-arch in CI (release.yml
            # "Pin the macOS floor") to each bundle's REAL minimum, which the
            # runner OS dictates: the bundled Homebrew libmpv/FFmpeg dylibs won't
            # load on a macOS older than the one they were built on, whatever this
            # key claims. The .dmgs ship Intel on Sequoia 15+, Apple Silicon on
            # Sonoma 14+ (the per-arch pin); this default stays 12.0 for MAS.
            "LSMinimumSystemVersion": "12.0",
            "LSApplicationCategoryType": "public.app-category.music",
            # Export compliance: jellytoast uses only EXEMPT encryption (HTTPS +
            # standard AES for the local credential blob), i.e. no non-exempt
            # encryption. Declaring this auto-answers App Store Connect's "Missing
            # Compliance" question on every upload (this build's predecessor had to
            # answer it manually).
            "ITSAppUsesNonExemptEncryption": False,
            # macOS 15+ shows a one-time Local Network prompt the first time
            # the app browses the LAN for Chromecast/AirPlay/DLNA/Sonos
            # devices; this string is the explanation shown in that dialog.
            "NSLocalNetworkUsageDescription": (
                "jellytoast discovers and streams to Chromecast, AirPlay, "
                "DLNA and Sonos devices on your local network."
            ),
            # Bonjour/mDNS service types the cast discovery browses. REQUIRED
            # under the App Sandbox (and good hygiene for the .dmg too): without
            # this allow-list a sandboxed build's Bonjour browse returns nothing,
            # so Chromecast/AirPlay discovery silently dies. DLNA and
            # classic Sonos use SSDP (UPnP), not Bonjour — those ride the network
            # entitlement + the Local Network prompt, not this list.
            "NSBonjourServices": [
                "_googlecast._tcp",      # Chromecast (pychromecast)
                "_airplay._tcp",         # AirPlay (pyatv)
                "_raop._tcp",            # AirPlay audio / RAOP (pyatv)
                "_companion-link._tcp",  # AirPlay 2 companion/pairing (pyatv)
                "_sonos._tcp",           # Sonos (mDNS-advertising models)
            ],
        },
    )
