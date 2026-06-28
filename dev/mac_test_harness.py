#!/usr/bin/env python3
"""macOS visual + feature test harness for jellytoast.

Drives the LIVE app through the test bridge (``jellytoast/test_bridge.py``) to
walk every surface and capture a REAL-BLUR screenshot of each — macOS
``screencapture`` of the composited screen, NOT the blur-blind ``win.grab()`` —
in dark AND light themes and across window states, then runs the smoke test.
The output is a screenshot gallery + a Markdown manifest so a reviewer (you, or
the Claude session running on the Mac) can read the captures back and judge
visual consistency surface-to-surface and against the Linux/KWin reference.

WHY a separate harness: the bridge's ``win.grab()`` paints only Qt's own pixels,
so it never shows the NSVisualEffectView vibrancy behind the window. To judge
the frosted glass you must capture the COMPOSITED screen — that's what
``screencapture`` does here.

────────────────────────────────────────────────────────────────────────────
PREREQ — launch the app with the bridge first, sharing TMPDIR so this client
reaches the same per-user socket (the load-bearing gotcha):

    cd jellytoast && source .venv/bin/activate
    TMPDIR=/tmp JT_TEST_BRIDGE=1 python3 -m jellytoast &

Then run this harness from the SAME shell (so TMPDIR matches):

    TMPDIR=/tmp python3 dev/mac_test_harness.py

Flags:
    --out DIR        gallery dir (default /tmp/jt_mac_gallery)
    --display N      screencapture -D index, if the app is on a non-main display
    --themes A,B     theme_mode values to sweep (default frosted_dark,frosted_light)
    --dialogs        also capture modal dialogs (Settings/Cast) — best-effort
    --no-smoke       skip the smoke test

SAFETY: this writes REAL settings (theme_mode) through the bridge, so it SAVES
your current theme_mode up front and RESTORES it (and the window to Normal) in a
finally block. If it's killed mid-run, restore manually:
    TMPDIR=/tmp python3 dev/jt_ctl.py exec "get_settings().theme_mode='frosted_dark'; \
        __import__('jellytoast.ui_helpers',fromlist=['x']).refresh_theme()"
"""

from __future__ import annotations

import argparse
import os
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


def screen_capture(path: str, display: int | None = None) -> bool:
    """Capture the composited macOS screen (real vibrancy) to ``path``."""
    d = os.path.dirname(path)
    if d:
        os.makedirs(d, exist_ok=True)
    cmd = ["screencapture", "-x"]
    if display is not None:
        cmd += ["-D", str(display)]
    cmd.append(path)
    try:
        subprocess.run(cmd, check=True, timeout=20)
        return os.path.exists(path)
    except Exception as e:  # pragma: no cover - macOS-only
        print(f"  ! screencapture failed: {e}")
        return False


def raise_window(b: Bridge) -> None:
    try:
        b.x("win.raise_(); win.activateWindow()")
    except BridgeError:
        pass


def set_theme(b: Bridge, mode: str) -> None:
    # Emit theme_changed too — NOT just refresh_theme(). On macOS the native
    # vibrancy appearance (VibrantDark/VibrantLight) is only re-applied on the
    # PlayerBus.theme_changed signal (wired to app._apply_blur); refresh_theme()
    # alone updates only the Qt palette. Without the emit, the light-theme
    # captures show a light body over a STALE DARK vibrancy backdrop — a
    # misleading frost. This mirrors the real Settings path, which emits
    # theme_changed after setting theme_mode.
    b.x(
        f"get_settings().theme_mode = {mode!r}; "
        "from jellytoast import ui_helpers as _u; _u.refresh_theme(); "
        "from jellytoast.player_state import PlayerBus as _B; _B.get().theme_changed.emit()"
    )


def capture_surfaces(b, out, themes, display, manifest):
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
            got = screen_capture(os.path.join(out, fn), display)
            manifest.append((theme, label, fn, ok and got))
            print(f"  [{'ok' if ok and got else '..'}] {theme} / {label}")


def capture_states(b, out, theme, display, manifest):
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
            screen_capture(os.path.join(out, fn), display)
            manifest.append((theme, label, fn, True))
            print(f"  [..] {label}")
        b.x("win.showNormal()")
        b.pump(0.3)
    except BridgeError as e:
        print(f"  ! window-state pass failed: {e}")


def capture_mini(b, out, theme, display, manifest):
    try:
        b.x("mini.show(); mini.raise_()")
        b.pump(0.9)
        fn = "zz_mini_player.png"
        screen_capture(os.path.join(out, fn), display)
        manifest.append((theme, "mini_player", fn, True))
        print("  [..] mini_player")
        b.x("mini.hide()")
        b.pump(0.2)
    except BridgeError as e:
        print(f"  ! mini-player pass failed: {e}")


