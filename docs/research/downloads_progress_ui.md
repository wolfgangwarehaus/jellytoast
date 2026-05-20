# Downloads progress UI — surfacing in-flight activity

> **📍 Status — 2026-05-20:** Shipped. This doc drove the downloads
> progress arc that landed 2026-05-18 (aggregate progress block,
> library walk, notification toggle, standalone Downloads page). Kept
> for rationale — see `docs/SPEC.md` §5 and `CHANGELOG.md`.

Status: spec / ready for slicing into autonomous + paired work.
Date: 2026-05-18.

The Downloads settings page (`modules/downloads_view.py:197`) surfaces
the *persistent* state — storage, the row list, pause toggle,
Wi-Fi-only toggle, quality combo. It does **not** surface *in-flight
activity*: "is anything downloading right now? how fast? when will
it be done?" Today the only answer is "scroll the list, look for any
row whose sub-line reads `Downloading… 42%`." This doc decides where
an aggregate cluster lives, what it shows, how the back end gets the
numbers, and how completion is signalled.

---

## 1. Goal & non-goals

**Goal.** Make "is anything downloading right now? how fast? when
will it be done?" answerable at a glance, plus fire a completion
notification on the already-shipped channel when the queue drains.

**Non-goals.** Bandwidth-limiter UI; scheduling; multi-server
routing; reworking the row list to `QAbstractListModel`/`QListView`
(the list is small — `downloads_view.py:8-11`); a top-bar downloads
chip (see §3).

---

## 2. Current state

What the back end emits (`modules/offline/manager.py`):

- `PlayerBus.download_progress = Signal(str, str, float)`
  (`player_state.py:397`), `state ∈ pending|downloading|complete|
  failed|removed`. Per-track fraction comes from the chunked GET loop
  at `manager.py:502-511`, throttled to 2 % steps
  (`_PROGRESS_STEP`, `manager.py:66`). Per-cascade-root fraction is
  the count-based aggregate at `_bump_parent`
  (`manager.py:438-457`).
- Queue pause/resume: `download_queue_paused`, `download_queue_
  resumed` (`player_state.py:401-402`). Wi-Fi-only:
  `downloads_wifi_only_changed` (`player_state.py:407`).
- Concurrency cap `_MAX_CONCURRENT = 2` (`manager.py:71`).

What's **missing**, mapped to specific gaps:

- **No byte-rate sampling.** The chunk loop tracks `got` bytes
  (`manager.py:499,506`) and total (`manager.py:494`) but never
  samples elapsed time. Speed and ETA cannot be derived from the
  existing signal — new code required.
- **No global aggregate.** `_pending` (`manager.py:80`) is per-
  cascade-root; nothing owns "the queue as a whole." Two requested
  albums are two independent progress streams.
- **No drain edge.** A cascade root emits its terminal state at
  `manager.py:455-457`; no signal fires when the *queue* drains.
- **No UI affordance for "currently downloading"** in
  `DownloadsView` (`downloads_view.py:230-376`).

Per-row state today (`_DownloadRow.update_state`,
`downloads_view.py:164-194`) already shows pct for `downloading` and
short status strings for the other states. We keep that.

---

## 3. Aggregate progress block — placement

**Pick: an inline block at the top of the page, between the storage
label and the pause button (`downloads_view.py:230-251`), visible
only when the queue has activity.** Hides back to zero height when
idle so the steady-state page reads the same as today.

