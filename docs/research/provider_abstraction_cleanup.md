# Provider abstraction cleanup — cast_manager.py + cast/dlna.py split

Status: research / pre-build. Last updated 2026-05-18. Gated task from
`docs/autonomous_tasks.md` §"Candidates needing research first". This
doc IS the architecture for the followup autonomous slice.

Scope naming: the backlog entry calls this "Provider abstraction
cleanup" but the actual targets are two cast-subsystem files. The
music-provider abstraction (`modules/providers/`) is healthy; if it
ever needs a split, that's a separate doc.

Line counts confirmed 2026-05-18: `cast_manager.py` 794 lines (audit
said 789), `cast/dlna.py` 1188. Both proceed.

---

## 1. `cast_manager.py` — current responsibilities

The file's docstring (lines 1–8) is honest: this is Chromecast +
AirPlay only. DLNA / Sonos / Snapcast moved to `modules/cast/`. Inside
`CastManager`, the concerns segregate cleanly along the existing `# ──`
banner comments:

| Concern | Lines | Notes |
|---|---|---|
| Module preamble + lazy `pychromecast` / `zeroconf` ensure | 1–52 | Two `_ensure_*` toggles + global bools |
| `CastDevice` dataclass | 55–66 | Shared by both protocols, plus `cast_object` opaque slot |
| `_AirPlayListener` (zeroconf service listener) | 69–93 | AirPlay v1 mDNS callback |
| `CastManager.__init__` + device-callback fanout | 97–110 | Owns `chromecast_devices`, `airplay_devices`, `active_cast`, `_on_update` |
| Chromecast discovery + connect + cast + transport | 112–427 | `discover_chromecasts`, `connect_to_chromecast*`, `cast_to_chromecast*`, `chromecast_pause/seek/set_volume/stop`, `_dump_cast_status`, `_CHROMECAST_AUDIO_MIME` table |
| Chromecast group member control | 429–526 | `group_members_async`, `set_member_volume_async` |
| AirPlay (v1 mDNS + pyatv v2) | 528–720 | `discover_airplay`, `_discover_airplay_pyatv`, `cast_to_airplay`, `_cast_to_airplay2`, `airplay_stop` |
| Common (gates + lifecycle) | 722–794 | `_type_enabled`, `discover_all`, `discover_all_at_boot`, `get_all_devices`, `stop_cast`, `cleanup` |

The Chromecast cluster alone is 315 lines. The AirPlay cluster is 192
lines. They share `CastDevice`, `active_cast`, and `_notify` — that's
the entire seam.

## 2. Proposed split — `cast_manager.py`

Convert `modules/cast_manager.py` into a package
`modules/cast_manager/` with the public surface preserved via
re-exports. The chromecast and airplay surfaces become sibling mixin
modules; `CastManager` itself ends up as a thin orchestrator.

```
modules/cast_manager/
    __init__.py            # re-exports CastManager, CastDevice
    _common.py             # CastDevice, _ensure_chromecast, _ensure_zeroconf,
                           #   _type_enabled, _notify helpers          (~120 lines)
    _chromecast.py         # ChromecastMixin: discover_chromecasts,
                           #   connect_to_chromecast*, cast_to_chromecast*,
                           #   chromecast_*, _dump_cast_status,
                           #   _CHROMECAST_AUDIO_MIME, group_members_async,
                           #   set_member_volume_async                (~340 lines)
    _airplay.py            # AirPlayMixin + _AirPlayListener:
                           #   discover_airplay*, cast_to_airplay,
                           #   _cast_to_airplay2, airplay_stop          (~210 lines)
    _manager.py            # CastManager(ChromecastMixin, AirPlayMixin):
                           #   __init__, set_devices_callback,
                           #   discover_all*, get_all_devices, stop_cast,
                           #   cleanup                                  (~80 lines)
```

All four implementation files land below 400 lines. The mixin
composition keeps `chromecast_devices`, `airplay_devices`,
`active_cast`, `_notify` on one instance, so existing attribute access
from `player_backend.py:332–968` and `now_playing_bar.py:875–998`
works unchanged.

Alternative considered: keep `cast_manager.py` as a thin facade and
put `_cm_chromecast.py` + `_cm_airplay.py` at modules-top level. The
package shape wins because the existing `modules/cast/` package is
already the codebase's idiom for protocol-cluster homes, and
`__init__.py` re-exports mean zero callsite edits.

## 3. Public surface preserved

Today's importers and the path they use:

