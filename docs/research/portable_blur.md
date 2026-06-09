# Portable Frosted-Glass Base Layer for jellytoast — Design

> Research + design for making "Frosted dark" transfer correctly across
> machines and OSes (KDE→KDE first, then other Linux DEs, then Windows).
> Produced 2026-06-04 from a multi-agent research+verification sweep.
> Load-bearing API facts adversarially verified, then the KDE facts
> re-confirmed on real hardware (see "Hardware verification" below).
>
> **⚠️ §5 SUPERSEDED (updated 2026-06-08):** §5 recommended Mica and said
> "do NOT wire Acrylic". As built, the Windows DEFAULT is real **Acrylic**
> blur-behind (`modules/blur/_dwm.py` `apply()` → `apply_acrylic()` via
> `ACCENT_ENABLE_ACRYLICBLURBEHIND`); Mica is now only the `JT_NO_WIN_BLUR`
> fallback. See `reference_windows_acrylic_blur` (memory) for the cracked
> recipe. §5's DWM constants / build gates still describe that Mica
> fallback, so the section is kept.

## Hardware verification (august's KDE Wayland box, 2026-06-04)

Every load-bearing KDE claim confirmed on `/usr/lib/libKF6WindowSystem.so.6`:

- `nm -D` shows all three symbols the design binds:
  - `_ZN14KWindowEffects17isEffectAvailableENS_6EffectE`  (the flagged-risky probe)
  - `_ZN14KWindowEffects16enableBlurBehindEP7QWindowbRK7QRegion`  (already used)
  - `_ZN10KX11Extras17compositingActiveEv`  (X11-only)
- Qt plugin dir present: `/usr/lib/qt6/plugins/kf6/kwindowsystem/`
  with `KF6WindowSystemKWaylandPlugin.so` + `KF6WindowSystemX11Plugin.so`.
- Session: `XDG_SESSION_TYPE=wayland`, `XDG_CURRENT_DESKTOP=KDE`.
- DBus Layer-B cross-check works verbatim: `qdbus6 org.kde.KWin /Effects
  isEffectLoaded blur` → `true`; `qdbus6 org.kde.KWin /Compositor active`
  → `true`. `kreadconfig6 --file kwinrc --group Plugins --key blurEnabled`
  → `true`.

So on a working KDE box the design yields `ACTIVE` → 172 (Frosted dark
unchanged). The detection mechanism is real and implementable as written.

---

## 1. The core problem & principle

Frosted dark paints the window body at ~67% opacity (RGBA `18,18,18,172`) and **relies on a compositor backdrop** (KWin blur) for legibility. Today `modules/blur/apply()` is fire-and-forget: the ctypes `enableBlurBehind` call returns `void`, our wrapper `return True` merely because the C call didn't raise (`_kwin.py:106`), and `is_supported()` only proves the `.so` dlopened + the symbol resolved (`_kwin.py:32-57`). None of that proves the **compositor actually blurred**. When it silently no-ops — missing `kwindowsystem` Qt plugin, KWin Blur effect off, no compositing, mis-detected GPU — the 67% body renders straight-through and Frosted dark looks broken. This is exactly the second-laptop "fully transparent" report.

**The principle — "who owns the glass":**

> The frosted look is produced by exactly one of two parties. Either the **OS/compositor owns the glass** (KWin blur landed, Windows Mica accepted, macOS vibrancy added) — and the app body stays translucent (~67%) and *rides* that real backdrop — **or the app owns the glass**: when no real backdrop is verified, the app paints a **near-opaque tinted body** (~92%) so Frosted dark still reads as a dark frosted panel and is *never* see-through-broken.

The body opacity must therefore become a **function of a detected, verified blur status**, not a per-theme constant. Frosted dark's identity (hue `18,18,18`, rounded corners, soft elevation washes) is preserved in both branches; only the alpha changes. The decision must be made **before first paint** so the window never flashes see-through.

Two hard-won caveats from verification frame everything below:
- **"Request issued" ≠ "blur visible."** A documented Plasma-6/X11/NVIDIA case sets the `_KDE_NET_WM_BLUR_BEHIND_REGION` atom correctly and loads the Blur effect, yet KWin skips the render pass on a GPU-capability mis-detection. So even a *correct* apply call can no-op. Detection must probe capability, not trust the call.
- **A Windows `S_OK` HRESULT (genuine feedback KDE lacks) confirms the API *accepted* the attribute, not that the effect *rendered*.** Keep the dark-tint body as a legibility floor on every platform regardless of status.

