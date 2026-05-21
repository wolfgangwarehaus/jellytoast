# Architecture decision log

Brief notes capturing significant architectural choices. Each entry
records *context → decision → alternatives → revisit triggers* so a
future-us can answer "why is it like this?" in 30 seconds.

Newest first. Lightweight ADR format — no formal numbering, just the
date so the order is obvious.

---

## 2026-05-21 — Drag-blur artifact fixed via a bundled KWin scripted effect

**Context:** Dragging a blurred jellytoast window on KDE Wayland left
stale "line artifacts" — KWin bug 455526/457727, the blur cache going
stale on the optimized partial-damage render path. Two research passes
confirmed: (a) it's unfixable from the Qt/Wayland-client side — a
client can't detect its own interactive move and client damage isn't
compositor damage; (b) a *transformed* window is rendered through
KWin's full-repaint path, which sidesteps the bug (this is why enabling
Wobbly Windows hides it). The spike found a static `set()` transform is
re-optimized by KWin — only an *in-progress* `animate()` keeps the
window repainting every frame.

**Decision:** Ship a tiny KWin **scripted effect** (`modules/drag_repaint/`,
plain `metadata.json` + `main.js` — no compiled code) that holds a
jellytoast window under an imperceptible in-progress transform for the
duration of a drag, plus `WindowForceBlurRole` so the blur survives it.
Installed into the user's KWin effects dir + loaded over D-Bus at
startup, unconditionally (no Settings toggle — it's a pure correctness
fix, not a preference, and matches how `keep_above`'s no-border rules
already write KWin config without a toggle). `JT_NO_DRAG_REPAINT=1` is
a support escape hatch.

**Alternatives:** Compiled C++ KWin effect — robust, but per-KWin-ABI
and can't ship inside a Flatpak. `WindowForceBlurRole` via the
`kwin-effects-forceblur` fork — changes the blur decision, not the
render path; doesn't fix it. Tell users to enable Wobbly Windows —
works, but global and unshippable. App-side opaque-while-dragging — not
implementable; the client can't detect a compositor-driven move.

**Revisit if:** KWin fixes 455526/457727 upstream (then the effect
becomes dead weight and can be dropped). The Flatpak build needs
`--filesystem=xdg-data/kwin` for the effect install to work sandboxed.

## 2026-05-20 — Context menus are built inline per view, not via shared installers

**Context:** `ui_helpers.py` carried a set of "installer" helpers
(`install_song/album/artist/genre_context_menu`) meant to let any view
opt into a right-click menu with a single call. In practice all three
music grids (LibraryGrid, GenresView, SongsView) built their context
menus inline in their own `contextMenuEvent` instead — the installers
sat unused with zero call sites.

**Decision:** Drop the installer layer. Each view builds its own
context menu inline. The pieces that are genuinely shared —
`start_seed_radio()` and `open_create_smart_playlist()` — stay in
`ui_helpers.py` as plain functions the inline menus call.

**Alternatives:** Retrofit the views onto the installers — would mean
reworking three working views to adopt an abstraction they had already
voted against by never using it. Keep the dead installers "for later"
— they would keep drifting out of sync with the real menus.

**Revisit if:** A future surface needs a menu identical to an existing
one — at that point extract the shared body, but from two real call
sites, not speculatively.

## 2026-05-17 — Heavyweight per-feature deps ship as optional extras

**Context:** Today's cast batch (A22/A23/A24) and visualizer FFT
backend each landed with a new runtime dep declared as a hard
dependency: numpy (visualizer), async-upnp-client (DLNA), soco
(Sonos), snapcast (Snapcast). All four are big, all four are useless
without hardware / opt-in, and all four pad the Flatpak bundle.

**Decision:** Each becomes a `[project.optional-dependencies]` extra
(`visualizer`, `dlna`, `sonos`, `snapcast`). The matching module
soft-imports the dep behind an `_ensure_<dep>()` gate and stays
dormant when missing. The base install carries only the truly-needed
runtime deps; users opt in with `pip install jellytoast[dlna,sonos]`.

**Alternatives:** Keep as hard deps — bloats every install for a
feature most users won't enable. Lazy-import without an extra (let
the user pip-install the lib themselves) — works but undiscoverable;
the extras name self-documents the feature.

**Revisit if:** A protocol turns out to be near-universal on user
hardware (then it graduates to hard). Or the extras explode in count
(>5-6) and become a UX maze — then we revisit the bundling strategy.

## 2026-05-17 — Snapcast ships Option B (control surface) only in v1

