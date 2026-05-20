# Theme live-apply (Phase A)

> **📍 Status — 2026-05-20:** Not yet built. Accent color already
> live-applies, but switching the overall light/dark theme still
> needs a restart — this doc's plan is open work on the P2 list in
> `docs/TODO.md`. Kept as the design plan.

Make `theme_mode` switching between the three shipped dark themes
(`FROSTED_DARK` / `DARK` / `TRANSPARENT`) apply without restart, by
extending the existing accent live-apply contract to cover every theme
token — not just `ACCENT`. No new themes, no `rgba(255,...)` audit, no
system-auto subscription.

## 1. Goal & non-goals

**Goal.** Switching `settings.theme_mode` in Settings → Display
re-paints the whole app to the new dark theme on the next event-loop
turn, identical to the way accent picks already work. The combo's
"Restart required" notice stops showing for theme picks (it stays for
`font_scale`).

**Non-goals** (Phase B): a `LIGHT` theme (needs ~95-occurrence rgba
audit across ~15 files); a `QStyleHints.colorSchemeChanged` listener
(only useful once light exists); migrating existing accent code
(Phase A *extends* it).

Phase A unblocks both Phase B items — you cannot ship a Light theme
until mode-switching works without restart.

## 2. Existing infrastructure

The accent live-apply contract is documented at
[[architecture_live_accent]]. Verified:

- **Signal.** `PlayerBus.theme_changed = Signal()`
  (`player_state.py:275`). No args; subscribers re-pull tokens.
  Docstring at lines 266-275 already calls out theme mode +
  font_scale as still bake-at-import.
- **Token mutator.** `ui_helpers.refresh_theme()`
  (`ui_helpers.py:403-455`) re-reads `get_active_theme()` and
  mutates every theme constant in place plus rebuilds GLOBAL_STYLE.
  **Already handles mode change** — bug is nothing calls it on
  `theme_mode` writes.
- **Global cascade.** `JellytoastWindow._cascade_global_style`
  (`jellytoast.py:1420-1515`) rebuilds GLOBAL_STYLE, hard-clears +
  re-sets on QApplication, refreshes palette, force-repolishes
  every QCheckBox / QRadioButton. Wired at `jellytoast.py:581`,
  already runs on `theme_changed`. Idempotent.
- **Color-editor cascade.** `color_tokens.apply_override()`
  (`color_tokens.py:361-388`) fires `theme_changed`;
  `_emit_theme_changed` (line 603) is the central emitter.
- **`_reapply_accent()` callsites** (9 surfaces):
  - `modules/now_playing_bar.py:204` — `_VolumeSliderPopup`
  - `modules/now_playing_bar.py:571` — `_GroupVolumePopup`
  - `modules/now_playing_bar.py:1096` — `_ScrobbleBadge._reapply_accent` (delegates to `_apply_style`)
  - `modules/now_playing_bar.py:1554` — `NowPlayingBar`
  - `modules/now_playing_bar.py:2839` — `CastDialog`
  - `modules/mini_player.py:1014` — `FloatingMiniPlayer`
  - `modules/now_playing_page.py:2400` — `NowPlayingPage`
  - `modules/search_view.py:441` — `SearchView`
  - `modules/login_view.py:379` — `LoginView`
- **Equivalent under different names:**
  - `modules/top_bar.py:484` — `TopBar._apply_styling` (already
    re-pulls non-accent tokens via `from modules import ui_helpers
    as _u`)
  - `modules/artist_page.py:411` — `ArtistPage._apply_styling`
    (same pattern, full token re-pull)
  - `modules/tray.py:67` — `SystemTray._reapply_menu_styling`
  - `modules/offline_banner.py:69` — `OfflineChip._apply_style`
  - `modules/settings_dialog.py:2491` —
    `SettingsDialog._reapply_dialog_accent_styling`
- **Paint-time-free surfaces (delegate-based)** — connect
  `theme_changed → self._view.viewport().update`, the delegate
  re-reads tokens at paint:
  - `modules/library_grid.py:1977`
  - `modules/songs_view.py:513`
  - `modules/genres_view.py:294` (paint at `genres_view.py:131`
    does `from modules.ui_helpers import ACCENT as _A, ACCENT_DEEP
    as _AD` inside paint)
  - `modules/horizontal_rail.py:253`
  - `modules/now_playing_page.py:2328` (list viewport)

## 3. The gap

