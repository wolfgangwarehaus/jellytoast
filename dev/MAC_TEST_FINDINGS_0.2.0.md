# macOS 0.2.0 universal-installer QA — findings

**Box:** Intel Mac (x86_64), macOS 15.7.7 (24G720) — so this run natively
exercises the **Intel slice** of the universal build (not Rosetta).
**Artifacts:** run [29176903113](https://github.com/wolfgangwarehaus/jellytoast/actions/runs/29176903113)
(latest green dispatch) — `jellytoast-0.2.0-macos-universal.dmg` +
`jellytoast-0.2.0-py3-none-any.whl`.
**Method:** structural checks on the dmg → quarantine-faithful Gatekeeper
launch → installed-app feature sweep via the test bridge + `qa_harness.py`
gallery (dark+light, 25/25 captures) → smoke test → visual review of composited
screenshots. Server: Navidrome (subsonic) at `192.168.50.100:4533`, signed in
as `avtips`.

**Verdict:** the universal packaging + the headline pyobjc-in-dmg work
(vibrancy, media controls, Notification Center) all land and work. **One real
bug**: the dmg bundles a `cryptography` whose native lib is ABI-mismatched
against the bundled `libssl`, so the encrypted-token *fallback* credential
store is dead on macOS (app still runs — Keychain is the primary store).

---

## Install mechanics — macOS dmg

| Check | Result |
|---|---|
| `lipo -archs …/MacOS/jellytoast` | ✅ `x86_64 arm64` (fat) |
| pyobjc `_objc.cpython-312-darwin.so` | ✅ fat `x86_64 arm64`, present in `Contents/Frameworks/objc` |
| `codesign --verify --deep --strict` | ✅ valid on disk, satisfies Designated Requirement |
| `spctl -a -t exec -vv` (Gatekeeper verdict) | ✅ accepted, source = **Notarized Developer ID** (WILLIAM AUGUST MUELLER, UNP3CF774H) |
| `stapler validate` — **dmg** | ✅ "The validate action worked!" (dmg carries the ticket, offline-valid) |
| `stapler validate` — **.app** | ⚠️ "does not have a ticket stapled to it" (see note) |
| `LSMinimumSystemVersion` | ✅ `15.0` |
| `CFBundleShortVersionString` | ✅ `0.2.0` |
| First-launch Gatekeeper (quarantine applied) | ✅ **benign** "downloaded from the Internet… Apple checked it… none was detected" dialog with a normal **Open** button — NOT the "unidentified developer" hard block |

**Notes**
- *Only the dmg is stapled, not the .app inside.* The dmg is offline-valid, but
  a copied-out `.app` carries no embedded ticket, so its *offline* first launch
  leans on Gatekeeper's cached notarization record / an online check rather than
  an embedded staple. Low severity (spctl accepts; users are typically online at
  first launch), but the PR checklist's "stapled ticket, offline-valid" is
  strictly true for the dmg, not the installed app. Consider `stapler staple`ing
  the `.app` before building the dmg if fully-offline first launch matters.
- The PR checklist item "first launch is Gatekeeper-clean — no warning dialog"
  is slightly optimistic: *any* quarantined download of *any* notarized app
  shows the one-time benign "downloaded from the Internet, are you sure?"
  confirmation. Notarization is working correctly; that prompt is expected and
  is the good one (one-click Open, no right-click-open dance). Screenshot:
  `01-first-launch.png`.
- Test-method caveat: artifacts came via `gh run download` (no quarantine), so I
  applied `com.apple.quarantine` by hand to make the Gatekeeper test faithful,
  then launched. A scripted `cp`+`open` of a quarantined app runs it under **App
  Translocation** — an artifact of not drag-installing via Finder, not a product
  issue. De-quarantining (what a Finder drag does) and relaunching from
  `/Applications` runs it in place, clean.

---

## New-in-dmg native features (the point of this PR) — all ✅

pyobjc genuinely ships and executes in the dmg (previously absent outside the
MAS build). Confirmed at runtime, not just by presence:

- **Native vibrancy (row 15):** `jellytoast.blur.status().name == "ACTIVE"`; the
  window body is genuinely translucent — the desktop shows through the frosted
  panel in both dark and light themes (`02-launch-state.png`,
  `light:frosted__albums.png`). App log shows `NSVisualEffectView` added +
  `blur/_macos.py` running `NSColor.clearColor().CGColor()`.
- **Control Center / media keys (row 13):** `MPNowPlayingInfoCenter` populated
  with title / artist / album / duration / elapsed / playbackRate **and
  `MPMediaItemArtwork`** — so the Now Playing widget shows cover art. Menu-bar
  now-playing indicator appears during playback (`03-playback.png`).
- **Notification Center banners (row 14):** `UNUserNotificationCenter` available
  (`_get_center()` non-nil, `_is_bundled()` True); `notify()` delivers a real
  Notification Center banner with the **jellytoast icon** (`05-notification.png`
  — the authorization banner; track-change body shows once permission granted).

---

## Feature sweep (PR §B)

| # | Feature | Result | Evidence |
|---|---|---|---|
| 1 | Sign in → relaunch → still signed in | ✅ | Navidrome `avtips` persisted across a full quit/relaunch; Settings→General shows it, green dot |
| 2 | Library browse + multi-library picker | ✅ (picker present) | "Music Library ▾" + "Albums ▾"; Albums/Artists/Songs/Genres all load (smoke: 229 genres). *Long-title degrade not stress-driven — see gaps.* |
| 3 | Playback: play/seek/next/prev/volume | ✅ | Played ABBA Gold — "Dancing Queen" **Streaming · FLAC · 1051 kbps**, position advanced, seek to 60s, next → "Knowing Me, Knowing You", volume set. Audio at low system volume. |
| 4 | Queue | ✅ (populate) | Full album queue in Now Playing, current row highlighted. *Reorder + quit/relaunch-restore not driven — see gaps.* |
| 5 | Now-playing page + lyrics + visualizer | ✅ | Cover, "Show lyrics" available (smoke: `get_lyrics` OK), live purple visualizer waveform (`04-now-playing.png`) |
| 6 | Equalizer audibly applies | ✅ | `apply_eq` pushed a real `firequalizer` chain into the live mpv `af` (10 bands/channel, exact gains) |
| 7 | Mini player — frost matches | ✅ | Compact frosted pill, body matches main window (`zz_mini_player.png`) |
| 8 | Casting: devices discovered | ✅ | 9 Chromecast devices found (Sunroom/Hallway/Living room speakers, groups). AirPlay/DLNA/Sonos: 0 this run. Discovery listing per brief. |
| 9 | Offline: download album, disconnect, play | ⬜ not driven | See gaps — involved + unrelated to this PR's changes |
| 10 | Smart playlist: create → preview → Save & Play | ◐ partial | Smoke: `date_added in_the_last 30` resolved **193/193 in-window**; rule validation + operators pass. UI create-flow not driven. |
| 11 | Internet radio plays | ✅ | Played "KEXP 90.3 Seattle" → now-playing carries the live MP3 stream URL |
| 12 | Scrobbling | ⬜ n/a this box | No Last.fm / ListenBrainz linked; link path not exercised. (Windows DPAPI re-link note is Windows-only.) |
| 13 | OS media integration (media keys / Control Center) | ✅ | See new-features above — MPNowPlayingInfoCenter + artwork |
| 14 | Notifications: track-change banner + frosted "other audio" toast | ✅ / ◐ | Notification Center banner delivered (branded). Frosted "other audio playing" toast not triggered. |
| 15 | Frost/theming: native blur + theme switch | ✅ | Vibrancy ACTIVE; body translucent; dark **and** light both correct across all 10 surfaces (gallery) |
| 16 | Tray icon + start-at-login + settings persist | ◐ present | "Hide to tray when window closed" ✓, "Launch jellytoast at login" toggle present in Settings; toggles not power-cycled |
| 17 | About/Settings shows 0.2.0; no false update chip | ✅ | Runtime `__version__ == 0.2.0`, plist `0.2.0`; no "update available" chip; "Check for updates" idle |

Smoke test (`dev/qa_harness.py` → smoke): **all checks passed**, incl. live
provider auth from the dual store, search (songs=12/albums=11/artists=2),
instant-mix, genre radio, stream serve (**206 audio/flac**), cover serve
(image/jpeg), smart-shuffle anti-clustering on live data.

Gallery: **25/25** captures OK, dark + light, all 10 surfaces + window states +
mini player + Settings dialog. No blank/black/mis-draw after
maximize/fullscreen. Light-theme frost reads as a clean near-white translucent
panel (legible), dark-theme frost shows desktop through — both correct.

---

## Bugs / issues

### 1. [Medium] dmg-bundled `cryptography` is ABI-broken → mac fallback credential store is dead
App log, twice (startup + on provider touch):

```
WARNING [jellytoast.credentials] token decryption failed:
  dlopen(…/Contents/Frameworks/cryptography/hazmat/bindings/_rust.abi3.so):
  Symbol not found: _SSL_get0_group_name
  Expected in: …/Contents/Frameworks/libssl.3.dylib
```

- The bundled `cryptography` Rust extension needs `_SSL_get0_group_name`
  (OpenSSL ≥3.2); the **bundled `libssl.3.dylib` is older** and doesn't export
  it. So the encrypted-token store can't decrypt.
- The QSettings `server/token` is a `v1:` encrypted blob — i.e. exactly what
  this broken lib is meant to read. It's currently **unreadable**. The app only
  stays signed in because the macOS **Keychain (keyring) is the primary store**;
  the `v1:` blob is the *resilience/fallback* copy and is effectively dead
  weight. Practical risk: if the Keychain entry were ever cleared, the session
  could not be recovered from the fallback and the user would have to re-sign-in.
- **Root cause is bundling, not source:** the wheel/mac still depend on
  `cryptography>=43.0.1` (metadata), and a fresh `pipx install` pulls a
  cryptography whose wheel matches its own OpenSSL — so this is a
  PyInstaller-freeze version-skew between the bundled cryptography and the
  bundled libssl in *this dmg*, not a code defect.
- **Fix direction:** pin the frozen `cryptography` to a build whose bundled
  OpenSSL exports `_SSL_get0_group_name` (or bundle a matching libssl ≥3.2), OR
  — consistent with the Windows direction of this PR — drop `cryptography` on
  macOS too and lean on the Keychain (keyring) as the sole store. Given the
  PR's whole theme is "swap the credential store off `cryptography`," macOS is
  the loose end.

### 2. [Low] .app not individually stapled (only the dmg)
See install-mechanics note. Offline first launch of the installed app isn't
staple-backed. Cheap to fix: `xcrun stapler staple jellytoast.app` before
packaging the dmg.

### 3. [Docs, trivial] "no warning dialog" wording
The §A checklist implies a stapled/notarized app opens with no dialog. Quarantined
downloads always show the one benign "downloaded from the Internet" confirm. Reword
to "no *blocking* Gatekeeper warning (unidentified-developer)".

---

## Not exercised (honest gaps — mostly orthogonal to this PR)
- **Offline download → disconnect → play** (row 9): involved, writes to cache; not driven.
- **Queue reorder + quit/relaunch restore** (row 4): populate + highlight verified; persistence not driven.
- **Smart-playlist UI create-flow** (row 10): resolve engine smoke-verified (193/193); dialog flow not driven.
- **Scrobbling link** (row 12): nothing linked on this box.
- **Tray/start-at-login toggle power-cycle** (row 16): toggles present; not flipped + rebooted.
- **Long library-title degrade** ("A +1" → "2 libraries", row 2): picker present; overflow not stress-driven. (Note: the unit test `test_view_dropdown_clamped_and_library_not_clipped_when_narrow` fails on the Linux CI runner under the PySide6 6.11 bump — font-metrics tolerance, passes on macOS.)
- **Intel-native caveat:** this box IS Intel, so the x86_64 slice is well-covered; the **arm64 slice ran only** via the fat binary's presence checks (lipo), not a native Apple-Silicon boot.
- **Wheel target:** `pipx` absent on this box; not installed. Wheel *metadata* verified — pyobjc (core/Cocoa/MediaPlayer/UserNotifications/ServiceManagement) declared for darwin, PySide6≥6.11, cryptography retained off-Windows. Runtime wheel launch not driven.
