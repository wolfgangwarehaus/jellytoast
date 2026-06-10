"""Cross-platform OS-level media-control integration. Surfaces play/pause
state, metadata, and Next/Prev/Stop/Seek commands to the host OS so
hardware media keys, lock-screen widgets, and bluetooth headset controls
all work.

Public API:
    MediaControlsService  — Qt-side controller; .start() / .stop().

Linux backend (`_mpris`): registers org.mpris.MediaPlayer2.jellytoast
on the session bus. Picked up by KDE Plasma media widget, GNOME Shell,
playerctl, waybar, etc.

Off-Linux: the unsupported backend is a no-op MediaControlsService that
satisfies the same shape so callers don't need platform branches.

Future backends:
- Windows: SMTC (System Media Transport Controls) via
  windows_toasts / winrt.windows.media — surfaces in the volume flyout
  and on the lock screen, picks up keyboard / bluetooth media keys.
- macOS: NowPlaying via pyobjc (MPNowPlayingInfoCenter +
  MPRemoteCommandCenter) — surfaces in Control Center and on the
  Touch Bar / Watch.
"""

from __future__ import annotations

from jellytoast.platform_compat import IS_LINUX

if IS_LINUX:
    try:
        from jellytoast.media_controls._mpris import MprisService as _Backend
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
