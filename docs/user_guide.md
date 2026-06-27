# jellytoast user guide

The long-form companion to the README: every surface, shortcut, and
troubleshooting path. Content here moved out of the front-page README on
2026-06-12 (packaging day) — it's the same material, maintained in one place.

## Keyboard shortcuts

System-wide media keys (Play/Pause/Next/Prev) work via MPRIS2.

| Key            | Action          |
| -------------- | --------------- |
| `Space`        | Play / Pause    |
| `Ctrl+Shift+L` | Open all music  |
| `Ctrl+F` / `/` | Focus search    |
| `Ctrl+Q`       | Quit            |

## Mini player

- **Compact** — narrow bar: cover, title marquee, transport, click-to-seek progress.
- **Expanded** — large square cover above the same bar.

Frameless, draggable, KDE blur on Plasma. On KDE Wayland, "always on top" is
delivered via a KWin window rule (xdg-shell forbids apps setting their own
stacking).

## Tray

- **Left-click** → toggle mini player
- **Middle-click** → play / pause
- **Double-click** → open main window
- **Right-click** → menu

Closing the window minimizes to tray. **Quit** from the tray menu or `Ctrl+Q` to exit.

## Casting

Per-protocol toggles + on-demand discovery in Settings → Casting:

- **Chromecast** — Default Media Receiver, direct-play for browser-supported codecs (MP3 / AAC / FLAC), HTTP audio fallback for everything else.
- **AirPlay 2** — pyatv-based, supports modern Apple TVs and AirPlay 2 speakers (Linux/macOS; broken upstream on Windows).
- **DLNA / UPnP-AV** — SSDP discovery + AVTransport push, 714/701 transcode-retry decision tree, mandatory upnp:class for spec-finicky renderers. Off by default.
- **Sonos** — native SoCo-based zone discovery + group transport. Off by default.

The **cast proxy** (`jellytoast/cast_proxy.py`) relays streams to receivers that
can't reach the server directly — Tailscale, remote, self-signed certs — and
serves downloaded blobs off disk so cast works fully offline. Toggle via
`cast_stream_routing` in Settings.

When casting starts, local mpv stops; on disconnect, the local stream resumes
at the cast position.

## Settings

- **General** — autostart at login (Linux), home destination, minimize-to-tray, mini-player keep-above.
- **Account** — server URL, username, sign in / sign out, switch backend.
- **Playback** — ReplayGain mode, smart shuffle, audio output device, bit-perfect options.
- **Library** — page size, cover prefetch, tile fade animation.
- **Lyrics** — font size.
- **Casting** — per-protocol toggles, discovery timing (startup vs on-demand), cast-stream routing.
- **Downloads** — download root, offline-mode toggle.
- **Appearance** — theme (dark / light), accent color, blur opacity.
- **Scrobbling** — ListenBrainz account hookup.

Window geometry, sort order, view mode (grid / list), shuffle / repeat all
persist automatically.

## Bit-perfect playback

The fast path: tick **Bit-perfect mode** at the top of Settings → Playback.
That one toggle locks the application-layer contract — Quality = Original
(no transcode), Normalization / EQ / Crossfade force-disabled, volume locked
at 100% with a tooltip explaining why — and the streaming-info pill gains a
"Lossless · " prefix when the source is served direct.

**PipeWire (Linux), the high-value step:** without a rate-following config,
every 44.1 kHz file gets silently resampled to 48 kHz. Settings → Playback →
"Install PipeWire bit-perfect config" drops the conf into
`~/.config/pipewire/pipewire.conf.d/` (idempotent, reversible — "Remove"
appears once installed); restart the audio stack when prompted.