Most `_reapply_accent` bodies only re-stamp QSS that bakes `ACCENT`.
They do **not** re-stamp QSS that bakes `BG_PANEL`, `BORDER`, `TEXT`,
etc. — those came from `__init__`-time `from modules.ui_helpers import
BG_PANEL`, and `refresh_theme()` mutates `ui_helpers`'s `globals()`
not the *consumer module's* local binding. The local `BG_PANEL` /
`MINI_BODY_COLOR` / etc. names still point to the original strings.
Example: `LoginView._reapply_accent` (`login_view.py:379`) re-stamps
the Sign in button via a fresh `from modules.ui_helpers import ACCENT
as _ACCENT` but the combo QSS uses construction-time `TEXT` /
`BORDER`. FROSTED_DARK → DARK leaves the combo on the old border alpha
(0.08 vs 0.10).

The three **paintEvent body fills** are worst:

- `mini_player.py:30, 799` — `QColor(*MINI_BODY_COLOR)`, stale tuple.
- `settings_dialog.py:224, 2583` — `QColor(*DIALOG_BODY_COLOR)`, stale.
- `jellytoast.py:430-432, 776-787` — builds `self._body_qcolor` once
  at `__init__`; paintEvent reads the cached QColor. **Worst** — the
  main window body never changes without a restart.

## 4. Recommended contract: option (a) — rename `_reapply_accent` → `_reapply_theme` and extend

**Pick (a). Rename + extend every existing `_reapply_accent` to
`_reapply_theme`, and have it re-pull non-accent tokens too.**

Why not (b) — add a second `_reapply_theme` next to `_reapply_accent`:
two methods doing structurally identical work, double the bus
subscriptions, double the chance of one going stale. Theme switches
already need to rebuild every accent-derived QSS string (because the
themes can differ in border alphas, e.g.
`border_accent: rgba(150,125,225,0.35)` in FROSTED vs `0.45` in
SOLID). Splitting "accent" from "theme" is a false distinction at
runtime.

Why not (c) — `ThemedMixin` with auto-discovery: over-engineering. We
have ~14 surfaces, each currently with hand-written QSS. A mixin would
have to either reach into every QSS-emitting method (impossible
without inspecting source) or duplicate the QSS into a registry.
Skip.

**The new contract:**

- Every surface that consumes any theme token (accent OR non-accent)
  has a single `_reapply_theme(self)` method.
- That method's first statements re-import every token it consumes
  from `modules.ui_helpers` (`from modules import ui_helpers as _u`,
  or per-name late imports). Never read instance-cached copies.
- One bus subscription: `PlayerBus.get().theme_changed.connect(
  self._reapply_theme)`, wired in `__init__` / `_connect_bus` /
  equivalent. Never inside `_reapply_theme` itself
  ([[feedback_signal_connects_in_init]]).
- For paintEvent-only surfaces (mini player body, dialog body, main
  window body): subscriber drops the cached `_body_qcolor` /
  re-imports the tuple inside paintEvent. See section 8.

Signal fanout order (verified, matches `_on_accent_picked` at
`settings_dialog.py:2436-2490`):

1. `settings.theme_mode = chosen`
2. `ui_helpers.refresh_theme()` (mutates module constants, rebuilds
   GLOBAL_STYLE)
3. `icons.refresh_theme()` (refreshes `ICON_ACCENT`)
4. `app.setStyleSheet("")` then `app.setStyleSheet(new_global_style)`
   (cache-bust) + `app.setPalette` (Highlight role)
5. `PlayerBus.theme_changed.emit()` — broadcasts to every
   `_reapply_theme`

The Settings dialog's own `_reapply_dialog_accent_styling` already
runs on the bus connection at `settings_dialog.py:420`, so it gets
swept along. The main window's `_cascade_global_style` already runs.

## 5. Surface inventory

Classification for every surface that has a styling-restamp method
today, plus surfaces that need one added for Phase A.

### Trivial (rename + already pulls all tokens late) — 5 surfaces

These already do `from modules import ui_helpers as _u` inside their
`_apply_styling` / `_apply_style` / `_menu_qss` builders. Effort:
rename (or keep current name as `_reapply_theme` alias) + verify they
cover every QSS string in the widget.

- `modules/top_bar.py:484` — `TopBar._apply_styling`. Already calls
  `self._icon_btn_qss()`, `_view_btn_qss()`, `_search_btn_qss()` —
  all late-import tokens. ~1 LoC: rename + connect to
  `_reapply_theme`.
- `modules/artist_page.py:411` — `ArtistPage._apply_styling`. ~1 LoC.
- `modules/tray.py:67` — `SystemTray._reapply_menu_styling`. ~1 LoC.
- `modules/offline_banner.py:69` — `OfflineChip._apply_style`. ~1 LoC.
- `modules/now_playing_bar.py:1084-1097` — `_ScrobbleBadge._apply_style`
  + `_reapply_accent`. ~1 LoC.

### Needs extension (only restamps accent today) — 8 surfaces