def capture_dialogs(b, out, theme, display, manifest):
    """Best-effort modal-dialog capture. Opens via a deferred timer so the
    bridge RPC returns before the dialog's nested event loop starts, then
    closes it the same way. If this hangs/flakes on your build, skip it and
    capture dialogs manually (open the dialog, then run:
        screencapture -x /tmp/jt_mac_gallery/zz_settings.png )."""
    dialogs = [
        ("settings", "win._open_settings()"),
    ]
    for label, opener in dialogs:
        try:
            b.x(f"from PySide6.QtCore import QTimer as _T; _T.singleShot(0, lambda: {opener})")
            time.sleep(1.0)  # client-side; let the modal open + paint
            fn = f"zz_dialog_{label}.png"
            screen_capture(os.path.join(out, fn), display)
            manifest.append((theme, f"dialog_{label}", fn, True))
            print(f"  [..] dialog_{label}")
            # Close the topmost modal via a deferred timer (don't spin a loop
            # from inside this RPC — schedule and return).
            b.x(
                "from PySide6.QtCore import QTimer as _T; "
                "_T.singleShot(0, lambda: (QApplication.activeModalWidget() "
                "and QApplication.activeModalWidget().close()))"
            )
            b.pump(0.5)
        except BridgeError as e:
            print(f"  ! dialog {label} failed: {e}")


def write_manifest(out, manifest, smoke_path):
    lines = [
        "# jellytoast macOS test gallery",
        "",
        "Real-blur `screencapture` of every surface. Review for: frost reads as",
        "glass over the desktop (not opaque), no blank/black/mis-draw, rounded",
        "corners intact, text legible over the lighter glass, mini matches main.",
        "",
    ]
    by_theme: dict[str, list] = {}
    extras = []
    for theme, label, fn, ok in manifest:
        if label.startswith("state_") or label in ("mini_player",) or label.startswith("dialog_"):
            extras.append((label, fn, ok))
        else:
            by_theme.setdefault(theme, []).append((label, fn, ok))
    for theme, rows in by_theme.items():
        lines.append(f"## {theme}")
        lines.append("")
        for label, fn, ok in rows:
            flag = "" if ok else "  ⚠️ nav/capture failed"
            lines.append(f"### {label}{flag}")
            lines.append(f"![{label}]({fn})")
            lines.append("")
    if extras:
        lines.append("## window states / mini / dialogs")
        lines.append("")
        for label, fn, _ok in extras:
            lines.append(f"### {label}")
            lines.append(f"![{label}]({fn})")
            lines.append("")
    if smoke_path and os.path.exists(smoke_path):
        lines.append("## smoke test")
        lines.append(f"See `{os.path.basename(smoke_path)}` in this folder.")
        lines.append("")
    path = os.path.join(out, "manifest.md")
    with open(path, "w") as f:
        f.write("\n".join(lines))
    return path


def main() -> int:
    ap = argparse.ArgumentParser(description="macOS visual + feature test harness")
    ap.add_argument("--out", default="/tmp/jt_mac_gallery")
    ap.add_argument("--display", type=int, default=None)
    ap.add_argument("--themes", default=",".join(DEFAULT_THEMES))
    ap.add_argument("--dialogs", action="store_true")
    ap.add_argument("--no-smoke", action="store_true")
    args = ap.parse_args()

    tmp = os.environ.get("TMPDIR", "")
    if tmp.rstrip("/") != "/tmp":
        print("WARNING: TMPDIR is not /tmp — the bridge socket may be unreachable.")
        print("  Launch the app AND this harness with TMPDIR=/tmp.\n")

    out = args.out
    themes = [t.strip() for t in args.themes.split(",") if t.strip()]
    b = Bridge()
    try:
        alive = b.try_e("1")[0] is True
    except BridgeError:
        alive = False
    if not alive:
        print(
            "ERROR: can't reach the test bridge. Launch the app first with:\n"
            "  TMPDIR=/tmp JT_TEST_BRIDGE=1 python3 -m jellytoast &\n"
            "(and run this harness with TMPDIR=/tmp in the same shell)."
        )
        return 2

    orig_mode = b.e("get_settings().theme_mode")
    print(f"Saved theme_mode = {orig_mode!r} (will restore). Gallery -> {out}\n")
    manifest: list = []
    try:
        capture_surfaces(b, out, themes, args.display, manifest)
        capture_states(b, out, themes[0], args.display, manifest)
        capture_mini(b, out, themes[0], args.display, manifest)
        if args.dialogs:
            capture_dialogs(b, out, themes[0], args.display, manifest)
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
        smoke_path = os.path.join(out, "smoke.txt")
        os.makedirs(out, exist_ok=True)
        try:
            with open(smoke_path, "w") as f:
                subprocess.run(
                    [sys.executable, os.path.join(_ROOT, "dev", "smoke_test.py")],
                    cwd=_ROOT,
                    stdout=f,
                    stderr=subprocess.STDOUT,
                    timeout=240,
                )
            print(f"  smoke output -> {smoke_path}")
        except Exception as e:
            print(f"  ! smoke test failed to run: {e}")

    mpath = write_manifest(out, manifest, smoke_path)
    n_ok = sum(1 for *_, ok in manifest if ok)
    print(f"\nDone: {n_ok}/{len(manifest)} captures ok. Manifest -> {mpath}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
