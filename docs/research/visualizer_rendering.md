# Visualizer Rendering Widget — Spec

> **📍 Status — 2026-05-20:** Shipped. This spec drove the visualizer
> paint widget that landed 2026-05-19 (later upgraded to a Bezier
> wave). Kept for rationale — see `CHANGELOG.md`.

Status: spec — shipped (see the Shipped banner above; original spec text
retained below for rationale). Date: 2026-05-18.

Follow-up to `docs/research/visualizers.md` (umbrella). That doc made
the broad call (`QPainter` widget, third NP left-pane mode); this
one pins geometry, smoothing, colour, idle, throttling tightly enough
that a code agent can ship verbatim.

**Scope.** Spectrum bars only. Oscilloscope is a later widget.

**Out of scope.** Real mpv `lavfi-complex` audio tap (returns zeros
today — `modules/visualizer.py:218`), oscilloscope, VU meters,
spectrogram, ProjectM, settings UI panel, per-OS loopback backends.

---

## 1. Goal & non-goals

**Goal.** Render the live FFT bands from
`PlayerBus.visualizer_bands_changed` (`modules/player_state.py:423`)
as a spectrum-bar widget mounted as the third
`np_left_pane_mode = visualizer` slot on the NP page. Quiet enough to
live next to lyrics, expressive enough to read as "audio is playing
right now."

