"""KWindowSystem blur backend — `KWindowEffects::enableBlurBehind()`
reached through ctypes against ``libKF6WindowSystem``.

Why ctypes: KF6 ships no Python binding, and `QtWaylandClient` (which
would let us marshal the blur protocol ourselves) isn't bundled with
PySide6. ``enableBlurBehind`` is a plain — if mangled — C++ symbol, so
ctypes is the clean route. KWindowSystem itself does the hard parts:
it speaks `ext-background-effect-v1` where the compositor advertises it
and falls back to the legacy `org_kde_kwin_blur`, translates an empty
QRegion to "blur the whole window", and re-applies blur via its own
event filter whenever the Wayland surface is recreated — so callers
just call once per show / theme change.

Everything is best-effort: a missing library or symbol, a window with
no platform surface yet, a compositor with no blur protocol — all
resolve to a silent no-op. Blur is pure progressive enhancement.
"""

from __future__ import annotations

import ctypes

# KWindowEffects::enableBlurBehind(QWindow *, bool, QRegion const &)
# Itanium-mangled. ABI-stable for KF6's lifetime; guarded anyway.
_SYMBOL = "_ZN14KWindowEffects16enableBlurBehindEP7QWindowbRK7QRegion"
# KWindowEffects::isEffectAvailable(KWindowEffects::Effect) — the verify
# half. Static method (no implicit this), takes the Effect enum by value.
# Length prefix 17 vs 16 above; confirmed present in libKF6WindowSystem.so.6.
_AVAIL_SYMBOL = "_ZN14KWindowEffects17isEffectAvailableENS_6EffectE"
# KWindowEffects::Effect::BlurBehind, from kwindoweffects.h (KF6). Verified
# both against the header and at runtime (isEffectAvailable(7) -> True on a
# blur-capable KWin, isEffectAvailable(<bogus>) -> False).
_BLUR_BEHIND = 7
_SONAMES = ("libKF6WindowSystem.so.6", "libKF6WindowSystem.so")

_fn = None  # resolved ctypes callable, or None if unavailable
_resolved = False  # resolution attempted yet?

_avail_fn = None  # resolved isEffectAvailable callable, or None
_avail_resolved = False


def _resolve():
    """Load libKF6WindowSystem and bind the enableBlurBehind symbol.
    Cached — the result is stable for the process lifetime."""
    global _fn, _resolved
    if _resolved:
        return _fn
    _resolved = True
    for soname in _SONAMES:
        try:
            lib = ctypes.CDLL(soname)
        except OSError:
            continue
        try:
            fn = lib[_SYMBOL]
        except (AttributeError, KeyError):
            continue
        # (QWindow*, bool, QRegion*) — pointers passed as void*.
        fn.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_void_p]
        fn.restype = None
        _fn = fn
        return _fn
    return None


def _resolve_avail():
    """Load libKF6WindowSystem and bind isEffectAvailable. Cached, guarded
    exactly like _resolve() — a missing symbol (ABI drift) yields None so
    probe() degrades to REQUESTED_UNVERIFIABLE rather than crashing."""
    global _avail_fn, _avail_resolved
    if _avail_resolved:
        return _avail_fn
    _avail_resolved = True
    for soname in _SONAMES:
        try:
            lib = ctypes.CDLL(soname)
        except OSError:
            continue
        try:
            fn = lib[_AVAIL_SYMBOL]
        except (AttributeError, KeyError):
            continue
        fn.argtypes = [ctypes.c_int]  # Effect enum, passed by value
        fn.restype = ctypes.c_bool
        _avail_fn = fn
        return _avail_fn
    return None


def is_supported() -> bool:
    return _resolve() is not None


