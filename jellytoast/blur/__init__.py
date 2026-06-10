"""Window "blur behind" — the frosted-glass effect for translucent
surfaces. This is what visually separates Frosted mode (blurred glass)
from Transparent mode (clear glass).

Public API:
    is_supported() -> bool          # backend can *request* blur
    apply(widget, enabled) -> bool  # enable/disable blur behind widget
    status() -> BlurStatus          # is a real backdrop VERIFIED behind us?

The status() call is the one that fixes the "frosted renders see-through"
class of bug: `apply()` is fire-and-forget (KWin gives no success signal),
so a frosted body painted at ~67% opacity goes transparent-broken wherever
blur silently no-ops (Blur effect off, missing plugin, no compositing, a
non-KDE desktop, Windows < 11). `status()` probes whether a real backdrop
is actually present so the theme layer can pick the body alpha — full glass
when blur landed, a near-opaque "frosted panel" fallback when it didn't.
See jellytoast/theme.body_color_for() and docs/research/portable_blur.md.

Backend (Linux): KDE's KWindowSystem — `KWindowEffects::enableBlurBehind`
to request blur and `KWindowEffects::isEffectAvailable(BlurBehind)` to
verify it, both via ctypes (no PySide6 binding exists). KWindowSystem
speaks `ext-background-effect-v1` where the compositor offers it and falls
back to the legacy `org_kde_kwin_blur` — so this covers KWin, and also niri
/ COSMIC where KWindowSystem is installed.

Everywhere else (Windows, macOS, or a Linux box without KWindowSystem, or a
compositor with no blur protocol — Hyprland/Wayfire/sway/GNOME): the
backend reports UNSUPPORTED and the window paints a near-opaque body. On
compositors that blur via user config rather than a protocol (Hyprland,
Wayfire, SwayFX), the user can target jellytoast in their own window rules:
our Wayland app_id is the stable string "jellytoast" (set via
setDesktopFileName).
"""

from __future__ import annotations

import enum
import os
import sys


class BlurStatus(enum.Enum):
    """Whether a real compositor/OS backdrop is verified to sit behind the
    window — the signal the theme layer maps to a body opacity.

    ACTIVE                 — blur issued AND positive evidence it landed;
                             frosted surfaces ride it at full glass alpha.
    REQUESTED_UNVERIFIABLE — blur was issued but we can't confirm it took
                             (no success feedback, or a known-flaky path
                             like KDE X11); paint a conservative near-opaque
                             body so we never gamble on a see-through window.
    UNSUPPORTED            — the backend can't request blur here at all;
                             paint the near-opaque fallback body.
    DISABLED               — the active theme doesn't ask for blur (Solid /
                             Transparent); not an error. Bodies keep their
                             own opaque/translucent alpha unchanged.
    """

    ACTIVE = "active"
    REQUESTED_UNVERIFIABLE = "unverifiable"
    UNSUPPORTED = "unsupported"
    DISABLED = "disabled"


if sys.platform.startswith("linux"):
    from jellytoast.blur import _kwin as _backend
elif sys.platform == "win32":  # pragma: no cover - exercised on Windows
    from jellytoast.blur import _dwm as _backend
elif sys.platform == "darwin":  # pragma: no cover - exercised on macOS
    from jellytoast.blur import _macos as _backend
else:  # pragma: no cover
    from jellytoast.blur import _unsupported as _backend


# Process-wide cache. Blur availability is a per-session compositor/OS fact,
# not a per-window one (KWindowEffects::isEffectAvailable takes no window),
# so we probe once and reuse. force=True re-probes after a compositing /
# Blur-effect toggle.
_status_cache: BlurStatus | None = None

# Debug override: JT_BLUR_FORCE=active|unverifiable|unsupported pins the
# reported status, bypassing the probe. Lets you eyeball the near-opaque
# fallback body (JT_BLUR_FORCE=unsupported) without disabling the
# compositor's Blur effect, and the reverse on a box where blur is off.
# Same JT_* debug-switch family as JT_OPAQUE / JT_SHUFFLE_DEBUG.
_FORCE = os.environ.get("JT_BLUR_FORCE", "").strip().lower()


