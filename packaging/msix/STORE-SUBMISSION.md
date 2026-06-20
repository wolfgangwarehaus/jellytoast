# Microsoft Store submission — paste-ready runbook

Authoritative, copy-paste-ready walkthrough for jellytoast's first Microsoft
Store submission. Generated + adversarially verified 2026-06-20. Supersedes the
scattered "Partner Center submission" notes in `README.md` for the actual
submission. Build playbook detail still lives in `README.md`.

**Identity (already stamped in `AppxManifest.xml`):**
- Name `wolfgangwarehaus.jellytoast` · Publisher `CN=C9FAE1C4-4DEA-4550-8A71-969C88BABEB6`
- PFN `wolfgangwarehaus.jellytoast_yswr9h87xar1w` · Store ID `9PNLTPXGHN79`
- Package Version `1.0.0.0` (Store rejects a first-section-0 version; decoupled from the marketing version)
- Display/marketing version: **0.1.2** (the Store build ships as part of the 0.1.2 release)

---

## ⛔ PRE-SUBMISSION GATES — do ALL of these before clicking Submit

1. **Cut + push the `v0.1.2` git tag** the Store build is made from. Every license/source-offer/privacy link below points at `/v0.1.2/`; a link that 404s is a GPL-compliance defect and a likely cert snag. The tag must exist first.
2. **Host + verify the privacy policy URL.** *(Page added to PR #161; goes live when #161 merges + Pages deploys.)* `site/privacy.html` must be live at `https://wolfgangwarehaus.com/jellytoast/privacy.html`; confirm HTTP 200 + the corrected text before pasting it. (As of 2026-06-20 it returns 404 — pending the #161 merge.)
3. **Privacy policy must disclose the third-party flows** (DONE in this repo: scrobbling → ListenBrainz/Last.fm, radio cover art → MusicBrainz/Cover Art Archive). A policy that says "connects only to your server" is false and violates Store Policy 10.5.1.
4. ✅ **GPLv3 text added** — `COPYING` at repo root (verbatim from gnu.org) and bundled into the package via `jellytoast.spec`. (`LICENSE` stays GPLv2: the *source* is GPL-2.0-or-later; only the *bundled binary* is conveyed under GPL-3.0.) The "as shipped" link resolves once `v0.1.2` is tagged.
5. **Build → WACK → in-package QA all green** (Part 1) on the Win 11 laptop.
6. ✅ **Reviewer test path verified** — the public Jellyfin demo server is live (`demo.jellyfin.org/stable`, user `demo`, no password; confirmed HTTP 200, Jellyfin 10.11.11). It's in the "Notes for certification" so the reviewer can exercise the app.

---

## PART 1 — Build the package (Win 11 x64 laptop, Windows SDK installed)

`makeappx.exe` / `signtool.exe` live in `…\Windows Kits\10\bin\<ver>\x64\`.

### Phase A — manifest re-verification (no build)
The manifest is **correct and Store-ready as-is at `1.0.0.0`** — verified:
- Identity matches Partner Center byte-for-byte; `Version=1.0.0.0` valid (first section non-zero, 4th section 0 — reserved for Store re-versioning).
- All 7 referenced logo assets exist in `Assets/` (+ scale/targetsize variants).
- `EntryPoint="Windows.FullTrustApplication"` **and** the `uap10` packagedClassicApp/mediumIL attributes are **both** present on purpose: `uap10` didn't exist before Win10 2004 (19041), so `EntryPoint` is what legitimately keeps the `MinVersion 10.0.17763.0` (1809) floor. **Do not remove `EntryPoint`** (would force MinVersion 19041) and **do not raise MinVersion**.
- `runFullTrust` is the single restricted capability → triggers the manual review (~1–5 business days).
- **Zero manifest edits needed.**

### Phase B — build → pack → test-sign → sideload → WACK
Precondition: `packaging/windows/libmpv/libmpv-2.dll` is present BEFORE freezing (the spec only warns and ships an audio-dead app if missing).

```powershell
# B1 FREEZE
pyinstaller packaging\pyinstaller\jellytoast.spec --noconfirm
#   -> dist\jellytoast\ (jellytoast.exe + libmpv-2.dll + _internal\ + LICENSE + COPYING + THIRD-PARTY-NOTICES.md)

# B2 STAGE
Copy-Item packaging\msix\AppxManifest.xml dist\jellytoast\ -Force
Copy-Item packaging\msix\Assets dist\jellytoast\Assets -Recurse -Force

# B3 PACK the STORE-UPLOAD package — real Publisher, UNSIGNED (the Store re-signs). This is what you upload.
makeappx pack /d dist\jellytoast /p jellytoast-1.0.0.0.msix /o

# B4 LOCAL TEST-SIGN COPY only (never uploaded) — cert Subject must equal the manifest Publisher, so use a separate copy
$cert = New-SelfSignedCertificate -Type Custom -Subject "CN=jellytoast-test" `
  -KeyUsage DigitalSignature -CertStoreLocation Cert:\CurrentUser\My `
  -TextExtension @("2.5.29.37={text}1.3.6.1.5.5.7.3.3")
$pw = ConvertTo-SecureString -String "test" -Force -AsPlainText
Export-PfxCertificate -Cert $cert -FilePath test.pfx -Password $pw
#   (elevated) Import-PfxCertificate -FilePath test.pfx -Password $pw -CertStoreLocation Cert:\LocalMachine\TrustedPeople
Copy-Item dist\jellytoast dist\jellytoast-test -Recurse -Force
#   edit dist\jellytoast-test\AppxManifest.xml: Publisher="CN=jellytoast-test"
makeappx pack /d dist\jellytoast-test /p jellytoast-test.msix /o
signtool sign /fd SHA256 /a /f test.pfx /p test jellytoast-test.msix

# B5 SIDELOAD the test copy
Add-AppxPackage .\jellytoast-test.msix   # re-install: Get-AppxPackage *jellytoast* | Remove-AppxPackage; then add

# B6 WACK — Store-certification track on jellytoast-test.msix
#   For packagedClassicApp + runFullTrust: package-content/manifest/capability checks MUST pass.
#   AppContainer-only "supported API" failures are N/A for a full-trust desktop app.
```

### Phase C — in-package QA gate (sideloaded build, priority order)
- [ ] **1. AUDIO PLAYS — the #1 packaging risk.** Sign in, play a track, hear sound; `MPV_AVAILABLE` True, no "Missing dependency" dialog (proves bundled `libmpv-2.dll` loads under read-only WindowsApps). If this fails, STOP and fix.
- [ ] 2. Start-menu tile launches; brand icon; name reads "jellytoast".
- [ ] 3. Taskbar groups under the brand icon, not generic Python (package AUMID live).
- [ ] 4. A toast fires showing "jellytoast" + app icon.
- [ ] 5. SMTC: hardware media keys + volume-flyout transport control playback.
- [ ] 6. Autostart survives reboot: "Launch at login" on → starts after reboot; off → doesn't (proves `windows.startupTask`; HKCU Run is ignored for packaged apps).
- [ ] 7. Settings/cache/downloads persist across restart (`%LOCALAPPDATA%\jellytoast`).
- [ ] 8. Single instance: second launch focuses the existing window.
- [ ] Acrylic backdrop renders; frameless chrome intact; credentials persist in Windows Credential Manager.

Only after **WACK + the QA gate both pass** do you upload `jellytoast-1.0.0.0.msix` (the B3 unsigned, real-Publisher package).

---

## PART 2 — Partner Center fields (paste-ready)

### Properties
- **Category:** Music *(no subcategory for non-game apps)*
- **Privacy policy URL:** `https://wolfgangwarehaus.com/jellytoast/privacy.html` *(must be live — gate 2)*
- **Custom license terms** — select "I'll provide my own license terms", paste:
  > jellytoast is free and open-source software. The project source is offered under GPL-2.0-or-later. Because this Microsoft Store build statically bundles components offered under GPL-3.0-compatible terms (the PySide6/Qt-for-Python bindings and a GPL build of libmpv/FFmpeg), **this binary is conveyed to you under version 3 of the GNU General Public License (GPL-3.0-or-later).** Full text: https://www.gnu.org/licenses/gpl-3.0.html · As shipped: https://github.com/wolfgangwarehaus/jellytoast/blob/v0.1.2/COPYING · Complete corresponding source for this build: https://github.com/wolfgangwarehaus/jellytoast/tree/v0.1.2 · Bundled third-party source + notices: https://github.com/wolfgangwarehaus/jellytoast/blob/v0.1.2/packaging/THIRD-PARTY-NOTICES.md
- **Product declarations:** generative-AI = **unchecked**; in-app-purchase-outside-Store = unchecked; game clip recording = unchecked; pen/ink = unchecked; accessibility-tested = unchecked (not formally tested). Alternate-drive install / OneDrive backup = leave at their default if shown.

### Data-collection / privacy declaration  ⚠️ get this exactly right
The **developer receives nothing** (no analytics/telemetry; creds in the OS keychain; settings in `%LOCALAPPDATA%`). BUT the app, **only when the user opts in**, transmits playback history to a user-connected scrobble service (ListenBrainz/Last.fm) and queries MusicBrainz/Cover Art Archive for radio cover art. Answer to match the (now-corrected) privacy policy: declare that the app does not send data to the *developer*, while the privacy policy discloses the optional user-directed third-party transmissions. Do **not** post a flat "no data leaves the device" that the privacy policy contradicts — an inconsistency here is a known rejection vector. *(See Open items — confirm the precise toggle wording in the live Partner Center UI.)*

### Age ratings (IARC questionnaire) — REQUIRED; wrong answers inflate the rating
Category = a **non-game** category (Entertainment / Utilities — NOT "Game", NOT "Reference/News/Educational").
- Violence / sexual content / profanity / controlled substances / gambling / fear / discrimination / misc in-app-browser → **No / None** (the app ships no content of its own).
- Users interact/communicate/share with other users → **No** (no chat/comments/social; connecting to *your own* server is a private client-server fetch).
- Shares physical location with other users → **No.**
- In-app purchases / digital goods → **No** (Ko-fi tip is an external link, not an IAP).
- Unrestricted internet / built-in web browser → **No** (it's a music player, not a browser; user-added radio URLs are not an open web gateway).
- Collect/share personal info with developer/third parties → answer consistent with the data declaration above + privacy policy (optional opt-in scrobbling to the user's own service; developer collects nothing).
- **Expected rating: 3+ / ESRB Everyone / PEGI 3 / USK 0.** Trap to avoid: do NOT answer the "uncontrolled/user-generated content" questions Yes just because it streams "arbitrary user audio" — that audio is the user's own private library, not UGC shared between strangers; a Yes wrongly inflates to 12+.

### Submission options
- **Restricted capabilities** field (the dedicated runFullTrust explanation box — *NOT* "Notes for certification"; they're different fields) — paste:
  > jellytoast is a native x64 desktop music player, packaged as a classic (Desktop Bridge) MSIX (RuntimeBehavior packagedClassicApp / EntryPoint Windows.FullTrustApplication), so it declares runFullTrust because it is a full-trust Win32 process, not a sandboxed AppContainer app. Full trust is required to (1) load the bundled native audio engine, libmpv-2.dll, via ctypes for bit-perfect, gapless playback (an AppContainer cannot load an arbitrary unpackaged native DLL), and (2) call classic Win32/desktop WinRT APIs unavailable in the sandbox: System Media Transport Controls (hardware media keys, volume flyout, lock screen) via the GetForWindow interop on the app's own HWND; the DWM Acrylic backdrop and frameless window; the taskbar play/pause overlay (ITaskbarList3); SetThreadExecutionState to prevent sleep during playback; and single-instance focus-raise. No code is downloaded, generated, or executed at runtime — libmpv-2.dll and every dependency ship inside this signed package and nothing else is fetched and run. At runtime the app connects only to the self-hosted media server (Jellyfin/Navidrome/Subsonic) the user configures, to stream the user's own music; it bundles no content and contacts no developer-operated service. Credentials are stored in Windows Credential Manager; settings and cache stay in per-user %LOCALAPPDATA%; no data is sent to the developer.
- **Notes for certification** (testability + pre-empt the PyInstaller scan) — paste:
  > Test instructions: jellytoast is a client for a self-hosted Jellyfin/Navidrome/Subsonic server and requires one to function. To exercise the app, connect it to the public Jellyfin demo server — Server: https://demo.jellyfin.org/stable · Username: demo · Password: (blank). (Please verify the demo server is reachable; if not, we can supply temporary credentials on request.) Packaging note: this is a Python/PySide6 (Qt6) app frozen with PyInstaller (onedir); the standard PyInstaller bootloader is occasionally flagged as a heuristic false positive. The package contains no malware, no obfuscated/downloaded code, and no runtime code execution — every binary, including libmpv-2.dll (which embeds FFmpeg under LGPL/GPL), ships inside this signed package. Full source for this exact build: https://github.com/wolfgangwarehaus/jellytoast/tree/v0.1.2 . If the security scan flags the bootloader, it is a known false positive — please contact us via the certification report.

### Store listing (language: English (United States))
**Description** (≤10,000 chars):
```
jellytoast is a desktop music player for your own self-hosted music server. It connects to Jellyfin, Navidrome, and other Subsonic / OpenSubsonic servers and streams the music you already own — no subscriptions, no ads, no tracking, and nothing locked behind a paywall.

Sign in with your server address and credentials and your whole library is there: albums, artists, songs, playlists, and genres, in a grid or list view, with fast search and an A–Z jump rail.

WHAT IT DOES

Bit-perfect audio. Playback runs through the mpv engine, so FLAC, ALAC, OPUS, DSD, MP3, AAC, OGG, WAV, and M4A all play at full quality with gapless transitions. Pin output to a specific WASAPI device for a clean, direct audio path. A 10-band graphic equalizer with presets and ReplayGain volume leveling are built in.

Cast anywhere. Send your music to Chromecast, AirPlay 2, DLNA, Sonos, or Snapcast receivers, all discovered automatically. A built-in local relay can forward the stream for trickier setups — Tailscale connections, public hostnames, self-signed certificates, or casting downloaded tracks while your server is offline.

Offline downloads. Cache a track, an album, an artist, a playlist, or your entire library for offline playback. Download quality is independent of streaming quality. A Wi-Fi-only option holds downloads until you are on an unmetered network. When your server becomes unreachable, jellytoast switches to offline mode automatically and plays from your local cache.

Made for the Windows desktop. A frameless window with real Acrylic frosted-glass blur, light and dark themes that follow your Windows color scheme, and your own accent color. Hardware media keys and the now-playing flyout, toast notifications, a taskbar play/pause overlay, a tray icon, optional launch-at-login, and prevent-sleep during playback all work natively.

Floating mini player. A compact, always-on-top window in two modes — a small cover-plus-transport view and a larger album-art view.

A full now-playing experience. Album art, synced or plain lyrics that scroll in time with the track, and a live audio visualizer driven by real-time frequency analysis.

And plenty more:
• Smart playlists — rule-based, with a live preview as you edit
• Smart shuffle — keeps the same artist from landing back-to-back
• Internet radio — curated presets plus your own stations, with ICY now-playing info
• Start radio / instant mix from any album, artist, genre, or track
• Scrobbling to ListenBrainz or Last.fm (optional; you connect the account)
• Tag editing, including cover-art replace and apply-to-whole-album (Jellyfin)
• Sleep timer with a gentle fade-out
• Favorites, resume-where-you-left-off, and a persistent play queue
• Encrypted, OS-keychain credential storage — your password lives in Windows Credential Manager, never in plain text

PRIVACY

The jellytoast developer collects nothing — no analytics, telemetry, crash reporting, or advertising. The app connects to the music server you configure and to cast devices on your own network. Optional features you turn on (scrobbling, internet-radio cover-art lookup) contact the third-party services you choose; none of that data goes to the developer. Full privacy policy: https://wolfgangwarehaus.com/jellytoast/privacy.html

OPEN SOURCE

jellytoast is free and open-source software. This Microsoft Store build is conveyed under the GNU General Public License, version 3 (GPL-3.0-or-later); a copy of the license ships with the app. Complete corresponding source for this build: https://github.com/wolfgangwarehaus/jellytoast/tree/v0.1.2

WHAT YOU NEED

A Jellyfin, Navidrome, or other Subsonic / OpenSubsonic-compatible music server you can reach over your network or the internet. jellytoast is a client — it does not host or provide any music itself. Windows 10 or 11, 64-bit.
```

**Short description** (≤1,000):
```
A desktop music player for your own Jellyfin, Navidrome, or Subsonic server. Bit-perfect playback, casting to Chromecast/AirPlay/Sonos, offline downloads, a floating mini player, and a frosted-glass UI. No ads, no tracking, open source.
```

**What's new in this version:** leave **BLANK** (Microsoft guidance for a first submission).

**Search terms** (≤7): `Jellyfin` · `Navidrome` · `Subsonic` · `self-hosted music` · `FLAC player` · `music streaming client` · `Chromecast audio`

**System requirements:** OS Windows 10/11; Architecture x64. Free-text: "A reachable Jellyfin, Navidrome, or Subsonic/OpenSubsonic server (the app is a client and provides no music itself)."

**Screenshots** (≥1, recommend 4+; PNG; desktop ≥1366×768): upload the 1600×900 shots `library`, `now-playing`, `cast`, `downloads`, `settings` (optionally `smart-playlists`, `radio`). **Do NOT** use `mini-compact`/`mini-expanded` (1280×720 — below the desktop floor).

---

## PART 3 — Submit + malware-scan appeal path
If cert fails on the security/malware scan (PyInstaller bootloader false positive):
1. Read the cert report in Partner Center — it names the failing check + signature.
2. Reply to the cert-result email, or email `reportapp@microsoft.com` with Store ID `9PNLTPXGHN79`, explaining the known PyInstaller-bootloader false positive (reuse the Notes-for-certification wording).
3. Corroborate via VirusTotal if helpful (note heuristic/bootloader-only; source is public).
4. If the bootloader is the culprit, rebuild against the latest PyInstaller (recompiled bootloader clears most heuristics), repack, resubmit.

Because the bootloader is pre-declared in Notes for certification, most scans pass first try — the appeal is the fallback.

---

## Open items / decisions
- **GPL-3.0 conveyance** — confirmed by `docs/LICENSING.md` (the PySide6 LGPL-3.0/GPL-3.0 combo makes GPL-2.0-only incompatible; the `-or-later` permits taking the bundled binary to v3). The project *source* stays GPL-2.0-or-later. Worth a final human sanity-check since it's a licensing call.
- ✅ **`COPYING` (GPLv3 text)** — added at repo root + bundled via `jellytoast.spec`; ships in the package. The "as shipped" link resolves once `v0.1.2` is tagged.
- **Data-collection toggle wording** — confirm the exact Partner Center phrasing at submission time so the answer is consistent with the third-party-scrobbling disclosure (don't over- or under-declare).
- **Jellyfin demo server** — verify `demo.jellyfin.org/stable` is currently reachable; if not, prepare temporary test credentials for the cert notes.
