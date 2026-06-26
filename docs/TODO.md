# jellytoast — what's left to do

The running backlog. Closed work collapses to one-liners; the dated detail lives
in `docs/CHANGELOG.md`, the release pipeline in `docs/RELEASING.md`, and the rest
in this file's git history. **Last tidied 2026-06-24** (post-macOS, 0.1.4 prep).

> **Status — 🍎 0.1.4 in prep (the macOS release).** v0.1.3 is Latest
> (2026-06-21). **0.1.4** rolls up the macOS arc: a Developer-ID-signed,
> **notarized `.dmg`** with full native integration (media keys / Now Playing,
> vibrancy, global menu bar + Dock menu, integrated titlebar, notifications,
> launch-at-login, Reduce-Transparency), verified on macOS 26 Tahoe. Cut with
> `dev/cut_release.sh 0.1.4 --push` once dependabot #179 lands.
>
> **Shipped (detail in CHANGELOG):** native Windows integration + packaging
> (#85/#86), the release/CI workflow + `cut_release.sh` + branch protection, the
> P0–P3 bug-hunt sweeps, the v0.1.0 launch, the unified multi-channel pipeline,
> the v0.1.2 `.deb` X11 fix (#164), and v0.1.3 (AppImage + try-a-demo).

## Install channels — all LIVE (macOS `.dmg` arrives in 0.1.4)
**Microsoft Store** (ID `9PNLTPXGHN79`) · **winget** (`wolfgangwarehaus.jellytoast`)
· **`.deb`** · **AppImage** · **PyPI** (`pipx install jellytoast`) ·
**macOS notarized `.dmg`** · landing page + Ko-fi. One publish already
auto-fans-out to **PyPI + winget** and builds the signed `.dmg`. Microsoft Store
updates now build the `.msix` in CI and **attach it to the release** for a quick
manual Partner Center upload (Option B, no Windows box); fully hands-off Store
auto-submit (Option A) is gated on a **future move to a Company account** —
tracked in `packaging/msix/COMPANY-ACCOUNT.md`.

## august's eyes-on checklist
*Live-verification tasks from the 2026-06-11 round — still unverified, and they
predate the macOS / Windows / AppImage work, so they're due one fresh pass:*

- [ ] §-1 audio output re-walk (picker works; Linux first, then Windows — both
      pipx installs need a refresh first, see the CHANGELOG note)
- [ ] F1 Search results + Suggestions right edge — clean frost/body, no black
      strip, all 4 themes
- [ ] View dropdown: current tab pre-highlighted on open
- [ ] F2 design call: mini-player button is checkable but only ever opens
      (toggle-close vs plain button; `queue_btn` naming drift)
- [ ] §1 smart-playlist remainder on Subsonic: Save / Save & Play / provider grey-out
- [ ] Settings → General → "Launch at login" still visible AND functional on
      CachyOS across a reboot (the `is_supported()` gate must not hide it on Linux)

Priorities: **P0** confirmed bugs → **P1** medium → **P2** tidy → **P3** low /
features → **P4** hardware-gated / cross-platform → then release pipeline,
packaging, launch, parked, not-on-roadmap.

Companion docs: `docs/SPEC.md` (what the app does), `docs/CHANGELOG.md` (shipped,
dated), `docs/RELEASING.md` (the release pipeline), `docs/decisions.md` (why).

---

## P0 — confirmed behaviour bugs

✅ **ALL FIXED & MERGED**, each with a regression test (crossfade+skip near-silent
volume, failed-cast dead playback, shuffled-queue wrong-row remove, color-token
Reset drift, blocking primary climb-back probe). Write-ups: this file's git history.

## P1 — medium behaviour bugs

✅ **ALL 8 FIXED & MERGED** (stale top-bar menu colours, mini-player
`playback_restored`, replayed-pause play icon, `_compute_subtitle` delegate
crash, genres blank-grid refresh, AutoEQ Q-preserve no-op, stale MPRIS
shuffle/loop, AirPlay zeroconf + pairing-loop leaks). Write-ups: git history.

## P2 — tidy / cleanup

- ⏸️ **Mixed-DPI icon bake** (`jellytoast/icons.py` + `icon_button.py`) — pixmaps
  baked at app-DPR but painted at widget-DPR; blurry only on a mixed-DPI
  multi-monitor setup (a no-op on single-DPI). The fix touches the core icon
  paint path. **HARDWARE-GATED** — verify on actual differing-DPR monitors
  before landing (Approach B: a `svg_pixmap(name,color,size,dpr)` helper + an
  opt-in `IconButton.set_glyph`).
- [ ] **Bare-`QDialog` theming parity (low):** the Last.fm connect modal
  (`settings_dialog._lf_open_auth_modal`, gated behind an unshipped API key) and
  `SnapcastControlDialog` (hardware-gated) are still bare `QDialog` — bring to
  `FrostedDialog` parity when surfaced. *(The colors-page CRUD now has a wired UI
  entry point and is a settings page, not a dialog — dropped from this list.)*

## P3 — low-severity bugs + features

✅ **16 of 18 low bugs fixed 2026-06-08** (regression-tested; list in git history).
Still deferred:

- `cast_toggle_pause` flips `_cast_paused` even when the off-thread SOAP
  pause/resume fails — needs the DLNA/Sonos pause methods to report success
  (cross-thread, hardware path). *(Same root as the P4 cast-hardware item.)*

### Features (not yet pulling weight)

- **A registered Cast receiver app** — Chromecast screens show "Default Media
  Receiver" not "jellytoast"; needs a $5 Google dev account + a hosted receiver.
- **AirPlay 2 edge cases** — older LG webOS TVs / shairport-sync 5.x misbehave.
- **`QNetworkInformation` supplementary network-status signal** — flaky on Linux.
- **Importing server-side playlist files (m3u, …)** — likely out of scope unless asked.

## P4 — hardware-gated / cross-platform

- **Windows native integration — SHIPPED** (#85/#86, verified on Win 11:
  autostart, SMTC media keys + flyout, toasts, taskbar overlay badge,
  prevent-sleep, single-instance foreground, HiDPI + Acrylic blur + borderless).
  Tails:
  - **Native ARM64 build** — we ship **x64 today** (runs on ARM64 Windows via
    emulation, fine for now), but a native ARM64 build would serve the growing ARM
    Windows market. Needs ARM64 Python + `libmpv-2.dll` + PyInstaller on ARM64
    hardware → ship an `.msixbundle` with x64 + arm64 slices (see
    `packaging/msix/WINDOWS_SESSION.md` Phase 6).
  - *(LOW)* visualizer track-switch latency on WiFi (bars wait for the full
    compressed body; a two-phase Range fetch is risky vs the buffer-complete
    invariant); construction-time icon baking (a lazy `QIconEngine` was tried +
    **reverted** — softened glyphs at fractional scale; the baked path stays).
- **macOS — SHIPPED** (#177/#181: notarized `.dmg` + native integration). Tails:
  - **Intel Mac support (universal2)** — the `.dmg` is **arm64-only today**, so it
    will NOT run on Intel Macs. Ship a **universal2** (arm64 + x86_64) `.app` so both
    architectures are covered. Needs an x86_64 Python + `libmpv` in the `build-macos`
    job (see the note in `.github/workflows/release.yml`).
  - **Mac App Store track** (PR #178/#180, `needs:mac`) — LGPL libmpv proven; Apple
    certs + MAS secrets + the `build-mas` job in place; 0.1.3 build in Apple review.
- **Back-port the macOS arc into `dough`** — the native-integration work (media
  keys / Now Playing, `NSVisualEffectView` vibrancy + Reduce-Transparency fallback,
  native menu bar + Dock menu, integrated titlebar, Notification Center,
  launch-at-login, the faux-frost fallback) plus the sign / notarize + MAS-sandbox
  packaging pipeline was a lot of hard-won work. Fold the reusable pieces into the
  `dough` cross-platform base (the "develop in jellytoast → re-inject into dough →
  next apps" plan) so future apps inherit them instead of re-deriving them.
- **Cast hardware verification** — the cross-thread `active_cast`/`_cast_paused`
  write-race in `_CastTransportMixin` needs a live cast session to verify safely;
  **Sonos / Snapcast** are wired but unexercised on real hardware (Chromecast /
  AirPlay / DLNA already live-verified); Sonos out-of-band pause is undetectable
  (the 500ms status poll covers only Chromecast + DLNA; `SonosEventBridge` push
  is unwired).
- **Exclusive audio output (ASIO)** — Windows-only; only if a Windows user asks.
- Optional: a Windows **WASAPI** shared-mode loopback visualizer backend — the
  cross-platform `QtDecodeTap` already covers every OS, so this is an extra, not
  a parity gap.

---

## Release pipeline — streamline the update process (🎯 active)

One `dev/cut_release.sh X.Y.Z --push` → a draft GitHub release with every
artifact → one **publish** click. That already auto-fans-out to **PyPI + winget**
and builds the **signed, notarized macOS `.dmg`**. The goal: make **every**
channel auto-update from that single publish. Remaining wiring (canonical
pipeline reference: `docs/RELEASING.md`):

- [ ] **Microsoft Store** — automate *updates* via the `msstore` CLI / Store
  submission API (`microsoft/microsoft-store-apppublisher`) now the product is
  live; the free-product tier qualifies. **Cert-gated** (up to 3 business days,
  like the Mac App Store — *not* instant like winget). Sequenced after: (1) the
  3 MSIX code blockers verified on the Win 11 box, (2) confirming a **free
  individual Partner Center account can register the Azure AD app** (the
  load-bearing unknown — check first). The first (manual) submission is done.
- [ ] **AUR** — add `AUR_SSH_PRIVATE_KEY` + do the one-time AUR import (gated on
  Arch registration reopening); `aur.yml` already fires on publish.
- [ ] **Windows `.exe` signing** — add the `AZURE_*` secrets (Azure Artifact
  Signing, ~$10/mo) so `build-windows` signs; clears the *direct-download*
  SmartScreen warning (the Store copy is already covered by Microsoft's re-sign).
- [ ] **winget** — verify the first *automatic* (non-backfill) publish fires on
  0.1.4 (only the manual `workflow_dispatch` backfill has run so far).
- [ ] **Mac App Store** — Apple certs + the 7 MAS secrets + a `build-mas` job
  (PR #178/#180; the LGPL-libmpv showstopper is already solved).

---

## Packaging — channels

✅ **DONE / LIVE** (detail in `docs/RELEASING.md` + CHANGELOG): PyInstaller spec,
**`.deb`** (X11 closure fixed in v0.1.2 #164; CI smoke-tests Ubuntu 24.04/26.04 +
Debian every release), Windows Inno installer + portable zip, **AppImage** (#169),
**winget** (auto-submit), **PyPI** (OIDC Trusted Publishing), **Microsoft Store**
(live; submission manual by design — Microsoft re-signs), **macOS `.dmg`** (signed
+ notarized; all `APPLE_*` secrets set), landing page, Ko-fi. **Flatpak / Flathub
RETIRED** (#168 — Flathub bans AI-assisted code; don't re-litigate or cold-submit).

🔵 **OPEN** (the auto-update wiring is in the streamlining item above):
- **AUR** + **chaotic-AUR** — dormant (needs the SSH key + Arch registration; the
  chaotic-AUR `[Request]` issue follows once AUR is live).
- **Windows `.exe` Azure signing** — dormant (needs the `AZURE_*` secrets).
- **Mac App Store** spike — PR #178/#180 (`needs:mac`).
- **Cast-proxy demo clip** — a ~30s hero clip (Chromecast playing from a
  Tailscale-only server while the laptop is offline); pairs with the screenshots.

**Decided AGAINST** (don't re-litigate): Snap Store (KWin features dead under
confinement), Steam ($100 + category mismatch), CachyOS repos (AUR covers pure
Python), COPR/brew; openSUSE OBS parked until rpm users ask.

---

## Launch — go-to-market (refreshed 2026-06-22)

Order: directory listings → home-turf posts → Show HN → broader waves.
**The install story is COMPLETE** (Microsoft Store + winget + .deb + AppImage +
PyPI all live; macOS .dmg in 0.1.4), so the Show HN gate is cleared. Ready-to-submit
entries live in `docs/launch-listings/`; copy-paste post drafts in
`docs/launch-listings/launch-posts.md`. All venue schemas/rules re-verified 2026-06-22.

- [x] **Screenshots — DONE (#107)**; landing-page carousel live. Optional: a short GIF
  (now-playing + blur + cast) for social posts.

**Directory PRs — do first (the posts link to these); none submitted yet. Entries drafted in `launch-listings/`:**
  - [ ] **Navidrome apps catalog** → `navidrome/website`, `assets/apps/jellytoast/`
    (`api: OpenSubsonic`; `repoUrl` drives the badges). Run `npm run convert:images
    jellytoast` + `npm run validate:app jellytoast`. Template: the `aonsoku` entry.
  - [ ] **awesome-jellyfin** → `assets/clients/clients.yaml` on `main` (NOT the generated
    `CLIENTS.md`); `types: [Music]`; `github` type needs separate `owner:`/`repo:` keys;
    alphabetical order (a sort-check bot enforces it); Conventional Commit. ~10 min.
  - [ ] **Awesome-SelfHosted-Music** → `Tal0na/Awesome-SelfHosted-Music-Awesome`,
    `Servers-Clients/linux.md` + `windows.md` (new — a self-hosted-music list that *does*
    take clients, unlike awesome-selfhosted).
  - [ ] **jellyfin.org clients** → `src/data/clients.ts` on `master`; Store+Releases in
    `primaryLinks`, omit `recommended`. The "jelly" name may draw a branding comment.
  - [ ] **AlternativeTo** (web form; account must be ≥1 wk old — **create it NOW**):
    category *Audio Player*, then mark as an alt to Feishin/Supersonic/Sonixd.
  - [ ] **selfh.st/apps** (email `hello@selfh.st`): companion-**client** listing → auto-feeds
    the Self-Host Weekly newsletter via the generated release RSS.

**Announce — home turf first:** r/navidrome · **forum.jellyfin.org → Client Development**
  board (⚠️ r/jellyfin is permanently read-only — confirmed; the forum is the Jellyfin
  venue) · Navidrome GitHub Discussions "Show and tell" · Lemmy (selfhosted@lemmy.world,
  navidrome@discuss.tchncs.de, jellyfin@lemmy.world).

**Discords (verified invites; post once in #showcase/projects, read #rules first):**
  Sonixd/Feishin `discord.gg/FVKpcMDy5f` (the exact audience — be collegial, it's the
  peer server) · /r/SelfHosted Chat `discord.gg/UrZKzYZfcS` · Navidrome `discord.gg/xh7j7yF`
  · Homelab `discord.gg/homelab`. ⚠️ Jellyfin Discord needs mod OK for self-promo (no
  #showcase) — prefer the forum.

**Show HN — one shot (now unblocked).** `Show HN: jellytoast – a native Qt music player
  for Jellyfin and Navidrome`; lead "native, not Electron"; no version-bump framing; be in
  the comments all day; Tue–Thu AM ET. Draft in `launch-posts.md`.

**Broader waves (staggered, rewritten per sub, never the crosspost button):** r/selfhosted
  (flair REQUIRED → **"Release"**, *not* "Release (No AI)"; disclose authorship) · r/kde
  (KWin blur / Wayland shots) · **r/musichoarder** (singular, ~48k) · r/linux ("Software
  Release" flair) · r/opensource · r/unixporn (theme shots) · r/SideProject ·
  r/coolgithubprojects · Mastodon (#Jellyfin #Navidrome #SelfHosted #FOSS #Linux) ·
  OMG!Linux / OMG!Ubuntu tip lines · Changelog News · LinuxLinks.

**Skips (verified):** r/audiophile (bans self-promo) · r/archlinux (AUR is the channel) ·
  awesome-selfhosted (servers only) · console.dev (devtools only) · Lobsters (invite-only) ·
  subsonic.org / OpenSubsonic site list / LibHunt / BetaList (moribund or low-fit) · the
  Self-Hosted *podcast* Discord (a listener community, not a showcase — don't confuse it
  with /r/SelfHosted Chat).

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
> mode are now shipped; full convolution AutoEQ stays parked for now.
