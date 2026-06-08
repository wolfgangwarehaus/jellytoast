# jellytoast — Final Engineering Audit Report

> **⚠️ ARCHIVED SNAPSHOT (2026-06-01) — SUPERSEDED, NOT CURRENT STATE.**
> This is a point-in-time audit. ~170+ commits have landed since; its entire
> P0 block and the bulk of P1/P2/P3 are remediated. Kept for the rationale and
> the methodology, not as a live to-do list. For current open work see
> `docs/TODO.md`; for what shipped see `CHANGELOG.md`. A later whole-app review
> (2026-06-08) supersedes its findings.

## Executive Summary

jellytoast is a mature, disciplined codebase that already clears the bar most open-source desktop apps never reach. Across 23 specialist audit passes — every bug-like claim independently re-verified by a skeptic — the dominant signal is engineering discipline, not neglect: a true signal-bus mediator with zero UI back-imports, a strictly one-directional UI→backend boundary, no module-load-time import cycles across 122 modules, an exemplary cross-platform backend-package pattern, textbook credential crypto (AES-GCM + PBKDF2-SHA256, fail-closed, never plaintext), fully parameterized SQL with correct LIKE-escaping, no `eval`/`exec`/`pickle`/`shell=True`, and a 2,258-test suite with a hard-won isolation/randomization regime that was earned by chasing real cross-test SIGSEGVs.

There are **no critical findings** and **no high-severity findings on a common path**. The single high-severity item is a real but narrow UI-state bug (the live now-playing favorite toggle can never un-favorite). Everything else is `medium` or below, and the mediums cluster into three honest themes:

1. **Structure at the leaf level** — a handful of god-objects (`SettingsDialog` ~4k lines, `JellytoastWindow`, `MpvController`, `Settings`, `now_playing_page.py`, `now_playing_bar.py`, `library_grid.py`, `offline/manager.py`) concentrate change-risk. The macro-architecture is sound; the decomposition debt is at the bottom of the tree, and the team has *already* demonstrated the right extraction pattern (`settings_colors_page.py`, `eq_curve_editor.py`, the `cast_manager/` mixin split).
2. **Enforcement gaps in tooling** — no static type checker, no coverage signal, no dependency/security scanning, a single-interpreter CI matrix, and no wheel-build smoke. The discipline exists; it just isn't *enforced*, so drift is invisible.
3. **A small set of genuine correctness defects** in casting/state transitions and packaging — most notably the app icon shipping from a wheel-excluded path, a duplicated 5-way cast-dispatch ladder, and several silent cast-failure paths.

Methodology note: 23 auditors (dimension lenses + deep single-file reads) ran in parallel; **21 candidate findings were refuted during adversarial verification** (e.g. an inflated "110-method" count corrected to 87; an "85-site" provider-kind claim corrected to 4; a HEAD/Content-Length framing "bug" traced and confirmed correct). The findings below are the survivors.

**Overall grade: B+ (strong).** The justification: an A-grade core (concurrency, security, the provider/queue/state layer) sits under a B-grade leaf layer (oversized UI modules) and a B-grade tooling perimeter (no type/coverage/security gates). Close the enforcement gaps and decompose the top three god-files and this is an A-.

---

## Scorecard