- `jellytoast.py:157` — `from modules.cast_manager import CastManager`
- `now_playing_bar.py:76` — `from modules.cast_manager import CastManager, CastDevice`
- `cast_proxy.py:200`, `player_backend.py:667` — `from modules.cast_manager import CastManager`
- `tests/test_cast_gating.py:21,46` — `from modules.cast_manager import CastManager` *and* `import modules.cast_manager as _cm_mod` followed by `monkeypatch.setattr(_cm_mod, "pychromecast", ...)` (lines 51, 67, 92, 105)

That last one is the load-bearing constraint: the test patches six
module-level names (`pychromecast`, `CHROMECAST_AVAILABLE`,
`ZEROCONF_AVAILABLE`, `Zeroconf`, `ServiceBrowser`, `run_async`) on
the `modules.cast_manager` namespace. The split must preserve those
names *at the package namespace* so `monkeypatch.setattr(_cm_mod,
"pychromecast", _PCStub)` continues to work.

Open question, recommended default: keep the lazy-import globals and
the `_ensure_chromecast` / `_ensure_zeroconf` helpers in the **package
namespace** (`__init__.py` or `_common.py` re-exported from
`__init__.py`), not the submodule level. Submodules then read
`pychromecast` via `from modules.cast_manager import pychromecast as
_pc` at call time, or — simpler — via an indirection helper in
`_common.py`. The monkeypatch contract stays bit-for-bit valid.

## 4. Signal routing seam

`PlayerBus` cast signals stay where they are
(`modules/player_state.py:330–350`):

- `cast_started(str)` — emitted by callers (jellytoast.py around line
  2090, 2142), not by `CastManager` itself
- `cast_stopped()` — same
- `cast_devices_updated(list)` — same, fed from
  `set_devices_callback`'s callback
- All `snapcast_*` signals (10 of them) — unrelated; live in
  `modules/cast/snapcast.py`

`CastManager` doesn't import `PlayerBus` directly. The split changes
nothing about signals. Verify by `grep PlayerBus modules/cast_manager.py`
returning empty before and after.

## 5. Slice plan — `cast_manager.py`

**One PR, one slice.** The mechanical extraction is uniform (move
methods to mixin classes, add `__init__.py` re-exports, run tests).
Splitting Chromecast-first / AirPlay-second adds churn — the second
PR would touch `_manager.py` again just to register the second mixin.

**Size:** M. ~50 method moves, one new package. Test exposure:
`tests/test_cast_gating.py` is the canary; if it passes unmodified
the split is sound.

---

## 6. `cast/dlna.py` — current responsibilities

The file is one self-contained DLNA control-point. Same banner-comment
discipline as `cast_manager`:

| Concern | Lines | Public symbols |
|---|---|---|
| Module docstring (load-bearing rationale) | 1–68 | — |
| Imports + constants (SSDP ST, UA template, MIME / PN tables, retry codes, byte caps, poll cadence) | 70–144 | `SSDP_ST_MEDIA_RENDERER`, `USER_AGENT_TEMPLATE` |
| Lazy imports + availability probe | 147–176 | `is_available` |
| Settings access (enabled flag, UA overrides, per-device UA picker) | 179–241 | (private) |
| Dataclasses | 244–290 | `DlnaDevice`, `TrackMetadata` |
| DIDL-Lite builder | 293–441 | `build_didl_lite`, plus `_format_duration`, `_protocol_info_for`, `_xml_attr`, `_xml_text`, `_truncate_cover_url` |
| Codec-fallback decision tree | 444–512 | `PushDecision`, `decide_push_format`, `decide_retry_after_error` |
| SSDP discovery dedup helpers | 515–570 | `dedupe_search_response`, `parse_host_from_location`, `_parse_udn_from_usn` |
| Asyncio loop worker thread | 573–663 | `_DlnaLoopThread` |
| `DlnaController` (lifecycle, discovery, bind, push, transport, polling) | 665–1105 | `DlnaController`, `TranscodeUrlFn` |
| Module helpers (`_container_from_mime`, `_meta_with_mime`, `_td_to_sec`) | 1108–1157 | — |
| Singleton getter | 1160–1188 | `get_dlna_controller`, `__all__` |

The DIDL builder, the codec-fallback decision tree, and the SSDP dedup
helpers are pure functions — no state, no Qt, no asyncio. They make up
roughly 290 lines and are the easiest concern to peel off cleanly.

`DlnaController` itself is 440 lines and clusters into lifecycle (~30),
discovery (~80), bind (~35), push (~165 — includes `async_play`'s
714-retry tree + `_try_set_and_play`), transport-control (~45), and
polling (~80). The single state machine here is the controller's
`_lock`-guarded triple: `_devices`, `_active_udn`, `_active_device_obj`,
`_transcode_cache`, `_last_state`, `_poll_task` — all six mutate
together, all six must stay co-located.

