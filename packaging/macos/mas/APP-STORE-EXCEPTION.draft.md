# App Store distribution exception — DRAFT (not yet adopted)

> **This is a draft for august to review and decide on — it is NOT active.**
> Adopting it is a legal decision only the copyright holder can make. Nothing
> here changes jellytoast's license until you deliberately move this text into
> the repo root (e.g. append to `LICENSING.md` / add a `LICENSE-EXCEPTION`) and
> reference it from `README.md`.

## Why this exists

Apple's App Store wraps every download in the Licensed Application EULA
(device-count limits, mandatory ToS, FairPlay DRM) — exactly the "further
restrictions" that GPLv2 §6 / GPLv3 §10 forbid a *distributor* from adding. A
GPL app therefore can't be App-Store-distributed by an ordinary licensee.

**You don't strictly need this exception**, because you are the **sole
copyright holder** of jellytoast's first-party code: a rights-holder is not
bound by their own license grant (FSF GPL FAQ, *DeveloperViolate* /
*ReleaseUnderGPLAndNF*), so you may ship the same code on the App Store under
Apple's terms while keeping the public GPL-2.0-or-later release. A private
authorization memo (see below) is legally sufficient for the first-party code.

This **public §7 exception is recommended anyway** because it (a) removes all
ambiguity, (b) lets *anyone* (e.g. a future contributor or a downstream repackager)
App-Store-distribute it, and (c) documents intent. It rides on GPLv3 via the
project's `-or-later`, so the `GPL-2.0-or-later` status stays unchanged.

## Option A — public §7 additional permission (recommended)

Add to `LICENSING.md` and a top-of-tree notice:

> **App Store distribution exception.** As an additional permission under
> section 7 of the GNU General Public License version 3, you are permitted to
> convey the Program (and works based on it) through an application store or
> distribution channel, even if that store imposes terms or conditions that are
> incompatible with the GNU General Public License, provided that the complete
> corresponding source code of the conveyed work remains available to all
> recipients under the GNU General Public License (version 2 or later), with or
> without this additional permission, through a channel that does not impose
> those incompatible terms.

*(Adapted from the wger-project AGPL App Store exception; widely used pattern.)*

## Option B — private authorization memo (minimum, do this regardless)

Keep a dated, signed record (does not go in the public repo):

> I, august (augustvontrips@gmail.com), am the sole copyright holder of all
> first-party jellytoast source code. I authorize the distribution of this work
> via the Apple App Store and other application stores under those stores'
> terms, in addition to — and without affecting — the public release under
> GPL-2.0-or-later. Dated 2026-__-__.

## The part this does NOT solve — bundled third-party code

This exception covers **your** code only. It cannot relieve third-party
libraries you don't own. The bundle must therefore use **LGPL-only** native
media libs:

- **libmpv**: build with `-Dgpl=false` (mpv defaults to **GPL** — a plain
  `brew install mpv` is GPL and would re-introduce an unfixable conflict).
- **FFmpeg**: build **without** `--enable-gpl` / `--enable-nonfree` (LGPL is the
  default). All audio decoders jellytoast needs (FLAC/ALAC/MP3/AAC/Opus/Vorbis)
  are LGPL; only GPL *encoders*/filters (x264 etc.) are excluded — irrelevant to
  a player.
- **Qt/PySide6 (LGPLv3)**: fine on the **Mac** App Store (a `.app` is
  user-modifiable, satisfying the anti-tivoization/relink clause) — but **not**
  iOS. Ship Qt as replaceable dylibs and meet the LGPLv3 relink obligation.

See `packaging/macos/MAS_SESSION.md` for the full go/no-go and the build work.
