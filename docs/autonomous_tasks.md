# Autonomous tasks — queueable when stepping away

Work Claude can ship on a worktree branch without august watching.
Success measurable via tests or build output, not visual inspection.

Pair with `docs/TODO.md` (overall backlog) and
`docs/manual_test_plan.md` (visual checks).

## How to queue

Tell Claude something like:

> Pick the highest-priority autonomous task. Ship to
> `auto/<short-slug>`. Don't merge — leave the branch for review.

Each agent gets its own worktree so they don't conflict.

⚠️ **Worktree base caveat (learned 2026-05-20):** worktree-isolated
agents branch off the commit that was `main` at *session start*, not
current `main`. If earlier branches in the same session have already
merged, a later agent works against a stale tree — its diff may not
apply cleanly (A3 hit this: it edited the pre-split `cast_manager.py`
monolith and had to be hand-ported). For a chain of dependent
refactors, either fire them one-per-session, or expect to hand-port
the later ones.

## Last updated

2026-05-28 — `main` @ `503559b`, **1998 passed, 1 skipped**. This
session: AT-8/AT-9 merged, songs pagination + smart-playlist rework, a
full multi-agent audit, the cast play-dispatch wiring (DLNA/Sonos/Snapcast)
+ DLNA live-verified + LG fix, and AT-10/11/13/14 fired-and-merged (see
below). No pending `auto/*` branches remain (review-merged + cleaned up).

---

## 🔵 Fired — in flight

(Empty — AT-10/11/13/14 reviewed + merged 2026-05-28, see below.)

---

## ✅ Shipped 2026-05-28 — AT-10/11/13/14 (review-merged)

Fired as foreground worktree-isolated agents (4 in parallel) — that
worked cleanly; the old "background agents can't write" caveat is
foreground-exempt ([[feedback-background-agents-cant-write]]). Each was
reviewed (diffs walked, full suite green) then merged `--no-ff` to main;
the suite went 1875 → **1998** (+123). Branches + worktrees cleaned up.

- **AT-10** (`Merge 7baf722`) — +57 real-impl provider auth/streaming
  tests: `test_subsonic_auth.py` (salt/md5 token, `u/t/s/v/c` params,
  stream-URL) + `test_jellyfin_requests.py` (stream/playback-report
  request shape + ~15 request-builders). Closed the moat coverage gap.
- **AT-11** (`Merge 503559b`) — +63 Chromecast media-load/transport
  tests (`test_cast_chromecast.py`): MIME matrix, connect/cast, poll-loop
  branches, transport controls. Mocked Cast, no network.
- **AT-13** (`Merge ecf6472`) — `_RowDelegate` list-row cover scale now
  cached + `genres_view._GenreDelegate` fonts cached (`_build_fonts()` /
  `theme_changed`). +3 regression tests. The last two per-paint nits.
- **AT-14** (`Merge 8eda2e9`) — declared `python-xlib` (linux) in
  pyproject + `dev/install.sh`; capped `pyatv<1.0` (private rtsp API) +
  `PySide6<7.0`; cap-policy comment. ⚠️ A clean-room `pip install` to
  confirm the markers resolve on a fresh env still wants august's eyes.

---

## 🟢 Ready to fire (in priority order)

### AT-12 — Dead-code purge — RE-SCOPE before firing

⚠️ Partially stale as of 2026-05-28: the audit listed the DLNA
controller's `start_polling`/`stop_polling` as dead, but they were
**wired this session** (`cast_to_dlna`/`dlna_stop`, `44f18d5`) — do NOT
delete them. Still-dead candidates: `known_devices`, the NowPlaying
vestigial methods, `library_grid._on_view_activated` (decide wire vs
delete), and the ~10 accessors. Re-verify each with a fresh repo-wide
grep before firing (this is the [[feedback-audit-before-fire]] trap).

---

## Drained this session (older candidates)

Surfaced by the 2026-05-28 full audit; AT-10/11/13/14 fired above.
Full context lives in the "Full audit (2026-05-28)" section of
`docs/TODO.md`.

### AT-10 — Provider auth/streaming tests (HIGH value: covers the moats)

The documented differentiators are under-tested at the implementation
level. Add real-implementation tests (not just consumer fakes):

- `test_subsonic_auth.py` — assert `_auth_params` (subsonic.py:160)
  produces correct `md5(password + salt)`, fresh/long-enough salt, and
  the `u/t/s/v/c` query params; assert `get_audio_stream_url`
  (subsonic.py:897) shape.
- Jellyfin request-shape tests for `get_audio_stream_url`
  (jellyfin_api.py:453) + `report_playback_*` (jellyfin_api.py:522-585),
  stubbing `session`/`_get` the way `test_tag_editing.py` already does.
