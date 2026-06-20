# `.deb` Qt runtime dependency closure — derivation & verification

How the Linux `.deb`'s `Depends` was derived and proven, and how to re-derive it
if PySide6 is bumped. The goal: declare the **complete `DT_NEEDED` closure** of
the bundled Qt `xcb` platform plugin (the whole class at once, not one lib at a
time) so a clean install launches on X11/XWayland on every target distro.

> **Status: RESOLVED + verified.** `build_deb.sh`'s `Depends` declares the full
> `readelf -d` closure of `libqxcb.so` + `libQt6XcbQpa.so.6` + `libQt6Gui.so.6`.
> Verified: (a) `ldd` closure complete on Ubuntu 26.04; (b) the bundled app boots
> under `QT_QPA_PLATFORM=xcb` with no plugin-load abort; (c) every declared
> package name resolves on `ubuntu:24.04` (noble), `ubuntu:26.04` (resolute) and
> `debian:stable` (trixie) — none took a `t64` rename; (d) the container smoke
> (`smoke_test_deb.sh`) boots `xcb` under Xvfb. Re-derive (below) only if a build
> still fails the smoke or PySide6 is bumped.

## Background — the bug this fixed

The Qt `xcb` platform plugin (`libqxcb.so` → `libQt6XcbQpa.so.6` → `libQt6Gui.so.6`)
hard-links a ~22-package closure of X / xcb / xkb / fontconfig / GL system libs.
Most are present on a normal desktop (pulled in by mesa/X/fontconfig), so the gap
was invisible there — but on a **minimal install / container** they're absent and
the plugin aborts on boot (`rc=134`, *"could not load the Qt platform plugin
xcb"*). The set was originally enumerated by hand and grew piecemeal (#149 added
`cursor0`; #162 added `icccm4`/`keysyms1`/`libgl1` — still only 4, still
incomplete; this fix declares the whole readelf closure).

> **Implication:** v0.1.0 / v0.1.1's `.deb` is **broken on X11 / XWayland** — their
> smoke test only checked the deps were *present*, never booted `xcb`. The #162
> boot-under-Xvfb probe is what caught it. Worth a CHANGELOG line.

## What is declared (and what is deliberately left transitive)

`Depends:` (see `build_deb.sh`) carries the **explicit** closure:

- **X / xcb / xkb (18):** `libx11-6`, `libx11-xcb1`, `libxcb1`, `libxcb-cursor0`,
  `libxcb-icccm4`, `libxcb-image0`, `libxcb-keysyms1`, `libxcb-randr0`,
  `libxcb-render0`, `libxcb-render-util0`, `libxcb-shape0`, `libxcb-shm0`,
  `libxcb-sync1`, `libxcb-util1`, `libxcb-xfixes0`, `libxcb-xkb1`,
  `libxkbcommon0`, `libxkbcommon-x11-0`
- **Qt font + GL stack (4):** `libfontconfig1`, `libfreetype6`, `libegl1`,
  `libgl1` — these are in Qt's own `DT_NEEDED`; `libfontconfig1`/`libfreetype6`/
  `libegl1` are *also* reachable transitively via `libmpv2→libass9`, but are
  declared explicitly so Qt's closure doesn't hinge on the media player's deps.

Deliberately **not** declared (guaranteed transitively by a hard `Depends`, so
adding them would be redundant): `libxau6`/`libxdmcp6` (via `libxcb1`),
`libglvnd0`/`libglx0` (via `libgl1`), and the `glib`/`dbus`/`png`/`expat`/
`pcre2`/`systemd`/`brotli` libs (via `libmpv2`). Base/Essential libs
(`libc6`, `libstdc++6`, `libgcc-s1`, `zlib1g`, …) are always present.

`libmpv2 | libmpv1` stays a real `Depends` — `python-mpv` dlopens the **system**
`libmpv.so.2` (audio = the distro's own libmpv→ffmpeg, matching the AUR package).
Note `libmpv1` no longer exists on noble/resolute/trixie, so the alternative
always resolves to `libmpv2` there.

## Re-derive the closure (if PySide6 is bumped or the smoke fails)

```bash
# 1. Build the bundle + .deb (same as CI, on the 22.04-class builder)
pyinstaller packaging/pyinstaller/jellytoast.spec --noconfirm
bash packaging/deb/build_deb.sh 0.1.2          # → dist/*.deb + dist/jellytoast/

# 2. Direct DT_NEEDED of the xcb plugin + its Qt support libs
find dist/jellytoast -name 'libqxcb.so' -o -name 'libQt6XcbQpa.so.6' -o -name 'libQt6Gui.so.6'
readelf -d <those .so files> | grep NEEDED

# 3. Full transitive closure, split system-vs-bundled (RPATH=$ORIGIN is honored):
ldd dist/jellytoast/_internal/PySide6/Qt/plugins/platforms/libqxcb.so \
  | grep -v '/opt/jellytoast\|dist/jellytoast'      # the rows resolving to /usr|/lib = system closure

# 4. Map each system .so → its Debian package
dpkg -S /usr/lib/x86_64-linux-gnu/libXXX.so.N        # or: apt-file search libXXX.so.N
```

Add any newly-required system lib to the `Depends:` line in `build_deb.sh` **and**
the dep-guard list in `smoke_test_deb.sh` (keep them in sync — drift is a latent
bug), and update the comment block above the `Depends:` line. Don't blind-add the
single name the log happens to print — enumerate the whole `readelf` closure.

## Verify

```bash
# Container smoke across all three targets (must all pass)
for img in ubuntu:24.04 ubuntu:26.04 debian:stable; do
  docker run --rm -v "$PWD:/src:ro" "$img" bash /src/packaging/deb/smoke_test_deb.sh
done
```

This runs in `release.yml` on a tag / `workflow_dispatch`. Xvfb proves the plugin
*loads*; for full confidence also install the `.deb` on a **real X11/Xorg login
session** and confirm the window opens, audio plays, and the tray works.

## Recommended follow-up

The full `.deb` build+smoke only runs on a release tag, which is how the
incomplete set reached "release" twice. Add a `workflow_dispatch` (or a
`deb`-label-gated) PR job that runs `build_deb.sh` + the container smoke, so
packaging regressions fail **before** a tag.
