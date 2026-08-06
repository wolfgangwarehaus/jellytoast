"""Diagnostics bundle — the one-click support report.

``collect_report()`` returns a plain-text snapshot of everything a jellytoast
bug report usually needs three round-trips to gather: version + install
channel, OS / session type (Wayland vs X11, which desktop), Qt + PySide6,
whether libmpv actually loaded, the active theme and the VERIFIED blur status
with its human reason, which backend you're signed in to, offline-mode state,
the cover-pipeline counters, a settings dump, and the tail of the log file when
``jellytoast.log`` is installed.

SECRETS NEVER APPEAR. The report is designed to be pasted straight into a
public GitHub issue, so:

  * the ``credentials/`` subtree is skipped wholesale, and any key whose name
    smells like a secret (token / password / secret / auth / key / api_key) is
    dropped — that covers ``server/token``, the ListenBrainz / Last.fm tokens
    and the AirPlay pairing blobs without this module needing to know they
    exist;
  * ``jellytoast.credentials`` is never imported or read here at all;
  * the server URL is reduced to scheme + host — no path, no query string
    (same reasoning as ``ui_helpers._redact_url``, which strips Subsonic's
    ``p``/``t``/``s``/``u`` and Jellyfin's ``api_key``), and no username.

``tests/test_diagnostics.py`` plants fake secrets and asserts none of them
reach the output; keep that test honest when adding sections.

The Settings dialog's "Copy diagnostics" button puts the report on the
clipboard; headless callers can just print it.
"""

from __future__ import annotations

import sys

# Settings keys are skipped when their path contains one of these fragments —
# belt-and-braces on top of the credentials/ subtree exclusion, so a secret
# stashed under an app-specific key (server/token, scrobble/lastfm_session_key)
# is still safe.
_SECRET_FRAGMENTS = (
    "credentials/",
    "token",
    "password",
    "secret",
    "auth",
    "key",
    "api_key",
    "pairing",
)

# Keys that aren't secret but leak more of the user's setup than a bug report
# needs — the server host/user identity lives in the "server:" line above the
# dump instead, already reduced to scheme + host.
_PRIVATE_KEYS = ("server/url", "server/username", "server/hostnames")


def _is_secret_key(key: str) -> bool:
    k = key.lower()
    return any(frag in k for frag in _SECRET_FRAGMENTS) or k in _PRIVATE_KEYS


def _redact_server_url(url: str) -> str:
    """``https://music.example.com/jf?api_key=…`` → ``https://music.example.com``.
    Scheme + host only: the path can carry a reverse-proxy prefix and the query
    can carry auth (see ``ui_helpers._redact_url``), and neither helps debug."""
    if not url:
        return "(not configured)"
    try:
        from urllib.parse import urlparse

        parts = urlparse(url)
        if not parts.scheme or not parts.hostname:
            return "<url>"
        host = parts.hostname
        if parts.port:
            host = f"{host}:{parts.port}"
        return f"{parts.scheme}://{host}"
    except Exception:
        return "<url>"


def _session_type() -> str:
    """wayland / x11 / windows / macos — the platform half a paste never
    includes but a chrome bug always needs."""
    import os

    from jellytoast.platform_compat import IS_LINUX, IS_MACOS, IS_WINDOWS

    if IS_WINDOWS:
        return "windows"
    if IS_MACOS:
        return "macos"
    if IS_LINUX:
        session = os.environ.get("XDG_SESSION_TYPE") or "unknown"
        desktop = os.environ.get("XDG_CURRENT_DESKTOP") or ""
        return f"{session}{f' ({desktop})' if desktop else ''}"
    return sys.platform


def _mpv_line() -> str:
    """Whether libmpv loaded, and which client API it speaks. python-mpv binds
    libmpv at IMPORT time, so if the module is already in ``sys.modules`` this
    costs nothing; if it isn't (a report collected before playback ever
    started) we don't force the load — an absent/mismatched libmpv is exactly
    the failure this line is meant to surface, and importing it here to find
    out could take the report down with it."""
    mod = sys.modules.get("mpv")
    if mod is None:
        try:
            import importlib.util

            found = importlib.util.find_spec("mpv") is not None
        except Exception:
            found = False
        return "python-mpv installed, libmpv not loaded yet" if found else "not available"
    bits = []
    ver = getattr(mod, "__version__", None)
    if ver:
        bits.append(f"python-mpv {ver}")
    try:
        api = mod._mpv_client_api_version()
        bits.append("libmpv client API %d.%d" % (api[0], api[1]))
    except Exception:
        bits.append("libmpv loaded")
    return ", ".join(bits)


