# Bit-perfect playback — research

**Status — shipped (updated 2026-06-08):** the bit-perfect path landed T1-T4 (`9abed14`). `settings.bit_perfect_mode` gates a runtime contract in `modules/player_backend.py` (`_compute_bit_perfect_active` + the `bit_perfect_changed` bus signal); `modules/pipewire_setup.py` ships the §4.2 `default.clock.allowed-rates` recipe; the user guide is `docs/bit_perfect.md`. Off by default (explicit opt-in). Companion to `eq_dsp.md`; kept for the original rationale.
**Scope:** the regular (EQ-off, ReplayGain-off, crossfade-off, no DSP) path. What jellytoast
currently does, what would make it audiophile-tier, and what each tier costs in UX.

## 1. What "bit-perfect" means here

A playback path is **bit-perfect** when the PCM samples decoded from the source file reach the
DAC byte-identical: no sample-rate conversion, no bit-depth conversion (other than zero-padding,
which Audirvana and the consensus consider neutral [1]), no dither, no float-math mixing, no
software volume scaling below 100 %, no shared-mixer summation with other apps.

Things that defeat it, ranked by how often they bite in practice:

1. **Session-rate resampling.** PipeWire's default session rate is 48 kHz. A 44.1 kHz CD-source
   file gets resampled to 48 kHz by PipeWire's `spa_resample` before it reaches the DAC unless
   `default.clock.allowed-rates` is configured and the graph is idle when the stream starts [2][3].
2. **Software volume < 100 %.** mpv applies `volume` as a float multiply in its filter chain.
   At 100 % it's a no-op; at 99 % it isn't. (jellytoast's volume slider is software, not
   hardware-DAC.)
3. **ReplayGain.** A pre-amp + clip-protect float multiply; identical to (2) when active.
4. **Other apps' streams mixing in.** Any other PipeWire client (browser tab, Discord, system
   bell) on the same sink forces the mixer to operate in float and possibly resample. Exclusive
   modes (WASAPI exclusive, CoreAudio HogMode, raw ALSA `hw:`) bypass this.
5. **Format conversion inside mpv.** mpv negotiates `audio-format` with the AO; if AO advertises
   `f32` and the file is `s16`, mpv converts. Zero-pad / sign-extend conversions are loss-free in
   the Audirvana sense, but a true integer-perfect chain still wants them off.
6. **The EQ chain** — already disclosed by the EQ caption (eq_dsp §8). Out of scope here.

## 2. Current jellytoast playback path

Walkthrough of a 44.1 kHz / 16-bit FLAC played through jellytoast on the user's CachyOS / KDE
Wayland / PipeWire 1.6.x rig:

```
Jellyfin /Audio/{id}/stream?static=true     # direct, byte-verbatim FLAC bytes [verified §2 of
                                            # jellyfin_api.py:447-482 when audio_quality=='original']
    │
    ▼
mpv libavformat demux  →  mpv libavcodec decode  →  s16le PCM @ 44.1 kHz
    │
    ▼
mpv internal filter chain
    • volume        ← settings.volume (default 100, float multiply when < 100)
    • replaygain    ← settings.replaygain (default 'no' = bypass)
    • af= (empty unless EQ on)
    │
    ▼
ao=pipewire   (auto-selected; not explicitly set)
    │
    ▼
PipeWire spa_audioconvert
    • resample.quality = 4 (default)
    • format conversion to f32 (default node format)
    • mixer sums other clients on the same sink
    │
    ▼
PipeWire ALSA sink @ default.clock.rate = 48 kHz (PipeWire's default)
    │
    ▼
DAC
```

**Corner-cuts on this path, ranked**:

