# Contributing to jellytoast

Thanks for your interest! jellytoast is a native PySide6 desktop music
client for Jellyfin and Subsonic/Navidrome. This guide covers how to get
set up and the conventions the codebase follows.

## Developer setup

```bash
git clone https://github.com/wolfgangwarehaus/jellytoast.git
cd jellytoast
pip install -e ".[dev]"        # ruff + pytest + pytest-xdist + pytest-randomly + pre-commit
pre-commit install             # ruff lint + import-sort on commit
pytest -n auto -q              # full suite, parallel (test order is randomized)
bash dev/run.sh                # launch with libmpv env vars set
```

The cast backends (DLNA / Sonos / Snapcast) and the FFT visualizer ship as
part of the standard install — there are no extras to remember. Each stays
dormant until you enable it in Settings (the visualizer runs only in the
visualizer now-playing mode), so bundling them costs nothing at runtime.

## Before you open a PR

The project gate (also enforced in CI) is simple and non-negotiable:

1. **`ruff check .` is clean.** Lint rules are `E, F, I, B` (declared in
   `pyproject.toml [tool.ruff.lint]`).
2. **The full suite is green:** `pytest -n auto -q`. Add tests for any
   behavior change — the suite is the safety net for a single maintainer.

There is also an advisory `mypy jellytoast/providers` and a `pip-audit` step
in CI; they don't block, but don't make them worse.

## Conventions (these are deliberate)

Most of these exist to dodge a specific footgun, so please stick to them:

**Style**

- **No autoformatter.** `ruff format` is intentionally off, and so is the
  line-length check (`E501`). Wrap by judgment, match the code around you,
  and don't reflow files you aren't already changing.
- **Lazy imports are fine.** A module-level import that sits lower in the
  file (`E402` is off) is allowed to break an import cycle or guard an
  optional dependency.
- **Branding is always lowercase** — "jellytoast", never "JellyToast".

**Architecture**

- **The UI and the backend never call each other directly.** They talk
  through one signal bus, `PlayerBus` (`jellytoast/player_state.py`): the UI
  fires intents, the backend acts and reports state back. Don't reach into
  mpv or the queue straight from the UI.
- **All network and disk I/O goes through `jellytoast.async_io`** (use
  `run_async` / `get_qnam()`) — never a raw `threading.Thread`.
- **Respect the Qt GUI thread.** Anything that creates or touches a
  `QTimer` or `QObject` from another thread must hop back onto the GUI
  thread (`QTimer.singleShot(0, app, fn)` or a queued signal), or Qt will
  crash.
- **Both backends behave identically.** A feature must work the same on
  Jellyfin and Subsonic; keep any provider-specific code behind
  `jellytoast/providers/base.py`, not inline at the call site.
- **Use enums, not magic strings** for categorical values — e.g.
  `CastType`, `RepeatMode` (all string-backed).
- **The flat layout is intentional.** The whole app is one `jellytoast/`
  package at the repo root (entry point `jellytoast/app.py`), so
  `python3 -m jellytoast` runs straight from a checkout, no install needed.

## Architecture & docs

- `docs/SPEC.md` — what the app does today.
- `docs/decisions.md` — why certain choices were made.
- `packaging/` — build recipes for every platform (AUR, `.deb`, the Windows installer, winget).

## Reporting bugs & requesting features

Use the GitHub issue templates. For security issues, **do not** open a
public issue — see [`SECURITY.md`](SECURITY.md).

By contributing you agree your contributions are licensed under the
project's **GPL-2.0-or-later** (see [`LICENSING.md`](../docs/LICENSING.md)).
