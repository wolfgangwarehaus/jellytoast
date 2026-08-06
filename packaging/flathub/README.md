# Flathub submission kit

Everything the actual Flathub submission PR needs lives in this
directory. It is deliberately separate from
`packaging/flatpak/io.github.wolfgangwarehaus.jellytoast.yml` (the
self-distributed CI bundle, which pip-installs over the network — that
manifest is **not** Flathub-eligible and stays as our own channel).

Contents:

| file | goes where |
|---|---|
| `io.github.wolfgangwarehaus.jellytoast.yml` | top level of the submission (later: the per-app repo) |
| `io.github.wolfgangwarehaus.jellytoast.metainfo.xml` | next to the manifest — temporary override, see below |
| `README.md` | this file — does NOT go into the submission |

Status of local validation (2026-07-20):

- YAML parses; `flatpak-builder-lint manifest` passes with exactly one
  known error, `finish-args-kwin-talk-name` (deliberate — exception
  needed, see below) and one warning (runtime 6.11 available; we stay on
  6.10 because that is what the CI bundle and io.qt.PySide.BaseApp were
  validated against — bump later in the per-app repo).
- `flatpak-builder-lint appstream` and `appstreamcli validate --pedantic`
  both pass on the metainfo.
- All 66 hash-pinned sources (64 Python wheels/sdists + libass + the
  v0.2.1 tarball) were downloaded and their sha256 verified.
- Architectures: **both x86_64 and aarch64** are shipped (Flathub's
  default). io.qt.PySide.BaseApp 6.10 exists for both. Every binary
  wheel is pinned for both arches except `miniaudio` (a pyatv dep),
  which has no linux-aarch64 wheel upstream and builds from its sdist on
  aarch64 (cffi is installed first to make that work). If the aarch64
  build fails on the buildbot for some other reason, the escape hatch is
  a `flathub.json` next to the manifest with
  `{"only-arches": ["x86_64"]}` — but try both first.

## Before opening the PR

1. **Build and run it locally, on both a KDE and (ideally) a GNOME
   session** — reviewers expect you to have done this:

   ```sh
   flatpak install -y flathub org.flatpak.Builder
   flatpak run org.flatpak.Builder --user --install --force-clean --ccache \
       --install-deps-from=flathub --repo=repo builddir \
       packaging/flathub/io.github.wolfgangwarehaus.jellytoast.yml
   flatpak run io.github.wolfgangwarehaus.jellytoast
   flatpak run --command=flatpak-builder-lint org.flatpak.Builder repo repo
   ```

   Things to exercise: https server login (proves the gnutls ffmpeg),
   blur honesty on KDE with the Blur effect toggled off, tray icon,
   MPRIS media keys, keyring-backed login persistence across restarts.

