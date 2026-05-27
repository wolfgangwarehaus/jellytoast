# EQ + DSP chain — design research

> **📍 Status — 2026-05-27:** Shipped + corrected. The 10-band graphic
> EQ + master pre-amp landed 2026-05-19. The v1 ship cascaded mpv-native
> ``equalizer`` biquads as a workaround for an ``anequalizer`` syntax
> bug we'd misdiagnosed as an mpv limitation. **EQ T1 landed 2026-05-27**
> — `modules/eq_presets.py` now emits a single ``anequalizer`` filter
> with concrete per-channel indices (the originally-planned design). See
> `docs/research/eq_dsp_v2.md` §2 for the wart investigation and the
> remaining T2-T4 roadmap; this doc is kept for the original rationale.

Status: research / pre-build. Target slot: post-Phase-5 (offline UI), before download-UX merge.

## 1. Goal & non-goals

**Goal.** Ship a 10-band graphic equalizer with presets and live-apply — meets the Supersonic / Strawberry / Tauon bar with room to overtake on UX polish. Off by default; explicit "bit-perfect is gone when EQ is on" disclosure.

**v1.** 10-band graphic EQ (ISO octave centres) at ±12 dB; 8 built-in presets (Flat, Rock, Pop, Classical, Jazz, Vocal, Bass Boost, Treble Boost); user-saved presets; master pre-amp; on/off toggle that takes effect mid-track without rebuffering; lives in Playback settings, no dedicated window.

**Non-goals.** Parametric EQ (Symfonium owns that), AutoEQ headphone profiles (defer), spectrum analyser (separate epic), per-track EQ (global only), other DSP filters (note the hooks, ship later), cast-side EQ (impossible — §8).

## 2. mpv DSP primer

mpv exposes ffmpeg `libavfilter` filters via the `lavfi` wrapper. The legacy `af=equalizer=...` is **deprecated**; ffmpeg recommends `anequalizer` or `firequalizer`. python-mpv (already a dep, see `modules/player_backend.py`) drives this via the `af` property or the `af-add` / `af-remove` change-list commands.

| Filter | Type | CPU | Notes |
| --- | --- | --- | --- |
| `equalizer` | IIR biquad per band | tiny | deprecated, avoid |
| `anequalizer` | High-order parametric multiband | small | Butterworth / Chebyshev; one filter, all bands |
| `firequalizer` | FIR, FFT-based | moderate | linear phase, audible buffer flush on mutate |

**Pick `anequalizer`.** Maintained, one filter instance for all bands (clean mutation), realtime for 24/192. `firequalizer` is the future "audiophile mode" toggle, not v1.

### Syntax we'll generate

`anequalizer` takes a pipe-separated band list; each band is `c<ch> f=<Hz> w=<Hz> g=<dB> t=<type>`. `c-1` addresses all channels. Example, +3 dB at 1 kHz, 1-octave Butterworth:

```
lavfi=[anequalizer=c-1 f=1000 w=1000 g=3 t=0]
```

We assemble all 10 bands into one filter, prefix a `volume=<dB>` filter for pre-amp, and assign the chain to `self._mpv["af"]`. mpv re-plugs the filter graph without rebuffering — Strawberry's GStreamer `issue #144` (EQ didn't apply until next track) is the failure mode to avoid; mpv's `af` mutation is the one that works.

### Other filters we'll keep on the shelf, not expose in v1

- `volume=<dB>` — already used for pre-amp.
- `loudnorm` (EBU R128) — squashes musical dynamics, future "loudness equalization" toggle.
- `dynaudnorm` — section-wise AGC, ~4x faster than loudnorm but smears dynamics. Not for music.
- `bass` / `treble` — redundant once we have a real EQ.
- `crystalizer` — gimmick, skip.

## 3. Architecture

### Chain position

Current pipeline inside mpv: decoder → ReplayGain → output. EQ goes between RG and output:

```
decode → replaygain → [pre-amp + anequalizer] → resample → ao
```

Order matters: RG normalises perceived loudness first, then the user's pre-amp gives headroom for EQ boosts, then bands shape the already-normalised signal.

### Signal flow

- `PlayerBus.eq_changed = Signal()` — fires when bands, pre-amp, or enabled flag changes. No payload; listeners re-read `settings.eq_*`. Same pattern as `theme_changed` / `replaygain_changed`.
- `PlayerBackend._connect_bus()` adds `self.bus.eq_changed.connect(self._apply_eq_chain)`.
- `_apply_eq_chain()` rebuilds the full filter string from settings and assigns `self._mpv["af"] = chain_str`. One write, no diffing.

