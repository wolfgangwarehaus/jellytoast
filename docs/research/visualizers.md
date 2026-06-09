# Audio Visualizers — Design Doc

> **📍 Status — 2026-05-20:** Shipped. The visualizer FFT backend
> landed 2026-05-17 and the paint widget 2026-05-19. Per-OS audio
> taps beyond Linux are still open (P4). Kept for rationale — see
> `CHANGELOG.md`.

Original proposal (2026-05-15, pre-build — see the Shipped banner above).

Companion to `docs/competitive_audit.md` (which calls visualizers "pure eye
candy but reviewers fixate. Do after EQ.") and `docs/SPEC.md`.

**Recommended v1 combo (one-liner):** tap mpv via `--lavfi-complex` running
`asplit` into a fixed-size `aresample` + `aphasemeter`/`showspectrum`-style
chain that publishes PCM frames via a libmpv property/socket, FFT in numpy on
a worker `QThread`, and render with `QPainter` on a `QWidget` inside the
now-playing left pane — i.e. **Source A + Render A** for v1, with a clear
upgrade path to Source B (OS-native loopback) + Render B (`QOpenGLWidget`)
once the surface earns its keep.

---

## 1. Goal & non-goals

**Goal.** Replace the now-playing cover (third left-pane mode after
`cover` / `lyrics`) with a live audio visualizer that reacts in real time
to whatever mpv is decoding. v1: spectrum bars and oscilloscope. v2+:
ProjectM (Milkdrop) optional.

**Non-goals.**

- Mini-player visualizer (too small to be useful).
- Lockscreen / Plasma media widget integration.
- Cast-device-side visualizer (Chromecast/AirPlay can't surface PCM back).
- Video playback. jellytoast is and stays music-only
  (`memory/feedback_music_only_focus.md`).
- Bringing `QWebEngineView` back to host butterchurn — explicitly counter
  to `memory/project_native_ui_pivot.md`.

Reviewers fixate on visualizers (Feishin shipped one in v1.2, Nokkvi,
Nocturne, and Subwave lean hard on Milkdrop), so the surface earns its keep
even if it's eye candy. Order remains: EQ first, visualizer after.

---

## 2. Audio-source options

### A. mpv `--lavfi-complex` filter tap (RECOMMENDED for v1)

mpv exposes an `--lavfi-complex` graph where `asplit` lets us fan the
decoded audio out to (a) the audio output and (b) a measurement chain. We
do **not** need ffmpeg's `showspectrum` filter to render — we want raw PCM
samples to FFT ourselves. The cleanest tap is:

```
[aid1]asplit[ao][tap]; [tap]aresample=44100,asetnsamples=n=1024:p=0,asendcmd=...
```

The community precedent (mfcc64/mpv-scripts `visualizer.lua`,
fraggod.net 2018 walkthrough) feeds the tap into `showcqt`/`avectorscope`
and uses mpv's *own* video output to display it. We won't do that — we
want the samples in Python, not a video pane. Two viable variants:

1. **`libmpv` audio property polling.** mpv exposes `audio-pts`,
   `audio-params`, and (newer) frame-level callbacks. python-mpv's
   `property_observer` gives us the metadata but not raw PCM frames.
2. **Companion ffmpeg subprocess.** Run a second short-lived ffmpeg that
   decodes the *same* URL/file to `f32le` PCM on stdout, sync'd by
   `time-pos`. Functional but doubles network/disk I/O and drifts.

The pragmatic v1 is (1) augmented by a small **audio-tap script** loaded
via `--script=` that uses mpv's Lua `mp.get_property` + a libmpv pipe to
ship PCM. Acceptable cost: per-frame IPC, ~1ms latency, no extra decode.

Quality: real audio, exact sync. Cost: per-OS-build mpv has to be compiled
with `--enable-libavfilter` (it is, on every distro we care about).
Caveat: `lavfi-complex` *can* break gapless prefetch
(`prefetch_playlist=yes`, see `player_backend.py:185`); we need to verify.

### B. OS loopback capture (PulseAudio/PipeWire monitor, WASAPI, CoreAudio Tap)

Read mpv's audio output back from the OS instead of tapping its filter
graph. Per-platform plumbing:

- **Linux:** every PulseAudio/PipeWire sink has a `.monitor` source that
  emits exactly what was played. `pasimple`, `sounddevice` (PortAudio
  with PA backend), `SoundCard`, or raw `pw-record` all work. PipeWire
  also lets us *target the jellytoast process node specifically* —
  cleaner than mixing in system sounds.
- **Windows:** WASAPI loopback. `PyAudioWPatch` (PortAudio fork) or
  `soundcard` both expose it. `ProcTap` does the modern per-process
  variant.
- **macOS 14.4+:** the new `CATapDescription` Core Audio API
  (`AudioHardwareCreateProcessTap` + aggregate device) — Apple's
  blessed alternative to BlackHole/Soundflower. Apple's own docs are
  thin; `insidegui/AudioCap` and `makeusabrew/audiotee` are the
  reference implementations. Requires `NSAudioCaptureUsageDescription`
  in Info.plist and a one-time user permission prompt.
- **macOS <14.4:** virtual cable (BlackHole) required, user-installed.
  Acceptable graceful-degrade.
- **iOS:** `AVAudioEngine` + an output tap. Different code path; out of
  scope until iOS exists at all (per
  `memory/user_hardware.md`).

Quality: real audio, perfect sync. Cost: **four distinct backends.** This
is exactly the multi-platform pain `memory/architecture_cross_platform.md`
warns against and exactly the pattern the project already uses for
`autostart/`, `media_controls/`, and `keep_above/` — so the cost is known.

### C. mpv property polling

`audio-bitrate`, `audio-params`, `audio-pts` exist; raw waveform does not.
Insufficient for spectrum/oscilloscope. Useful as an "is something
playing?" liveness signal only.

### D. Server-side pre-computed waveform peaks

Plex precomputes peaks for scrubber waveforms. Jellyfin / Subsonic /
Navidrome do not, and even if they did this is static-per-track scrubber
data, not real-time visualization. Useless for our goal.

### E. Parallel in-app decode (PyAV)

Decode the same URL a second time inside jellytoast using PyAV, FFT on the
decoded frames, sync via mpv's `time-pos`. Pros: zero OS coupling — same
code on every platform. Cons: doubles bandwidth/CPU, drift if mpv pauses
or seeks (we have to re-pump), and PyAV adds ~25MB to Flatpak.

**Recommendation.** v1 = **A** (mpv `lavfi-complex` tap, samples shipped
into Python). v2 if A's gapless interaction turns ugly = fall back to
**B** with the per-OS backend pattern. **E** stays as the
"works-everywhere safety net" only if both A and B prove painful on Windows
or macOS.

---

## 3. Render options

### A. QPainter on a plain QWidget (RECOMMENDED for v1)

`paintEvent` + `QPainter.drawRect` per band, fixed-rate `QTimer` at 60Hz.
Spectrum bars (30-60 bands), oscilloscope, and VU meters are all trivial
at 60fps on a CPU — `markjay4k/Audio-Spectrum-Analyzer-in-Python` and
swharden's PyQt monitor confirm.

Gotchas already in our memory:
`memory/feedback_qgraphicseffect_scroll.md` says detach
`QGraphicsOpacityEffect` after fading — DO NOT layer one on the
visualizer surface for "glow."
`memory/architecture_main_window_chrome.md` is irrelevant here (we're
inside a child widget, not the top-level).

### B. QOpenGLWidget

Required for: ProjectM, particle effects, large-band spectrograms,
shader-style trippy modes. GPU does the work, freeing CPU for FFT.
Wayland + KWin compose Qt GL surfaces fine.

Cost: OpenGL is deprecated on macOS (Apple wants Metal). Qt still ships
an OpenGL-over-Metal shim, but it's a fragility surface. Defer until v2+
when we want ProjectM anyway.

### C. ProjectM (libprojectM, MilkDrop preset format)

Industry standard. v4 release is on Flathub
(`net.sourceforge.projectM`); upstream "Python bindings" (`walshbp/pym`)
exist but are early. libprojectM expects raw PCM via `projectm_pcm_add_*`
and renders OpenGL into a context we provide. Means: we own the GL
context (a `QOpenGLWidget`), libprojectM owns the draw call, presets are
shipped as a data dir.

Flatpak cost: libprojectM + the preset packs (~100MB of `.milk` files) +
GL extension surface. Doable but non-trivial; community presets are
mixed-licensed and need vetting.

### D. butterchurn inside `QWebEngineView`

**Rejected.** `memory/project_native_ui_pivot.md` says the WebEngine
dependency is gone. Reintroducing 200MB of Chromium just for a visualizer
is a regression. Feishin gets away with it because it's already Electron.
We are not.

**Recommendation.** v1 = **A** (`QPainter`). v2 = **B**
(`QOpenGLWidget` for smoothed bars + spectrogram waterfall). v3 = **C**
(ProjectM, deferred behind a "Trippy mode" opt-in and Flatpak
extension).

---

## 4. Visualizations to ship (tiered)

### Tier 1 — v1 ship list (the bar to clear "competitive")

- **Spectrum bars.** 32-64 logarithmic-band FFT, smooth decay (peak-hold
  ~500ms), accent-colored. Drop-dead simple, the thing 90% of users
  picture when they hear "visualizer."
- **Oscilloscope.** Time-domain waveform, single accent line, optional
  mirror. Tiny CPU, distinctive look.

### Tier 2 — v2

- **VU meters.** Peak + RMS bars, dB scale. Audiophile crowd loves this.
- **Spectrogram waterfall.** 2D heatmap scrolling left, FFT bins on Y.
  PyQtGraph's `ImageItem` precedent is great here.
- **Polar/radial spectrum.** Spectrum bars wrapped around the cover.
  Composable with the cover staying visible.

### Tier 3 — opt-in only

- **ProjectM Milkdrop presets.** "Random preset on track change" toggle.
  Lives behind a Flatpak extension or an OS-package recommendation;
  jellytoast core doesn't hard-depend on libprojectM.

---

## 5. UI surface

### Where it lives

- **Now-playing left pane** is today either cover-only or
  cover-plus-lyrics. Today's code uses `self._show_lyrics: bool` in
  `modules/now_playing_page.py:1750` — that grows into a tri-state.
  Introduce `np_left_pane_mode` setting with values
  `cover | lyrics | visualizer`. Migrate
  `_show_lyrics` callers in one pass.
- **Top-bar / now-playing-bar toggle.** A small "viz" chip next to the
  existing "lyrics" toggle, cycles `cover → lyrics → visualizer →
  cover`.
- **Mini player.** Skip. The 240×96 surface (per
  `memory/feedback_cast_mini_player_differentiators.md`) is too small.
- **Cast active.** See §9.

### Per-visualization settings

- Color: accent (default) / album-art-dominant / custom hex.
- Sensitivity: gain knob -12dB…+24dB.
- Smoothing: decay 50ms–2s.
- Bar count (spectrum only): 16/32/64/128.
- "Random preset on track change" (ProjectM only).

All settings live under `visualizer/<type>/...` keys in `Settings`, hot
applied via `PlayerBus` (see §7).

---

## 6. Multi-platform audio-tap (load-bearing)

If we go down Source B (loopback) we end up with four backends. Honest
inventory:

| OS | Backend | Library | Notes |
|---|---|---|---|
| Linux (PipeWire) | `pw-stream` against `<process>.monitor` | `sounddevice`, `pasimple`, or `pipewire_python` | First-class, no perms prompt. The path we'd ship by default on every modern Wayland distro. |
| Linux (legacy Pulse) | `<sink>.monitor` source | same libs | Works the same API — Pulse compatibility layer just translates. |
| Windows 10+ | WASAPI loopback (per-process WASAPI 11) | `PyAudioWPatch` or `ProcTap` | `ProcTap` is the cleanest per-process API but young. |
| macOS 14.4+ | `CATapDescription` + aggregate device | `insidegui/AudioCap` reference (Swift); Python bridge via `ctypes` or a small Obj-C bundle | Requires `NSAudioCaptureUsageDescription` plus the runtime permission. Apple's docs are notoriously sparse — read AudioCap source first. |
| macOS <14.4 | BlackHole virtual device | n/a | User-installed system extension. Document and fall through. |
| iOS | `AVAudioEngine.installTap(onBus:)` | swift/obj-c only | Out of scope until iOS app exists. |

This is exactly the pattern jellytoast already uses for `autostart/`,
`media_controls/`, and `keep_above/` — a `modules/audio_tap/` package
with `_linux_pipewire.py`, `_windows_wasapi.py`, `_macos_catap.py`, and a
public `get_audio_tap()` dispatcher via `platform_compat.py`. If/when we
need Source B, this is the path that keeps the code clean.

The fact that Source A (mpv lavfi tap) **sidesteps all of that** is what
makes it the right v1 choice — one path, four-OS support, free.

---

## 7. Performance + threading

- **Target 60fps draw, 60Hz FFT.** 1024-2048 sample window on 44.1/48kHz
  → 23–47ms of audio per frame.
- **`np.fft.rfft`** on 2048 floats is sub-millisecond on every laptop CPU
  jellytoast targets. No GPU offload needed for Tier 1/2.
- **Threading.** Use `modules.async_io` patterns
  (`memory/feedback_async_io_pattern.md`) — *never* raw `threading.Thread`.
  Specifically: a `QObject` audio-tap worker on a dedicated `QThread`,
  reads samples, computes FFT, emits a `Signal(np.ndarray)` to the
  paint widget on the GUI thread. Coalesce to 60 Hz with a leading-edge
  throttle so the paint thread never gets behind.
- **Hidden/minimized window:** suspend the timer. Visibility tracked via
  `QWidget.isVisible()` + `QEvent.WindowStateChange`. Save ~3-5% CPU on
  idle.
- **Battery mode:** clamp to 30fps when on battery (read from
  `org.freedesktop.UPower` on Linux, `IOPSCopyPowerSourcesInfo` on
  macOS, `GetSystemPowerStatus` on Windows). Already a TODO for
  cover-art refresh timers; same plumbing.
- **DPR/HiDPI:** per
  `memory/architecture_hidpi_scaling.md`, draw with `setPixelSize` and
  let Qt's PassThrough rounding handle Wayland fractional scale. Don't
  cache scaled bitmaps for the visualizer — it repaints every frame.
- **Multi-monitor refresh:** if the user has a 60Hz and a 144Hz
  display, throttle to the screen the now-playing page is on via
  `window().screen().refreshRate()`. Don't paint at 144Hz if the user
  drags the window to the 60Hz panel.

---

## 8. Settings

New `Settings` keys (under `jellytoast` QSettings org):

```
np_left_pane_mode               cover | lyrics | visualizer
visualizer/active_type          spectrum | scope | vu | spectrogram | projectm
visualizer/spectrum/bands       32 | 64 | 128
visualizer/spectrum/smoothing   ms (50-2000)
visualizer/spectrum/sensitivity dB (-12 to +24)
visualizer/color_mode           accent | dominant | custom
visualizer/custom_color         #RRGGBB
visualizer/random_preset        bool
visualizer/fps_cap_battery      int (default 30)
```

Live-apply contract per `memory/architecture_live_accent.md`:
`PlayerBus.theme_changed` already fans accent changes — extend with
`PlayerBus.visualizer_settings_changed`. The widget's `_reapply_accent`
gets an analogous `_reapply_visualizer_settings`. **Important per
`memory/feedback_signal_connects_in_init.md`: signal `.connect()` calls
go in `__init__`, not in the reapply method.**

---

## 9. Edge cases (especially cast)

- **Paused / stopped.** Visualizer fades to idle (slowly decaying flat
  line / empty bars). Don't freeze the last frame — looks broken. Don't
  blank — looks like a bug.
- **Cast active.** This is the load-bearing one. Per
  `memory/architecture_cast_proxy.md` and `player_backend.py`'s cast
  routing, when we cast, **mpv stops decoding locally**. There are two
  options:
  1. **Pause the visualizer**, show a "Casting to <device>" placeholder.
     Honest and free.
  2. **Run a silent local decode** for visualization only — keep mpv
     decoding to the null sink (`ao=null`) while cast handles
     playback. Costs CPU + bandwidth (decoding twice) but keeps the
     visualizer alive.
  Recommendation: ship (1) for v1, evaluate (2) once a user complains.
- **Cast volume controls (group casts).** Already wired via
  `PlayerBus`; the visualizer is purely receive-side, no interaction.
- **No track loaded.** Hide the visualizer mode entirely or show the
  cover-art empty state; the third-mode toggle becomes a no-op until
  playback starts.
- **Offline / downloaded local file.** Works identically — mpv decodes
  the local file the same way. Source A is decode-side, not network-
  side, so connectivity changes (see
  `memory/architecture_offline_phase5.md`) are a non-event.
- **AirPlay 2 receiver.** Same as Cast: mpv isn't decoding locally. See
  `memory/reference_airplay2_pyatv_compat.md` for the routing path.
- **Replaygain on.** Source A taps *post-decode*, so the visualizer
  reflects the gain-adjusted signal — which is what users see/feel,
  so this is correct.
- **EQ on (future).** Once EQ ships, decide whether the visualizer taps
  pre- or post-EQ. Post-EQ is what reaches the speakers and is "more
  honest." Probably ship post-EQ as default with a debug toggle.

---

## 10. Effort + sequencing

| Phase | Scope | Effort | Risk |
|---|---|---|---|
| v1 | mpv lavfi tap + spectrum bars + oscilloscope, QPainter, now-playing third pane mode | **S** | Low — all CPU, no OS plumbing, mpv tap is well-trodden |
| v1.5 | Color modes (accent / dominant), per-viz settings, battery throttling | **S** | Low |
| v2 | Move to QOpenGLWidget, add VU meters + spectrogram waterfall | **M** | Med — GL state, multi-monitor, Wayland GL gotchas |
| v3 | OS loopback backends (Linux PW first, then Win, then macOS), gated by Source-A pain | **M** per backend | Med-High — `CATapDescription` permission UX is fragile |
| v4 | ProjectM behind Flatpak extension, preset pack, "Trippy mode" toggle | **L** | High — packaging, GL fragility, preset license review |

Order EQ → v1 visualizer → v1.5 → v2 → v3 → v4. Don't start v3 unless
Source A breaks something on Windows or macOS in real-world use.

**v1 done criteria:**

1. Now-playing left pane has a third mode `visualizer` next to
   `cover` / `lyrics`.
2. Spectrum bars + oscilloscope ship.
3. Decay/sensitivity/bar-count settings live-apply via `PlayerBus`.
4. Visualizer auto-pauses when window is hidden, throttles to 30fps on
   battery.
5. Cast active shows a placeholder, not a frozen frame.
6. Works on Linux Wayland (jellytoast's only tested-today surface);
   Windows + macOS smoke-tested once hardware exists per
   `memory/user_hardware.md`.

---

## 11. Sources

- [mpv `af.rst` filter docs](https://github.com/mpv-player/mpv/blob/master/DOCS/man/af.rst)
- [mpv stable manual](https://mpv.io/manual/stable/)
- [mpv JSON IPC protocol](https://mpv-player-mpv.mintlify.app/scripting/ipc-protocol)
- [fraggod.net — mpv audio visualization with `lavfi-complex`](https://blog.fraggod.net/2018/04/12/mpv-audio-visualization.html)
- [mfcc64/mpv-scripts `visualizer.lua`](https://github.com/mfcc64/mpv-scripts/blob/master/visualizer.lua)
- [Securitron Linux — mpv waveform display](https://www.securitronlinux.com/debian-testing/how-to-get-a-nice-waveform-display-with-the-mpv-media-player-on-linux/)
- [python-mpv (jaseg)](https://github.com/jaseg/python-mpv)
- [PipeWire loopback module docs](https://docs.pipewire.org/page_module_loopback.html)
- [PipeWire audio-capture example](https://docs.pipewire.org/audio-capture_8c-example.html)
- [`pipewire_python` on PyPI](https://pypi.org/project/pipewire_python/)
- [`pasimple` on PyPI](https://pypi.org/project/pasimple/)
- [`SoundCard` cross-platform Python](https://github.com/bastibe/SoundCard)
- [PyAudioWPatch — WASAPI loopback](https://github.com/s0d3s/PyAudioWPatch)
- [Microsoft — WASAPI loopback recording](https://learn.microsoft.com/en-us/windows/win32/coreaudio/loopback-recording)
- [ProcTap — per-process audio capture](https://github.com/m96-chan/ProcTap)
- [Apple — Capturing system audio with Core Audio taps](https://developer.apple.com/documentation/CoreAudio/capturing-system-audio-with-core-audio-taps)
- [insidegui/AudioCap — `CATapDescription` reference](https://github.com/insidegui/AudioCap)
- [makeusabrew/audiotee](https://github.com/makeusabrew/audiotee)
- [AudioTee write-up — Strongly Typed](https://stronglytyped.uk/articles/audiotee-capture-system-audio-output-macos)
- [`directmusic` gist — CATap example](https://gist.github.com/directmusic/7d653806c24fe5bb8166d12a9f4422de)
- [projectM on GitHub](https://github.com/projectM-visualizer/projectm)
- [projectM v4.0.0 release notes](https://github.com/projectM-visualizer/projectm/releases/tag/v4.0.0)
- [projectM on Flathub](https://flathub.org/en/apps/net.sourceforge.projectM)
- [walshbp/pym — projectM Python bindings](https://github.com/walshbp/pym)
- [butterchurn (WebGL MilkDrop)](https://github.com/jberg/butterchurn)
- [Feishin — `jeffvli/feishin`](https://github.com/jeffvli/feishin)
- [Feishin v1.2.0 release (butterchurn shipped)](https://github.com/jeffvli/feishin/releases/tag/v1.2.0)
- [Feishin issue #1546 — fullscreen visualizer](https://github.com/jeffvli/feishin/issues/1546)
- [Feishin issue #1355 — visualizer enhancement](https://github.com/jeffvli/feishin/issues/1355)
- [Realtime PyAudio FFT — `aiXander`](https://github.com/aiXander/Realtime_PyAudio_FFT)
- [markjay4k Audio Spectrum Analyzer in Python](https://github.com/markjay4k/Audio-Spectrum-Analyzer-in-Python)
- [swharden — Python real-time audio frequency monitor](https://swharden.com/blog/2016-07-31-real-time-audio-monitor-with-pyqt/)
- [Frolian's blog — PyQt microphone FFT](https://flothesof.github.io/pyqt-microphone-fft-application.html)
- [PySDR — real-time PyQt GUIs](https://pysdr.org/content/pyqt.html)
