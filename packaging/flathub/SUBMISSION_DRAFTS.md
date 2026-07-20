# Submission drafts — REWRITE IN YOUR OWN VOICE BEFORE POSTING

Working drafts for the two prose pieces of the Flathub submission. This
file does NOT go into the submission (same as README.md).

⚠️ Flathub closes PRs that read as AI-written, and explicitly rejects
LLM-written linter-exception requests. Treat everything below as raw
factual material: the facts are verified, the sentences are yours to
rewrite. Shorten, roughen, reorder — make it read like you.

---

## 1. The submission PR (against `flathub/flathub`, base branch `new-pr`)

**Title:** `Add io.github.wolfgangwarehaus.jellytoast`

**Body** (this is their template, filled — keep the template structure,
tick the boxes, replace the description/video slots):

```markdown
### Please confirm your submission meets all the criteria

- [x] Please describe the application briefly.
  jellytoast is a native desktop music player for self-hosted
  Jellyfin, Navidrome, and Subsonic servers. PySide6/Qt 6 UI with
  bit-perfect mpv playback, Chromecast/AirPlay 2/DLNA/Sonos casting,
  synced lyrics, an audio visualizer, offline downloads, smart
  playlists, MPRIS integration, and a floating mini player.
  GPL-2.0-or-later. I've been self-distributing a flatpak bundle from
  GitHub releases since 0.1.x (also tested on Steam Deck); this
  submission is the proper Flathub build — all Python dependencies are
  declared as hash-pinned sources (no network at build), and it uses
  the io.qt.PySide.BaseApp on org.kde.Platform 6.10.

- [ ] Please attach a video showcasing the application on Linux using
  the Flatpak. < RECORD THIS: launch from the Flatpak (not a dev
  checkout), sign in — the "Try a demo" button works on camera without
  exposing your server — browse the library, play a track, open the
  mini player. 30–60s screen capture is plenty. >

- [x] The Flatpak ID follows all the rules listed in the
  [Application ID requirements][appid].

- [x] I have read and followed all the [Submission requirements][reqs]
  and the [Submission guide][reqs2] and I agree to them.

- [x] I am an author/developer/upstream contributor to the project.

One note for reviewers: `flatpak-builder-lint` flags
`finish-args-kwin-talk-name` — that's expected. The app makes read-only
queries against KWin's Effects interface to check whether the Blur
desktop effect is actually enabled before it paints translucent
surfaces (KWin advertises the Wayland blur protocol even when the
effect is off, which left users with see-through windows). I've
requested an exception here: <LINK TO YOUR flatpak-builder-lint PR>.
```

Facts behind the checkboxes, so you can vouch for them honestly:
- App id: `io.github.wolfgangwarehaus.jellytoast` matches the
  io.github.<user>.<app> convention and your GitHub account — passes the
  id requirements and enables the GitHub-based verification later.
- Requirements: the kit was built against docs.flathub.org requirements
  (no build-time network, hash-pinned sources, validated AppStream
  metainfo with OARS + releases + screenshots, both default arches).
  BUT the checkbox is *your* attestation — skim the two linked pages
  once before ticking.

---

## 2. The linter-exception request (PR to `flathub/flatpak-builder-lint`)

Edit `flatpak_builder_lint/staticfiles/exceptions.json`, add:

```json
"io.github.wolfgangwarehaus.jellytoast": {
    "stable": {
        "finish-args-kwin-talk-name": "<one-line version of the reason>"
    }
}
```

**PR title:** `Add exception for io.github.wolfgangwarehaus.jellytoast (org.kde.KWin talk-name)`

**Draft reason — REWRITE THIS ONE MOST; they reject LLM-written
requests.** The verified facts to build your own paragraph from:

- The app's "frosted glass" theme paints translucent window surfaces
  that depend on compositor blur behind them.
- KWin keeps the Wayland blur protocol advertised even when the user
  has disabled the Blur desktop effect — so protocol presence alone is
  a lie. Painting glass over an unblurred desktop makes the window
  see-through (real user reports: your issue #229, and the Steam Deck
  0.2.0 field reports).
- The talk-name is used for read-only introspection only:
  `org.kde.kwin.Effects.isEffectLoaded("blur")` and a
  compositing-active check. No methods with side effects are called —
  nothing is toggled, configured, or written.
- When the check says no real blur, the app falls back to a
  near-opaque body instead of transparency ("blur honesty").
- Without the exception the app still works — it just can't tell a
  disabled Blur effect from an enabled one on KDE, and KDE users with
  blur off get see-through windows.

A too-clean version you should rough up / personalize:

> jellytoast draws translucent "frosted" windows that only look right
> with compositor blur behind them. On KWin the Wayland blur protocol
> stays advertised even when the user has the Blur effect turned off,
> so the app asks KWin directly (read-only:
> Effects.isEffectLoaded + compositing state) and falls back to an
> opaque body when there's no real blur. Nothing is written or toggled
> over this interface. Got this wrong in an early release and shipped
> see-through windows to KDE/Steam Deck users with blur disabled
> (wolfgangwarehaus/jellytoast#229) — the check is the fix.

---

## Order of operations

1. Record the video, do the local org.flatpak.Builder build + run test
   (README.md § "Before opening the PR").
2. Open the exception PR first (or same day) — link it from the
   submission PR body.
3. Open the submission PR against `new-pr` with the filled template.