---

## 2. Blur status model

Replace the swallowed `bool` with a single status enum that every backend returns. This is the **one contract** the theme layer consumes.

```python
class BlurStatus(enum.Enum):
    ACTIVE                 = "active"        # issued AND positive evidence it landed → glass body
    REQUESTED_UNVERIFIABLE = "unverifiable"  # issued, cannot confirm → conservative (opaque) body
    UNSUPPORTED            = "unsupported"   # backend cannot even issue → opaque body
    DISABLED               = "disabled"      # theme.blur is False (Solid dark) → not an error
```

(`FAILED` collapses into `UNSUPPORTED` for the theme's purposes — both mean "no real backdrop, paint opaque." Keep a human-readable `reason` string alongside the enum for the boot log + install doctor, but the theme only branches on these four.)

**Theme → body-alpha mapping.** The theme exposes two alphas per surface instead of one, and selects by status:

| Status | Body alpha (main) | Rationale |
|---|---|---|
| `ACTIVE` | **172** (~67%, today's value) | real backdrop owns the glass; exact Frosted dark |
| `REQUESTED_UNVERIFIABLE` | **236** (~92.5%, `0xEC`) | conservative — we can't prove blur, never show see-through |
| `UNSUPPORTED` | **236** | app owns the glass; reads as a dark frosted panel |
| `DISABLED` | **255** | Solid dark — fully opaque by definition |

**Default for `REQUESTED_UNVERIFIABLE` is opaque (236), not glass.** The failure cost of "pretty but broken see-through" (the original bug) vastly exceeds "slightly more opaque than strictly necessary." A future Settings opt-in ("trust blur on this machine") can flip an unverifiable box to 172, but the safe default never renders the broken window.

The frosted-fallback alpha (236, range 232–242 / `0xE8`–`0xF2`) keeps a faint translucency at the very edges/corners so it still reads "frosted," strictly better than jumping to Solid-dark 255. The existing `JT_OPAQUE=1` hard override (`jellytoast.py:214`) stays as the manual escape hatch → forces 255 and skips `blur.apply` entirely (covers the screencast-flicker case `QTBUG-128029` and any machine where the probe is wrong).

---

## 3. KDE/KWin (priority 1 — the KDE→KDE fix)

Flagship goal: **Frosted dark must transfer EXACTLY KDE→KDE**, and on a KDE box where blur won't land it must degrade, not break.

### Detection strategy (two layers)

**Layer A — the portable primitive: `isEffectAvailable(BlurBehind)` via ctypes.** Bind, from the *same* `libKF6WindowSystem.so.6` we already load:

- Symbol: `_ZN14KWindowEffects17isEffectAvailableENS_6EffectE` (note **17**, the Itanium length prefix for `isEffectAvailable`; existing `enableBlurBehind` uses **16**, `_kwin.py:25`). **Confirmed present on hardware.**
- `argtypes = [ctypes.c_int]`, `restype = ctypes.c_bool`, call with `7` (the `BlurBehind` enum value — verified against `kwindoweffects.h`).
- Probe **after** the QWindow exists and the platform plugin is bound (post-`show()`), in the same deferred hook that already calls `_blur.apply` (`jellytoast.py:404`). A `QApplication` is *necessary but not sufficient* — the answer also needs the compositor to offer blur.

What this primitive actually checks (verified against KWindowSystem source):
- **Wayland:** if the `ext_background_effect_manager_v1` manager is active, returns *its* `supportsBlur` capability bit; else returns whether the legacy `org_kde_kwin_blur_manager` is active. (Capability bit, not mere global-binding.)
- **X11:** returns `false` if `!KX11Extras::compositingActive()`, then `true` only if the `_KDE_NET_WM_BLUR_BEHIND_REGION` atom is present among root-window properties.

This single call answers `ACTIVE` vs `UNSUPPORTED` for the common cases. Critically, it sees what our dlopen-only `is_supported()` cannot: **a missing Qt plugin** (`KF6WindowSystemKWaylandPlugin.so` / `KF6WindowSystemX11Plugin.so` under `kf6/kwindowsystem/`) makes `isEffectAvailable` return `false` even when the `.so` resolves. That is the likeliest second-laptop cause.

> **Hardcoded-symbol safety:** mangled ABI strings can drift. `_resolve()` must bind `isEffectAvailable` in a `try/except (AttributeError, KeyError)` exactly like the existing `enableBlurBehind` bind — if the symbol is absent, treat as "can't probe" → `REQUESTED_UNVERIFIABLE`, never crash. The install doctor should run `nm -D libKF6WindowSystem.so.6 | grep -E 'isEffectAvailable|compositingActive'` on the target and warn if absent.

**Layer B — KDE-only confirmatory cross-check via QtDBus (for the WHY-log + tie-breaks).** **Plasma-only** D-Bus names — do not treat as portable:
- Blur effect loaded: `org.kde.KWin` path `/Effects`, method `isEffectLoaded("blur")` (the effect id is the string **`blur`**). **Confirmed → `true` on hardware.**
- Compositing active: `org.kde.KWin` path `/Compositor`, property `active`. **Confirmed → `true` on hardware.**

Use Layer B to demote: if Layer A says available but D-Bus says `isEffectLoaded("blur")` is false or compositing inactive, log the reason and stay conservative. If Layer A is `True` and D-Bus confirms, log `ACTIVE` with high confidence.

**Guard `KX11Extras::compositingActive()` strictly behind X11.** On Wayland it emits a runtime warning and returns an unreliable value — never call it there. Gate with `KWindowSystem::isPlatformX11()` (or `os.environ["XDG_SESSION_TYPE"] == "x11"`). On Wayland, assume compositing is on and rely on Layer A.

### Apply path

Unchanged: keep `enableBlurBehind` via ctypes (`_kwin.py`), empty `QRegion` for whole-window, rounded region for frameless dialogs (the existing `_rounded_region` in logical coords — do **not** pre-multiply by DPR; KWindowSystem scales). Go *through* KWindowSystem, never bind `org_kde_kwin_blur` directly: KDE is removing the legacy protocol in favor of `ext-background-effect-v1`, and KWindowSystem abstracts whichever the running KWin speaks. The bool return of `apply` becomes meaningless — the **status comes from the Layer-A probe**, computed once and cached.

### When detection says "blur won't land" on a KDE box

- **Wayland, available=False:** plugin missing or compositor offers no blur → `UNSUPPORTED` → paint the 236 frosted-fallback body. Log one INFO line + a one-time non-modal Settings note ("Frosted blur needs KWin's Blur effect / kwindowsystem; showing a near-opaque body — enable Desktop Effects → Blur, or pick Solid dark").
- **X11, available=True but the GPU-mis-detect failure mode:** the atom is set and the effect loaded, yet KWin may still skip rendering. We cannot detect this from the client. **Recommendation: on KDE *X11*, default `REQUESTED_UNVERIFIABLE` → 236**, and surface the `JT_OPAQUE` hint. KDE *Wayland* with Layer-A `True` + D-Bus confirm is the only path that earns the full `ACTIVE` 172 body. This keeps "exact KDE→KDE" true on the dominant (Wayland) KDE config without gambling on the known-flaky X11 path.

### Packaging dependency

`kwindowsystem` is **not** in the AUR PKGBUILD depends. Blur is progressive enhancement, but Frosted *legibility* depends on it. **Add `kwindowsystem` to `optdepends`** with a note:

```
'kwindowsystem: window blur for the Frosted theme (KDE)'
```

The install doctor (`dev/install_doctor.py`) must additionally check: `CDLL("libKF6WindowSystem.so.6")` loads **and** the `kf6/kwindowsystem/` plugin dir exists, and print the `pacman` remedy if missing. Flatpak `org.kde.Platform` bundles the framework — no manifest change — but the doctor should verify the sandbox Qt plugin path resolves to the runtime's `kf6/kwindowsystem` plugins (untested; see §10).

---

## 4. Other Linux DEs

As of mid-2026 the *only* compositors that honor an **app-requested** blur are KWin, niri 26.04, and (eventually) COSMIC — all via `ext-background-effect-v1` (merged to wayland-protocols 2025-05-27, shipped 1.45 on 2025-06-13, *staging* v1). Everything else is user-config or nothing.

| DE / compositor | Real app-requested blur? | What jellytoast does |
|---|---|---|
| **KWin (Wayland)** | **Yes** — `ext-background-effect-v1` (or legacy) via KWindowSystem | `ACTIVE` 172 when Layer-A probe confirms |
| **KWin (X11)** | Yes-ish — atom honored, but can silently skip (GPU mis-detect) | `REQUESTED_UNVERIFIABLE` 236 (conservative) |
| **niri 26.04** | **Yes** — speaks `ext-background-effect-v1`, zero config | `ACTIVE` 172 **iff** KWindowSystem installed and Layer-A reports the global; else 236 |
| **COSMIC** | Coming (Epoch 2, Dual-Kawase, no ship date early 2026) | Treat as no-blur today → 236; revisit |
| **GNOME/Mutter** | **No** — no compositor blur protocol, no app path (`_MUTTER_HINTS=blur-provider` hack removed in Blur-My-Shell v60) | `UNSUPPORTED` 236 |
| **Cinnamon/Muffin** | **No** standard path (request `linuxmint/wayland#182` unstaffed; "Blur Cinnamon" experimental/buggy, user-installed) | `UNSUPPORTED` 236 |
| **XFCE/xfwm4, MATE/Marco** | **No** app API | `UNSUPPORTED` 236 |
| **wlroots: Hyprland / SwayFX / Wayfire** | **No app protocol** — blur is 100% user-config keyed on app_id | `UNSUPPORTED` 236; **document the user-side lever** |

**The `app_id` window-rule story (wlroots).** We already set the Wayland `app_id` to the stable string **`jellytoast`** via `setDesktopFileName`. We cannot request blur, but the user can target us:
- **Hyprland:** blur is global and **ON by default**, so it may already work — document *not* to add a `noblur` rule. Explicit rule, modern syntax: `windowrule = noblur, class:^(jellytoast)$`. **`windowrulev2` is deprecated** — don't ship it in docs.
- **SwayFX / Wayfire:** per-app/layer blur config keyed on `app_id = jellytoast` (best-effort; SwayFX app-window blur is unreliable vs its layer-effects support).

Because Hyprland blur may already be landing but we report `UNSUPPORTED`, the 236 body is safe there too — reads as a slightly-more-opaque frosted panel even if blur is active. Acceptable; never broken.

**The X11 atom story.** `enableBlurBehind` on XCB auto-interns `_KDE_NET_WM_BLUR_BEHIND_REGION` and sets it on the window. Only **KWin and Deepin** read it. picom and generic X11 compositors ignore it — picom blurs only via its own `blur-background` user config. So on any non-KDE X11 session: `UNSUPPORTED` → 236.

**Detection without `wayland-info` (recommended default):** don't add a `wayland-utils` dependency for v1. The Layer-A `isEffectAvailable` probe already answers correctly wherever KWindowSystem is installed (KWin *and* niri). Where KWindowSystem is absent (a niri box with no KF6), we cannot issue blur anyway → `UNSUPPORTED` 236. Use a cheap env heuristic (`XDG_CURRENT_DESKTOP`/`XDG_SESSION_TYPE`) only to decide *whether to bother probing* and to populate the WHY-log; never as the sole gate.

---

## 5. Windows 11 Mica (+ Win10/older fallback)

> **⚠️ SUPERSEDED (2026-06-08):** the as-built Windows default is real
> **Acrylic** blur-behind, not Mica — `modules/blur/_dwm.py` `apply()`
> calls `apply_acrylic()` (`ACCENT_ENABLE_ACRYLICBLURBEHIND`) unless
> `JT_NO_WIN_BLUR` is set, in which case it falls back to the Mica
> backdrop described below. So the "Win10 / Acrylic: do NOT wire it"
> recommendation at the end of this section was reversed in practice.
> The DWM constants + build gates below still describe the live Mica
> fallback; read the Acrylic recipe in `reference_windows_acrylic_blur`.

Mica is achievable from PySide6 with a vendored ~40-line ctypes routine, **no PyPI dependency**. Unlike KWin, `DwmSetWindowAttribute` returns an `HRESULT` — real success feedback, the thing the original bug lacked.

### The Qt-translucency crux (resolved)

Mica composites **behind** the window, visible only through transparent Qt pixels. So:
- **`WA_TranslucentBackground` HELPS Mica — it is required.** Omitting it leaves Qt's opaque backbuffer, which **blacks out** Mica.
- jellytoast already sets `WA_TranslucentBackground` (`jellytoast.py:342, 824`) and is frameless — both Qt-on-Windows preconditions for an alpha channel are met (frameless **or** an OpenGL surface; we have frameless).
- **Keep the ~67% `_body_qcolor` fill** — it sits *on top* of Mica and tints it dark (the Windows analog of KWin blur). Do **not** make the central widget fully transparent. Caveat (eyeball on hardware): Mica is a slow, wallpaper-derived low-frequency blur, **not** a live blur-behind, so it's an *approximate* analog; tune the Windows body alpha (likely ~120–150, lighter than Linux's 172) for the legibility/Mica-show-through sweet spot.

### The ctypes recipe (corrected constants & gates)

Run **after `show()`** — in `showEvent` or `QTimer.singleShot(0, ...)`, GUI thread — because **Qt 6.8+ re-runs native window setup and overwrites constructor-time DWM calls** (mirrors our already-deferred `_blur.apply`).

```python
# modules/blur/_dwm.py  (vendored, zero deps; ctypes + dwmapi.dll which ships with Windows)
_DM     = 20    # DWMWA_USE_IMMERSIVE_DARK_MODE
_SBT    = 38    # DWMWA_SYSTEMBACKDROP_TYPE      (documented; build 22621+)
_LEGACY = 1029  # DWMWA_MICA_EFFECT (undocumented; build 22000..22620, value 1 = Mica)
_MAIN   = 2     # DWMSBT_MAINWINDOW (Mica)

sa = ctypes.windll.dwmapi.DwmSetWindowAttribute
sa.restype  = ctypes.c_long                       # HRESULT — MUST be c_long to read 0x80070057 as signed
sa.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]

sa(hwnd, _DM, byref(c_int(1)), 4)                  # dark titlebar
DwmExtendFrameIntoClientArea(hwnd, MARGINS(-1,-1,-1,-1))   # glass sheet over whole window
build = sys.getwindowsversion().build
if build >= 22621:
    hr = sa(hwnd, _SBT, byref(c_int(_MAIN)), 4)    # documented Mica
elif build >= 22000:
    hr = sa(hwnd, _LEGACY, byref(c_int(1)), 4)     # legacy undocumented Mica (value 1 ONLY)
else:
    return BlurStatus.UNSUPPORTED                  # Win10 and older → opaque
return BlurStatus.ACTIVE if hr == 0 else BlurStatus.REQUESTED_UNVERIFIABLE
```

**Corrected facts baked in (from verification):**
- `DWMWA_SYSTEMBACKDROP_TYPE` = **38**, min build **22621**; enum `DWMSBT_MAINWINDOW=2` (Mica), `DWMSBT_TRANSIENTWINDOW=3` (Acrylic), `DWMSBT_TABBEDWINDOW=4` (Mica Alt).
- Legacy attr **1029 takes value `1` only** for default Mica. (The "1029 value 4 for Mica Alt" claim is **wrong**.) We only ship default Mica → use **1029 → 1**.
- Gate at **22621** (documented), mirroring winmica — **not** pywinstyles (which only sets undocumented 1029, never documented 38). **Vendor our own routine; do not depend on pywinstyles.**
- `winId()` → HWND is `int(widget.winId())`; valid only post-show.
- `DwmExtendFrameIntoClientArea(-1,-1,-1,-1)` required on the legacy path, harmless-recommended on 22621+. Keep it.
- `restype = c_long` required to read `E_INVALIDARG = 0x80070057` (high bit set) as a failure.

### Success detection & fallback

- `HRESULT == 0` (`S_OK`) → `ACTIVE` → keep `WA_TranslucentBackground` + the dark body (tinted Mica). **But** `S_OK` proves the attribute was *accepted*, not *rendered* — so the dark-tint floor stays as legibility insurance. **(SUPERSEDED — as built:** this `WA_TranslucentBackground`-kept description applies only to the **Mica `JT_NO_WIN_BLUR` fallback** and to the layered mini player / dialogs. The DEFAULT Acrylic main-window path instead **drops** `WA_TranslucentBackground` — a NON-layered window — so Acrylic blurs behind it; see `modules/blur/_dwm.py:8-11` and `jellytoast.py` `_win_blur`.**)**
- `HRESULT != 0`, or build < 22000 → **opaque body (255)**, the existing `JT_OPAQUE` path. Log one non-modal line.
- **Win10 / Acrylic: do NOT wire it.** `SetWindowCompositionAttribute(ACCENT_ENABLE_ACRYLICBLURBEHIND)` causes severe drag/resize lag since 1903 and drops on maximize. Win10 → opaque, full stop.

---

## 6. macOS (deferred / untestable)

No Mac available → **design-doc row only, ship a stub** (`modules/blur/_macos.py` → `UNSUPPORTED`). When a Mac arrives, proven path (pyqt-liquidglass, MIT):

1. Post-`show()`, bridge the widget to its NSView (`int(widget.winId())` *is* the NSView pointer on macOS), then `.window()` for the NSWindow. Re-apply if Qt recreates the native view.
2. `nswindow.setOpaque_(False)` + `setBackgroundColor_(NSColor.clearColor())`.
3. Insert an `NSVisualEffectView` (material; `setBlendingMode_(BehindWindow)`; `setState_(Active)`) **`NSWindowBelow`** the Qt root view (content over the effect view gets no vibrancy — it must sit *below*).
4. macOS 26+: prefer `NSGlassEffectView` (Liquid Glass) via `objc.lookUpClass`, falling back to `NSVisualEffectView`.
5. Keep the Qt body at reduced alpha (same model); let the NSVisualEffectView own the translucency.

Dep: `pyobjc-framework-Cocoa` as a **macOS-only extra**. Don't ship until testable (project policy: no untestable Apple code).

---

## 7. Proposed code architecture

Faithful to existing `modules/blur/` (`__init__.py` dispatch + `_kwin.py` + `_unsupported.py`), `modules/theme.py` (`body_color` tuples + `blur: bool`), `jellytoast.py` (`_body_qcolor`, `paintEvent`).

### `modules/blur/` — backend-per-platform, status-returning

- **`modules/blur/__init__.py`** — add `class BlurStatus(enum.Enum)` (§2). Dispatch grows a Windows arm:
  ```python
  if sys.platform.startswith("linux"):  from modules.blur import _kwin as _backend
  elif sys.platform == "win32":         from modules.blur import _dwm as _backend   # NEW
  elif sys.platform == "darwin":        from modules.blur import _macos as _backend # NEW stub
  else:                                  from modules.blur import _unsupported as _backend
  ```
  Public API becomes:
  - `apply(widget, enabled, corner_radius=0) -> BlurStatus` — `DISABLED` if `not enabled`, else delegates.
  - **`probe(widget) -> BlurStatus`** — NEW; the verification call the theme uses to pick body alpha. Computed once, cached for the process.
  - keep `is_supported()` for back-compat / install doctor, but **demote its meaning** in the docstring: "can issue, not will-blur."

- **`modules/blur/_kwin.py`** — keep `_resolve()`/`apply()` exactly. ADD:
  - `_resolve_is_available()` — binds `_ZN14KWindowEffects17isEffectAvailableENS_6EffectE` (argtype `c_int`, restype `c_bool`), guarded like the existing bind.
  - `probe(widget) -> BlurStatus` — Layer A (`isEffectAvailable(7)`), demoted/confirmed by Layer B, X11-vs-Wayland branch (X11 → `REQUESTED_UNVERIFIABLE`), returns the enum + caches.
  - `_blur_effect_active() -> bool | None` — QtDBus `org.kde.KWin /Effects isEffectLoaded("blur")` + `/Compositor active`; `None` if inconclusive. WHY-log + demote.

- **`modules/blur/_dwm.py`** (NEW) — §5 recipe. `apply()` issues the backdrop, returns `BlurStatus` straight from the HRESULT; `probe()` returns the cached apply result. `is_supported()` = `sys.platform=="win32" and build>=22000`.

- **`modules/blur/_macos.py`** (NEW stub) — `UNSUPPORTED`; the §6 body lives behind `sys.platform=="darwin"` but is not wired until hardware exists.

- **`modules/blur/_unsupported.py`** — `apply`/`probe` → `UNSUPPORTED`.

### `modules/theme.py` — body alpha becomes status-driven

- Add `glass_alpha` (172) and `solid_alpha` (236) to the frosted theme (Solid dark stays 255 / `DISABLED`).
- Add `theme.body_color_for(status, surface) -> tuple[int,int,int,int]` returning hue + status-selected alpha (`ACTIVE`→glass, else solid; `DISABLED`/`JT_OPAQUE`→255). Keep `mini_body_color`/`dialog_body_color` parallel.

### `jellytoast.py` — consume status before first paint

- Make `_body_qcolor` a function of `blur.probe()`:
  - In the deferred post-`show()` hook that already calls `_blur.apply` (`:404`, `:1201`), capture `status = blur.apply(...)` then `status = blur.probe(self)`; set `self._body_qcolor = QColor(*theme.body_color_for(status, "main"))`; `self.update()`.
  - **Decide before first visible paint:** run an *initial* probe in `__init__`/early `showEvent` so the first `paintEvent` already uses 236 if unverified — never a 172 flash. (If the QWindow isn't ready, default conservative 236 and upgrade on the deferred re-probe.)
  - `paintEvent` unchanged — still fills `self._body_qcolor`.
  - On `theme_changed` and (KDE) `compositingToggled` / periodic re-probe, recompute. Keep `JT_OPAQUE` forcing 255 as the top override.
- **Boot log + install doctor:** one INFO line with the status + `reason`. `dev/install_doctor.py` adds the lib + plugin-dir + `nm -D` symbol checks.

### Packaging touch-points

- `packaging/aur/PKGBUILD` `optdepends` += `kwindowsystem`.
- No new runtime deps for Windows. `wayland-utils` deliberately **not** added.

---

## 8. Capability / degradation matrix

> **⚠️ §8 Windows rows SUPERSEDED (2026-06-08):** see the §5 banner. The
> live Windows DEFAULT is real **Acrylic** blur-behind
> (`modules/blur/_dwm.py` `apply()` → `apply_acrylic()`,
> `ACCENT_ENABLE_ACRYLICBLURBEHIND`), NOT the Mica these rows describe;
> Mica is only the `JT_NO_WIN_BLUR` fallback, and the "Acrylic too laggy —
> declined" justification in the Windows-10 row no longer reflects the
> project's posture (Win11 ships Acrylic by default; only Win10 < 22000
> still stays opaque). The Mica detail in the rows is kept as the
> documented fallback.

| Row | Real blur? How | Detection (the gate) | Frosted-dark fallback |
|---|---|---|---|
| **KDE Wayland** | **Yes** — `ext-background-effect-v1` / legacy via KWindowSystem | `isEffectAvailable(7)` ctypes + DBus `isEffectLoaded("blur")` confirm | `ACTIVE` → **172**; if probe False → **236** + note |
| **KDE X11** | Partial — atom honored but can silently skip | `isEffectAvailable(7)` (needs `compositingActive()`, X11-guarded) | **`REQUESTED_UNVERIFIABLE` → 236** |
| **GNOME/Mutter** | **No** | `XDG_CURRENT_DESKTOP=gnome` → don't probe | `UNSUPPORTED` → **236** |
| **Cinnamon/Muffin** | **No** standard path | env heuristic | `UNSUPPORTED` → **236** |
| **XFCE / MATE** | **No** app API | env heuristic | `UNSUPPORTED` → **236** |
| **wlroots (Hyprland/SwayFX/Wayfire)** | **No app protocol** — user-config only (Hyprland blur ON by default) | can't probe | **236** (safe even if user blur active); docs: app_id `jellytoast` is the lever, `windowrule = noblur, class:^(jellytoast)$` the opt-out |
| **niri 26.04** | **Yes** — `ext-background-effect-v1`, zero config | `isEffectAvailable(7)` iff KWindowSystem installed | `ACTIVE` → **172** if probe True; else **236** |
| **COSMIC** | Coming (no date) | n/a today | **236** today; revisit |
| **Windows 11 22H2+ (≥22621)** | **Yes** — Mica via `DWMWA_SYSTEMBACKDROP_TYPE=38`, `DWMSBT_MAINWINDOW=2` | `DwmSetWindowAttribute` HRESULT `== S_OK` | `ACTIVE` → translucent dark body (~120–150) over Mica; `HRESULT!=0` → **255** |
| **Windows 11 21H2 (22000–22620)** | **Yes** — legacy Mica attr `1029` value `1` | HRESULT `== S_OK` | `ACTIVE` → tinted Mica; else **255** |
| **Windows 10 / older** | **No** (Acrylic too laggy — declined) | build `< 22000` | `UNSUPPORTED` → **255** opaque |
| **macOS** | Yes (NSVisualEffectView) — **deferred** | effect view added | stub → `UNSUPPORTED` → **236** until hardware |

---

## 9. Phased implementation plan

### P0 — KDE→KDE transparent-bug fix (verifiable now, on august's two KDE boxes)
**Scope:** `BlurStatus` enum + `blur.probe()` (`isEffectAvailable` ctypes + QtDBus cross-check, X11/Wayland branch); status-driven `_body_qcolor` decided **before first paint**; theme two-alpha (172/236); boot log + install-doctor checks; `kwindowsystem` → AUR optdepends.
**Risk:** low–medium. Main risk is the mangled `isEffectAvailable` symbol drifting → mitigated by guarded bind + `nm -D` doctor check + conservative-on-failure default. (Symbol **confirmed present on hardware**, so risk is now low.)
**Test method:** on the working KDE laptop expect `ACTIVE`/172 (Frosted identical to today). **Reproduce the second-laptop failure**: disable KWin's Blur desktop effect and/or remove the kwindowsystem Qt plugin in a VM → confirm 236 body (legible, no see-through) + the one-time note. Use the live-app test bridge (`JT_TEST_BRIDGE=1`, `TMPDIR=/tmp`) to read `blur.probe()` and `_body_qcolor.alpha()`; judge visually with `spectacle -f -b -n`.
**Verifiable now:** **yes**, fully, including the failure path via effect-toggle.

### P1 — Windows 11 Mica (verifiable on the Windows laptop)
**Scope:** `modules/blur/_dwm.py` (§5), Windows dispatch arm, deferred `showEvent` apply, HRESULT→status, Windows body-alpha tuning.
**Risk:** medium. Mica-show-through-vs-legibility is taste; the dark-tint floor de-risks. Qt-6.8 overwrite handled by deferral.
**Test method:** Win11 ≥22621 — confirm `S_OK` + dark-tinted Mica body at several wallpapers; dark titlebar; force build<22621 path; confirm Win10 → opaque 255.
**Verifiable now:** **yes** (Windows laptop available).

### P2 — cross-DE fallback polish (verifiable now)
**Scope:** env-heuristic gating (don't probe on GNOME/XFCE/Cinnamon), the one-time Settings note + WHY-log copy, README docs for wlroots app_id rules, `JT_OPAQUE` → Settings → Display toggle promotion.
**Risk:** low. The frosted-fallback alpha (236) needs august's eyeball on a GNOME/XFCE box to confirm "frosted panel" not "dark box."
**Test method:** GNOME/Cinnamon/XFCE live session/VM; confirm 236 body + correct note; no see-through on Mutter even at maximize.
**Verifiable now:** **yes** (VMs/live USBs).

### P3 — macOS vibrancy (hardware-gated, deferred)
**Scope:** flesh out `_macos.py` per §6. **Verifiable now:** **no** — keep the `UNSUPPORTED` stub.

---

## 10. Open questions & risks (with recommendations)

**Decisive recommendations:**

1. **`REQUESTED_UNVERIFIABLE` defaults to opaque (236), not glass.** A broken see-through window is the bug we're fixing; a slightly-too-opaque frosted panel is invisible to most users. Settings opt-in later for power users.
2. **KDE X11 → conservative 236 by default.** The documented atom-set-but-render-skipped failure means we cannot earn `ACTIVE` on X11 from the client. Only KDE *Wayland* with positive `isEffectAvailable` + DBus confirm gets 172.
3. **Don't add `wayland-utils`/`wayland-info`.** The `isEffectAvailable` probe is sufficient.
4. **Vendor the Windows ctypes routine; no pywinstyles.** pywinstyles never sets the documented attr 38.

**Flagged-uncertain — TEST, don't trust:**

- ✅ **RESOLVED on hardware:** the `isEffectAvailable` / `compositingActive` mangled symbols both present in `libKF6WindowSystem.so.6`. (Re-confirm on the *second* laptop before trusting `ACTIVE` there.)
- **(P0 root-cause) What exactly failed on the second laptop** — missing kwindowsystem Qt plugin, disabled KWin Blur effect, or compositing off? The probe handles all three; log the `reason` so we learn which.
- **(verify on a niri box) Does KWindowSystem route to `ext-background-effect-v1` on niri with KF6 installed but no KDE shell?** Likely yes, untested. If false, niri stays at 236 — safe.
- **(verify in Flatpak) Sandbox Qt plugin path + blur-global exposure.** If unavailable, Flatpak falls back to 236. Test before claiming Flatpak Frosted dark.
- **(eyeball, P1) Windows body alpha over Mica** — pick (~120–150) on real 22621+ hardware at several wallpapers.
- **(eyeball, P2) Frosted-fallback alpha 236** — confirm it reads as "frosted panel," not "dark box," on GNOME/XFCE; tune within 232–242.
- **(monitor) Legacy `org_kde_kwin_blur` removal** — we go *through* KWindowSystem so we're insulated; watch Plasma versions.

---

**Files to change:** `modules/blur/__init__.py` (status enum + dispatch + `probe`), `modules/blur/_kwin.py` (`isEffectAvailable` bind + `probe` + DBus cross-check), `modules/blur/_dwm.py` (NEW), `modules/blur/_macos.py` (NEW stub), `modules/blur/_unsupported.py` (status), `modules/theme.py` (two-alpha + `body_color_for`), `jellytoast.py` (status-driven `_body_qcolor` before first paint, re-probe hooks), `dev/install_doctor.py` (lib + plugin-dir + `nm -D` checks), `packaging/aur/PKGBUILD` (`kwindowsystem` → optdepends). **Start with P0 — fully verifiable now on august's two KDE laptops and directly closes the "fully transparent" bug.**
