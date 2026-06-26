# Mac App Store runtime test — sandboxed build checklist

The build / sign / upload pipeline runs entirely from Linux CI (no Mac). The ONE
thing a Mac is needed for is **runtime-testing the sandboxed build** — the App
Sandbox can break things `altool --validate-app` never sees. Run this on each
rented Mac before relying on (or submitting) a build.

## Install + how to see the logs

1. App Store Connect → TestFlight → add yourself to an **Internal Testing** group
   and add the build → install via the **TestFlight** app on the Mac.
2. A GUI launch **discards stdout**, and a clean-exit failure leaves **no crash
   report** and **nothing in the unified log**. To read the app's mpv / Python
   errors, **quit the app**, then run the binary directly from Terminal:
   ```
   /Applications/jellytoast.app/Contents/MacOS/jellytoast
   ```
   Reproduce the issue; the traceback prints in the terminal.

## Checklist (sandbox-risky surfaces)

- [ ] **Launch** — window appears (single-instance gate must fail open: the
      sandbox blocks QSharedMemory's semaphore → it falls back to the socket)
- [ ] **Sign in** — Jellyfin demo `https://demo.jellyfin.org/stable`, user
      `demo`, no password (or your own server)
- [ ] **Playback** — play / pause / seek / volume / next-prev / gapless; the
      position counter advances and audio is audible
- [ ] **Sign-in persistence** — quit + relaunch → still signed in (keychain /
      AES-blob credential store under the sandbox)
- [ ] **Settings persistence** — change theme / EQ / audio output → quit +
      relaunch → it stuck (QSettings redirects into the container)
- [ ] **Offline download** — download a track → it lands in the container →
      plays back with the network off
- [ ] **Browse + search** — albums / artists / genres / search all load
- [ ] **Casting** — the cast menu opens and macOS shows the Local-Network prompt
      (network.server + NSBonjourServices). A full device test needs real
      Chromecast / AirPlay hardware on the LAN.

## Known sandbox issues already fixed (don't re-debug)

- **Single-instance gate** — QSharedMemory's POSIX-shm backing is unavailable
  under the sandbox (`QSystemSemaphore: permission denied`), so
  `SingleInstance.acquire()` fails open to the QLocalServer socket.
  (`jellytoast/single_instance.py`)
- **Playback `ytdl`** — the LGPL / no-Lua MAS libmpv has no `ytdl` hook, so
  `ytdl=False` raised "mpv option does not exist". `_open_mpv()` drops any
  unsupported option and retries. (`jellytoast/player_backend.py`)
- **Leftover keychain item** — a `jellytoast` login-keychain item from an
  un-sandboxed dev run makes the sandboxed app loop on a keychain-password
  prompt. Clear it with `security delete-generic-password -s/-l jellytoast`.
  A fresh user never hits it. (On a rented Mac the login-keychain password is
  the macOS account password, not the cloud-console one.)

## Build number

CFBundleVersion = `GITHUB_RUN_NUMBER` (CI's shallow checkout caps the git
commit-count at 1). Each CI run gets a unique, increasing build number, so
re-uploads of the same `0.1.3` marketing version are always accepted.

## Resume next rental

Certs / provisioning profile / API key all live in GitHub Actions secrets, so CI
is Mac-free. To resume: rent a Mac → install TestFlight → install the latest
build → run this checklist. Encrypted cert backups are at
`~/jellytoast-mac-backup/` on the Linux dev box (move them to a password
manager; they decrypt with the cert export password).
