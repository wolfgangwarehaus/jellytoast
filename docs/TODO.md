# jellytoast — what's left to do

The running backlog, in plain language. Last refreshed **2026-05-22**
against the code on `polish/2026-05-22-sweep` (`12edd03`, 1695 tests
passing).

Companion docs:

- `docs/manual_test_plan.md` — things to check by hand / by eye.
- `docs/autonomous_tasks.md` — work that can be handed to an unattended
  agent.
- `docs/SPEC.md` — what the app actually does today.
- `CHANGELOG.md` — what's already shipped, dated.
- `docs/research/` — the original design docs for each feature (each
  now carries a status banner saying whether it shipped).
- `docs/decisions.md` — why certain architectural choices were made.

## How this list is ordered

**Priority reset 2026-05-21:** the focus is nailing the feature list,
polishing, and bug-testing — getting the project genuinely dialled in
*before* any distribution push. Packaging is scaffolded and ready to
go, but it has been deliberately moved off the top: it is no longer
the gate, and we won't over-focus on shipping until the app is solid.

- **Right now** — feature completeness & polish.
- **Bug-testing pass** — work through the manual test plan by hand.
- **Packaging — scaffolded, deferred** — ready when august says go.
- **Later (P3)** — genuine ideas, not yet load-bearing.
- **Hardware-blocked (P4)** — needs a Windows machine or a Mac.

---

## Right now — feature completeness & polish

The remaining feature gaps. In each case the engine is already built
and tested; what's missing is the user-facing finish.

### Tag editing — cover-art UI + bulk edit

Single-track tag editing shipped 2026-05-20 (`modules/tag_editor.py`,
right-click "Edit tags…", Jellyfin admins, gated on
`can_edit_metadata_on_account()`). The `upload_cover_art` provider
method landed 2026-05-21 (Jellyfin: base64-body / image-mime request
shape via `JellyfinAPI.upload_primary_image`, mocked-HTTP tested).

Still to do:

- **A cover-picker control** in the "Edit tags…" dialog — file dialog
  + preview, wired to `upload_cover_art`. Visual; build with august.
- **Bulk "apply to whole album"** editing. The *backend* for this —
  a provider method that applies a field-change set across every
  track of an album — is queued as an autonomous task (AT-3, see
  `docs/autonomous_tasks.md`); the dialog wiring is visual and waits
  for august.
- Neither `upload_cover_art` nor the bulk path has been exercised
  against a live Jellyfin server yet.

### Crossfade easing curve

The crossfade volume ramp is a deliberate linear v1 placeholder
(`modules/playback/crossfade.py:316`, marked `TODO(august)`). Swapping
in a tuned curve (equal-power, S-curve) is a subjective polish call —
august's to make. Design notes: `docs/research/crossfade.md` §5.

### Polish pass — the front-of-house surfaces

The Cast dialog and the mini player are the app's differentiators and
the first thing anyone sees. Keep polishing them as rough edges turn
up — these are what eventual screenshots will bake in, so they earn
disproportionate attention.

Ongoing — a session-by-session see-it/fix-it sweep covers the rest
of the chrome (dropdowns, tooltips, sliders, popups). Next on this
front:

- **Theme-switch latency** — flipping Mode in Settings still has a
  perceptible pause before everything re-stamps. Cause is the
  synchronous emit fanning out to ~25 subscribers + the dialog's
  full page rebuild + the global QSS push. Worth profiling and
  tightening (defer offscreen surfaces, batch the repolish loop).
- **Tooltip backdrop visual check** — the new `_DARK_POPUP_OPAQUE`
  alias for tooltips landed; confirm top + bottom tooltips read
  identical against bright wallpapers.

---

## Bug-testing pass

This is now a first-class priority, not an afterthought.
`docs/manual_test_plan.md` carries eight "ready to verify now"
sections — features that shipped with working UI but have never been
confirmed by hand: smart playlists, the start-radio right-click
entries, internet radio, the audio visualizer, the five-protocol cast
dialog, the full downloads arc, the date-based smart-playlist rules,
and the sleep-timer / smart-shuffle UI.

Working through that list — finding and fixing the bugs it surfaces —
is the work that gets the project genuinely dialled in. Do this
before packaging.

---

## Packaging — scaffolded, deferred

Deferred by choice on 2026-05-21: the app should be feature-complete
and polished first. None of this is dropped — the scaffolding is done
so it's a short hop when the time comes, and lining up more
scaffolding or research now is welcome. It just isn't the focus.

### AUR package

The app has been pip-installable since 2026-05-17 — proper build
system, flat layout, `gui-scripts` entry point. All code-side
prerequisites are done. What's left is writing the Arch `PKGBUILD`
and submitting it. Mechanical, but it needs a maintainer's judgement
on optional dependencies and post-install hooks — do it with august.

