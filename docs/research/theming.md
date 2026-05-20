# Theming — Research & Design

> **📍 Status — 2026-05-20:** Research complete; implementation not
> started. This doc captures cross-platform light/dark theming
> standards and a proposed architecture for jellytoast. The app today
> ships three dark-only themes (`modules/theme.py`) with a working
> live *accent* swap but restart-required theme *mode*.

## 1. Goal

Add a proper **light theme** as a sibling of the existing dark themes,
make theme switching **live** (no restart — the standard for quality
apps), and **follow the OS light/dark preference** by default, on KDE
Plasma, other Linux desktops, Windows, and (eventually) macOS.

## 2. The OS standard — there is one, and Qt exposes it

### freedesktop / XDG (all Linux desktops)

`org.freedesktop.appearance` `color-scheme`, exposed via the XDG
Desktop Portal (`org.freedesktop.portal.Settings`), is **the**
cross-desktop standard. Every modern toolkit reads it.

- Values: `0` = no preference, `1` = prefer dark, `2` = prefer light.
- Read via `ReadOne(namespace, key)`; subscribe via the
  `SettingChanged` D-Bus signal.
- **KDE Plasma 6** implements it (`xdg-desktop-portal-kde`) and
  resolves light/dark from the active color scheme's luminance, so
  custom `.colors` schemes work too.
- **GNOME** maps `org.gnome.desktop.interface color-scheme`
  (`default` / `prefer-dark` / `prefer-light`) onto the same portal key.
- Caveat: if `xdg-desktop-portal` isn't running, apps fall back to
  light.

### Windows

Two independent settings under Personalization → Colors: **Windows
mode** (chrome) and **App mode** (apps — the one to follow).

- Registry: `HKCU\…\Themes\Personalize\AppsUseLightTheme` (DWORD,
  `1` = light, `0` = dark).
- Dark title bars need an explicit opt-in:
  `DwmSetWindowAttribute(hwnd, DWMWA_USE_IMMERSIVE_DARK_MODE=20, …)`.
- Accent lives under `…\DWM`; the supported read is
  `UISettings.GetColorValue(UIColorType.Accent)`.

### macOS

`NSAppearance` — Light / Dark / Auto (Auto = Night-Shift schedule).
Per-**window** appearance is supported. `Info.plist` must **not** set
`NSRequiresAquaSystemAppearance=true` or the app is pinned light.
System accent + a separate highlight colour.

### Qt 6 ties it together — one API, all platforms

- **`QGuiApplication::styleHints()->colorScheme()`** → `Qt::ColorScheme`
  (`Light` / `Dark` / `Unknown`). Added **Qt 6.5**.
- **`QStyleHints::colorSchemeChanged`** signal — fires on a runtime OS
  flip. Added **Qt 6.5**.
- On Linux Qt reads the XDG portal underneath; on Windows it reads
  `AppsUseLightTheme` and auto-applies the dark title bar; on macOS it
  observes `NSApp.effectiveAppearance`. **One detection path covers
  all three platforms** — matches jellytoast's provider-parity ethos.
- `QStyleHints::setColorScheme()` (Qt 6.8) can *request* a scheme but
  is unreliable on Linux — treat the OS preference as an input only
  and drive our own QSS/tokens directly.
