# Steam Deck 0.2.0 flatpak QA — findings

**Box:** Steam Deck (LCD), SteamOS, desktop mode — Plasma 6 / KWin on
**Wayland**, 1280×800 @ **scale 1.0** (not the 1.25 the brief assumed; see §C).
**Artifact:** `jellytoast.flatpak` from the v0.2.0 release — the **re-roll**
(asset stamped `2026-07-13T03:24Z`, hours after every other 0.2.0 asset), sha256
`2a60f4f4…9256`, verified against `SHA256SUMS`.
**Method:** true fresh install (prior system install *and* the earlier session's
hand-applied overrides removed) → in-asset verification of the #230 fix → feature
sweep driven through the `JT_TEST_BRIDGE` socket → pixel-level visual judgement.
Server: Navidrome (subsonic) at `http://192.168.50.100:4533`, signed in as `avtips`.

**Verdict:** the #230 Qt-skew fix **is** in the asset (wheel Qt gone, `org.kde.KWin=talk`
granted by the manifest, no overrides needed) — but **the glass still does not land.**
Row 15 fails on the same symptom the re-roll was cut to fix, now with a cleaner
diagnosis. Separately, the sweep turned up a **release-blocking packaging defect
that has nothing to do with blur**: the bundled libmpv has **no `https` support**,
so any TLS media URL is silently unplayable.

---

## The two headline bugs

### 1. 🔴 libmpv in the flatpak cannot play `https://` — *new, worst finding*

The bundled `/app/lib/libmpv.so.2` exposes 47 protocols. `http`, `hls`, `rtmp`
are there. **`https` is not** (nor `rtmps`):

```
http   YES
https  *** MISSING ***
```

Loading a TLS stream dies immediately with
`end-file  reason=error  file_error='loading failed'` — while `curl` inside the
very same sandbox fetches that URL fine (`http=200, audio/mpeg`). So it is not
network, not the portal, not DNS. It is the codec build.

**Cause:** `packaging/flatpak/…yml`, module `ffmpeg-lgpl`, has no TLS backend in
`config-opts` — no `--enable-gnutls`, `--enable-openssl`, or `--enable-mbedtls`.
FFmpeg's `https` protocol requires one; without it, it silently builds without it.
The bundled libmpv links `libcrypto` but no `libssl`/`gnutls` — the classic
signature.

**Blast radius is much wider than internet radio.** Library browsing goes through
Python's HTTP stack (its own TLS — fine), but *audio* goes through mpv. So a user
whose Jellyfin/Navidrome is behind TLS — a reverse proxy, Tailscale,
`https://music.example.com`, i.e. the *normal* remote setup — would **browse their
whole library and get silence on every track**, with no useful error. This Deck
only plays because its Navidrome is plain `http://` on the LAN.

**Suggested fix:** add `--enable-gnutls` to the `ffmpeg-lgpl` `config-opts`.
gnutls (LGPL-2.1+) is the GPL-compatible choice — OpenSSL's Apache-2.0 is
incompatible with this project's GPL-2.0+. `libgnutls.so.30` already ships in
`org.kde.Platform 6.10`; CI should confirm the SDK carries the dev headers (else
add a gnutls module). **`packaging/macos/mas/build_libmpv_lgpl.sh` has no TLS flag
either** — the Mac/MAS build is likely to have the same hole and is worth checking
on that leg.

### 2. 🔴 Row 15 — blur reports ACTIVE, but the window composites opaque

`blur.status()` → `ACTIVE`, `reason()` → `"KWin blur active"`, with **zero
overrides** in play. The desktop is nonetheless **not** visible through the window.

Evidence (`dev/steamdeck_test_artifacts/blur-fail-over-checkerboard.png`): I set the
wallpaper to a neon magenta/green checkerboard, launched the app fresh and *never
touched its geometry*, and sampled pixels.

| surface | sample over magenta | sample over green | reads as |
|---|---|---|---|
| **Konsole** (control, same compositor) | `(44,24,50)` | `(19,49,40)` | backdrop bleeds through — translucent ✅ |
| **jellytoast body** | `(53,54,62)` | `(55,55,63)` | neutral grey, **R≈G≈B** — zero bleed ❌ |

Konsole proves KWin's translucency+blur genuinely work on this host. KWin agrees:
`isEffectLoaded(blur)` → `true`, blur+contrast both in `activeEffects`, compositing
`gl2`.

What makes this *actionable* rather than just "still broken": **the app-side
painting is correct.** The mini player rendered over a magenta canvas *inside* the
process comes out dark-purple with properly transparent rounded corners — i.e. the
widget really does paint at the 67% glass alpha. And:

