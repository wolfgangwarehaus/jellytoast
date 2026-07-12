# 0.2.0 universal-installer QA — mission brief (for a Claude running ON the Mac or Windows box)

You are a Claude Code session running on **august's real Mac or Windows
machine**, in the `jellytoast` repo checkout, on branch
**`feat/0.2.0-universal`** (`git fetch && git checkout feat/0.2.0-universal &&
git pull` first). The primary-machine session (Linux) prepared this brief;
local memory doesn't sync across machines — everything you need is here.

**jellytoast** is a native PySide6/Qt6 **music-only** client for Jellyfin +
Subsonic/Navidrome. Branding is always lowercase **jellytoast**.

## Your mission

Execute **PR #228's test matrix autonomously** against the REAL 0.2.0
artifacts — not a source checkout. Four targets, two per OS:

- **This-OS installer** (the user path — priority): universal `.dmg` on macOS,
  dual-arch `setup.exe` on Windows.
- **This-OS wheel** via pipx (`python-dist` artifact).

For each target: install it, drive the full feature sweep through the test
bridge, capture and **visually review** screenshots (you can read PNGs — judge
frost transparency, text legibility, layout, artwork), hunt bugs, then write
findings and tick the PR matrix. **Do NOT merge the PR** — august approves
merges; you test and report.

## What's new on this branch (the things most likely to break)

1. **macOS: pyobjc now ships in the dmg AND the wheel** (was silently absent
   forever — only the MAS build had it). So the dmg gets, for the first time:
   native NSVisualEffectView vibrancy, the non-opaque NSWindow (transparent
   frost), Control Center / media keys, Notification Center banners. All of
   rows 13–15 in the PR matrix are effectively NEW on this install channel.
2. **Windows: cryptography is GONE from the resolve.** The credential
   resilience store is DPAPI now (`d1:` blobs, `jellytoast/credentials.py`).
   Server sign-in must survive relaunches; ListenBrainz/Last.fm re-link ONCE
   after an upgrade from 0.1.9 (expected, release-noted) and must never show a
   garbled token state.
3. **Universal/dual-arch packaging**: macOS main binary is fat
   (`x86_64 arm64`), floor macOS 15; Windows installer carries x64 + native
   ARM64 trees and picks the machine's native one.
4. Smaller fixes to eyeball: multi-library title degrades ("A + B" → "A +1" →
   "2 libraries") before touching the Albums dropdown; the "other audio is
   playing" toast wears the frosted-pill tooltip style.

## Setup (once)

```bash
cd <repo checkout> && git fetch && git checkout feat/0.2.0-universal && git pull
# client-side venv for the bridge tools (PySide6 only needs to import):
source .venv/bin/activate   # or: python3 -m venv .venv && pip install -e .
```

Get the artifacts from the latest GREEN dispatch run on this branch:

```bash
gh run list --workflow=release.yml --branch feat/0.2.0-universal --limit 3
gh run download <RUN_ID> -n macos-universal -n python-dist -D /tmp/jt-qa   # mac
gh run download <RUN_ID> -n windows -n python-dist -D "$env:TEMP\jt-qa"    # windows
```

## Target 1 — the installer

### macOS
```bash
hdiutil attach /tmp/jt-qa/jellytoast-0.2.0-macos-universal.dmg
cp -R "/Volumes/jellytoast 0.2.0/jellytoast.app" /Applications/
hdiutil detach "/Volumes/jellytoast 0.2.0"
# Structure checks BEFORE first launch:
lipo -archs /Applications/jellytoast.app/Contents/MacOS/jellytoast   # → x86_64 arm64
stapler validate /Applications/jellytoast.app                        # → worked
spctl -a -vv /Applications/jellytoast.app                            # → accepted, Developer ID
```
First launch **from Finder** (Gatekeeper check — the one-time benign
"downloaded from the Internet… Apple checked it" confirm with a normal Open
button is EXPECTED and correct; what must NOT appear is the blocking
"unidentified developer" refusal. Screenshot the dialog). Then quit, and
relaunch with the bridge for the automated sweep — direct exec passes env
where `open` doesn't:
```bash
TMPDIR=/tmp JT_TEST_BRIDGE=1 /Applications/jellytoast.app/Contents/MacOS/jellytoast &
```

