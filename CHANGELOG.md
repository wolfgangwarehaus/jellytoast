# Changelog

What changed in each release, in plain language — newest first. The detailed,
developer-facing history lives in [`docs/CHANGELOG.md`](docs/CHANGELOG.md).

## [Unreleased]

<!-- Voice: short, plain, a little casual — what's new for a *user*, not a press
     release. Specs right (versions/platforms); drop the gloss + the internals.
     This block becomes the GitHub release notes; cut_release.sh stamps it into a
     dated version on release. One line per change where you can. Bold titles
     should stand alone: the Store "What's new" shows just the title (+ first
     sentence in --detail mode). -->

- **One download for every Mac.** macOS is now a single universal .dmg that
  runs natively on both Apple Silicon and Intel — no more picking the right
  one. Heads up: it now needs macOS 15 (Sequoia) or newer.
- **The Mac .dmg gets its glass back.** The direct-download build was missing
  the piece that powers native frosted blur, Control Center controls, and
  notifications (the App Store build had it all along). Now they match.
- **Native on Windows ARM.** There's now a real ARM64 build, so jellytoast
  runs at full speed on Snapdragon / ARM PCs instead of through emulation.
  One installer picks the right version for your PC automatically.
- **The library name shrinks before it collides.** With a couple of libraries
  selected, the title up top steps down ("Music Library +1", then "2
  libraries") instead of sliding under the Albums menu.
- **"Other audio is playing" matches the rest.** That little popup now wears
  the same frosted style as the app's other hover notes.
- **Windows scrobblers: reconnect once.** Windows now keeps your logins in its
  own secure store. Your server sign-in carries over automatically — but if you
  scrobble to ListenBrainz or Last.fm, you'll reconnect those one time after
  updating.

## [0.1.9] — 2026-07-10

- **Smart playlists respect your library picker.** With only some libraries
  selected, previews and Save & Play no longer sneak in tracks from the
  ones you deselected — fixed on both Jellyfin and Navidrome / Subsonic.

## [0.1.8] — 2026-07-06

- **Picking a few libraries now actually filters.** With 3+ libraries on
  the server, checking a subset in the Music dropdown used to quietly show
  everything (with an apologetic toast). Now Albums, Artists, Songs, and
  Suggestions really show just the libraries you picked — and on a Jellyfin
  server with several music libraries, "all" finally means all of them, not
  just the first one.
- **Smart playlists start much faster on Navidrome / Subsonic.** Hitting
  Play on a smart playlist could sit there for 10+ seconds while we fetched
  the library one album at a time; it now grabs songs in bulk (and in
  parallel where it can't). Bonus: playlists built on play count, rating,
  or artist/album text used to only see the first 500 albums of a big
  library — that limit is gone on Navidrome.
- **AppImage self-updates actually work now.** Every AppImage shipped an
  update feed pointing at a file we never uploaded, so AppImageUpdate always
  failed. Fixed from this release on — and since the feed watches the *latest*
  release, AppImages you already have start updating too.
- **Songs sorting fixed on Navidrome / Subsonic.** On big libraries your
  chosen sort only held within each 500-track chunk; it now applies to the
  whole list.
- **Repeat-all un-greys Next.** With repeat on, the OS media controls (KDE
  widget, Windows flyout / lock screen) no longer disable Next on the last
  track — it wraps, like it always did in-app.
- **Smart playlists are honest on Subsonic.** "Rating" and "Last played"
  rules can't match on Subsonic / Navidrome (the server doesn't send that
  data) — the editor now says so instead of quietly building a playlist
  that stays empty forever.
- **Tab works on the sign-in form.** It now hops straight down the fields
  to Sign in, and you can always see where it landed.
- **Smart playlist rules are back on Navidrome / Subsonic.** Adding or
  editing a rule quietly did nothing there — fixed, and the fields your
  server can't match now explain themselves on hover.
- **Small stuff.** Selecting several libraries (but not all) now tells you
  the filter isn't applied yet; the "Only download on Wi-Fi" checkbox is
  gone until it can actually pause anything; failed favorites / scrobbles
  now leave a trace in the log instead of vanishing.

## [0.1.7] — 2026-07-03

- **Themes.** Settings → Display now has a proper theme picker: Catppuccin
  (all four flavors), Dracula, Everforest, Gruvbox, Nord, One Dark, Rosé Pine,
  Solarized, and Tokyo Night, alongside the jellytoast look — each previewed
  with its own palette strip. Families with a dark and a light half can follow
  your system's light/dark setting, and a **glass opacity** slider tunes how
  deep the frosted look goes. Everything applies live, with a 10-second
  keep-or-revert prompt so a bad pick undoes itself.
- **Bring your own theme.** Import any base16 `.yaml` color scheme (there are
  hundreds in the community), or drop schemes into
  `~/.config/jellytoast/themes/` and they show up in the picker. If a file in
  that folder changes while it's your active theme, the app re-themes on the
  spot — so wallpaper-theming tools like matugen can drive jellytoast.
- **Follow your desktop.** **Follow system accent** adopts your OS accent color
  live — on KDE / GNOME, Windows, and macOS. And on Linux, **Follow pywal /
  wallust** re-themes the whole app every time your wallpaper changes.

## [0.1.6] — 2026-07-01

- **Font and size change live.** **Settings → Display → Font** lets you set the
  app's text to any font installed on your system (each previewed in itself;
  icons are unaffected), and both the **font** and the **font size** now apply
  **instantly** — no restart. Each change shows a 10-second "Keep or revert"
  prompt so a choice that doesn't work for you undoes itself, and that prompt
  always stays readable even if the font you picked isn't.
