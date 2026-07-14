# Steam Deck blur diagnosis — mission brief (for a Claude running ON the Deck)

You are a Claude Code session running on **august's Steam Deck, in desktop
mode**, in a `jellytoast` repo checkout on `main`. Your job is to diagnose
**issue #229** — the flatpak paints nearly-transparent glass over an
UNBLURRED desktop — gather evidence, apply the known-good mitigation, and
report everything back on the issue. The primary-machine session (Linux
desktop) prepared this brief; local memory doesn't sync across machines.

**jellytoast** is a PySide6/Qt6 music client. Frosted themes ride real
compositor blur when the platform verifies it (`jellytoast/blur/`), else a
near-opaque faux-frost fallback. The bug: on the Deck the window is CLEAR —
the app believed blur was ACTIVE and painted full-transparency glass, but
nothing blurred behind it.

## Background you need

- KWin advertises its Wayland blur protocol even when the Blur desktop
  effect is off — the capability bit alone can't be trusted.
- `blur/_kwin.py` cross-checks over D-Bus (`org.kde.KWin /Effects
  isEffectLoaded blur`). Fix 3f9e11f granted the sandbox
  `--talk-name=org.kde.KWin` (re-rolled into the 0.2.0 flatpak asset) and
  demotes to the frosted fallback when the check is INCONCLUSIVE in a
  flatpak on KDE.
- The reporter STILL sees clear glass after reinstalling. Two candidate
  explanations, and your evidence decides:
  (a) the reinstall raced the asset swap → old bundle without the grant;
  (b) the Deck's KWin CLAIMS `isEffectLoaded blur == true` but skips the
      render → a second lie the current demotion doesn't cover.

## Evidence to collect (Konsole; capture exact output for the report)

```bash
# 0. What's installed
flatpak info io.github.wolfgangwarehaus.jellytoast | head -8
flatpak info --show-permissions io.github.wolfgangwarehaus.jellytoast | grep -i kwin
# no org.kde.KWin=talk → OLD bundle: reinstall from
# https://github.com/wolfgangwarehaus/jellytoast/releases/latest/download/jellytoast.flatpak
# then re-check, and note the race in the report.

# 1. Session + compositor claims
echo "$XDG_SESSION_TYPE"
qdbus org.kde.KWin /Effects isEffectLoaded blur 2>/dev/null \
  || qdbus6 org.kde.KWin /Effects isEffectLoaded blur
kreadconfig6 --file kwinrc --group Plugins --key blurEnabled 2>/dev/null \
  || kreadconfig5 --file kwinrc --group Plugins --key blurEnabled
cat /etc/os-release | head -4   # SteamOS version — matters for the heuristic

# 2. The app's own verdict, from inside the sandbox
flatpak run io.github.wolfgangwarehaus.jellytoast 2>&1 | grep -i blur | head -8
# (let it boot fully, then close it; the blur status/reason lines print early)
```

## Visual evidence

Use `spectacle -b -n -o <path>` (or `grim` if present) for full-screen
captures you then READ YOURSELF to judge:

1. App as-is (the bug state, or not — maybe the reinstall fixed it).
2. After the known-good forcing, relaunch and capture again:
   ```bash
   flatpak override --user --env=JT_BLUR_FORCE=unverifiable io.github.wolfgangwarehaus.jellytoast
   ```
   Expected: near-opaque frosted body, text fully legible. This is the
   mitigation august keeps until a proper fix ships — LEAVE IT IN PLACE
   unless the as-is capture already shows correct frost.

## Interpreting

| Evidence | Conclusion |
|---|---|
| No `org.kde.KWin=talk` in permissions | Race — old bundle. Reinstall, re-run everything, report both states. |
| Grant present + `isEffectLoaded blur` = false + app log says ACTIVE | The D-Bus check isn't working in-sandbox despite the grant — capture the exact app log lines; that's a bug in `_blur_effect_active()` under the Deck's bus. |
| Grant present + `isEffectLoaded blur` = true + still visually unblurred | The second lie: KWin claims the effect but skips the render (Deck GPU / SteamOS policy). The fix is a SteamOS demotion heuristic (`/etc/os-release` `ID=steamos`, readable in-sandbox) — note it in the report; do NOT implement app changes from the Deck. |
| Frost renders correctly after reinstall | The race was the whole story — say so and close the loop. |

## Report

Comment EVERYTHING on **issue #229** (`gh issue comment 229 --body-file …`):
the exact command outputs, your reading of the screenshots, which row of the
table applies, and the override end-state (in place / not needed). Do NOT
push code changes; do NOT close the issue. If `gh` isn't authed on the Deck,
run `gh auth login` (august completes the device-code flow).

## Deck environment notes

- SteamOS's rootfs is read-only — install nothing with pacman. Claude Code
  and `gh` live fine in `~/.local/bin`; git ships with SteamOS desktop mode.
- Discover may show a spurious "install failed" for sideloaded bundles —
  known quirk, ignore it if `flatpak info` shows the app installed.
- Keep system volume LOW if you drive playback (not required for this brief).

---

## Follow-up: opaque-surface vs blur-artifact (#deck-opaque-blur, 0.2.1)

Row 15 came back "ACTIVE but composites opaque". Two things to nail before a
fix, both need a build with the diagnostics (any 0.2.1-dev flatpak):

### 1. Disambiguate — was the checkerboard a red herring?
The original test used a **magenta/green** wallpaper. Those are complementary,
so a *working* blur averages them to neutral grey — indistinguishable from a
truly opaque surface. Re-test on a **solid, saturated single-colour wallpaper**
(pure red `#FF0000` fills the screen):
- Sample the jellytoast body over the middle of the screen.
- **Reddish-grey** (R clearly > G,B) → blur IS working; the earlier "opaque"
  read was a wallpaper artifact. Close the bug.
- **Neutral grey** (R≈G≈B) → genuinely opaque; continue below.

### 2. Capture the surface truth
Launch with `JT_BLUR_DIAG=1` and grab the `BLUR-DIAG:` line from
`~/.var/app/io.github.wolfgangwarehaus.jellytoast/cache/jellytoast/jellytoast.log`
(flatpak) — it reports WA_TranslucentBackground, alphaBufferSize, chrome mode,
body rgba, faux_frost. `alphaBufferSize=0` or `WA_TranslucentBackground=false`
would point at the surface format; both-correct-but-opaque points at the
compositor/decoration integration.

### 3. A/B the SSD hypothesis
KDE Wayland uses KWin server-side decorations + a noborder rule; the leading
theory is older SteamOS KWin composites that opaque. Test the client-side-
decoration path:
```
flatpak override --user --env=JT_KDE_FORCE_CSD=1 io.github.wolfgangwarehaus.jellytoast
```
Relaunch, repeat the solid-red test. If the body now shows the red backdrop,
**SSD was the culprit** → the fix is to default KDE Wayland (or just SteamOS)
to CSD. Undo: `flatpak override --user --unset-env=JT_KDE_FORCE_CSD …`.

Report all three on issue #229 / the 0.2.1 tracker.