### Windows
```powershell
& "$env:TEMP\jt-qa\jellytoast-0.2.0-windows-setup.exe" /VERYSILENT /SUPPRESSMSGBOXES
# per-user install → no UAC prompt should appear at all. Then:
$exe = "$env:LOCALAPPDATA\Programs\jellytoast\jellytoast.exe"
# Native-arch check: on x64 expect x64; on ARM expect Arm64.
# (Get-Command $exe).FileVersionInfo / dumpbin /headers — or PE machine field:
python -c "import struct;d=open(r'$env:LOCALAPPDATA\Programs\jellytoast\jellytoast.exe','rb').read(4096);o=struct.unpack_from('<I',d,60)[0];m=struct.unpack_from('<H',d,o+4)[0];print(hex(m),{0x8664:'x64',0xaa64:'ARM64'}.get(m))"
$env:JT_TEST_BRIDGE = "1"; & $exe
```

## The sweep (same for installer and wheel)

Use the existing tooling — read `dev/QA_SESSION_COMMON.md` for the bridge
idioms, then:

1. **Automated gallery**: `python dev/qa_harness.py` sweeps every surface in
   dark + light, window states, mini player, dialogs, using REAL composited
   capture (`screencapture` / PowerShell — never `win.grab()`, it is
   blur-blind). Review every PNG yourself.
2. **Feature drive** via `dev/jt_drive.py` `Bridge` + UI where needed — walk PR
   #228 §B rows 1–17 in order. Sign in with the demo button if no server creds
   are on the box. Casting: discovery listing is enough if no device is
   reachable. Offline: download one album, toggle offline, play.
3. **The new-code assertions**:
   - macOS — vibrancy is genuinely ACTIVE, not faux frost:
     `python dev/jt_ctl.py eval "__import__('jellytoast.blur',fromlist=['x']).status().name"`
     and visually: window body shows the blurred DESKTOP through it.
   - macOS — banners: play/skip a track, check Notification Center attribution.
   - Windows — `pipx runpip jellytoast show cryptography` (wheel) / for the
     installer assert no `cryptography` folder under
     `$env:LOCALAPPDATA\Programs\jellytoast\_internal`.
   - Windows — sign in, quit, relaunch → still signed in; the QSettings
     `server/token` value starts with `d1:` (bridge:
     `eval "get_settings()._s.value('server/token','',str)[:3]"`).
4. **Bug hunt**: resize/maximize/fullscreen/focus-away stress on the frost;
   long library names vs the Albums dropdown; rapid queue ops; theme flips.

## Target 2 — the wheel

```bash
pipx install /tmp/jt-qa/jellytoast-0.2.0-py3-none-any.whl   # mac (brew install mpv first)
pipx runpip jellytoast show pyobjc-core                     # must SUCCEED on mac
```
```powershell
pipx install "$env:TEMP\jt-qa\jellytoast-0.2.0-py3-none-any.whl"  # win (libmpv-2.dll on PATH)
pipx runpip jellytoast show cryptography                          # must FAIL on win
```
Launch with the bridge env and repeat the sweep (the gallery can be abbreviated
— surfaces in one theme — but run ALL of §B's feature rows).

## Deliverables

1. `dev/MAC_TEST_FINDINGS_0.2.0.md` or `dev/WINDOWS_TEST_FINDINGS_0.2.0.md` —
   findings in the house style of the 0.1.7 files: what passed, what broke
   (with repro + screenshot path), visual judgements.
2. **Tick PR #228**: edit the §A checkboxes and flip your OS's two matrix
   columns ⬜→✅ (or ❌ with a footnote) via `gh pr edit 228 --body-file …`.
3. Comment a short findings summary on the PR (`gh pr comment 228`).
4. Commit the findings file to the branch. **No co-author trailers. Do not
   merge the PR.**

## House rules

- Evidence over vibes: screenshot anything you judge, and re-read the PNG.
- If audio-out matters for a check, keep system volume LOW but nonzero.
- Kill any launched app instances before switching targets
  (`pkill -f jellytoast` / `Stop-Process -Name jellytoast`).
- Uninstall order at the end: leave the INSTALLER build installed (august may
  keep using it), `pipx uninstall jellytoast`.