| # | What's being cut | Where | Severity |
| - | --- | --- | --- |
| 1 | 44.1 kHz source resampled to 48 kHz by PipeWire | PipeWire `default.clock.rate=48000`, no `default.clock.allowed-rates` set | High — affects every CD-rate file, which is most music |
| 2 | Software volume float-multiply at 99 % and below | mpv `volume` kwarg | Medium — only when volume < 100 |
| 3 | Format conversion to f32 inside PipeWire | mpv has no `audio-format` constraint | Low (zero-padding is benign per Audirvana [1]) |
| 4 | Shared mixer (other apps can sum in) | mpv `audio-exclusive` unset → defaults to no | Medium — silent in the empty case, audible if a browser tab is buffering |
| 5 | `gapless-audio=weak` may insert silence/skip to align rates | mpv `gapless_audio="weak"` | Negligible for normal play; relevant only at track-boundaries on rate-mismatched files |

What's **already right**:

- `audio_quality=='original'` hits `/Audio/{id}/stream?static=true` (direct play, no transcode).
- `replaygain` defaults to `"no"` (bypass — never touches samples).
- `replaygain_clip="no"` (no extra limiter even when RG is on).
- `audio_display="no"` (no video pipeline interfering).
- `hwdec="auto-safe"` (only video; audio decode is unaffected).
- `audio_client_name="jellytoast"` (just a PW node name; bit-neutral).

## 3. mpv config audit

mpv option defaults pulled from the manual [4]; behaviour confirmed against the mpv-player issue
tracker [5][6][7][8][9].

