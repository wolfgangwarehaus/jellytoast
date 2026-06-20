"""Windows MSIX (packaged-app) launch-on-login backend.

Packaged apps can't use the per-user Run key — Windows ignores Run-key
entries from packages. Autostart for a packaged app is a *startup task*
declared in the manifest (``windows.startupTask`` with
``TaskId="jellytoastStartup"``) and toggled at runtime through the
``Windows.ApplicationModel.StartupTask`` WinRT API. The user can always
override us from Settings -> Apps -> Startup; once they disable it there,
Windows forbids programmatic re-enable (state ``DisabledByUser``).

Mirrors the public backend API (``is_supported``/``is_enabled``/``enable``/
``disable``). Every call is defensively wrapped: a WinRT/projection failure
degrades to "unsupported" instead of raising, so a packaging quirk can never
crash startup.

>>> VERIFY ON THE WIN 11 LAPTOP <<<
The WinRT StartupTask projection is untested off-device: the sync-resolution
of the IAsyncOperation (``.get()`` below) and the ``winrt`` import path may
need adjustment for the bundled projection. ``_TASK_ID`` must match
``packaging/msix/AppxManifest.xml`` exactly.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Must match the <desktop:StartupTask TaskId="..."> in AppxManifest.xml.
_TASK_ID = "jellytoastStartup"


def _resolve(op):
    """Block on a WinRT IAsyncOperation. The projection exposes ``.get()``
    for synchronous callers; fall back to asyncio if a build only awaits."""
    get = getattr(op, "get", None)
    if callable(get):
        return get()
    import asyncio

    return asyncio.run(op)  # pragma: no cover — projection-dependent


def _get_task():
    """Our StartupTask, or None if the API/projection is unavailable."""
    try:
        from winrt.windows.applicationmodel import StartupTask

        return _resolve(StartupTask.get_async(_TASK_ID))
    except Exception as e:  # pragma: no cover — Windows/MSIX-only
        logger.debug("StartupTask.get_async(%s) failed: %s", _TASK_ID, e)
        # Temporary: write exception to file so we can diagnose in console=False build
        try:
            import os, traceback
            _dbg = os.path.join(os.environ.get("TEMP", "C:\\Temp"), "jt_msix_debug.txt")
            with open(_dbg, "a") as f:
                f.write(f"_get_task failed: {type(e).__name__}: {e}\n{traceback.format_exc()}\n")
        except Exception:
            pass
        return None


def _is_enabled_state(state) -> bool:
    try:
        from winrt.windows.applicationmodel import StartupTaskState

        return state in (StartupTaskState.ENABLED, StartupTaskState.ENABLED_BY_POLICY)
    except Exception:  # pragma: no cover — Windows/MSIX-only
        return False


def is_supported() -> bool:
    return _get_task() is not None


def is_enabled() -> bool:
    task = _get_task()
    if task is None:
        return False
    try:
        return _is_enabled_state(task.state)
    except Exception as e:  # pragma: no cover — Windows/MSIX-only
        logger.debug("StartupTask state read failed: %s", e)
        return False


def enable() -> bool:
    """Request enable. Returns True only if Windows actually enabled it — a
    user who turned it off in Settings (DisabledByUser) blocks us, and the
    caller should point them at Settings -> Apps -> Startup."""
    task = _get_task()
    if task is None:
        return False
    try:
        return _is_enabled_state(_resolve(task.request_enable_async()))
    except Exception as e:  # pragma: no cover — Windows/MSIX-only
        logger.debug("StartupTask request_enable failed: %s", e)
        return False


def disable() -> bool:
    task = _get_task()
    if task is None:
        return False
    try:
        task.disable()
        return True
    except Exception as e:  # pragma: no cover — Windows/MSIX-only
        logger.debug("StartupTask disable failed: %s", e)
        return False
