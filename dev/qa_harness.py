#!/usr/bin/env python3
"""Cross-platform visual + feature QA harness for jellytoast.

Drives the LIVE app through the test bridge (``jellytoast/test_bridge.py``) to
walk every surface and capture a REAL-COMPOSITED-SCREEN screenshot of each — so
window blur / vibrancy / Acrylic is actually in the shot (the bridge's own
``win.grab()`` is blur-blind; on Wayland Qt's screen grab is blocked entirely) —
in dark AND light themes and across window states, then runs the smoke test.
Output is a screenshot gallery + a Markdown manifest you (or a Claude session on
that box) can read back to judge visual consistency surface-to-surface and
platform-to-platform.

This is the SAME harness for every OS; only the screen-capture backend differs
and is auto-detected:
    macOS         screencapture -x {path}
    KDE (X11/Way) spectacle -f -b -n -o {path}
    GNOME/X11     gnome-screenshot -f {path}  ->  scrot {path}  ->  import -window root {path}
    Windows       PowerShell System.Drawing CopyFromScreen (whole virtual screen)
Override with --capture "tool ... {path}" if detection picks wrong.

────────────────────────────────────────────────────────────────────────────
PREREQ — launch the app with the bridge first, sharing TMPDIR so this client
reaches the same per-user socket (load-bearing on macOS/Linux; harmless on Win):

    # macOS / Linux:
    TMPDIR=/tmp JT_TEST_BRIDGE=1 python3 -m jellytoast &
    # Windows (PowerShell):
    $env:JT_TEST_BRIDGE=1; python -m jellytoast

Then run this harness in the SAME shell (so TMPDIR matches on mac/linux):
    TMPDIR=/tmp python3 dev/qa_harness.py            # add --dialogs for modals

SAFETY: this writes REAL settings (theme_mode) through the bridge, so it SAVES
your current theme_mode up front and RESTORES it (+ the window to Normal) in a
finally block. If killed mid-run, restore with:
    python3 dev/jt_ctl.py exec "get_settings().theme_mode='frosted_dark'; \
        __import__('jellytoast.ui_helpers',fromlist=['x']).refresh_theme()"
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import time

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

from dev.jt_drive import Bridge, BridgeError  # noqa: E402

# (label, bridge-exec to navigate there). The _NavMixin methods live on `win`.
# If a nav call errors on your build, read jellytoast/nav_controller.py and fix
# the call here — the harness logs which surface failed so it's obvious.
SURFACES = [
    ("albums", 'win._show_native_music_grid("album")'),
    ("artists", 'win._show_native_music_grid("artist")'),
    ("songs", "win._show_songs_view()"),
    ("genres", "win._show_genres_view()"),
    ("suggestions", "win._show_suggestions_view()"),
    ("radio", "win._show_radio_view()"),
    ("downloads", "win._show_downloads_library_view()"),
    ("smart_playlists", "win._show_smart_playlists_view()"),
    ("search", "win._show_search_view()"),
    ("now_playing", "win._show_now_playing()"),
]

DEFAULT_THEMES = ["frosted_dark", "frosted_light"]


# ── capture backend ──────────────────────────────────────────────────────
def _run(cmd) -> bool:
    try:
        subprocess.run(cmd, check=True, timeout=25)
        return True
    except Exception as e:
        print(f"  ! capture cmd failed ({cmd[0]}): {e}")
        return False


def _powershell_capture(path: str) -> bool:
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms,System.Drawing;"
        "$b=[System.Windows.Forms.SystemInformation]::VirtualScreen;"
        "$bmp=New-Object System.Drawing.Bitmap $b.Width,$b.Height;"
        "$g=[System.Drawing.Graphics]::FromImage($bmp);"
        "$g.CopyFromScreen($b.Location,[System.Drawing.Point]::Empty,$b.Size);"
        f"$bmp.Save('{path}');$g.Dispose();$bmp.Dispose()"
    )
    return _run(["powershell", "-NoProfile", "-Command", ps])


def detect_capture(override: str | None):
    """Return (name, fn(path)->bool) for capturing the full composited screen."""
    if override:
        return (
            override.split()[0],
            lambda path: _run(
                [p.replace("{path}", path) for p in override.split()]
            ),
        )
    sysname = platform.system()
    if sysname == "Darwin":
        return ("screencapture", lambda path: _run(["screencapture", "-x", path]))
    if sysname == "Windows":
        return ("powershell", _powershell_capture)
    # Linux: prefer composited-screen tools that work under the live session.
    # spectacle covers KDE on BOTH X11 and Wayland (grim is wlroots-only and
    # does NOT work on KWin — don't use it here).
    if shutil.which("spectacle"):
        return (
            "spectacle",
            lambda path: _run(["spectacle", "-f", "-b", "-n", "-o", path]),
        )
    if shutil.which("gnome-screenshot"):
        return ("gnome-screenshot", lambda path: _run(["gnome-screenshot", "-f", path]))
    if shutil.which("scrot"):
        return ("scrot", lambda path: _run(["scrot", path]))
    if shutil.which("import"):
        return ("import", lambda path: _run(["import", "-window", "root", path]))
    return ("none", None)


def capture(fn, path: str) -> bool:
    if fn is None:
        return False
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    return fn(path) and os.path.exists(path)


# ── drive helpers ────────────────────────────────────────────────────────
def raise_window(b: Bridge) -> None:
    try:
        b.x("win.raise_(); win.activateWindow()")
    except BridgeError:
        pass


def set_theme(b: Bridge, mode: str) -> None:
    # Emit theme_changed too — refresh_theme() updates the palette tokens but the
    # per-surface QSS re-stamp + the blur/vibrancy re-apply only fire on
    # theme_changed (the live-apply contract). Without it the light-theme
    # captures show stale frost (surfaced by the macOS test pass, issue #197/F6).
    # icons.refresh_theme() MUST run between refresh_theme() and the
    # theme_changed emit — the real app does exactly this (settings_dialog
    # _on_theme_changed / app._on_os_scheme_changed) so the icon-tint globals
    # (ICON_DIM / ICON_BRIGHT) are fresh before subscribers like the top bar
    # re-issue their glyphs on theme_changed. Omitting it left the captured
    # light-theme toolbar glyphs one switch behind (stale dark tint → invisible
    # on the light frost) — a capture artifact, not an app bug (Windows pass,
    # 2026-06-28).
    b.x(
        f"get_settings().theme_mode = {mode!r}; "
        "from jellytoast import ui_helpers as _u, icons as _ic; "
        "_u.refresh_theme(); _ic.refresh_theme(); "
        "bus.theme_changed.emit()"
    )


def capture_surfaces(b, shot, out, themes, manifest):
    for theme in themes:
        try:
            set_theme(b, theme)
        except BridgeError as e:
            print(f"  ! could not set theme {theme!r}: {e}")
        b.pump(0.6)
        for label, nav in SURFACES:
            ok = True
            try:
                b.x(nav)
            except BridgeError as e:
                ok = False
                print(f"  ! nav {label} failed: {e}")
            b.pump(0.7)
            raise_window(b)
            b.pump(0.25)
            fn = f"{theme}__{label}.png"
            got = capture(shot, os.path.join(out, fn))
            manifest.append((theme, label, fn, ok and got))
            print(f"  [{'ok' if ok and got else '..'}] {theme} / {label}")


def capture_states(b, shot, out, theme, manifest):
    try:
        set_theme(b, theme)
        b.pump(0.4)
        b.x('win._show_native_music_grid("album")')
        b.pump(0.5)
        for label, call, settle in [
            ("state_maximized", "win.showMaximized()", 0.9),
            ("state_fullscreen", "win.showFullScreen()", 1.6),
            ("state_normal", "win.showNormal()", 0.9),
        ]:
            b.x(call)
            b.pump(settle)
            raise_window(b)
            b.pump(0.2)
            fn = f"zz_{label}.png"
            capture(shot, os.path.join(out, fn))
            manifest.append((theme, label, fn, True))
            print(f"  [..] {label}")
        b.x("win.showNormal()")
        b.pump(0.3)
    except BridgeError as e:
        print(f"  ! window-state pass failed: {e}")


def capture_mini(b, shot, out, theme, manifest):
    try:
        b.x("mini.show(); mini.raise_()")
        b.pump(0.9)
        fn = "zz_mini_player.png"
        capture(shot, os.path.join(out, fn))
        manifest.append((theme, "mini_player", fn, True))
        print("  [..] mini_player")
        b.x("mini.hide()")
        b.pump(0.2)
    except BridgeError as e:
        print(f"  ! mini-player pass failed: {e}")


def capture_dialogs(b, shot, out, theme, manifest):
    """Best-effort modal capture. Opens via a deferred timer so the bridge RPC
    returns before the modal's nested loop starts, then closes the same way. If
    flaky on your build, skip it and capture dialogs by hand (open the dialog,
    run your platform's screenshot command)."""
    for label, opener in [("settings", "win._open_settings()")]:
        try:
            b.x(f"from PySide6.QtCore import QTimer as _T; _T.singleShot(0, lambda: {opener})")
            time.sleep(1.0)
            fn = f"zz_dialog_{label}.png"
            capture(shot, os.path.join(out, fn))
            manifest.append((theme, f"dialog_{label}", fn, True))
            print(f"  [..] dialog_{label}")
            b.x(
                "from PySide6.QtCore import QTimer as _T; "
                "_T.singleShot(0, lambda: (QApplication.activeModalWidget() "
                "and QApplication.activeModalWidget().close()))"
            )
            b.pump(0.5)
        except BridgeError as e:
            print(f"  ! dialog {label} failed: {e}")


def write_manifest(out, manifest, smoke_path, capture_name):
    lines = [
        "# jellytoast QA gallery",
        "",
        f"Capture backend: `{capture_name}` (full composited screen).",
        "Review for: frost/blur reads as glass over the desktop (not opaque, not",
        "see-through-broken), no blank/black/mis-draw, rounded corners intact,",
        "text legible over the body, mini matches main, dark vs light both right.",
        "",
    ]
    by_theme: dict[str, list] = {}
    extras = []
    for theme, label, fn, ok in manifest:
        if label.startswith("state_") or label == "mini_player" or label.startswith("dialog_"):
            extras.append((label, fn))
        else:
            by_theme.setdefault(theme, []).append((label, fn, ok))
    for theme, rows in by_theme.items():
        lines += [f"## {theme}", ""]
        for label, fn, ok in rows:
            flag = "" if ok else "  ⚠️ nav/capture failed"
            lines += [f"### {label}{flag}", f"![{label}]({fn})", ""]
    if extras:
        lines += ["## window states / mini / dialogs", ""]
        for label, fn in extras:
            lines += [f"### {label}", f"![{label}]({fn})", ""]
    if smoke_path and os.path.exists(smoke_path):
        lines += ["## smoke test", f"See `{os.path.basename(smoke_path)}`.", ""]
    path = os.path.join(out, "manifest.md")
    with open(path, "w") as f:
        f.write("\n".join(lines))
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="cross-platform visual + feature QA harness")
    ap.add_argument("--out", default=os.path.join(os.environ.get("TMPDIR", "/tmp"), "jt_qa_gallery"))
    ap.add_argument("--themes", default=",".join(DEFAULT_THEMES))
    ap.add_argument("--capture", default=None, help='override capture cmd, e.g. "scrot {path}"')
    ap.add_argument("--dialogs", action="store_true")
    ap.add_argument("--no-smoke", action="store_true")
    args = ap.parse_args()

    cap_name, shot = detect_capture(args.capture)
    if shot is None:
        print("ERROR: no screen-capture tool found. Install one (spectacle / "
              "gnome-screenshot / scrot / imagemagick) or pass --capture.")
        return 3
    print(f"Capture backend: {cap_name}")

    out = args.out
    themes = [t.strip() for t in args.themes.split(",") if t.strip()]
    b = Bridge()
    try:
        alive = b.try_e("1")[0] is True
    except BridgeError:
        alive = False
    if not alive:
        print(
            "ERROR: can't reach the test bridge. Launch the app first with "
            "JT_TEST_BRIDGE=1 (and TMPDIR=/tmp on mac/linux, same shell)."
        )
        return 2

    orig_mode = b.e("get_settings().theme_mode")
    print(f"Saved theme_mode = {orig_mode!r} (will restore). Gallery -> {out}\n")
    manifest: list = []
    try:
        capture_surfaces(b, shot, out, themes, manifest)
        capture_states(b, shot, out, themes[0], manifest)
        capture_mini(b, shot, out, themes[0], manifest)
        if args.dialogs:
            capture_dialogs(b, shot, out, themes[0], manifest)
    finally:
        try:
            set_theme(b, orig_mode)
            b.x("win.showNormal(); win.raise_()")
            print(f"\nRestored theme_mode = {orig_mode!r}")
        except Exception as e:
            print(f"  ! restore FAILED (set theme_mode back to {orig_mode!r}): {e}")

    smoke_path = ""
    if not args.no_smoke:
        print("\nRunning smoke test...")
        os.makedirs(out, exist_ok=True)
        smoke_path = os.path.join(out, "smoke.txt")
        try:
            # UTF-8 both on the file AND the child's stdout: smoke_test.py
            # prints box-drawing glyphs (─) that crash on Windows' default
            # cp1252 stream (UnicodeEncodeError) before any check runs. Force
            # UTF-8 so the smoke output is captured on every platform (Windows
            # pass, 2026-06-28).
            smoke_env = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
            with open(smoke_path, "w", encoding="utf-8", errors="replace") as f:
                subprocess.run(
                    [sys.executable, os.path.join(_ROOT, "dev", "smoke_test.py")],
                    cwd=_ROOT, stdout=f, stderr=subprocess.STDOUT, timeout=240,
                    env=smoke_env,
                )
            print(f"  smoke output -> {smoke_path}")
        except Exception as e:
            print(f"  ! smoke test failed to run: {e}")

    mpath = write_manifest(out, manifest, smoke_path, cap_name)
    n_ok = sum(1 for *_, ok in manifest if ok)
    print(f"\nDone: {n_ok}/{len(manifest)} captures ok. Manifest -> {mpath}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
