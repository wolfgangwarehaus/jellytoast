# dev/ — tools, briefs, and QA history

Everything here supports development and release; nothing ships. Three kinds
of file live side by side — this index says which is which, so the living
tools stay findable as the QA history accumulates. Paths are stable on
purpose (memory notes, issues, and PRs link into them); prefer adding to this
index over renaming/moving.

## Active tools (run these)

| File | Purpose |
|---|---|
| `cut_release.sh` | **The release ritual.** `dev/cut_release.sh X.Y.Z --push` stamps every manifest, snips the CHANGELOG, tags, pushes → draft release + MAS auto-submit. Idempotent metainfo stamping (safe on pre-stamped trees). |
| `run.sh` / `install.sh` / `install_doctor.py` / `create_desktop_entry.sh` | Local dev setup: run from checkout, system install, install verifier, desktop shortcut. |
| `smoke_test.py` | Headless end-to-end smoke against a live server (auth, search, stream, covers). Run by `qa_harness.py` and CI-adjacent checks. |
| `qa_harness.py` | Screenshot-gallery QA driver: sweeps every surface (dark+light, window states, mini player, dialogs) via the test bridge using REAL compositor capture (`spectacle`/PowerShell — never `win.grab()`, it's blur-blind). |
| `jt_ctl.py` / `jt_drive.py` | Test-bridge clients (app launched with `JT_TEST_BRIDGE=1`): one-shot CLI / reusable scenario library. `TMPDIR=/tmp` on both ends is load-bearing on Linux. |
| `gen_stress_library.py` | Generates a Skope-scale synthetic library (5,200 albums / 72.8k tracks / unique mixed-size covers, ~1.7 GB) for any Navidrome. Built for #cover-stall; reusable for all large-library work. |
| `repro_cover_stall.py` | Drives the REAL provider + image loader + connectivity tracker against a live (throttled) server — the harness that reproduced and then verified the #cover-stall fix. Pair with `gen_stress_library.py`. |
| `stress_large_library.py` | In-process large-grid stress with a FAKE QNAM (gate/no-stall/no-wipe invariants). Faster than `repro_cover_stall.py` but blind to real-server behavior — use both. |
| `update_translations.sh` | i18n catalog refresh (#232): `pyside6-lupdate` sweeps `jellytoast/*.py` for `tr()` strings into `jellytoast/i18n/*.ts`, `pyside6-lrelease` compiles the shipped `.qm`. Pass a language code (`dev/update_translations.sh fr`) to bootstrap a new catalog. Commit both `.ts` and `.qm`. |
| `mas_submit.py` | App Store Connect auto-submit (ops#2): waits for build processing, creates the version (AFTER_APPROVAL), sets What's-New, submits for review. Called by the `mas-submit` job. |
| `store_whats_new.py` / `store_patch_release_notes.py` | Store "What's new" extraction from the CHANGELOG (titles-only voice) + MS Store submission patching. Called by `msstore.yml` / `mas_submit.py`. |

## Living mission briefs (hand to a Claude session on the target machine)

Self-contained instructions for autonomous QA runs on other hardware — the
pattern: `git pull`, open Claude Code at the repo root, say
*"Read dev/<BRIEF>.md and execute it."*

| File | Target |
|---|---|
| `QA_SESSION_COMMON.md` | Shared bridge idioms + house rules every brief builds on. |
| `QA_SESSION_PLASMA.md` / `QA_SESSION_UBUNTU.md` / `QA_SESSION_WINDOWS.md` | Per-platform QA sessions (0.1.x era; still the base pattern). |
| `MAC_TEST_SESSION.md` | The macOS on-hardware session brief (0.1.7 blur era; superseded in parts by `QA_0.2.0_UNIVERSAL.md`). |
| `QA_0.2.0_UNIVERSAL.md` | The 0.2.0 artifact-QA program: installer + wheel matrix on Mac & Windows (PR #228's walkthrough). |
| `QA_STEAMDECK_FLATPAK.md` | The Deck flatpak leg (PR #231's matrix). |
| `QA_STEAMDECK_BLUR.md` | Deck blur diagnosis (#229) + the opaque-blur follow-up — **resolved**, kept for the methodology (complementary-wallpaper trap, red/green control test). |
| `QA_PICKUP.md` | Known QA-infra gaps to pick up (e.g. Windows capture foreground race). |

## Historical findings (evidence — read, don't rerun)

| File | What it records |
|---|---|
| `QA_0.1.5.md`, `MAC_TEST_FINDINGS.md`, `MAC_TEST_FINDINGS_0.1.7.md`, `UBUNTU_TEST_FINDINGS.md`, `WINDOWS_TEST_FINDINGS.md` | 0.1.x-era platform findings. |
| `MAC_TEST_FINDINGS_0.2.0.md` | 0.2.0 universal-dmg QA (found the ABI-broken bundled cryptography). |
| `WINDOWS_TEST_FINDINGS_0.2.0.md` | 0.2.0 Windows QA (validated DPAPI end-to-end; found the offline-play crash + upgrade-orphan issue). |
| `STEAMDECK_TEST_FINDINGS_0.2.0.md` | Deck flatpak QA (found the Qt-skew blur bug #230 and the https/TLS gap; the "opaque blur" row later resolved as a wallpaper artifact). |
| `mac_test_artifacts/`, `steamdeck_test_artifacts/` | Screenshot evidence referenced by the findings files. |