**Non-goals.** GPU/OpenGL/shaders (v2). Beat / onset / BPM / key
detection. Peak-hold caps (WinAmp-style floating squares — adds a
tunable for negligible readability win). 3D, particles, glow effects
(forbidden by `[[feedback-qgraphicseffect-scroll]]`).
Album-art-dominant or custom-hex colour modes — accent only for v1
(dominant needs a colour-quantization pipeline we don't have). Cover
or lyrics overlay — visualizer is a *mode*, not a backdrop.

---

## 2. Bar count + frequency mapping

**Pick: 32 bars, log-spaced, fixed.** No auto-calc, no runtime
16/32/64/128 selector for v1.

The backend already emits exactly 32 log-spaced mel bands across
50 Hz–16 kHz (`modules/visualizer.py:55,117-130`). Matching the
widget to the signal is the right call: one emitted float ↔ one
drawn bar, no merging/splitting. At the NP left-pane width
(~360–520 px logical, 50/50 split per
`modules/now_playing_page.py:1905-1906`) that's ~8–12 px per bar
including gap.

**Frequency mapping.** Log-spaced (already geomspace in the
backend). No further curve in the paint widget — the dB
normalisation in `compute_bands` (`modules/visualizer.py:204-209`)
already maps -80 dB → 0 and 0 dB → 1.

**Dismissed:** 64 bars (mismatches backend; umbrella doc §8's
runtime selector is a v2 settings concern, not v1 paint).
16 bars (chunky, loses high-end definition). Per-width auto-calc
(exactly the subjective call we're eliminating).

---

## 3. Bar style

**Pick: grounded vertical rectangles, square tops, 2 px horizontal gap, flat fill.**

- **Anchor:** baseline at the bottom, bars grow upward (foobar2000 /
  Strawberry style). Not centre-mirrored — mirror halves the usable
  vertical range and the pane is already short (~200 px cover slot,
  `modules/now_playing_page.py:1788`).
- **Geometry:** plain `QPainter.fillRect` rectangles. No rounded
  tops — invisible on a ~6-px-wide bar at 30 Hz. No bevels.
- **Bar width:** `floor((widget_width - (N - 1) * gap) / N)` integer
  px. Leftover pixels go into the left margin so bars stay flush
  with the widget's right edge on resize.
- **Gap:** 2 px logical.
- **Minimum bar height:** 2 px even at 0.0 value — see §6.

**Dismissed:** Centre-mirrored Spotify/Apple style (halves range).
Rounded-top rectangles (invisible at 6 px). Diamond/triangle bars
(gimmicky). Per-bar stacked-block style (dated, more code).

---

## 4. Smoothing

**Pick: asymmetric exponential smoothing (attack/release), per-band.**

Each emitted band value `b_i` is the *target*. The widget keeps a
parallel `displayed_i` array, updated each frame:

```
attack_α  = 0.35   # rise rate
release_α = 0.12   # fall rate

if b_i > displayed_i:
    displayed_i = displayed_i + attack_α  * (b_i - displayed_i)
else:
    displayed_i = displayed_i + release_α * (b_i - displayed_i)
```

Fast attack so a kick reads as a jab, slower release so bars don't
strobe at 30 Hz. These work out to ~60-ms attack and ~200-ms release
time-constants at the 30 Hz cadence.

**Tuning rule.** "Frantic/jittery" → lower `attack_α` (0.25).
"Sluggish/dead" → raise it (0.45–0.55). Release is rarely the
problem; lower to ~0.08 only if "bars hang too long." Never touch
the math shape, only the two constants.

**Dismissed:** Symmetric single-factor smoothing (attacks lose
punch). Peak-hold-with-fall (third tunable; the floating cap is
banned in §1). Per-band adaptive smoothing (v2+).

---

## 5. Colour treatment

**Pick: vertical gradient — `ACCENT_DEEP` at the baseline to `ACCENT`
at the top, flat across all bars (no per-band hue shift).**

- Read `ACCENT` + `ACCENT_DEEP` from `modules.ui_helpers`
  (`ui_helpers.py:66-67`).
- Build *one* `QLinearGradient(0, bottom_y, 0, top_y)` per paint
  pass, reused for all 32 bars.
- Gradient spans the widget's full vertical range, not each bar's
  height. Tall bars show the full ACCENT→ACCENT_DEEP band, short
  bars only show the bottom (deeper) slice. Reads as
  "energy = brightness."
- Transparent widget background — NP page paints frosted blur
  underneath (`modules/now_playing_page.py:1860-1896`).

**Live-apply** per `[[architecture-live-accent]]`: connect
`PlayerBus.theme_changed` to a `_reapply_accent` slot that just calls
`self.update()`. Per `[[feedback-signal-connects-in-init]]` the
`.connect()` lives in `__init__`, never in `_reapply_accent`. Don't
cache colours as instance attrs — re-read `ui_helpers.ACCENT` /
`ACCENT_DEEP` (kept current by `refresh_theme` —
`ui_helpers.py:420-421`) each paint.

**Dismissed:** Solid bars (flat). Per-bar frequency-rainbow HSV
(dated, fights theme). Per-bar gradient within each bar (less
informative than widget-spanning). Album-art-dominant (needs
pipeline; deferred per umbrella doc §10).

---

## 6. Idle behaviour

**Pick: slow decay to a baseline of 0.02 (≈ 2 % of widget height,
clamped to a 2-px minimum).**

- **Active + audio energy:** §4 smoothing applies.
- **Active but silent passage:** backend emits zeros
  (`modules/visualizer.py:168-170`); release smoothing pulls bars
  down to baseline. Reads as "alive, listening."
- **Paused / no track / engine dormant:** same baseline. Do NOT hold
  the last frame (looks frozen). Do NOT fake an animation (looks
  like a screensaver).

Clamp at *draw* time on the displayed values — never feed the clamp
back into smoothing state (keeps the math monotone).

**Dismissed:** Hold-last-frame (looks crashed). Fade-to-zero with no
baseline (widget vanishes, looks broken). Animated idle wash
(dishonest, distracting). Hide widget on pause (user chose this mode
deliberately).

---

## 7. Frame rate + throttling

**Pick: paint exactly when `visualizer_bands_changed` fires. No
internal timer, no FPS cap, no min interval.**

The backend already throttles to 30 Hz
(`modules/visualizer.py:72,286-303`). Painting on every signal gives
a free 30 Hz repaint with no risk of overrun: `bands_ready` arrives
on the GUI thread via QueuedConnection and `update()` coalesces.

**Visibility gating.** Skip `update()` when `self.isVisible()` is
False, but stay connected (re-connecting on show races the FFT
thread; the 32-float signal cost is negligible).

**showEvent** calls `update()` immediately so the user sees current
bands without a 33-ms wait. `hideEvent` no-ops.

**Signal stalls.** If the tap hangs, bars freeze on their last
`displayed_i`. Honest read; better than faking life.

**Battery mode is not addressed here** — we're already at the 30 Hz
floor the umbrella doc §7 prescribes for battery. Revisit if the
backend cadence climbs.

**Dismissed:** Internal 60 Hz `QTimer` interpolating between signal
events (sub-pixel smoothing invisible at 6 px). FPS-clamp via timer
skipping emits (reintroduces a tunable for no win).

---

## 8. Cast edge case

**Pick: static, centred placeholder when cast routing is active.**

When casting, mpv stops decoding locally so the FFT would freeze on
its last frame. Detect cast-active state and paint a placeholder.

**Layout** (centred in the widget rect):

- Cast icon, 48 px logical, `TEXT_DIM` tint, via the existing icon
  pipeline (implementer audits the right name).
- Below: `"Casting to <device>"` — `TYPE_CAPTION`
  (`modules/design_tokens.py:93`), `TEXT_DIM`, centred.
- Static. No spinner, no marquee.

**State source.** Subscribe to existing `PlayerBus` cast signals (see
`[[architecture-cast-proxy]]`). Don't reach into the cast manager
directly — the widget stays signal-driven.

**Transitions.** Cast-on → `update()` → placeholder. Cast-off →
`update()` → bars resume from their decayed (0.02) baseline as fresh
audio arrives. Smooth, not jarring.

**Dismissed:** Animated placeholder (this state is *paused*, not
*busy*). Parallel silent decode (umbrella doc §9 option 2 — decoding
twice for eye candy is wrong). Fall back to cover (user chose this
mode).

---

## 9. HiDPI

Per `[[architecture-hidpi-scaling]]`:

- Sizes (gap, baseline height, placeholder icon) in **logical px**;
  let PassThrough rounding scale them. `QPainter` targets logical
  coords by default.
- Do NOT call `screen_dpr()` and multiply manually — that fights
  Qt's own scaling on fractional scales
  (`[[feedback-dpr-cache-key-fragmentation]]`).
- No pixmap caching — we repaint every signal.
- Bar widths from §3's `floor(...)` are logical-px integers. Qt's
  painter anti-aliases the gaps correctly on Wayland fractional.
- **Windows / macOS:** no special handling — Qt 6 PMv2 + Retina
  Just Work for `fillRect`.
- **Multi-monitor DPR change.** Connect `PlayerBus.dpr_changed` to
  `update()`. No recalc — `self.rect()` always reports logical px.

---

## 10. Provider abstraction

**Confirmed: NONE.** Widget reads only from `PlayerBus`
(`visualizer_bands_changed`, `theme_changed`, `dpr_changed`, the
cast-active signal in §8).

The provider singleton is *not* a dependency. Jellyfin and Subsonic
both route through the same mpv handle, and the FFT taps mpv
(`modules/visualizer.py:218-251`). The widget must not import
`modules.providers` or call `get_provider()`. Sign-out / kind-switch
are zero-effort: nothing to reset.

---

## 11. Test surface

**Testable without a display.**

- Smoothing math: drive `[0.0]*32` then `[1.0]*32` through the
  smoothing function; assert `displayed_i` converges with
  `1 - (1-α)^N` shape at α=0.35.
- `paintEvent` to a `QImage` buffer: construct widget at 320×200
  logical, set known band values, render via `widget.render(image)`,
  assert non-zero pixel counts in expected column ranges.
- Idle decay: feed zeros, assert `displayed_i` decays toward the
  0.02 baseline (never below).
- Cast-active branch: set the flag, render to QImage, assert no
  vertical bar columns + non-zero centre coverage (icon + text).

**Not testable.** Visual aesthetic (august's eye, post-merge).
Wayland fractional-scale rendering. Cast-transition smoothness.

---

## 12. Effort + slice plan

**One slice.** Estimate: ~250–350 LOC across:

1. `modules/visualizer_widget.py` — new file. The `QWidget`,
   smoothing state, paintEvent, signal connects, cast placeholder.
   ~200–250 LOC.
2. `modules/now_playing_page.py` — grow `_show_lyrics: bool`
   (`now_playing_page.py:1819`) into an `np_left_pane_mode`
   tri-state (`cover | lyrics | visualizer`); swap the
   lyrics-scroll vs visualizer-widget on mode change. ~50–80 LOC.
3. `tests/test_visualizer_widget.py` — covers §11 bullets. ~100 LOC.
4. `modules/settings.py` — add the `np_left_pane_mode` typed property
   (default `lyrics` to preserve current behaviour); migrate any
   prior `_show_lyrics`-derived key on first load.

**Not in this slice.** Real audio tap. Visualizer settings panel
(umbrella doc §8 — bands count, smoothing, sensitivity, colour
mode). Oscilloscope. Battery throttle. Spectrogram. Move to
`QOpenGLWidget`. Dominant-colour mode.

**One open implementation question (defaulted).** Does the tri-state
cycle on the existing lyrics toggle button, or do we add a separate
mode chip? Default: cycle on the existing toggle, relabel
`"Lyrics ▸ Visualizer ▸ Cover"`. If august prefers three discrete
chips, that's a UI tweak inside the same slice.

---

## References

**Internal.**

- `modules/visualizer.py:50-77` — band count (32), range (50 Hz–16 kHz), window (2048), 30 Hz cadence, dB normalisation.
- `modules/visualizer.py:133-210` — `compute_bands` output contract.
- `modules/player_state.py:423` — `visualizer_bands_changed` payload.
- `modules/player_state.py:275` — `theme_changed`.
- `modules/now_playing_page.py:1788,1819,1905-1906` — cover slot, `_show_lyrics`, 50/50 split.
- `modules/now_playing_page.py:2373,2400-2415` — `theme_changed` connect + `_reapply_accent` precedent.
- `modules/ui_helpers.py:66-67,420-421` — `ACCENT` / `ACCENT_DEEP` + `refresh_theme()` contract.
- `modules/design_tokens.py:93` — `TYPE_CAPTION`.
- `docs/research/visualizers.md` — umbrella doc (§4, §5, §7, §9).
- `[[architecture-live-accent]]`, `[[architecture-hidpi-scaling]]`, `[[architecture-cast-proxy]]`.
- `[[feedback-signal-connects-in-init]]`, `[[feedback-typography-tokens]]`, `[[feedback-qgraphicseffect-scroll]]`, `[[feedback-provider-singleton-refs]]`.

**External reference implementations** (≥3 per spec):

- foobar2000 default spectrum analyzer — grounded log-spaced bars, asymmetric attack/release. [hydrogenaudio thread on default vis](https://hydrogenaud.io/index.php/topic,114503.0.html). The visual reference point for §3 + §4.
- Strawberry — block analyzer in `src/analyzer/` ([strawberry source](https://github.com/strawberrymusicplayer/strawberry/tree/master/src/analyzer)). Uses a similar log-FFT → exponential-smoothing → Qt-painter pipeline; confirms the math choice for §4.
- Tauon Music Box — `gui.py` spectrum mode. Pythonic + Qt-adjacent (uses pygame). Confirms 32-band log is the sweet spot for a small pane.
- markjay4k Audio-Spectrum-Analyzer-in-Python ([repo](https://github.com/markjay4k/Audio-Spectrum-Analyzer-in-Python)) — Python + pyqtgraph, matches our smoothing choice + grounded-bar choice.
- swharden's PyQt real-time monitor ([blog](https://swharden.com/blog/2016-07-31-real-time-audio-monitor-with-pyqt/)) — confirms `QPainter` is fast enough at 30 Hz on Python without OpenGL.

**Deliberately not modelled on:**

- Spotify desktop visualizer (centre-mirrored, gradient-fill, per-bar HSV) — too maximalist for our pane size and theme.
- WinAmp / Milkdrop — wrong era, wrong genre.
- Apple Music animated covers — not a spectrum analyzer.