- `WA_TranslucentBackground` = `true`
- surface `alphaBufferSize` = `8`
- theme `frosted_dark`, `body_color_for(...)` = `rgba(18,18,18,172)` (the ACTIVE/glass alpha)

So the theme layer, the alpha, and the surface format are all right. The tell is
that **neither blurred *nor* sharp desktop** shows through. A translucent surface
with no blur would still show the *sharp* checkerboard. Showing *nothing* means the
surface is reaching KWin as **opaque** — the problem is in the surface/blur
integration with the compositor, not in `theme.body_color_for()` and not in the
status probe's plumbing.

**Net:** #230 correctly stopped the *wheel Qt* from clobbering the runtime Qt, and
the KWin grant is now in the manifest — but `status()` returning `ACTIVE` is still
not evidence that glass landed. The probe remains a promise the compositor doesn't
keep.

---

## §A — Install mechanics (fresh, user-path)

| Check | Result |
|---|---|
| Prior install + overrides fully removed | ✅ removed the **system** install *and* the earlier session's manual crutches (`org.kde.KWin=talk` override, `JT_BLUR_FORCE=unverifiable`) |
| Asset integrity | ✅ sha256 matches `SHA256SUMS`; flatpak asset is the re-roll (03:24Z vs 19:53Z for all others) |
| Discover sideload | ⚠️ **not autonomously verifiable** — see note |
| Wheel Qt absent from site-packages | ✅ `libQt6Gui.so.6`: No such file |
| `org.kde.KWin=talk` present | ✅ **from the manifest**, with overrides empty |
| App launches | ✅ (launched cleanly ~6× across the session) |

**Notes**
- *The spurious "install failed" toast could not be tested.* Discover is a native
  **Wayland** client, so an agent has no way to press its Install button (xdotool
  reaches XWayland only; there is no `ydotool`/`kdotool` on this box). Discover did
  accept the bundle and render the app page correctly — **0.2.0, 715.5 MiB,
  GPL-2.0+, developer "august"**, AppStream screenshots all loading — so the
  metainfo is healthy. Install completed via the brief's sanctioned CLI fallback.
  **This row needs a human click.**
- Discover also reported *"jellytoast is not installed but it still has data
  present"* — leftover `~/.var/app` state from the earlier session. Kept
  deliberately (the brief prefers reusing the existing Navidrome sign-in); note
  that this means "fresh install" here = fresh **app**, retained **user data**.

## §B — Feature sweep

