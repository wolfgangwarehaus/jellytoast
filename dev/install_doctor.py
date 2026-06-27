#!/usr/bin/env python
"""jellytoast install doctor — confirm a fresh install actually works.

Run this on a freshly-installed machine (a different laptop, Windows, etc.)
to verify the deployment-critical pieces resolved BEFORE you sink time into
manual testing. Unlike ``dev/smoke_test.py`` — which exercises app *logic*
from the source tree — this runs against the **installed package** and checks
the things that break a cross-machine install: the native libmpv, the Qt
platform plugin, the ``jellytoast`` entry point, and the bundled icon.

It is intentionally self-contained: no repo checkout, no server, no audio,
no writes. Copy this one file to the target machine and run it with the
interpreter that has jellytoast installed.

    # pipx install (run with THAT venv's interpreter):
    #   Linux:   ~/.local/share/pipx/venvs/jellytoast/bin/python install_doctor.py
    #   Windows: %USERPROFILE%\\pipx\\venvs\\jellytoast\\Scripts\\python.exe install_doctor.py
    #   (find it with:  pipx environment   or   pipx list --verbose)
    # or from a source checkout:
    #   python dev/install_doctor.py

Exit code = number of CRITICAL failures (0 = good to go). Platform features
that are *expected* to be absent off Linux/KDE (MPRIS, media keys, tray,
visualizer, keep-above, AirPlay) are reported as info, never failures.
"""
from __future__ import annotations

import importlib.util
import os
import platform
import sys

# Prefer an INSTALLED jellytoast (the whole point — test the install). Only if
# the package isn't importable at all do we fall back to a source checkout
# sitting next to this script, so `python dev/install_doctor.py` also works
# from a clone. find_spec doesn't execute the module, so a missing libmpv
# can't false-trigger the fallback.
if importlib.util.find_spec("jellytoast") is None:
    _repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.isfile(os.path.join(_repo, "jellytoast/app.py")) and _repo not in sys.path:
        sys.path.insert(0, _repo)

_TTY = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


PASS, FAIL, INFO, WARN = _c("32", "PASS"), _c("31", "FAIL"), _c("36", "info"), _c("33", "warn")
_crit = 0


def section(title: str) -> None:
    print("\n" + _c("1", f"── {title} "))


def critical(name: str, ok: bool, hint: str = "") -> bool:
    """A check that MUST pass for the app to run. Counts toward exit code."""
    global _crit
    if not ok:
        _crit += 1
    line = f"  [{PASS if ok else FAIL}] {name}"
    if hint and not ok:
        line += f"\n        → {hint}"
    print(line)
    return ok


def info(name: str, detail: str = "") -> None:
    print(f"  [{INFO}] {name}" + (f"  — {detail}" if detail else ""))


def warn(name: str, detail: str = "") -> None:
    print(f"  [{WARN}] {name}" + (f"  — {detail}" if detail else ""))