| kwarg | current value | mpv default | bit-perfect implication | recommended (audiophile) |
| --- | --- | --- | --- | --- |
| `ytdl` | `False` | `True` | neutral (we don't stream YT) | keep |
| `osc`, `force_window`, `input_*` | various off | various | neutral (UI only) | keep |
| `audio_display` | `"no"` | `"attachment"` | neutral; avoids cover-art video output | keep |
| `hwdec` | `"auto-safe"` | `"no"` | **audio-neutral** (video only) | keep |
| `cache` | `"yes"` | varies | neutral; bytes are bit-identical to source | keep |
| `volume` | persisted (0-100) | `100` | **NOT bit-perfect at < 100** — float multiply | expose "100 % only" mode (T3) |
| `replaygain` | persisted (default `"no"`) | `"no"` | bit-perfect when `"no"` | keep default; already gated by setting |
| `replaygain_clip` | `"no"` | `"yes"` | neutral when RG is off; better than default when RG is on | keep |
| `gapless_audio` | `"weak"` | `"weak"` | `"weak"` keeps streaming gap-free across same-codec tracks but **may insert silence** on mismatched rates [4]; `"yes"` forces a single rate (which would cause more resampling). | keep `"weak"`; reaching `"yes"` would be a net loss |
| `prefetch_playlist` | `"yes"` | `"no"` | neutral; pre-decodes next track | keep |
| `audio_client_name` | `"jellytoast"` | `"mpv"` | neutral (just a PW node name) | keep |
| `audio_samplerate` | **not set** | mpv asks AO for source rate; AO may negotiate down | **PipeWire's session rate wins**; mpv doesn't push for a rate switch | leave unset; the fix lives in PipeWire config (§4) |
| `audio_format` | **not set** | auto-negotiated, usually `f32` | format conversion happens silently | leave unset (zero-padding is benign per [1]) |
| `audio_channels` | **not set** | `"auto-safe"` (stereo) | neutral for music | keep |
| `audio_exclusive` | **not set** | `"no"` | shared mixer; other apps can summing in | T3 toggle (§7) |
| `audio_resample_filter` | **not set** | `"linear"` w/ swresample defaults | only matters when mpv resamples — which it shouldn't if PipeWire handles it | leave |
| `audio_pitch_correction` | **not set** | `"yes"` | inserts `scaletempo2` only at non-1.0× speed; bit-neutral at 1.0× | leave |
| `audio_spdif` | **not set** | unset | only relevant for AC3/DTS passthrough — not music | leave |
| `audio_fallback_to_null` | **not set** | `"no"` | neutral | leave |
| `ao` | **not set** | auto (PipeWire on Linux ≥ 0.35; CoreAudio on macOS; WASAPI on Windows) | auto is fine on modern Linux | leave; advanced users override via mpv.conf |

**Key finding:** the *mpv handle itself* is already very close to bit-perfect once `volume` is
100 and `replaygain` is `"no"`. mpv does **not** silently insert resampling — it requests the
source rate from the AO and lets the AO decide. On Linux/PipeWire that decision happens at the
PipeWire layer, not in mpv [10].

## 4. The PipeWire interaction

This is where 90 % of the loss happens, and it is **not fixable from inside mpv**.

### 4.1 Why 44.1 → 48 kHz resampling happens by default

PipeWire ships with `default.clock.rate = 48000` and no `default.clock.allowed-rates` set. The
graph runs at 48 kHz; any stream at a different rate is resampled by `spa_audioconvert` using the
PipeWire resampler [3]. Quality knob is `resample.quality` (0-15, default **4**) [3][13].

### 4.2 How to make PipeWire follow the source rate

User config in `~/.config/pipewire/pipewire.conf.d/10-jellytoast-bitperfect.conf`:

```
context.properties = {
    default.clock.allowed-rates = [ 44100 48000 88200 96000 176400 192000 ]
}
stream.properties = {
    resample.quality = 14
}
```

With `allowed-rates` populated, PipeWire will switch the graph rate to match the playing stream
**provided the device is idle when the new stream starts** [2][11]. If anything else is producing
on the same sink (a background EasyEffects, cava, a paused browser tab keeping the node corked),
the rate is locked and the new stream is resampled.

The forum-verified gotcha [11]: **first stream wins**. Once a 48 kHz client lands on the sink,
every subsequent 44.1 kHz stream resamples until the sink goes idle. Practical mitigation: start
jellytoast first, kill `cava` / EasyEffects when bit-perfect matters.

### 4.3 mpv `audio-exclusive=yes` on PipeWire

`audio-exclusive=yes` on the `pipewire` AO sets `node.exclusive` on the stream, which corks all
other streams routed to that sink for the duration of playback. It is NOT the same level of
guarantee as WASAPI exclusive on Windows (the kernel-level lock); it's a PipeWire-graph
convention. It does, however, give the rate-switch logic a clean shot — nothing else is corking
the device — so it is the cheapest local fix for the "first stream wins" problem [11][14].

Downsides on Linux: notifications and other apps go silent during playback. For a music player
this is mostly desirable; for a casual listener mid-meeting it isn't. Behind a setting.

### 4.4 `resample.quality` levels

Default is 4 (linear-ish); audiophile-tier consensus on HeadFi / EndeavourOS / ArchWiki is **14**
(near sinc, high CPU, low ringing) [2][3][13]. Even with `allowed-rates` set, you still want
`resample.quality = 14` for the cases where resampling is forced (96 kHz file on a DAC that maxes
at 48 kHz, mixing-during-rate-lock, etc.). This is a stream-property and a user-side PipeWire
config — not a per-app mpv option.

## 5. Cross-platform

### 5.1 Windows — WASAPI exclusive

`ao=wasapi` + `audio-exclusive=yes` gives true exclusive access via the WASAPI exclusive endpoint
[15][16]. The DAC opens at the file's native rate/depth; nothing else plays through it.

**Caveats** the mpv tracker still lists as open as of late-2025:
- #11600 — playback freezes on some Realtek configs (under-run loop) [6].
- #11733 — "Device doesn't recognize this command" on exclusive on some hardware (closed) [7].

Stable on most external USB DACs and SPDIF. Internal-laptop Realtek is the canary failure mode.

UX cost: other apps go fully silent until mpv releases the device. Standard audiophile trade.

### 5.2 macOS — CoreAudio HogMode

mpv ships `ao_coreaudio_exclusive` (`ao=coreaudio_exclusive` or `coreaudio` + `audio-exclusive=yes`),
which sets `kAudioDevicePropertyHogMode` to take exclusive control [17][18].

Hard rule: macOS **refuses** HogMode on internal headphone / speaker outputs and will kill the
process [17]. Only USB / external DACs work. Two modes inside the implementation — Float (32-bit
float internal) and Integer (DAC-native int passed straight through). Integer mode is the real
bit-perfect target.

We are not targeting macOS yet (user_hardware.md), but the option exists when we do.

### 5.3 iOS — not applicable

mpv isn't the right engine on iOS (eq_dsp_v2 §6 already states this). `AVAudioEngine` would be
the equivalent. Out of scope.

### 5.4 Linux raw ALSA

`ao=alsa` + a `hw:` device + `audio-exclusive=yes` bypasses dmix and PipeWire entirely [12]. The
hardware-level exclusive equivalent. Cost: PipeWire's downstream apps (browser, video calls)
break for the session, the cast subsystem still works (it never used the local DAC), but
notifications die silently. Power-user toggle only.

## 6. Symfonium + the audiophile bar

### 6.1 Symfonium

Symfonium's developer has confirmed in the feature-request thread that Symfonium is **not
bit-perfect** [19]. Symfonium uses the standard Android Media3/ExoPlayer audio path (AudioTrack,
shared mode), the Android system mixer is in the loop, and the Android global mix output rate
applies. Workaround the Symfonium community recommends for true bit-perfect is to use UAPP (USB
Audio Pro), which bypasses Android's audio framework and talks USB to a DAC directly.

This means jellytoast, **today**, can over-take Symfonium on bit-perfect just by shipping the
T1 quiet improvements below — neither player is bit-perfect by default, but jellytoast has the
mpv + PipeWire / WASAPI / CoreAudio toolchain available to reach the bar; Symfonium is constrained
by Android's audio stack.

### 6.2 Audirvana / Roon / foobar2000 / Strawberry — the bar

| Player | Bit-perfect by default? | How |
| --- | --- | --- |
| **Audirvana** [1] | yes (Windows + macOS) | Bypasses system mixer via WASAPI exclusive / CoreAudio HogMode; sends source rate / depth verbatim. Zero-pad to wider DAC depth is allowed. |
| **Roon** | yes (Windows / macOS / Linux RAAT endpoint) | Exclusive output per platform; explicit "signal path" view that lights up "Lossless" only when no DSP is in the chain. |
| **foobar2000** [20] | yes since 2.0 | Built-in WASAPI exclusive output that switches the device's rate/depth to match the track. ASIO is the historical alternative. |
| **Strawberry** [21] | configurable | WASAPI exclusive on Windows, raw ALSA `hw:` on Linux; needs user-set volume = 100 % and ALSA backend chosen. Default install is *not* bit-perfect. |
| **HQPlayer** | "as much as you want" | Their whole shtick is configurable upsampling + DSD conversion; bit-perfect is one mode among many. |
| **Tauon / Supersonic** | no | Default GStreamer / Miniaudio paths through PulseAudio/PipeWire shared mode. |

**The bar to clear:** opt-in exclusive mode + automatic device-rate-switching to match the source.
Audirvana / Roon / foobar2000 / Strawberry all let the user enable this with one setting. Tauon
and Supersonic do not. jellytoast today is at the Tauon / Supersonic tier on this axis.

## 7. Tiered upgrade path

Mirroring the tier framework used in `eq_dsp.md`.

### T0 — current state (today)

Direct stream from Jellyfin, mpv decode, default PipeWire / CoreAudio / WASAPI shared mode. Loss
points: 44.1 → 48 kHz resampling at PipeWire defaults, software volume below 100 %, shared mixer.
No user surface, no caption, no awareness.

### T1 — cheap wins, zero UX cost (recommended to ship quietly)

1. **Caption under the audio-quality dropdown.** "For bit-perfect playback, set Volume to 100 %
   and configure PipeWire's `default.clock.allowed-rates` to your DAC's supported rates. See
   docs/bit_perfect.md." Same disclosure pattern as the EQ caption.
2. **Lock-step the existing settings.** When the user picks `audio_quality = 'original'`, the
   path is already bit-capable. Nothing changes in the handle.
3. **Doc page** (separate from this research doc): one-screen recipe for the PipeWire conf
   above, the Windows WASAPI checkbox plan from T3, and the "what defeats bit-perfect" list.

Cost: ~30 min of doc work. UX cost: zero (caption only). Win: parity with Strawberry's
*configurability* — same recipe, just less hand-holding.

### T2 — small toggle in Settings → Playback → "Bit-perfect mode"

A single checkbox. When on:

- Force `volume = 100` and lock the slider (with a "Disable bit-perfect mode to adjust" tooltip).
- Force `replaygain = 'no'` (already default, but make sure the setting can't override it).
- Force `eq_enabled = False` (when EQ ships).
- Caption nearby explains the PipeWire config is the user's responsibility (or, if shipped, a
  link to the helper script that drops the conf in place).

mpv changes: none beyond what the gating already does. Implementation is in Settings + a small
guard in `_make_mpv_handle` and the slider widget. Worth doing only after T1 has been live and the
user has confirmed the audible-difference test passes.

Cost: ~half a day (settings + UI + guards + tests). UX cost: slider is locked while on (this is
the standard audiophile convention; Roon, Audirvana, fb2k all do the same).

### T3 — exclusive output

Add an `audio_exclusive` boolean to Settings → Playback → Bit-perfect mode → "Exclusive output".
Plumbs through to `audio_exclusive=yes` in `_make_mpv_handle`.

- **Linux/PipeWire**: corks other streams on the sink for the play session. Notifications die
  during playback. Standard audiophile behaviour.
- **Windows/WASAPI**: locks out other apps. Known mpv issues #11600 / #11733 mean a
  fallback-to-shared on failure is mandatory (mpv exposes `--audio-fallback-to-null` but not a
  "shared-on-exclusive-fail"; jellytoast would need to catch the open failure and rebuild the
  handle without the flag).
- **macOS**: HogMode; works on USB DACs, fails on internal speakers (macOS kills mpv). Gate the
  toggle behind a "USB DAC detected" check before we even surface it.

Cost: ~1 day plus per-platform live testing. UX cost: significant — other apps go silent, system
sounds die, must be opt-in.

### T4 — rate-switching helper

Ship a small Settings → Playback → "Apply audiophile PipeWire config" button that drops the
`10-jellytoast-bitperfect.conf` snippet from §4.2 into the user's `~/.config/pipewire/pipewire.conf.d/`.
Idempotent, reversible ("Remove" button), tells the user to log out/in. Linux only.

Cost: ~2 hours. UX cost: minimal (it's gated, it's reversible). Win: jellytoast becomes the first
Jellyfin client on Linux to ship audiophile-tier PipeWire setup by default.

## 8. Concrete recommendation

**Right now (do quietly):**

- Land **T1**: write `docs/bit_perfect.md` (user-facing, not research) with the PipeWire conf
  recipe + per-platform notes + "what defeats bit-perfect". Add a one-line caption under
  Settings → Playback → Audio quality: "bit-perfect playback requires 'Original' quality,
  Volume 100 %, ReplayGain off, and matched session rate. See Bit-perfect setup".
- Audit `_make_mpv_handle` for unused-but-noisy options. The current block is clean — no
  changes recommended.

**Next iteration (after the EQ doc lands and the slider is gated):**

- Land **T2**: a "Bit-perfect mode" toggle that gates volume/replaygain/eq into the safe
  configuration. No exclusive output yet.
- Add a green "Lossless" badge somewhere small on the now-playing page when (a) source is
  served direct, (b) mode is on, (c) no DSP active. Roon's signal-path indicator, scaled down.

**Phase 2 (audiophile bragging-rights tier, only after stable user base):**

- Land **T3** Linux first (PipeWire exclusive — least risky), then Windows WASAPI with a
  shared-fallback wrapper, then macOS HogMode gated on USB-DAC detection.
- Land **T4** the helper button.

**Things to leave alone:**

- `gapless_audio="weak"`. The bit-perfect cost is theoretical (silence on rate-mismatch, never
  resampling); the gapless UX win is real. `"yes"` would force a fixed rate and hurt more than
  it helps.
- `audio-format` / `audio-samplerate` / `audio-channels` mpv kwargs. Leaving them unset lets
  mpv negotiate with the AO, which is correct on all three target platforms.
- Crossfade. By design it isn't bit-perfect (eq_dsp §8 family). Already gated behind a separate
  toggle.

**The one-line elevator pitch:** *jellytoast is bit-perfect-capable today; the one thing keeping
the typical user from hearing it is PipeWire's 48 kHz session rate. Land a doc, then a toggle,
then exclusive. Don't touch mpv's defaults — they're already correct.*

## 9. Sources

[1] Audirvana support — "Is Audirvana bit-perfect?" — https://help.audirvana.com/en/support/solutions/articles/202000051094-is-audirv%C4%81na-bit-perfect-
[2] Arch BBS — "[SOLVED] Get bit-perfect audio with PipeWire" — https://bbs.archlinux.org/viewtopic.php?id=290859
[3] PipeWire docs — pipewire-props man page — https://docs.pipewire.org/page_man_pipewire-props_7.html
[4] mpv manual (stable) — https://mpv.io/manual/stable/
[5] mpv issue #10441 — sample rate switch between tracks under PipeWire (closed not-planned) — https://github.com/mpv-player/mpv/issues/10441
[6] mpv issue #11600 — WASAPI exclusive under-run freeze — https://github.com/mpv-player/mpv/issues/11600
[7] mpv issue #11733 — WASAPI exclusive "Device doesn't recognize this command" — https://github.com/mpv-player/mpv/issues/11733
[8] mpv issue #11221 — sample rate confusion — https://github.com/mpv-player/mpv/issues/11221
[9] mpv issue #16459 — PipeWire AO regressions — https://github.com/mpv-player/mpv/issues/16459
[10] Kodi forum — "PipeWire backend does not switch samplerate" — https://forum.kodi.tv/showthread.php?tid=375550
[11] Arch BBS — "[SOLVED] PipeWire not changing sample rate on the fly" — https://bbs.archlinux.org/viewtopic.php?id=288932
[12] Arch BBS — "PipeWire bitperfect recording" — https://bbs.archlinux.org/viewtopic.php?id=275395
[13] Head-Fi — "Bit perfect playback on Linux (PipeWire)" — https://www.head-fi.org/threads/bit-perfect-playback-on-linux-pipewire.973318/
[14] EndeavourOS — "How to achieve bit perfect audio output" — https://forum.endeavouros.com/t/how-to-achieve-bit-perfect-audio-output/49760
[15] Auris Journal — "WASAPI Exclusive vs Shared Mode: 2026 Setup Guide" — https://aurisplayer.com/blog/wasapi-exclusive-guide.html
[16] Fidelizer Audio — foobar2000 WASAPI exclusive — https://fidelizer-audio.com/tag/foobar2000/
[17] Apple Developer Forums — kAudioDevicePropertyHogMode — https://developer.apple.com/forums/thread/70274
[18] mpv source — `audio/out/ao_coreaudio_exclusive.c` — https://github.com/mpv-player/mpv/blob/master/audio/out/ao_coreaudio_exclusive.c
[19] Symfonium support — "Is it possible to add bit perfect audio please" — https://support.symfonium.app/t/is-it-possible-to-add-bit-perfect-audio-pleased/10400
[20] foobar2000 components — WASAPI output support — https://www.foobar2000.org/components/view/foo_out_wasapi
[21] Strawberry music player wiki — FAQ — https://wiki.strawberrymusicplayer.org/wiki/FAQ
[22] mpv issue #15804 — passthrough with PipeWire — https://github.com/mpv-player/mpv/issues/15804
[23] easyeffects issue #4920 — 48 kHz resampling regression — https://github.com/wwmm/easyeffects/issues/4920
[24] PipeWire docs — pipewire.conf man page — https://docs.pipewire.org/page_man_pipewire_conf_5.html
