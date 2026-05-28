# Crossfade — research & design

> **📍 Status — shipped end-to-end (updated 2026-05-28 audit):** The
> crossfade engine landed 2026-05-18; the Settings-screen controls
> (enable checkbox + smart-album-continuity toggle + duration slider
> in Settings → Playback) shipped 2026-05-20; the equal-power easing
> curve (`_equal_power_gains`, replacing the linear placeholder)
> shipped 2026-05-25. Gated on the `crossfade_enabled` setting (the
> old `JT_CROSSFADE=1` env gate is gone). Perceived-loudness flatness
> across cross-album fades is the one open hand-check
> (`manual_test_plan` §10). Original design doc, kept for rationale.

Status: shipped. Owner: august.
Last updated: 2026-05-20.

## 1. Goal & non-goals

**Goal.** Overlap the tail of track N with the head of track N+1 by a user-configurable duration (1–10 s, default 4 s), with the polish bar set by Tauon and Symfonium. Local-playback only; must coexist with the existing gapless prefetch path without regression.

**Non-goals.**
- Loudness-aware "Sweet Fade" / MixRamp-style overlap-point detection — requires per-track analysis, future v2 ([Music Assistant 2.7](https://github.com/orgs/music-assistant/discussions/3929)).
- Crossfade on cast targets. Chromecast / AirPlay play the server stream directly; the device controls the mixer. Setting dims while a cast device is active.
- Skip/prev/queue-jump aren't true overlaps — they use the existing fade-out + load-next pattern (Symfonium calls it "transition fade").
- iOS — AVAudioEngine has native crossfade primitives but the iOS port is a future code path.

Recommendation below is **Option A — two alternating mpv instances**, ping-pong style.

## 2. mpv approach analysis

### Option A — two alternating mpv instances (recommended)

Carry two `mpv.MPV(...)` in `MpvController` (`_mpv_a` / `_mpv_b`) plus an `_active` pointer. Inactive instance is dormant (`idle=yes`, no loaded file). When the active instance's `time-pos` crosses `duration - crossfade_duration_ms`, the inactive instance `loadfile`s the next track at `volume=0`; both run for the fade window with linear ramps in opposite directions; at the end the now-silent prior instance `stop`s, roles swap, queue advances. Ping-pong A→B→A→B.

Pros: pure mpv on both sides (decoder quality, per-instance ReplayGain unchanged); two instances in one process is supported ([python-mpv #126](https://github.com/jaseg/python-mpv/issues/126), libmpv APIs are thread-safe); precedent — Strawberry, Sonixd, Tauon all converged on a dual-pipeline pattern (Strawberry runs two GStreamer pipelines, [`src/core/player.cpp`](https://github.com/strawberrymusicplayer/strawberry/blob/master/src/core/player.cpp)).

Cons / hard parts:
- **MPRIS bus ownership.** Today one mpv owns the bus. Keep MPRIS rooted in `MpvController` and have it read from the *outgoing* instance during a fade; flip at fade-out completion. One bus, one identity.
- **Audio device contention.** Linux PipeWire/PulseAudio mixes two clients on the same sink. Windows **WASAPI exclusive mode** locks the second instance out; ALSA-only without `dmix` does the same. Force `audio-exclusive=no` on both; document that audiophiles wanting bit-exact output disable crossfade.
- **Memory.** Two demuxers, two decoders, two 100 MiB buffers → ~2× streaming RAM during the fade. Dormant instance can `stop` to release its demux buffer. Worst-case under 250 MB for two FLAC streams.
- **Prefetch refactor.** Today the active mpv prefetches via `loadfile … append` + `gapless_audio=weak` + `prefetch_playlist=yes` (`modules/player_backend.py:192-194`, `:797-866`). With two instances, prefetch moves out of mpv into our scheduler — the inactive instance *is* the prefetch. Meaningful refactor of `_on_prefetch_request` / `_clear_prefetch`.
- **Cast handoff.** Cast start must stop both; cast end restores to the "A" slot by convention. Existing `_on_cast_started` / `_on_cast_stopped` become instance-aware.

### Option B — single mpv with `af=acrossfade` / lavfi acrossfade

mpv ships the `acrossfade` ffmpeg filter but it operates **once between two inputs** — see [mpv issue #4512](https://github.com/mpv-player/mpv/issues/4512). It can't continuously crossfade across an N-entry playlist; the filter consumes both inputs and terminates. Rebuilding the filter graph at every boundary tears down audio output and reintroduces exactly the gap the gapless code path was written to avoid. Rejected.

### Option C — pre-mix offline via ffmpeg

`ffmpeg -filter_complex acrossfade=d=4` bakes a pair into a temp file. Works on any backend including cast, but every transition needs a fresh prerender (download N+1 → encode pair → re-stream): seconds of latency, double bandwidth, disk thrash, and it breaks the streaming model. Cast was already ruled out. Rejected.

### Option D — drop mpv, mix in-process with PyAudio / libsoundio + numpy

Maximum control, maximum maintenance burden. Loses mpv's resampler, ReplayGain, codec breadth, HW decode. Last resort if Option A turns out untenable. Rejected for v1.

**Recommendation: Option A** — two alternating mpv instances, ping-pong scheduler, MPRIS rooted in `MpvController`, `audio-exclusive=no`.

## 3. Gapless interop

Crossfade and gapless are mutually exclusive *per track pair*, not globally. Model:

- `playback/gapless` stays as today (default on, drives `gapless_audio=weak` + `prefetch_playlist=yes` on the active instance).
- Add `playback/crossfade_enabled` (default off, opt-in).
- Add `playback/crossfade_smart_album_continuity` (default **on**) — skip the fade and run a pure gapless transition when outgoing/incoming share `AlbumId` AND incoming `IndexNumber == outgoing.IndexNumber + 1`. Tauon's behavior; the difference between a player that respects albums and one that hashes "Money" into "Us and Them" on Dark Side of the Moon.
- Gapless-skip case: prefetch path is unchanged — append to the active instance's playlist, libmpv handles it, inactive instance stays dormant.
- Crossfade case: active instance does NOT prefetch into its playlist; scheduler loads N+1 into the *inactive* instance at the fade-start mark.

Net rule: crossfade overrides gapless **only** when smart-album-continuity fails. Same-album adjacent → gapless. Cross-album → crossfade.

## 4. ReplayGain + cast interop

**ReplayGain.** mpv's `replaygain` option is per-instance and applies before the volume property. Each instance carries its own track's RG tags; both contribute to the fade with gains already applied. Chain order is RG → volume(fade ramp) → output. The only thing to verify: the inactive instance must read RG tags from N+1's metadata before the fade starts (mpv does this synchronously during `loadfile` for local; for streams it's deferred, which is fine).

**Cast.** Cast targets stream the raw server URL — neither jellytoast's mpv nor any local mixer is in the path. Dim the crossfade row (`setEnabled(False)`) while `cast_manager.active_cast is not None`, tooltip "Crossfade is local-playback only. Disconnect from <device> to enable." Re-enable on `cast_stopped`. Live-apply rule (§5) means a mid-cast toggle silently no-ops even if dimming is bypassed.

## 5. Settings + UI

QSettings keys (live in `playback/` to match `playback/gapless` and `playback/replaygain`):

| Key | Type | Default | Range |
| --- | --- | --- | --- |
| `playback/crossfade_enabled` | bool | `False` | — |
| `playback/crossfade_duration_ms` | int | `4000` | `1000`–`10000` |
| `playback/crossfade_smart_album_continuity` | bool | `True` | — |
| `playback/crossfade_curve` | str | `"linear"` | `"linear"` \| `"equal_power"` (v2) |

Settings → Playback additions: toggle row "Crossfade tracks," duration slider (1.0–10.0 s, step 0.5), sub-toggle "Skip crossfade between adjacent album tracks." Sub-controls indent under the parent toggle and disable when it's off.

**Live-apply rule.** Crossfade settings take effect on the **next** track transition, never mid-fade. The scheduler reads current values when it arms the fade-start trigger; once armed, the fade runs to completion under those values. Mirrors the deferred-effect pattern in `_on_prefetch_request` (see `modules/player_backend.py:824-866`).

**Curve choice for v1: linear.** Equal-power (≈ 3 dB midpoint boost) is correct for uncorrelated content like cross-album transitions ([Sound on Sound: linear vs constant-power](https://www.soundonsound.com/sound-advice/q-should-use-linear-or-constant-power-crossfades)), but we've already routed same-album adjacent to the gapless path so the linear-vs-equal-power difference is small enough to ship linear and iterate. Add equal-power as a v2 setting once the scheduler is stable.

## 6. Multi-platform notes

- **Linux (PipeWire / PulseAudio).** Two libmpv clients open two streams on the same sink; the server mixes. No contention. Primary dev surface.
- **Linux (raw ALSA, no PA/PW).** Without `dmix`, second client gets `-EBUSY`. Detect via `loadfile` error on the inactive instance; fall back to "fade-out → load → fade-in" (no overlap) and log once.
- **Windows.** WASAPI shared-mode fine; **exclusive** mode locks the second instance out. Force `audio-exclusive=no` on both; tooltip on the slider notes exclusive-mode WASAPI is disabled while crossfade is on. Audiophiles turning crossfade off get exclusive back.
- **macOS.** Core Audio mixes by default. Same code path as Linux PipeWire.
- **iOS.** Out of scope — AVAudioEngine has native crossfade nodes, but this Python codepath doesn't run on iOS anyway.

## 7. Edge cases

- **Skip during fade.** Hard-cut both, load the new "next" into the freshly-inactive slot at volume 0, fade up only the new one. Don't "finish the fade then advance" — users pressing skip want it gone now.
- **Pause during fade.** Pause **both** instances; ramps freeze at their current values; resume continues from there. (Snap-to-louder-and-pause-one feels broken — rejected.)
- **Track shorter than `2 × crossfade_duration_ms`.** Clamp fade to `track_duration / 2` rounded down to 100 ms. Under 500 ms total, skip the fade entirely (gapless).
- **"Stop after this" boundary.** Honor it — don't load N+1 into the inactive instance. Active fades to silence at EOF naturally; scheduler emits `playback_stopped`.
- **Seek during fade.** Cancel. Both instances hard-stop; seek target plays on the (newly active) instance from volume 0 with a 200 ms fade-in to suppress the click.
- **Repeat-one with crossfade.** A track fading into itself sounds awful (phasing, comb-filtering). When `RepeatMode.ONE` is on, force-disable crossfade for the self-loop regardless of setting. Document; not user-tunable.
- **Queue-end with `RepeatMode.OFF`.** Last track fades naturally at its own EOF; inactive instance never loads.
- **`end-file` while inactive is mid-`loadfile` (slow-link race, ~200–500 ms).** Guard the fade-start trigger with "inactive must report `playback-time > 0` before we ramp"; otherwise hold the active at full volume until incoming is ready. Worst case: sub-second gapless-like EOF extension, still better than a stutter.

## 8. Effort + sequencing

**Sizing: L** for the full feature; **M** for v1 with corners deferred.

Recommended v1 scope (M):
1. Two-instance scaffolding in `MpvController` — promote `_mpv` to `_mpv_a` / `_mpv_b`, route every existing method through the active pointer, no behavior change yet. (S)
2. Scheduler — observe `time-pos` on the active instance, arm fade at `duration - crossfade_duration_ms`, run linear ramps via a 50 ms QTimer on both instances' `volume`. (M)
3. Wire `playback/crossfade_enabled` + `_duration_ms` settings, UI row, cast-dim, live-apply. (S)
4. Smart-album-continuity check — same `AlbumId` + adjacent `IndexNumber` → gapless; otherwise crossfade. (S)
5. Edge cases: skip, pause, seek, repeat-one, short-track clamp, queue-end. (M)
6. Manual test pass on Linux + a Windows VM.

Deferred to v2: equal-power curve setting; Sweet-Fade/MixRamp loudness-based overlap; ALSA-only fallback to fade-out-then-in; per-playlist crossfade toggle ([Symfonium feature request 12679](https://support.symfonium.app/t/crossfade-toggle/12679)).

MPRIS routing and cast-handoff are the load-bearing pieces; both can regress today's well-tested behavior. Land the two-instance scaffolding behind `JT_CROSSFADE=1` first and verify single-instance behavior is byte-identical before exposing the Settings toggle.

## 9. Sources

- [mpv issue #4512 — crossfade in mpv](https://github.com/mpv-player/mpv/issues/4512)
- [python-mpv issue #126 — multiple instances](https://github.com/jaseg/python-mpv/issues/126)
- [Strawberry — src/core/player.cpp](https://github.com/strawberrymusicplayer/strawberry)
- [Tauon Music Box](https://github.com/Taiko2k/Tauon) · [Sonixd](https://github.com/jeffvli/sonixd)
- [Symfonium — Playback Transitions](https://support.symfonium.app/t/settings-playback-transitions/7559) · [per-playlist crossfade toggle](https://support.symfonium.app/t/crossfade-toggle/12679)
- [Music Assistant — Sweet Fade / MixRamp discussion](https://github.com/orgs/music-assistant/discussions/3929) · [Plexamp launch post](https://medium.com/plexlabs/introducing-plexamp-9493a658847a)
- [Sound on Sound — linear vs constant-power](https://www.soundonsound.com/sound-advice/q-should-use-linear-or-constant-power-crossfades) · [Foobar2000 Crossfader DSP](https://wiki.hydrogenaudio.org/index.php?title=Foobar2000%3AComponents%2FCrossfader_%28foo_dsp_crossfader%29)
- Repo: `modules/player_backend.py:148-194` (gapless prefetch state + mpv init), `:797-866` (prefetch slots), `modules/queue_manager.py:435-446` (`_emit_prefetch`), `modules/settings.py:742-796` (gapless + replaygain pattern).