- **Square corners, if that's your thing.** A new **Settings → Display → Square
  corners** switch squares off every rounded corner in the app — windows, album
  art, players, dialogs, buttons, menus, the lot — for a sharper, boxier look.
  Genuinely round controls (the play button, sliders) stay round. Takes effect
  after a restart.
- **Big libraries load their album art reliably now.** On a large library
  (thousands of albums, especially on Subsonic / Navidrome) the cover grid could
  load a handful of covers and then stall, or all the art could blink out at once
  and come back — over and over. Three things were behind it: the loader flooded a
  busy server, a slow-but-alive server got mistaken for "offline" (which blanked
  the grid), and the on-disk cover cache was too small to keep a big library
  between launches. All fixed — covers fill in steadily and stay put, and ones you
  scroll past and back reload instantly.
- **Lighter still on huge track lists.** The Songs view caps the thumbnails it
  keeps in memory, so a library with tens of thousands of tracks no longer grows
  RAM the longer you scroll.

## [0.1.5] — 2026-06-29

- **Intel Mac support.** A native Intel (x86_64) `.dmg` now ships alongside the
  Apple Silicon one — grab the build for your chip. Both are signed + notarized.
  Needs macOS Sequoia (15) or newer on Intel, Sonoma (14) or newer on Apple Silicon.
- **Real frosted-glass blur on macOS.** The window now uses genuine native
  vibrancy — where it used to fall back to a near-opaque panel — and it stays put
  through resize, fullscreen, and focus changes.
- **Update notifications.** On manual installs (`.dmg` / `.deb` / AppImage /
  installer), jellytoast now quietly checks for a newer release and shows an
  "Update available" chip in the top bar — no more unknowingly running an old
  version. The Microsoft Store / Mac App Store builds update themselves and are
  left alone. You can turn the check off in Settings → General.
- **Snapcast removed.** It only ever *controlled* an existing Snapcast server — it
  couldn't play your library to one — so it's gone. Chromecast, AirPlay 2, DLNA,
  and Sonos are unchanged.
- **Casting is opt-in.** Nothing scans your network until you turn a protocol on in
  Settings → Casting; the cast button takes you there if nothing's enabled yet.
- **Lighter on big libraries.** The album grid no longer keeps every cover you've
  scrolled past in memory — it caps them and reloads from disk as needed, so a huge
  library won't balloon RAM.
- **The Songs view loads instantly again.** It re-renders from its saved cache
  instead of re-fetching the whole track list every time you open it.
- **Small polish.** Long titles that overflow now scroll with a soft fade instead
  of a hard cut, and a tile with no artwork shows a subtle placeholder glyph in
  place of a blank square.
- **Fixes.** The year no longer shows "None" for tracks without one; shuffle,
  repeat, and window position now stick when you quit from the tray; rewinding a
  track no longer skews your scrobbles; plus casting + reconnection robustness.

## [0.1.4] — 2026-06-26

- **macOS support** — a signed, notarized `.dmg` with the native niceties: media
  keys & Now Playing, real window blur (honoring Reduce Transparency), a native
  menu bar + Dock menu, integrated titlebar, notifications, and launch-at-login.
- An interrupted download no longer wedges the queue; on macOS the mini-player
  lands bottom-right and the app shows "jellytoast", not "Python".

## [0.1.3] — 2026-06-21

- **Universal-Linux AppImage** — one self-contained file that runs on any modern
  distro with no install and no root (it bundles its own mpv).
- **"Try a demo"** on the sign-in screen — explore jellytoast against a public,
  read-only server with one click, no server of your own needed.

## [0.1.2] — 2026-06-20

- **The Linux `.deb` now launches on X11 / XWayland** (it was missing part of the
  Qt xcb library closure — this also affected 0.1.0 and 0.1.1).
- Fixed a Jellyfin crash on tracks with an unknown duration; downloads no longer
  follow you across a sign-out / server switch; internet radio casts reliably to
  DLNA & Sonos.
- **Security:** the credential file is owner-only from the very first launch (Linux).

## [0.1.1] — 2026-06-17

- **The Linux `.deb` launches on modern Ubuntu (24.04 / 26.04)** — the v0.1.0
  package failed to start.
- Cast / AirPlay discovery no longer deadlocks on Python 3.14.
- Frosted glass + frameless chrome on GNOME and other non-KDE Wayland desktops.

## [0.1.0] — 2026-06-16

First release — a native, frosted-glass music player for Jellyfin and Subsonic /
Navidrome (Linux `.deb` + Windows).

- Two backends at full parity (Jellyfin + Subsonic / Navidrome).
- Bit-perfect, gapless playback via libmpv.
- Cast anywhere — Chromecast, AirPlay 2, DLNA, Sonos (plus a Snapcast control
  surface), with a built-in proxy for receivers the app can't reach directly.
- Real offline mode, a floating mini player, frosted-glass UI, media keys, tray,
  ListenBrainz scrobbling, smart playlists, an FFT visualizer, and Jellyfin tag editing.