def _blur_effect_active():
    """KDE-only confirmatory cross-check via in-process QtDBus. Returns True
    if KWin reports the Blur effect loaded (and compositing not explicitly
    off), False if either is explicitly off, None if inconclusive (DBus
    unavailable, not KDE, or any error). Best-effort; never raises — a None
    means "trust the isEffectAvailable capability bit alone"."""
    try:
        from PySide6.QtDBus import QDBusConnection, QDBusInterface
    except Exception:
        return None
    try:
        bus = QDBusConnection.sessionBus()
        if not bus.isConnected():
            return None
        eff = QDBusInterface("org.kde.KWin", "/Effects", "org.kde.kwin.Effects", bus)
        if not eff.isValid():
            return None  # not Plasma / KWin not on the bus
        reply = eff.call("isEffectLoaded", "blur")
        args = reply.arguments()
        loaded = bool(args[0]) if args else None
        if loaded is False:
            return False  # user disabled the Blur desktop effect
        comp = QDBusInterface(
            "org.kde.KWin", "/Compositor", "org.kde.kwin.Compositing", bus
        )
        if comp.isValid() and comp.property("active") is False:
            return False  # compositing suspended
        return loaded  # True, or None if the /Effects reply was empty
    except Exception:
        return None


_delivery_ok = None  # tri-state self-test result: True / False / None
_delivery_tested = False


def _blur_request_reaches_compositor():
    """Does ``enableBlurBehind`` actually reach the compositor here?

    KWindowSystem talks to Wayland through Qt's native interface, and that
    interface is REVISION-CHECKED: a KF6 compiled against one Qt minor
    cannot obtain it from another. When the check fails, Qt hands back
    nullptr, and ``enableBlurBehind`` returns normally WITHOUT ever sending
    ``org_kde_kwin_blur.create`` — the request is dropped on the floor and
    nothing blurs. Meanwhile ``isEffectAvailable()`` still answers True (it
    needs no native interface), so the capability gate in probe() sails
    through and we paint full-transparency glass over an UNBLURRED desktop.
    That is exactly the 0.2.0 flatpak bug (#229): the sandbox shipped
    PySide6's bundled Qt over the runtime's, so KF6 — built against the
    runtime's Qt — was handed a Qt it couldn't speak to.

    The failure is silent by design (blur is fire-and-forget; KWin sends no
    ack), and Qt reports it ONLY by logging. So we run the call once, for
    real, on a throwaway QWindow — created but never shown, so there is no
    visible artifact — with a message handler installed, and watch for the
    complaint.

    Returns True if the call ran clean (the request is being delivered),
    False if Qt refused the native interface (blur silently no-ops here),
    and None if the test couldn't be run at all (no QGuiApplication yet, no
    symbol, any error). Callers must NOT demote on None — absence of a
    verdict is not evidence of failure. Cached: the answer is a per-process
    fact (it depends on which Qt got loaded), and the window churn is not
    worth repeating. Never raises.
    """
    global _delivery_ok, _delivery_tested
    if _delivery_tested:
        return _delivery_ok
    _delivery_tested = True
    try:
        import ctypes as _ct

        import shiboken6
        from PySide6.QtCore import qInstallMessageHandler
        from PySide6.QtGui import QGuiApplication, QRegion, QWindow

        fn = _resolve()
        if fn is None or QGuiApplication.instance() is None:
            return _delivery_ok  # None — nothing to test against (yet)

        complained = []

        def _catch(mode, ctx, msg):
            # Qt logs the refusal as e.g. "Native interface revision mismatch
            # (requested 1 / available 2) for interface QWaylandApplication"
            # under the qt.nativeinterface category.
            if "revision mismatch" in msg or "native interface" in msg.lower():
                complained.append(msg)

        win = QWindow()
        win.create()  # platform window only — never shown, never mapped
        prev = qInstallMessageHandler(_catch)
        try:
            region = QRegion()
            fn(
                _ct.c_void_p(shiboken6.getCppPointer(win)[0]),
                True,
                _ct.c_void_p(shiboken6.getCppPointer(region)[0]),
            )
        finally:
            qInstallMessageHandler(prev)
            win.destroy()

        _delivery_ok = not complained
    except Exception:
        _delivery_ok = None  # inconclusive — never demote on a failed test
    return _delivery_ok


def _kreadconfig_bin():
    import shutil

    for cand in ("kreadconfig6", "kreadconfig5"):
        path = shutil.which(cand)
        if path:
            return path
    return None


