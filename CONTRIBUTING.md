# Contributing to jellytoast

Thanks for your interest! jellytoast is a native PySide6 desktop music
client for Jellyfin and Subsonic/Navidrome. This guide covers how to get
set up and the conventions the codebase follows.

## Developer setup

```bash
git clone https://github.com/augustvontrips66/jellytoast.git
cd jellytoast
pip install -e ".[dev]"        # ruff + pytest + pytest-xdist + pytest-randomly + pre-commit
pre-commit install             # ruff lint + import-sort on commit
pytest -n auto -q              # full suite, parallel (test order is randomized)
bash dev/run.sh                # launch with libmpv env vars set
```

For the optional cast/visualizer backends, install the extras you need
(`.[dlna,sonos,snapcast,visualizer]`) — see the README's "Optional extras".

## Before you open a PR

The project gate (also enforced in CI) is simple and non-negotiable:

1. **`ruff check .` is clean.** Lint rules are `E, F, I, B` (declared in
   `pyproject.toml [tool.ruff.lint]`).
2. **The full suite is green:** `pytest -n auto -q`. Add tests for any
   behavior change — the suite is the safety net for a single maintainer.

There is also an advisory `mypy modules/providers` and a `pip-audit` step
in CI; they don't block, but don't make them worse.

## Conventions (important — these are deliberate)

- **No autoformatter.** `ruff format` is intentionally *not* used; line
  length (`E501`) is off. Wrap by editorial judgment and match the
  surrounding code. Don't reflow files you're not changing.
- **Module-level lazy imports are allowed** (`E402` is off) where they
  break an import cycle or guard an optional dependency.
- **Everything talks through `PlayerBus`** (`modules/player_state.py`) —
  Qt signals. UI emits *intents* (`queue_play_now`); the backend listens,
  acts, and emits *state* (`playback_started`). Don't wire UI directly to
  mpv or the queue.
- **I/O goes through `modules.async_io`** (`run_async`, `get_qnam()`) —
  never raw `threading.Thread` for network/disk.
- **Qt thread affinity:** any code that creates/starts/stops a `QTimer` or
  mutates a `QObject` off the GUI thread must hop back via
  `QTimer.singleShot(0, app, fn)` / a queued signal, or Qt will crash.
- **Provider parity:** features must work identically on Jellyfin and
  Subsonic; per-provider differences live behind the
  `modules/providers/base.py` abstraction, never inlined at call sites.
- **Categorical values are enums**, not bare strings (`CastType`,
  `DownloadState`, `RepeatMode`, `QueueKind` — all `str`-backed).
- **Branding is lowercase** — always "jellytoast", never "JellyToast".
- **Flat layout is intentional** (`jellytoast.py` + `modules/` at the
  repo root) so `python jellytoast.py` keeps working.

## Architecture & docs

- `docs/SPEC.md` — what the app does today.
- `docs/decisions.md` — why certain choices were made.
- `docs/TODO.md` — the backlog (P0–P4).
- `docs/code_audit_2026-06-01.md` — the standing engineering audit.
- `docs/manual_test_plan.md` — by-hand / by-eye checks.

## Reporting bugs & requesting features

Use the GitHub issue templates. For security issues, **do not** open a
public issue — see [`SECURITY.md`](SECURITY.md).

By contributing you agree your contributions are licensed under the
project's **GPL-2.0-or-later** (see [`LICENSING.md`](LICENSING.md)).