### Throttling

A slider drag at 60 Hz × 10 bands is 600 `af` writes/sec. Coalesce with a 30 ms settle timer — last tick wins, one rebuild. The empty `eq_changed` payload exists for this collapse.

### Glitch behaviour

`af` mutation with `anequalizer` doesn't stop playback. There may be a one-block silence (~5 ms at 48 kHz) on chain rebuild — inaudible during drag, fine for preset switches.

## 4. UI surface

### Placement

Settings → Playback, new "Equalizer" section after "Normalization". Tauon and Supersonic both bury it in a dialog; nobody EQs more than once per listening session. Optionally add a small "EQ on" badge to the streaming-info row above the play button (only when that readout is already enabled) so the user doesn't forget.

### Controls

Horizontal layout, vertical sliders (the visual that says "equalizer" to every user since Winamp):

```
[Pre]  [31]  [62]  [125]  [250]  [500]  [1k]  [2k]  [4k]  [8k]  [16k]
 ▣     ▣    ▣     ▣      ▣      ▣      ▣     ▣     ▣     ▣     ▣
```

- Each slider -12 dB ↔ +12 dB, snap-to-zero at midpoint, double-click → 0 dB. Pre-amp same range.
- Band-centre label (Hz / kHz) under each slider; live dB readout above the thumb on drag.
- **Preset** combo + `Save…` / `Delete…` buttons at the top. Picking a preset sets all 11 sliders; dragging any slider switches the combo to `Custom`.
- **Enabled** checkbox top-left. When off, sliders dim but stay legible so the user can preview a curve.
- Caption under the enable toggle: "EQ on = audio is no longer bit-perfect." Same style as the existing gapless caption.

No curve/graph in v1 — that's a `QPainter` overlay we can add later.

### Anti-patterns

- Strawberry #144 (EQ doesn't apply mid-track) — solved by mpv `af` mutation.
- Supersonic shipped 15-band EQ in v0.4.0 but didn't add preset management until v0.21.0 — ship save/load on day one.

## 5. Settings

New `QSettings` keys, following the `playback/*` pattern in `modules/settings.py`:

| Key | Type | Default | Notes |
| --- | --- | --- | --- |
| `playback/eq_enabled` | bool | `False` | Off by default. Bit-perfect users keep their pipe clean. |
| `playback/eq_preamp` | float | `0.0` | dB, range -12 to +12. |
| `playback/eq_bands` | str (JSON list of 10 floats) | `"[0,0,0,0,0,0,0,0,0,0]"` | dB per band, ISO order 31→16k. Stored as JSON string for the same reason `favorite_cast_devices` is. |
| `playback/eq_preset` | str | `"Flat"` | Last-selected preset name. `"Custom"` if the user has dragged a slider. |
| `playback/eq_user_presets` | str (JSON object) | `"{}"` | `{name: {preamp: float, bands: [10 floats]}}`. |

Properties on `Settings` follow the existing `replaygain` / `gapless` shape. Setters emit `PlayerBus.eq_changed` only from the *user-facing* mutation points (slider release, preset pick, toggle), not from the QSettings setter — same rule as `theme_changed` (signal-connects-in-init memory entry).

## 6. Multi-platform notes

- **Linux (primary).** mpv + PipeWire/PulseAudio. Realtime through 24/192. No special handling.
- **Windows.** mpv with WASAPI; chain runs in mpv before the sink. The native WASAPI APO path would need a COM `IAudioProcessingObject` — out of scope, mpv keeps parity.
- **macOS (eventual).** mpv on macOS uses the same chain. Apple's `AUNBandEQ` is the native equivalent; we don't need it — parity wins, and the user memory says don't write Apple code we can't test.
- **iOS (long-term).** mpv isn't the right engine on iOS — `AVAudioEngine` + `AVAudioUnitEQ` is. The parametric-Butterworth model maps 1:1 to `anequalizer`; persisted settings (bands + presets, all floats in JSON) port directly.

## 7. Provider abstraction

EQ is a playback-pipeline concern, not a backend concern. Jellyfin and Subsonic ship bytes; mpv processes them; the EQ lives inside mpv. No changes to `MusicProvider`, no per-provider override.

## 8. Edge cases

