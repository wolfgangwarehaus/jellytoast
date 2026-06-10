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

2026-06-09 (autonomous run) — branched off `main` @ `91ec327` (suite
**2756**, ruff clean). A fresh multi-agent audit (13 finder lanes +
adversarial verify + 4 docs lanes) refilled the queue with **21 confirmed
test/build-verifiable findings**, all implemented across **7 `auto/*`
branches** + an `auto/docs-verification` branch, then **all 8 MERGED to
`main` this session** (`--no-ff`; integrated suite **2796** green, ruff
clean; one test-only conflict in `test_theme_restamp.py` resolved by
keeping both new classes). +40 tests. Pushed to origin in the 2026-06-09
wind-down (origin/main == main). The 2
deferred P3 low-bugs (`cast_toggle_pause` off-thread flag, mixed-DPI icon
bake) stayed deferred (hardware/visual). Per-fix detail in `CHANGELOG.md`
(2026-06-09). Run in the MAIN session (bg agents can't write —
[[feedback_background_agents_cant_write]]).

2026-06-02 (autonomous run) — `main` @ `df78434`, **2396 passed**, ruff
clean, **CI green** (pytest 3.11/3.12/3.13 + wheel build/import +
pip-audit). Four branches built/verified/merged+pushed this run, all as
direct main-session work (the move-and-reexport recipe, NOT worktree
agents — see [[reference_god_file_decomposition]]): **`volume_button.py`**
(`VolumeButton`+popups out of `now_playing_bar`, 2591→1384, kills
mini_player's transitive bar import); **`np_track_list.py`** (the
track-list MVC stack) + **`download_button.py`** (`_DownloadButton`),
both out of `now_playing_page` (**4064→2389, −41%**); and a **2-fix P0
correctness batch** (silent cast auto-advance failure now logs;
`library_grid.load_items` bumps the load-gen before the offline
short-circuit). +4 tests. Merged `--no-ff`, branches deleted post-merge.

2026-06-01 (pm) — `main` @ `82d8df5`, **2344 passed**, ruff clean (now
with flake8-bugbear `B`). The 23-pass comprehensive audit (overall B+;
report `docs/code_audit_2026-06-01.md`, roadmap in `docs/TODO.md`) drove
**all of AT-15…AT-20 — built, verified, and MERGED to `main`** this
session (local; not pushed). AT-15 also patched a live CVE the new
pip-audit gate surfaced (`zeroconf` → `>=0.149.5`,
CVE-2026-47180/47183/47184). **AT-18 + AT-19** hit the worktree
stale-base trap (the Agent tool forks *session-start* `main`), so they
were rebased onto the post-merge tree — one banner conflict in
`now_playing_bar.py` resolved in favour of AT-20's `SECTION_LABELS` fix —
re-verified under ruff `B`, then merged. Deferred GUI eyeballs from AT-20
logged in `manual_test_plan.md` (favorite heart in live mode; cast banner
label on DLNA/Sonos/Snapcast).

2026-06-01 (am) — three cast fixes merged + hardware-verified (DLNA/Sonos
initial volume, Chromecast-under-Tailscale discovery + host-based connect).

2026-05-28 — `main` @ `503559b`, **1998 passed, 1 skipped**: AT-8/AT-9
merged, songs pagination + smart-playlist rework, a full multi-agent
audit, the cast play-dispatch wiring + DLNA live-verify, and
AT-10/11/13/14 fired-and-merged (see below).

---

## 🔵 Fired — in flight

(Empty — the 2026-06-09 audit batch all merged 2026-06-09; the 8-branch
record is just below.)

---

## ✅ Shipped — 2026-06-09 audit batch (8 branches, merged `--no-ff`, not yet pushed)

21 audit findings + docs reconciliation, integrated suite **2796** green
(per-fix detail in `CHANGELOG.md` 2026-06-09):

- **`auto/queue-state-integrity`** — `Queue.current_item` inner-index
  crash guard (corrupt `queue.json` crashed boot) + stale-`NowPlaying` on
  queue clear/tail-remove + dead `notify_track` signal removed. +4 tests.
- **`auto/offline-sync-ghost`** — library-sync that dispatches nothing
  (all already downloaded / cancelled) left the aggregate "0 of N" + the
  persisted flag stuck for the session; reset guarded. +2 tests.
- **`auto/now-playing-page`** — live idle-ink restamp + real dpr cover
  refresh (was a dead `load_preview` round-trip) + `_on_row_clicked` /
  `_items_span_multiple_artists` coverage. +12 tests.
- **`auto/theme-token-correctness`** — mini-player toggle glyph `_jt_icon`
  property + ACCENT_DEEP unified on `theme._darken` + `import_palette`
  honours explicit accent-followers. +3 tests.
- **`auto/library-search-fixes`** — artist-page async cover-bleed guard +
  direction-aware `merge_paged` reverse over-fetch + `_name_score` tests.
  +11 tests.
- **`auto/playback-crossfade-cast`** — clear mute on cast-stop + drop dead
  `Crossfader._duration_ms`. +3 tests.
- **`auto/cast-scrobble-provider`** — bounded `cc.wait(timeout=5)` (pool
  leak) + evict all-malformed scrobble head + invalidate played-cache. +5
  tests.

Plus **`auto/docs-verification`** — the doc reconciliations (portable_blur
§5/§8 Acrylic-default banners, offline/scrobbling banner-vs-body, packaging
extras claim) + this run record.

---

## ✅ Shipped — AT-10 … AT-20 + AT-12 (review-merged)

The whole 2026-05-28 → 2026-06-01 autonomous backlog (AT-10/11/12/13/14
then the 2026-06-01-audit batch AT-15…AT-20) is merged to `main`. The
per-task detail lives in `CHANGELOG.md` and in git
(`git log --oneline | grep -E 'AT-1[0-9]|AT-20'`); this file only tracks
what's still queueable, so the finished specs are dropped rather than
kept as a second copy.

---

## 🟢 Ready to fire (in priority order)

(Empty — the 2026-06-01-audit queue AT-15…AT-20 plus the older
AT-10/11/12/13/14 all shipped; see CHANGELOG.md / git for detail.)

To refill: run a fresh audit (the last full one is
`docs/archive/code_audit_2026-06-01.md`, roadmap in `docs/TODO.md`), pick the
test- or build-verifiable findings, and list them here as
`### AT-NN — <title>` with a one-paragraph spec, success criterion, and
an `auto/<slug>` branch name. Each ships to its own worktree, full suite
+ ruff green, **not merged** — left for review.

---

## 🟡 Candidates needing research first

### AT-5 — Flatpak build manifest (packaging scaffolding)

Draft the missing `io.github.wolfgangwarehaus.jellytoast.yaml` Flatpak
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
