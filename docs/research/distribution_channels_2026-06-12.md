# Distribution channels beyond the core four — research (2026-06-12)

> **Status: research / decision input — nothing here is shipped.**
> Commissioned on packaging day to answer: what would Snap Store,
> Microsoft Store, CachyOS repos, Linux Mint, and Steam Deck cost us, and
> which are worth it? Core four already in flight: Flathub, AUR, .deb on
> Releases, Windows installer + winget (see `packaging/`).

## TL;DR — friction-ranked

**Free wins (already paid for by the Flathub/AUR work):**
1. **Steam Deck** — Discover in Desktop Mode is preconfigured with Flathub;
   the day we're on Flathub, every Deck can install us. Zero extra work.
   (SteamOS rootfs is read-only → flatpak is THE path.) Deck audio is
   PipeWire; note the Deck always mixes system sounds, so the
   contested-bit-perfect toast will fire there — correct behavior.
2. **Linux Mint** — Software Manager ships Flathub-enabled (snap
   hard-blocked via APT). **One catch: since Mint 22, unverified flatpaks
   are hidden by default.** → Complete Flathub publisher verification
   during submission (minutes, via the GitHub org) or we're invisible to
   default Mint users. Our 22.04-built deb also installs on Mint 21/22
   directly (worth one container smoke test for the libmpv2 dep on Noble).
3. **CachyOS users** — covered by AUR; CachyOS tooling (paru/octopi)
   makes AUR first-class. Their official repos' value is x86-64-v3/v4
   rebuilds, which a pure-Python app gains nothing from; intake is
   discretionary forum requests. Not worth pursuing as a channel.

**Cheap (an hour or two):**
4. **chaotic-AUR** — the prebuilt-AUR binary repo many Arch/CachyOS/Garuda
   users enable. One `[Request]` issue on `chaotic-aur/packages` after our
   AUR package is live; their CI then auto-rebuilds from AUR forever (our
   only duty: keep AUR healthy, which we do anyway). Best
   effort-to-reach ratio of everything researched.

**Medium effort, real payoff — recommended post-v0.1.0:**
5. **Microsoft Store (MSIX route)** — landscape changed in our favor:
   - Individual dev registration is **FREE** since Sept 2025 (was ~$19);
     lightweight ID verification.
   - **The Store signs the MSIX** — no code-signing cert purchase, ever.
   - MusicBrainz Picard (GPL, Python+Qt, PyInstaller) is a line-for-line
     blueprint: `appxmanifest.xml.in` template + `makepri`/`makeappx`
     over the PyInstaller dist in stock GitHub Actions, `runFullTrust` +
     `unvirtualizedResources`.
   - Store-managed silent auto-updates (winget users must `winget
     upgrade` manually); no SmartScreen warning (our Inno exe is
     unsigned); a Store listing is also `winget install -s msstore`.
   - GPL apps are explicitly fine (2022 policy scare was reverted);
     Jellyfin's own client lives there; IARC age rating is a 5-min
     questionnaire; certification 24–72h.
   - **Our work items if/when:** (a) autostart inside MSIX needs a
     manifest `desktop:StartupTask` (registry Run keys don't work) — new
     branch in the autostart backend, detect packaged context via
     `GetCurrentPackageFullName`; (b) QSettings/HKCU + the AES-GCM blob
     in AppData get copy-on-write virtualized (settings die on
     uninstall) unless we exclude them Picard-style
     (`RegistryWriteVirtualization`/`FilesystemWriteVirtualization`
     disabled); python-keyring uses Credential Manager — NOT virtualized,
     fine as-is; (c) no loopback/network restrictions for full-trust —
     mDNS/SSDP/cast-proxy behave exactly like the Inno build; (d) libmpv
     is a non-issue (bundled in the payload).
   - Effort S–M (~a day cribbing Picard + the autostart branch).
     **The Win32-EXE submission route is a trap**: looks like "reuse the
     Inno exe" but requires a PURCHASED code-signing cert (must chain to
     MS Trusted Root) and leaves updates self-managed. MSIX is cheaper
     AND better. CI automation later via msstore-cli.

