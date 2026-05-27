# EQ + DSP — v2 research (the anequalizer wart, Symfonium target, tiered upgrade)

> **Status — 2026-05-27:** Research only. v1 of the EQ shipped 2026-05-19
> (see `eq_dsp.md`, `modules/eq_presets.py`). This doc revisits the
> `anequalizer` fallback ("the wart"), benchmarks Symfonium concretely,
> and lays out a tiered path from v1 → audiophile-tier.

## 1. The wart, re-stated

`modules/eq_presets.py` ships a chain of mpv's **deprecated** `equalizer`
biquads (one per band) because the originally-planned `anequalizer`
"silently no-opped on mpv 0.x" when wrapped in `lavfi=[anequalizer=c-1 ...]`.
The deprecation warning is tolerated; the bands actually attenuate audio.

Costs of the fallback: deprecation warning per chain rebuild, more phase
smear from 10 cascaded biquads vs one multiband filter, no upgrade path
to per-band Q/freq movability (every centre is fixed).

## 2. Wart investigation — does `anequalizer` actually work?

**Yes. The filter works fine in current ffmpeg/mpv. The v1 attempt used
invalid channel syntax (`c-1`); `anequalizer` silently discards bands
whose `chn` doesn't match a real channel index.**

### 2.1 `c-1` is the smoking gun

`af_anequalizer.c` parses `chn` as a concrete channel index (0, 1, 2…).
The doc note "if input doesn't have that channel, the entry is ignored"
[3] is literal: `c-1` matches nothing, every band is dropped, the filter
graph stays plumbed with zero active bands. That matches the observed
symptom exactly — filter creates, no audio change.

There is no all-channels sentinel. The FFmpeg test fixture [4] uses `c0`:
```
anequalizer='c0 f=200 w=200 g=-80 t=1'
```
A working stereo multi-band config from the FreeBSD MPD forum [5] enumerates
the cross-product (one band per channel, per centre):
```
anequalizer=c0 f=80 w=25 g=-2.6 t=0|c0 f=105 w=105 g=5.5 t=0|...
            c1 f=80 w=25 g=-2.6 t=0|c1 f=105 w=105 g=5.5 t=0|...
```

### 2.2 Documentation has historically been wrong

The FFmpeg-user thread [6] caught the official docs labeling the type
parameter `f` (instead of `t`) and the frequency `cf` in prose vs `f`
in the example. Paul B Mahol (filter author) confirmed: "the example
is correct and works. Documentation above was wrong." Anyone who read
the prose and typed `cf=…` got a parse error; the v1 design doc may
have inherited either confusion.

### 2.3 lavfi escaping

mpv's `--af` value isn't pure libavfilter syntax [2]. Two rules:
- `lavfi=[…]` wrapping forces the libavfilter route. Required for names
  that collide with mpv builtins (e.g. `equalizer`); **not required** for
  `anequalizer` (no collision).
- Spaces inside a band spec (`c0 f=200 w=200 g=3 t=0`) are the band's
  internal separator, not graph escapes — they work as-is.

So `self._mpv["af"] = "anequalizer=c0 f=1000 w=1000 g=3 t=0|c1 f=1000 w=1000 g=3 t=0"`
should just work from python-mpv.

### 2.4 Verdict

Not an mpv bug, not a regression — a syntax bug. Fix: query mpv's
channel-count property at chain-build time, emit one band-entry per
(channel × frequency). ~10 lines.

## 3. Symfonium target — what we're benchmarking against

Symfonium rebuilt its EQ in v13.7.0 (early 2026 [7,8,9,10]).

| Feature | Symfonium PEQ | Symfonium GEQ | Symfonium AutoEQ |
| --- | --- | --- | --- |
| Mode | Parametric (IIR biquads) | Graphic | Convolution-equiv |
| Bands default / max | **10 / 64 (expert)** | 5, 10, 15, or 31 | **127** (per profile) |
| Per-band freq / Q | Yes (movable) | Fixed ISO, 1-oct | Per profile |
| Min frequency | **5 Hz** | ISO | n/a |
| Channel routing | All / L / R | All | All |
| Profiles | Multiple, switchable | Multiple | Embedded DB (4,200+ headphones) |
| Import | AutoEQ + APO PEQ | AutoEQ `GraphicEQ.txt` | Built-in |
| Compressor / limiter / virtualizer | Expert mode | — | — |
| Bass / volume boost | Yes (effects) | — | — |
| Cast | Local-only | Local-only | Local-only |
| Backend | AudioTrack + ExoPlayer | same | same |

