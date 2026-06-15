# jellytoast — what's left to do

The running backlog. Last boiled down **2026-06-12** (packaging day),
refreshed **2026-06-14** (Windows-standards round — all shipped + verified).
Closed work collapses to one-liners; the dated detail lives in
`docs/CHANGELOG.md` and this file's git history.

> **2026-06-14 Windows-standards round — ✅ SHIPPED + VERIFIED (PR #86):**
> SMTC (hardware media keys + the now-playing flyout/lock-screen),
> prevent-sleep during playback (cross-platform — fixed the Linux gap
> too), single-instance window foregrounding, Windows toasts
> (download-complete now works + an opt-in now-playing toast), and the
> taskbar play/pause overlay badge — all live-verified on the Windows 11
> laptop and merged. Plus the image-cache async-write drain on
> sign-out / shutdown (pre-merge review fix). The earlier 2026-06-12
> Windows-parity items (autostart, taskbar icon/AUMID, boot-stall fix,
> visualizer rebuild) verified + merged in PR #85.
>
> Open Windows tail (both LOW):
> - [ ] **Visualizer track-switch latency on WiFi** — bars wait for the
>       full compressed body download (~1s on laptop WiFi vs ~0.1s wired);
>       audio is unaffected. Planned: two-phase fetch (Range-limited first
>       chunk for instant bars). Risky vs the buffer-complete invariant.
> - [ ] **Construction-time icon baking** — a lazy `QIconEngine` was tried
>       (PR branch `perf/lazy-icons`) and **reverted**: softened/chunked
>       glyphs at fractional scale on Windows. The baked path stays; the
>       real cold-boot win is the installer, not this.

> **2026-06-15 core bug-hunt (9 confirmed).** Adversarial correctness sweep
> of playback/queue/providers/offline/cast/ui-state. **Fixed on branch
> `fix/core-bug-hunt` (PR, awaiting review):** mute-while-casting no-op +
> icon desync (`player_backend.toggle_mute`); planning-failure leaking the
> session failure-counter (`offline/manager._record_failure bump_session`);
> now-playing bar clobbering the ICY radio title on a replayed `_on_started`
> (`now_playing_bar` radio guard). Each with a regression test. **Open:**
> - [ ] **HIGH — drag-reorder moves the WRONG track on a shuffled
>       album/playlist.** `np_track_list.py:1338` emits `queue_move_item`
>       with displayed (source-order) indices, but `QueueManager.move_item`
>       treats them as play-order — same class as the fixed remove bug.
>       Fix: mirror the remove-path Id→play-order map
>       (`now_playing_page.py:1394-1406`, pinned by
>       `test_np_context_remove.py`) on the move path. Top priority; needs
>       a careful signal-flow touch + a drag re-verify, so left for an
>       eyes-on pass rather than an unattended edit.
> - [ ] MED — per-server downloads LIKE filter cross-matches when one
>       `server_url` is a `:`-boundary prefix of another
>       (`offline/index._ident_like`). Robust fix = a `server_ident` column
>       (schema migration) — needs a decision, hence deferred.
> - [ ] LOW — connectivity offline-flip can drop a concurrent `note_success`
>       (read-then-flip outside `_state_lock`, `offline/connectivity.py`).
> - [ ] LOW (cast, hardware-gated) — failed auto-advance cast leaves
>       `active_cast` armed with nothing playing; Chromecast status listener
>       leaks on disconnect; `_cast_paused` vs device-side pause desync.

**august's eyes-on checklist** (from the 2026-06-11 live round, still pending):

- [ ] §-1 audio output re-walk (the picker WORKS now — Linux first, then
      Windows; both pipx installs need a refresh first, see CHANGELOG note)
- [ ] F1 visual check on the live compositor: Search results + Suggestions
      right edge — gutter should show clean frost/body, no black strip,
      all 4 themes
- [ ] View dropdown: open, arrow keys — current tab should be pre-highlighted
- [ ] F2 design call: mini-player button is checkable but only ever opens
      (no toggle-close, stale check state, `queue_btn` naming drift) —
      decide toggle vs plain button
- [ ] §1 smart-playlist remainder on Subsonic: Save / Save & Play / provider
      grey-out (editor + live preview verified working this round)
- [ ] PR #82 spot check: on `fix/platform-sweep`, Settings → General →
      "Launch jellytoast at login" still visible AND functional on CachyOS
      (the new `is_supported()` gate must not hide it on Linux) — verify
      across a reboot

Companion docs: `docs/manual_test_plan.md` (by-hand checks), `docs/SPEC.md`
(what the app does), `docs/CHANGELOG.md` (shipped, dated), `docs/decisions.md`
(why), `docs/research/` (active design docs only — shipped ones live in git
history).

Priorities: **P0** confirmed bugs → **P1** medium bugs → **P2** tidy →
**P3** low bugs + features → **P4** hardware-gated / cross-platform → then
packaging, launch, parked, not-on-roadmap.

---

## P0 — confirmed behaviour bugs

✅ **ALL FIXED & MERGED** (`fix/review-2026-06-08-bugs`, `7b8481a`; re-audited
in main 2026-06-09), each with a regression test: crossfade+skip near-silent
volume, failed-cast dead playback, shuffled-queue wrong-row remove,
color-token Reset drift, blocking primary climb-back probe. Full write-ups:
git history of this file.

---

## P1 — medium behaviour bugs

✅ **ALL 8 FIXED & MERGED** (same branch/audit as P0): stale top-bar menu
colours, mini-player `playback_restored`, replayed-pause play icon,
`_compute_subtitle` delegate crash, genres blank-grid refresh, AutoEQ
Q-preserve no-op, stale MPRIS shuffle/loop, AirPlay zeroconf + pairing-loop
leaks. Full write-ups: git history of this file.

---

## P2 — tidy / cleanup

✅ **DRAINED** across the 2026-06-08/09 sweeps (dead-code removal, tidy
refactors, docs-accuracy audit, theming audit + restamp sweeps — detail in
`docs/CHANGELOG.md` and git history) and the 2026-06-12 branch cleanup
(stale branches audited + deleted, auto-delete-on-merge ON). Two items
remain:

- ⏸️ **Mixed-DPI icon bake** (`jellytoast/icons.py` + `icon_button.py`) —
  pixmaps baked at app-DPR but painted at widget-DPR (blurry only on a
  mixed-DPI multi-monitor setup; a no-op on single-DPI). The fix touches the
  core icon paint path, so it needs verification on actual differing-DPR
  monitors before landing. Approach B (a `svg_pixmap(name,color,size,dpr)`
  helper + an opt-in `IconButton.set_glyph`) is the low-churn route.
  HARDWARE-GATED — do not land unverified.
- [ ] **Latent / unreachable theming parity (low):** Last.fm connect modal
  (`settings_dialog._lf_open_auth_modal`, gated behind an unshipped API key),
  the `settings_colors_page.py` palette CRUD (no UI entry point), and
  `SnapcastControlDialog` (hardware-gated) — all bare `QDialog`/native-msgbox;
  bring to `FrostedDialog` parity when surfaced.

---

## P3 — low-severity bugs + features not yet pulling weight

### Low-severity behaviour bugs

✅ **16 of 18 fixed 2026-06-08** (`fix/review-2026-06-08-low-bugs`, 9
regression tests — list in git history). **2 deferred** (higher-risk /
lowest-value):

- `cast_toggle_pause` flips `_cast_paused` even when the off-thread SOAP
  pause/resume fails — needs the DLNA/Sonos pause methods to report success
  and a `_run_off_thread_result` + `call_on_gui` flag flip (cross-thread,
  hardware path).
- Icon pixmaps baked at app-DPR but painted at widget-DPR — only mixed-DPI
  multi-monitor setups; the common downscale stays acceptably sharp
  (`jellytoast/icons.py`). (Same item as the P2 mixed-DPI bake.)

### Features

- **OS media-integration enable/disable toggle.** *(requested 2026-06-06)* The
  Windows **SMTC backend shipped** (PR #86, `media_controls/_windows.py` — WinRT
  metadata/thumbnail/transport + hardware keys, verified). What's left is the
  small UX piece: a Settings → Playback toggle to enable/disable OS media
  integration (gate `media_controls` start/stop on a new QSetting, default on).
- **A registered Cast receiver app** — Chromecast screens show "Default Media
  Receiver" not "jellytoast". Needs a $5 Google dev account + a hosted receiver.
- **AirPlay 2 edge cases** — older LG webOS TVs / shairport-sync 5.x misbehave.
- **`QNetworkInformation` supplementary network-status signal** — flaky on Linux;
  revisit during the Windows/macOS push.
- **Importing server-side playlist files (m3u, …)** — probably out of scope unless
  asked.

---

## P4 — hardware-gated / cross-platform

- **Windows native integration — SHIPPED** (#85/#86, verified on Win 11):
  autostart, SMTC media keys + flyout, toast notifications, taskbar overlay
  badge, prevent-sleep, single-instance foreground, HiDPI + Acrylic blur +
  borderless chrome. Remaining stub: mini-player always-on-top uses Qt's
  native `WindowStaysOnTopHint` (no OS-level rule needed off KDE Wayland).
- **Cross-thread cast write-race** — `active_cast`/`_cast_paused` written off the
  GUI thread inside `_CastTransportMixin`; needs a hardware cast session to verify
  the fix safely.
- **Sonos / Snapcast live cast verification** — code wired, not yet exercised on
  real hardware (Chromecast / AirPlay / DLNA already live-verified).
- **macOS support** — native bits via the Mac APIs (vibrancy, NowPlaying, …).
- **iOS** — only after a Mac exists.
- **Exclusive audio output (ASIO)** — Windows-only; only if a Windows user asks.
- **Per-OS visualizer audio taps** — Linux tap works; Windows/macOS/iOS need
  equivalents for parity.

---

## Packaging — in flight (packaging day 2026-06-12)

Authored on `feat/packaging-day`: PyInstaller spec (shared Linux/Windows),
`.deb` builder, Inno Setup installer + pinned-libmpv fetch, winget manifests,
Flatpak manifest, `release.yml` (tag → draft release with deb + Windows
installer + portable zip + sdist/wheel), README restructure (long-form →
`docs/user_guide.md`).

- [x] **Package rename `modules` → `jellytoast`** — ✅ DONE 2026-06-10.
- [x] **PyInstaller spec** — ✅ `packaging/pyinstaller/jellytoast.spec`; Linux
  onedir build verified locally (boots offscreen, clean SIGTERM shutdown).
- [x] **Windows installer** — ✅ `packaging/windows/jellytoast.iss` + pinned
  libmpv-2.dll fetch (`get_libmpv.ps1`, shinchiro 20260610, sha256-pinned) +
  multi-size `jellytoast.ico`. CI-built; needs one validation pass on the
  Win 11 laptop after the first workflow run.
- [x] **.deb** — ✅ `packaging/deb/build_deb.sh` (self-contained /opt bundle,
  system libmpv as Depends). Built by `release.yml` on ubuntu-22.04.
- [x] **Release automation** — ✅ `.github/workflows/release.yml`: v* tag →
  draft release with all artifacts; `workflow_dispatch` for dry runs.
- **AUR** — PKGBUILD written + dry-run validated. Left: tag a real `v0.1.0`,
  then `updpkgsums` + `makepkg -si` + `namcap` + `.SRCINFO` + push to
  `aur@aur.archlinux.org` (steps in `packaging/aur/README.md`) — with august.
- **Flathub** — manifest now exists (`packaging/flatpak/*.yaml`, KDE 6.8
  runtime + PySide BaseApp + libass/libplacebo/mpv modules, sha256-pinned);
  full runbook in `packaging/flatpak/README.md`; `python3-requirements.json`
  ✅ generated 2026-06-12 (in-repo `generate_requirements.py`, 53 pinned
  sources). Left: **screenshots** (see Launch below — shared asset),
  uncomment the metainfo `<screenshots>` block, swap the manifest `dir`
  source → `git` pinned to the v0.1.0 tag, local `flatpak-builder` test,
  submit, **and complete publisher VERIFICATION** — Mint 22's Software
  Manager hides unverified flatpaks by default, so unverified = invisible
  on Mint (research: `distribution_channels_2026-06-12.md`).
- **chaotic-AUR** — after the AUR package is live: one `[Request]` issue on
  `github.com/chaotic-aur/packages` (template asks for the AUR link). Their
  CI then auto-rebuilds from AUR forever. Best effort-to-reach ratio found.
- **winget** — manifests authored (`packaging/winget/`). Submit AFTER the
  v0.1.0 release is published (needs the live installer URL + its sha256;
  `wingetcreate` one-liner in `packaging/winget/README.md`).
- **PyPI** — wheel/sdist build in release.yml; `twine upload` once v0.1.0 is
  cut (README install table already promises `pipx install jellytoast`).
- **Landing page** — `site/index.html` authored (frosted-dark, auto-wired
  download buttons via the GitHub releases API, Ko-fi box); august owns
  wolfgangwarehaus.com. Left (post-merge, ~10 min): Settings → Pages →
  deploy `main` `/site`, set custom domain, add DNS (4 apex A records +
  www CNAME — exact records in `site/README.md`), Enforce HTTPS, drop in
  the hero screenshot.
- [x] **Ko-fi funding** — ✅ 2026-06-12: `.github/FUNDING.yml`
  (`ko_fi: wolfgangwarehaus`) → repo Sponsor button on merge; README badge;
  landing-page tip box.
- **Microsoft Store (MSIX)** — recommended post-v0.1.0 follow-up
  (research: `distribution_channels_2026-06-12.md`): registration now FREE
  for individuals, Store signs the package (no cert purchase), Picard is
  the line-for-line blueprint. Code prep needed: autostart backend MSIX
  branch (`desktop:StartupTask` — registry Run keys don't work in MSIX) +
  exclude config from filesystem virtualization Picard-style. The
  Win32-EXE submission route is a TRAP (needs a purchased cert) — MSIX only.
- **Mint 22 deb smoke test** — one container run to confirm the
  22.04-built deb's libmpv2 dep resolves on the Noble base.
- **Decided AGAINST** (reasons in `distribution_channels_2026-06-12.md`,
  don't re-litigate): Snap Store (KWin features dead under confinement, 3
  manual plugs; revisit only on real Ubuntu demand — name registration is
  free + ~2 days if we want to squat `jellytoast`), Steam store proper
  ($100 + category mismatch), CachyOS official repos (no benefit for pure
  Python; AUR covers it), COPR/AppImage/brew (redundant with Flathub).
  openSUSE OBS parked until rpm users ask.
- **Cast-proxy demo clip** — a ~30s hero clip (Chromecast playing from a
  Tailscale-only server while the laptop is offline); pairs with the screenshots.

---

## Launch — go-to-market (post-v0.1.0)

Playbook with verified rules/links: `docs/research/community_launch_2026-06-12.md`.
Order matters: packaging → screenshots → directory listings → posts → HN.

- **NOW (account age gate):** create the alternativeto.net account —
  submissions need it ≥1 week old.
- **Screenshots — the critical-path asset.** One shoot feeds everything:
  Flathub metainfo, the Navidrome catalog, the landing-page hero, every
  Reddit post. Shot list: Library / Now Playing (lyrics or visualizer) /
  Cast menu / Downloads / Settings → Playback (bit-perfect legend) / Smart
  Playlists / Radio. Masters at 1200px; **WebP ≤500KB for the Navidrome
  catalog (thumbnail must be real UI, not the logo)**. Plus a short GIF
  (now-playing + blur + cast) for posts.
- **Directory PRs (before any social posts):**
  - [ ] **Navidrome apps catalog** — PR to `github.com/navidrome/website`:
    `assets/apps/jellytoast/` with `index.yaml` (`api: OpenSubsonic`,
    platforms linux+windows, repoUrl for the freshness badge) + WebP
    screenshots; run their `npm run validate:app` first. Tag v0.1.0 BEFORE
    this (badge reads GitHub releases).
  - [ ] **jellyfin.org clients page** — PR editing `src/data/clients.ts`;
    review Jellyfin branding guidelines first (the "jelly" name riff may
    draw comment; precedent exists).
  - [ ] **awesome-jellyfin** — PR editing `clients.yaml` (CLIENTS.md is
    generated, don't touch). ~10 min.
- **Announce, home turf first:** r/navidrome + Navidrome Discord
  (discord.gg/xh7j7yF) + **forum.jellyfin.org** (⚠️ r/jellyfin is
  permanently read-only — never plan a post there) + Lemmy
  discuss.tchncs.de/c/navidrome.
- **Wave 2:** r/selfhosted (disclosure + 90/10 ratio; frame as "client for
  your self-hosted Navidrome/Jellyfin"), selfhosted@lemmy.world, Mastodon
  (#linux #selfhosted #foss #jellyfin #navidrome). A good r/selfhosted
  post often gets picked up by the selfh.st newsletter automatically.
- **Wave 3 (staggered 1–2 weeks, rewritten per sub, never the crosspost
  button):** r/linux (release flair; only once one-command install
  exists), r/kde (lead with KWin blur/Wayland polish), r/musichoarders,
  r/opensource; r/linuxaudio only with the bit-perfect/ALSA-direct angle.
- **Show HN — one shot.** Only when Flathub is live + README is
  screenshot-rich. Framing that lands: "native, not Electron." Be in the
  comments all day.
- **Cleanup:** alternativeto.net submission ("Feishin/Sonixd alternative"
  SEO), LinuxLinks contact-form suggestion.
- **Skips (verified):** r/audiophile (bans self-promo), r/archlinux (AUR
  is the channel), awesome-selfhosted (servers only), apps.kde.org (KDE
  Incubator projects only), OpenHub/AlternativeOSS (moribund).

---

## Parked — deferred, not dropped

- **Last.fm scrobbling** — client code built + dormant in
  `jellytoast/scrobble/lastfm.py`; needs an in-app API key (signup firewall Error 406
  blocked registration). The Settings → Scrobbling Last.fm section stays hidden
  while `API_KEY`/`API_SECRET` are empty. **ListenBrainz** is the supported path
  and works today.

---

## Explicitly not on the roadmap

Deliberately out of scope — each is a fight a competitor already wins:

- **Local-file libraries** — Strawberry / Tauon territory.
- **Podcasts** — outside the music-only focus.
- **A mobile app** — Symfonium / Finamp own that space.
- **CarPlay / Android Auto** — mobile-only.

> **Note 2026-05-27.** "Heavy audiophile DSP" left this list after a Symfonium
> benchmark found the gap closeable in ~1 work-week. Parametric EQ + bit-perfect
> mode are now shipped; full convolution AutoEQ stays parked past Flathub.