- Broaden `test_jellyfin_api.py` past the metadata LRU cache to the
  ~20 request-builders (jellyfin_api.py:290-733).

### AT-11 — Chromecast media-load / transport tests

`_chromecast.py` connect / cast / pause / seek / set_volume / stop
(:112, :180, :308, :351-377) and `chromecast_audio_mime_for` (:146)
have zero coverage — only discovery/gating is tested. Mirror
`test_cast_snapcast.py` against a fake Cast; parametrize the MIME
classmethod over the container matrix (pure, trivial).

### AT-12 — Dead-code purge (~17 verified-zero-caller symbols)

Delete the confirmed-dead methods/accessors listed in the TODO audit
section (grepped repo-wide incl. tests; each has exactly one
reference = its own def). Success = suite still green. NOTE: decide
per-symbol whether to *wire* rather than delete — e.g.
`library_grid._on_view_activated` (Enter-to-browse) and the DLNA
controller polling API are dead because a feature was never wired, not
because it's vestigial (see AT-13 / TODO).

### AT-13 — Two per-paint perf fixes (build-verifiable)

- Cache the list-mode row cover: `library_grid._RowDelegate.paint`
  (:1429-1436) re-runs a SmoothTransformation downscale + crop every
  paint; give it the `_scaled_cover_cache` the sibling `_TileDelegate`
  already has (:1014).
- Cache the genres delegate fonts: `genres_view._GenreDelegate.paint`
  (:156-160) builds QFont + QFontMetrics per tile per paint; add the
  `_build_fonts()` + `theme_changed` pattern the other 4 delegates use.
- Add a regression test (font/scale constructor spy) like
  `test_delegate_font_cache.py`.

### AT-14 — Dependency-declaration hygiene (trivial, build-verifiable)

- Declare `python-xlib` (imported at jellytoast.py:2953 for KDE
  startup-notification cleanup, undeclared in pyproject + install.sh).
- Cap `pyatv` (`>=0.17` is uncapped while airplay2.py:81 drives the
  private `pyatv.support.rtsp` API) and decide a PySide6 upper bound.
- Reconcile the cap policy (pychromecast/soco capped; zeroconf/snapcast
  not) and the lazy-vs-hard-dep modeling for pychromecast/zeroconf.

---

## 🟡 Candidates needing research first

### AT-5 — Flatpak build manifest (packaging scaffolding)

Draft the missing `io.github.augustvontrips66.jellytoast.yaml` Flatpak
build manifest. Packaging is deferred, but lining up scaffolding is
welcome. **Not yet ready to fire** — it can't be build-verified
without the `flatpak-builder` toolchain, and it needs research into
the right runtime/SDK versions, the Python dependency vendoring
strategy, and confirming the `--filesystem=xdg-data/kwin` permission
grant for `modules/drag_repaint/`. Promote to ready-to-fire once that
research is captured in `docs/research/`.

---

## 🔴 NOT autonomous — needs august

For reference, so I don't accidentally try:

- Anything where the success criterion is "looks right" (paint,
  layout, animation).
- Real-world disconnect testing.
- Real-world scrobble end-to-end.
- Cast / AirPlay / DLNA / Sonos / Snapcast device behaviour — the
  backends + discovery fan-out ship but no hardware is available to
  verify against.