Notes:
- "256 bands" in marketing = the **127-band AutoEQ graphic** plus
  imports, not a 256-band PEQ.
- Filter type is undocumented but almost certainly **IIR biquads**
  (AAudio output, mobile latency expectations). AutoEQ profiles consumed
  as biquad coefficients, not convolution IRs.
- Cast pipeline short-circuits DSP — same constraint we already document.

## 4. The broader audiophile bar

| App | Bands | Type | Convolution | Notes |
| --- | --- | --- | --- | --- |
| Roon | 5–10 per stage, chainable | IIR + FIR | Yes (per-rate WAV IR) | AutoEQ-friendly |
| Audirvana | 10-band PEQ + curve editor | IIR | Limited | Desktop |
| foobar2000 `foo_dsp_xgeq` | **31** | IIR, minimum-phase | No (separate `foo_convolver`) | 1/3-oct reference |
| foobar2000 SuperEQ (builtin) | 18 | FFT | No | LGPL ref impl |
| Equalizer APO (Windows) | Unlimited PEQ + GraphicEQ + Convolution | IIR + FIR + IR | **First-class** | System-wide gold standard |
| HQPlayer | Filter-bank model | FIR convolution | Yes | High-end ceiling |
| CamillaDSP | Unlimited stages | IIR biquads + FIR + mixers | **Yes** | Linux Rust DSP, PipeWire-native [14] |
| Easy Effects | LSP 8/16/32-band PEQ | IIR + FIR | Yes (LSP convolver) | PipeWire-side system EQ |

Read top-down: below Audirvana, everything lives at the system/pipeline
level. Roon + Symfonium are the realistic peer set for jellytoast.

## 5. mpv-compatible options on Linux

Filters we can plumb via `self._mpv["af"]`:

| Filter | Type | Latency | CPU | Quality | Status |
| --- | --- | --- | --- | --- | --- |
| `equalizer` (current) | IIR biquad × N | ~0 | tiny | Cascaded phase smear | Deprecated [1] |
| `anequalizer` | High-order IIR multiband | ~0 | small | Single graph, cleaner phase | **Works with correct syntax** |
| `firequalizer` | FFT-based FIR | 10 ms default | moderate | Linear phase via `zero_phase=on` [11,16] | Stable |
| `superequalizer` | 18-band FFT | small | low | Fixed 65 Hz–16.7 kHz centres [12] | Limited control |
| `afir` | Direct convolution from WAV IR | depends on tap count | depends | AutoEQ-WAV path | Stable, ≥ ffmpeg 3.4 |
| `lavfi=ladspa=…` | Any LADSPA plugin | depends | depends | LSP plugins [13] | Heavy dep |

Key numbers for the tier plan:
- `firequalizer` default `delay=0.01` (10 ms), `accuracy=5` Hz. Higher
  delay = sharper low-end control. Music absorbs 20-50 ms latency fine.
- `zero_phase=on` removes the perceived latency via PTS adjust — no
  pre-ring, no audible echo.
- `afir` with 4096-tap FIR + 48k content + 48k IR ≈ 2 ms convolution.

### Deliberately not in scope