Each restyles ACCENT-bearing QSS or `accent_icon()` today; needs
non-accent QSS re-stamped via late-import builders. ~5–15 LoC each.

- `mini_player.py:1014` — `FloatingMiniPlayer`. Today: heart + idle
  play icon. Add: per-panel `TEXT`/`TEXT_DIM` re-stamp, paintEvent
  invalidate. ~10 LoC + paint (section 8).
- `now_playing_page.py:2400` — `NowPlayingPage`. Today: heart CTA +
  viewport invalidate (covers tracks). Add: page-level QSS (scrollbar,
  list container). ~10 LoC.
- `now_playing_bar.py:1554` — `NowPlayingBar`. Today: shuffle/repeat/
  heart icons. Add: bar background + streaming-info pill. ~15 LoC.
- `now_playing_bar.py:204` — `_VolumeSliderPopup`. Today: slider QSS.
  Add: popup body (POPUP_OPAQUE_FILL + BORDER_ACCENT). ~5 LoC.
- `now_playing_bar.py:571` — `_GroupVolumePopup`. Same + per-speaker
  columns. ~10 LoC.
- `now_playing_bar.py:2839` — `CastDialog`. Today: banner + Cast btn.
  Add: list rows, section headers (`BORDER`, `BG_PANEL`, `TEXT_DIM`)
  + paintEvent fix (section 8). ~10 LoC.
- `search_view.py:441` — `SearchView`. Today: input. Add: empty-state
  + section header colors. ~5 LoC.
- `login_view.py:379` — `LoginView`. Today: submit btn + combo. Add:
  card background, field labels. Reachable on sign-out. ~10 LoC.

### Paint-time free — 5 surfaces

Delegate `paint()` reads tokens via late import every frame, so a
`viewport().update()` triggered by the bus is enough. No new code
needed; existing wiring stands.

- `modules/library_grid.py:1977`
- `modules/songs_view.py:513`
- `modules/genres_view.py:294` (delegate at line 131 already late-imports)
- `modules/horizontal_rail.py:253`
- `modules/now_playing_page.py:2328` (list viewport — track delegate
  already re-reads ACCENT per [[architecture_live_accent]])

### New surfaces needing a method added — 3 (paintEvent only)

These have NO restamp method today because they were accent-free; they
need paintEvent fixes for body fills (see section 8).

- `JellytoastWindow` (main window body) — `jellytoast.py:430-432, 776-787`
- `FloatingMiniPlayer.paintEvent` — `mini_player.py:776-801`
- `SettingsDialog.paintEvent` — `settings_dialog.py:2566-2587`

### Total

- Trivial: 5 surfaces, ~5 LoC each = ~25 LoC
- Extension: 8 surfaces, avg ~10 LoC = ~80 LoC
- Paint fixes: 3 surfaces, ~3 LoC each = ~9 LoC
- New plumbing in `_on_theme_changed`: ~20 LoC
- Tests: ~80 LoC

Estimated total: **~210 LoC**, Phase A is a **single M-sized PR**.

## 6. `refresh_theme()` changes

`refresh_theme()` already does the heavy lifting. The remaining
work is in `settings_dialog._on_theme_changed`
(`settings_dialog.py:1809-1812`), which today only persists the
setting and refreshes the restart-notice visibility:

```python
def _on_theme_changed(self):
    chosen = self._theme_combo.currentData() or "frosted_dark"
    self.s.theme_mode = chosen
    self._refresh_restart_notice_visibility()
```

Phase A rewrites it to mirror `_on_accent_picked`
(`settings_dialog.py:2436-2490`):

