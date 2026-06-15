"""Linux notifications backend. Shells out to `notify-send` (part of
libnotify), which talks to the freedesktop `org.freedesktop.Notifications`
D-Bus service exposed by every mainstream notification daemon (KDE
Plasma, GNOME Shell, dunst, mako, swaync, xfce4-notifyd).

Why notify-send over a direct dbus-next call: notifications are
fire-and-forget UI events. Spinning up an asyncio MessageBus per toast
is heavy, and dbus-next mid-call failures on headless systems are
fiddlier to error-suppress than a subprocess that returns a non-zero
exit code. notify-send is the canonical CLI for this and is present on
any desktop install that ships libnotify.

is_supported() returns False if notify-send isn't on PATH — the package
falls back to silent no-op behavior in that case.
"""

from __future__ import annotations

import shutil
import subprocess

_NOTIFY_SEND_TIMEOUT = 3


def _notify_send_bin() -> str | None:
    return shutil.which("notify-send")


def is_supported() -> bool:
    return _notify_send_bin() is not None


def notify(
    title: str,
    body: str = "",
    icon: str | None = None,
    app_name: str = "jellytoast",
    tag: str | None = None,
) -> None:
    bin_ = _notify_send_bin()
    if not bin_:
        return

    cmd = [bin_, "--app-name", app_name]
    if icon:
        cmd.extend(["--icon", icon])
    if tag:
        # The "synchronous" hint makes mainstream daemons (KDE, GNOME,
        # dunst) replace a prior notification with the same tag in place
        # rather than stacking — exactly the now-playing stream's need.
        cmd.extend(["--hint", f"string:x-canonical-private-synchronous:{tag}"])
    cmd.append(title)
    if body:
        cmd.append(body)

    try:
        subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            timeout=_NOTIFY_SEND_TIMEOUT,
        )
    except Exception:
        pass