PipeWire-side filters / Easy Effects (changes user's system pipeline,
not our job); CamillaDSP (same; worth a docs note: "if you run CamillaDSP
system-wide, disable jellytoast's EQ to avoid stacking"); VST hosting
(Linux VST story is mediocre, Symfonium doesn't either).

## 6. Tiered upgrade path

### Tier 0 — current (shipped 2026-05-19)

10-band biquad graphic EQ, fixed ISO centres, ±12 dB, 8 built-in
presets, user presets, master pre-amp, live-apply, bit-perfect
disclosure, cast-greying.

vs Symfonium GEQ: 10-band parity. Loses on band-count choice (5/15/31)
and L/R routing.

### Tier 1 — fix the wart (`anequalizer`)

**Scope.** Drop the chain of biquads. Query mpv's
`audio-params/channel-count` at apply time, emit one
`c<n> f=… w=… g=… t=0` per (channel × band). Settings schema unchanged.

**Cost.** S (~half a day). Swap the inner loop in `format_eq_filter_string`.

**UI.** Zero changes.

**Gains.** Removes deprecation warning. Single filter = cleaner phase
composite than 10 cascaded biquads. Unblocks Tier 3 (per-band Q/freq).
L/R routing becomes a one-line UI knob.

**Deps.** ffmpeg ≥ 3.0. Every distro + Flatpak runtime has it.

**Recommendation: ship next.** Pure-win refactor.

### Tier 2 — linear-phase FIR mode toggle (`firequalizer`)

**Scope.** New setting `playback/eq_linear_phase` (bool). When on,
replace `anequalizer` with
`firequalizer=gain_entry='entry(31,g1);entry(62,g2);…':zero_phase=on:delay=0.02`.

**Cost.** M (~1 day). New formatter, one checkbox + caption ("Higher
CPU; eliminates phase distortion. ~20 ms internal latency").

**Gains.** Linear phase — transient response preserved through EQ.
Audible on drums, plucked strings, percussive material. Matches Roon /
LSP plugins / Equalizer APO's linear-phase mode.

**Costs.** ~3× IIR CPU (still well under one core for 48k stereo).
20 ms latency. Filter-graph rebuild ≈ one-buffer silence per drag
tick (gated by existing 30 ms settle timer).

**Deps.** ffmpeg ≥ 3.0, mpv ≥ 0.27.

**Recommendation: ship after Tier 1 as opt-in.** Off by default —
bit-perfect-by-default stance argues against enabling DSP users didn't
request. The toggle is the differentiator.

### Tier 3 — full parametric EQ (movable centre + per-band Q)

**Scope.** Promote graphic → parametric:
- Per-band freq + Q sliders (or width-in-octaves).
- Band count selectable (5 / 10 / 15 / 31, matching Symfonium GEQ).
- Add/remove bands up to 16 soft cap (AutoEQ-format PEQ profiles are
  typically 10; 16 has headroom).
- AutoEQ `ParametricEQ.txt` import (text → bands).

**Cost.** L (3-5 days).
- Settings migration: `eq_bands` becomes `[{f, w, g, t}, …]` instead
  of 10 floats. Migrate on first load, keep old key as backup.
- UI: the chunky bit. A curve editor (QPainter overlay, log-frequency
  x-axis, draggable freq+Q+gain nodes). Two presentations:
  - **Simple**: keep the slider strip, label freq under thumb in real
    time, lock freq movement unless user enters "advanced".
  - **Advanced**: full curve editor. Tab between them.
- AutoEQ parser: ~50 lines of regex on
  `Filter N: ON PK Fc 105 Hz Gain 5.5 dB Q 1.41`.

**Gains.** True Symfonium PEQ parity at the default 10-band mode.
AutoEQ headphone correction via PEQ profiles (the most-shared format
on the web).

**Deps.** Tier 1 prerequisite — chained biquads can't move centres
without rebuilding the whole filter string per drag.

**Recommendation: where we earn "match Symfonium".** Worth doing,
after Tier 1 lands and Tier 2's linear-phase toggle is in (Tier 3 + 2
stacks — parametric + linear-phase is what Roon ships).

### Tier 4 — convolution-based AutoEQ headphone profiles

**Scope.** New "Headphone correction" section:
- Headphone picker (autocomplete over AutoEQ DB) **or** "Import IR…".
- Under the hood: `afir=dry=10:wet=10:length=1:gtype=peak` over WAV.
- Separate signal-flow stage *after* user EQ:
  `replaygain → preamp → user_eq → headphone_correction → out`.

**Cost.** L+ (5-10 days), mostly DB plumbing.
- Vendoring AutoEQ DB ≈ 30 MB binary hit (just WAV IRs).
- Or: "Import IR…" only, link out to AutoEQ — lower-friction, lower-cost.
- mpv `afir` plumbing is tested-good.

**Gains.** Symfonium parity on AutoEQ embedded DB (if vendored), or
near-parity with import-only.

**Costs.** 30 MB binary if vendored. AutoEQ DB updates monthly →
fetch-from-GitHub pattern, bundled snapshot for offline.

**Deps.** Tier 1 prerequisite. `afir` is in ffmpeg ≥ 3.4.

**Recommendation: ship "Import IR…" minimal variant first**, vendored
DB only after the cast/Phase-4 work is clear and the Flathub
distribution shape is settled.

## 7. Reframing the TODO carve-out

`docs/TODO.md` lines 268-269 currently lists "Heavy audiophile DSP" as
deliberately out of scope, citing Symfonium uncatchability. This research
argues for **softening, not deleting**:

- Tier 1 is a bug fix, not "heavy DSP". In scope.
- Tier 2 is opt-in linear-phase, matches Roon's audio-quality switch,
  no UX surface area. In scope.
- Tier 3 is "heavy" but tractable; the gap to Symfonium is **the UI**,
  not the audio engine. Catchable in PySide6.
- Tier 4 is the one place Symfonium genuinely leads on integration
  (embedded DB). Keep the carve-out unless we decide the binary-size
  hit is worth it.

Suggested TODO copy:
> **Audiophile DSP** — graphic EQ shipped; parametric mode + linear-phase
> FIR + AutoEQ PEQ import is the path. Convolution-based headphone DB
> (vendored) remains deferred past Flathub launch.

## 8. Concrete recommendation

The user said "match Symfonium". Reading that as **PEQ at default band
count + AutoEQ PEQ import**, the path is:

1. **Now**: Fix the wart (Tier 1, half-day). Pure win, no UX change.
2. **Next**: Linear-phase toggle (Tier 2, one day). Opt-in audiophile
   mode, differentiator.
3. **Then**: Parametric EQ + curve editor + AutoEQ import (Tier 3,
   3-5 days). Symfonium parity earned here.
4. **Defer**: Convolution AutoEQ (Tier 4) until post-Flathub.

Total Tier 1 + 2 + 3 ≈ one week of focused work to land at genuine
Symfonium parity. Tier 4 is a separate project that adds audiophile
credibility but doesn't change the parity answer.

Bit-perfect-by-default contract holds throughout — every tier stays
opt-in. The chain `replaygain → preamp → EQ → headphone_correction → out`
is the working composition; each tier slots in without re-architecting.

## 9. Out-of-scope reminders

Cast-side EQ impossible (downstream of stream handoff). DSD passthrough
gone whenever EQ on (already disclosed). ReplayGain stays before EQ.
Global EQ only — no per-track.

## 10. Sources

1. mpv #4455 — legacy `equalizer` deprecated — https://github.com/mpv-player/mpv/issues/4455
2. mpv audio-filter manual — https://github.com/mpv-player/mpv/blob/master/DOCS/man/af.rst
3. FFmpeg `anequalizer` filter docs — https://ffmpeg.org/ffmpeg-filters.html#anequalizer
4. FFmpeg `anequalizer` test fixture — https://github.com/FFmpeg/FFmpeg/blob/master/tests/filtergraphs/anequalizer
5. FreeBSD forum — MPD + anequalizer working stereo config — https://forums.freebsd.org/threads/mpd-server-ncmpcpp-player-apply-ffmpegs-anequalizer-filter.96888/
6. FFmpeg-user — anequalizer doc/example mismatch (Paul B Mahol) — https://ffmpeg-user.ffmpeg.narkive.com/2lDJmOzm/anequalizer-example-error-on-doc-site-and-manpage
7. Symfonium — PEQ/GEQ/AutoEQ spec page — https://symfonium.app/android-music-player-peq-autoeq/
8. Symfonium support wiki — Advanced Equalizer/AutoEQ — https://support.symfonium.app/t/advanced-equalizer-autoeq/677
9. Symfonium v13.7.0 release notes — https://symfonium.app/news/version-1370/
10. Symfonium — custom AutoEQ profile import — https://support.symfonium.app/t/use-a-custom-autoeq-profile/2344
11. FFmpeg `firequalizer` docs — https://ayosec.github.io/ffmpeg-filters-docs/3.1/Filters/Audio/firequalizer.html
12. FFmpeg `superequalizer` docs — https://ayosec.github.io/ffmpeg-filters-docs/8.0/Filters/Audio/superequalizer.html
13. LSP Parametric Equalizer — https://lsp-plug.in/?page=manuals&section=para_equalizer_x16_stereo
14. CamillaDSP — https://github.com/HEnquist/camilladsp
15. AutoEQ (4,200+ headphone profiles, GraphicEQ + ParametricEQ + WAV IR) — https://github.com/jaakkopasanen/AutoEq
16. mpv #6239 — using `firequalizer` in mpv — https://github.com/mpv-player/mpv/issues/6239
17. mpv #6210 — AUDIO EQUALIZER request — https://github.com/mpv-player/mpv/issues/6210
18. Roon — parametric + convolution headphone EQ thread — https://community.roonlabs.com/t/headphone-settings-for-parametric-and-convolution-equalizer/63730
19. Equalizer APO config reference — https://sourceforge.net/p/equalizerapo/wiki/Configuration%20reference/
20. foobar2000 `foo_dsp_xgeq` — 31-band 1/3-octave reference — https://www.foobar2000.org/components/view/foo_dsp_xgeq
21. PipeWire parametric-equalizer module — https://docs.pipewire.org/page_module_parametric_equalizer.html