def main() -> int:
    is_win = sys.platform.startswith("win")
    is_mac = sys.platform == "darwin"
    print(_c("1", "jellytoast install doctor") + "  (no audio, no server, no writes)")
    info("platform", f"{platform.system()} {platform.release()} / Python {platform.python_version()} ({sys.platform})")

    # ── 1. Interpreter ───────────────────────────────────────────────────
    section("Interpreter")
    critical(
        "Python >= 3.11",
        sys.version_info >= (3, 11),
        f"jellytoast needs 3.11+; this is {platform.python_version()}. Reinstall under a newer Python.",
    )

    # ── 2. Native libmpv (THE #1 cross-machine gotcha) ───────────────────
    section("Native libmpv (audio engine)")
    if is_win:
        mpv_hint = (
            "python-mpv dlopen()s libmpv AT IMPORT. Put a 64-bit `libmpv-2.dll` "
            "somewhere on your PATH (a shinchiro/sourceforge mpv-dev build ships it). "
            "There is NO env-var override — it must be findable on PATH."
        )
    elif is_mac:
        mpv_hint = "Install mpv's lib: `brew install mpv` (provides libmpv)."
    else:
        mpv_hint = "Install libmpv: Arch `sudo pacman -S mpv`; Debian/Ubuntu `sudo apt install libmpv2` (or libmpv1)."
    try:
        import mpv  # noqa: F401

        critical("python-mpv imports + libmpv loads", True)
    except ImportError as e:
        # python-mpv itself missing (dependency not installed) — distinct from
        # the native lib missing, but same user-facing fix on most platforms.
        critical("python-mpv imports + libmpv loads", False, f"{e}\n        → {mpv_hint}")
    except OSError as e:
        # The classic: python-mpv is present but can't find/load the native lib.
        critical("python-mpv imports + libmpv loads", False, f"libmpv not loadable: {e}\n        → {mpv_hint}")

    # ── 3. The jellytoast package + entry point ──────────────────────────
    section("Package + entry point")
    try:
        import jellytoast  # the root entry module

        critical("`import jellytoast` succeeds", True)
        critical(
            "entry point target `jellytoast:main` exists",
            callable(getattr(jellytoast, "main", None)),
            "the `jellytoast` command won't launch without it — reinstall the wheel.",
        )
    except Exception as e:  # noqa: BLE001 - any import-time failure is a real blocker
        critical("`import jellytoast` succeeds", False, f"{type(e).__name__}: {e}")
        critical("entry point target `jellytoast:main` exists", False, "package failed to import (see above)")

    # ── 4. Bundled asset (icon) ──────────────────────────────────────────
    section("Bundled assets")
    try:
        import importlib.resources as ir

        svg = ir.files("jellytoast") / "assets" / "jellytoast.svg"
        critical(
            "app icon `jellytoast/assets/jellytoast.svg` is bundled",
            svg.is_file(),
            "the wheel is missing package-data — rebuild with the assets included.",
        )
    except Exception as e:  # noqa: BLE001
        critical("app icon `jellytoast/assets/jellytoast.svg` is bundled", False, f"{type(e).__name__}: {e}")

    # ── 5. Core modules import (no Qt app yet) ───────────────────────────
    section("Core modules")
    for mod in ("jellytoast.providers", "jellytoast.player_state", "jellytoast.async_io", "jellytoast.settings"):
        try:
            __import__(mod)
            critical(f"`import {mod}`", True)
        except Exception as e:  # noqa: BLE001
            critical(f"`import {mod}`", False, f"{type(e).__name__}: {e}")

    # ── 6. Optional / platform features (info only — never a failure) ────
    # (import-name, what it powers, pip-extra to enable it | None, expect-present)
    section("Optional features (degraded-but-fine if absent)")
    optional = [
        ("requests", "server HTTP", None, True),
        ("PySide6.QtSvg", "SVG icon rendering", None, True),
        ("numpy", "audio visualizer", "visualizer", False),
        ("async_upnp_client", "DLNA casting", "dlna", False),
        ("soco", "Sonos casting", "sonos", False),
        ("dbus_next", "MPRIS / media keys", None, not (is_win or is_mac)),
        ("pyatv", "AirPlay 2 casting", None, not is_win),
        ("Xlib", "media-key hotkeys", None, not (is_win or is_mac)),
    ]
    for mod, what, extra, expect in optional:
        try:
            __import__(mod)
            info(mod, what)
        except Exception:  # noqa: BLE001
            if extra:  # opt-in extra — show how to turn it on
                info(f"{mod} absent", f"{what} off — enable with:  pip install 'jellytoast[{extra}]'")
            elif expect:  # a base dep we DID expect on this platform
                warn(f"{mod} absent", f"{what} unavailable")
            else:  # platform feature that's expected-absent here
                info(f"{mod} absent", f"{what} not supported on this platform (expected)")

    # ── 7. Qt platform plugin — LAST (a broken plugin can hard-abort) ────
    section("Qt platform plugin (GUI) — done last; a fatal here aborts before the summary")
    try:
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance() or QApplication([])
        critical(
            "QApplication initializes (platform plugin loads)",
            app is not None,
            "Qt couldn't load its platform plugin — on minimal Linux add the xcb/wayland "
            "system libs; on Windows reinstall PySide6.",
        )
    except Exception as e:  # noqa: BLE001
        critical(
            "QApplication initializes (platform plugin loads)",
            False,
            f"{type(e).__name__}: {e}\n        → on minimal Linux install the Qt platform "
            "system libs (xcb/wayland); on Windows reinstall PySide6.",
        )

    # ── 8. Frosted-theme blur backdrop (cosmetic — never critical) ───────
    # The Frosted theme rides compositor blur; without it the body paints
    # near-opaque (legible, just no live glass). This surfaces WHY blur may
    # not land on a fresh install — the #1 cross-machine visual gotcha
    # ("Frosted dark renders see-through on the new laptop").
    section("Frosted-theme blur backdrop (cosmetic — Frosted degrades gracefully)")
    if is_win:
        info("Windows", "Frosted paints a near-opaque body for now (Mica backdrop pending); no action needed.")
    elif is_mac:
        info("macOS", "Frosted paints a near-opaque body for now (vibrancy pending); no action needed.")
    else:
        import ctypes

        lib_ok = False
        for so in ("libKF6WindowSystem.so.6", "libKF6WindowSystem.so"):
            try:
                ctypes.CDLL(so)
                lib_ok = True
                break
            except OSError:
                continue
        if not lib_ok:
            warn(
                "libKF6WindowSystem absent",
                "Frosted glass blur off → near-opaque body. Install it: Arch `sudo pacman -S kwindowsystem`.",
            )
        else:
            import glob

            plugin_found = any(
                glob.glob(os.path.join(p, "kf6", "kwindowsystem"))
                for p in (
                    "/usr/lib/qt6/plugins",
                    "/usr/lib64/qt6/plugins",
                    "/usr/lib/qt/plugins",
                    "/usr/lib/x86_64-linux-gnu/qt6/plugins",
                )
            )
            if not plugin_found:
                warn(
                    "kf6/kwindowsystem Qt plugin not found",
                    "blur can't be requested → near-opaque body. Reinstall `kwindowsystem` (ships the Qt plugin).",
                )
            else:
                info("libKF6WindowSystem + Qt plugin", "present")
            # Live verified status (uses the QApplication from section 7).
            try:
                from jellytoast.blur import status as _blur_status

                st = _blur_status(force=True)
                if st.value == "active":
                    info("compositor blur", "ACTIVE — Frosted renders true glass")
                else:
                    warn(
                        f"compositor blur {st.value}",
                        "Frosted paints a near-opaque body. Enable System Settings → Desktop "
                        "Effects → Blur and confirm compositing is on (or use Solid dark).",
                    )
            except Exception as e:  # noqa: BLE001
                info("compositor blur status", f"unavailable ({type(e).__name__})")

    # ── Summary ──────────────────────────────────────────────────────────
    section("Summary")
    if _crit:
        print(f"\n  {_c('31', f'{_crit} critical check(s) failed')} — fix the above before launching.")
    else:
        print(f"\n  {_c('32', 'all critical checks passed')} — try:  jellytoast")
        info("first launch", "run from a real console to see startup logs: `JT_LOG_LEVEL=DEBUG jellytoast`")
    return _crit


if __name__ == "__main__":
    raise SystemExit(main())
