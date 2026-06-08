"""Cast dispatch for the main window.

The cast-button / cast-dialog glue, extracted from ``jellytoast.py``: open the
cast dialog, the cast-button right-click quick menu, disconnect, find a device
by uuid, cast a favourite, the unified per-protocol ``_cast_to_device`` dispatch
(Chromecast / DLNA / Sonos / AirPlay / Snapcast), and the Snapcast control
surface.

``_CastDispatcherMixin`` is mixed into ``JellytoastWindow`` — not standalone.
Its methods reference window state (``self.cast_manager``, ``self.bus``, the
lazy ``self._cast_dlg`` / ``self._snapcast_dlg`` dialog singletons) and the
window-core placement helpers ``self._center_dialog_on_main`` (centers the
cast-picker dialog over the main window) and
``self._position_dialog_above_now_playing`` (docks the Snapcast control surface
above the now-playing bar); all resolve on the combined instance. Heavy / circular-prone deps (``airplay2``,
``airplay_pairing``, ``snapcast_control``, ``scrobble``, ``icon``,
``opaque_menu``, ``get_snapcast_controller``) stay as in-method imports exactly
as before, preserving the lazy-import boot savings and avoiding a
cast_dialog ↔ host cycle.
"""

import logging
import os

from PySide6.QtCore import QPoint, QTimer

from modules.cast_dialog import CastDialog
from modules.cast_manager import CastType
from modules.player_state import get_now_playing
from modules.providers import get_provider
from modules.settings import get_settings

logger = logging.getLogger("jellytoast")

# AirPlay 2 / pyatv pairing tracing — noisy during the LG-webOS pairing
# investigation, off in normal use (JT_AP2_DEBUG=1). Moved here from
# jellytoast.py with _cast_to_device, its only consumer.
_AP2_DBG = os.environ.get("JT_AP2_DEBUG") == "1"


def _ap2_dbg(msg: str) -> None:
    if _AP2_DBG:
        logger.debug("ap2: %s", msg)


