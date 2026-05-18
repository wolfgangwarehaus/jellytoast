# Cross-platform backend dispatch standardization

Status: research / spec
Author: research agent (2026-05-18)
Audience: future autonomous refactor pass; reviewable by august

## 1. Goal & non-goals

**Goal.** Pick one dispatch shape for the four `modules/<feature>/` backend
packages (`autostart`, `media_controls`, `keep_above`, `notifications`) and
spec the refactor so a code agent can execute it without making
architectural judgement calls. Unification buys:

- **Testability.** Every test file today reimplements the same
  `importlib.reload` choreography because dispatch happens at module-import
  time. A function-level dispatch with a documented reset hook removes that.
- **Contributor onboarding.** Four packages, four shapes is an unforced
  inconsistency. New backends (Windows SMTC, macOS NowPlaying, etc.) should
  be slot-fillable from a template.
- **Auditability.** A grep for `_select_backend` finds every platform
  decision point. Today the patterns are too divergent to grep usefully.

**Non-goals.** No public-API changes (`is_supported`, `enable`,
`MediaControlsService`, `install_mini_player_rule`, `notify`,
`MINI_PLAYER_WINDOW_TITLE` stay verbatim). No new Windows / macOS
implementations (see `[[user-hardware]]` — untestable Apple/Windows code is
out of scope). No change to `modules/platform_compat.py`; it already
exposes everything the unified shape needs.

## 2. Inventory: what each package does today

See `[[architecture-cross-platform]]` for the design intent.

| Package | Dispatch site | Gate input | Public binding | Module-level state |
|---|---|---|---|---|
| `autostart` | import-time `if/else` reassigns `_backend` | `IS_LINUX` constant | function wrappers | `_backend` (module) |
| `media_controls` | import-time `if/else` with `try/except` around dbus_next | `IS_LINUX` + `import` success | class alias `MediaControlsService = _Backend` | `_Backend` (class) |
| `keep_above` | import-time `if/else` | runtime call `is_kde_wayland()` | function wrappers + re-exported constant | `_backend` (module), `MINI_PLAYER_WINDOW_TITLE` |
| `notifications` | function-level memoized `_select_backend()` | `sys.platform.startswith("linux")` | function wrappers | `_backend: ModuleType \| None` (module-private cache) |

Concrete file:line cites:

- `modules/autostart/__init__.py:17-22` — top-level `if IS_LINUX: from ... import _linux as _backend`. No memoization needed because the gate is itself a constant.
- `modules/media_controls/__init__.py:27-39` — `IS_LINUX` plus a `try/except Exception` around `from modules.media_controls._mpris import MprisService as _Backend`. Line 43 aliases the class: `MediaControlsService = _Backend`.
- `modules/keep_above/__init__.py:24-38` — imports `is_kde_wayland` from `platform_compat`, calls it at module-load time, then `from modules.keep_above import _kwin as _backend`. The constant `MINI_PLAYER_WINDOW_TITLE = "jellytoast Mini Player"` lives on the package and is imported *back* by `_kwin.py:27`.
- `modules/notifications/__init__.py` (branch `auto/notifications-backend`, commit `21bd63b`) — `_select_backend()` function inspects `sys.platform.startswith("linux")` once, memoizes into module-level `_backend: ModuleType | None`. Each public function (`is_supported`, `notify`) calls `_select_backend()` first.

The four packages all share the `_<impl>.py` / `_unsupported.py` naming
convention from `[[architecture-cross-platform]]` and all keep their
`_unsupported.py` total (every public function returns False / no-op). That
part is already uniform.

## 3. Recommended unified shape

**Adopt the `notifications` shape, with two small additions.** A
function-level memoized `_select_backend()`, an `is_active()` hook on each
backend module, and the option to expose package-level constants from
`_constants.py`.

### Public API surface (`__init__.py` exports)

Each package's `__init__.py` exports exactly:

- Its existing public functions / classes (unchanged contract per package).
- A documented `_select_backend() -> ModuleType` function. The leading
  underscore signals "module-internal but stable hook for tests".
- A documented `_reset_backend_cache() -> None` function. Tests call this in
  place of `importlib.reload(...)`.