Storage used is what users currently see first; slotting the activity
cluster directly below it makes pause read as contextual ("Pause these
5 active downloads") rather than abstract. The conditional show/hide
follows the existing `_empty` vs `_list_host` pattern
(`downloads_view.py:412-414`).

**Dismissed: banner pinned above the storage label.** Wrong
hierarchy — storage is the persistent fact, activity is transient.

**Dismissed: separate "Activity" tab in settings nav.** Doubles the
clicks; tabs are for orthogonal settings, not ephemeral status.

**Dismissed: top-bar chip outside the settings dialog.** Downloads
are explicit and bounded — unlike connectivity, which deserves
global real estate (`[[architecture-offline-phase5]]`). A chip would
compete with the offline + cast + now-playing chips. Revisit if
real-world feedback surfaces "I started a download three days ago
and forgot" patterns.

---

## 4. What the aggregate shows

**Pick: one row of text with three slots — counts, aggregate speed,
ETA — plus a 4 px progress bar underneath.** Pause and
Wi-Fi-only-blocked variants replace the speed/ETA slots.

Exact format (TYPE_BODY for the counts line, TYPE_CAPTION for the
tail; both via `type_qss` per `[[feedback-typography-tokens]]`):

```
Downloading 3 of 14         4.2 MB/s · 1 min 12 s left
[progress bar — accent fill, completed_fraction wide]
```

Slot rules:

- **Counts**: `Downloading {active+queued} of {total_session}`.
  `total_session` is the count across the whole session, not just
  what's left, so the number doesn't shrink when a job completes —
  only the left side ticks. Resets to zero when the queue idles for
  ≥5 s.
- **Aggregate speed**: sum of per-track byte-rate samples over the
  rolling window (§7), formatted as `_fmt_size` + `/s`. Hide when
  the sum < 1 KB/s for ≥2 s. If 0 KB/s persists ≥10 s, replace with
  "Stalled".
- **ETA**: `remaining_bytes / aggregate_speed` for the **longest
  job**, not the sum — the queue drains in parallel up to
  `_MAX_CONCURRENT = 2`, so "when am I done" is bottlenecked by the
  slowest remaining track. Format: "1 min 12 s left" if < 1 h, "1 h
  23 min left" otherwise, "calculating…" until ≥3 s of samples
  exist. Hide entirely if ETA > 12 h (the number isn't useful at
  that distance and likely means a stall).
- **Progress bar**: accent fill, `BG_CARD` track, 4 px tall, fraction
  = `sum(completed_bytes) / sum(total_bytes)` across the session.
  No animation. See §8.

**Pause variant**: counts read `Paused · 3 of 14 waiting`; speed/ETA
empty; bar fill stays at current fraction but in `TEXT_DIM`.

**Wi-Fi-only-blocked variant** (`_wifi_only and _on_metered`,
`manager.py:332`): counts read `Waiting for Wi-Fi · 3 of 14`; same
bar treatment as pause.

**All-failed variant** (active = 0, ≥1 failed this session, queue
otherwise idle): counts read `{n} download(s) failed`; slot 2
becomes a `Retry` button calling `manager.retry_failed(force=True)`;
auto-hides 30 s later or on the next enqueue.

**Dismissed: separate "completed this session" counter.** "of N"
already encodes it (N − active − queued). A second number is noise.

---

## 5. Per-row treatment

**Pick: keep the per-row percentage exactly as it is today
(`downloads_view.py:177-180`). Do not add speed or ETA to rows.**

Rows are also the post-download listing — every completed item lives
here too. Crowding the in-flight rows with speed/ETA text makes the
static rows feel inconsistent. Per-row speed is rarely actionable
(users don't pick one item to speed up). The aggregate ETA *is* the
longest-job ETA (§4), so the bottleneck row's ETA is already on
screen.

**Dismissed: replace per-row pct with speed.** Sacrifices "how close
is this one specific item" for a number already in the aggregate.
**Dismissed: rows go static once started.** Loses the affordance
that confirms the right node started in a cascade.

What changes in `_DownloadRow`: nothing visible.

---

## 6. Completion signal

**Pick: a desktop notification via the existing
`modules/notifications/` package when the queue transitions from
"≥1 active or queued" to "idle, with ≥1 completion this session."**
Single notification per drain edge, gated by a new
`get_settings().notify_on_download_complete` (default True).

Body text:

- 1 item: `Downloaded "{name}"` (root's metadata name).
- 2+ items: `Downloaded {n} items` (count of completed roots, not
  leaf tracks — a 50-track album is one item from the user's view).
- Any failures in the same drain: `Downloaded {k}, {f} failed`.

Icon: pass the tray icon path through `notifications.notify`'s
`icon` kwarg (`notifications/__init__.py:50-61`).

Why notifications, not an in-app toast: the user almost certainly
navigated away from Settings → Downloads after kicking off the
queue. We already ship `modules/notifications/`
(`notifications/__init__.py:1-19`) — no new platform work — and it
honours DND, focus modes, and daemon-level mute for free.

User disables via a new checkbox under Wi-Fi-only: "Notify me when
downloads finish." Default on.

**Dismissed: in-app toast** (user isn't on the page).
**Dismissed: no signal** (the "downloaded an artist's discography,
went to make coffee" case deserves an answer).
**Dismissed: tray-icon flash** (KDE tray APIs through Qt are fiddly
and the payoff over `notify-send` is marginal).

---

## 7. Backend changes

All edits in `modules/offline/manager.py`, plus one new bus signal.

1. **New signal**: `download_queue_stats = Signal(int, int, float,
   float)` — `(active, total_session, speed_bps, eta_seconds)`. Add
   next to `download_progress` (`player_state.py:397`). Speed in
   bytes/sec, eta in seconds (negative = "calculating", 0 = idle).
   Emit from a GUI-thread `QTimer`.
2. **Per-job byte sampling**. In `_download_track`
   (`manager.py:479-513`), maintain `_rates: Dict[str, list[(t,
   bytes)]]`, last ~3 s of samples, updated on each chunk
   (`manager.py:505-506`). Module-level state next to
   `_queue/_active/_jobs`. Only the pool worker for that tid writes
   its entry; only the GUI-thread stats tick reads. Use atomic ref
   swap (write the new list, then point the dict entry at it) so a
   read sees old-or-new but never a torn list.
3. **Per-job totals**. Cache `total` (Content-Length, already parsed
   at `manager.py:494`) into `_jobs[tid]["total_bytes"]` and the
   running `got` into `_jobs[tid]["got_bytes"]`.
4. **Stats tick `QTimer`**, 1 Hz, GUI thread. Started lazily when
   `_active` first becomes non-empty in `_dispatch`
   (`manager.py:322-340`), stopped when `_active` and `_queue` are
   both empty and the drain-edge notification has fired. Each tick:
   compute per-tid rate from `_rates`, sum to `speed_bps`, take the
   longest-job projected ETA, read `len(_active)` and the new
   `_session_total`, emit.
5. **Drain-edge detection**. In `_finish` (`manager.py:380-418`),
   after `_dispatch()`, when `_active` and `_queue` are empty and
   `_session_total > 0`, call a new `_emit_drain_complete()` —
   builds title/body, calls `notifications.notify`, gated by the
   setting. Stop the timer. Reset `_session_total`. Track
   `_session_failed` via `_record_failure` for the body string.
6. **Settings**: add `notify_on_download_complete: bool = True` to
   `modules/settings.py`.

Estimated LOC: ~120–160 in `manager.py`, ~5 in `player_state.py`,
~3 in `settings.py`. Plus tests (§10).

---

## 8. Visual treatment

Subtle, theme-aware, no animation that pulls focus from the rest of
the page.

- **Bar**: 4 px-tall `QFrame`, `border-radius: 2px`, `BG_CARD`
  track, `ACCENT` fill via a child fixed-width widget. Re-paints at
  1 Hz — well below any flicker threshold, orders of magnitude
  under the 30 Hz "animated bars stacking up" we'd get from naive
  `QProgressBar` per row.
- **No `QGraphicsOpacityEffect`** anywhere on this surface. The
  page is a scroll area (`downloads_view.py:214-222`) and per
  `[[feedback-qgraphicseffect-scroll]]` opacity effects leave
  scrollable surfaces half-painted on Wayland.
- **Accent**: use `ui_helpers.ACCENT` at construction; restyle on
  `PlayerBus.theme_changed` per
  `[[architecture-live-accent]]`. The aggregate block joins the
  page's existing `_reapply_accent` chain.
- **Typography**: counts `TYPE_BODY`, tail `TYPE_CAPTION`. No raw
  px — see `[[feedback-typography-tokens]]`.
- **Hide-when-idle**: `setVisible(False)` on the whole block when
  the back end emits `download_queue_stats(0, 0, 0.0, 0.0)` with
  failed-count 0. No fade-in.

**Dismissed: progress ring** (implies a single quantity; we have
three). **Dismissed: per-row mini-bar** (crowds static rows).
**Dismissed: sparkline** (too eye-catching for a settings page).
**Dismissed: text-only** (no parallel-scan "about half done"
answer).

---

## 9. Edge cases

- **Pause mid-download.** In-flight tracks finish (`manager.py:543-
  544`). Aggregate switches to Paused the instant `is_paused()`
  flips. Drain-edge notification does **not** fire on pause.
- **All-failed, queue idle.** All-failed variant (§4). Drain-edge
  notification **does** fire, failures-only body.
- **Queue cleared mid-download** (Remove on the only in-flight).
  `remove` cancels (`manager.py:190-223`) and emits `("removed",
  0.0)`. Hide silently, no notification.
- **Very-fast** (< 1 s, < 1 MB). The 2 % throttle may not fire and
  the 1 Hz tick may not sample. Aggregate flashes briefly visible
  then hides; notification still fires.
- **Very-slow** (< 100 KB/s, ETA > 1 h). Show speed up to 12 h ETA;
  past that, hide ETA, keep speed visible.
- **No Content-Length** (`manager.py:494-496`). `total_bytes` is 0.
  Exclude from ETA, include in speed sum. Bar fraction uses
  `sum(got) / max(sum(got), sum(total))` so the job parks the bar
  at "what we know."
- **Wi-Fi-only blocking** (`manager.py:332`). Active jobs drain;
  new ones don't dispatch. Wi-Fi variant. When `mark_metered(False)`
  flips, slots come back as dispatch resumes.
- **Page opened mid-flight.** `DownloadsView.__init__` already
  subscribes to `download_progress` (`downloads_view.py:382`); also
  subscribe to `download_queue_stats` and prime from a one-shot
  `manager.get_queue_stats()` read (purely additive).

---

## 10. Effort + slice plan

Three slices, in order. A + C are autonomous-friendly per
`[[workflow-practices]]`; B wants pair-programming because it's the
only one touching visible chrome.

**Slice A — back-end instrumentation + tests (autonomous).** ~150
LOC in `manager.py`, ~5 in `player_state.py`, ~3 in `settings.py`,
~200 LOC of tests. Adds: byte sampling, the 1 Hz `_stats_timer`,
`download_queue_stats`, `_session_total` / `_session_failed`
counters, drain-edge detection firing `notifications.notify`,
`get_queue_stats()`. Tests in `tests/test_offline_manager_stats.py`
with a fake bus and `mark_metered` toggles to exercise §9 variants.
No UI changes.

**Slice B — aggregate UI block (pair with august).** ~120 LOC in
`downloads_view.py`. Adds a new `_QueueAggregateBlock` inserted
between the storage label and the pause row
(`downloads_view.py:232-251`); subscription to
`download_queue_stats`; the variants from §4; the visual treatment
from §8. Reuses `_fmt_size`; adds `_fmt_speed` / `_fmt_eta` next
to it.

**Slice C — notification setting + toggle (autonomous).** ~30 LOC.
Adds the "Notify me when downloads finish" checkbox, wires
`settings.notify_on_download_complete`, verifies slice A's gating.
Manual-test plan entry: kick off a multi-album download, navigate
away, confirm one notification at the end with correct counts.

Total: ~400 LOC implementation + ~250 LOC tests. Slice A unblocks
B; C depends only on A's gating field.

---

## 11. Sources

WebFetch on most reference-app help pages (Spotify, Tidal, Apple
Music, Steam, Transmission, qBittorrent) returned no substantive UI
detail or hit redirect / Anubis / 404 walls. Mix below: verified
pages where useful, direct-use references labelled as such.

- **Firefox downloads panel** — verified at
  `https://support.mozilla.org/en-US/kb/find-and-manage-downloaded-
  files`. Shows file size + status + a visual progress indicator;
  the toolbar button itself fills. Does **not** expose per-row
  speed or ETA. Validates §5's "rows stay light, aggregate carries
  the dynamic numbers" call.
- **Wikipedia "Download manager"** — verified at
  `https://en.wikipedia.org/wiki/Download_manager`. Confirms the
  category-wide convention of exposing simultaneous-transfer state.
  Cited for framing only.
- **qBittorrent** (direct use). Status-bar `↓ X.X MB/s · N active`
  plus per-row columns for ETA / speed / percent is the canonical
  "X of Y at Z" affordance that influenced §4. We drop the up-rate.
- **Transmission** (direct use). Per-torrent ETA shown as a
  relative duration ("3 min remaining"). §4's "1 min 12 s left"
  follows this over wall-clock format — relative reads better when
  watching the queue actively.
- **Apple Music macOS download manager** (direct use). Popover is
  hidden until activity exists, lists per-item progress with no
  speed text, silently disappears on drain. Closest precedent for
  jellytoast (music app, not torrent), confirms §3's "hidden when
  idle." Apple Music does **not** notify on drain — precisely the
  gap §6 targets.

---

## 12. Not changing

Per the brief, do not re-recommend shipped features:

- Pause/resume button (`downloads_view.py:240-249`,
  `manager.py:542-568`).
- Per-row pct (`downloads_view.py:177-180`).
- Per-row Re-sync (`downloads_view.py:127-131`).
- Stale badge (`downloads_view.py:187-190`).
- Wi-Fi-only toggle (`downloads_view.py:316-330`,
  `manager.py:668-690`) — input to the §4 variant, untouched.
- Offline mode / auto-offline / prefer-server checkboxes
  (`downloads_view.py:258-310`).
- Download quality combo (`downloads_view.py:334-358`).

New: aggregate block above the pause button (§3, §4, §8), back-end
instrumentation (§7), drain notification through
`modules/notifications/` (§6) plus its gating setting (slice C).