| Area | Grade | One-line justification |
|---|---|---|
| Concurrency & thread safety | **A** | Centralized `async_io` marshalling, GUI-thread QTimer hops, documented download-queue invariant; only unguarded cross-thread `active_cast` writes (GIL-benign) hold it back. |
| Security & credentials | **A** | AES-GCM/PBKDF2 crypto, TLS-on, parameterized SQL, token-gated proxy; lone gap is AirPlay HAP creds in plaintext. |
| Provider abstraction (`providers/`) | **A** | Exemplary ABC: 36 abstract + 14 capability methods, both backends honor every signature; findings are edge-case fragilities. |
| Queue + player-state core | **A** | Permutation queue model, generation-token radio refill, identity-based index tracking; defects are trivial. |
| Architecture & code layout | **B** | Sound macro-architecture; weakness is leaf-level god-objects + documented PascalCase provider-shape leak. |
| Python idioms & maintainability | **B** | No mutable defaults/bare-excepts/prints; drags are stringly-typing, a duplicated dispatch ladder, ~161 silent excepts. |
| Type safety & API contracts | **B** | Broad hints + strong ABC; undermined by zero static-checker enforcement. |
| Error handling & robustness | **B** | Rigorous resource cleanup + network classification; some silent swallows on data paths + a silent cast-advance failure. |
| Testing quality & coverage | **B** | 2,258 real-assertion tests, superb isolation; gaps are scrobble backends + no coverage tooling. |
| Packaging, licensing & compliance | **B** | Correct GPLv2/SPDX, GPL-compatible tree; functional bugs (wheel-excluded icon, 7-place version, bogus MIME). |
| Dependency hygiene & supply chain | **B** | Every dep imported, reasoned caps, clean soft-imports; floors too low on `requests`/`cryptography`, no scanning. |
| Documentation & onboarding | **B** | README/SPEC/ADR log strong; missing community-health files + stale light-theme docs. |
| CI, tooling & developer experience | **B** | CI mirrors local green bar; no type/coverage/security/wheel/matrix coverage. |
| `jellytoast.py` (entry/main window) | **B** | Superbly commented; god-object window + a few GUI-thread blocking calls. |
| `settings_dialog.py` | **B** | Careful local correctness; oversized + a real PlayerBus slot-leak on close. |
| `now_playing_page.py` | **B** | Correct MVC/DPR/preview discipline; two state bugs + no unit coverage of pure helpers. |
| `now_playing_bar.py` | **B** | Two domains in one file; active-cast banner mislabels non-AirPlay devices. |
| `library_grid.py` | **B** | Performance-engineered MVC; oversized + an offline/online pagination interleave gap. |
| `player_backend.py` | **B** | Densely documented; god-object + half-cast-state defects on Stop/pause-while-casting. |
| `settings.py` | **B** | Correct crypto + graceful degradation; 2.5k-line god-object, read-as-write getters. |
| `ui_helpers.py` | **B** | Sophisticated image loader; wrong-layer orchestration entry points + stale cache-clear bookkeeping. |
| `offline/` subsystem | **B** | Well-decomposed node-graph store; double-counted failure + cross-thread invariant violation. |
| Cast surface (`cast_proxy`/`cast_manager`/`cast`) | **B** | Clean mixin split; open 0.0.0.0 relay + dead code paths to document/wire. |
| **Overall** | **B+** | A-grade core + leaf-level structural debt + an unenforced tooling perimeter. |

---

## What's Already Excellent

These are concrete, verified strengths — the report would be dishonest without leading on them.

- **The signal bus is a real mediator, not a hidden god-object.** `PlayerBus` (`modules/player_state.py:213`) declares ~88 documented signals, has only ~11 methods, and imports **zero** UI/view modules. Every signal carries an inline payload-type and emitter-contract comment grouped by direction (UI→backend intents vs backend→UI state).
- **Clean, verified layering.** `player_backend.py`'s `MpvController` imports only `player_state`, never a view; all UI→engine traffic flows through the bus. An AST walk confirmed **no module-load-time import cycles across 122 modules**, and **none** of the ~400 deferred imports would close a cycle if hoisted — they exist for soft-importing optional deps, lazy views, and re-pulling theme-mutated constants.
- **Credential crypto is textbook.** AES-GCM with a fresh `os.urandom(12)` nonce per encryption, PBKDF2-HMAC-SHA256 at 100k iterations, key never persisted, and a deliberate **fail-closed to empty-string** on crypto error so a failure never degrades to writing plaintext (`modules/settings.py:76-175`). The dual-store divergence resolution (blob-wins + keyring rewrite) is genuine engineering that fixed a real "login devolves" bug class.
- **Concurrency hardened by scar tissue.** `modules/async_io.py` moves the result-carrier `_Signaler` onto the GUI thread *before* connecting (so completion routes via `QueuedConnection`), pins live carriers against premature GC, and swallows shutdown `RuntimeError` — with docstrings citing the exact bug each guard prevents. QTimer create/start/stop correctly hop to the owning thread.
- **The provider ABC is a model contract.** `modules/providers/base.py` documents per-method cross-provider semantics and the raise-vs-return-empty convention (e.g. `verify_session` must return `True` on network error, `False` only on definitive reject); both concrete providers honor every signature, including capability overrides.
- **Exceptional test isolation.** Per-xdist-worker `HOME`/`XDG`, `QStandardPaths` test mode, and a 100-line autouse drain fixture that pumps deferred Qt callbacks, drains the pool, stops cast threads, and `gc.collect`s — each step justified against a specific past SIGSEGV. `pytest-randomly` is on in CI. Only **3 skips** in the entire suite, all honest hardware/dependency gates.
- **Resource cleanup is rigorous.** The visualizer subprocess (terminate→wait→kill→wait, `stdout` closed in `finally`, zombie reaper), the SQLite layer (WAL, `foreign_keys=ON`, re-entrant transaction CM that releases the RLock on `__enter__` failure), the FFT QThread teardown ordering, and atomic `.part`→`os.replace` download writes are all done correctly.
- **Licensing is correct by construction.** A real GPLv2 `LICENSE`, SPDX `GPL-2.0-or-later`, matching classifier, CC0/GPL metadata split in the AppStream metainfo, and — critically — the PySide6 `LGPL-3.0/GPL-2.0-only/GPL-3.0-only` triple-license is resolved cleanly *because* the project is "or-later" (see Compliance section).

---

## Findings by Theme

### Theme 1 — Correctness defects (functional bugs)

