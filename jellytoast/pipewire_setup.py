"""T4 audiophile PipeWire conf installer.

Drops a single conf snippet into the user's
``~/.config/pipewire/pipewire.conf.d/`` so PipeWire follows the source
sample rate instead of resampling 44.1 kHz CD-quality material to its
default 48 kHz session rate. See ``docs/bit_perfect.md`` for the user
guide and ``docs/research/bit_perfect_playback.md`` §4.2 for the
recipe + why each property is set the way it is.

Linux-only. The Settings UI hides the button on other platforms; the
``is_supported()`` check below is the canonical gate.

Design notes:
- The file is ID-stamped with a comment header so ``uninstall()``
  refuses to touch a file the user wrote by hand at the same path.
- ``install()`` is idempotent — overwriting the file with the same
  content is a no-op from PipeWire's perspective (no rescan needed
  until the user logs out / restarts pipewire).
- All paths are taken through ``Path.home()`` rather than hard-coded
  so the test suite can monkey-patch the home dir without touching
  real config.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Stable filename — the leading `10-` puts us early in PipeWire's
# numerical conf-merge order without colliding with system defaults
# (which typically live at `99-`). `jellytoast-bitperfect` makes the
# origin and purpose self-documenting if the user goes spelunking.
CONF_FILENAME = "10-jellytoast-bitperfect.conf"

# Relative to ``Path.home()``. PipeWire reads conf.d/* alongside the
# main pipewire.conf, so user-level overrides drop here.
CONF_DIR_RELATIVE = ".config/pipewire/pipewire.conf.d"

# ID-stamp. Present in every file we write; absent from any other.
# ``uninstall()`` verifies before unlinking so a user who hand-edited
# the same path doesn't lose their work to the Settings button.
CONF_HEADER = (
    "# Installed by jellytoast — Settings → Playback → Bit-perfect.\n"
    "# Safe to edit or remove by hand; jellytoast's uninstall path\n"
    "# only touches files containing this exact header.\n"
)

# Recipe per `docs/research/bit_perfect_playback.md` §4.2.
#  - ``default.clock.allowed-rates`` lets PipeWire switch its session
#    rate to match the playing stream (44.1 kHz CD-quality files stop
#    resampling to 48 kHz). The list is the canonical "PCM rates a DAC
#    might support" set; PipeWire picks whichever the active stream
#    requests.
#  - ``resample.quality = 14`` bumps the resampler to near-sinc for the
#    cases where resampling is still forced (e.g. a 96 kHz file on a
#    48 kHz-max DAC). Default is 4 (linear-ish). Costs a few % of one
#    CPU core during playback; the audiophile community consensus
#    setting on ArchWiki + HeadFi.
CONF_BODY = """context.properties = {
    default.clock.allowed-rates = [ 44100 48000 88200 96000 176400 192000 ]
}

stream.properties = {
    resample.quality = 14
}
"""

# Full file contents (header + body). One concatenation point so the
# matching uninstall + the install never drift.
CONF_FILE_CONTENTS = CONF_HEADER + "\n" + CONF_BODY


def is_supported() -> bool:
    """T4 only ships on Linux — PipeWire isn't a thing on the other
    target platforms. Hidden entirely (not just disabled) elsewhere."""
    return sys.platform.startswith("linux")


def conf_path(home: Path | None = None) -> Path:
    """Resolved path to the conf file. ``home`` override exists for
    tests; production code calls without an argument."""
    base = home if home is not None else Path.home()
    return base / CONF_DIR_RELATIVE / CONF_FILENAME


def is_installed(home: Path | None = None) -> bool:
    """True iff the file at ``conf_path()`` exists AND carries our
    ID-stamp header. A user-authored file at the same path reads as
    "not installed" so we don't claim credit for someone else's work."""
    p = conf_path(home)
    if not p.exists():
        return False
    try:
        return CONF_HEADER in p.read_text(encoding="utf-8")
    except OSError:
        return False


def install(home: Path | None = None) -> None:
    """Write (or overwrite) our conf file. Idempotent — writing twice
    with the same contents is a no-op as far as PipeWire is concerned;
    the user still has to restart pipewire / log out for the new
    properties to be picked up."""
    p = conf_path(home)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(CONF_FILE_CONTENTS, encoding="utf-8")


def uninstall(home: Path | None = None) -> bool:
    """Remove our conf file. Returns True if a file was removed, False
    if there was nothing to remove or the file at the path lacks our
    ID-stamp (the user wrote it themselves — leave it alone)."""
    p = conf_path(home)
    if not p.exists():
        return False
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return False
    if CONF_HEADER not in text:
        return False
    try:
        p.unlink()
    except OSError:
        return False
    return True