def opaque_mode_active() -> bool:
    """Dev diagnostic: fully-opaque chrome — no translucency, no blur — via the
    ``JT_OPAQUE=1`` env switch. When on, :func:`status` reports UNSUPPORTED (so
    frosted bodies + popups use their near-opaque fallback) and :func:`apply`
    skips requesting compositor blur.

    Env-only on purpose — there is NO user-facing setting. A frosted theme that
    can't get real blur already falls back to a near-opaque body automatically
    (status() → UNSUPPORTED/REQUESTED_UNVERIFIABLE), which covers the real user
    need; the old Settings toggle additionally dropped WA_TranslucentBackground
    and broke the window's rounded corners, so it was removed. JT_OPAQUE stays
    for the screencast / streaming-flicker repro it was born for. Never raises."""
    return os.environ.get("JT_OPAQUE") == "1"


def is_supported() -> bool:
    """True if the backend can *request* blur (KWindowSystem present /
    Windows 11 build). A True here doesn't guarantee the compositor will
    actually blur — that's what status() checks."""
    return _backend.is_supported()


def apply(
    widget, enabled: bool, corner_radius: int = 0, dark: bool | None = None
) -> bool:
    """Enable (``enabled=True``) or remove (``False``) compositor blur
    behind ``widget``'s window. ``widget`` is a QWidget; its QWindow must
    already exist (call after ``show()``).

    ``corner_radius``: when > 0 the blur region is shaped to a rounded
    rectangle of that radius matching the widget's current size — pass this
    for frameless rounded windows (mini player, settings dialog) so the blur
    doesn't bleed into the transparent corners. ``0`` blurs the whole window
    rectangle — correct for server-side-decorated windows.

    ``dark``: dark vs light variant for backends whose backdrop has one
    (Windows Mica's immersive dark/light tint). ``None`` (the default)
    resolves it from the active theme, so every call site gets the right
    variant for free; the KWin / macOS / unsupported backends ignore it.

    Returns True if the request was issued, False on any unsupported /
    not-yet-shown case. The return is best-effort ("issued", not "blurred")
    — use status() to learn whether blur actually landed. Never raises."""
    if enabled and opaque_mode_active():
        # User forced opaque chrome — never request blur (and remove any
        # already-applied blur on this widget).
        enabled = False
    # "Never raises" is part of the contract (blur is progressive enhancement)
    # — theme resolution + the backend call are best-effort, so swallow errors.
    try:
        if dark is None:
            from jellytoast.theme import get_active_theme

            dark = get_active_theme().dark
        return _backend.apply(widget, enabled, corner_radius, dark)
    except Exception:
        return False


def status(*, force: bool = False) -> BlurStatus:
    """Whether a real backdrop is VERIFIED behind our windows on this
    machine/session — the value the theme layer uses to choose body opacity.

    Computed once and cached (the answer is a per-session compositor/OS
    fact). Pass ``force=True`` to re-probe, e.g. after the window is mapped
    or a compositing/Blur-effect toggle. Returns ACTIVE /
    REQUESTED_UNVERIFIABLE / UNSUPPORTED — never DISABLED (that's the theme's
    call, not the machine's). Never raises; any failure resolves to the
    conservative REQUESTED_UNVERIFIABLE so a frosted body stays near-opaque
    rather than risking see-through."""
    global _status_cache
    if opaque_mode_active():
        # User forced opaque chrome — report no backdrop so frosted bodies +
        # popups fall back to their near-opaque alpha.
        return BlurStatus.UNSUPPORTED
    if _FORCE:
        try:
            forced = BlurStatus(_FORCE)
        except ValueError:
            forced = None  # unrecognised value → ignore, probe normally
        # status() reports a machine capability and never returns DISABLED
        # (that's the theme's call) — so JT_BLUR_FORCE=disabled is ignored.
        if forced is not None and forced is not BlurStatus.DISABLED:
            return forced
    if _status_cache is not None and not force:
        return _status_cache
    try:
        _status_cache = _backend.probe()
    except Exception:
        _status_cache = BlurStatus.REQUESTED_UNVERIFIABLE
    return _status_cache


def reason() -> str:
    """A short, human-readable explanation of the current blur status — for
    the boot log and the Settings hint when a frosted theme can't get real
    blur (e.g. "GNOME has no app-controllable window blur", "KWin's Blur
    effect is off", "Windows 10 has no Mica backdrop"). Reads status()
    (cached) + the environment via the active backend. Never raises."""
    st = status()
    try:
        return _backend.reason(st)
    except Exception:
        if st is BlurStatus.ACTIVE:
            return "compositor blur active"
        return "compositor blur unavailable — using a near-opaque body"
