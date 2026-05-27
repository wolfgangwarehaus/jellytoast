# Bit-perfect playback in jellytoast

A practical guide. If you don't care about resampling artifacts, ignore this
page — the defaults are fine for everyone else.

For the deep audit of jellytoast's audio path against Audirvana / Roon /
foobar2000 / Symfonium, see `docs/research/bit_perfect_playback.md`.

## What "bit-perfect" means here

The DAC sees the same PCM samples the file contains. No resampling, no dither,
no float-multiply gain stages, no mixer-side conversion between decode and the
hardware.

It's not a quality claim — it's a *fidelity* claim. A 320 kbps MP3 played
bit-perfect is still a 320 kbps MP3; bit-perfect just means no extra processing
is being layered on top.

## What defeats it

Five things, in roughly the order people trip over them:

1. **Server-side transcode.** Anything other than `Original` in Settings →
   Playback → Quality forces Jellyfin to re-encode to MP3 server-side. Bit-
   perfect is over before the bytes leave the server.
2. **Sample-rate mismatch with the audio session.** PipeWire defaults to a
   fixed 48 kHz session rate. A 44.1 kHz CD-source file (which is most music)
   gets resampled. This is the biggest invisible quality loss on Linux —
   see §[PipeWire setup](#pipewire-setup-linux) below.
3. **Software volume below 100 %.** mpv attenuates by multiplying the float
   samples. Not bit-identical. (Universal across software players — the only
   alternative is hardware-only volume on the DAC, which loses resolution at
   low levels.)
4. **ReplayGain / Normalization on.** Applies a per-track gain filter. Bit-
   perfect is off the moment it's enabled.
5. **EQ on, or Crossfade running.** Both are DSP filters in mpv's chain;
   neither is bit-perfect by construction. (The Playback settings page
   surfaces each with its own toggle so you can tell at a glance whether
   bit-perfect is intact.)

## The bit-perfect contract — quick checklist

The fast path: tick **Bit-perfect mode** at the top of Settings → Playback.
That single toggle locks the application-layer contract — Normalization, EQ,
and Crossfade are force-disabled while it's on; the volume slider in the
now-playing bar locks at 100 % with a tooltip explaining why; the streaming-
info pill above the play button gains a "Lossless · " prefix when the source
is being served direct.

Manually, the same contract is:

- [ ] **Quality** = `Original` (DirectStream from server, no transcode)
- [ ] **Normalization** = `Off`
- [ ] **Crossfade** = `Off`
- [ ] **Equalizer Enable** = `Off`
- [ ] **Volume** = `100 %` (top-bar slider, fully right)

That's the application-layer contract. The OS layer is the next section.

## PipeWire setup (Linux)

This is the high-value step. Without it, every 44.1 kHz file (most CD-sourced
music) gets resampled to 48 kHz by PipeWire — silently.

### The fast path

Open Settings → Playback. Under the BIT-PERFECT section there's an
"Install PipeWire bit-perfect config" button — click it. It drops the same
file described below into `~/.config/pipewire/pipewire.conf.d/`. Idempotent
(safe to click twice), reversible ("Remove" appears once installed), and the
file carries a comment header so a future uninstall won't touch a file you
wrote by hand at the same path.

After install, restart the audio stack (see "Then restart" below) and you're
done.

### The manual path

If you'd rather edit the file yourself, drop the following snippet into
`~/.config/pipewire/pipewire.conf.d/10-jellytoast-bitperfect.conf`
(create the directory if it doesn't exist):

```
context.properties = {
    default.clock.allowed-rates = [ 44100 48000 88200 96000 176400 192000 ]
}

stream.properties = {
    resample.quality = 14
}
```

Then restart the audio stack:

```
systemctl --user restart pipewire pipewire-pulse wireplumber
```

What it does:

- **`default.clock.allowed-rates`** lets PipeWire switch its session rate to
  match the playing stream. With this set, a 44.1 kHz file plays at 44.1 kHz;
  a 96 kHz file plays at 96 kHz. Without it, everything gets resampled to
  48 kHz.
- **`resample.quality = 14`** is near-sinc, low ringing. Default is `4`
  (linear-ish). When a resample *is* forced (e.g. you have a 96 kHz file but
  your DAC tops out at 48), this is what runs. Costs a few percent of one CPU
  core during playback; inaudible to most listeners but bit-perfect-adjacent.

### The "first stream wins" gotcha

PipeWire only switches the session rate **when the sink is idle**. If anything
else is currently producing to it — a paused browser tab, EasyEffects, `cava`,
a Discord call on hold — the first rate wins and your next stream gets
resampled until everything releases the sink.

In practice: if you're chasing bit-perfect for a specific listen, close the
browser tab playing silent audio. Kill EasyEffects if you don't need it. Then
start jellytoast.

### Verifying it worked

Pick a 44.1 kHz source file (most music). With jellytoast playing it, in a
terminal:

```
pw-top
```

Look at the `RATE` column for the jellytoast stream. If it reads `44100`,
PipeWire followed the source rate. If it reads `48000`, something corked the
sink first (see above), or the conf didn't get loaded (rerun the restart
command above).

## Volume — the unavoidable corner

The top-bar slider attenuates in software. The only way to keep bit-perfect
**and** control loudness is hardware volume on the DAC itself (a knob, a
remote, or `wpctl set-volume @DEFAULT_AUDIO_SINK@`). Software volume below
100 % is universal across players — Audirvana, Roon, foobar2000 all break
bit-perfect the moment you move the slider off the top.

If you have a DAC with a hardware volume control, leave the slider at 100 and
use the knob.

## What jellytoast does correctly already

For the curious — the mpv configuration in `modules/player_backend.py`
(`_make_mpv_handle`) is audited against the audiophile-tier player setups and
doesn't cut corners:

- `replaygain="no"` by default (no normalisation unless you opt in)
- `replaygain_clip="no"` (no peak-clipping gain)
- `audio_quality="original"` → `/Audio/{id}/stream?static=true` →
  DirectStream from Jellyfin (verbatim bytes, no server transcode)
- `gapless_audio="weak"` — gapless without aligning samples across track
  boundaries
- `audio-format`, `audio-samplerate`, `audio-channels` deliberately **unset** —
  this lets mpv negotiate the native source rate/format with the audio output
  rather than locking one in upstream

So the application-layer path is already audiophile-correct. The loss
points are downstream — PipeWire session rate, software volume, anything
else corking the sink.

## Exclusive output (Linux + Windows + macOS)

Settings → Playback → Bit-perfect mode → **Exclusive output** takes the DAC
over and bypasses the OS mixer entirely. Same trick Audirvana and Roon use.

- **Linux/PipeWire** — corks every other stream on the active sink for the
  duration of playback. Notifications, browser audio, video calls all go
  silent until jellytoast releases the device. Standard audiophile behaviour.
- **Windows** — opens the DAC in WASAPI Exclusive mode. Other apps go silent.
  Some DACs (a few internal Realtek codecs in particular — mpv issues
  [#11600](https://github.com/mpv-player/mpv/issues/11600) and
  [#11733](https://github.com/mpv-player/mpv/issues/11733)) refuse exclusive
  open; jellytoast catches the failure and falls back to shared mode so the
  app still launches. You'll see a warning in the logs but playback works.
- **macOS** — sets `kAudioDevicePropertyHogMode` on the active output. macOS
  refuses HogMode on internal speakers/headphone outputs and will kill the
  process; only USB / external DACs work. The toggle exists on macOS but
  needs USB-DAC detection before it's safe to expose (T3 macOS path is
  hardware-blocked until a Mac is available for testing).

UX cost is real on every platform — other apps go silent during playback,
system sounds die. Don't enable it for casual listening. The toggle is
intentionally a sub-option of Bit-perfect mode so it can't be activated by
accident.

The setting takes effect on the next track open (mpv opens its audio output
per `play()`), not at the moment you click the checkbox.

## Where to dig deeper

- `docs/research/bit_perfect_playback.md` — the full audit, including the mpv
  config breakdown, PipeWire interaction details, cross-platform comparison,
  Symfonium / Roon / Audirvana / foobar2000 benchmark, and the T0–T4 roadmap.
- PipeWire's own `pipewire-props(7)` man page is the authoritative reference
  for every key in the conf snippet above.
- For DAC-specific tuning (especially USB DACs that advertise capabilities
  they don't actually support cleanly), the Head-Fi PipeWire thread linked
  in the research doc has the most accumulated empirical wisdom.