1. `self.s.theme_mode = chosen`
2. `ui_helpers.refresh_theme()`
3. `icons.refresh_theme()`
4. `app.setStyleSheet("")` + `app.setStyleSheet(new_global)` +
   `app.setPalette(...)` (or just emit `theme_changed` and let
   `_cascade_global_style` do it — it's already wired)
5. `self._reapply_dialog_accent_styling()` (renamed to
   `_reapply_dialog_theme_styling`)
6. `PlayerBus.theme_changed.emit()`
7. `self._refresh_restart_notice_visibility()` (which now only
   considers `font_scale`)

The Colors page's `_emit_theme_changed` path
(`color_tokens.py:603-610`) is unchanged — token-level overrides
still fire the same signal.

## 7. Settings UI

The "Light theme coming in a future build" caption at
`settings_dialog.py:1700-1702` is fine to leave. The restart-notice
hook is `_refresh_restart_notice_visibility` at
`settings_dialog.py:1797-1807`:

```python
dirty = (
    self._theme_combo.currentData() != self._initial_theme
    or self.s.font_scale != self._initial_font_scale
)
```

Phase A change: remove the theme_combo clause. After Phase A, picking
a new theme is a fully live change and not "restart required."
Font-scale clause stays.

## 8. Edge cases

**paintEvent body fills (load-bearing).** Three surfaces paint their
own body via `QColor(*BODY_COLOR_TUPLE)`:

- `JellytoastWindow.paintEvent` (`jellytoast.py:776-787`) reads cached
  `self._body_qcolor`. Fix: switch paintEvent to
  `QColor(*ui_helpers.BODY_COLOR)` (read via live module attr, cheap
  — one dict lookup). The `_OPAQUE_BODY=force alpha 255` branch moves
  inside paintEvent too.
- `FloatingMiniPlayer.paintEvent` (`mini_player.py:776-801`). Switch
  `QColor(*MINI_BODY_COLOR)` → `QColor(*ui_helpers.MINI_BODY_COLOR)`.
  Connect `theme_changed → self.update()` to invalidate.
- `SettingsDialog.paintEvent` (`settings_dialog.py:2566-2587`). Same
  fix + `self.update()` (already subscribed; extend it).

This is **make-or-break** for Phase A — without it, the main window
body wins over every child re-stamp.

**Color-editor overrides.** Verified at `ui_helpers.py:438-453`:
`refresh_theme()` calls `color_tokens.load_persisted_overrides()`
after mutating defaults, so user edits survive mode switches. No
change.

**TRANSPARENT.** Per `theme.py:71-83`, TRANSPARENT relies on alpha
(110/255) for wallpaper bleed; there's no KWin blur wiring (no
PySide6 binding for `org_kde_kwin_blur`). Mode-switch needs no KWin
work — paintEvent fix is sufficient. Caveat: `JT_OPAQUE=1` overrides
the alpha to 255 (diagnostic path); document but don't fix.

**`accent_icon()` cache.** `icons.refresh_theme()` runs in
`_on_accent_picked` step 3 and must also live in `_on_theme_changed`.
Trivial.

**Frame radii.** `BODY_RADIUS = 12` etc. don't differ between themes;
safe to stay construction-baked.

## 9. Test surface

Three headless layers (`tests/conftest.py` + `pytest-qt`):

- **Layer 1 — token propagation.** Set `settings.theme_mode = "dark"`,
  call `ui_helpers.refresh_theme()`, assert `ui_helpers.BG_PANEL ==
  "#181818"` (DARK vs FROSTED_DARK's `"#1a1a1a"`). Parametrize across
  the three themes. Pure-Python. ~30 LoC.
- **Layer 2 — per-surface restamp.** For each of the 13 surfaces with
  a method, construct under `QApplication([])`, snapshot
  `widget.styleSheet()`, mutate theme, call `_reapply_theme()`, assert
  the stylesheet contains a theme-unique substring. ~50 LoC.
- **Layer 3 — signal fanout.** `patch.object` each `_reapply_theme`,
  emit `PlayerBus.theme_changed`, assert one call each. Catches missed
  `.connect()`. ~20 LoC.

**Out of scope headless:** paintEvent body color changes (live in
`QPainter.fillRect()`, not QSS). Lowest-effort: `widget.grab()` +
center-pixel sample. Acceptable but spot-check at runtime instead.

## 10. Slice plan

**Single Phase A PR.** Splitting it loses the atomic-correctness
benefit of doing every surface in one commit: a half-renamed
codebase has surfaces still calling `_reapply_accent` while the bus
is firing `_reapply_theme`-style work, fragmenting the user
experience across surfaces ("My top bar updated but my mini player
didn't"). Atomic rename has a sharper diff and a sharper
mental model.

Estimated **M** (~210 LoC code + ~80 LoC tests, single commit chain).

## 11. What does NOT change

- No new themes. `THEMES = {FROSTED_DARK, DARK, TRANSPARENT}`
  untouched.
- No `rgba(255,255,255,…)` audit (Phase B).
- No `QStyleHints.colorSchemeChanged` listener.
- No `_reapply_accent` callers preserved — every callsite renames to
  `_reapply_theme`. Grep for the old name before merging.
- `font_scale` stays restart-required; restart-notice keeps that path.
- `lyrics_font_size_changed` (separate live signal) untouched.
- Colors page keeps its `theme_changed` subscription for slider
  re-load (independent of styling).

## Top risk

The paintEvent body-fill fix is the only piece that touches
QPainter-driven rendering. Misorder it (e.g. update `_body_qcolor`
after `theme_changed.emit()` instead of before, or forget the
`self.update()`) and the user sees the new widget styling but the
old body color for one to several frames until the next paint
event — a flash. Verify each of the three paintEvent surfaces (main
window, mini player, settings dialog) triggers a `self.update()` in
the order: token mutate → update() → next paint reads new tuple.
Test by sampling pixel centers after a mode switch.