- Any package-level constants (e.g. `MINI_PLAYER_WINDOW_TITLE`).

For `media_controls`, where the public binding is a *class*, the wrapper
becomes a thin module-level function:

```python
def MediaControlsService(*args, **kwargs):
    return _select_backend().Service(*args, **kwargs)
```

Backends export a class named `Service` (uniformly). The public name
`MediaControlsService` is a factory function, not a class alias. This is the
one place the unified shape forces a tiny call-site-invisible change — every
existing call site already uses `MediaControlsService(...)` as a constructor
call, which works identically when the name is a function returning an
instance.

### Internal dispatch — exact signature

```python
_backend: ModuleType | None = None

def _select_backend() -> ModuleType:
    global _backend
    if _backend is not None:
        return _backend
    _backend = _pick_backend()
    return _backend

def _pick_backend() -> ModuleType:
    # Per-package body. See sections 3.x.
    ...

def _reset_backend_cache() -> None:
    """Test hook. Drop the memoized backend so the next call to
    _select_backend() re-evaluates `_pick_backend()` against current
    monkeypatches."""
    global _backend
    _backend = None
```

`_pick_backend()` is the per-package gate body. It's the only piece that
varies, and its contents read like a sentence:

- `autostart._pick_backend`: `if IS_LINUX: from . import _linux as b; return b` else unsupported.
- `notifications._pick_backend`: same as today.
- `media_controls._pick_backend`: `if IS_LINUX and _can_import_dbus_next(): from . import _mpris as b; return b` else unsupported. `_can_import_dbus_next()` does the `try/import/except` once.
- `keep_above._pick_backend`: `if is_kde_wayland(): from . import _kwin as b; return b` else unsupported.

### Platform-gate inputs

`_pick_backend()` is allowed to call any helper in `modules.platform_compat`.
This is the composable hook. `keep_above` continues to call
`is_kde_wayland()`; everyone else uses `IS_LINUX`. We do **not** add a
`runtime_probe: Callable[[], bool]` parameter to `_select_backend()` —
that's option (a) in section 6 and we reject it.

### Unsupported-fallback signature

Each `_unsupported.py` stays as-is: same module-level functions / class as
the active backend, every function returns `False` / no-op. The unified
shape does not change unsupported fallbacks.

### Testability hook

Tests replace the `importlib.reload` choreography (visible in
`tests/test_autostart.py`, `tests/test_media_controls.py`,
`tests/test_keep_above.py` on branch `auto/backend-package-tests`) with:

```python
def _reload(monkeypatch):
    import modules.autostart as m
    m._reset_backend_cache()
    return m
```

`monkeypatch` of `platform_compat.IS_LINUX` (or `is_kde_wayland`) then takes
effect on the next `_select_backend()` call. No `sys.modules.pop` dance, no
`importlib.import_module`.

`test_notifications.py` (branch `auto/notifications-backend`) already
shadows this pattern via `monkeypatch.setattr(notifications,
"_select_backend", lambda: _unsupported)`. That direct-attr patch still
works on the unified shape, so the existing notifications test contract is
preserved verbatim.

## 4. Per-package migration plan

| Package | Cost | Why |
|---|---|---|
| `notifications` | **S** | Already on the target shape. Add `_reset_backend_cache()` and `_pick_backend()` split-out. ~10 lines net. |
| `autostart` | **S** | Pure function-wrapper package, no class binding. Lift the `if IS_LINUX` from module-level to `_pick_backend()`, add the cache + reset hook. ~20 lines net. |
| `media_controls` | **M** | The `MediaControlsService = _Backend` class alias becomes a factory function. Backends rename `MprisService` → `Service` and `UnsupportedMediaControlsService` → `Service` to match. Call sites are unchanged (still `MediaControlsService(parent)`). The dbus_next `try/except` moves into `_can_import_dbus_next()`. The MPRIS-thread/state tests in `test_media_controls.py` (`MprisPlayer`, `update_volume`, etc.) import the class by name from `modules.media_controls._mpris`; those imports survive unchanged. |
| `keep_above` | **M** | Runtime-gate `is_kde_wayland()` move from module-load to `_pick_backend()` is mechanical. The wrinkle: `MINI_PLAYER_WINDOW_TITLE` is imported back from the package `__init__` by `_kwin.py:27`. Move the constant into `modules/keep_above/_constants.py` and have both `__init__.py` (for re-export) and `_kwin.py` import from there. Breaks the inner circular shape and survives a deferred backend import. |