- Qt only auto-swaps the *default* `QPalette`. jellytoast paints a
  fully custom QSS theme, so **the OS preference is just an input** —
  we switch our own tokens. Re-theme on `QEvent::ApplicationPaletteChange`
  (the palette isn't updated yet when `colorSchemeChanged` fires).

**ACTION:** confirm the installed Qt is ≥ 6.5 (almost certainly is).

## 3. The convention: Light / Dark / Auto, default Auto

Every platform offers this tri-state (macOS Appearance; Windows
scheduled Auto; GNOME). Well-behaved apps **follow the OS by default**
but keep an explicit override — not following when asked is the #1
complaint about non-native Linux apps; auto-only with no override
surprises users who pin a music app dark on a light desktop.

So: a `theme_preference` of **Light / Dark / Follow system**, default
**Follow system**. "Follow system" resolves via `colorScheme()`.

## 4. Modern theme architecture — semantic design tokens

The consensus structure (Material 3, GitHub Primer, Atlassian, Radix):

1. **Primitive tokens** — raw palette values, no meaning
   (`gray.900 = #1a1a1a`). Never referenced by widgets.
2. **Semantic tokens** — named by *intent*: `bg.canvas`,
   `text.primary`, `border.subtle`, `accent.default`. **This layer is
   what swaps between light and dark** — one set of names, a different
   primitive per theme.
3. **Component tokens** — per-widget decisions, reference semantic
   tokens.

The W3C **Design Tokens Format Module** hit its first stable release
(2025.10) — a vendor-neutral JSON format; tooling (Style Dictionary)
can compile it to a Python module / QSS.

### Radix Colors' 12-step scale — a ready model for the semantic roles

| Steps | Role |
|-------|------|
| 1–2 | App background; subtle/card background |
| 3–5 | Component background — normal / hover / pressed |
| 6–8 | Borders — subtle / interactive / strong + focus |
| 9–10 | Solid (accent) fills — base / hover |
| 11–12 | Text — muted / high-contrast |

Light and dark scales share step numbers, so semantic aliases resolve
identically in both. jellytoast's roles map cleanly onto this.

## 5. Light/dark design rules (strong cross-source consensus)

- **No pure black or pure white for large surfaces.** Dark base ≈
  `#121212`–`#1a1a1a`; light base ≈ `#ffffff`–`#fafafa`. Body text
  off-white (`#e0e0e0`-ish), not `#fff`.
- **Elevation via surface lightness, not shadow, in dark mode** — a
  ladder like `#121212 → #1e1e1e → #242424`. In *light* mode shadows
  work; surfaces stay near-white.
- **Two-tier surfaces** — a base background and a lighter "elevated"
  background for cards, popovers, the now-playing bar, menus.
- **Desaturate + lighten the accent on dark**; **darken it on light**
  so it keeps contrast against the surface. The accent is a small
  scale (default / hover / pressed + `text.onAccent`), not one colour
  — and the light and dark accent values differ.
- **Borders = text colour at low alpha** (`~8–12%`) — auto-adapts to
  both themes, no per-theme hand-tuning.
- **Contrast:** WCAG 2 AA — 4.5:1 body text, 3:1 large text / UI
  components — is the compliance floor. **APCA** (WCAG 3 candidate,
  perceptually uniform) is the better tool for *designing* dark mode;
  WCAG 2's math overstates contrast for near-black pairs.

## 6. Tooling

- **Style Dictionary** — compiles DTCG-format token JSON to any target
  incl. a custom Python/QSS emitter. The standard way to drive a
  non-web app from tokens.
- **Radix Colors** — ready-made accessible 12-step light+dark scales;
  a reference even though the components are web-only.
- **Material Theme Builder** / **Leonardo** (Adobe) — generate a full
  light+dark scheme from a source colour (Leonardo by contrast target).
- **APCA checker** + **WebAIM** — verify every text/surface and
  accent/surface pair, both themes.

## 7. jellytoast today

- `modules/theme.py` — `Theme` frozen dataclass, 13 colour fields + 3
  paint-fill tuples. Three **dark** themes (FROSTED_DARK / DARK /
  TRANSPARENT). `get_active_theme()` reads `theme_mode`, applies the
  accent override.
- `modules/ui_helpers.py` — module-global colour constants; ~10 derive
  from the active `Theme`, ~12 are **hardcoded white-on-dark literals**
  (`WASH_HOVER`, `SURFACE_INPUT`, `HOVER_SUBTLE`, …). Those can't flip.
- `modules/color_tokens.py` — a ~30-token registry + the
  Settings → Colors override editor (`debug/colors/*` in QSettings).
- ~176 hardcoded white literals scattered across ~17 widget files.
- Live **accent** swap works (`PlayerBus.theme_changed` +
  per-surface `_reapply_accent()`); theme **mode** requires a restart.

## 8. Proposed architecture

1. **Semantic token layer.** A single canonical set of semantic
   tokens (Radix-role-based). Expand `Theme` to carry the *full* set;
   widgets reference tokens only. The three dark themes keep their
   exact current values — **no visual change to dark**.
2. **Tokenize the ~176 white literals** — route them through semantic
   tokens.
3. **Live switching.** Broaden `theme_changed` → every painted surface
   re-stamps its full QSS via a `_reapply_theme()` contract (today
   only the accent re-stamps). Re-theme on `ApplicationPaletteChange`.
4. **Light theme.** Author a light `Theme` per §5 — needs real visual
   QA, not a mechanical invert.
5. **OS-follow.** A `theme_preference` setting (Light / Dark / Follow
   system, default Follow system) reading `QStyleHints.colorScheme()`
   + `colorSchemeChanged`.
6. **Cross-platform.** Qt 6.5+ gives the Windows dark title bar for
   free; verify on the Windows laptop. macOS path stays
   documented-behaviour-only until a Mac exists.

## 9. Sources

- [XDG portal Settings](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.Settings.html)
- [Qt: Dark Mode on Windows 11 with Qt 6.5](https://www.qt.io/blog/dark-mode-on-windows-11-with-qt-6.5) ·
  [QStyleHints](https://doc.qt.io/qt-6/qstylehints.html)
- [Apple HIG — Dark Mode](https://developer.apple.com/design/human-interface-guidelines/dark-mode)
- [Windows — light/dark theming](https://learn.microsoft.com/en-us/windows/apps/desktop/modernize/ui/apply-windows-themes)
- [Material 3 — colour roles](https://m3.material.io/styles/color/roles) ·
  [Radix Colors — the scale](https://www.radix-ui.com/colors/docs/palette-composition/understanding-the-scale)
- [W3C Design Tokens Format Module](https://www.designtokens.org/tr/drafts/format/)
- [APCA in a nutshell](https://git.apcacontrast.com/documentation/APCA_in_a_Nutshell.html)
