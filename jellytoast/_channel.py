"""Build-time install-channel stamp.

``CHANNEL`` records HOW this copy of jellytoast was packaged, so the in-app
update check (``jellytoast/updates.py``) only nags channels the user updates BY
HAND (.dmg / .deb / AppImage / installer / portable / source) and stays silent
on the auto-updating ones (Store / Mac App Store / AUR), which would otherwise
be steered toward a conflicting manual installer.

The default is ``"source"`` — a source checkout / pip install / anything we
haven't stamped. That deliberately ERRS TOWARD checking (a harmless extra nag)
rather than toward silently suppressing on an unrecognised build (the wrong
direction).

IMPORTANT — what this does and does NOT gate:
- The load-bearing "never nag a self-updating build" guarantee does NOT depend
  on this constant. The Store (``msix``) and Mac App Store (``mas``) channels
  are detected at RUNTIME (``platform_compat.is_msix_packaged`` /
  ``is_macos_sandboxed``) and suppressed regardless of the stamp.
- This constant only refines the DOWNLOAD DEEP-LINK on manual channels: a known
  value (``dmg`` ``deb`` ``appimage`` ``inno`` ``portable``) lets the update
  chip point at the exact asset for the user's package; ``"source"`` falls back
  to the Releases page (still correct, just less precise).

Per-artifact stamping is a planned enhancement, NOT yet wired: PyInstaller
freezes this module into a PYZ archive, so a post-build ``sed`` can't reach it —
it needs a marker file beside the frozen bundle (or a per-launcher
``JT_CHANNEL`` env var), verified on each real package. Until then every build
reports ``"source"`` (manual installs get a notice + the Releases page; the
runtime-probed Store/MAS builds stay silent). See ``updates.get_channel()``.
"""

from __future__ import annotations

CHANNEL = "source"
