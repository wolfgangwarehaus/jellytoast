"""
Equalizer presets + mpv ``anequalizer`` filter-string formatter.

Pure-Python module — no Qt, no mpv, no settings. Imported by
``modules.player_backend`` (which writes the filter string to mpv)
and the EQ settings page (which reads ``PRESETS`` to populate the
preset combo).

Design notes:

* Ten-band ISO octave centres — same layout Audacious, foobar2000,
  and the Apple AVAudioUnitEQ default ship with. The band order is
  documented in ``BAND_FREQUENCIES`` so callers don't have to guess
  which slot in a ``bands`` list maps to which frequency.
* Preset gains in dB, clamped to the ±12 dB range the research doc
  specifies. Numbers derived from the Audacious preset table.
* The mpv filter is ``anequalizer`` — single parametric multiband
  IIR (Butterworth, t=0). One filter instance for all bands, all
  channels; cleaner composite phase than 10 cascaded biquads. See
  ``docs/research/eq_dsp_v2.md`` §2 for the syntax / "the wart" fix
  (the v1 implementation cascaded mpv-native ``equalizer`` biquads
  because the originally-planned ``anequalizer`` used a non-existent
  ``c-1`` all-channels sentinel and silently dropped every band).

The filter-string builder intentionally omits the master pre-amp
— that's a separate ``volume=<dB>`` filter the backend prepends.
Keeping the two layers independent lets the pre-amp slider move
without rebuilding the EQ string.
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


def format_eq_filter_string(bands: list[float], channel_count: int = 2) -> str:
    """Build the mpv ``af``-value substring for a 10-band graphic EQ
    as a single ``anequalizer`` filter, with one band entry per
    (channel × frequency).

    Output shape — one filter, pipe-separated band entries:

        anequalizer=c0 f=31 w=31 g=0 t=0|c0 f=62 w=62 g=0 t=0|…|
                    c1 f=31 w=31 g=0 t=0|c1 f=62 w=62 g=0 t=0|…

    Each entry is ``c<n> f=<Hz> w=<Hz> g=<dB> t=0`` — Butterworth
    type, one-octave wide (``w = f`` is the standard one-octave
    bandwidth in the parametric-Butterworth sense; ``anequalizer``
    internally composes the bands without the cumulative-biquad
    smear the legacy ``equalizer`` filter had at the same width).

    **The channel cross-product matters.** ``anequalizer`` requires
    concrete channel indices (``c0``, ``c1``, …); there is no
    all-channels sentinel. The v1 implementation used ``c-1`` and
    every band was silently discarded — see
    ``docs/research/eq_dsp_v2.md`` §2 ("the wart"). The caller is
    responsible for querying mpv's ``audio-params/channel-count``
    and passing it here; default ``channel_count=2`` (stereo) covers
    the vast majority of music sources.

    ``channel_count`` is clamped to ``[1, 8]`` defensively — a bad
    value from a misbehaving handle property won't produce a
    syntactically broken or unbounded filter.

    Returns the bare filter spec (no leading ``af=``) so the caller
    can chain it with other filters (``volume=...``) using ``,``.

    ``ValueError`` is raised for any band-list length that isn't
    exactly ``BAND_COUNT``; a half-built filter would either error
    the whole graph or silently leave bands at zero, and a hard
    fail upstream is easier to diagnose than either.

    An all-zeros input still returns a fully-formed filter string;
    callers that want a true bypass should disable the filter via
    ``apply_eq(enabled=False, …)`` rather than relying on this
    returning an empty string.
    """
    if len(bands) != BAND_COUNT:
        raise ValueError(f"expected {BAND_COUNT} bands, got {len(bands)}")
    try:
        channels = int(channel_count)
    except (TypeError, ValueError):
        channels = 2
    channels = max(1, min(8, channels))
    entries = []
    for ch in range(channels):
        for freq, gain in zip(BAND_FREQUENCIES, bands):
            g = _clamp_gain(gain)
            # Drop the decimal when the value is a clean integer —
            # keeps the filter string short and matches the mpv /
            # ffmpeg manual examples.
            if float(g).is_integer():
                g_str = str(int(g))
            else:
                g_str = f"{g:g}"
            # ``w = f`` is the parametric-Butterworth one-octave
            # bandwidth in Hz (anequalizer's ``w=`` takes Hz, not
            # octaves). One filter instance composes all bands so
            # the cumulative-smear problem of cascaded biquads
            # doesn't apply — ``w=f`` is the canonical choice.
            entries.append(f"c{ch} f={freq} w={freq} g={g_str} t=0")
    # ffmpeg 8 / mpv 0.41 dropped the positional shorthand
    # (``anequalizer=c0 f=…|c1 …``) — only the named AVOption form
    # ``anequalizer=params="…"`` parses. mpv unquotes the value and
    # passes the inner ``|``-list through to anequalizer untouched.
    return 'anequalizer=params="' + "|".join(entries) + '"'


# Back-compat alias. Older imports still resolve to the new function
# so the rename is a single-commit change without breaking external
# call sites (there are none in-tree, but the name is referenced in
# memory entries and docs). Safe to delete once those have caught up.
format_anequalizer_string = format_eq_filter_string


# ── EQ T2 — linear-phase FIR ─────────────────────────────────────────
# FIR delay tuned to the research doc's value (§6 T2). 20 ms is the
# audiophile-tier balance: long enough that the FIR taps cover the
# lowest band's period (31 Hz → ~32 ms period; the filter still
# responds well at 20 ms tap window because the window function tapers
# energy off the tail), short enough that the latency doesn't feel
# like the player is "behind" the seek bar. Exposed here so the test
# suite can assert against it without re-deriving the value.
FIREQUALIZER_DELAY_S: float = 0.02


def format_firequalizer_string(bands: list[float]) -> str:
    """Build the mpv ``af``-value substring for a linear-phase FIR
    equalizer (EQ T2 — see ``docs/research/eq_dsp_v2.md`` §6).

    Output shape — a single ``firequalizer=`` filter, with the band
    centres + gains carried in ``gain_entry`` and ``zero_phase=on``
    enabling the FIR's linear-phase mode:

        firequalizer=gain_entry='entry(31,g1);entry(62,g2);…':\
                     zero_phase=on:delay=0.02

    Linear phase preserves transient response through the EQ —
    audible on drums, plucked strings, percussive material. Costs
    ~3× IIR CPU (still under one core for 48 k stereo) and ~20 ms
    of internal pre-roll latency.

    Unlike ``format_eq_filter_string``, ``firequalizer`` is global
    across channels — no channel-cross-product needed. ffmpeg handles
    the per-channel routing internally.

    ``ValueError`` is raised for any band-list length that isn't
    exactly ``BAND_COUNT`` — same hard-fail contract as the IIR
    formatter so a half-built filter never reaches mpv.

    Returns the bare filter spec (no leading ``af=``) so the caller
    can chain it with other filters (``volume=...``) using ``,``.
    """
    if len(bands) != BAND_COUNT:
        raise ValueError(f"expected {BAND_COUNT} bands, got {len(bands)}")
    entries = []
    for freq, gain in zip(BAND_FREQUENCIES, bands):
        g = _clamp_gain(gain)
        # Same int / float emission rule as the IIR path so integer
        # gains stay short and the test suite can assert on either
        # form.
        if float(g).is_integer():
            g_str = str(int(g))
        else:
            g_str = f"{g:g}"
        entries.append(f"entry({freq},{g_str})")
    # ``firequalizer``'s gain_entry option takes a semicolon-joined
    # list of ``entry(freq, gain_dB)`` pairs. Single-quoting protects
    # the inner semicolons from mpv's filter-string parser, which
    # otherwise uses semicolons to separate parallel filter chains.
    gain_entry = ";".join(entries)
    return (
        f"firequalizer=gain_entry='{gain_entry}'"
        f":zero_phase=on:delay={FIREQUALIZER_DELAY_S}"
    )


def get_preset(name: str) -> list[float]:
    """Return a fresh copy of a built-in preset's band list. Unknown
    names fall back to ``Flat`` rather than raising, so a settings
    string that's drifted from the shipped preset names degrades to
    bypass instead of crashing the audio chain."""
    bands = PRESETS.get(name)
    if bands is None:
        bands = PRESETS[DEFAULT_PRESET]
    return list(bands)


# ── EQ T3a — parametric bands + AutoEQ ParametricEQ.txt import ──────
#
# Parametric bands generalise the 10-band graphic EQ: each band carries
# its own centre frequency, bandwidth, gain, and filter type. The
# graphic-mode formatters (above) construct fixed-frequency parametric
# bands internally; the AutoEQ import path supplies them directly from
# the user's headphone-correction profile.
#
# Band representation: a plain dict with these keys (TypedDict-shaped,
# but kept as untyped dict for trivial JSON serialisation):
#   f: int    — centre frequency in Hz
#   w: float  — bandwidth in Hz (anequalizer's ``w=`` parameter)
#   g: float  — gain in dB (clamped to ±12)
#   t: int    — filter type (0 = Butterworth peaking; the only one
#              ffmpeg's ``anequalizer`` natively peaks with)

import re

# AutoEQ filter-type tags we recognise.
AUTOEQ_TYPE_PEAKING = "PK"
# Low-shelf / high-shelf — present in some AutoEQ profiles but
# ``anequalizer`` doesn't natively support shelf filters. Recorded
# in the parser's ``skipped`` list so the import dialog can show
# the user what wasn't applied. Most headphone-correction PEQ
# profiles are predominantly PK anyway.
AUTOEQ_TYPE_LOW_SHELF = "LSC"
AUTOEQ_TYPE_HIGH_SHELF = "HSC"


def q_to_width_hz(freq_hz: float, q: float) -> float:
    """Convert a parametric-EQ Q factor to ``anequalizer``'s width-in-Hz
    representation. Standard definition: ``Q = f_center / bandwidth``,
    so ``bandwidth = f / Q``. Q ≤ 0 is meaningless (would imply infinite
    bandwidth); clamp to a minimum of 0.1 to avoid division explosions
    from a malformed AutoEQ profile."""
    q_safe = max(0.1, float(q))
    return float(freq_hz) / q_safe


# AutoEQ ParametricEQ.txt lines look like:
#   Preamp: -6.6 dB
#   Filter 1: ON PK Fc 105 Hz Gain 5.5 dB Q 1.41
#   Filter 2: ON LSC Fc 105 Hz Gain -2 dB Q 0.71
# The parser is lenient — extra whitespace, missing trailing units,
# lowercase variants all parse cleanly. Profiles in the wild are
# generated by autoeq.app and follow this exact shape, but it doesn't
# cost anything to be forgiving.
_AUTOEQ_PREAMP_RE = re.compile(
    r"^\s*preamp\s*:\s*([-+]?\d+(?:\.\d+)?)\s*db", re.IGNORECASE
)
_AUTOEQ_FILTER_RE = re.compile(
    r"^\s*filter\s+\d+\s*:\s*"
    r"(on|off)\s+"
    r"([A-Za-z]+)\s+"
    r"fc\s+([-+]?\d+(?:\.\d+)?)\s*hz\s+"
    r"gain\s+([-+]?\d+(?:\.\d+)?)\s*db\s+"
    r"q\s+([-+]?\d+(?:\.\d+)?)",
    re.IGNORECASE,
)


def parse_autoeq_profile(text: str) -> dict:
    """Parse an AutoEQ ``ParametricEQ.txt``-format string into a profile.

    Returns a dict with three keys:

    * ``preamp_db`` — float, the master pre-amp the profile specifies
      (0.0 if absent).
    * ``bands`` — list of BandSpec dicts (the supported PK filters,
      clamped to ±12 dB, with ``w`` derived from Q).
    * ``skipped`` — list of ``{"type": ..., "reason": ..., "freq": ...}``
      for filters that weren't representable as Butterworth peaking
      bands. Shelf filters (LSC / HSC) and OFF filters land here; the
      import dialog can surface this to the user.

    The parser tolerates blank lines, comments, and unrecognised
    properties. Unparseable lines are silently skipped (the AutoEQ
    format isn't a strict standard — some generators include extra
    metadata at the top of the file).
    """
    preamp_db = 0.0
    bands: list[dict] = []
    skipped: list[dict] = []
    for raw_line in (text or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = _AUTOEQ_PREAMP_RE.match(line)
        if m:
            try:
                preamp_db = float(m.group(1))
            except ValueError:
                pass
            continue
        m = _AUTOEQ_FILTER_RE.match(line)
        if m is None:
            # Unrecognised line — silently skip. AutoEQ headers /
            # comments / metadata land here.
            continue
        on_off, ftype, fc, gain, q = m.groups()
        ftype_u = ftype.upper()
        if on_off.lower() != "on":
            skipped.append({
                "type": ftype_u,
                "reason": "filter is OFF in profile",
                "freq": _coerce_float(fc),
            })
            continue
        if ftype_u != AUTOEQ_TYPE_PEAKING:
            skipped.append({
                "type": ftype_u,
                "reason": (
                    f"{ftype_u} is a shelf filter; not representable as "
                    "Butterworth peaking"
                ),
                "freq": _coerce_float(fc),
            })
            continue
        try:
            f = int(round(float(fc)))
            g = _clamp_gain(float(gain))
            w = q_to_width_hz(f, float(q))
        except ValueError:
            continue
        bands.append({"f": f, "w": w, "g": g, "t": 0})
    return {"preamp_db": preamp_db, "bands": bands, "skipped": skipped}


def _coerce_float(s: str) -> float:
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0


def build_default_parametric_bands(gains: list[float]) -> list[dict]:
    """Construct the 10 ISO-octave parametric bands the graphic-EQ
    surface drives. One band per ``BAND_FREQUENCIES`` entry, ``w = f``
    (one-octave Butterworth), gains taken from the input list."""
    if len(gains) != BAND_COUNT:
        raise ValueError(f"expected {BAND_COUNT} gains, got {len(gains)}")
    return [
        {"f": int(f), "w": float(f), "g": _clamp_gain(g), "t": 0}
        for f, g in zip(BAND_FREQUENCIES, gains)
    ]


def format_anequalizer_parametric(
    bands: list[dict], channel_count: int = 2
) -> str:
    """Build the ``anequalizer`` filter string from a list of parametric
    bands. Same channel-cross-product rule as
    ``format_eq_filter_string`` — see ``docs/research/eq_dsp_v2.md`` §2
    for the wart-fix history.

    Empty band list returns the empty string so the caller can decide
    to skip the filter entirely (an empty ``anequalizer=`` is a syntax
    error in ffmpeg)."""
    if not bands:
        return ""
    try:
        channels = int(channel_count)
    except (TypeError, ValueError):
        channels = 2
    channels = max(1, min(8, channels))
    entries = []
    for ch in range(channels):
        for band in bands:
            f = int(band.get("f", 1000))
            w = float(band.get("w", float(f)))
            g = _clamp_gain(band.get("g", 0.0))
            t = int(band.get("t", 0))
            if float(g).is_integer():
                g_str = str(int(g))
            else:
                g_str = f"{g:g}"
            if float(w).is_integer():
                w_str = str(int(w))
            else:
                w_str = f"{w:g}"
            entries.append(f"c{ch} f={f} w={w_str} g={g_str} t={t}")
    # See ``format_eq_filter_string`` — ffmpeg 8 / mpv 0.41 require
    # the named ``params="…"`` form.
    return 'anequalizer=params="' + "|".join(entries) + '"'


def format_firequalizer_parametric(bands: list[dict]) -> str:
    """Build the ``firequalizer`` filter string from parametric bands.

    ``firequalizer`` interpolates between ``gain_entry`` points, so
    Q doesn't factor in — only (freq, gain) pairs. Bands at duplicate
    centre frequencies are kept (firequalizer sums them); the caller
    is responsible for de-duplicating if that's not desired.

    Empty band list returns the empty string (same rationale as
    ``format_anequalizer_parametric``)."""
    if not bands:
        return ""
    entries = []
    for band in bands:
        f = int(band.get("f", 1000))
        g = _clamp_gain(band.get("g", 0.0))
        if float(g).is_integer():
            g_str = str(int(g))
        else:
            g_str = f"{g:g}"
        entries.append(f"entry({f},{g_str})")
    gain_entry = ";".join(entries)
    return (
        f"firequalizer=gain_entry='{gain_entry}'"
        f":zero_phase=on:delay={FIREQUALIZER_DELAY_S}"
    )
