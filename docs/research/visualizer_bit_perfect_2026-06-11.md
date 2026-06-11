# Visualizer under bit-perfect playback — research (2026-06-11)

> **Scope.** How to keep the audio visualizer (`jellytoast/visualizer.py`,
> gated by `JT_VISUALIZER=1`) producing bands when playback is bit-perfect —
> especially on the direct `alsa/hw:` output path, where the stream never
> crosses PipeWire and the current default-sink-monitor tap
> (`MonitorAudioTap`) has nothing to read. Constraint: **zero added
> DSP/conversion/rerouting in the path between mpv's decoder and the DAC.**

## Summary

**Recommendation: the parallel-decode tap.** Spawn a second decoder —
`ffmpeg -i <stream_url> -f f32le -ac 1 -ar 44100 pipe:1` — purely for
analysis, synced to mpv's playback clock via the `position_updated` signal
that `PlayerBus` already carries. It is the only candidate that is
simultaneously (a) provably bit-perfect-safe (the playback chain is
*untouched* — the analysis stream is a separate process reading the same
bytes), (b) functional on ALSA-direct, (c) functional under WASAPI Exclusive
on Windows (where OS loopback capture is *documented as impossible* —
exclusive-mode streams cannot be loopback-captured), and (d) shaped exactly
like the existing tap contract: `MonitorAudioTap.__call__` already reads raw
f32le mono PCM from a subprocess stdout pipe, so a `ParallelDecodeTap` is a
sibling class behind the same `PcmCallback` interface, not a rearchitecture.
This is also conceptually how foobar2000 / Strawberry / DeaDBeeF keep
visualizations alive under exclusive output: they tap their own decode
pipeline pre-output and time-align against the playback clock, rather than
capturing from the OS.

**Bonus finding that raises the stakes:** the *current* monitor tap is itself
a bit-perfect liability in the PipeWire path. PipeWire only switches the
graph sample rate when the sink goes idle, and an active capture stream on
the sink monitor keeps it busy — the Arch wiki and forum threads explicitly
name `cava` (a monitor-capturing visualizer, i.e. exactly what we do) as a
process that locks the sample rate. So `JT_VISUALIZER=1` + the rate-following
bit-perfect config can pin the graph at the wrong rate and silently force a
resample of playback. The parallel-decode tap fixes ALSA-direct *and* removes
this footgun: while bit-perfect is active we should not have a monitor
capture open at all.

**Effort estimate:** one focused session for the tap class +
source-selection + bus wiring (~250–350 new lines in `visualizer.py` plus
~30 in the engine/widget), a second session for tests + edge cases (seek
storms, internet radio, offline files, ffmpeg-missing fallback). No changes
to `player_backend.py`'s audio path — that's the point.

## Candidates compared