class _CastDispatcherMixin:
    """Cast dispatch, mixed into ``JellytoastWindow``. Plain-``object`` mixin
    (single Qt base on the window)."""

    def _open_cast_dialog(self):
        # Open without gating — picking a device when nothing is playing
        # pre-arms it as the cast target. The next track the user starts
        # will route to that device automatically (MpvController.play
        # checks active_cast and forwards to cast_manager).
        #
        # Non-modal so a modal exec() doesn't disable + dim the main
        # window. parent=self establishes a Wayland transient-for
        # relationship so KWin places the dialog relative to the main
        # window instead of dropping it via "Smart" auto-placement (on
        # X11 / Windows / macOS we additionally call move() below to
        # dock the dialog above the cast button; xdg-shell forbids
        # client-side move() so on KDE Wayland that's silently ignored
        # and the parent relationship is what gets the dialog onto the
        # right surface). Singleton-guard a re-click.
        existing = getattr(self, "_cast_dlg", None)
        if existing is not None and existing.isVisible():
            existing.raise_()
            existing.activateWindow()
            return
        dlg = CastDialog(self.cast_manager, self)
        self._cast_dlg = dlg

        def _on_cast_accepted():
            if dlg.selected_device:
                self._cast_to_device(dlg.selected_device)

        dlg.accepted.connect(_on_cast_accepted)
        dlg.finished.connect(lambda _r: setattr(self, "_cast_dlg", None))
        # Center over the main window. On KDE Wayland xdg-shell ignores
        # client move() and KWin centers the dialog via the parent=self
        # transient-for relationship; on Windows / macOS / X11 move() IS
        # honored, so we center explicitly to match. (The old right-edge
        # anchor docked the dialog flush to the window's right side on
        # those platforms — only Wayland's dropped move() made it look
        # centered.) User can still drag the dialog after open.
        self._center_dialog_on_main(dlg)
        dlg.show()

    def _show_cast_context_menu(self, global_pos):
        """Right-click on the bottom bar's cast button — a quick menu of
        hearted devices (cast straight to them) plus Disconnect, without
        opening the full picker."""
        from modules.icons import icon as _icon
        from modules.ui_helpers import opaque_menu

        menu = opaque_menu(self)
        favs = get_settings().favorite_cast_devices
        # Self-heal legacy entries: the first cut of this feature stored
        # only the uuid, so the migration left name == uuid. If discovery
        # has since turned the device up, adopt its real name/type and
        # persist the upgrade so the menu reads properly from now on.
        upgraded = False
        for fav in favs:
            if fav["name"] == fav["uuid"] or not fav.get("type"):
                live = self._find_cast_device(fav["uuid"])
                if live is not None:
                    fav["name"] = live.name
                    fav["type"] = live.device_type
                    upgraded = True
        if upgraded:
            get_settings().favorite_cast_devices = favs
        if favs:
            for fav in favs:
                glyph = _icon("airplay" if fav.get("type") == "airplay" else "cast")
                act = menu.addAction(glyph, fav.get("name") or "Device")
                act.triggered.connect(lambda _=False, f=fav: self._cast_to_favorite(f))
        else:
            placeholder = menu.addAction("No favorite devices")
            placeholder.setEnabled(False)
        menu.addSeparator()
        disconnect = menu.addAction("Disconnect")
        disconnect.setEnabled(self.cast_manager.active_cast is not None)
        disconnect.triggered.connect(self._disconnect_cast)
        menu.addSeparator()
        open_picker = menu.addAction("Open cast menu…")
        open_picker.triggered.connect(self._open_cast_dialog)
        # Anchor the menu fully above the mini-player / cast / volume
        # icon cluster, horizontally centered on it — rather than at the
        # cursor, which would spill off the bottom-right corner. Clamp
        # the final rect to the window so it can never leave the UI.
        size = menu.sizeHint()
        cluster = [self.np_bar.queue_btn, self.np_bar.cast_btn, self.np_bar.vol_btn]
        tls = [b.mapToGlobal(QPoint(0, 0)) for b in cluster]
        cluster_left = min(p.x() for p in tls)
        cluster_right = max(tls[i].x() + cluster[i].width() for i in range(len(cluster)))
        cluster_top = min(p.y() for p in tls)
        x = (cluster_left + cluster_right) // 2 - size.width() // 2
        y = cluster_top - size.height() - 6
        win = self.frameGeometry()  # global coords
        x = max(win.left() + 4, min(x, win.right() - size.width() - 4))
        y = max(win.top() + 4, min(y, win.bottom() - size.height() - 4))
        menu.exec(QPoint(x, y))

    def _disconnect_cast(self):
        """Stop the active cast session — mirrors CastDialog's
        Disconnect button, for the cast button's right-click menu."""
        self.cast_manager.stop_cast()
        self.bus.cast_stopped.emit()

    def _find_cast_device(self, uuid: str):
        """The live CastDevice for a uuid, or None if discovery hasn't
        turned it up this session."""
        for d in self.cast_manager.get_all_devices():
            if d.uuid == uuid:
                return d
        return None

    def _cast_to_favorite(self, fav: dict):
        """Cast to a hearted device by uuid. If discovery hasn't found
        it yet, kick a scan and poll briefly before giving up."""
        uuid = fav.get("uuid", "")
        dev = self._find_cast_device(uuid)
        if dev is not None:
            self._cast_to_device(dev)
            return
        # Not in the discovery cache yet — scan, then poll for a few
        # seconds (discover_all populates cast_manager's device lists
        # directly, so a plain poll is enough; no callback wiring).
        self.cast_manager.discover_all()
        state = {"tries": 0}
        timer = QTimer(self)
        timer.setInterval(700)

        def _poll():
            state["tries"] += 1
            found = self._find_cast_device(uuid)
            if found is not None:
                timer.stop()
                timer.deleteLater()
                self._cast_to_device(found)
            elif state["tries"] >= 6:
                timer.stop()
                timer.deleteLater()
                from modules.frosted_dialog import frosted_info

                frosted_info(
                    self,
                    "Cast",
                    f"Couldn't find “{fav.get('name')}” on the "
                    f"network right now. Open the cast menu to rescan.",
                )

        timer.timeout.connect(_poll)
        timer.start()

    def _cast_to_device(self, dev):
        """Cast to a specific CastDevice — shared by the cast dialog's
        pick and the cast button's right-click quick menu."""
        np = get_now_playing()
        playing_now = bool(np.item_id and np.stream_url)
        # Capture position BEFORE we touch mpv — np.position is updated
        # by MpvController on every time-pos tick. Once we stop, the
        # value still reflects the last-seen position, but we want the
        # cast to resume exactly where the user was.
        resume_seconds = (np.position / 1000.0) if playing_now else 0.0

        # Snapcast is a multiroom routing matrix (groups → streams +
        # per-room volume), not a play-this-track target — picking it must
        # NOT stop local playback or arm active_cast as a stream sink. Hand
        # off to its own control surface and return before the stop/cast
        # flow below (also keeps it out of the AirPlay fall-through).
        if dev.device_type == CastType.SNAPCAST:
            self._open_snapcast_control(dev)
            return

        # IMPORTANT: stop the local mpv stream BEFORE we set active_cast.
        # MpvController.stop now routes to chromecast_stop when active_cast
        # is set, so emitting stop_requested afterwards would kill the cast
        # session we just initiated. Stop also clears the now-playing UI
        # via playback_stopped — we re-emit playback_started after the
        # cast lands so the bar / mini player re-render the same track.
        if playing_now:
            self.bus.stop_requested.emit()

        # Result handling is shared across transports. For Chromecast it
        # runs from an async callback (the cast call blocks for seconds
        # on cc.wait() + block_until_active); for AirPlay it's invoked
        # inline at the end of this method.
        def _on_cast_result(ok, _dev=dev, _np=np, _playing=playing_now):
            if ok:
                self.bus.cast_started.emit(_dev.name)
                if _playing:
                    # Re-render the now-playing UI so the title, artist,
                    # cover art, and progress bar reflect the track that's
                    # now on the cast device. Without this, the bar shows
                    # "Nothing playing" because of the prior stop_requested.
                    # Flag this re-emit as a cast handoff so the scrobble
                    # manager doesn't re-arm + double-count a track that
                    # was already scrobbled before the cast.
                    try:
                        from modules.scrobble import get_scrobble_manager

                        get_scrobble_manager().note_cast_handoff()
                    except Exception:
                        pass
                    self.bus.playback_started.emit(_np)
            else:
                from modules.frosted_dialog import frosted_warning

                frosted_warning(
                    self, "Cast failed", f"Could not cast to {_dev.name}.", icon_name="cast"
                )
                # We stopped local mpv up front (stop_requested at the top of
                # _cast_to_device) before attempting the cast. On failure the
                # success-path restore never runs, so the track is left dead
                # and the bar reads "Nothing playing". Resume LOCAL playback
                # via play_requested — NOT playback_started, which is only a
                # UI/EQ notification and would leave audio stopped. play()
                # resumes at _np.position and re-renders the bar itself.
                if _playing:
                    self.bus.play_requested.emit(_np)

        # When a track is playing, the per-type push (chromecast direct-play
        # MIME pick + transcode fallback, DLNA/Sonos off-thread SOAP, AirPlay
        # sync) is the unified CastManager.start_track surface — shared with
        # the auto-advance site (player_backend.MpvController.play). This site
        # resumes mid-track, so it passes resume_seconds (start_track drops it
        # for unseekable live radio). The connect-without-media paths below
        # are device-pick-only and stay here.
        if dev.device_type == CastType.CHROMECAST:
            # Chromecast connect/play block on cc.wait() +
            # block_until_active — run them off the GUI thread so the
            # dialog doesn't freeze while the receiver negotiates, then
            # report back through _on_cast_result.
            if playing_now:
                self.cast_manager.start_track(
                    dev, np, provider=get_provider(),
                    resume_seconds=resume_seconds, on_done=_on_cast_result,
                )
            else:
                self.cast_manager.connect_to_chromecast_async(dev, on_done=_on_cast_result)
            return
        elif dev.device_type == CastType.DLNA:
            # DLNA push runs OFF the GUI thread — DlnaController.play
            # blocks on SOAP (up to 30 s on a slow renderer). start_track
            # builds the DIDL metadata + provider transcode fallback from np;
            # the 714-retry inside the controller handles a renderer that
            # refuses the native MIME.
            if playing_now:
                self.cast_manager.start_track(
                    dev, np, provider=get_provider(),
                    resume_seconds=resume_seconds, on_done=_on_cast_result,
                )
            else:
                self.cast_manager.active_cast = dev
                _on_cast_result(True)
            return
        elif dev.device_type == CastType.SONOS:
            # Sonos coordinator push — also blocking SOAP, off the GUI
            # thread. The backend cast_to_sonos resolves the cast-proxy
            # URL + builds DIDL itself.
            if playing_now:
                self.cast_manager.start_track(
                    dev, np, provider=get_provider(),
                    resume_seconds=resume_seconds, on_done=_on_cast_result,
                )
            else:
                self.cast_manager.active_cast = dev
                _on_cast_result(True)
            return
        else:
            # AirPlay v1 has no real "connect without media" handshake;
            # if there's nothing to cast, just record the choice. Calls
            # to play() afterward will issue POST /play to this device.
            #
            # AirPlay 2 receivers that need pairing get intercepted
            # here: launch the pairing modal before attempting cast.
            # If pairing succeeds the credentials are persisted by the
            # dialog itself and the cast retries.
            from modules import airplay2 as _ap2

            is_ap2 = isinstance(dev.cast_object, _ap2.AirPlay2Device)
            _ap2_dbg(
                f"cast handler: dev={dev.name!r} type={dev.device_type} "
                f"is_ap2={is_ap2} playing_now={playing_now}"
            )
            if is_ap2:
                ap2_obj = dev.cast_object  # type: ignore[assignment]
                stored = _ap2.get_stored_credentials(ap2_obj.identifier)
                _ap2_dbg(
                    f"ap2 device: id={ap2_obj.identifier!r} "
                    f"requires_pairing={ap2_obj.requires_pairing} "
                    f"stored_creds_len={len(stored)}"
                )
            if (
                is_ap2
                and dev.cast_object.requires_pairing
                and not _ap2.get_stored_credentials(dev.cast_object.identifier)
            ):
                from modules.airplay_pairing import PairingDialog

                ap2_dev: _ap2.AirPlay2Device = dev.cast_object  # type: ignore[assignment]
                _ap2_dbg(f"launching pairing dialog for {ap2_dev.name!r}")
                creds = PairingDialog.run(self, ap2_dev)
                _ap2_dbg(f"pairing dialog returned: creds_len={len(creds)}")
                if not creds:
                    # User cancelled or pairing failed — resume the local
                    # stream (we stopped it up front) so the abandoned cast
                    # attempt doesn't leave the user on "Nothing playing" with
                    # dead audio. play_requested actually restarts mpv at
                    # np.position; playback_started would only re-render the UI.
                    if playing_now:
                        self.bus.play_requested.emit(np)
                    return
                # Successfully paired; fall through into the regular
                # cast path which will pick up the newly-stored creds
                # via _cast_to_airplay2 → play_url_sync.
            if playing_now:
                # AirPlay v1 is synchronous; route through start_track so
                # the per-type ladder lives in one place. on_done (i.e.
                # _on_cast_result) fires inline for AirPlay.
                _ap2_dbg(f"calling start_track url_len={len(np.stream_url)} title={np.title!r}")
                self.cast_manager.start_track(
                    dev, np, provider=get_provider(),
                    resume_seconds=resume_seconds, on_done=_on_cast_result,
                )
                return
            else:
                self.cast_manager.active_cast = dev
                _on_cast_result(True)
            return

    def _open_snapcast_control(self, dev):
        """Open the Snapcast control surface for the picked server.

        Snapcast is a multiroom routing matrix (groups → streams +
        per-room volume), not a play-this-track target, so it gets its own
        control dialog instead of the stop-local-playback + active_cast
        push flow the URL-push protocols use. ``dev.cast_object`` is the
        ``SnapcastServerInfo``; the dialog connects via the shared
        controller singleton and drives it from there."""
        from modules.cast.snapcast import get_snapcast_controller
        from modules.snapcast_control import SnapcastControlDialog

        existing = getattr(self, "_snapcast_dlg", None)
        if existing is not None and existing.isVisible():
            existing.raise_()
            existing.activateWindow()
            return
        server_info = dev.cast_object if dev.cast_object is not None else dev
        dlg = SnapcastControlDialog(get_snapcast_controller(), server_info, self)
        self._snapcast_dlg = dlg
        dlg.finished.connect(lambda _r: setattr(self, "_snapcast_dlg", None))
        self._position_dialog_above_now_playing(dlg)
        dlg.show()