- **Bit-perfect.** EQ on = filter graph = no longer bit-identical. Disclose in the caption. Off-by-default is part of the contract.
- **Hi-res (24/96, 24/192).** `anequalizer` works in float; no truncation, no resample, well under one core.
- **DSD.** mpv decodes DSD to PCM for any filter. With EQ on, PCM conversion happens regardless. DoP-direct users lose direct-DSD when EQ is on. Doc caveat, not a UI surface.
- **Cast routing.** Cast devices decode the stream themselves; our EQ is downstream of the handoff and **does not** apply when casting. Grey the EQ section out (or show an "applies to local playback only" caption) on `PlayerBus.cast_started` / `cast_stopped`.
- **Gapless.** mpv's gapless prefetches into a second decoder; `af` is global, both decoders share it.
- **ReplayGain interaction.** Chain order `replaygain → preamp → anequalizer → out` is the working composition; no fight.
- **Pre-amp clipping.** Boosting bands without pulling the pre-amp clips on hot masters. Default presets ship with a small negative pre-amp where any band is positive. Make pre-amp visible — Audacious hides it, users complain.

## 9. Effort & sequencing

**Estimate: M.** Settings + JSON ~1 h, bus signal + `_apply_eq_chain` + throttle ~2 h, UI section in `_build_playback` (sliders, preset combo, save/delete) ~4 h, preset table ~1 h, cast-greying + bit-perfect caption ~1 h, manual-test pass ~2 h. ~One work-day.

**Blockers.** None hard. python-mpv `af` assignment is stable; `anequalizer` has shipped in ffmpeg since 3.2 so the CachyOS / Flatpak runtime / Windows mpv builds all have it.

**Slot in `docs/TODO.md`.** Lands cleanly after Phase-5 offline UI and before download-UX merges — pure playback-pipeline polish, doesn't touch queue, provider, or offline subsystems.

### Recommended built-in presets (Audacious-derived, ISO 10-band 31/62/125/250/500/1k/2k/4k/8k/16k)

| Preset | Preamp | 31 | 62 | 125 | 250 | 500 | 1k | 2k | 4k | 8k | 16k |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Flat | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| Rock | 0 | +5 | +3 | -3 | -5 | -2 | +3 | +6 | +7 | +7 | +7 |
| Pop | 0 | -1 | +3 | +5 | +5 | +3 | -1 | -2 | -2 | -1 | -1 |
| Classical | -1 | 0 | 0 | 0 | 0 | 0 | 0 | -5 | -5 | -5 | -6 |
| Jazz | 0 | +4 | +3 | +1 | +2 | -2 | -2 | 0 | +1 | +3 | +4 |
| Vocal | -2 | -3 | -3 | -2 | +1 | +4 | +4 | +3 | +2 | 0 | -2 |
| Bass Boost | -3 | +7 | +6 | +5 | +3 | +1 | 0 | 0 | 0 | 0 | 0 |
| Treble Boost | -3 | 0 | 0 | 0 | 0 | 0 | +1 | +3 | +5 | +6 | +7 |

Numbers rounded from Audacious `eq.preset` and adjusted so positive-band presets carry a negative pre-amp to forestall clipping. Treat as v1 defaults; revisit after community feedback.

## 10. Sources

- mpv audio-filter manual — https://github.com/mpv-player/mpv/blob/master/DOCS/man/af.rst
- mpv issue: legacy `equalizer` deprecated, use `anequalizer` / `firequalizer` — https://github.com/mpv-player/mpv/issues/4455
- mpv issue: `firequalizer` for mpv — https://github.com/mpv-player/mpv/issues/6239
- mpv visual EQ script (band layout reference) — https://gist.github.com/avih/41acff712abd32e1f436235388c8b523
- ffmpeg filter docs (anequalizer, firequalizer, equalizer, loudnorm, dynaudnorm) — https://ffmpeg.org/ffmpeg-filters.html
- python-mpv bindings — https://github.com/jaseg/python-mpv
- Supersonic changelog (15-band → 10-band + AutoEQ + presets in v0.21.0; ffmpeg filter-string fix in v0.20.1) — https://github.com/dweymouth/supersonic/blob/main/CHANGELOG.md
- Strawberry issue #144, EQ doesn't apply mid-track (GStreamer) — https://github.com/strawberrymusicplayer/strawberry/issues/144
- Strawberry issue #126, EQ does nothing for some users — https://github.com/jonaski/strawberry/issues/126
- Audacious preset values (10-band reference) — https://gist.github.com/kra3/9781800
- Audacious EQ source — https://github.com/audacious-media-player/audacious/blob/master/src/libaudcore/equalizer-preset.cc
- AVAudioUnitEQ (iOS native EQ reference) — https://developer.apple.com/documentation/avfaudio/avaudiouniteq
- Easy Effects equalizer plugin docs (PipeWire-side reference for what a system-wide EQ looks like) — https://wwmm.github.io/easyeffects/plugins/equalizer.html
