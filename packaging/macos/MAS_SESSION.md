# Mac App Store — feasibility, compliance & worklist

The **App Store** track for jellytoast, run in parallel with (and *after*) the
Developer-ID notarized `.dmg` (see `MACOS_SESSION.md`). This is the **harder**
path; the `.dmg` ships first and is the primary channel. MAS is pursued for
discoverability/trust, accepting it takes real research + engineering.

Backed by a verified-research pass (2026-06-23): GPL/App-Store licensing
go/no-go, the sandbox build+submission pipeline, and a codebase
filesystem-access audit. Durable summary in memory `project_macos_release`.

---

## Verdict — two gates

| Gate | Verdict | The crux |
|---|---|---|
| **1. Licensing** | ✅ **GO (conditional)** | You're the **sole copyright holder** → self-authorize App Store distribution of your own code, no relicensing campaign. **Condition:** bundle **LGPL-only** libmpv + FFmpeg (you can't relieve code you don't own). |
| **2. Technical (sandbox)** | ⚠️ **HARD but bounded** | Unproven for Python+PySide6, but most of the audit's "blockers" are **Linux/Windows-only code paths that never run on macOS**. The real macOS work is a finite list (below). |

**Bottom line:** neither gate is a wall. Licensing is solvable with a short
exception + an LGPL media build. The sandbox is real engineering but smaller on
macOS than first feared. Prove it with a cheap **feasibility spike** (below)
*before* the storage refactor. **macOS App Store only — never iOS** (LGPLv3 Qt
can't satisfy anti-tivoization on a locked iPhone; a Mac `.app` can).

---

## Gate 1 — Licensing (GO, conditional)

**Why it's a go:** Apple's Licensed Application EULA (device limits, ToS,
FairPlay DRM) imposes "further restrictions" GPLv2 §6 / GPLv3 §10 forbid a
*distributor* from adding. But a **copyright owner is not bound by their own
license grant** (FSF GPL FAQ). Git confirms jellytoast is sole-authored by
august (dependabot's manifest bumps + the Claude AI trailer create no
third-party copyright). So you can ship the same code on the App Store under
Apple's terms while keeping the public GPL-2.0-or-later release — VLC's outcome
without VLC's multi-contributor relicensing pain.

**Steps:**
- [ ] **Adopt the App Store exception** — review `mas/APP-STORE-EXCEPTION.draft.md`;
      add the GPLv3 §7 additional permission to `LICENSING.md` (recommended) and
      keep the private authorization memo. **Keep `GPL-2.0-or-later`** — the
      `-or-later` is load-bearing (PySide6 LGPLv3 compat + the §7 route via GPLv3).
- [ ] **Build libmpv LGPL-only** — `-Dgpl=false` (mpv defaults to **GPL**!) over
      an **LGPL FFmpeg** (omit `--enable-gpl`, the default). Zero functional loss
      for a player (only GPL *encoders*/filters dropped). ⚠️ The `.dmg` build's
      `get_libmpv.sh` uses Homebrew mpv = **GPL** — fine for the GPL `.dmg`, but
      the MAS build needs a **separate from-source LGPL libmpv** (also built
      **without Lua**, see Gate 2). Verify no GPL file is linked (`-Dgpl=false`
      excludes files but you must confirm the enabled feature set is LGPL).
- [ ] Adopt a **CLA/DCO** going forward — the sole-author escape silently erodes
      the moment an outside contributor lands copyrightable code.

*Open (medium-confidence):* the LGPLv3 **relink obligation** under MAS code
signing — confirm a user can replace a bundled Qt dylib in the installed `.app`
and relaunch. Test in the spike. (iOS fails this; macOS should pass.)

---

## Gate 2 — Technical (sandbox): the macOS-relevant work

The fs-audit flagged 10 areas, but **most are non-macOS paths**. Triaged for the
macOS sandboxed build:

### Not macOS problems (Linux/Windows-only — never execute on a Mac)
KWin `kreadconfig`/`kwriteconfig`/`qdbus` (blur/keep_above/drag_repaint/
kde_titlebar), `pactl` (mpv uses coreaudio on macOS), `xprop`/`xrdb` + `/usr/
share/icons` reads (X11/KDE), `notify-send` (Linux), `powershell` (Windows),
PipeWire config, `/etc/machine-id` (macOS falls back to hostname:username).
These are guarded by `IS_LINUX`/`is_x11`/`is_kde_*`/`IS_WINDOWS` and are no-ops
on macOS — **no sandbox work needed**.

### Real macOS-MAS work items
- [ ] **Container path migration.** Under the sandbox, `QStandardPaths` /
      `Path.home()` / `$HOME` auto-redirect into
      `~/Library/Containers/io.github.wolfgangwarehaus.jellytoast/Data`, so
      `settings.py` (QSettings), `offline/locations.py` (downloads.db +
      downloads/), `image_cache.py` (covers), `disk_cache.py` (view_cache)
      "just move." Work = **migrate existing data** from the non-sandboxed `.dmg`
      location on first sandboxed launch (one-time copy).
- [ ] **Autostart needs a different mechanism.** A sandboxed app **cannot** write
      `~/Library/LaunchAgents` (the `autostart/_macos.py` from the `.dmg` branch).
      The MAS build must use the App Sandbox login-item API (`SMAppService` /
      `ServiceManagement`, via pyobjc) — gate it on the build variant.
- [ ] **Cast proxy under sandbox.** Keep it; add `network.server`. The listener
      (port 8943) + serving offline blobs is fine because the blobs live **in the
      container**. LAN discovery triggers the macOS 15+ **Local Network prompt**
      (one-time; handle a denial gracefully). Verify the listener binds under
      sandbox in the spike.
- [ ] **Keychain.** Add `keychain-access-groups` entitlement for the keyring
      store; the dual-store AES-GCM blob (in container QSettings) is the fallback.
- [ ] **macOS notifications backend** — `notify-send` is Linux-only; the Mac
      needs a `UNUserNotification`/pyobjc backend (also wanted for the `.dmg`).
- [ ] **User-selected download folder** (Phase 6) — outside the container needs
      `files.user-selected.*` + a **security-scoped bookmark** via native NSURL
      APIs (pyobjc); Qt's `QFileDialog` doesn't create bookmarks itself.
- [ ] **Build-variant flag** — a way for the app to know it's the sandboxed MAS
      build (switches autostart + path-migration + any feature gating).

### Packaging / signing / submission (different from the .dmg)
- [ ] **Certs:** Apple Distribution (signs the `.app`) + Mac Installer
      Distribution (signs the `.pkg`) — **not** Developer ID. Plus an App ID + a
      macOS App Store provisioning profile carrying `app-sandbox`.
- [ ] **Entitlements:** use `mas/entitlements.mas.plist` (sandbox set; **no**
      `disable-library-validation`, **no** JIT entitlements).
- [ ] **Library validation:** re-sign **every** bundled Mach-O (Qt, Python,
      libmpv + closure, all `.so`) with your **own Apple Distribution Team ID**,
      **inside-out**, no `--deep`. (disable-library-validation is rejected on MAS
      and **crashes on macOS 26 Tahoe** — re-signing is the only path.)
- [ ] **libmpv without Lua** (`-Dlua=disabled`) so there's **no JIT** requirement
      (avoids `allow-jit`/`allow-unsigned-executable-memory`, hostile to MAS).
- [ ] **Non-public-API scan:** run `mas/scan_symbols.sh` before every upload.
      Targets: Qt `_responsibility_*` (**fixed in PySide6/Qt ≥ 6.5** — verify the
      wheel; older needs `-feature-appstore-compliant`), CPython **`itms-services`
      string** (Py 3.12+ → build CPython with **`--with-app-store-compliance`**,
      3.13+), and linked `_lzma_*` (strip/avoid). Assume the stock pip wheel +
      python.org CPython are **not** MAS-clean until the scan says so.
- [ ] **Toolchain:** PyInstaller `--onefile` is **barred** under sandbox; use
      `--onedir` or **py2app** (more proven for `.app`), then the bespoke
      inside-out signing script + `productbuild` → signed `.pkg` →
      **Transporter** / `altool --upload-app` (MAS builds are **not** notarized).

---

## The feasibility spike (do this BEFORE the big refactor)

Smallest thing that proves or kills the path on the Scaleway Mac:

1. A trivial **sandboxed PySide6 window + LGPL-libmpv** (no Lua) `.app`.
2. Sign **inside-out** with the **Apple Distribution** cert + `entitlements.mas.plist`
   + provisioning profile; `productbuild` a signed `.pkg`.
3. `mas/scan_symbols.sh` → **clean** (proves the Qt/CPython symbol gate).
4. **Transporter validate** the `.pkg` → accepted (proves certs/sandbox/profile).
5. On launch: libmpv **plays audio** under sandbox; the **Local Network prompt**
   appears and a cast device is discovered.
6. Replace a bundled **Qt dylib** in the installed `.app` and relaunch (proves the
   **LGPLv3 relink** obligation is satisfiable under MAS signing).

✅ all six → invest in the full container migration + autostart + cast-proxy
hardening + the real bundle. ❌ any hard fail (esp. 3/4/6) → MAS stays deferred
and the `.dmg` remains the macOS channel.

---

## Risks / open questions
- No public record of a **PySide6 app on MAS** — integration is unproven (spike
  de-risks it).
- The **LGPLv3 relink-under-MAS-signing** question (spike step 6) is the
  medium-confidence legal-meets-technical unknown.
- An **LGPL libmpv arm64 build without Lua** has to be produced from source
  (no off-the-shelf Homebrew option) — non-trivial but well-trodden.
- A **custom CPython** (`--with-app-store-compliance`) may be needed — adds build
  complexity vs the stock interpreter the `.dmg` uses.

## Status
This branch (`feat/macos-app-store`, stacked on `feat/macos-packaging-foundation`)
adds only **research + scaffolding**: `mas/entitlements.mas.plist`,
`mas/scan_symbols.sh`, `mas/APP-STORE-EXCEPTION.draft.md`, and this worklist. **No
app code or build wiring yet** — that starts after the spike. DON'T merge as
"MAS support"; it's the compliance foundation.
