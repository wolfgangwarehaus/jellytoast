# jellytoast — App Store Connect Privacy ("Nutrition Label") Answers

Written to `/home/august/Projects/jellytoast/submission/PRIVACY-LABELS.md`.

## Bottom line for the developer

**The whole label resolves to "Data Not Collected."** In App Store Connect → App Privacy, answer the very first question — *"Do you or your third-party partners collect data from this app?"* — with **"No, we do not collect data from this app."** That ends the questionnaire (no per-category / Linked / Tracking screens appear) and the product page shows **Data Not Collected**.

## Why — the load-bearing definition

Apple defines **"Collect"** as *transmitting data off the device in a way that allows you and/or your third-party partners to access it* longer than needed to service a real-time request. Two facts make every category resolve to *Not Collected*:

1. **Locally-stored credentials are NOT "collected."** The server URL + login live in the macOS **Keychain** plus the encrypted dual-store blob, read on-device only, never sent anywhere the **developer** can access. Storage ≠ collection. This is the crux question the task asked about, and the answer is clean.
2. **"Collect" means access by *you* (the developer) or *your* partners — not by the user's own server.** jellytoast talks only to the **user's own** Jellyfin/Subsonic/Navidrome server, and the developer runs **no** backend, analytics, telemetry, crash reporter, ad network, or third-party SDK. Sending the user's creds to the user's own server is not developer collection.

## Per-category mapping (all 14 Apple data types)

Every category is **Collected: No**; "Linked to user" and "Used for tracking" are therefore **N/A**, and **Used for Tracking is NO everywhere** regardless (no ad/broker integration exists):

Contact Info · Health & Fitness · Financial Info · Location · Sensitive Info · Contacts · User Content · Browsing History · Search History · Identifiers · Purchases · Usage Data · Diagnostics · Other Data — **all Not Collected.** The doc has a full table with a one-line justification per row (e.g. Usage Data: no analytics SDK; Diagnostics: no crash reporter; Identifiers: no IDFA/device-ID, the server account ID is the user's not the developer's).

## Optional outbound calls — still Not Collected (documented for review, not on the label)

Off-by-default **scrobbling** (to the user's own ListenBrainz/Last.fm), incidental **internet-radio cover-art** lookup (MusicBrainz/Cover Art Archive), and LAN **casting** all reach only the user-chosen service/device, never the developer. None is "collected by you," and each independently meets Apple's **optional-disclosure** criteria. The doc routes these into the **App Review notes** (so a reviewer who sees outbound traffic isn't surprised) rather than the privacy label.

## Also included in the file

- The exact form-flow to click, plus a **privacy-policy URL field** reminder (required even for no-collection apps) pointed at the existing `docs/PRIVACY.md`.
- A pre-submission **"no-collection" audit checklist** (no analytics/crash/ad SDK, no developer backend, empty `NSPrivacyCollectedDataTypes` in any future `PrivacyInfo.xcprivacy`) so the label stays true build-to-build.
- A ready-to-paste **App Review notes** paragraph covering on-device storage, no-backend, optional third parties, demo creds, and the "independent / not affiliated" disclaimer.
- Sourced from Apple's App Privacy Details, User Privacy and Data Use, App Store Connect Help, and Privacy Definitions pages.

## Relevant files

- `/home/august/Projects/jellytoast/submission/PRIVACY-LABELS.md` (new — the deliverable)
- `/home/august/Projects/jellytoast/docs/PRIVACY.md` (existing public policy this label is consistent with)
- `/home/august/Projects/jellytoast/packaging/macos/mas/scan_symbols.sh` (the related privacy-manifest / symbol gate referenced in the audit)