**Verify:** play a 44.1 kHz file and run `pw-top` — the jellytoast stream's
`RATE` column should read `44100`. If it reads `48000`, another stream
corked the sink first (PipeWire's "first stream wins") or the conf didn't
load.

**The maximal route (Linux):** pick an `alsa/…` device in the output picker
for ALSA-direct output — bypasses PipeWire entirely, claims the device
exclusively (other audio won't play), and the purple legend in Settings
spells out the consequences. On Windows/macOS the equivalent is the
Exclusive output checkbox (WASAPI / CoreAudio).

The full deep-dive (mpv flags, DSD, the volume corner, troubleshooting) is
preserved in git history as `docs/bit_perfect.md`.

## Themes & blur

jellytoast ships two dark + two light themes. **Frosted** is the flagship —
a translucent "glass" body that rides the compositor's blur-behind. Whether real
blur is available depends on your desktop, and jellytoast detects it so Frosted
is **never see-through**: where blur can't land it paints a near-opaque frosted
panel instead of glass.

| Desktop / OS | App-controllable blur | Frosted dark renders as |
| --- | --- | --- |
| KDE Plasma (Wayland) | ✅ KWin blur (needs `kwindowsystem`) | true frosted glass |
| KDE Plasma (X11) | ⚠️ can't verify from the client | near-opaque panel (conservative) |
| niri | ✅ `ext-background-effect-v1` | true frosted glass |
| GNOME, Cinnamon, XFCE, MATE | ❌ no app-controllable blur | near-opaque panel |
| Hyprland / SwayFX / Wayfire | 〰️ user-configured (see below) | near-opaque unless you add a rule |
| Windows 11 (22000+) | ✅ Acrylic blur-behind | true frosted glass (`JT_NO_WIN_BLUR` → Mica) |
| Windows 10 / macOS | ❌ / not yet implemented | near-opaque panel |

The boot log and **Settings → Display** explain why on your machine. To force an
opaque body regardless, launch with `JT_OPAQUE=1` or pick the **Solid dark**
theme. (`JT_BLUR_FORCE=active|unsupported` overrides detection — handy for
previewing the fallback.)

**KDE — Frosted looks flat?** Enable System Settings → Desktop Effects → **Blur**,
confirm compositing is on, and install `kwindowsystem` if missing.

**wlroots compositors (Hyprland, SwayFX, Wayfire):** jellytoast can't *request*
blur, but these compositors blur via *your* config keyed on the window's
`app_id` — the stable string **`jellytoast`**. Hyprland blurs by default (it may
already work); to control it explicitly use modern rule syntax:

```
windowrule = noblur, class:^(jellytoast)$   # Hyprland — opt OUT (blur is on by default)
```

(SwayFX / Wayfire: target `app_id = jellytoast` in their per-app / layer blur
config.)

## Why mpv?

Browser-based playback stacks transcode FLAC / ALAC to AAC server-side. mpv
plays them untouched, supports gapless and ReplayGain, and uses much less RAM
than a browser playback pipeline.

## Why MPRIS?

MPRIS2 is the integration point on Linux:

- Keyboard media keys work.
- KDE Plasma's media widget shows the current track + controls.
- GNOME Shell's media controls show jellytoast.
- `playerctl play-pause` works from a script or keybinding.
- waybar / polybar / etc. can render the current song.
- Browsers pause their media when jellytoast starts.

## Troubleshooting

| Issue | Fix |
| --- | --- |
| `mpv` import fails | Install libmpv via your package manager (`sudo pacman -S mpv` on Arch, `sudo apt install libmpv2` on Ubuntu) |
| `pyside6` import fails | `sudo pacman -S pyside6` or `pip install PySide6` |
| No tray icon | Install a tray helper for your DE (e.g. `plasma-systray`) |
| Chromecast not found | Open UDP 5353 (mDNS); same VLAN as the Chromecast |
| AirPlay / DLNA not found but Chromecast works | A host firewall is blocking discovery replies — Settings → Casting has a ⓘ with a copy-paste allow rule for your subnet |
| AirPlay receiver not found | Check Settings → Casting has AirPlay enabled; some older LG webOS / shairport-sync 5.x receivers are broken in pyatv |
| DLNA / Sonos not listed in cast menu | The backends ship in the standard install — enable the protocol in Settings → Casting |
| Frosted theme looks flat, not glassy | Compositor blur isn't active here — Frosted falls back to a near-opaque panel (never see-through). On KDE enable Desktop Effects → Blur + install `kwindowsystem`; GNOME/Cinnamon/XFCE have no app-controllable blur. See **Themes & blur** |
| Wayland: video shows in wrong spot | Set `QT_QPA_PLATFORM=xcb` in the environment to force XWayland |
| Login devolves to LoginView across launches | Keyring (kwalletd6) is unresponsive at boot — the encrypted file fallback should kick in. Sign in once; subsequent launches auto-sign-in via the file. |