**Context:** Per `docs/research/casting_snapcast.md`, Snapcast offers
two integration shapes: Option A is true audio routing (mpv →
snapserver pipe — the user replaces their existing audio setup),
Option B is a control surface for an existing snapserver (groups,
clients, volume, stream switching).

**Decision:** v1 ships Option B only. The library, the cast dialog,
and the cast menu all treat Snapcast as a "remote DJ" surface —
jellytoast doesn't try to inject audio. Option A (Linux-only
experimental) is deferred to v1.5 with august's hardware.

**Alternatives:** Skip Snapcast entirely — leaves Linux multi-room
users with nothing. Ship Option A first — requires audio-device
contention handling and a much bigger Settings surface.

**Revisit if:** Significant user demand for Option A routing, or
v1.5 audio-device contention work makes it tractable.

## 2026-05-17 — Cast settings get their own Settings page

**Context:** Per [[feedback-cast-settings-own-tab]]. Cast settings
were nested under Playback. A25 added 5+ new keys (per-protocol
toggles, discovery timing, stream routing already there).

**Decision:** Settings → Casting becomes its own sidebar entry.
Includes: per-protocol enable toggles (Chromecast, AirPlay, DLNA,
Sonos, Snapcast), discovery timing radio (startup vs on-demand),
cast-stream routing combo.

**Alternatives:** Keep nested under Playback — would push Playback's
page past the comfortable height. Hide protocol toggles in an
"Advanced" expander — discoverability hit.

**Revisit if:** The page itself gets too tall (>10 keys); split
into "Casting" + "Casting (advanced)".

## 2026-05-17 — `pyproject.toml` flat layout, not `src/jellytoast/`

**Context:** Pre-packaging, the repo lacked a `[build-system]` table
(comment said "not pip-installable as-is"). The original plan was to
move `modules/` → `src/jellytoast/` and add the build-system. AUR +
Flathub both need a buildable wheel.

**Decision:** Flat layout. `modules/` package tree stays at repo root;
`jellytoast.py` stays at repo root as a single top-level module
(declared via `[tool.setuptools] py-modules`). `gui-scripts` entry
point exposes `jellytoast`. Wheel builds cleanly with no import
changes anywhere in the tree.

**Alternatives:** Move to `src/jellytoast/` — would have touched every
`from modules.X import Y` in the repo and every test path. Out of
scope for a cleanup pass; an isolated migration later if needed.

**Revisit if:** We start shipping additional console scripts or a
proper PyPI release where the namespace package matters more than
the dev-launch ergonomics.

## 2026-05-17 — `pyproject.toml` is the single source of truth for deps

**Context:** Both `pyproject.toml` and `requirements.txt` declared
deps; they had already drifted (pychromecast `<16` ceiling missing
in requirements.txt).

**Decision:** Drop `requirements.txt`. The AUR PKGBUILD and Flatpak
manifest both read from pyproject.toml. The dev-install path
(`bash dev/install.sh`) explicitly pip-installs the few packages
not in the Arch repos.

**Alternatives:** Keep both in sync via a generator — adds tooling
for no UX gain. Use a lockfile (pip-tools / uv) — overkill for the
current install audience.

**Revisit if:** We start producing reproducible-build artifacts that
need a lockfile.

## 2026-05-17 — Pre-commit + ruff hooks scaffolded, not auto-installed

**Context:** Format + lint drift was accumulating across the
autonomous-agent rounds. Each branch picked its own style.

**Decision:** `.pre-commit-config.yaml` wires `ruff` (lint + `--fix`)
and `ruff-format` from `astral-sh/ruff-pre-commit`. Lint rules
declared in `pyproject.toml [tool.ruff.lint]`; the hook doesn't
duplicate them. Hooks are opt-in via
`pip install pre-commit && pre-commit install`.

**Alternatives:** Install hooks automatically on first launch — fails
quietly in the absence of `pre-commit`; surprises users who don't
expect their commits to be reformatted. Skip hooks entirely — drift
keeps accumulating.

**Revisit if:** A second contributor joins (hook config maybe moves
to CI enforcement).

---

## 2026-05-15 — Tracking docs live in-repo, not in memory

**Context:** Tracking todos / tests / autonomous work via session
memory worked while scope was small but scale-fragile.
**Decision:** Three Markdown docs in `docs/` (`TODO.md`,
`manual_test_plan.md`, `autonomous_tasks.md`), with priorities
(P0-P4) and effort tags (S/M/L/XL). Memory entries point at them but
don't duplicate content.
**Alternatives:** Issue tracker (GitHub Issues) — overkill for a
solo project; loses local-edit speed. Memory-only — doesn't survive
the truncation cliff.
**Revisit if:** Sharing happens beyond august (collaborators need
public-facing tracking), or if any doc exceeds ~400 lines.