| # | Feature | Result |
|---|---|---|
| 1 | Sign in → relaunch → still signed in | ✅ `SubsonicProvider` / Navidrome survived 4+ relaunches |
| 2 | Library browse + multi-library picker | ✅ 3 libraries (Music Library, Discovery, Library) |
| 3 | play / seek / next / prev / volume | ✅ all five (`prev` restarting the track past 3s is intended — `queue_manager.previous()`) |
| 4 | Queue reorder; restore across relaunch | ✅ reorder held, and survived relaunch with current track **and** position (2:37) |
| 5 | Now-playing + synced lyrics + visualizer | ✅ all three; lyrics highlight the live line; visualizer draws real FFT |
| 6 | Equalizer applies | ✅ at the filter level: mpv `af` goes `[]` → `anequalizer` with all 10 bands on both channels at exactly the set gains, and clears on disable. *(Not literally listened to — an agent can't hear.)* |
| 7 | Mini player frost matches main window | ⚠️ consistent with the main window — both paint 67% glass, neither gets a compositor backdrop. Tied to row 15. |
| 8 | Casting discovery | ⚠️ **not verifiable here** — `avahi-daemon` is **inactive** on this SteamOS; the *host* sees 0 mDNS services of any kind. Cast manager loads (Chromecast/AirPlay/DLNA/Sonos) and discovery ran without error; 0 devices found. Needs a box with mDNS + a real cast target. |
| 9 | Offline: download → plays, under `~/.var/app` | ✅ 27 MB FLAC at `~/.var/app/…/downloads/eb/eb1bfc…flac`; plays from the **local path** (`is_local: true`) |
| 10 | Smart playlist → preview → Save & Play | ✅ resolved to 100 items, queue context `PLAYLIST`, audio started |
| 11 | Internet radio plays | ❌ **FAIL — headline bug #1** (https) |
| 12 | Scrobbling | n/a — no Last.fm/ListenBrainz creds configured on this Deck |
| 13 | MPRIS: Plasma media widget + media keys | ✅ own-name `org.mpris.MediaPlayer2.jellytoast`, Identity `jellytoast`, full metadata; Pause/Play/Next over MPRIS all work (this is the path Plasma's media keys use) |
| 14 | Notifications: track-change banner | ✅ fires when enabled (**off by default** — correct, `notify_on_track_change`) |
| 15 | **Blur genuinely ACTIVE** | ❌ **FAIL — headline bug #2** |
| 16 | Tray + hide-to-tray; start-at-login | ⚠️ **split**: tray ✅ (registered as `StatusNotifierItem`, Title `jellytoast`, Status `Active`; close → window hides, process survives, restores). Start-at-login ❌ — see below |
| 17 | About shows 0.2.0; no false update chip | ✅ 0.2.0 everywhere; `is_newer` false for `0.2.0` and `v0.2.0`, true only for `0.2.1`; `UpdateChip` not visible |

## §C — Deck-specific

| Check | Result |
|---|---|
| 1280×800 rendering, no clipped dialogs | ✅ — **at both 100% (this Deck's real setting) and a simulated 125%** (`QT_SCALE_FACTOR=1.25`). The settings dialog adapts its height (620→574 logical) and its content **scrolls** (`vbar_visible: true`, `max: 133`) — nothing unreachable, text legible. *Note: the brief assumed 1.25; the Deck is actually at Scale 1.* |
| CPU while playing FLAC, visualizer off vs on | ⚠️ see visualizer leak below |
| Suspend/resume mid-playback | **NOT RUN** — needs a physical power-button wake; running it risked stranding a remote session with nobody at the Deck. Skipped on august's call. |
| STRETCH: Gaming Mode as non-Steam app | **NOT RUN** (stretch; time went to the two headline bugs) |

---

## Other findings

### 3. 🟠 Start-at-login writes a `.desktop` that cannot work under flatpak

The toggle *does* write through the narrow `~/.config/autostart:create` grant — the
grant is fine. But what it writes is sandbox-internal:

```ini
Exec=/usr/bin/python3 -m jellytoast
Path=/app/lib/python3.13/site-packages
```

`~/.config/autostart` is executed by the **host** Plasma session, where `/app/…`
does not exist and the host's `python3` has no `jellytoast` module (verified: `No
module named jellytoast` from a neutral cwd). So **start-at-login silently does
nothing** for every flatpak user. Needs the flatpak-aware form —
`Exec=flatpak run io.github.wolfgangwarehaus.jellytoast`, no `Path=` — gated on
`/.flatpak-info` / `$FLATPAK_ID`. Toggling back off removes the file cleanly.

### 4. 🟠 The visualizer never shuts down — a permanent ~12%-of-a-core tax

On a handheld this matters. Same track, same FLAC, one process, `%` of one core:

| state | CPU |
|---|---|
| library page, visualizer never built | **5.4%** |
| Now-Playing, cover pane | 5.6% |
| visualizer **visible** | **17.7%** |
| switched **back to cover** (visualizer hidden) | **17.6%** ← does not come back down |

`_visualizer_engine` is still alive (`engine_alive: true`) while the pane is
`cover`. Once you glance at the visualizer, its decode tap keeps running and
burning ~12 points of a core **for the rest of the session** — until you restart
the app. Tearing the engine down on pane-switch would give the battery back.

### 5. 🟡 `mpris:trackid` is empty

MPRIS metadata is otherwise complete, but `mpris:trackid` is an empty string. The
spec types it as an object path (`o`); some clients are strict about it. KDE
tolerates it.

### 6. 🟡 Track-change notification is unbranded

The banner shows up as **"System Notifications"** with a generic
`start-here-kde-plasma` icon rather than jellytoast + its own icon — the Linux
backend shells out to `notify-send` without `--app-name`/`--icon`.
(`notify-send` *is* present in the runtime; notifications themselves work.)

### 7. 🟡 Doubled path segment in the downloads dir

`~/.var/app/io.github.wolfgangwarehaus.jellytoast/data/**jellytoast/jellytoast**/downloads`
— harmless, but the app name appears twice.

---

## Environment casualty (not an app defect)

Mid-session I `pkill`ed spectacle while it had a capture in flight, which **wedged
KWin's screenshot backend** for the rest of the session (`KWin screenshot request
failed`; the portal's `CaptureWorkspace` then returned `NoReply`). Reloading the
`screenshot` effect and `reconfigure` did not clear it, and the only real fix —
restarting the compositor — would have killed the Konsole this session runs in.

**This is my tooling mistake, not a jellytoast bug**, and it does not touch the
findings: **every pixel of the row-15 blur evidence was captured before the wedge.**
Visual judgement afterwards used in-process `QWidget.grab()` renders (correct for
layout/legibility/clipping; by construction it cannot show a compositor backdrop,
which is why the blur case was settled first). A fresh Plasma session restores
`spectacle`.