| Candidate | Bit-perfect-safe? | ALSA-direct? | WASAPI-exclusive? | Sync quality | CPU / IO cost | Complexity |
|---|---|---|---|---|---|---|
| **1. Parallel-decode tap (ffmpeg subprocess)** | **Yes — playback chain untouched** | **Yes** | **Yes** | Good (~±50–100 ms vs `time-pos`; fine for 30 Hz bars) | ~1–2 % of a core (FLAC decode); doubles stream bandwidth for remote sources | Medium |
| 2a. mpv `af-add` tap (e.g. lavfi) | **No** — a filter sits between decoder and AO; the filter system inserts conversion filters when formats mismatch, and passthrough can't be proven per-codec | n/a | n/a | Perfect | ~0 | Medium, and pointless |
| 2b. mpv `--lavfi-complex` branch | Unproven at best — audio routes *through* libavfilter to reach the AO; no path to ship PCM to Python (graph outputs only map to `[ao]`/`[vo]`); known track-switch breakage (mpv #6354) | n/a | n/a | Perfect | low | High, dead-ends on PCM extraction |
| 2c. libmpv observed properties / render API / scripts | Safe but **no PCM exists** — `audio-pts`/`audio-params` only; render API is video-only; no audio-frame callback in client.h | — | — | — | — | — |
| 3. ALSA-side tee (snd-aloop / `file` plugin / dsnoop) | Mostly no — repoints the user's pinned device at a plugin PCM (no longer the raw `hw:` they chose), user-level `.asoundrc` writes, fragile | Defeats the premise | n/a | Perfect | low | High + invasive |
| 4. Second mpv handle, `--ao=pcm` to a FIFO | Yes (separate handle) — but `ao_pcm` is hard-coded **untimed** (decodes flat-out): same consumer-pacing problem as ffmpeg with a heavier process + WAV-header nuisance + no clean `-ss` restarts | Yes | Yes | Same as 1 after the same sync work | Higher (full mpv core) | Strictly worse than 1 |
| 5. Status quo monitor tap (PipeWire path) | **Partially compromised** — the capture stream pins the graph rate while open (the cava problem) | No (the gap this doc exists for) | No (loopback ≠ exclusive) | Good | ~0 | Already shipped |

### Candidate notes

**1. Parallel-decode tap.** Key facts established:

- ffmpeg with no rate flag decodes **as fast as possible** and blocks on the
  stdout pipe once the kernel pipe buffer (~64 KiB ≈ 0.37 s of mono f32 @
  44.1 kHz) fills — so a consumer that reads paced-by-playback-clock
  automatically rate-limits the decoder. `-re` exists (paces input reads
  against wall clock) but is the wrong tool: it free-runs on ffmpeg's own
  clock and drifts from mpv's DAC-driven clock, whereas consumer-paced reads
  keyed to `time-pos` are drift-free *by construction*. Don't use `-re`;
  pace in the reader.
- `-ss <time-pos>` before `-i` does a fast demuxer seek; for HTTP sources
  ffmpeg issues Range requests, which Jellyfin's `static=true` direct-stream
  endpoint honors. Restart-on-seek is cheap (<200 ms typically).
- Stream URLs are self-contained: `jellyfin_api.get_audio_stream_url` bakes
  `api_key=<token>` into the query string (jellyfin_api.py:506-512), and
  Subsonic auth is always query-params — ffmpeg needs **zero auth
  plumbing**; it fetches the exact same URL mpv plays. Offline tracks are
  `file://` URIs (`NowPlaying.is_local`) — strip the scheme and hand ffmpeg
  the path; no second network fetch at all in the offline case.
- CPU: FLAC and MP3 decode at >100× realtime; expect ~1–2 % of one core,
  less than the FFT itself at 30 Hz. The real cost is **bandwidth for remote
  streams** (a second full-rate fetch — ~1 Mbps extra for 16/44 FLAC).
  Trivial on LAN, noticeable over Tailscale/WAN; worth a settings hint.
- It would also, incidentally, work **while casting** (the monitor tap is
  zeroed during cast today because local mpv is silent) — but cast-position
  sync is much looser; treat as a possible follow-up, not a v1 goal.

**2. mpv-internal taps — why they're honestly out.**

- *Filter insertion:* mpv's filter docs are clear that the filter system may
  "insert necessary conversion filters before or after" a filter. An
  analysis filter declares the formats it supports; if the decoder produced
  `s32` (typical FLAC) and the filter wants float, conversion happens *in
  the playback path*. Even when a given filter happens to pass through
  unconverted, it can't be guaranteed across codecs/builds — and "probably
  bit-perfect" is not a claim to print next to a "Bit Perfect" badge.
  Verdict: any `af-add` while the contract is active is disqualified.
- *`--lavfi-complex`:* same chain concern, plus two practical killers:
  (a) the graph's labeled outputs can only map to mpv's own `[ao]`/`[vo]` —
  no supported filter ships PCM to an external process, which is why the
  community `visualizer.lua` precedent renders *inside mpv's video output*;
  (b) known fragility around track/aid switching (mpv #6354) and an
  unverified interaction with gapless prefetch our own visualizers.md
  already flagged.
- *libmpv API:* render API is video-only; properties expose metadata
  (`audio-params`, `audio-pts`, `audio-bitrate`), never frames. No
  audio-frame callback in the client API as of mpv 0.41. NOTE: the
  `MpvAudioTap` stub's docstring in visualizer.py ("af-add … ships PCM
  frames back via a libmpv IPC pipe") describes plumbing that does not
  exist upstream — correct it when this lands.
- *Marginal trick:* `af-metadata` on an inserted `ebur128`/`astats` filter
  can surface per-frame loudness via property observation — enough for a VU
  meter, not a spectrum, and it still inserts a filter into the chain.

**3. ALSA-side loopback.** `snd-aloop` means playing into a loopback card and
having something else move samples to the DAC — the user no longer plays to
the device they pinned. `dsnoop` shares *capture* devices; it can't tee
playback. The closest real option is ALSA's `file` plugin (`type file`,
`slave.pcm "hw:X,Y"`, raw passthrough to a FIFO) — samples reach the hardware
unmodified, so it's *arguably* bit-exact — but it requires writing a
user-level `.asoundrc` PCM, pointing mpv at `alsa/jellytoast_tee` instead of
the user's raw `hw:` pick, keeping the FIFO drained (or playback stalls), and
surviving device-name drift. It converts "pin the raw device" into "trust our
plugin chain" — precisely the psychology ALSA-direct users opted out of.
Rejected.

**4. Second mpv handle.** Multi-handle infrastructure exists
(`_make_mpv_handle` is factored for the Crossfader sibling), so this was
worth taking seriously. The only PCM egress libmpv offers is `--ao=pcm`
(file/FIFO). Two problems: `ao_pcm.c` hard-sets `untimed = true` — it decodes
flat-out, so you inherit the identical pacing/sync problem as ffmpeg while
paying for a whole mpv core, a FIFO, and a WAV header; and seeks mean driving
a second mpv via commands instead of killing/respawning a one-line
subprocess. ffmpeg is strictly better. (`ao=null` on a second handle gets
nothing — null discards samples with no egress.)

**5. PipeWire-only world.** Two sub-findings:

- *The good:* when bit-perfect rides PipeWire (rate-following config
  installed), the sink monitor exists and carries the post-mix signal, so
  the current tap keeps producing bands. The monitor runs at the **graph
  rate**; `pw-record --rate=44100` resamples in the *capture* stream, which
  does not touch the playback branch.
- *The bad (new):* rate switching only happens when the sink is idle, and an
  open monitor-capture keeps it active. Our `pw-record` runs continuously
  from the first time the visualizer widget is built until app teardown.
  Concretely: graph idles at 48 kHz → user opens the visualizer → pw-record
  pins 48 kHz → user plays a 44.1 kHz FLAC → PipeWire resamples it, with the
  bit-perfect badge lit. `docs/bit_perfect.md` ("first stream wins") even
  lists `cava` as a culprit without noticing we *are* cava. The honest
  messaging is therefore **not** "visualizer works unless you go
  ALSA-direct" — it's "the monitor tap and the bit-perfect contract conflict
  in *both* paths, so bit-perfect playback should always use the
  parallel-decode tap (or pause the monitor capture)."

**Windows.** WASAPI loopback capture is only available for shared-mode
streams — Microsoft's docs state exclusive-mode streams cannot operate in
loopback mode. The planned P4 WASAPI-loopback backend would therefore go dark
exactly when the audiophile features are on — same shape as the ALSA-direct
gap. The parallel-decode tap covers Windows-exclusive with the *same code*
(ffmpeg.exe needed — see packaging). Strong argument for making the parallel
tap the canonical "bit-perfect companion" rather than a Linux-ALSA special
case.

## Recommended design

### New class: `ParallelDecodeTap` (in `jellytoast/visualizer.py`)

Same `PcmCallback` shape as `MonitorAudioTap` — `start()` / `stop(fast=)` /
`__call__() -> Optional[NDArray]` — so `_FFTWorker` and `VisualizerEngine`
need no structural change.

**Subprocess:**

```
ffmpeg -hide_banner -loglevel error [-ss <pos_s>] -i <source>
       -vn -map a:0 -f f32le -acodec pcm_f32le -ac 1 -ar 44100 pipe:1
```

- `<source>`: `NowPlaying.stream_url`, with `file://` stripped for local
  blobs. Resolve at `playback_started` time.
- `-ar 44100` keeps `compute_bands`'s fixed `sample_rate=44100` valid
  (resampling here is analysis-only — irrelevant to playback fidelity).
- No `-re`. Pacing is consumer-side; kernel pipe back-pressure caps ffmpeg's
  read-ahead at ~0.4 s.

**Sync (the core of the work).** Maintain `consumed_samples` (incremented per
read) and `anchor_s` (the `-ss` value the current process started at). Target
position = latest `position_updated` value (ms → samples). Each `__call__`:

1. `lead = (anchor_s * rate + consumed) - target_samples`
2. Within ±2×`_FFT_WINDOW` (~93 ms) slop: read one window, return it.
3. Behind beyond slop: read-and-discard until aligned (decode is faster than
   realtime — cheap), then return a window.
4. Ahead beyond slop: return `None` this tick (can't un-read); the
   consumer-paced loop makes this rare.
5. `|lead|` exceeds the restart threshold (~2 s — a real seek): kill +
   respawn with `-ss target`.

**Event wiring** (all signals already exist on `PlayerBus`):

- `playback_started(NowPlaying)` → respawn with the new source at
  `np.position` (covers track change + restore-at-position).
- `position_updated(ms)` → atomically store target (variable write only).
- `playback_paused` / `playback_resumed` → stop reading on pause (target
  stops advancing, step 4 naturally yields `None`/zeros; SIGSTOP/SIGCONT is
  a nice-to-have, not required given back-pressure).
- `playback_stopped` / `playback_ended` → kill the subprocess.
- Seeks need no dedicated signal: a `position_updated` discontinuity trips
  the restart threshold. (`seek_requested` exists if we ever pre-empt.)

**Source-selection logic** (engine + a live-swap slot):

```
use_parallel = (
    settings.audio_output_device.startswith("alsa/")   # monitor doesn't exist
    or bus.bit_perfect_active                          # don't pin the graph rate
    or (windows and exclusive active)                  # loopback can't see it
)
```

Subscribe to `audio_output_device_changed` and `bit_perfect_active_changed`
to swap taps live (the widget already subscribes to the former for its
caption — the caption then only remains for the ffmpeg-missing case).
Default stays `MonitorAudioTap`: zero extra bandwidth, reacts to system
audio, already proven. A future `visualizer/tap_source` setting
(`auto` / `monitor` / `decode`) is cheap if wanted.

**Failure modes:**

- `shutil.which("ffmpeg") is None` → log once, tap returns `None`, widget
  keeps a caption ("visualizer needs ffmpeg for bit-perfect output") —
  mirror the existing `_warned_missing` pattern.
- ffmpeg dies (network blip, 401 on token rotation) → reuse the
  respawn-with-backoff pattern (`_RESPAWN_BACKOFF_S`), respawning at the
  current target position.
- `QueueKind.INTERNET_RADIO` → live streams can't `-ss` and have no stable
  timeline; decode live without sync (ICY buffers are small — bars will be
  near-real-time) or fall back to caption. Recommend: decode live, skip the
  seek logic, accept ~stream-buffer lag.
- Crossfade overlap: irrelevant under bit-perfect (force-disabled) and on
  ALSA-direct (`_ensure_crossfader` returns None for `alsa/`).

**Tests:** fake-ffmpeg script emitting a known waveform (the pipe interface
makes this trivial — same trick as existing tap tests), sync math with a
pinned clock (injectable `now_fn` already exists in tap + worker),
seek-restart threshold, pause = zeros, missing-binary fallback.

**Also fix while in there:** stop the `MonitorAudioTap` while
`bit_perfect_active` even when the user *hasn't* gone ALSA-direct (the
rate-pinning finding) — selecting the parallel tap in that state does this
implicitly.

### Packaging

- **Linux native:** ffmpeg CLI is effectively universal where mpv is
  installed, but it is *not* a hard dependency today — handle absence
  gracefully; consider an optional-dep note.
- **Flatpak:** per `docs/research/flatpak_manifest_2026-06-11.md`, ffmpeg
  comes from the KDE runtime — verify the CLI binary (not just libs) ships
  in the runtime during the packaging pass; if libs-only, a small manifest
  module bundles it.
- **Windows:** ffmpeg.exe must ship or be found on PATH (~25 MB full, ~10 MB
  audio-trimmed). Alternative: PyAV (bundled wheel, in-process, no
  subprocess management) — deletes the lifecycle code at ~25 MB wheel cost;
  subprocess-ffmpeg keeps GPL coupling at arm's length (CLI invocation, no
  linking question).

