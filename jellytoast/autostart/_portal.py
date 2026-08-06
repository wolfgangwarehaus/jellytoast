"""XDG Background portal — the sandboxed route to "launch at login".

``org.freedesktop.portal.Background.RequestBackground`` with
``autostart: true`` asks the desktop to start us on login. The portal writes
the host-side autostart entry itself, so it needs **no** filesystem
permission — which is the point: Flathub's linter hard-rejects
``--filesystem=~/.config/autostart:create``
(``finish-args-autostart-filesystem-access``), so the .desktop route in
``_linux.py`` is a no-op in a Flathub build. The portal is the sanctioned
replacement; ``_linux.py`` tries it first and keeps the .desktop writer as
the fallback for X11-only / no-portal sessions.

D-Bus is driven with **jeepney** (already a declared Linux dependency), the
same idiom ``jellytoast/color_picker.py`` uses for
``Screenshot.PickColor``: send the method call, then wait for the
``Response`` signal that lands on the returned Request object path.

The portal may put the request in front of the user ("Allow jellytoast to
run in the background?"), and it can be **denied** — a denial is reported
back honestly rather than papered over with a .desktop file, so the
settings checkbox reflects reality.

There is no portal API to read the current autostart state back (and inside
a sandbox we can't see the host's autostart dir either), so a grant is
recorded in a marker file under the app config dir; ``is_enabled()`` reads
that when the portal path was the one used.
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

log = logging.getLogger(__name__)

_PORTAL_SERVICE = "org.freedesktop.portal.Desktop"
_PORTAL_PATH = "/org/freedesktop/portal/desktop"
_BACKGROUND_IFACE = "org.freedesktop.portal.Background"
_REQUEST_IFACE = "org.freedesktop.portal.Request"

# Bounded wait for the Response signal. The portal answers immediately when
# it auto-approves and within a few seconds when it prompts; the cap only
# exists so a portal that never answers can't wedge the caller forever
# (enable()/disable() are synchronous — they're called straight from the
# settings checkbox handler).
_REQUEST_TIMEOUT_S = 20

_CONFIG_DIR = (
    Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "jellytoast"
)
# Existence == the Background portal granted us autostart. Removed on
# disable / denial.
_STATE_FILE = _CONFIG_DIR / "autostart-portal"


def is_available() -> bool:
    """True when the Background portal can plausibly be driven: jeepney is
    importable and either we're inside a flatpak (a portal is always there)
    or the portal owns its name on the session bus."""
    if not _jeepney_available():
        return False
    if os.environ.get("FLATPAK_ID"):
        return True
    return _portal_on_bus()


def request_autostart(want: bool) -> bool | None:
    """Ask the portal to turn login-launch on (``want=True``) or off.

    Returns True when the portal confirmed the requested state, False when
    it denied/cancelled, and None when the portal couldn't be reached at all
    (the caller should then fall back to the .desktop file). Never raises.
    """
    try:
        return _request_background(want)
    except Exception:
        log.warning("autostart: Background portal request failed", exc_info=True)
        return None


def autostart_granted() -> bool:
    """True iff a previous portal request was granted (marker present)."""
    try:
        return _STATE_FILE.exists()
    except Exception:
        return False


def mark_granted() -> None:
    """Record that the portal granted autostart. Best effort — a failure
    here only costs us the checkbox state on the next launch."""
    try:
        _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _STATE_FILE.write_text("granted\n")
    except Exception:
        log.debug("autostart: could not write portal state marker", exc_info=True)


def clear_granted() -> None:
    """Forget any recorded portal grant."""
    try:
        _STATE_FILE.unlink()
    except FileNotFoundError:
        pass
    except Exception:
        log.debug("autostart: could not clear portal state marker", exc_info=True)


def _jeepney_available() -> bool:
    try:
        import jeepney  # noqa: F401
        import jeepney.io.blocking  # noqa: F401

        return True
    except Exception:
        return False


def _portal_on_bus() -> bool:
    """NameHasOwner(org.freedesktop.portal.Desktop) — cheap presence probe."""
    try:
        from jeepney.bus_messages import message_bus
        from jeepney.io.blocking import open_dbus_connection

        conn = open_dbus_connection(bus="SESSION")
        try:
            reply = conn.send_and_get_reply(message_bus.NameHasOwner(_PORTAL_SERVICE))
            return bool(reply.body[0])
        finally:
            conn.close()
    except Exception:
        log.debug("autostart: no Background portal on the session bus", exc_info=True)
        return False


def _request_background(want: bool) -> bool | None:
    """Blocking RequestBackground round-trip. See request_autostart()."""
    from jeepney import DBusAddress, MatchRule, new_method_call
    from jeepney.bus_messages import message_bus
    from jeepney.io.blocking import open_dbus_connection

    conn = open_dbus_connection(bus="SESSION")
    try:
        background = DBusAddress(
            _PORTAL_PATH, bus_name=_PORTAL_SERVICE, interface=_BACKGROUND_IFACE
        )
        options = {
            "reason": ("s", _reason_text()),
            "autostart": ("b", bool(want)),
            "commandline": ("as", _commandline()),
            # We are not a D-Bus-activatable service; the portal must write a
            # plain Exec= entry.
            "dbus-activatable": ("b", False),
        }
        reply = conn.send_and_get_reply(
            new_method_call(background, "RequestBackground", "sa{sv}", ("", options))
        )
        request_path = reply.body[0]
        log.info(
            "autostart: RequestBackground(autostart=%s) dispatched, request=%s",
            want,
            request_path,
        )
        rule = MatchRule(
            type="signal",
            interface=_REQUEST_IFACE,
            member="Response",
            path=request_path,
        )
        conn.send_and_get_reply(message_bus.AddMatch(rule))
        with conn.filter(rule) as queue:
            msg = conn.recv_until_filtered(queue, timeout=_REQUEST_TIMEOUT_S)
        code, results = msg.body
        log.info("autostart: Background portal Response code=%s", code)
        if code != 0:  # 0 = success; 1 = cancelled; 2 = other
            return False
        return _autostart_granted(results, want)
    finally:
        conn.close()


def _variant_value(v):
    """jeepney hands back a{sv} entries as ``(signature, value)``; tolerate a
    bare value too."""
    if isinstance(v, tuple) and len(v) == 2 and isinstance(v[0], str):
        return v[1]
    return v


def _autostart_granted(results, want: bool) -> bool:
    """Did the portal actually give us the state we asked for? A success code
    with ``autostart: false`` after we asked for true is a DENIAL."""
    got = results.get("autostart") if hasattr(results, "get") else None
    if got is None:
        # Success code but no echo of the field — trust the code.
        return True
    return bool(_variant_value(got)) is bool(want)


def _reason_text() -> str:
    """Localized one-liner the portal shows the user. Qt is always present in
    the app; fall back to English if the translation machinery isn't up
    (headless imports, early startup)."""
    text = "Start jellytoast when you log in"
    try:
        from PySide6.QtCore import QCoreApplication

        return QCoreApplication.translate("Autostart", text)
    except Exception:
        return text


def _commandline() -> list[str]:
    """argv the portal bakes into the autostart entry. Inside a flatpak this
    is resolved against the sandbox (the portal wraps it in ``flatpak run``
    itself), so the bare entry-point name is what we want."""
    if os.environ.get("FLATPAK_ID"):
        return ["jellytoast"]
    try:
        import shutil

        exe = shutil.which("jellytoast")
    except Exception:
        exe = None
    if exe:
        return [exe]
    return [sys.executable or "python3", "-m", "jellytoast"]