## 2026-05-15 — Autonomous agents ship to `auto/*` worktree branches, never merge

**Context:** Logic-only tasks (bug fixes, tests) suit unattended
work; visual tasks don't.
**Decision:** Spawn `Agent({isolation: "worktree", ...})` with
explicit instructions to commit + leave the branch local. august
reviews + merges manually.
**Alternatives:** Direct commits to `main` — too high-risk for
something august hasn't eyeballed. PRs — premature for a solo repo.
**Revisit if:** A second contributor joins (then real PR review).

## 2026-05-15 — Research before implementation, doc lives in `docs/research/`

**Context:** P1/P2 parity features (EQ, smart playlists, etc.) each
have non-trivial design space.
**Decision:** Spawn one research agent per feature, ~1500-word doc
each, sectioned predictably. Doc is input to the next pass — TODO
items reference back to it.
**Alternatives:** Design-as-you-implement — fine for tiny features,
risks bad first-iteration architecture at this scope.
**Revisit if:** Features start landing without a research doc and
quality holds — means design was obvious enough not to bother next
time.

## 2026-05-15 — Offline chip in top bar, not full-width banner

**Context:** First Phase 5 UI iteration was a 30px accent-tinted
strip below the top bar. august wanted "more subtle, rounded square,
and a connecting indicator on click."
**Decision:** Small accent-tinted pill (rounded-square 6px radius)
in the top bar's right column. Cycling-dots "Connecting…"
animation for ~700ms before lifting offline.
**Alternatives:** Mini-player chip only — invisible to users not in
mini mode. Snackbar / toast — already in use; would compete.
**Revisit if:** Users miss the offline state cue (it's deliberately
subtle).

## 2026-05-15 — Offline library shows `list_complete_items`, not just `list_requested`

**Context:** Initially the offline Albums tab only showed albums the
user explicitly requested as downloads. But when a user downloads an
artist, the cascaded-in albums show up as state=complete child nodes
without `requested=1`.
**Decision:** Library views use `offline.list_complete_items(kind)`
in offline mode. The Downloads management screen continues to use
`list_downloads` (= `list_requested`) since "things I asked for" is
the right framing there.
**Alternatives:** Filter both to `requested=1` — hides downloaded
content from the user. Show everything in `state != "deleted"` —
includes in-flight downloads, confusing.
**Revisit if:** Users get confused by an album showing in library
that they "didn't download" (it cascaded from an artist).

## 2026-05-15 — EQ uses `anequalizer`, not deprecated `equalizer` filter

**Context:** From `docs/research/eq_dsp.md`.
**Decision:** mpv's classic `equalizer` filter is deprecated;
`anequalizer` is the supported path. ~10-band ISO octaves.
**Alternatives:** `firequalizer` (FIR, higher quality, more CPU);
`anequalizer` is the right S/M tier choice for v1.
**Revisit if:** Users ask for high-Q parametric bands (move to
`firequalizer` for v2) or AutoEQ-style headphone correction.

## 2026-05-15 — Smart playlists v1 = client-side rules + provider-rendered evaluation

**Context:** From `docs/research/smart_playlists.md`. Navidrome has
no REST endpoint for *creating* smart playlists; Feishin only works
because it talks to Navidrome's separate admin API. Jellyfin has 3
competing plugins, none bundled.
**Decision:** Local `smart_playlists.json` for rules. Each provider
translates as much of the rule set as it can into a native query
(Jellyfin `/Items` filters, Subsonic `getAlbumList2` /
`getSongsByGenre`) and refines the remainder in Python. v2 layers in
read-only display of Navidrome's `.nsp` smart playlists via
OpenSubsonic's `readonly: true` flag.
**Alternatives:** Server-side write-through — would require auth
tier beyond what Subsonic exposes. Pure client-side filter on a
fully-fetched library — fine for small libraries, dies at scale.
**Revisit if:** Users want smart playlists synced across devices
(then server-side becomes necessary; explore Jellyfin's plugin
ecosystem and Navidrome admin API).

## 2026-05-15 — Crossfade via two alternating libmpv instances

**Context:** From `docs/research/crossfade.md`. mpv has no
continuous-crossfade primitive across its playlist.
**Decision:** Two libmpv instances in one process, ping-pong
A→B→A→B. Inactive instance acts as prefetch slot. QTimer drives
linear volume ramps. Smart-album-continuity check routes adjacent
same-album tracks back through the existing gapless path.
**Alternatives:** `af=lavfi[acrossfade]` — one-shot only, not
continuous. ffmpeg pre-mix — breaks streaming. Custom mixer with
PyAudio + numpy — maintenance burden.
**Revisit if:** MPRIS routing or cast handoff gets too messy with
two instances; or if mpv ever ships a continuous-crossfade option.