## 7. Proposed split — `cast/dlna.py`

Convert `modules/cast/dlna.py` into a package
`modules/cast/dlna/` with re-exports preserving every symbol in
today's `__all__` (lines 1174–1188) plus the underscore-prefixed
helpers the test suite imports (see §8).

```
modules/cast/dlna/
    __init__.py            # re-exports the public + test-touched API   (~30 lines)
    _constants.py          # SSDP_ST_MEDIA_RENDERER, USER_AGENT_TEMPLATE,
                           #   _DLNA_PN_BY_MIME, _MIME_BY_CONTAINER,
                           #   _TRANSCODE_RETRY_ERRORS, _DIDL_MAX_BYTES,
                           #   _DIDL_COVER_MAX_CHARS, _POLL_INTERVAL_SEC (~60 lines)
    _settings.py           # _settings_enabled, _settings_user_agent_overrides,
                           #   _ua_for_device                            (~70 lines)
    _models.py             # DlnaDevice, TrackMetadata, PushDecision,
                           #   TranscodeUrlFn type alias                (~70 lines)
    didl.py                # build_didl_lite + private xml/duration/cover
                           #   helpers + _container_from_mime,
                           #   _meta_with_mime                         (~190 lines)
    codec.py               # decide_push_format, decide_retry_after_error
                           #   (plus is_available probe + _ensure_async_upnp)
                           #                                            (~80 lines)
    discovery.py           # _parse_udn_from_usn, dedupe_search_response,
                           #   parse_host_from_location                 (~80 lines)
    _loop.py               # _DlnaLoopThread                            (~95 lines)
    controller.py          # DlnaController + get_dlna_controller +
                           #   _td_to_sec helper                       (~380 lines)
```

Every file under 400 lines; `controller.py` at ~380 is the largest and
takes the irreducible state-machine surface. Re-exports from
`__init__.py` cover the existing `__all__` plus the underscore symbols
`tests/test_cast_dlna.py:34–56` reaches into (`_container_from_mime`,
`_DlnaLoopThread`, `_format_duration`, `_meta_with_mime`,
`_parse_udn_from_usn`, `_protocol_info_for`, `_td_to_sec`,
`_truncate_cover_url`, `_ua_for_device`).

`is_available` and `_ensure_async_upnp` live in `codec.py` because
they're the "can we even speak DLNA" predicate, of a piece with the
decision tree. `_constants.py` would be an acceptable alternative.

I deliberately did **not** split the controller along
discovery/bind/push/transport/poll lines. They share a six-field state
machine under one `_lock`; pulling them apart means inventing a
`ControllerState` dataclass and threading it through five helpers —
net negative for a class that reads cleanly at 440 lines.

## 8. DLNA-specific gotchas

1. **The test suite imports private helpers.** `tests/test_cast_dlna.py:34–56`
   imports nine underscore-prefixed names by full path
   (`_container_from_mime`, `_DlnaLoopThread`, `_format_duration`,
   `_meta_with_mime`, `_parse_udn_from_usn`, `_protocol_info_for`,
   `_td_to_sec`, `_truncate_cover_url`, `_ua_for_device`).
   `__init__.py` must re-export every one or the suite breaks.

2. **`_DlnaLoopThread` is the codebase's only `threading.Thread`
   exception.** Module docstring lines 25–33 call it out as the
   documented carve-out from "no raw threads, use `modules.async_io`".
   The rationale must travel into `_loop.py` as a module docstring.

3. **`async_play` is the largest method (~60 lines) and is non-trivial
   state-machine logic** — it calls `async_bind`, `decide_push_format`,
   `_try_set_and_play`, `decide_retry_after_error`, `transcode_url_fn`,
   `build_didl_lite`, `_meta_with_mime`. Five submodules' worth of
   helpers in 60 lines. Stays in `controller.py`.

4. **Lazy `async_upnp_client` imports stay at call sites** —
   `async_discover` line 760, `async_bind` 825–827, `_try_set_and_play`
   958–961. The ~150 ms cold-import cost rationale doesn't change.