## What stays impossible

- **PCM out of the playing mpv handle without touching the output chain.**
  libmpv has no audio-frame callback; `lavfi-complex` can't export samples;
  any inserted filter is *in the chain* and disqualified. Until upstream
  grows an audio-tap API, the playing handle is a black box between decoder
  and AO.
- **OS-level capture of an exclusive stream.** WASAPI loopback is
  shared-mode-only by API contract; a raw `hw:` ALSA open has no monitor by
  definition; macOS HogMode will have the same shape. Structural, not an
  implementation gap.
- **Sample-exact sync.** `time-pos`-anchored alignment is good to roughly
  ±50–100 ms. Invisible for 30 Hz bars; might not be for a future
  oscilloscope view. The monitor tap has its own ~20 ms+ latency, so not a
  regression.
- **Visualizing without the second fetch on remote streams.** Any parallel
  decode re-downloads the audio (except offline/local). Tapping mpv's
  demuxer cache is not exposed.

## Open questions for august

1. **Default tap policy:** parallel tap whenever `bit_perfect_active`
   (recommended, due to the rate-pinning finding), or only on ALSA-direct?
2. **Bandwidth consent:** silent second stream over the network OK, or
   should remote (non-LAN/non-offline) sources show a one-time hint /
   setting? (Tailscale-remote listening is a real use case.)
