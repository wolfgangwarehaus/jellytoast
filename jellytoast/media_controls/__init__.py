"""Cross-platform OS-level media-control integration. Surfaces play/pause
state, metadata, and Next/Prev/Stop/Seek commands to the host OS so
hardware media keys, lock-screen widgets, and bluetooth headset controls
all work.

Public API:
    MediaControlsService  — Qt-side controller; .start() / .stop().

Linux backend (`_mpris`): registers org.mpris.MediaPlayer2.jellytoast
on the session bus. Picked up by KDE Plasma media widget, GNOME Shell,
playerctl, waybar, etc.

Windows backend (`_windows`): SMTC (System Media Transport Controls) via
the PyWinRT ``get_for_window`` interop — surfaces in the volume flyout
and on the lock screen and picks up keyboard / bluetooth media keys.

macOS backend (`_macos`): NowPlaying via pyobjc (MPNowPlayingInfoCenter +
MPRemoteCommandCenter) — surfaces in Control Center's Now Playing module
and on the lock screen, and routes the hardware media keys / headset
buttons back to the app.

Off Linux/Windows/macOS (or if the backend can't initialise): the
unsupported backend is a no-op MediaControlsService that satisfies the same
shape so callers don't need platform branches.
"""

from __future__ import annotations

from jellytoast.platform_compat import IS_LINUX, IS_MACOS, IS_WINDOWS

if IS_LINUX:
    try:
        from jellytoast.media_controls._mpris import MprisService as _Backend
    except Exception:
        from jellytoast.media_controls._unsupported import (
            UnsupportedMediaControlsService as _Backend,
        )
elif IS_WINDOWS:
    try:
        from jellytoast.media_controls._windows import (
            WindowsMediaControlsService as _Backend,
        )
    except Exception:
        from jellytoast.media_controls._unsupported import (
            UnsupportedMediaControlsService as _Backend,
        )
elif IS_MACOS:
    try:
        from jellytoast.media_controls._macos import (
            MacMediaControlsService as _Backend,
        )
    except Exception:
        from jellytoast.media_controls._unsupported import (
            UnsupportedMediaControlsService as _Backend,
        )
else:
    from jellytoast.media_controls._unsupported import (
        UnsupportedMediaControlsService as _Backend,
    )


# Public name. Backend swap is transparent to call sites.
MediaControlsService = _Backend
