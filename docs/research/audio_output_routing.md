# Audio output routing — Pulse / PipeWire / ALSA-direct

> **📍 Status — 2026-06-10:** Shipped same-day (Settings → Playback →
> Audio output, `feat/audio-output-device`). This doc carries the
> rationale; see `docs/SPEC.md` §2 and `docs/bit_perfect.md` for
> as-built behavior.

## 1. Goal

Let the user pin mpv's audio output to a specific device — most
importantly a **raw ALSA `hw:` device**, the Linux audiophile direct
path that bypasses PipeWire (and its session clock) entirely. Secondary
win: the same picker exposes PipeWire/Pulse sinks and Windows WASAPI
endpoints, so "play through the USB DAC, not the laptop speakers" stops
requiring system-settings spelunking on every platform.

## 2. The Linux pipe landscape (why this design)

| Pipe | Reality | Our handling |
| --- | --- | --- |
| **PipeWire** | The default on every modern distro (CachyOS, Arch, Fedora, Ubuntu 22.10+). Owns one session clock; apps cannot bypass it from inside. | First-class. The bit-perfect conf installer (`jellytoast/pipewire_setup.py`) widens `default.clock.allowed-rates` so the graph follows the stream rate. |
| **PulseAudio** | Legacy LTS distros only. | Free: mpv's `pulse` output speaks to real Pulse and `pipewire-pulse` identically; pulse sinks appear in the picker via mpv's enumeration. No jellytoast code is Pulse-specific. |
| **ALSA (direct `hw:`)** | Not a server — the kernel layer. Opening `hw:` directly is exclusive, bit-exact, and bypasses every mixer. The Audirvana/Roon-bar route. | The picker's raison d'être. Guardrails below. |
| JACK | Pro-audio; absorbed by PipeWire. | Out of scope. |

## 3. Design

- **One setting**: `playback/audio_output_device` (default `"auto"`),
  holding an mpv `audio-device-list` name verbatim. mpv's enumeration
  is the single source of device truth — no per-backend probing code
  in jellytoast, and the same picker works on Windows (WASAPI
  endpoints) and macOS for free.
- **Apply semantics**: mpv reads `audio-device` when it (re)opens the
  AO — i.e. the next track. The Settings handler persists + emits
  `audio_output_device_changed`; `MpvController.set_audio_output_device`
  pushes to the live handle AND the crossfade sibling;
  `_make_mpv_handle` reads the setting for future constructions. Same
  three-layer pattern as `audio_exclusive`.
- **Layered open fallback** (constructor must never leave the app
  silent): pinned device fails (unplugged DAC, stale name) → retry on
  auto; exclusive refused → retry shared. Logs each shed layer;
  *never* rewrites the persisted setting — the device may be back next
  launch, and the picker shows it as "(not connected)".

## 4. ALSA-direct guardrails

1. **Crossfade**: a fade needs BOTH mpv handles playing through the
   overlap; `hw:` allows one open. `_ensure_crossfader` returns None
   while an `alsa/` device is pinned — those users route through the
   ordinary gapless path. (Bit-perfect mode already disables crossfade,
   and ALSA-direct users are bit-perfect users almost by definition, so
   this gate rarely bites alone.)
2. **Visualizer**: the FFT taps the PipeWire default-sink monitor
   (`jellytoast/visualizer.py` MonitorAudioTap); a direct `hw:` stream
   never crosses PipeWire, so there is nothing to tap. The visualizer
   paints an explanatory caption ("direct ALSA output bypasses the
   audio tap") instead of an eternal "waiting for audio signal".
3. **Settings hint**: picking an `alsa/` device reveals a caption
   spelling out the trade (other apps silenced, crossfade bypassed,
   visualizer dark). The trade is the point — but it should never be a
   surprise.

## 5. Rejected alternatives

- **Exposing mpv `--ao` (backend choice) instead of devices** — users
  think in devices ("the DAC"), not output drivers; and `--audio-device`
  names imply the ao anyway (`alsa/…` selects the alsa ao).
- **Auto-switching to ALSA when bit-perfect is on** — too magical;
  silencing every other app on the machine must be an explicit pick.
- **Per-backend device probing in jellytoast** — mpv already maintains
  exactly this list, cross-platform, with human descriptions.

## 6. Verification

- `tests/test_audio_output_device.py` — setting contract, factory
  kwarg + layered fallback, runtime push to both handles, enumeration
  parsing, ALSA crossfade guardrail, visualizer caption state.
- Manual (see `docs/manual_test_plan.md`): pick a `pipewire/` sink →
  audio moves on next track; pick `alsa/hw:` → playback continues,
  other apps silenced, visualizer caption appears, crossfade falls
  back to gapless; unplug the pinned DAC → next launch logs the
  fallback and plays on auto.