**Skip (with reasons, so we don't re-litigate):**
6. **Snap Store** — researched thoroughly; the verdict is skip-for-now:
   - kde-neon-6 extension is C++-only; the workable recipe is KDE's
     `kde-pyside6-core24-sdk` build-snap (Krita's pattern) +
     `stage-packages: libmpv2` + ffmpeg-2404 content snap (Haruna's
     pattern). Nobody has shipped a PySide6+libmpv snap — we'd pathfind.
   - **Every KWin differentiator dies under strict confinement**: the
     dbus interface is snap-to-snap only and KWin isn't a snap → no
     `org.kde.KWin` access → drag-repaint effect, keep-above rule,
     borderless SSD rule, kwinrc blur detection all degraded. Blur
     *request* might survive via the Wayland protocol; detection won't.
   - Three needed plugs don't auto-connect: `avahi-observe` (cast
     discovery), `audio-record` (visualizer tap),
     `password-manager-service` (keyring — auto-connect requests for
     this are habitually DENIED; our AES-GCM file fallback in
     $SNAP_USER_DATA saves logins, keyring becomes best-effort). So
     casting + visualizer silently degrade until users run
     `snap connect` by hand. Highest user friction of any channel.
   - Audience math: Mint blocks snapd; Fedora/Pop/Manjaro/SteamOS
     preinstall flatpak. The marginal reach is stock-Ubuntu-GNOME users
     who never add Flathub — who'd receive our most degraded build.
   - If Ubuntu demand materializes: name registration is free with ~2
     business days of manual review — register `jellytoast` early to
     hold the name, publish later. CI is mature (snapcore/action-build +
     action-publish) when we want it.
7. **Steam store proper** — $100/app Steam Direct fee (recoupable only
   past $1k revenue — sunk for a free app), and the accepted software
   categories (production tools: Animation, AV *Production*, Design,
   Player Tools…) don't cleanly admit a music *consumption* client.
   The OSS successes there (Blender, Krita, OBS) are production tools.
   Deck users get us via Flathub anyway.
8. **Fedora COPR / AppImage / Homebrew-on-Linux** — redundant once
   Flathub is live (Fedora's GNOME Software shows Flathub; AppImage
   duplicates the Flatpak with a worse story — Spotube even dropped
   theirs). **openSUSE OBS** is the one "park it" item: it can build
   rpms+debs with per-distro repos from one spec — revisit only if rpm
   users actually ask.

## Channel matrix (after everything above ships)

| Audience | Channel | State |
| --- | --- | --- |
| Arch / CachyOS / Garuda | AUR (+ chaotic-AUR binary) | AUR ready; chaotic = 1 issue post-AUR |
| Ubuntu | .deb on Releases; Flathub | built by release.yml / manifest authored |
| Mint | Flathub (verified!) + same .deb | verification = during submission |
| Fedora / openSUSE / others | Flathub | manifest authored |
| Steam Deck | Flathub via Discover | free win |
| Windows enthusiasts | winget + Inno + portable zip | authored, awaiting v0.1.0 |
| Windows mainstream | **Microsoft Store MSIX** | recommended next, post-v0.1.0 |
| Any distro, CLI-comfortable | pipx (PyPI) | upload at v0.1.0 |

## Sources

Full per-channel reports with citations live in the 2026-06-12 session
research (Snap: snapcraft docs, KDE snap guidelines, Krita/Haruna yamls,
interface docs; MS Store: Windows Dev Blog fee removal, MSIX
virtualization docs, Picard's appxmanifest + CI; channels: chaotic-aur
packages repo, Mint 22 release notes, CachyOS wiki/forum, Steamworks
onboarding docs).
