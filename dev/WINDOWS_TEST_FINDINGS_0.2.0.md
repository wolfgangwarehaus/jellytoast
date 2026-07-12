# Windows 0.2.0 universal-installer QA — findings

**Box:** Windows 11 Home (10.0.26200), x86_64, non-admin user, single 125%
display. LAN `192.168.50.66/24`.
**Artifacts:** run [29176903113](https://github.com/wolfgangwarehaus/jellytoast/actions/runs/29176903113)
(latest green dispatch) — `jellytoast-0.2.0-windows-setup.exe`,
`…-x64-portable.zip`, `…-arm64-portable.zip`, `jellytoast-0.2.0-py3-none-any.whl`.
**Method:** structural checks on the real setup.exe + both portable zips →
silent per-user install → installed-app drive via the test bridge +
`qa_harness.py` gallery → smoke test → clean-venv `pipx` install of the wheel →
raw-Subsonic + credential-store diagnostics. Server: Navidrome (subsonic) at
`192.168.50.100:4533`, user `avtips` (**same server the Mac session used**).

**Verdict:** the headline **Windows change lands** — the DPAPI credential store
is correct end-to-end (lossless round-trip, consistent dual store, faithful
`d1:` migration on upgrade) and a **clean** install/resolve pulls **no
cryptography** on either channel. Native-ARM64 tree ships as its own distinct
zip. **No PR-caused defect found.** Two minor packaging notes (upgrade leaves a
stale `cryptography` orphan) and one **environmental block**: this box's saved
server password is stale (server-side rotation), so live auth fails 40 and the
content-dependent feature rows could not be driven here (couldn't re-enter the
current password; declined to clobber the stored login by switching to a demo).

---

## Install mechanics — Windows setup.exe

| Check | Result |
|---|---|
| Silent per-user install (`/VERYSILENT`), **no admin** | ✅ exit 0 in 27 s as a **non-admin** user — no elevation possible, so per-user confirmed |
| Launches | ✅ window up, bridge live, `jellytoast.version.__version__ == 0.2.0` |
| PE machine field of installed `jellytoast.exe` | ✅ `0x8664` (x64) — native slice on this x64 box |
| Native **ARM64** tree exists as its own artifact | ✅ `…-arm64-portable.zip` is a distinct tree (can't boot — no ARM hardware) |
| **No `cryptography`** in a **clean** tree | ✅ **both** portable zips contain **zero** `cryptography` entries |
| Upgrade over the prior 0.1.x install | ✅ installed over it; **still signed in**, resilience token **migrated `→ d1:`** (see credentials) |
| Uninstall from Settings → Apps | ⬜ not run (left installed per house rules) |

### 🟡 Minor (upgrade hygiene): stale `cryptography` orphan survives an in-place upgrade
The **installed** `_internal\cryptography` (+ `cryptography-49.0.0.dist-info`) is
present after the upgrade — but it is **stale**: dated **2026-06-20** while every
fresh 0.2.0 file is dated the build day (07-12). The 0.2.0 build itself is clean
(both portable zips: zero `cryptography`); the Inno installer overlays the new
files but never **purges** the now-dropped `cryptography` tree the old 0.1.x left
behind. Confirmed dead weight, not loaded — `'cryptography' in sys.modules ==
False` in the running app (DPAPI is the sole active path). Same pattern repeats
on the wheel channel (below). **Fix idea:** an Inno `[InstallDelete]` (or clean
target dir) so an upgrade drops files removed between versions; and qualify the
brief's "assert no `cryptography` folder" as *clean-install only*.

## Install mechanics — Windows wheel (pipx 1.14.0)

| Check | Result |
|---|---|
| `pipx install …whl` | ✅ jellytoast 0.2.0 (Python 3.14.5) |
| `cryptography` GONE on a **clean venv** | ✅ **ABSENT** after a fresh `pipx uninstall` + install — the resolve pulls none |
| `pyobjc-core` (mac-only) absent on Windows | ✅ ABSENT |
| Windows stack present | ✅ `winrt-*`, `Windows-Toasts`, `comtypes`, `pywin32-ctypes`, `keyring` all resolved |
| Full wheel feature sweep | ⬜ not driven (needs `libmpv-2.dll` on PATH + a working server) |

> ⚠️ **`--force` reinstall over an existing venv keeps the orphan.** A
> `pipx install --force` (upgrade in place) showed `cryptography 49.0.0` **with
> `Required-by:` empty** — orphaned from the prior 0.1.x, not pruned. A clean
> `uninstall`→`install` correctly resolves **zero** cryptography. So the source
> is right; only the *in-place upgrade* leaves the dead package — mirror of the
> installer note.

---

## The headline Windows change — DPAPI credentials (validated ✅)

`jellytoast/credentials.py` moves the Windows resilience store off `cryptography`
onto OS-native DPAPI (`d1:` blobs via `CryptProtectData`). Verified against the
**real installed app**, booleans only (no secrets printed):

| Assertion | Result |
|---|---|
| DPAPI `protect→unprotect` round-trip lossless | ✅ `True` |
| Server token migrated old-format `→ d1:` on this upgrade | ✅ (`d1:` after launch; was non-`d1:` before) |
| Stored `d1:` blob decrypts to non-empty | ✅ |
| Primary store (Credential Manager / keyring) present | ✅ |
| keyring value **==** DPAPI-decrypted value (dual store consistent) | ✅ |
| `cryptography` imported at runtime | ✅ **No** (`'cryptography' not in sys.modules`) |
| QSettings writable (no `AccessError` latch) | ✅ `Status.NoError` |

The credential machinery is correct. The migration **faithfully** re-encrypted
the existing token.

### 🔵 Environmental (NOT a PR bug): this box's saved password is stale → live auth 40
A raw `GET /rest/ping` returns **`Subsonic error 40: Wrong username or
password`**; so do `getAlbumList2` / `search_all` / `getMusicFolders` → every
library surface reads **empty** ("No albums yet"). Ruled out every PR-side cause:

- DPAPI round-trips losslessly and the dual store is **consistent** (above) — the
  credential isn't corrupted, it's faithfully preserved.
- Re-submitting the **same** stored creds via `authenticate()` **also** returns
  40 — so this is **not** the documented transient Navidrome "first-ping-after-
  restart" quirk (that clears on `authenticate`; see `verify_session` docstring).

⇒ the server-side password for `avtips` was **rotated**; the Mac box carries the
current one, this Windows box retained the old. **Environmental.** Consequence:
the audio/content rows (3–6, 9–14) **cannot be driven on this box** — I can't
re-enter the current password, and I declined switching to a public demo because
it would overwrite the stored URL/username/keyring/DPAPI blob (login clobber) for
non-new playback code already covered on macOS.

### 🟣 Minor UX (pre-existing, documented tradeoff): stale-cred state is ambiguous
By design `verify_session()` returns `True` on persisted creds (avoids a
false-positive logout on the Navidrome ping quirk), so with a genuinely-stale
password the UI shows **"No albums yet — your library may still be loading, or
it's empty"** rather than a re-login prompt — indistinguishable from a real empty
library or a slow load. A dedicated "sign-in expired" state would be clearer.
Platform-agnostic, not a blocker, and an explicit design decision — noting for
the record.

---

## Feature sweep — win exe column

| # | Feature | Result | Evidence |
|---|---|---|---|
| 1 | Sign in → relaunch → still signed in | ✅ | survived 0.1.x→0.2.0 upgrade; token migrated `→ d1:`, dual store consistent, session state restored on launch |
| 2 | Library browse + **multi-library picker degrade** | ⚠️ picker ✅ / browse ⬜ | **degrade fully validated** (below); content browse blocked by stale server |
| 3 | Playback | ⬜ | stale server, no radio station to fall back on |
| 4 | Queue reorder/restore | ⬜ | needs content |
| 5 | Now-playing + lyrics + visualizer | ⬜ | needs content |
| 6 | Equalizer audibly applies | ⬜ | needs audio |
| 7 | Mini player frost | ⚠️ | acrylic applies app-wide (ACTIVE); clean mini capture not obtained on this box (see harness note) |
| 8 | Casting discovery | ⚠️ | `discover_all()` ran cleanly; **0 devices** on this network — subsystem OK, nothing to drive |
| 9 | Offline download | ⬜ | needs content |
| 10 | Smart playlist | ⚠️ | resolve/rule engine **smoke PASS** (rule validation, date ops, crossfade curve); UI create-flow ⬜ |
| 11 | Internet radio | ⬜ | **0 stations** on server; can't create one without a server write |
| 12 | Scrobbling | ⬜ | nothing linked; DPAPI upgrade preserved token cleanly, **no garbled state** |
| 13 | OS media integration (SMTC) | ⚠️ | backend loaded (`winrt.windows.media` + `.interop`); live flyout not driven (no playback) |
| 14 | Notifications | ⬜ | no track-changes to fire; `Windows-Toasts` dep present |
| 15 | Frost/theming — **native acrylic** | ✅ | `blur.status() == ACTIVE`; body genuinely translucent (desktop bleeds through) — dark frosted composited shot |
| 16 | Tray + start-at-login + settings persist | ✅ | tray present; **start-at-login toggle validated** (adds/removes `HKCU\…\Run\jellytoast`, `is_enabled()` tracks, restored to off); QSettings `NoError` |
| 17 | About shows **0.2.0**; no false update chip | ✅ | `__version__ 0.2.0`; `is_newer('0.2.0','0.2.0') == False`; channel `source` |

**Row 2 — multi-library picker degrade (the new fix): validated.** Fed two
libraries + the three title forms, swept window width:

| win width | form shown | btn width == sizeHint | overruns the Albums dropdown? |
|---|---|---|---|
| 1400 / 1000 | `Music Library + Discovery` | 200 == 200 | no |
| 820 | `Music Library +1` | 140 == 140 | no |
| 720 / 620 | `2 libraries` | 98 == 98 | no |

Degrades through all three forms; the chosen form's **full** text always shows
(never clipped) and the button **never** overlaps the centred view dropdown. This
is the same layout logic behind the CI test I corrected on this branch.

**Row 15 — acrylic** (`zz_state_normal` @ `jt_qa_gallery`): dark-frosted body is
translucent with the desktop visibly blurred through it; layout clean, text
legible, empty-state renders gracefully across normal/maximized/fullscreen.
Light-mode composite not separately captured (harness capture limitation below),
but the acrylic backend is theme-independent and reads ACTIVE.

## Feature sweep — win wheel column
Install + dependency resolve validated (cryptography absent on clean venv,
pyobjc absent, Windows stack present). Runtime feature sweep **not driven** —
needs `libmpv-2.dll` on PATH and a working server; left ⬜, mirroring the Mac
session's wheel column.

---

## Test-infra (Windows) — fixed on branch
- 🟡 **`dev/qa_harness.py` manifest write crashed with `UnicodeEncodeError`
  (cp1252)** on Windows — the manifest carries unicode. **Fixed**: pinned
  `encoding="utf-8"` on the write (commit alongside these findings).
- ℹ️ Per-surface gallery captures didn't save on this box — only the final
  `zz_state_*` / mini frames landed (Windows foreground/capture race; QA_PICKUP
  already flags that Windows captures need a background driver + `force_foreground`).
  Not fixed here; the state frames were sufficient for the acrylic review.

## Housekeeping
- Stored credentials confirmed **intact** after diagnostics (URL/username/`d1:`
  token/keyring all present; an in-memory `authenticate()` probe wrote nothing to
  the store — next launch reloads the original state).
- Installer build left installed (august may keep using it); wheel `pipx`
  install to be removed. **Not merged.**