def _settings_lines() -> list[str]:
    from jellytoast.settings import get_settings

    qs = get_settings()._s
    lines = []
    for key in sorted(qs.allKeys()):
        if _is_secret_key(key):
            continue
        try:
            val = qs.value(key)
        except Exception:
            val = "<unreadable>"
        if isinstance(val, (bytes, bytearray)) or type(val).__name__ == "QByteArray":
            val = f"<binary {len(val)}B>"  # window geometry etc. — noise, not signal
        lines.append(f"  {key} = {val}")
    return lines or ["  (empty)"]


def _log_tail(max_lines: int = 100) -> list[str]:
    """The last ~100 lines of the active log file, or a one-line explanation
    when file logging isn't installed. Bounded read (64 KB) so a huge log
    can't stall the settings dialog."""
    try:
        from jellytoast import log as jlog

        path = jlog.log_file_path()
        if path is None or not path.is_file():
            return ["  (file logging not installed — relaunch with JT_LOG=debug)"]
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 65536))
            text = f.read().decode("utf-8", "replace")
        lines = text.splitlines()[-max_lines:]
        return [f"  {ln}" for ln in lines] or ["  (log is empty)"]
    except Exception as e:
        return [f"  (log unavailable: {e})"]


def collect_report() -> str:
    """The full plain-text diagnostics report. Every section is best-effort —
    a failing probe becomes a line in the report, never an exception out of
    the support path."""
    import platform

    from jellytoast.version import __version__

    out: list[str] = []
    out.append("=== jellytoast diagnostics ===")
    try:
        from jellytoast import updates

        channel = updates.get_channel()
    except Exception:
        channel = "unknown"
    out.append(f"app: jellytoast {__version__} ({channel})")

    # ── platform ──────────────────────────────────────────────────────
    try:
        out.append(f"os: {platform.platform()}")
    except Exception:
        out.append("os: <unavailable>")
    out.append(f"session: {_session_type()}")
    out.append(f"python: {sys.version.split()[0]}")
    try:
        import PySide6
        from PySide6.QtCore import qVersion

        out.append(f"qt: {qVersion()} (PySide6 {PySide6.__version__})")
    except Exception:
        out.append("qt: <unavailable>")
    try:
        out.append(f"mpv: {_mpv_line()}")
    except Exception:
        out.append("mpv: <unavailable>")

    # ── theme + blur ──────────────────────────────────────────────────
    try:
        from jellytoast.settings import get_settings

        s = get_settings()
        out.append(
            f"theme: mode={s.theme_mode} family={s.theme_family} "
            f"accent={s.accent_color} font_scale={s.font_scale}"
        )
    except Exception:
        out.append("theme: <unavailable>")
    try:
        from jellytoast import blur

        out.append(f"blur: {blur.status().value} — {blur.reason()}")
    except Exception:
        out.append("blur: <unavailable>")

    # ── server / session (no credentials, host only) ──────────────────
    try:
        from jellytoast.settings import get_settings

        s = get_settings()
        kind = (s.provider_kind or "jellyfin").lower()
        # bool() of the token — never the token itself.
        signed_in = "yes" if s.access_token else "no"
        out.append(f"provider: {kind} (signed in: {signed_in})")
        out.append(f"server: {_redact_server_url(s.server_url)}")
    except Exception:
        out.append("provider: <unavailable>")
    try:
        from jellytoast import offline

        out.append(f"offline mode: {'on' if offline.is_offline_mode() else 'off'}")
    except Exception:
        out.append("offline mode: <unavailable>")

    # ── cover pipeline ────────────────────────────────────────────────
    # The counters behind JT_COVER_DIAG — "art stopped loading" reports live
    # or die on these, and they're a cheap read of module globals.
    try:
        from jellytoast.ui_helpers import cover_pipeline_stats

        stats = cover_pipeline_stats()
        out.append(
            "covers: " + " ".join(f"{k}={v}" for k, v in sorted(stats.items()))
        )
    except Exception:
        out.append("covers: <unavailable>")

    # ── settings (secrets excluded) ───────────────────────────────────
    out.append("")
    out.append("--- settings (credentials/secrets excluded) ---")
    try:
        out.extend(_settings_lines())
    except Exception as e:
        out.append(f"  (settings unavailable: {e})")

    # ── log tail ──────────────────────────────────────────────────────
    out.append("")
    out.append("--- log tail (last 100 lines) ---")
    out.extend(_log_tail())

    return "\n".join(out) + "\n"


def copy_to_clipboard() -> bool:
    """collect_report() → the system clipboard. Returns False when there's no
    QApplication (headless caller — print the report instead)."""
    try:
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is None:
            return False
        QApplication.clipboard().setText(collect_report())
        return True
    except Exception:
        return False