5. **`get_dlna_controller()` singleton.** [[feedback-provider-singleton-refs]]
   applies obliquely: if a future sign-out path resets the DLNA
   controller, cached references go stale. No live caller caches it
   today (DLNA backend isn't wired into UI yet). Future-watch, not a
   current blocker.

## 9. Slice plan — `cast/dlna.py`

**Two slices, sequenced.**

- **DLNA-1: pure-function extraction. S.** Move `_constants.py`,
  `_models.py`, `didl.py`, `codec.py`, `discovery.py`, `_settings.py`
  out into a new `modules/cast/dlna/` package. Controller and loop
  thread stay in `dlna.py` temporarily as a facade re-importing from
  the new submodules. Zero behavioural change; the pure functions have
  no shared state with the controller, so this is the lowest-risk
  landing.

- **DLNA-2: controller + loop split. S–M.** Move `_DlnaLoopThread` to
  `_loop.py`, `DlnaController` + `get_dlna_controller` to
  `controller.py`; replace `dlna.py` with `dlna/__init__.py`. This is
  where the test-import risk lives.

Two slices, not one, because DLNA-2 carries the missed-re-export risk.
Landing DLNA-1 first proves the re-export pattern on the easier
surface so DLNA-2 inherits a validated `__init__.py` shape.

---

## 10. Sequencing — which split first?

**DLNA first, then cast_manager.**

1. `cast_manager.py` doesn't import from `modules/cast/dlna.py` today
   (`grep "cast.dlna" modules/cast_manager.py` is empty). DLNA-side
   work has zero blast radius on cast_manager.
2. `cast_manager.py` is about to grow when DLNA UI integration lands
   (`casting_dlna.md` §12–§13 wants `CastManager` to become a
   registry of protocol backends). That rework targets the post-split
   shape; doing cast_manager second lets the rework benefit directly.
3. DLNA has more test exposure (`test_cast_dlna.py` ~1100 lines vs.
   `test_cast_gating.py` ~260). Proving the re-export pattern on the
   harder case first means cast_manager inherits the precedent.

Dependency direction snapshot: `cast_proxy.py:200,224`,
`player_backend.py:667`, `now_playing_bar.py:76`, `jellytoast.py:157`
all import `CastManager`. None imports from `modules/cast/dlna`. The
DLNA singleton has zero `modules/` callers today.

## 11. Risk register

1. **Test patches reach into module namespace by name.**
   `test_cast_gating.py` monkeypatches six module-level names on
   `modules.cast_manager`. Mitigation: re-export from the package's
   `__init__.py`. Run the suite before merging.

2. **`test_cast_dlna.py` imports nine underscore-prefixed helpers.**
   Mitigation: `__init__.py` explicitly imports each by name from
   the owning submodule.

3. **Circular import risk inside `cast/dlna/`.** `controller.py`
   depends on every other submodule; nothing depends back on
   `controller.py`. Safe today. Mitigation: codify the one-way
   dependency arrow in a header comment in `__init__.py`.

4. **Submodule re-export drift.** New public symbols can land in a
   submodule and miss the `__init__.py` list — silent breakage for
   later importers. Mitigation: each submodule defines `__all__`;
   `__init__.py` uses `from .X import *`.

5. **Mixin MRO surprises on `CastManager`.** Two mixins today; if a
   third lands and methods collide, MRO silently picks one.
   Mitigation: keep the `chromecast_*` / `airplay_*` naming
   convention (already in place). A one-shot `dir(MRO)` assertion
   in `test_cast_gating.py` is cheap if drift becomes real.

## 12. What stays untouched

Out of scope this round even though they're cast-neighbourhood:

- `modules/cast/sonos.py` — 724 lines, below threshold; split when it
  passes ~1000.
- `modules/cast/snapcast.py` — 982 lines, right at the edge. Defer
  one cycle; recheck after the unified-cast-menu UI lands.
- `modules/cast/__init__.py` 23, `cast_proxy.py` 413,
  `airplay2.py` 383, `airplay_pairing.py` 436,
  `cast_dialog_sections.py` 116 — all below threshold.
- `modules/providers/` — the music-provider abstraction is healthy
  despite the doc's title. See preamble.

Cross-cutting invariants: no changes to `PlayerBus` signal
definitions (`player_state.py:330–350`). No changes to importer
call sites — all `from modules.cast_manager import …` lines remain
verbatim.

---

## Summary for the implementer

Order: DLNA-1 (S) → DLNA-2 (S–M) → cast_manager (M).

For each: create the package directory, move clusters by line range
from §2 and §7, build `__init__.py` re-exports, run
`pytest tests/test_cast_dlna.py tests/test_cast_gating.py` then the
full suite. If a test patches a module-level name and fails, the fix
is always "add it to `__init__.py`'s re-export list", not "refactor
the test". The patches are the public contract.

Cross-references: [[architecture-cast-proxy]] (proxy stays put),
[[feedback-cast-menu-unified-collapsible]] (the UI consumer the
cast_manager split paves for), [[feedback-provider-singleton-refs]]
(future singleton-refresh concern, not a blocker today).