## 2026-05-15 — Visualizers v1 taps mpv via `--lavfi-complex`, defers OS loopback

**Context:** From `docs/research/visualizers.md`. Per-OS loopback
backends (PipeWire/WASAPI/CATap) is real cross-platform work and not
in v1's budget.
**Decision:** v1 = mpv `--lavfi-complex` with `asplit` tap → PCM
into Python → FFT on QThread → QPainter render → third
`np_left_pane_mode = visualizer`. Per-OS loopback backends and
ProjectM/OpenGL are v2+.
**Alternatives:** ProjectM via Flatpak extension — heavy native dep
for v1. butterchurn-in-WebView — would reverse the WebEngine
removal.
**Revisit if:** Users want visualization during cast (cast → mpv
goes silent so v1 tap returns nothing; ship "Casting to <device>"
placeholder for now).

## 2026-05-15 — Tag editing is Jellyfin-admin-only (documented parity exception)

**Context:** From `docs/research/tag_editing.md`. Subsonic and
OpenSubsonic have zero metadata-edit endpoints. Navidrome is
read-only on music files by design.
**Decision:** New `provider.can_edit_metadata` boolean gates the
UI. Jellyfin gets a full edit path (POST `/Items/{id}` with full
BaseItemDto + `LockedFields` to avoid bug #10724 corruption).
Subsonic + Navidrome stay read-only.
**Alternatives:** Local annotation overlay — works for any backend
but device-local-only and the server library never reflects changes.
**Revisit if:** Users on Subsonic/Navidrome specifically request
tag editing in significant numbers — then implement annotation
overlay (Phase 5 in the research doc).

## 2026-05-15 — Lowercase rename: `jellytoast`, not `JellyToast`

**Context:** Brand pass before AUR/Flathub packaging.
**Decision:** All branding uses lowercase. Two intentional
`JellyToast` survivors live in `settings.py`: `_LEGACY_*` constants
+ the migration log line. Don't change those.
**Alternatives:** TitleCase everywhere — feels off for a Linux CLI-
adjacent tool.
**Revisit if:** Never. (Marketed packaging will reinforce the
choice.)

## 2026-05-09 — Mini-player keep-above via KWin rule, not Qt flags

**Context:** Wayland forbids client-set absolute window stacking via
Qt's `Qt.WindowStaysOnTopHint`.
**Decision:** Write a KWin window rule into
`~/.config/kwinrulesrc` opt-in. Lives in `modules/keep_above/_kwin.py`.
**Alternatives:** Qt flag — silently no-ops on Wayland. Other WMs —
out of scope; jellytoast primary target is KDE Plasma Wayland.
**Revisit if:** GNOME / Hyprland support becomes a priority; per-WM
backends in the existing `keep_above/` package structure.

## 2026-05-10 — Main window uses KDE server-side decorations

**Context:** Earlier iterations used `Qt.FramelessWindowHint` + a
custom titlebar. Wayland + KWin handle decorations better
themselves.
**Decision:** Main window: KWin owns chrome. Mini player + settings
dialog: still frameless (small, dialog-shaped).
**Alternatives:** Stay frameless across the board — duplicates work
KWin does for free; introduces resize-hit-zone gymnastics.
**Revisit if:** macOS / Windows backends arrive; their native
chrome paths may want different choices.

## 2026-05-08 — Native PySide6 surfaces, not QWebEngineView

**Context:** Jellyfin Web embed had ~750 LOC bridge scaffolding + a
Chromium runtime cost.
**Decision:** Every clicked surface (browse, search, suggestions,
login, account, now-playing) is native PySide6. WebEngine retired.
**Alternatives:** Keep WebEngine for niche surfaces (lyrics editor,
admin pages) — splits the runtime; users see two visual languages.
**Revisit if:** A surface so complex it's not worth re-implementing
natively (none currently in scope; music-only narrows this).

## 2026-05-08 — Dual-store credentials: keyring + AES-GCM file

**Context:** Boot was hanging waiting for KWallet to wake up on
some sessions.
**Decision:** Pair OS keyring (preferred) with an AES-GCM-encrypted
QSettings blob. Key derived from `/etc/machine-id` + `$USER` via
PBKDF2-SHA256. Config file `chmod 600`.
**Alternatives:** Keyring-only — boot hangs noted above.
Plaintext — never. Encrypted-file-only — loses OS keyring's
"unlock once per session" UX.
**Revisit if:** A platform appears where neither store works well
(unlikely on macOS/Windows; iOS sandbox has its own primitives).
