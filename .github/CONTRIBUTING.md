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
dormant until you enable it in Settings (the visualizer also needs
`JT_VISUALIZER=1`), so bundling them costs nothing at runtime.

## Before you open a PR

The project gate (also enforced in CI) is simple and non-negotiable:

1. **`ruff check .` is clean.** Lint rules are `E, F, I, B` (declared in
   `pyproject.toml [tool.ruff.lint]`).
2. **The full suite is green:** `pytest -n auto -q`. Add tests for any
   behavior change — the suite is the safety net for a single maintainer.

There is also an advisory `mypy jellytoast/providers` and a `pip-audit` step
in CI; they don't block, but don't make them worse.

## Conventions (important — these are deliberate)

- **No autoformatter.** `ruff format` is intentionally *not* used; line
  length (`E501`) is off. Wrap by editorial judgment and match the
  surrounding code. Don't reflow files you're not changing.
- **Module-level lazy imports are allowed** (`E402` is off) where they
  break an import cycle or guard an optional dependency.
- **Everything talks through `PlayerBus`** (`jellytoast/player_state.py`) —
  Qt signals. UI emits *intents* (`queue_play_now`); the backend listens,
  acts, and emits *state* (`playback_started`). Don't wire UI directly to
  mpv or the queue.
- **I/O goes through `jellytoast.async_io`** (`run_async`, `get_qnam()`) —
  never raw `threading.Thread` for network/disk.
- **Qt thread affinity:** any code that creates/starts/stops a `QTimer` or
  mutates a `QObject` off the GUI thread must hop back via
  `QTimer.singleShot(0, app, fn)` / a queued signal, or Qt will crash.
- **Provider parity:** features must work identically on Jellyfin and
  Subsonic; per-provider differences live behind the
  `jellytoast/providers/base.py` abstraction, never inlined at call sites.
- **Categorical values are enums**, not bare strings (`CastType`,
  `DownloadState`, `RepeatMode`, `QueueKind` — all `str`-backed).
- **Branding is lowercase** — always "jellytoast", never "JellyToast".
- **Flat layout is intentional** (the single `jellytoast/` package at the
  repo root, app entry in `jellytoast/app.py`) so `python3 -m jellytoast`
  works from a checkout with no install.

## Architecture & docs

- `docs/SPEC.md` — what the app does today.
- `docs/decisions.md` — why certain choices were made.
- `docs/TODO.md` — the backlog (P0–P4).
- `docs/archive/code_audit_2026-06-01.md` — the most recent full engineering audit (archived).
- `docs/manual_test_plan.md` — by-hand / by-eye checks.

## Reporting bugs & requesting features

Use the GitHub issue templates. For security issues, **do not** open a
public issue — see [`SECURITY.md`](SECURITY.md).

By contributing you agree your contributions are licensed under the
project's **GPL-2.0-or-later** (see [`LICENSING.md`](../docs/LICENSING.md)).
