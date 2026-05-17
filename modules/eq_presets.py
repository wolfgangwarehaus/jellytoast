"""
Equalizer presets + mpv ``anequalizer`` filter-string formatter.

Pure-Python module — no Qt, no mpv, no settings. Imported by
``modules.player_backend`` (which writes the filter string to mpv)
and by the future EQ settings page (which reads ``PRESETS`` to
populate the preset combo).

Design notes:

* Ten-band ISO octave centres — same layout Audacious, foobar2000,
  and the Apple AVAudioUnitEQ default ship with. The band order is
  documented in ``BAND_FREQUENCIES`` so callers don't have to guess
  which slot in a ``bands`` list maps to which frequency.
* Preset gains in dB, clamped to the ±12 dB range the research doc
  specifies. Numbers derived from the Audacious preset table with
  the rounding the doc recommends.
* The mpv filter is ``anequalizer`` (parametric multiband, IIR).
  See ``docs/research/eq_dsp.md`` §2 for why we picked it over the
  deprecated ``equalizer`` and the heavier ``firequalizer``.

The filter-string builder here intentionally omits the master
pre-amp — that's a separate ``volume=<dB>`` filter the backend
prepends. Keeping the two layers independent means the pre-amp
slider can move without rebuilding the EQ string.
"""

from __future__ import annotations

# ISO octave band centres in Hz. Listed in the order the gains in
# every ``bands`` list are interpreted. Public so the future UI can
# label its slider column without duplicating the literal.
BAND_FREQUENCIES: tuple[int, ...] = (
    31,
    62,
    125,
    250,
    500,
    1000,
    2000,
    4000,
    8000,
    16000,
)

# Per-band quality factor (bandwidth in Hz for anequalizer's
# ``w=`` parameter). One octave wide for every band — Butterworth
# response, the standard graphic-EQ shape. Width equals the centre
# frequency to a first approximation; ffmpeg's anequalizer takes
# width in Hz, not Q, so the simple "w = f" rule yields a clean
# one-octave skirt. Stored alongside the band centres so the
# formatter doesn't have to recompute it.
_BAND_WIDTHS: tuple[int, ...] = BAND_FREQUENCIES

# Default preset name. ``Flat`` zeroes every band — equivalent to
# the filter being bypassed, kept around so the preset combo always
# has a valid selection.
DEFAULT_PRESET: str = "Flat"

# Built-in preset gains in dB, ordered to match ``BAND_FREQUENCIES``.
# Values derived from ``docs/research/eq_dsp.md`` §9; the master
# pre-amp documented there is intentionally not stored here — this
# scaffold ships bands only, and the pre-amp lands with the UI.
PRESETS: dict[str, list[float]] = {
    "Flat": [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    "Rock": [5, 3, -3, -5, -2, 3, 6, 7, 7, 7],
    "Pop": [-1, 3, 5, 5, 3, -1, -2, -2, -1, -1],
    "Jazz": [4, 3, 1, 2, -2, -2, 0, 1, 3, 4],
    "Classical": [0, 0, 0, 0, 0, 0, -5, -5, -5, -6],
    "Electronic": [5, 4, 1, 0, -2, 2, 1, 1, 4, 5],
    "Vocal": [-3, -3, -2, 1, 4, 4, 3, 2, 0, -2],
    "Bass Boost": [7, 6, 5, 3, 1, 0, 0, 0, 0, 0],
}

BAND_COUNT: int = len(BAND_FREQUENCIES)
GAIN_LIMIT_DB: float = 12.0


def _clamp_gain(g: float) -> float:
    """Saturate any inbound band gain to the documented ±12 dB
    envelope. Anything outside is almost certainly a user-config
    bug; mpv would happily accept ±60 dB and shred speakers."""
    try:
        x = float(g)
    except (TypeError, ValueError):
        return 0.0
    if x > GAIN_LIMIT_DB:
        return GAIN_LIMIT_DB
    if x < -GAIN_LIMIT_DB:
        return -GAIN_LIMIT_DB
    return x


def format_eq_filter_string(bands: list[float]) -> str:
    """Build the mpv ``af``-value substring for a 10-band graphic EQ
    as a chain of mpv-native ``equalizer`` biquad filters.

    Output shape — one filter per band, comma-separated, one octave
    wide (width_type=o, w=1) which is the standard graphic-EQ shape:

        equalizer=f=31:width_type=o:w=1:g=0,equalizer=f=62:width_type=o:w=1:g=0,...

    Why not ``anequalizer``: the research doc originally specified
    ``anequalizer`` (parametric multiband, libavfilter-only) wrapped
    in ``lavfi=[...]``. In practice that path silently no-ops on
    mpv 0.x — the filter is created but doesn't filter audio (pre-amp
    via ``volume=`` works, the bands don't). The deprecated-but-working
    ``equalizer`` is in mpv's native filter list and definitely
    applies. Trading the deprecation warning for actually-functional
    EQ is the right v1 tradeoff; if mpv ever drops ``equalizer`` we
    revisit the ``anequalizer`` path with whatever escaping it then
    requires.

    Returns the bare filter spec (no leading ``af=``) so the caller
    can chain it with other filters (``volume=...``) using ``,``.

    ``ValueError`` is raised for any list length that isn't exactly
    ``BAND_COUNT`` — feeding mpv a half-built filter would either
    error the whole filter graph or silently leave bands at zero,
    and a hard fail upstream is easier to diagnose than either.

    An all-zeros input still returns a fully-formed filter string;
    callers that want a true "bypass" should disable the filter
    rather than relying on this returning an empty string. The
    bypass call is cheaper in mpv than evaluating ten 0-dB biquads
    but the difference is negligible — keeping the output shape
    consistent matters more for the calling convention.
    """
    if len(bands) != BAND_COUNT:
        raise ValueError(f"expected {BAND_COUNT} bands, got {len(bands)}")
    parts = []
    for freq, gain in zip(BAND_FREQUENCIES, bands):
        g = _clamp_gain(gain)
        # Drop the decimal when the value is a clean integer — keeps
        # the filter string short and matches mpv's manual examples.
        if float(g).is_integer():
            g_str = str(int(g))
        else:
            g_str = f"{g:g}"
        # width_type=o + w=0.7 = 0.7-octave-wide bell. One-octave
        # (w=1) bands overlap heavily with their neighbours and the
        # cumulative gain of stacked biquads sounds muddy / smeared.
        # 0.7 octave is the standard graphic-EQ compromise — narrow
        # enough that adjacent bands don't fight each other but wide
        # enough to feel like a graphic EQ, not a parametric.
        parts.append(f"equalizer=f={freq}:width_type=o:w=0.7:g={g_str}")
    return ",".join(parts)


# Back-compat alias. Older imports still resolve to the new function
# so the rename is a single-commit change without breaking external
# call sites (there are none in-tree, but the name is referenced in
# memory entries and docs). Safe to delete once those have caught up.
format_anequalizer_string = format_eq_filter_string


def get_preset(name: str) -> list[float]:
    """Return a fresh copy of a built-in preset's band list. Unknown
    names fall back to ``Flat`` rather than raising, so a settings
    string that's drifted from the shipped preset names degrades to
    bypass instead of crashing the audio chain."""
    bands = PRESETS.get(name)
    if bands is None:
        bands = PRESETS[DEFAULT_PRESET]
    return list(bands)