Order of refactor: `notifications` → `autostart` → `media_controls` →
`keep_above`. See section 7.

## 5. Test-contract preservation

The tests on `auto/backend-package-tests` and `auto/notifications-backend`
are the public contract. Each refactor must keep these hook points working.
Per file:

- **`tests/test_autostart.py`** patches `modules.platform_compat.IS_LINUX`,
  then reloads. After refactor: patches the same attr, calls
  `autostart._reset_backend_cache()`. All assertions
  (`autostart._backend.__name__ == "modules.autostart._linux"`) survive
  because `_backend` is still the module-level memoized value. Specific
  filesystem mocks (`_AUTOSTART_DIR`, `_AUTOSTART_FILE`, `_SOURCE_DESKTOP`
  on `modules.autostart._linux`) are untouched.

- **`tests/test_media_controls.py`** patches `IS_LINUX` and (in one test)
  injects exploding `dbus_next` modules into `sys.modules`. After refactor:
  same pattern, but `_can_import_dbus_next()` must do the `import` itself
  (not rely on a prior import) so the sabotaged `sys.modules` entry is
  hit. The `MprisPlayer` direct-import tests (`update_volume`,
  `update_status`, `update_can_next_prev`) survive verbatim — they import
  `modules.media_controls._mpris.MprisPlayer` by full path. The
  `MediaControlsService.__name__ == "MprisService"` assertion **breaks**;
  swap to `_select_backend().Service.__name__ == "Service"` or
  `_select_backend().__name__.endswith("_mpris")`. Flag this as the one
  forced test edit.

- **`tests/test_keep_above.py`** patches `platform_compat.is_kde_wayland`
  with `lambda: True/False`. After refactor: identical patch, then
  `keep_above._reset_backend_cache()`. The
  `keep_above._backend.__name__ == "modules.keep_above._kwin"` assertion
  survives.

- **`tests/test_notifications.py`** already uses
  `monkeypatch.setattr(notifications, "_select_backend", ...)` and the
  `_reload_notifications()` autouse fixture. After refactor: keep the autouse
  fixture (it survives), but the inner module-level `sys.modules.pop` dance
  can collapse into `m._reset_backend_cache()`. The
  `test_select_backend_memoizes` test continues to validate the cache.

One test edit forced: the `MprisService.__name__` assertion. Everything else
is additive or no-change.

## 6. The runtime-gate problem (keep_above)

`keep_above`'s gate is not `sys.platform` — it's "KDE Plasma running on
Wayland" (`is_kde_wayland() = is_kde_desktop() and will_be_wayland()`).
GNOME-Wayland, Sway, KDE-X11 all need the unsupported backend. Options:

- **(a) Parameterize `_select_backend(runtime_probe=...)`.** Rejected:
  couples the dispatcher to per-package gate semantics; the probe needs
  different inputs per package. Generalizing it just rebuilds
  `_pick_backend()` under another name.
- **(b) Each backend defines `is_active() -> bool`.** Backends self-report
  fitness; `_pick_backend()` picks the first True. Rejected: pushes the
  gate from one place (package) to many (backends), forces loading every
  backend at decision time (defeating lazy import), and
  `_unsupported.is_active()` as always-True-fallback is a footgun.
- **(c, recommended) Keep `_pick_backend()` per-package.** One-line body
  per package; lives in `__init__.py`; freely calls whatever
  `platform_compat` helper makes sense. The unified shape is the dispatch
  contract (`_select_backend` / `_reset_backend_cache`), not the gate
  input. `keep_above` is a normal case, not a carve-out.

## 7. Sequencing

Refactor order:

1. **`notifications` first.** It's already 90% on the target shape. Adding
   `_reset_backend_cache()` and renaming the in-function backend resolution
   into `_pick_backend()` lets us anchor the convention before touching
   anything that risks regression. Net cost ~10 lines; can ship in 30 min.