def _blur_disabled_in_kwinrc():
    """True iff kwinrc explicitly disables the Blur effect — `[Plugins]
    blurEnabled=false`. A static config read via kreadconfig (no live D-Bus),
    so it catches a user who turned Blur off even where ``PySide6.QtDBus``
    isn't importable (it's a separate package on some distros). False when
    the effect is enabled, the key is absent (KWin default-on), it's not a
    KDE box (no kwinrc → empty), or kreadconfig is missing — we only ever use
    this to DEMOTE, never to grant ACTIVE."""
    bin_ = _kreadconfig_bin()
    if not bin_:
        return False
    try:
        import subprocess

        out = subprocess.run(
            [bin_, "--file", "kwinrc", "--group", "Plugins", "--key", "blurEnabled"],
            capture_output=True,
            text=True,
            timeout=2,
        )
        return out.stdout.strip().lower() == "false"
    except Exception:
        return False


def _blur_disabled():
    """A POSITIVE "blur won't render" signal, used only to demote a True
    capability bit on Wayland. Either source saying "off" is enough:

      * kwinrc `[Plugins] blurEnabled=false` (config read; survives a missing
        QtDBus — the failure mode the cross-check alone didn't cover), OR
      * KWin's D-Bus reports the Blur effect unloaded / compositing suspended.

    Both are KDE-only by construction (kwinrc + org.kde.KWin), so on a non-KDE
    Wayland compositor that genuinely offers app blur (niri's
    ext-background-effect-v1) neither fires → the honest capability bit stands
    and the surface stays full-glass. Absence of a signal is NOT taken as
    "disabled" — we never gamble a False here."""
    if _blur_disabled_in_kwinrc():
        return True
    if _blur_effect_active() is False:
        return True
    return False


def probe():
    """Return the verified BlurStatus for this session. See
    jellytoast/blur/__init__.py and docs/research/portable_blur.md.

    Strategy:
      1. If we can't even load enableBlurBehind → UNSUPPORTED.
      2. If the isEffectAvailable symbol won't bind → REQUESTED_UNVERIFIABLE
         (we issued blur but can't verify it).
      3. isEffectAvailable(BlurBehind) is the capability gate: on Wayland it
         reflects the ext-background-effect-v1 / org_kde_kwin_blur global; on
         X11 it requires compositing + the blur atom. False → UNSUPPORTED.
      4. KDE X11 honours the blur atom but can still silently skip the render
         pass (GPU mis-detect), invisible to the client → stay conservative
         (REQUESTED_UNVERIFIABLE). Only Wayland earns ACTIVE.
      5. On Wayland the capability bit can stay True even when KWin's Blur
         desktop effect is toggled OFF (the bit advertises the protocol, not
         the effect's enabled state). So demote to UNSUPPORTED on any positive
         "blur is disabled" signal (kwinrc config OR D-Bus); otherwise ACTIVE.
         The demotion signals are KDE-only, so a non-KDE compositor that
         honestly advertises blur (niri) keeps ACTIVE.
    """
    from jellytoast.blur import BlurStatus

    if _resolve() is None:
        return BlurStatus.UNSUPPORTED
    avail = _resolve_avail()
    if avail is None:
        return BlurStatus.REQUESTED_UNVERIFIABLE
    try:
        available = bool(avail(_BLUR_BEHIND))
    except Exception:
        return BlurStatus.REQUESTED_UNVERIFIABLE
    if not available:
        return BlurStatus.UNSUPPORTED

    from jellytoast.platform_compat import is_x11

    if is_x11():
        # Atom honoured but render-skip is undetectable from the client.
        return BlurStatus.REQUESTED_UNVERIFIABLE
    if _blur_disabled():
        return BlurStatus.UNSUPPORTED
    import os

    if os.environ.get("FLATPAK_ID"):
        from jellytoast.platform_compat import is_kde_desktop

        if is_kde_desktop() and _blur_effect_active() is None:
            # KDE inside a flatpak with an INCONCLUSIVE effect check: the
            # sandbox likely can't reach org.kde.KWin on the session bus (a
            # bundle built before the --talk-name=org.kde.KWin grant), so a
            # host with the Blur effect OFF is indistinguishable from one
            # with it on — while the Wayland capability bit stays True
            # either way. Trusting the bit painted full-transparency glass
            # over an UNBLURRED desktop (the 0.2.0 Steam Deck report).
            # Claim only what we can verify; the near-opaque frosted
            # fallback is the honest render. Outside the sandbox an
            # inconclusive check is rare (missing QtDBus) and host
            # behaviour is unchanged.
            return BlurStatus.REQUESTED_UNVERIFIABLE

    # Last honesty gate. Everything above asks the COMPOSITOR whether it can
    # blur; none of it checks whether our request ever gets there. On a Qt /
    # KWindowSystem version skew enableBlurBehind silently drops it while
    # every capability signal above still says yes — the #229 failure. Only
    # a False demotes: an inconclusive test (None) leaves behaviour as it was.
    if _blur_request_reaches_compositor() is False:
        return BlurStatus.REQUESTED_UNVERIFIABLE
    return BlurStatus.ACTIVE