3. **Windows ffmpeg:** bundle ffmpeg.exe, or take the PyAV dependency for an
   in-process decoder on all platforms?
4. **Internet radio:** decode-live-unsynced bars, or caption out?
5. Rewrite the `MpvAudioTap` stub's docstring (promises an af-add path that
   can't preserve bit-perfect and has no PCM egress) so the dead end isn't
   re-attempted.

## Sources

- Code: `jellytoast/visualizer.py` (MonitorAudioTap pipe contract, worker,
  engine), `jellytoast/visualizer_widget.py:105-130, 224-237, 449-457`,
  `jellytoast/player_backend.py:321-410, 897-926`,
  `jellytoast/player_state.py` (PlayerBus signals),
  `jellytoast/jellyfin_api.py:484-524` (token-in-URL stream endpoints),
  `jellytoast/np_left_pane.py:60-94` (engine lifecycle),
  `docs/bit_perfect.md`, `docs/research/audio_output_routing.md`,
  `docs/research/bit_perfect_playback.md`, `docs/research/visualizers.md` §2,
  `docs/research/flatpak_manifest_2026-06-11.md`.
- mpv af.rst (filter system inserts conversion filters) —
  https://github.com/mpv-player/mpv/blob/master/DOCS/man/af.rst
- mpv issue #6354 (lavfi-complex track-switch breakage) —
  https://github.com/mpv-player/mpv/issues/6354
- mpv ao.rst + ao_pcm.c (`ao_pcm` is untimed) —
  https://github.com/mpv-player/mpv/blob/master/audio/out/ao_pcm.c
- Microsoft — Loopback Recording (shared-mode only) —
  https://learn.microsoft.com/en-us/windows/win32/coreaudio/loopback-recording
- Arch wiki PipeWire + forum thread on rate switching (cava pins the rate) —
  https://wiki.archlinux.org/title/PipeWire ·
  https://bbs.archlinux.org/viewtopic.php?id=288932
- ffmpeg `-re` semantics — https://www.ffmpeg.org/ffmpeg.html ·
  https://github.com/kkroening/ffmpeg-python/issues/176
- foobar2000 WASAPI component + Hydrogenaudio output docs (visualizations
  under exclusive output run off the player's own decode pipeline) —
  https://www.foobar2000.org/components/view/foo_out_wasapi ·
  https://wiki.hydrogenaudio.org/index.php?title=Foobar2000%3APreferences%3AOutput