2. **Request the linter exception for `--talk-name=org.kde.KWin`.**
   Open a PR against
   `https://github.com/flathub/flatpak-builder-lint` editing
   `flatpak_builder_lint/staticfiles/exceptions.json`:

   ```json
   "io.github.wolfgangwarehaus.jellytoast": {
       "stable": {
           "finish-args-kwin-talk-name": "<your reason>"
       }
   }
   ```

   The substance of the reason (write it in your own words — Flathub
   explicitly rejects LLM-written exception requests): read-only D-Bus
   queries (`isEffectLoaded` / compositing-active) against KWin's
   Effects interface, used only to verify the Blur desktop effect is
   actually enabled before painting transparent surfaces; KWin's Wayland
   blur protocol stays advertised even when the effect is off, so
   without the check the app paints full-transparency glass over an
   unblurred desktop (our issue #229 / Steam Deck 0.2.0 reports). No
   methods with side effects are called. Criteria per their docs:
   "Granted on sufficient explanation being provided." This can happen
   in parallel with the submission PR — reviewers will see the linter
   error and the pending exception.

3. `--filesystem=~/.config/autostart:create` is absent — and **no longer
   needs to be**. The linter hard-rejects it
   (`finish-args-autostart-filesystem-access`), which used to leave
   launch-at-login a silent no-op in the Flathub build. Fixed upstream:
   launch-at-login now goes through the XDG Background portal
   (`org.freedesktop.portal.Background.RequestBackground` with
   `autostart: true`, see `jellytoast/autostart/_portal.py`). The portal
   writes the host-side autostart entry itself, so it needs no filesystem
   permission and nothing has to be re-granted — the toggle works on
   Flathub. The old `.desktop` writer stays only as the fallback for
   no-portal sessions outside a sandbox.

## Submission steps

1. Fork `https://github.com/flathub/flathub` and clone the `new-pr`
   branch (NOT master):

   ```sh
   git clone --branch=new-pr git@github.com:<you>/flathub.git flathub-submission
   cd flathub-submission
   git checkout -b jellytoast new-pr
   ```

2. Copy in, at the **top level** of that checkout:
   - `io.github.wolfgangwarehaus.jellytoast.yml`
   - `io.github.wolfgangwarehaus.jellytoast.metainfo.xml`
     (the manifest references it as a `type: file` source — it overrides
     the stale copy inside the v0.2.1 tarball, whose screenshot URLs
     were replaced after the tag; four of them now 404)

   No `flathub.json` is needed (we build the default arches).

3. Commit, push, open a PR **against the `new-pr` branch** of
   `flathub/flathub`. Keep the PR template and fill it in — PRs that
   delete the template, ignore the guidelines, or read as
   AI-extruded text get closed without review. Mention in the PR body:
   the pending linter exception for `org.kde.KWin` (link your
   flatpak-builder-lint PR) and why (blur honesty check, read-only).

4. The build bot (`flathubbot`) will run test builds for x86_64 and
   aarch64 and the linter on every push. Comment `bot, build` to
   re-trigger if needed. Fix what it flags; the KWin error is expected
   until the exception lands.

5. Review is by volunteers — no SLA; expect anywhere from days to a few
   weeks. After approval and merge, a per-app repo
   `github.com/flathub/io.github.wolfgangwarehaus.jellytoast` is
   created, you get invited as collaborator (**accept within one week,
   2FA required on your GitHub account**), and the first official build
   publishes within an hour or two of merge.

6. After publication, mark the app **verified** on Flathub: log in at
   flathub.org, claim the app via the GitHub-account verification path
   (the `io.github.wolfgangwarehaus.*` id verifies against your GitHub
   user).

## Maintenance after acceptance

- All future changes happen as PRs to the per-app repo
  `flathub/io.github.wolfgangwarehaus.jellytoast` (default branch
  `master`; merges auto-build and publish to stable). Test builds run
  per-PR, same bot.

- **Release flow:** `x-checker-data` on the jellytoast source points
  flatpak-external-data-checker at the GitHub latest-release API. When
  you cut a tag, the checker opens a PR on the per-app repo bumping the
  tarball URL + sha256. You review, check whether `pyproject`
  dependencies changed, and merge. It bumps ONLY the app tarball —
  Python deps are pinned and need manual regeneration when they change
  (below).

- **Drop the metainfo override** once you cut a release (>= 0.2.2)
  whose in-tree `packaging/io.github.wolfgangwarehaus.jellytoast.metainfo.xml`
  has the fixed screenshot list: delete the `type: file` source +
  `metainfo-override.xml` install line and restore the install from
  `packaging/…` inside the tarball.

- **Metainfo releases:** `cut_release.sh` stamps each release into the
  canonical metainfo in this repo; since the Flathub build installs the
  metainfo from the tarball (post-override), release notes flow
  automatically once the override is dropped.

## Regenerating the pinned Python sources

Do this whenever `[project].dependencies` in `pyproject.toml` changes
(the manifest's drift guard fails the build if you forget). The pin set
was produced with the **runtime's own Python 3.13** so the resolve
matches Flathub's buildbot:

```sh
# 1. Linux dep list minus PySide6 (BaseApp provides it — SONAME
#    collision invariant) — same derivation the CI manifest uses:
python3 - <<'PY'
import tomllib
deps = tomllib.load(open("pyproject.toml", "rb"))["project"]["dependencies"]
keep = [d for d in deps if not d.lower().replace("_", "-").startswith("pyside6")]
open("/tmp/flatpak-deps.txt", "w").write("\n".join(keep) + "\n")
PY

# 2. Resolve pins with the runtime's pip (Python 3.13):
flatpak run --command=sh --share=network --filesystem=/tmp org.kde.Sdk//6.10 -c '
  cd /tmp && python3 -m pip install --dry-run --ignore-installed \
    --report resolve.json -r flatpak-deps.txt'
python3 -c '
import json
r = json.load(open("/tmp/resolve.json"))
pins = sorted(i["metadata"]["name"] + "==" + i["metadata"]["version"]
              for i in r["install"])
open("/tmp/requirements-pinned.txt", "w").write("\n".join(pins) + "\n")'

# 3. Wheel URLs + sha256 for both arches (pip install req2flatpak):
req2flatpak -r /tmp/requirements-pinned.txt -t 313-x86_64 313-aarch64 \
    --outfile /tmp/python-deps.json
```

Then splice `/tmp/python-deps.json` back into the manifest's
`python3-dependencies` module, keeping the local conventions:

- **drop numpy** (the BaseApp ships it; ours would be shadowed, and
  `BASEAPP_DISABLE_NUMPY` must stay unset or the cleanup script would
  uninstall the app's copy too);
- keep the **two-stage pip**: `cffi pycparser` first, then everything
  else (miniaudio's aarch64 sdist build imports cffi under
  `--no-build-isolation`);
- `only-arches` on each arch-specific wheel, and on the miniaudio sdist
  (`aarch64` — x86_64 uses the wheel);
- re-verify every URL + sha256 downloads clean before pushing.

Runtime bumps (e.g. 6.10 → 6.11): check the BaseApp branch exists for
the new version, re-check the runtime's Python minor (re-pin with the
new `-t` tags if it moved past 3.13), rebuild, retest blur + https.

**Why this manifest is still on 6.10 (2026-08, issue #237).** Platform
*and* BaseApp 6.11 now exist for both arches, and the SELF-DISTRIBUTED
manifest (`packaging/flatpak/`) has moved to it. This one deliberately
has not: 22 of its pinned wheels carry `cp313` tags resolved against the
6.10 runtime's Python, so the bump is not a two-line edit — every pin has
to be re-resolved, and the Python minor in 6.11 can't be confirmed
without pulling the ~1.5 GB SDK. Submitting on the runtime we actually
built, linted and hash-verified is the lower-risk path (Flathub routinely
accepts current-1 runtimes). Do the bump AFTER acceptance, as a PR to the
per-app repo, where the buildbot builds both arches for you and a
mis-resolved pin fails visibly instead of silently shipping.
