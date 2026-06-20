# `.deb` xcb dependency fix — Ubuntu session worklist

**Open this on the Ubuntu box (Docker + a real X11 session available).** This PR
sorts out the Linux `.deb`'s Qt `xcb` platform-plugin dependencies *properly* —
the whole `DT_NEEDED` class at once, not one lib at a time.

> ✅ **The complete closure is already declared.** `build_deb.sh`'s `Depends` was
> extended to the full `readelf -d` closure of the `pyside6-essentials 6.11`
> wheel's `libqxcb.so` + `libQt6XcbQpa.so.6` (done off-box, no container needed).
> **This session is now VERIFICATION, not discovery** — build + smoke across the
> three distro containers + boot on a real X11 session, confirm green. Re-derive
> (steps 1–4 below) only if it still fails or PySide6 was bumped.

## The bug

The v0.1.2 release build **failed** on the `.deb` smoke test. The Qt `xcb`
platform plugin aborts on boot under Xvfb (`rc=134`) even though the currently
declared deps install fine:

```
Depends: libmpv2 | libmpv1, libxcb-cursor0, libxcb-icccm4, libxcb-keysyms1, libgl1
```

So **at least one more `libxcb-*` / X / xkbcommon `DT_NEEDED` is missing** from
`Depends` (and isn't bundled). This set was enumerated by hand and is incomplete
— it's been patched piecemeal (#149 added `cursor0`; #162 added
`icccm4`/`keysyms1`/`libgl1`) and there's *still* a gap.

> ⚠️ **Implication:** v0.1.0 / v0.1.1's `.deb` is very likely **already broken on
> X11 / XWayland** — their smoke test only checked the dep was *present*, never
> actually booted `xcb`. The #162 boot-under-Xvfb probe is doing its job. Worth a
> CHANGELOG line + a heads-up if anyone reported "won't launch on X11".

## Goal — fix the whole class

Declare (or bundle) the **complete** `DT_NEEDED` closure of the bundled Qt xcb
plugin so a clean install launches on X11/XWayland on every target distro. Don't
add just the one the log names — enumerate them all.

## Steps

1. **Build the `.deb`** (same as CI, on the 22.04-class builder):
   ```bash
   bash packaging/deb/build_deb.sh 0.1.2     # → dist/*.deb + dist/jellytoast/ bundle
   ```

2. **Let the smoke test name the first culprit.** The boot probe now runs with
   `QT_DEBUG_PLUGINS=1` and prints the missing `.so` on failure:
   ```bash
   docker run --rm -v "$PWD:/src:ro" ubuntu:24.04 bash /src/packaging/deb/smoke_test_deb.sh
   # look for: "Cannot load library … (libXXX.so.N: cannot open shared object file)"
   ```

3. **Enumerate the WHOLE closure** (don't stop at the one name). On the bundle:
   ```bash
   # locate the xcb plugin + its Qt support lib in the PyInstaller bundle
   find dist/jellytoast -name 'libqxcb.so' -o -name 'libQt6XcbQpa.so.6'
   # full direct DT_NEEDED of each:
   readelf -d <those .so files> | grep NEEDED
   # transitive: what's NOT already inside the bundle and NOT pulled by libc/libmpv:
   ldd <libqxcb.so> 2>&1 | grep 'not found'   # run inside a clean ubuntu:24.04 container
   ```
   Typical Qt6 xcb closure beyond what we declare: `libxcb-render-util0`
   (`libxcb-util`), `libxcb-image0`, `libxcb-shape0`, `libxcb-randr0`,
   `libxcb-sync1`, `libxcb-xfixes0`, `libxcb-xinerama0`, `libxcb-glx0`,
   `libxkbcommon-x11-0`, `libxcb-xkb1`, `libsm6`, `libice6`. **Verify each
   against `readelf`/`ldd` — don't blind-add.**

4. **Map each missing `.so` → its Debian package** (on a box that has it):
   ```bash
   dpkg -S /usr/lib/x86_64-linux-gnu/libXXX.so.N      # or: apt-file search libXXX.so.N
   ```

5. **Decide depend-vs-bundle.** We currently *depend* (matches the libmpv
   policy — keep the `.deb` thin). Stay consistent: add the full set to the
   `Depends:` line in `packaging/deb/build_deb.sh` (and update the explanatory
   comment block above it). *(If we'd rather make the `.deb` self-contained like
   the planned AppImage, that's a bigger call — note it, don't do it here.)*

6. **Rebuild + smoke-test across all three targets** (must all pass):
   ```bash
   bash packaging/deb/build_deb.sh 0.1.2
   for img in ubuntu:24.04 ubuntu:26.04 debian:stable; do
     docker run --rm -v "$PWD:/src:ro" "$img" bash /src/packaging/deb/smoke_test_deb.sh
   done
   ```

7. **Boot on a REAL X11 session** (not just Xvfb) — install the `.deb` on the
   Ubuntu box under X11/XWayland and confirm the window actually opens, plays
   audio, and the tray works. Xvfb proves the plugin *loads*; a real session
   proves it's usable.

## While here (recommended)

- **Catch this earlier next time.** The full `.deb` build+smoke only runs on a
  release tag, which is why it reached "release". Consider a `workflow_dispatch`
  (or a `deb`-label-gated) job that runs `build_deb.sh` + the container smoke
  test on a PR, so packaging regressions fail *before* a tag.
- **CHANGELOG:** add a "Fixed — `.deb` launches on X11/XWayland (complete Qt xcb
  dependency set)" line to `[Unreleased]`.

## Then

Merge this → `main`, **then** cut v0.1.2 (it'll include this fix + the review
fixes). See the note in the PR description about the premature v0.1.2 tag.