- Anything involving signing in to a new server.
- Anything that affects shared state outside the repo (pushing PRs
  to GitHub on august's behalf, posting issues, modifying CI, etc.).
- Crossfade Settings UI exposure — the QSettings keys + backend
  honour are tested; the checkbox + slider visuals are not.
- Multi-server hostnames login UI — backend tested; "+ Add alternate
  URL" affordance is visual.
- Hotkey rebinding UI — `QKeySequenceEdit` per row is visual.
- Tag editing UI — right-click affordance + dialog is visual (backend
  is tested).
- Smart-playlist right-click "Create from this X" affordance — visual;
  the recipe factories that power it (`from_artist` etc.) shipped
  2026-05-20, so only the QMenu hookup remains.
- EQ preset *curve* tuning (the values themselves — math against
  source curves is fine; subjective adjustment isn't).
- Capturing screenshots for Flathub submission.
- Writing AUR PKGBUILD content (mechanical but needs maintainer
  judgement on optdepends + post-install hooks — bundle with august).

---

## ✅ Recently shipped (paper trail)

**2026-05-27 — AT-6 + AT-7 merged**:

- `auto/test-coverage-sweep-2` (AT-6, `cc90700`) — +29 focused
  Qt-fixture tests for `single_instance.py`, `cast_manager/_common.py`,
  and `login_view._UrlRow` / `_AlternateUrlsDialog`. No production
  change. 1695 → 1724.
- `auto/dpr-unify` (AT-7, `169cea9`) — DPR-research rollout from
  `docs/research/dpr_cache_keys.md`. Fixed `server_px` for
  `search_view` (132), `artist_page` header + tiles (540),
  `now_playing_bar` live + prefetch (324 via new `_BAR_SOURCE_PX`
  constant), and folded `songs_view` from `dpr_bucket()` to the
  same pattern for cross-surface L2 hits with `search_view`. +6
  tests verify each site's `get_image_url` size is DPR-invariant.
  Radio cover (`now_playing_bar.py:2133`) left alone — its L2 raw
  key is the URL itself, no DPR fragmentation. 1724 → 1730.
- Both branches were built unattended in the prior session,
  reviewed + merged this session.

**2026-05-22 — AT-3 / AT-4 built + merged**:

- `auto/bulk-tag-edit-backend` — `update_album_track_metadata`
  provider method (base stub + Jellyfin override). Enumerates the
  album, writes each track through the single-track path,
  fault-tolerant per track, returns `{album_id, succeeded, failed,
  total}`. Subsonic unsupported. Mocked-HTTP tested (+6).
- `auto/test-coverage-sweep` — `test_sort_utils.py`,
  `test_disk_cache.py`, `test_platform_compat.py` for three
  zero-coverage pure-helper modules (+55, test-only).
- Both done in a foreground session — background Agent runs were
  fired first but couldn't write files (no interactive permission).
- Suite: 1631 → 1692.

**2026-05-21 (PM) — AT-1 / AT-2 merged**:

- `auto/cover-art-upload` — Jellyfin `upload_cover_art` (base64-body /
  image-mime request shape via `JellyfinAPI.upload_primary_image`),
  mocked-HTTP tested. No UI yet; live-server check still pending.
- `auto/theming-blur-tests` — `test_theme.py` + `test_blur.py`,
  +57 tests for the theming rework + blur subsystem (test-only).
- Suite: 1533 → 1597.

**2026-05-20 (PM) — context-menu pickup + doc audit**:

- "Create smart playlist" + "Start radio" right-click entries wired
  into the song / album / artist / genre menus; the dead context-menu
  installer layer removed (+13 tests). Done in the main session, not
  on an `auto/*` branch.
- `auto/smart-rule-schema-v2` — `date_added` / `last_played`
  smart-playlist rule fields, run unattended (+42 tests). Merged to
  `main` 2026-05-20 and verified against live Jellyfin / Subsonic
  servers; the editor integration + a Jellyfin full-library-fetch
  timeout were fixed during that verification.
- Full docs audit + rewrite (this doc set).

**2026-05-20 session — A1-A6 round, all merged to `main`**:

- `auto/cast-dlna-split` — 1188-LOC `cast/dlna.py` → 9-file subpackage.
- `auto/cast-manager-split` — 794-LOC `cast_manager.py` → package
  (`_ChromecastMixin` + `_AirplayMixin` + thin orchestrator).
- CastManager DLNA / Sonos / Snapcast discovery fan-out — hand-ported
  from `auto/cast-manager-discovery-fanout` (stale-base, see caveat
  above); landed as `_OtherProtocolsMixin` (+15 tests).
- `auto/radio-entry-points-parity` — album / artist / genre right-click
  "Start radio" (+8 tests).
- `auto/smart-playlist-backend-hardening` — recipe factories, schema
  additions (`is_favorite` / `starts_with` / `ends_with` / random
  sort), `schema_version` persistence (+71 tests).
- `auto/ruff-cleanup-2026-05-19` — 11 lint findings cleared.
- Suite: 1348 → 1442.

**2026-05-18 → 2026-05-19 — B-round + visualizer / smart playlists /
internet radio direct to `main`**:

- **B1 — Visualizer paint widget** → shipped, upgraded bars-to-Bezier
  on 2026-05-19. Audio tap via `pw-record --target=jellytoast` +
  `parec` fallback.
- Direct-to-main 2026-05-19: smart playlists end-to-end, internet
  radio UI, mini-player volume slot, EQ Settings UI, downloads
  tile/cover overlays.

**2026-05-18 session — 9 `auto/*` branches + 2 research docs**:
- offline-phase6-wifi-only, offline-phase6-downloads-ui,
  font-token-cleanup, smart-playlist-presets, notifications-backend,
  radio-feeder, crossfade-v1-backend, backend-package-tests,
  qss-parse-fix.
- Research: `visualizer_rendering.md`, `provider_abstraction_cleanup.md`.

**2026-05-17 (round 1-4 + Phase D + Phase E)** — A1-A26 shipped
through 11 merges. `git log --oneline | grep -E "^[0-9a-f]+ (A[0-9]+
|Merge auto/)"` is canonical for historical detail.
