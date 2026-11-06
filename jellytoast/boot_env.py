"""Process-wide boot setup shared by both front ends — what has to happen
BEFORE the first Qt import (locale for libmpv, Qt env, the KDE plugin path,
HiDPI policy), and the crash / diagnostics / shutdown helpers.

``jellytoast.app`` (the classic window) imports this first thing;
``jellytoast.quick.main`` (the phone UI) imports it instead of ``app`` —
``app`` pulls in the whole desktop UI, mpv and D-Bus at import time, which
on a phone is over a second before anything can show. Import-time side
effects here are deliberate and idempotent.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

logging.basicConfig(
    level=os.environ.get("JT_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("jellytoast")

from jellytoast.platform_compat import IS_LINUX, will_be_wayland  # noqa: E402

# libmpv requires LC_NUMERIC=C; Qt's setlocale() undoes Python-side fixes.
# Setting it before any libmpv / Qt import is enough — libmpv reads it
# lazily on first use. (We used to os.execve here for the same effect,
# but that doesn't exist on Windows; mutating os.environ is portable
# and works for our case because nothing has loaded mpv yet.)
os.environ.pop("LC_ALL", None)
os.environ["LC_NUMERIC"] = "C"
os.environ.setdefault("LANG", "C.UTF-8")

# Silence the cosmetic Qt portal warning ("Could not register app ID … App
# info not found for 'jellytoast'") that fires on installs without a .desktop
# file (pipx, run-from-source). Appended so it doesn't clobber a user's own
# QT_LOGGING_RULES; must be set before QApplication so Qt picks it up.
_qt_rules = os.environ.get("QT_LOGGING_RULES", "")
os.environ["QT_LOGGING_RULES"] = (
    _qt_rules + ";" if _qt_rules else ""
) + "qt.qpa.services.warning=false"

# Identify the audio stream as "jellytoast" in the system mixer
# (KDE Plasma's Audio Volume → Applications, pavucontrol, etc.) instead
# of "python3.14". PulseAudio + PipeWire-PA compat read these
# ``PULSE_PROP_*`` env vars at client-connection time and stamp them
# into PA_PROP_APPLICATION_NAME / _APPLICATION_ICON_NAME, which is what
# the mixer shows. mpv's ``audio-client-name`` option only renames the
# per-stream media slot — not the application row above it.
# Set BEFORE any audio client (mpv, anything PulseAudio-aware) can connect.
os.environ.setdefault("PULSE_PROP_application.name", "jellytoast")
os.environ.setdefault("PULSE_PROP_application.icon_name", "jellytoast")


def _theme_sizes(theme: str) -> list[int]:
    import struct

    for base in (
        "/usr/share/icons",
        os.path.expanduser("~/.icons"),
        os.path.expanduser("~/.local/share/icons"),
    ):
        path = os.path.join(base, theme, "cursors", "default")
        if not os.path.exists(path):
            continue
        try:
            with open(path, "rb") as f:
                data = f.read()
            _, _, _, ntoc = struct.unpack("<4sIII", data[:16])
            sizes = set()
            for i in range(ntoc):
                off = 16 + i * 12
                typ, sub, _ = struct.unpack("<III", data[off : off + 12])
                if typ == 0xFFFD0002:  # Xcursor IMAGE chunk
                    sizes.add(sub)
            return sorted(sizes)
        except Exception:
            continue
    return []


def _bootstrap_cursor_env():
    # X11/XWayland-only concept. On Wayland, KWin renders cursors
    # itself; on Windows / macOS the OS owns the cursor entirely.
    if not IS_LINUX or will_be_wayland():
        return
    try:
        theme = os.environ.get("XCURSOR_THEME", "")
        if not theme:
            from configparser import ConfigParser

            cfg = ConfigParser(strict=False)
            cfg.read(os.path.expanduser("~/.config/kcminputrc"))
            theme = cfg.get("Mouse", "cursorTheme", fallback="").strip()
            if theme:
                os.environ["XCURSOR_THEME"] = theme
        if "XCURSOR_SIZE" not in os.environ:
            import subprocess

            out = subprocess.run(
                ["xrdb", "-query"],
                capture_output=True,
                text=True,
                timeout=1,
            ).stdout
            requested = 0
            for line in out.splitlines():
                if line.startswith("Xcursor.size:"):
                    s = line.split(":", 1)[1].strip()
                    if s.isdigit():
                        requested = int(s)
                    break
            if theme and requested:
                available = _theme_sizes(theme)
                if available:
                    # Smallest size >= requested, else the largest.
                    size = next((s for s in available if s >= requested), available[-1])
                    os.environ["XCURSOR_SIZE"] = str(size)
                else:
                    os.environ["XCURSOR_SIZE"] = str(requested)
    except Exception:
        pass


_bootstrap_cursor_env()

# Source-checkout convenience: make the directory HOLDING the package
# importable regardless of launch cwd (`import jellytoast` already
# succeeded to get here, but child tooling that re-execs the interpreter
# inherits sys.path via PYTHONPATH-less spawns). Must be the package's
# PARENT — inserting the package dir itself would let its module names
# (settings, theme, …) shadow top-level imports.
_PKG_PARENT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_PKG_PARENT_DIR))

# KDE + a pip/pipx-bundled PySide6: the bundled Qt can't see the system KDE
# Qt plugins, so KWindowSystem fails to load its platform backend and frosted
# blur silently degrades to the near-opaque fallback. Point Qt at the system
# plugin dir so blur works on a plain `pipx install`. No-op on a distro/system
# PySide6, off KDE, or with JT_NO_QT_PLUGIN_FIX=1. MUST run before the
# PySide6 import below — Qt reads QT_PLUGIN_PATH at plugin-load
# time. See jellytoast/kde_qt_plugin_fix.py.
from jellytoast.kde_qt_plugin_fix import heal_qt_plugin_path  # noqa: E402

_qt_plugin_added = heal_qt_plugin_path()
if _qt_plugin_added:
    logger.debug(
        "KDE + bundled Qt: added %s to QT_PLUGIN_PATH for compositor blur",
        _qt_plugin_added,
    )


def _setup_hidpi() -> None:
    """High-DPI scaffolding for Qt 6 / PySide6.

    Qt 6 turns HiDPI ON by default; the Qt 5 ceremony
    (AA_EnableHighDpiScaling / AA_UseHighDpiPixmaps /
    AA_DisableWindowContextHelpButton) is now a no-op and intentionally
    omitted. What still matters:

    - **Rounding policy.** Qt 6's default is ``PassThrough``, which is
      what we want: at KDE fractional scales (125 % / 150 % / 175 %)
      Qt renders at the true buffer size via wp_fractional_scale_v1,
      so text and chrome stay sharp. We set it explicitly anyway so a
      future Qt default change (or a user with ``QT_SCALE_FACTOR_*``
      already exported) lands on a known-good policy. ``Round`` would
      lose density on 125 % laptops; ``RoundPreferFloor`` is the only
      sane alternative if a future widget glitches at 1.5 ×.
    - **Per-Monitor V2 (Windows).** Qt 6 already requests it at
      startup, no manifest required. PyInstaller builds must keep the
      bundled manifest's ``dpiAwareness`` at ``PerMonitorV2`` — never
      downgrade to ``System``.
    - **NSHighResolutionCapable (macOS).** Must be ``true`` in the
      bundled ``Info.plist``; without it AppKit pixel-doubles the app
      and Retina rendering is lost. The bundler concern, not a
      runtime call.
    - **Wayland fractional scaling.** Qt 6.7+ talks
      ``wp_fractional_scale_v1`` to KWin natively. Do NOT force
      ``QT_QPA_PLATFORM=xcb`` to "fix" blur reports from older Qt
      versions; xcb + XWayland produces worse fractional-scale output
      than native Wayland on Plasma 6.

    Must run before ``QApplication`` is constructed — the rounding
    policy is consulted during Qt platform-plugin init.
    """
    # Power-user override path: respect an existing env var rather
    # than clobbering it, so ``QT_SCALE_FACTOR_ROUNDING_POLICY=Round
    # ./jellytoast/app.py`` works for users with picky displays.
    os.environ.setdefault("QT_SCALE_FACTOR_ROUNDING_POLICY", "PassThrough")
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QGuiApplication

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    _apply_phone_scale()


# Extra whole-UI scale on a phone shell, on top of the shell's own display
# scale. Off by default: the shell's scale is the user's size choice, and
# stacking ours on it squeezed a 402 px-wide phone (shell at 3×) to 268 px —
# one album column, the mini player overlapping itself. (1.5 was a crutch
# for a shell left at 2×, ~600 px wide.) JT_PHONE_SCALE=<n> still opts in;
# an exported QT_SCALE_FACTOR always wins.
PHONE_SCALE = 1.0


def _apply_phone_scale() -> None:
    from jellytoast.platform_compat import is_mobile_shell

    if "QT_SCALE_FACTOR" in os.environ or not is_mobile_shell():
        return
    try:
        scale = float(os.environ.get("JT_PHONE_SCALE", PHONE_SCALE))
    except ValueError:
        scale = PHONE_SCALE
    if scale > 0 and scale != 1:
        os.environ["QT_SCALE_FACTOR"] = f"{scale:g}"


def _shutdown_log(msg: str) -> None:
    """Record a shutdown step to both stderr and a log file.

    The file (``/tmp/jellytoast-shutdown.log``) survives the launch
    terminal closing — so when the app is killed by closing its
    terminal, there's still a record of whether the signal handler
    fired and how far cast cleanup got. Diagnostic aid for the
    "Chromecast keeps playing after the app exits" class of bug."""
    import time as _t

    line = f"{_t.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    try:
        with open("/tmp/jellytoast-shutdown.log", "a") as f:
            f.write(line + "\n")
    except Exception:
        pass
    logger.info("%s", msg)


# Held for process lifetime so faulthandler's file sink isn't garbage-
# collected/closed out from under it on GUI-subsystem builds.
_CRASH_LOG_FH = None


def _diag_log_dir():
    """Best-effort writable dir for crash/diagnostic logs, resolved WITHOUT
    a QApplication (this runs before one exists). Windows → ``%LOCALAPPDATA%
    \\jellytoast``; otherwise ``$XDG_CACHE_HOME`` (or ``~/.cache``)
    ``/jellytoast``. Returns None if nothing is writable."""
    import os as _os

    try:
        if _os.name == "nt":
            base = _os.environ.get("LOCALAPPDATA") or _os.path.expanduser("~")
        else:
            base = _os.environ.get("XDG_CACHE_HOME") or _os.path.join(
                _os.path.expanduser("~"), ".cache"
            )
        d = _os.path.join(base, "jellytoast")
        _os.makedirs(d, exist_ok=True)
        return d
    except Exception:
        return None


def _enable_faulthandler() -> None:
    """Convert a hard native crash (embedded libmpv, or a cross-thread
    ~QObject — jellytoast's documented SIGSEGV class) into an
    attributable Python+C stack instead of silent process death.

    On a normal interpreter this writes to stderr. Under a GUI-subsystem
    interpreter (the pipx ``jellytoast.exe`` gui-script on Windows /
    pythonw) ``sys.stderr`` is ``None`` and a bare ``enable()`` raises
    ``RuntimeError`` — the 2026-06-10 silent no-window launch failure. The
    old code just skipped it there, which is exactly why the first external
    user's Windows 'crash' left ZERO trace and was undiagnosable. Now we
    instead point faulthandler at a crash-log FILE and attach a file log so
    a windowed-build user can hand back ``crash.log`` / ``jellytoast.log``
    (the connectivity + offline-mode transitions log at INFO)."""
    import faulthandler

    if sys.stderr is not None:
        try:
            faulthandler.enable()
        except Exception:
            # e.g. a stderr replaced by a fileno-less stream (test capture,
            # embedded hosts) — losing the crash hook must never be fatal.
            pass
        return

    # stderr is None (GUI-subsystem build) — route to files instead.
    d = _diag_log_dir()
    if not d:
        return
    import os as _os

    global _CRASH_LOG_FH
    try:
        _CRASH_LOG_FH = open(_os.path.join(d, "crash.log"), "a", buffering=1)
        faulthandler.enable(file=_CRASH_LOG_FH, all_threads=True)
    except Exception:
        pass
    try:
        import logging as _logging

        level = (_os.environ.get("JT_LOG_LEVEL") or "INFO").upper()
        fh = _logging.FileHandler(_os.path.join(d, "jellytoast.log"))
        fh.setFormatter(_logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root = _logging.getLogger()
        root.addHandler(fh)
        root.setLevel(getattr(_logging, level, _logging.INFO))
    except Exception:
        pass


def tune_malloc() -> bool:
    """glibc: give freed image buffers back to the OS.

    Album art comes and goes in ~1 MB buffers (a 512² cover). glibc's
    dynamic mmap threshold grows past that after the first big free, so
    those buffers then live on the main heap — and a freed heap block
    under live ones is never returned. On the 6T a 12-fling browse took
    the app from 377 MB to 794 MB resident with 338 MB of it *free* in
    the allocator (``mallinfo2``); ``malloc_trim`` gave 244 MB straight
    back. A fixed mmap threshold keeps image-sized buffers in their own
    mappings (returned on free), and a low trim threshold hands the heap
    top back promptly. Both read by glibc at the next allocation, so this
    runs at import, before Qt. No-op off glibc (musl, Windows, macOS).
    True when applied."""
    if not sys.platform.startswith("linux"):
        return False
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6")
        mallopt = libc.mallopt
        mallopt.argtypes = (ctypes.c_int, ctypes.c_int)
        mallopt.restype = ctypes.c_int
        M_TRIM_THRESHOLD, M_TOP_PAD, M_MMAP_THRESHOLD = -1, -2, -3
        ok = mallopt(M_MMAP_THRESHOLD, 256 * 1024) == 1
        ok = mallopt(M_TRIM_THRESHOLD, 4 * 1024 * 1024) == 1 and ok
        ok = mallopt(M_TOP_PAD, 1024 * 1024) == 1 and ok
        return ok
    except Exception as e:  # musl has no mallopt; nothing to tune
        logger.debug("malloc tuning unavailable: %s", e)
        return False


_MALLOC_TUNED = tune_malloc()


def install_signal_handlers(app) -> None:
    """SIGINT / SIGTERM / SIGHUP → a clean ``app.quit()`` (cast cleanup, queue
    save). Python only runs a signal handler on its own bytecode, and the Qt
    event loop runs none: the handler is woken through ``signal.set_wakeup_fd``
    and a socket notifier, so the process sleeps for real between events
    (a 200 ms "wake the interpreter" timer, the old way, is 5 wake-ups a
    second for the app's whole life — on a phone, battery). The timer stays
    as the fallback where the wake-up fd can't be set up."""
    import signal

    def _graceful_shutdown(signum, _frame):
        for _s in (signal.SIGINT, signal.SIGTERM, getattr(signal, "SIGHUP", None)):
            if _s is not None:
                signal.signal(_s, signal.SIG_IGN)
        if hasattr(signal, "alarm"):
            signal.alarm(5)  # a hung cleanup never leaves a zombie
        _shutdown_log(f"shutdown signal {signum} received -> app.quit()")
        app.quit()

    for _signame in ("SIGINT", "SIGTERM", "SIGHUP"):
        _signum = getattr(signal, _signame, None)
        if _signum is not None:  # SIGHUP is POSIX-only
            signal.signal(_signum, _graceful_shutdown)

    try:
        import socket

        from PySide6.QtCore import QSocketNotifier

        rsock, wsock = socket.socketpair()
        rsock.setblocking(False)
        wsock.setblocking(False)
        signal.set_wakeup_fd(wsock.fileno(), warn_on_full_buffer=False)
        notifier = QSocketNotifier(rsock.fileno(), QSocketNotifier.Type.Read, app)

        def _drain():
            try:
                rsock.recv(64)
            except (BlockingIOError, InterruptedError):
                pass
            # Reaching here IS the handler running: the interpreter ran
            # Python bytecode to get into this slot.

        notifier.activated.connect(_drain)
        app._jt_signal_wake = (rsock, wsock, notifier)
    except Exception as e:
        logger.debug("signal wake-up fd unavailable (%s); polling instead", e)
        from PySide6.QtCore import QTimer

        _sig_wake = QTimer(app)
        _sig_wake.timeout.connect(lambda: None)
        _sig_wake.start(200)
        app._jt_signal_wake = _sig_wake