### Flathub

The AppStream metadata file, the `.desktop` file, and the icons are
all in `packaging/`. Still missing:

- **Screenshots.** Clean PNGs of Library, Now Playing, the Cast
  dialog, Downloads, Settings, the Visualizer, Smart Playlists, Radio.
- The `<screenshots>` block in the metainfo XML is written but
  commented out — uncomment and fill it once the PNGs exist.
- **A Flatpak build manifest** (`.yaml`) — doesn't exist yet. Must
  grant `--filesystem=xdg-data/kwin` so `modules/drag_repaint/` can
  install its KWin scripted effect from inside the sandbox. Drafting
  this is queued as a candidate autonomous task (AT-5).
- Then a pull request against `flathub/flathub` and days of reviewer
  back-and-forth.

### Cast-proxy demo clip

A ~30-second hero clip for the README: a Chromecast playing music
from a Tailscale-only server while the laptop is offline — the single
most distinctive thing the app does. Needs a real recording session;
pairs naturally with capturing the Flathub screenshots.

---

## Later (P3)

Real ideas, but not yet pulling weight.

- **A registered Cast receiver app.** Right now Chromecast screens
  show "Default Media Receiver" instead of "jellytoast". Fixing that
  needs a $5 Google developer account and a small hosted web app.
- **AirPlay 2 edge cases.** A few specific receivers (older LG webOS
  TVs, shairport-sync 5.x) misbehave with the AirPlay library.
- **A supplementary network-status signal** (`QNetworkInformation`) —
  flaky on Linux; worth revisiting when the Windows/macOS work starts.
- **Importing server-side playlist files (m3u, etc.)** — probably out
  of scope for a streaming-first music app unless someone asks.

---

## Hardware-blocked (P4)

These need a Windows machine or a Mac, neither of which is available
for testing yet, so writing the code now would be writing it blind.

- **Windows support** — the native bits for media-key integration,
  autostart, always-on-top, and notifications; plus checking the
  HiDPI path.
- **macOS support** — the same set of native bits via the Mac APIs.
- **iOS** — only after a Mac exists. Needs download-storage sandbox
  handling, CarPlay handoff, lock-screen artwork.
- **Exclusive audio output (ASIO)** — a Windows-only audiophile
  feature; only if a Windows user asks for it.
- **Per-OS visualizer audio taps** — the Linux audio tap works; the
  visualizer needs equivalent taps on Windows, macOS, and iOS for
  cross-platform parity.

---

## Tiny loose ends

(None open. The stale `cast_dialog_sections.py` comment that claimed
the DLNA/Sonos/Snapcast sections "stay empty" was corrected
2026-05-21 — discovery for all five protocols ships.)

---

## Recently shipped

The full dated history lives in `CHANGELOG.md`. The short version of
the last few sessions: smart playlists end-to-end, the audio
visualizer, internet radio, the 10-band EQ, the whole downloads /
offline system, all five casting protocols wired up, the right-click
"Create smart playlist" and "Start radio" menu entries, the
sleep-timer menu and the smart-shuffle toggle, smart-rule schema v2
(date-based rules), crossfade controls, the multi-server login UI
(alternate-URL manager + failover toast), the editable Hotkeys page,
single-track tag editing, the borderless main window, light themes
end-to-end (FROSTED_LIGHT / LIGHT / TRANSPARENT_LIGHT) with live
mode-switching, the audio routing fix (PipeWire 1.6.5 link-policy +
WirePlumber persisted mute), and the unified elevated-surface
treatment for dark themes (one `_DARK_ELEVATED` knob for hovers /
highlights / volume popup, one `_DARK_POPUP_OPAQUE` for menus /
combos / tooltips).

---

## Parked — deferred, not dropped

- **Last.fm scrobbling.** The client code is built and stays dormant
  in `modules/scrobble/lastfm.py`, but registering the in-app API key
  needs a Last.fm account — and their signup firewall (Error 406)
  blocked it repeatedly, from several networks and devices. The
  Settings → Scrobbling page hides the Last.fm section entirely while
  `API_KEY` / `API_SECRET` are empty; populate them to bring it back.
  **ListenBrainz** is the supported scrobbling path and works today.

---

## Explicitly not on the roadmap

Deliberately out of scope — each is a fight a competitor already wins:

- **Local-file libraries** — that's Strawberry / Tauon territory.
- **Podcasts** — outside the music-only focus.
- **A mobile app** — Symfonium and Finamp own that space.
- **Heavy audiophile DSP** (automatic headphone correction, very
  high-band parametric EQ) — Symfonium is uncatchable there.
- **CarPlay / Android Auto** — mobile-only concerns.
