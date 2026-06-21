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

Threading — why every call hops to a worker thread:
``StartupTask``'s API is asynchronous, and the only synchronous way to consume
an ``IAsyncOperation`` is its blocking ``.get()`` — which WinRT forbids on a
single-threaded apartment. The Qt GUI thread is an STA (Qt and ``comtypes``
initialise it that way), so calling ``.get()`` there raises ``RuntimeError:
Cannot call blocking method from single-threaded apartment`` (observed
in-package 2026-06-18). Each WinRT touch therefore runs on a short-lived thread
initialised ``MULTI_THREADED`` (an MTA), where the blocking wait is legal; only
a plain ``bool`` crosses back to the GUI thread. (Synchronous WinRT — e.g. the
SMTC interop in ``media_controls/_windows.py`` — is fine on the STA; only the
blocking await is not.) ``_TASK_ID`` must match ``AppxManifest.xml`` exactly.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Must match the <desktop:StartupTask TaskId="..."> in AppxManifest.xml.
_TASK_ID = "jellytoastStartup"


def _run_in_mta(fn):
    """Run ``fn`` on a dedicated MTA thread and return its result (or None).

    WinRT's blocking ``IAsyncOperation.get()`` cannot run on the Qt GUI thread
    (an STA). A fresh thread initialised ``MULTI_THREADED`` runs the blocking
    waits legally; it is discarded after the call, so the apartment lifecycle
    stays clean and the rare autostart toggles never touch a shared pool. Any
    failure (no winrt, apartment init refused, projection error) degrades to
    ``None`` so the public API can never raise.
    """
    import threading

    box = {}

    def _target():
        try:
            from winrt.runtime import (
                ApartmentType,
                init_apartment,
                uninit_apartment,
            )
        except Exception as e:  # pragma: no cover — Windows/MSIX-only
            logger.debug("winrt apartment API unavailable: %s", e)
            return
        try:
            init_apartment(ApartmentType.MULTI_THREADED)
        except Exception as e:  # pragma: no cover — Windows/MSIX-only
            logger.debug("init_apartment(MTA) failed: %s", e)
            return
        try:
            box["result"] = fn()
        except Exception as e:  # pragma: no cover — Windows/MSIX-only
            logger.debug("StartupTask worker call failed: %s", e)
        finally:
            try:
                uninit_apartment()
            except Exception:  # pragma: no cover — Windows/MSIX-only
                pass

    t = threading.Thread(target=_target, name="jt-startuptask", daemon=True)
    t.start()
    t.join()
    return box.get("result")


def _resolve(op):
    """Block on a WinRT IAsyncOperation. Legal only off the GUI STA — callers
    reach it through ``_run_in_mta``. The projection exposes ``.get()`` for
    synchronous callers; fall back to asyncio if a build only awaits."""
    get = getattr(op, "get", None)
    if callable(get):
        return get()
    import asyncio

    return asyncio.run(op)  # pragma: no cover — projection-dependent


def _get_task():
    """Our StartupTask, or None if the API/projection is unavailable.
    MUST run inside ``_run_in_mta`` — it performs the blocking WinRT wait."""
    try:
        from winrt.windows.applicationmodel import StartupTask

        return _resolve(StartupTask.get_async(_TASK_ID))
    except Exception as e:  # pragma: no cover — Windows/MSIX-only
        logger.debug("StartupTask.get_async(%s) failed: %s", _TASK_ID, e)
        return None


def _is_enabled_state(state) -> bool:
    try:
        from winrt.windows.applicationmodel import StartupTaskState

        return state in (StartupTaskState.ENABLED, StartupTaskState.ENABLED_BY_POLICY)
    except Exception:  # pragma: no cover — Windows/MSIX-only
        return False


# ── Worker-thread implementations (each runs inside an MTA via _run_in_mta) ──


def _is_supported_impl() -> bool:
    return _get_task() is not None


def _is_enabled_impl() -> bool:
    task = _get_task()
    if task is None:
        return False
    try:
        return _is_enabled_state(task.state)
    except Exception as e:  # pragma: no cover — Windows/MSIX-only
        logger.debug("StartupTask state read failed: %s", e)
        return False


def _enable_impl() -> bool:
    task = _get_task()
    if task is None:
        return False
    try:
        return _is_enabled_state(_resolve(task.request_enable_async()))
    except Exception as e:  # pragma: no cover — Windows/MSIX-only
        logger.debug("StartupTask request_enable failed: %s", e)
        return False


def _disable_impl() -> bool:
    task = _get_task()
    if task is None:
        return False
    try:
        task.disable()
        return True
    except Exception as e:  # pragma: no cover — Windows/MSIX-only
        logger.debug("StartupTask disable failed: %s", e)
        return False


# ── Public API — mirrors the platform-agnostic backend contract ─────────────


def is_supported() -> bool:
    return bool(_run_in_mta(_is_supported_impl))


def is_enabled() -> bool:
    return bool(_run_in_mta(_is_enabled_impl))


def enable() -> bool:
    """Request enable. Returns True only if Windows actually enabled it — a
    user who turned it off in Settings (DisabledByUser) blocks us, and the
    caller should point them at Settings -> Apps -> Startup."""
    return bool(_run_in_mta(_enable_impl))


def disable() -> bool:
    return bool(_run_in_mta(_disable_impl))