#### 1.1 Live-mode favorite toggle can never un-favorite and silently loses state — **HIGH**
**`modules/now_playing_page.py:3704-3718`** (`_on_favorite_cta`, live branch). In live mode `_preview_meta` is reliably `{}` (reset at 3047/3694/3749/3816 on every preview→live transition). The live branch reads `cur_fav` from `_preview_meta` and writes the new state back into it:
```python
cur_fav = bool(cur_meta.get("UserData", {}).get("IsFavorite", False))   # always False
new_state = not cur_fav                                                 # always True
cur_meta.setdefault("UserData", {})["IsFavorite"] = new_state           # written to dead dict
```
So the CTA always sends `favorite=True` (can never toggle off) and the new state is written into the unused preview dict and lost. The inline comment "not used in live path" is wrong — the dict *is* read at 3712 and written at 3715.
**Fix:** Seed `cur_fav` from the live source's real favorite state (queue context / fetched `UserData` for `source_id`) and persist it where `_on_favorite_toggled` can observe it.

#### 1.2 The 5-way cast-dispatch ladder is duplicated, and one copy mislabels/misroutes — **MEDIUM** (×3 related)
The same `dev.device_type` branch-over-five-protocols ladder lives independently in **`modules/player_backend.py:898-1025`** (`MpvController.play`) and **`jellytoast.py:3017-3224`** (`_cast_to_device`). A new protocol or routing-rule change must be edited in two places — exactly the class that caused the documented DLNA/Sonos→AirPlay misroute (memory #8). Two concrete consequences confirmed:
- **Cast failure on track-advance is silent** (`modules/player_backend.py:946-1024`): the advance paths act only on `if ok:`/`on_error=lambda _e: None` with no else, no log, no toast. The *initial* pick surfaces a `QMessageBox` (`jellytoast.py:3069`); the advance path mirrors none of it, so a device dropping mid-session leaves silently-dead playback with no diagnosis.
- **Active-cast banner mislabels DLNA/Sonos/Snapcast as "AirPlay"** (`modules/now_playing_bar.py:3416`): `kind = "Chromecast" if active.device_type == "chromecast" else "AirPlay"` — the exact bug class the file's own device-row code (2622-2623) already fixed via `SECTION_LABELS`.
**Fix:** Extract one dispatch surface (e.g. `CastManager.start_track(dev, np, on_done)` or a per-`CastType` strategy table) called from both paths; add a failure branch (log + toast); reuse `SECTION_LABELS.get(...)` for the banner.

#### 1.3 Stop / pause while casting leaves the controller in a half-cast state — **MEDIUM** (×3 related)
`modules/player_backend.py` maintains two notions of "casting" that diverge:
- **`stop()` (1151-1160)** calls `stop_cast()` (which clears `active_cast`) but emits `playback_stopped`, **not** `cast_stopped`. The two teardown handlers (`_on_cast_stopped` at 569, `_on_cast_stopped_bit_perfect` at 2151) never run, so the 500 ms poll timer keeps waking, `_cast_active_flag` stays `True` (bit-perfect stays falsely disabled), and the slider isn't restored to local volume.
- **`pause()` (1874-1883)** is documented "idempotent pause" (used by sleep-timer fire paths) but routes the cast branch through `cast_toggle_pause()`, which *toggles* — so if the cast is already paused when the sleep timer fires, it **resumes** playback.
- **`_cast_active()` (live) vs `_cast_active_flag` (cached)** are the root divergence; bit-perfect gating reads the cached flag while transport reads the live check.
**Fix:** Either have `stop_cast()` emit `cast_stopped`, or have `stop()` invoke the same teardown as a disconnect. Add a one-way `cast_pause()` to `CastManager`. Consolidate on the live `_cast_active()` as the single source of truth, or document that the flag must reset on every `active_cast` clear.

#### 1.4 Offline short-circuit skips the load-generation bump → online pages can append onto the offline render — **MEDIUM**
**`modules/library_grid.py:2571-2573`** returns from `load_items()` before the `self._load_gen += 1` at 2583. An in-flight online auto-paginate cascade that captured `gen == self._load_gen` therefore never bails; its `_on_page_loaded` reaches `append_items(items)` (3041) onto the offline-only model that `_render_offline_items` just rendered. The `_loading_more` latch doesn't close the window (offline render sets it `False`).
**Fix:** Bump `_load_gen` (and clear `_silent_fetch_in_flight`/`_partial_cache_buffer`/`_loading_more`) at the very top of `load_items`, before the offline branch — or have `_render_offline_items` stamp the current gen into its emitted envelope.

#### 1.5 No-URL download failure is recorded twice — **MEDIUM**
**`modules/offline/manager.py:458-462`** calls `_record_failure(tid)` + emits `failed` and *then* calls `_finish(tid, success=False)`, whose else-branch (539-541) calls `_record_failure` again. Result: `retry_count` is bumped twice (backoff jumps a step early — first failure lands on the 60 s window instead of 30 s) and `_session_failed` double-counts (drain notification can report more failures than tracks).
**Fix:** Drop the two lines before `_finish` and just call `_finish(tid, success=False)`.

#### 1.6 `_on_dpr_changed` hardcodes `kind="album"`, corrupting an in-progress playlist preview — **MEDIUM**
**`modules/now_playing_page.py:2530-2534`** re-enters `load_preview(self._preview_id, kind="album")` on a DPR/monitor change. The early-return guard (3738) fails because `ALBUM != self._preview_kind(PLAYLIST)`, so it refetches via `get_album_tracks` on a playlist id and resets `_preview_kind` to `ALBUM` (mislabeling the kicker, installing the wrong `QueueKind` on play).
**Fix:** Derive kind from `self._preview_kind` instead of hardcoding.

#### 1.7 Subsonic Favorites filter silently dropped under any non-default sort — **MEDIUM**
**`modules/providers/subsonic.py:620-634`**: the `IsFavorite`/`IsPlayed` branches sit at the *end* of the `elif` chain that derives `kind` from the sort key, so they are only reached when the sort is the default `SortName`. With any mapped sort (`AlbumArtist`, `PremiereDate`, …) the favorites filter is dropped and `getAlbumList2` returns the whole library. (`line 676`'s `AlbumArtist` branch is dead for the same reason.) Cross-provider parity is broken — Jellyfin applies `IsFavorite` regardless of sort.
**Fix:** Check `filter_set` *first* (it's a content filter, not a sort), then apply the closest sort client-side.

#### 1.8 App icon loads from a wheel-excluded path with no fallback guard — **MEDIUM** (×2 related)
**`modules/ui_helpers.py:1191-1217`**: the icon SVG path is `__file__/../../packaging/icons/jellytoast.svg`, but `pyproject.toml:137-148` *excludes* `packaging*` from the wheel and never declares the SVG as package-data. In any pip/Flatpak install the window/tray/QApplication icon is blank. Worse, `make_app_icon` has **no `.exists()`/`isValid()` guard** — `QSvgRenderer` on a missing path yields an empty pixmap silently.
**Fix:** Move the SVG into a package (e.g. `modules/assets/`), declare it as package-data, load via `importlib.resources`, and add an `isValid()` fallback to a `QStyle` standard icon.

#### Lower-severity correctness items (LOW / INFO, verified)
- **`@Slot(bool)` on a zero-arg slot** connected to `Signal()` (`jellytoast.py:1609`) — wrong arity annotation, not a runtime bug.
- **`clear_image_caches()` leaves in-flight/low-prio bookkeeping stale** (`ui_helpers.py:759-768`) — an old-server reply can repopulate the freshly-cleared cache under a reused semantic key (cross-server art-bleed).
- **Deferred low-prio image request bypasses the cache re-check** before a fresh GET (`ui_helpers.py:1006-1018`) — a wasted round-trip when another consumer warmed the cache.
- **AutoEQ curve-drag mirrors arbitrary band onto fixed graphic sliders** (`settings_dialog.py:2178-2194`) — desyncs slider widgets from `eq_bands` in AutoEQ mode.
- **`_finish` commit-failure leaves the `.part`/final fragment orphaned** (`offline/manager.py:518-538`) — no FS cleanup on a post-`os.replace` DB-insert failure.
- **Subsonic year smart-rule admits off-year tracks** on the album-expansion leg (`subsonic.py:1432-1456`) — the satisfied year rule is dropped from refine.
- **`_DlnaLoopThread.submit()` relies on an `assert`** removed under `python -O` (`cast/dlna/_loop.py:51,82-85`) — snapcast's sibling raises properly.
- **User skip under `RepeatMode.ONE` records a radio skip without advancing** (`queue_manager.py:504-510`); **`queue_changed` double-emitted** on remove-of-current (`queue_manager.py:339-349`) — both INFO.

### Theme 2 — God-objects & decomposition debt

The macro-architecture is clean; the cost is concentrated in oversized leaf modules. Verified line/method counts:

| File / class | Size | What it fuses |
|---|---|---|
| `settings_dialog.py` `SettingsDialog` | ~3,994 lines, 100 methods (`418-4412`) | 9 settings pages + all handlers inline |
| `player_backend.py` `MpvController` | 2,203 lines | local transport + cast bridge + crossfade + EQ + sleep-timer + bit-perfect + session reporting |
| `settings.py` `Settings` | 2,467 lines, 103 prop/setter pairs | 10 unrelated key namespaces + crypto + migration |
| `jellytoast.py` `JellytoastWindow` | 2,531 lines, **87** methods (`754-3285`) | chrome + nav + cast dispatch + auth + library-selection + shuffle |
| `now_playing_page.py` | 3,964 lines | track MVC stack + drag engine + download button + lyrics controller |
| `now_playing_bar.py` | 3,576 lines | transport bar + volume popups + the entire Cast picker dialog |
| `library_grid.py` `LibraryGrid` | 3,603 lines | model/view wiring + a 12-flag pagination/cache state machine |
| `offline/manager.py` | 1,366 lines, ~15 module globals | queue core + stats + policy flags |
| `subsonic.py` `SubsonicProvider` | 1,538 lines | auth + adapters + browse + radio + smart-playlist eval |
| `PlayerBus` | ~330 lines, ~80 signals | ~15 subsystems behind section banners |

**Why it matters:** these files are the highest accidental-complexity and change-risk in the tree (the `library_grid` backfill state machine bred the double-load race; `player_backend`'s split cast-gating bred §1.3). **The strongest evidence this is tractable:** the author has *already applied the prescribed extraction* — `_build_colors` (`settings_dialog.py:3888`) is a 7-line delegator to a standalone 623-line `settings_colors_page.py` with the docstring "settings_dialog.py is already big," and `cast_manager/` was split from a 794-line god-file into a mixin composition.

**Fix (the pattern is established):** Extract cohesive seams into collaborators/page-modules:
- `settings_dialog.py` → `EqSettingsPage` (the ~1,000-line EQ/crossfade/bit-perfect cluster, most cohesive first target), `ScrobblingSettingsPage`, `CastingSettingsPage`.
- `player_backend.py` → `CastTransportBridge` (~350 lines, the most error-prone cross-thread logic), `SleepTimer`, `EqController`.
- `jellytoast.py` → `CastDispatcher`, `NavController`, `AuthLifecycle`/`SessionController`, `LibrarySelectionController`, `ShufflePrimer`.
- `now_playing_bar.py` → split at line 2587 into `cast_dialog.py`; extract `VolumeButton`/popups into `volume_button.py` (kills `mini_player`'s transitive import of the whole bar).
- `now_playing_page.py` → `np_track_list.py` (MVC stack), `np_lyrics`, a reusable download button.
- `library_grid.py` → a `LibraryPaginator` collaborator owning the fetch state machine; lift the duplicated `_ElidingLabel` (also in `songs_view`/`now_playing_page`) into a shared widgets module.
- `settings.py` → `credentials.py` (the security-critical crypto/dual-store layer — should be readable in isolation), `settings_migration.py`, a `CastSettings` sub-object.
- `subsonic.py` → `subsonic_adapters.py` (the three pure `_adapt_*` statics).

### Theme 3 — Stringly-typing & duplication

The project *proves it knows* the `class X(str, Enum)` idiom (`RepeatMode`, `QueueKind`, `CrossfadeState`) but doesn't apply it to several categorical values where a typo fails as a silent non-match:
- **Cast `device_type`** is `str` on the `CastDevice` dataclass (`cast_manager/_common.py:22`), dispatched across ~27 sites / 64 literals. **MEDIUM** — introduce `CastType(str, Enum)`.
- **Download lifecycle states** are free-form strings (`offline/index.py:252-277`) leaking into 6 UI consumers. **MEDIUM** — `DownloadState(str, Enum)`.
- **Provider kind** compared as `"jellyfin"`/`"subsonic"` literals (4 sites — the "85" claim was refuted). **LOW** — `ProviderKind(str, Enum)`.

Verified duplication worth a shared helper (all LOW): the 5-way dispatch ladder (§1.2); `_compute_subtitle`/`_artist_id_for_album` (instance + module copies in `library_grid.py`, the instance copies on never-instantiated `LibraryTile`); year-extraction copy-pasted **8** times in `library_grid.py`; volume-slider QSS across 4 builders in `now_playing_bar.py`; the frameless/rounded-paint/titlebar-drag boilerplate across `_AboutDialog`/`SettingsDialog`; `resume_pending`/`retry_failed` re-queue body; and `rgb()/rgba()` parsing 3× with divergent fallbacks in `ui_helpers.py`.

### Theme 4 — Exception hygiene & silent failures

For ~468 `except Exception` handlers this is unusually disciplined — **zero bare `except:`**, essentially zero production `print()`. But ~161 are immediately followed by `pass` with no logging, and discipline is inconsistent: the cast subsystem logs via `# noqa: BLE001` + `_emit_error` (25 sites) while most modules swallow silently. **MEDIUM/LOW.**

The handlers that matter sit on plumbing/data paths:
- **`async_io` user-callback exceptions are swallowed with no logging** (`async_io.py:108-120, 230-233, 252-255`) — the central plumbing for nearly all async results; a bug in any `on_result`/`on_error` vanishes with zero trace. **MEDIUM** — add `logger.exception(...)` (keep swallowing the dispatcher).
- Connectivity host-probe (`offline/connectivity.py:304`), best-effort persistence writes (`offline/manager.py:962-989` — the exact subsystem with the documented QSettings-flush hazard), and lazy-import-`_emit_*` helpers that also swallow connected-slot exceptions (`offline/manager.py:971-1007`). **LOW** — narrow to expected types + debug-log.

**Fix:** Adopt one convention — either keep `BLE001` visible and require a log/emit on every broad catch, or annotate the deliberate best-effort swallows inline. Add a gated debug log (like the existing `JT_*` switches) to the data-path swallows.

### Theme 5 — Concurrency residuals

The concurrency design is A-grade; the residuals are unguarded cross-thread *contracts* that are benign on CPython (GIL makes the pointer write atomic) but undocumented:
- **`CastManager.active_cast` / `_cast_paused` written from pool workers, read on the GUI thread** (`cast_manager/_others.py:213-214,245-246`; `_chromecast.py:163,299,314`). A `stop_cast()` racing an in-flight `cast_to_dlna` can read a stale `active_cast`. **LOW** — hoist the assignment into the GUI-thread `on_result` callback (single writer thread).
- **`offline/manager.py:_planning_in_flight` is read/written off the GUI thread** (`manager.py:244-246`, read at `library_sync.py:158`), violating the module's own documented "GUI-thread-only, no lock" invariant. **LOW** — guard with a small lock or marshal `enqueue`'s body onto the GUI thread; also add a public `planning_in_flight()` accessor (today `library_sync` reaches a leading-underscore global via `getattr`).
- **`CastProxy._lan_ip` mutated outside the lock** in `proxy_url()` (`cast_proxy.py:389-392`). **INFO.**

### Theme 6 — Security (defense-in-depth)

Fundamentals are A-grade. The actionable items:
- **AirPlay 2 HAP pairing credentials stored in QSettings plaintext** (`modules/airplay2.py:118-133`) — inconsistent with the AES-GCM-at-rest standard every other secret uses (server token, ListenBrainz, Last.fm). These are long-lived device-control secrets and the `_encrypt_token`/`_decrypt_token` helpers are one import away. **MEDIUM** — wrap store/get with forward-migration on read, exactly as `listenbrainz_token` does.
- **Cast proxy is an open relay bound to `0.0.0.0`**, token-gated, that relays credential-bearing upstream URLs with **TLS verification fully disabled** for all https upstreams (`cast_proxy.py:142-150, 346-348`). Defensible by design (reaches self-signed/Tailscale hosts the speaker can't), but the posture should be hardened and documented: bind the resolved LAN IP rather than every interface; verify-by-default and only fall back to `CERT_NONE` on `SSLCertVerificationError`; expire tokens on cast-stop; write a threat-model note. **LOW.**
- **TOCTOU on the proxy local-file open** (`cast_proxy.py:206-219`) — the symlink-resolving containment check validates `path.resolve()` but `open()`s the unresolved `path`. **LOW** — open the resolved path.
- **Subsonic plain-password mode** puts `?p=<password>` in query strings (server-log exposure), and **Jellyfin/Subsonic stream URLs embed the token** (inherent to casting; the proxy already drops auth-echo headers). **LOW/INFO** — prefer `enc:` form / `token_present` over `token_len` in logs; confirm these URLs are never persisted or logged at info level.
- **Machine-key fallback to `hostname:username`** in containers/minimal installs weakens the blob's off-box secrecy (`settings.py:113-118`) — documented as "weaker"; the keyring + chmod-600 remain. **INFO** — optionally mix in a per-install random salt.

---

## Compliance & Licensing

**Verdict: fundamentally sound; the defects are functional packaging bugs, not license violations.**

- **Base license is correct and consistent.** A real GPLv2 `LICENSE` ships and is in the sdist; `pyproject.toml:20` declares SPDX `GPL-2.0-or-later`; the classifier `GPLv2+` matches; the AppStream metainfo splits `<metadata_license>CC0-1.0` from `<project_license>GPL-2.0-or-later`.
- **The dependency tree is GPL-2-compatible across the board.** pyatv/soco/pychromecast/keyring/dbus-next/ifaddr are MIT; requests/async-upnp-client/cryptography are Apache-2.0 (cryptography is Apache OR BSD); numpy BSD; zeroconf/python-xlib LGPL-2.1+/LGPLv2+; python-mpv GPLv2+/LGPLv2.1+. No copyleft-incompatible or proprietary dependency is present.
- **The PySide6 LGPL/GPL interaction is resolved correctly — by luck of the "or-later" choice, not by recorded reasoning.** PySide6/shiboken6 are `LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only`. LGPL-3.0/GPL-3.0 are incompatible with a GPL-2.0-**only** program; they are compatible only because jellytoast is GPL-2.0-**or-later**, allowing a conveyed combined work to be taken to GPL-3.0. **This is the single most important compliance fact and it is documented nowhere.** **LOW** — add a `LICENSING`/`NOTICE` note (or a comment by the PySide6 pin) so a future maintainer can't "simplify" to GPL-2.0-only and silently break Qt-binding compatibility.

**Functional packaging defects** (severity drivers, not legal issues):
- **Version string hand-duplicated across 7 files** with no single source of truth (`pyproject.toml:16`, `jellytoast.py:3419`, `settings_dialog.py:292`, `scrobble/listenbrainz.py:48,97`, `scrobble/server_scrobble_detect.py:94`, `metainfo.xml:77`). A release bump that misses one ships an inconsistent MPRIS/scrobble-client/About version. **MEDIUM** — define one `__version__` (or read `importlib.metadata.version`) and derive all strings from it.
- **App icon shipped from a wheel-excluded path** (§1.8). **MEDIUM.**
- **Desktop entry advertises local-audio MIME handling the app cannot perform** (`...jellytoast.desktop:6,13`: `Exec=jellytoast %U` + a full `audio/*` `MimeType=`). The only `sys.argv` use is `QApplication(sys.argv)`; there is no `QFileOpenEvent` filter, no URL-scheme handler, no argv parsing, and the single-instance relay only sends `b"raise"`. Double-clicking a `.flac` launches jellytoast and silently ignores the file — a broken desktop contract and a Flathub-reviewer risk. **LOW** — remove the `MimeType=` line and `%U`, or implement file/URL handling.

**Deferred (honestly documented, noted as state not defect):**
- **No Flatpak manifest exists** — `docs/research/flatpak_packaging.md` is a research note; the metainfo `<screenshots>` block is commented out, so the AppStream component cannot pass Flathub validation. Before submission: author the manifest, capture screenshots, uncomment the block.
- **No `NOTICE`/`THIRD-PARTY-LICENSES`** aggregating Apache/LGPL attribution — low risk for pip, but a Flatpak bundle vendors these libraries and should carry their notices (`pip-licenses` can generate it). **LOW.**
- **Stale `*.egg-info/SOURCES.txt`** lists `cast_manager.py`/`dlna.py` as flat modules (pre-package-split); gitignored so not shipped, but misleading. **INFO** — regenerate via `python -m build` when validating packaging.

---

## Prioritized Remediation Roadmap

Effort: **S** ≈ <½ day · **M** ≈ ½–2 days · **L** ≈ multi-day.

### P0 — Do now (correctness on user-facing paths)

| # | Item | Effort | Files |
|---|---|---|---|
| 1 | Fix live-mode favorite toggle (seed from real source state; persist observably) — §1.1 | S | `modules/now_playing_page.py:3704-3718` |
| 2 | Ship the app icon in the wheel (move to a package, `package-data`, `importlib.resources`) + add an `isValid()` fallback — §1.8 | S | `modules/ui_helpers.py:1191-1217`, `pyproject.toml:137-148` |
| 3 | Surface cast-advance failures (log + toast) and add a one-way `cast_pause()`; emit `cast_stopped` on Stop-while-casting — §1.3 | M | `modules/player_backend.py:946-1024,1151-1160,1874-1883`, `modules/cast_manager/_manager.py` |
| 4 | Fix banner mislabel via `SECTION_LABELS.get(...)` — §1.2 | S | `modules/now_playing_bar.py:3416` |
| 5 | Bump `_load_gen` before the offline short-circuit — §1.4 | S | `modules/library_grid.py:2571-2583` |
| 6 | Encrypt AirPlay HAP credentials at rest (forward-migrate on read) — Theme 6 | S | `modules/airplay2.py:118-133` |

### P1 — Soon (enforcement perimeter + remaining correctness)

| # | Item | Effort | Files |
|---|---|---|---|
| 7 | Add a static type checker (mypy/pyright) advisory-first, starting on `providers/`, then ratchet | M | `pyproject.toml`, `.pre-commit-config.yaml`, `.github/workflows/ci.yml` |
| 8 | Add `pytest-cov` (non-gating report) + `pip-audit` step + `.github/dependabot.yml` | S | `pyproject.toml`, `.github/workflows/ci.yml`, `.github/dependabot.yml` |
| 9 | Raise security floors: `requests>=2.32.4`, bump `cryptography` floor | S | `pyproject.toml:79` |
| 10 | Single source of truth for version; derive all 7 strings | S | `pyproject.toml:16` + 6 consumers |
| 11 | Extract the 5-way cast dispatch into one surface; back it with `CastType(str, Enum)` — §1.2, Theme 3 | M | `modules/player_backend.py`, `jellytoast.py`, `modules/cast_manager/_common.py` |
| 12 | Fix Subsonic favorites-under-sort + the dead `AlbumArtist` branch — §1.7 | S | `modules/providers/subsonic.py:620-676` |
| 13 | Fix double-counted no-URL download failure; clean up orphaned `.part` on commit failure — §1.5, Theme 1 | S | `modules/offline/manager.py:458-462,518-538` |
| 14 | Fix `_on_dpr_changed` playlist-preview corruption — §1.6 | S | `modules/now_playing_page.py:2530-2534` |
| 15 | Log `async_io` callback exceptions (keep swallowing) — Theme 4 | S | `modules/async_io.py:108-120,230-233,252-255` |
| 16 | Extend CI to a `python-version` matrix (3.10–3.13) and add a wheel-build + import smoke job | M | `.github/workflows/ci.yml` |
| 17 | Unit-test the scrobble HTTP backends (Last.fm `_sign` digest vector, LB payload shape) | M | `tests/`, `modules/scrobble/{lastfm,listenbrainz}.py` |
| 18 | Add `B` (flake8-bugbear) to ruff (already flags 8 `B905` + 1 `B017`) | S | `pyproject.toml:171` |

### P2 — Structural & hardening

| # | Item | Effort | Files |
|---|---|---|---|
| 19 | Extract `SettingsDialog` pages (EQ first), `MpvController` cast bridge, and `JellytoastWindow` controllers — Theme 2 | L | `settings_dialog.py`, `player_backend.py`, `jellytoast.py` |
| 20 | Split `now_playing_bar.py` (cast dialog + volume button) and `now_playing_page.py` (track MVC + lyrics) | L | `now_playing_bar.py`, `now_playing_page.py`, new modules |
| 21 | Extract a `LibraryPaginator`; collapse the duplicated subtitle/artist-id/year helpers | M | `library_grid.py` |
| 22 | Extract `credentials.py` + `settings_migration.py` from `settings.py`; introduce `DownloadState`/`ProviderKind` enums | M | `settings.py`, `offline/index.py`, `providers/__init__.py` |
| 23 | Harden cast proxy: bind resolved LAN IP, verify-TLS-by-default with `CERT_NONE` fallback, token expiry on stop, open the resolved path; write a threat-model note | M | `modules/cast_proxy.py` |
| 24 | Resolve cross-thread residuals: hoist `active_cast` writes to GUI callback; guard/accessor `_planning_in_flight` | M | `cast_manager/_others.py`, `_chromecast.py`, `offline/manager.py`, `library_sync.py` |
| 25 | Add `CONTRIBUTING.md`, `SECURITY.md`, `CODE_OF_CONDUCT.md`, issue/PR templates, `[project.urls]` | S | repo root, `.github/`, `pyproject.toml` |
| 26 | Add a `LICENSING`/`NOTICE` note recording the PySide6 "or-later" rationale | S | repo root / near `pyproject.toml:47` |

### P3 — Polish, docs & known deferrals

| # | Item | Effort | Files |
|---|---|---|---|
| 27 | Fix the light-theme doc contradictions (ships but documented as absent in 5 places) | S | `docs/SPEC.md:200,254-256`, `README.md:210`, `settings_dialog.py:8-9`, `docs/research/visualizers.md:8` |
| 28 | Add a "Docs map" to README; expand the `PlayerBus`/`player_backend`/`settings_dialog` module docstrings; fix stale web-view comments | S | `README.md`, `player_state.py`, `player_backend.py`, `jellytoast.py:2749,3561` |
| 29 | Wire or clearly mark the dead `SonosEventBridge` and DLNA User-Agent override (both shipped + tested, no caller) | M | `modules/cast/sonos.py:496-678`, `modules/cast/dlna/controller.py:255` |
| 30 | Disconnect `SettingsDialog`'s PlayerBus slots on close (or `WA_DeleteOnClose`); move bus connects to `__init__` | S | `modules/settings_dialog.py:561-565,1480-1481,1819-1824,686-703` |
| 31 | Align `dev/install.sh` with `pip install -e .[extras]` so the from-source bar matches CI; declare `shiboken6` | S | `dev/install.sh`, `pyproject.toml` |
| 32 | Make the visualizer worker test predicate-poll instead of fixed sleep; add unit tests for `tray._quit` + `now_playing_page` pure helpers | M | `tests/test_visualizer.py`, `tests/` |
| 33 | Promote `keep_alive_url()` to the provider ABC (drop the host's `AttributeError` guard) | S | `modules/providers/base.py`, `jellytoast.py:1729-1732` |
| 34 | Author the Flatpak manifest + screenshots + `NOTICE` when packaging resumes (deferred per project policy) | L | `packaging/` |

---

*Methodology: 23 specialist auditors (dimension lenses + deep single-file reads) ran in parallel; every bug-like finding was adversarially verified by an independent skeptic, and **21 candidate findings were refuted** during verification (inflated counts corrected, a HEAD/Content-Length "framing bug" traced and confirmed correct, an `is_admin` "ABC gap" shown already-declared). Build artifacts (`build/`, `*.egg-info/`) were excluded; only tracked source was audited. Deliberate, documented conventions — flat layout, `E501`/`E402` off, lowercase branding, soft-imported optional deps — were treated as intentional trade-offs, not defects.*