# Non-KDE Linux desktops with NO app-controllable window-blur protocol — so
# the right user message is "your desktop can't do this", not "install / enable
# something". (Deliberately excludes KDE, and niri/COSMIC which DO speak
# ext-background-effect-v1.)
_NO_BLUR_DESKTOPS = (
    "gnome",
    "cinnamon",
    "xfce",
    "mate",
    "lxqt",
    "lxde",
    "unity",
    "pantheon",
)


def reason(status):
    """A short human explanation for the given BlurStatus on this box — used
    by the boot log + the Settings hint. Never raises."""
    from jellytoast.blur import BlurStatus
    from jellytoast.platform_compat import desktop_name, is_kde_desktop, is_x11

    if status is BlurStatus.ACTIVE:
        return "KWin blur active"
    de = desktop_name()
    if not is_kde_desktop() and any(k in de.lower() for k in _NO_BLUR_DESKTOPS):
        return f"{de or 'this desktop'} has no app-controllable window blur — using a near-opaque body"
    if is_x11():
        return "X11 session — blur can't be verified; using a near-opaque body"
    if _resolve() is None:
        return "KWindowSystem missing — install kwindowsystem for Frosted glass blur on KDE"
    if is_kde_desktop() and _blur_disabled():
        return "KWin's Blur effect is off — enable System Settings → Desktop Effects → Blur"
    if _blur_request_reaches_compositor() is False:
        # The compositor is willing; we're the ones who can't ask it.
        return (
            "KWindowSystem can't drive this Qt build (version skew) — blur "
            "requests are dropped; using a near-opaque body"
        )
    return "compositor blur unavailable here — using a near-opaque body"


def _rounded_region(widget, radius: int):
    """A QRegion shaped to a rounded rect matching ``widget``'s current
    (logical) size. Rasterised through a monochrome QBitmap mask —
    QRegion has no rounded-rect constructor. KWindowSystem scales the
    region by the window's DPR, so logical coordinates are correct."""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QBitmap, QPainter, QPainterPath, QRegion

    w, h = widget.width(), widget.height()
    if w <= 0 or h <= 0:
        return QRegion()  # not laid out yet — fall back to whole-window
    bmp = QBitmap(w, h)
    bmp.fill(Qt.GlobalColor.color0)  # color0 = outside the region
    path = QPainterPath()
    path.addRoundedRect(QRectF(0, 0, w, h), radius, radius)
    p = QPainter(bmp)
    # No AA — a region is a hard 1-bit mask; antialiased edge pixels
    # would just become ragged region boundary.
    p.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    p.fillPath(path, Qt.GlobalColor.color1)  # color1 = inside the region
    p.end()
    return QRegion(bmp)


def apply(
    widget,
    enabled: bool,
    corner_radius: int = 0,
    dark: bool = True,
    elevated: bool = False,
) -> bool:
    """Issue enableBlurBehind for ``widget``'s QWindow. ``corner_radius``
    > 0 shapes the blur region to a rounded rect; 0 = whole window.
    Returns False (no-op) if the lib is missing or the widget has no
    platform window yet."""
    fn = _resolve()
    if fn is None:
        return False
    try:
        import shiboken6
        from PySide6.QtGui import QRegion

        qwindow = widget.windowHandle()
        if qwindow is None:
            return False  # not shown yet — no platform window to blur
        if corner_radius > 0:
            region = _rounded_region(widget, corner_radius)
        else:
            region = QRegion()  # empty == KWindowSystem blurs whole window
        win_ptr = shiboken6.getCppPointer(qwindow)[0]
        reg_ptr = shiboken6.getCppPointer(region)[0]
        fn(ctypes.c_void_p(win_ptr), bool(enabled), ctypes.c_void_p(reg_ptr))
        return True
    except Exception:
        return False