2. **`autostart` second.** Smallest non-conforming package. Pure function
   wrappers, no class binding wrinkle, no runtime gate, no constant
   re-export. Establishes the migration pattern on the simplest victim.
3. **`media_controls` third.** Class-alias-to-factory plus
   `_can_import_dbus_next` plus one forced test edit. The hardest correct-
   ness call (`MediaControlsService` becoming a function) lands after the
   pattern is established.
4. **`keep_above` last.** Constant relocation to `_constants.py` plus
   runtime-gate lift. Touches the most surface — but by this point the
   pattern is proven and we know the test contract holds.

Each step is independently revertible. The order also matches risk
ascending — the most-tested code (`media_controls` MPRIS state) lands when
the pattern is best-understood.

## 8. Risk register

- **Import-time vs function-time dispatch under test.** Today, importing
  `modules.autostart` resolves the backend eagerly; post-refactor it
  resolves on first `_select_backend()` call. A test that imports then
  patches `IS_LINUX` without calling `_reset_backend_cache()` silently
  sees the old backend. Mitigation: every test helper (`_reload`) calls
  `_reset_backend_cache()`; autouse fixtures invoke it post-test.
- **`MprisService` construction side effects.** MPRIS binds dbus_next
  state in `__init__`. Today `MediaControlsService = _Backend` references
  the class *without* instantiating; the unified factory function preserves
  that (instantiation still only at call sites). Verify by running
  `test_mpris_service_constructs_without_starting_thread` unchanged.
- **`MINI_PLAYER_WINDOW_TITLE` import cycle.** Moving the constant to
  `_constants.py` touches `modules/mini_player.py:598` and `_kwin.py:27`.
  Mitigation: re-export from `__init__.py` (`from ._constants import
  MINI_PLAYER_WINDOW_TITLE`) so the public import path keeps working.
- **dbus_next sabotage test.**
  `test_falls_back_to_unsupported_when_dbus_next_broken` poisons
  `sys.modules["dbus_next"]`. `_can_import_dbus_next()` must use a real
  `import` statement (not check `sys.modules`) so it walks the sabotaged
  finder. Existing test order (poison, then reload) survives.
- **Stale `_backend` across test files.** A previous test that left
  `_backend` populated poisons the next. Notifications tests already
  solved this with an autouse fixture; after refactor it calls
  `_reset_backend_cache()` instead of `sys.modules.pop`.

## 9. Effort + slice plan

**One PR per package, four PRs total**, merged in the order from section 7.
Reasons:

- Each refactor's diff is ~20–60 lines (`media_controls` is the largest).
  Four small PRs are reviewable; one ~200-line PR is not.
- A blast-radius regression caught between PR 2 and PR 3 lets us revert
  cleanly without losing earlier progress.
- The forced test edit in `media_controls` (PR 3) is the one place a
  reviewer should focus; isolating it makes review easier.
- `notifications` is on a branch that hasn't merged yet
  (`auto/notifications-backend`). Land that branch first as-is (it's the
  reference shape), *then* do PR 1 to add `_reset_backend_cache()` on top.

Total estimate: ~3 hours of code-agent time. Each PR includes its own
test updates; existing test files on `auto/backend-package-tests` rebase
cleanly onto each refactor in turn.

## 10. Out-of-scope / open questions

- Should `_select_backend()` and `_reset_backend_cache()` be moved into a
  shared `modules/_backend_dispatch.py` helper? Tempting (DRY), but the
  function bodies are 3 lines each and the per-package `_pick_backend()` is
  the only varying piece. Recommendation: skip the helper module for now;
  revisit if a fifth or sixth backend package lands.
- Should backends export an `is_active()` predicate for documentation
  purposes even if `_pick_backend` doesn't consume it? Probably not — adds
  surface without callers. Revisit when Windows / macOS backends land and
  multiple-backends-per-platform becomes a real scenario (e.g. Windows
  SMTC vs Windows Toasts).
- The `_LEGACY_*` rename precedent in `settings.py` suggests we'd survive a
  global rename of `_backend` to something more searchable (e.g.
  `_active_backend`). Not blocking; leave for a cosmetic follow-up.
