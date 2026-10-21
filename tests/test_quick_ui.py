"""Qt Quick front end, driven like a person would (tests/quick_support.py)."""

from __future__ import annotations

from jellytoast.player_state import PlayerBus


def test_tap_album_opens_its_page_and_back_returns(quick):
    quick.tap("text:Album 001")
    quick.wait_for("trackList")
    quick.wait_idle()
    s = quick.state()
    assert s["depth"] == 2 and s["page"] == "AlbumPage"
    assert quick.find("text:Track 3 of Album 001") is not None

    quick.key("esc")
    quick.wait_idle()
    assert quick.state()["depth"] == 1


def test_track_tap_queues_whole_album_from_that_track(quick):
    got = []
    bus = PlayerBus.get()
    slot = lambda items, i, ctx: got.append((items, i, ctx))  # noqa: E731
    bus.queue_play_now.connect(slot)
    try:
        quick.tap("text:Album 002")
        quick.wait_for("text:Track 4 of Album 002")
        quick.tap("text:Track 4 of Album 002")
        try:
            quick.wait_until(lambda: got, 5)
        except Exception:
            row = quick.find("text:Track 4 of Album 002")
            raise AssertionError(
                f"no queue_play_now; blocked={bus.signalsBlocked()} state={quick.state()} "
                f"row={row and quick.point(row)} grabber={quick.window.mouseGrabberItem()} "
                f"active={quick.window.activeFocusItem()}\n{quick.tree()}"
            ) from None
    finally:
        bus.queue_play_now.disconnect(slot)
    items, idx, ctx = got[-1]
    assert len(items) == 6 and idx == 3 and ctx.source_id == "al2"


def test_grid_pages_in_more_albums_on_scroll(quick):
    assert quick.state()["albums"]["count"] == 100
    quick.scroll_to_end("albumGrid")
    assert quick.state()["albums"]["count"] == 150


def test_mini_player_opens_now_playing_and_drag_down_closes(quick):
    assert quick.find("miniPlayer") is None  # nothing playing → hidden
    quick.fake_now_playing("Song", "Someone", "Album 004", album_id="al4")
    quick.wait_for("miniPlayer")
    quick.tap("text:Someone")
    quick.wait_until(lambda: quick.state()["nowPlaying"])
    quick.drag("npCover", dy=420, ms=300)
    quick.wait_until(lambda: not quick.state()["nowPlaying"])


def test_now_playing_go_to_album(quick):
    quick.fake_now_playing("Song", "Someone", "Album 004", album_id="al4")
    quick.wait_for("miniPlayer")
    quick.tap("text:Someone")
    quick.wait_until(lambda: quick.state()["nowPlaying"])
    quick.tap("npMore")
    quick.tap("text:Go to album")
    quick.wait_until(lambda: not quick.state()["nowPlaying"])
    quick.wait_for("text:Track 1 of Album 004")
    assert quick.state()["page"] == "AlbumPage"


def test_seek_drag_requests_position(quick):
    got = []
    bus = PlayerBus.get()
    bus.seek_requested.connect(got.append)
    try:
        quick.fake_now_playing(duration_ms=200_000, position_ms=0)
        quick.wait_for("miniPlayer")
        quick.tap("text:Test Artist")
        quick.wait_until(lambda: quick.state()["nowPlaying"])
        bar = quick.wait_still("seekBar")
        p = bar.mapToScene(bar.boundingRect().topLeft())
        quick.tap((p.x() + bar.width() / 2, p.y() + 12))
        quick.wait_until(lambda: got)
    finally:
        bus.seek_requested.disconnect(got.append)
    assert 90_000 < got[-1] < 110_000


# ── M2: sections, search, gestures, menus, queue / lyrics ──


class _Spy:
    """Collect PlayerBus emissions for the duration of a test."""

    def __init__(self, *names):
        self.bus = PlayerBus.get()
        self.got = {n: [] for n in names}
        self._slots = []
        for n in names:
            slot = lambda *a, n=n: self.got[n].append(a)  # noqa: E731
            getattr(self.bus, n).connect(slot)
            self._slots.append((n, slot))

    def close(self):
        for n, slot in self._slots:
            getattr(self.bus, n).disconnect(slot)


def _options(quick, *targets):
    """⋮ → the options sheet, then tap each target (chips keep it open)."""
    if not quick.eval("optionsSheet.opened"):
        quick.tap("moreButton")
        quick.wait_until(lambda: quick.eval("optionsSheet.opened"))
    for t in targets:
        quick.tap(t)


def _close_options(quick):
    quick.eval("optionsSheet.close()")
    quick.wait_until(lambda: not quick.eval("optionsSheet.visible"))


def _open_settings(quick):
    _options(quick, "settingsItem")


def _section(quick, label):
    """Albums ⌄ → the section sheet → the row labelled ``label``."""
    key = quick.eval(f"win.sections.find(s => s.label === {label!r}).key")
    quick.tap("sectionButton")
    quick.wait_until(lambda: quick.eval("sectionSheet.opened"))
    quick.tap(f"section_{key}")
    quick.wait_until(lambda: not quick.eval("sectionSheet.visible"))
    quick.wait_idle()


def test_sections_menu_switches_root_page(quick):
    _section(quick, "Artists")
    assert quick.state()["title"] == "Artists" and quick.state()["depth"] == 1
    quick.wait_for("text:Boards of Canada")
    _section(quick, "Songs")
    assert quick.state()["page"] == "SongsPage"
    _section(quick, "Albums")
    assert quick.state()["page"] == "LibraryGridPage" and quick.state()["title"] == "Albums"


def test_artist_opens_their_albums_with_play(quick):
    _section(quick, "Artists")
    quick.tap("text:Caribou")
    quick.wait_idle()
    s = quick.state()
    assert s["depth"] == 2 and s["title"] == "Caribou"
    # Caribou is artist 3 → albums 3, 9, 15, … (newest first)
    quick.wait_for("text:Album 0")
    shown = [
        int(it.property("text")[6:])
        for it in quick.items()
        if str(it.property("text") or "").startswith("Album ") and quick._on_screen(it)
    ]
    assert shown and all(n % 6 == 3 for n in shown)
    spy = _Spy("queue_play_now")
    try:
        quick.tap("text:Play")
        quick.wait_until(lambda: spy.got["queue_play_now"])
    finally:
        spy.close()
    items, start, ctx = spy.got["queue_play_now"][-1]
    assert start == 0 and ctx.source_id == "ar3" and len(items) == 25 * 6


def test_playlist_opens_its_tracks(quick):
    _section(quick, "Playlists")
    quick.tap("text:Playlist 2")
    quick.wait_for("trackList")
    quick.wait_idle()
    assert ("get_playlist_items", "pl2") in quick.provider.calls
    assert quick.find("text:5 songs") is not None


def test_song_tap_plays_from_there(quick):
    _section(quick, "Songs")
    first = quick.provider.songs[2]["Name"]
    spy = _Spy("queue_play_now")
    try:
        quick.tap(f"text:{first}")
        quick.wait_until(lambda: spy.got["queue_play_now"])
    finally:
        spy.close()
    items, start, _ctx = spy.got["queue_play_now"][-1]
    assert start == 2 and items[2]["Name"] == first


def test_genre_shows_only_its_albums(quick):
    _section(quick, "Genres")
    quick.tap("text:Pop")
    quick.wait_idle()
    quick.wait_for("text:Album 002")  # Pop = every 4th album from 2
    assert quick.state()["title"] == "Pop"
    assert quick.find("text:Album 001") is None


def test_search_finds_and_opens_album(quick):
    quick.tap("searchButton")
    quick.wait_idle()
    quick.type("Album 01")
    quick.wait_for("text:Album 012", timeout=5)
    quick.tap("text:Album 012")
    quick.wait_for("trackList")
    quick.wait_idle()
    assert quick.state()["page"] == "AlbumPage" and quick.find("text:Track 1 of Album 012")


def test_swipe_track_left_adds_to_queue(quick):
    quick.tap("text:Album 001")
    quick.wait_for("text:Track 2 of Album 001")
    spy = _Spy("queue_add_end")
    try:
        row = quick.get("text:Track 2 of Album 001")
        p = quick.point(row)
        quick.drag((quick.window.width() - 30, p.y()), dx=-300, ms=250)
        quick.wait_until(lambda: spy.got["queue_add_end"])
    finally:
        spy.close()
    (items,) = spy.got["queue_add_end"][-1]
    assert [t["Name"] for t in items] == ["Track 2 of Album 001"]
    assert quick.find("text:Added to queue") is not None  # toast


def test_long_press_album_play_next(quick):
    spy = _Spy("queue_add_next")
    try:
        quick.long_press("text:Album 005")
        quick.tap("text:Play next")
        quick.wait_until(lambda: spy.got["queue_add_next"])
    finally:
        spy.close()
    (items,) = spy.got["queue_add_next"][-1]
    assert len(items) == 6 and items[0]["AlbumId"] == "al5"


def test_edge_swipe_goes_back(quick):
    quick.tap("text:Album 001")
    quick.wait_for("trackList")
    quick.wait_idle()
    quick.edge_swipe()
    quick.wait_idle()
    assert quick.state()["depth"] == 1


def test_now_playing_lyrics_and_queue_surfaces(quick):
    quick.fake_now_playing("Song", "Someone", "Album 004", item_id="al4-t1", position_ms=12_000)
    tracks = quick.provider.get_album_tracks("al4")
    PlayerBus.get().queue_changed.emit(tracks, 0)
    quick.wait_for("miniPlayer")
    quick.tap("text:Someone")
    quick.wait_until(lambda: quick.state()["nowPlaying"])

    quick.tap("lyricsToggle")
    quick.wait_for("text:Line 2")
    assert ("get_lyrics", "al4-t1") in quick.provider.calls
    # 12 s in → line 2 (starts at 10 s) is the current one.
    assert quick.eval("lyrics.lineAt(player.position)") == 1

    quick.tap("queueToggle")
    quick.wait_for("text:Track 3 of Album 004")
    spy = _Spy("track_jumped")
    try:
        quick.tap("text:Track 3 of Album 004")
        quick.wait_until(lambda: spy.got["track_jumped"])
    finally:
        spy.close()
    assert spy.got["track_jumped"][-1] == (2,)

    quick.key("esc")  # back from the queue → cover
    assert quick.state()["nowPlaying"] and quick.eval("nowPlaying.surface") == "cover"


def test_section_is_remembered(quick):
    from jellytoast.settings import get_settings

    _section(quick, "Playlists")
    assert get_settings().quick_section == "playlist"


# ── M3: sign in / out ──


def _signed_out(qapp, fake_provider, monkeypatch):
    """A `quick` rig that starts signed out."""
    fake_provider.is_authenticated = False
    monkeypatch.setattr("jellytoast.providers.reset_provider", lambda: None)
    from jellytoast.scrobble import server_scrobble_detect  # noqa: F401 — import before patching

    monkeypatch.setattr("jellytoast.scrobble.refresh_server_scrobble_flags", lambda: None)


def test_sign_in_flow(qapp, fake_provider, monkeypatch, request):
    _signed_out(qapp, fake_provider, monkeypatch)
    quick = request.getfixturevalue("quick")
    assert quick.state()["page"] == "SignInPage"
    assert quick.find("sectionButton") is None and quick.find("searchButton") is None

    quick.tap("serverField")
    quick.eval("stack.currentItem")  # page is up
    quick.get("serverField").setProperty("text", "music.example")
    quick.get("userField").setProperty("text", "august")
    quick.get("passwordField").setProperty("text", "wrong")
    quick.tap("signInButton")
    quick.wait_until(lambda: quick.prop("signInError", "text"))
    assert "password" in quick.prop("signInError", "text").lower()
    assert ("probe", "http://music.example") in fake_provider.calls

    quick.get("passwordField").setProperty("text", "secret")
    quick.tap("signInButton")
    quick.wait_until(lambda: quick.state()["page"] == "LibraryGridPage")
    quick.wait_for("text:Album 000")


def test_rig_without_null_keyring_refuses_sign_in(qapp, fake_provider, monkeypatch, request):
    _signed_out(qapp, fake_provider, monkeypatch)
    monkeypatch.setenv("JT_RIG", "1")
    monkeypatch.setenv("PYTHON_KEYRING_BACKEND", "keyring.backends.SecretService.Keyring")
    quick = request.getfixturevalue("quick")
    quick.get("serverField").setProperty("text", "music.example")
    quick.get("userField").setProperty("text", "august")
    quick.get("passwordField").setProperty("text", "secret")
    quick.tap("signInButton")
    quick.wait_until(lambda: quick.prop("signInError", "text"))
    assert "dev rig" in quick.prop("signInError", "text")
    assert not any(c[0] == "authenticate" for c in fake_provider.calls)


def test_settings_change_quality_and_sign_out(quick, monkeypatch):
    from jellytoast.settings import get_settings

    monkeypatch.setattr("jellytoast.providers.reset_provider", lambda: None)
    _open_settings(quick)
    quick.wait_for("settingsPage")
    quick.wait_idle()
    quick.tap("qualityRow")
    quick.tap("text:192 kbps")
    assert get_settings().audio_quality == "192"
    spy = _Spy("replaygain_changed")
    try:
        quick.tap("normalizationRow")
        quick.tap("text:Per album")
    finally:
        spy.close()
    assert spy.got["replaygain_changed"][-1] == ("album",)

    quick.tap("signOutButton")
    quick.wait_for("signOutDialog")
    quick.tap("text:OK")
    quick.wait_until(lambda: quick.state()["page"] == "SignInPage")
    assert ("server_logout",) in quick.provider.calls


# ── M3: favourites, radio ──


def test_favorites_section_and_liked_songs(quick):
    _section(quick, "Favorites")
    quick.wait_for("text:Album 010")
    assert quick.find("text:Album 001") is None  # only every 10th is a favourite
    quick.tap("likedSongs")
    quick.wait_for("songList")
    quick.wait_idle()
    assert quick.state()["title"] == "Liked songs"
    assert quick.find("text:Track 1 of Album 000") is not None
    assert quick.find("text:Track 2 of Album 000") is None


def test_long_press_start_radio_and_favorite(quick):
    spy = _Spy("queue_play_now")
    try:
        quick.long_press("text:Album 003")
        quick.tap("text:Start radio")
        quick.wait_until(lambda: spy.got["queue_play_now"])
    finally:
        spy.close()
    assert ("get_instant_mix", "al3") in quick.provider.calls
    _items, _start, ctx = spy.got["queue_play_now"][-1]
    assert ctx.source_id == "al3"

    quick.long_press("text:Album 003")
    quick.tap("text:Add to favorites")
    quick.wait_until(lambda: ("toggle_favorite", "al3", True) in quick.provider.calls)
    quick.long_press("text:Album 003")
    assert quick.find("text:Remove from favorites") is not None


# ── M3: cast picker ──


class _FakeCastManager:
    def __init__(self):
        from types import SimpleNamespace

        from jellytoast.cast_manager import CastType

        self.devices = [
            SimpleNamespace(
                name="Living Room TV", uuid="tv1", device_type=CastType.DLNA, cast_object=None
            ),
            SimpleNamespace(
                name="Kitchen", uuid="k1", device_type=CastType.CHROMECAST, cast_object=None
            ),
        ]
        self._on_update = None
        self.active_cast = None
        self.last_cast_failure = None
        self.started = []

    def set_devices_callback(self, cb):
        self._on_update = cb

    def get_all_devices(self):
        return list(self.devices)

    def discover_all(self):
        if self._on_update:
            self._on_update(self.get_all_devices())

    def start_track(self, dev, np, *, provider, resume_seconds, on_done):
        self.started.append((dev.name, resume_seconds))
        on_done(False)  # the TV refused

    def stop_cast(self):
        self.active_cast = None


def test_cast_sheet_lists_devices_and_reports_failure(quick, monkeypatch):
    from jellytoast.quick.cast import Cast
    from jellytoast.settings import get_settings

    monkeypatch.setattr(get_settings(), "cast_dlna_enabled", True, raising=False)
    cm = _FakeCastManager()
    cast = Cast(cm, quick.engine)
    quick.engine.rootContext().setContextProperty("cast", cast)

    quick.fake_now_playing("Song", "Someone", position_ms=30_000)
    from jellytoast.player_state import get_now_playing

    get_now_playing().stream_url = "http://server/stream"
    spy = _Spy("stop_requested", "play_requested")
    try:
        quick.wait_for("miniPlayer")
        quick.tap("text:Someone")
        quick.wait_until(lambda: quick.state()["nowPlaying"])
        quick.tap("castButton")
        quick.wait_for("text:Living Room TV")
        assert quick.find("text:Kitchen") is not None
        target = quick.get("text:Living Room TV")
        p = quick.point(target)
        rows = [
            (i.property("label"), quick.point(i).y(), i.isVisible())
            for i in quick.items()
            if i.property("label") in ("Living Room TV", "Kitchen", "This device")
        ]
        quick.tap("text:Living Room TV")
        quick.wait_for("castFailed")
    finally:
        spy.close()
    assert cm.started == [("Living Room TV", 30.0)], f"tapped {p}; rows {rows}"
    assert quick.find("text:Could not cast to Living Room TV") is not None
    # The failed hand-off resumed local playback.
    assert spy.got["stop_requested"] and spy.got["play_requested"]


# ── M3: downloads ──


class _FakeOffline:
    """Stands in for jellytoast.offline's public API."""

    def __init__(self):
        self.rows = [
            {
                "item_id": "al7",
                "kind": "album",
                "state": "complete",
                "name": "Album 007",
                "metadata": {"Name": "Album 007", "AlbumArtist": "Air"},
            },
            {
                "item_id": "pl1",
                "kind": "playlist",
                "state": "downloading",
                "name": "Playlist 1",
                "metadata": {"Name": "Playlist 1"},
            },
        ]
        self.downloaded, self.removed, self.paused = [], [], False

    def list_downloads(self, kind=None):
        return [dict(r) for r in self.rows]

    def item_size(self, item_id):
        return 52_428_800 if item_id == "al7" else 0

    def storage_usage(self):
        return {"total": 52_428_800}

    def is_paused(self):
        return self.paused

    def download(self, item):
        self.downloaded.append(item["Id"])

    def remove(self, item_id):
        self.removed.append(item_id)

    def pause(self):
        self.paused = True
        PlayerBus.get().download_queue_paused.emit()

    def resume(self):
        self.paused = False
        PlayerBus.get().download_queue_resumed.emit()


def _patch_offline(monkeypatch):
    import jellytoast.offline as offline

    fake = _FakeOffline()
    for name in (
        "list_downloads",
        "item_size",
        "storage_usage",
        "is_paused",
        "download",
        "remove",
        "pause",
        "resume",
    ):
        monkeypatch.setattr(offline, name, getattr(fake, name))
    return fake


def test_long_press_download_album(quick, monkeypatch):
    fake = _patch_offline(monkeypatch)
    quick.long_press("text:Album 004")
    quick.tap("text:Download")
    quick.wait_until(lambda: fake.downloaded)
    assert fake.downloaded == ["al4"]
    assert quick.find("text:Downloading Album 004") is not None  # toast


def test_downloads_page_progress_pause_and_remove(quick, monkeypatch):
    fake = _patch_offline(monkeypatch)
    _section(quick, "Downloads")
    quick.wait_for("text:Album 007")
    assert quick.find("text:50.0 MB on this device") is not None
    assert quick.find("text:Air · 50.0 MB") is not None

    # Live progress reaches the row; completion swaps the bar for the size.
    PlayerBus.get().download_progress.emit("pl1", "downloading", 0.5)
    from jellytoast.quick.rig import pump

    pump(250)
    quick.tap("pauseDownloads")
    assert fake.paused and quick.find("text:Resume") is not None

    row = quick.get("text:Playlist 1")
    p = quick.point(row)
    quick.drag((quick.window.width() - 30, p.y()), dx=-320, ms=250)
    quick.wait_until(lambda: fake.removed)
    assert fake.removed == ["pl1"]
    assert quick.find("text:Playlist 1") is None


def test_sleep_timer_menu(quick):
    spy = _Spy("sleep_timer_requested", "sleep_timer_cancel_requested")
    try:
        quick.fake_now_playing()
        quick.wait_for("miniPlayer")
        quick.tap("text:Test Artist")
        quick.wait_until(lambda: quick.state()["nowPlaying"])
        quick.tap("sleepButton")
        quick.tap("text:30 minutes")
        assert spy.got["sleep_timer_requested"][-1] == (1800, "end_of_track")
        PlayerBus.get().sleep_timer_started.emit(1800)  # the backend's answer
        quick.wait_until(lambda: 1790 <= quick.eval("player.sleepLeft") <= 1800)
        assert quick.find("text:29:") or quick.find("text:30:00")
        quick.tap("sleepButton")
        quick.tap("text:Turn off timer")
        assert spy.got["sleep_timer_cancel_requested"]
    finally:
        spy.close()


def test_now_playing_format_line(quick):
    quick.fake_now_playing("Song", "Someone")
    quick.wait_for("miniPlayer")
    quick.tap("text:Someone")
    quick.wait_until(lambda: quick.state()["nowPlaying"])
    bus = PlayerBus.get()
    bus.streaming_info_updated.emit("flac", 1411)
    quick.wait_until(lambda: "FLAC · 1411 kbps" in (quick.prop("formatLine", "text") or ""))
    assert quick.prop("formatLine", "text").startswith("Streaming")
    bus.cast_started.emit("Kitchen")
    quick.wait_until(lambda: quick.prop("formatLine", "text") == "Casting to Kitchen")
    bus.cast_stopped.emit()
    quick.wait_until(lambda: "FLAC" in quick.prop("formatLine", "text"))


# ── M3: offline browsing ──


def _offline_library(monkeypatch, on=True):
    """Downloads index: albums 7 and 12 (with tracks), Air as an artist."""
    import jellytoast.offline as offline
    from tests.quick_support import FakeProvider

    lib = FakeProvider()
    albums = {a["Id"]: a for a in lib.albums if a["Id"] in ("al7", "al12")}
    tracks = {aid: lib.get_album_tracks(aid)[:3] for aid in albums}
    state = {"on": on}

    def list_complete_items(kind=None):
        if kind == "album":
            return [dict(a) for a in albums.values()]
        if kind == "track":
            return [dict(t) for ts in tracks.values() for t in ts]
        return []

    monkeypatch.setattr(offline, "is_offline_mode", lambda: state["on"])
    monkeypatch.setattr(offline, "offline_source", lambda: "user")
    monkeypatch.setattr(offline, "list_complete_items", list_complete_items)
    monkeypatch.setattr(
        offline, "child_snapshots", lambda cid, kind=None: [dict(t) for t in tracks.get(cid, [])]
    )
    return state


def test_offline_mode_shows_downloads_only(quick, monkeypatch):
    state = _offline_library(monkeypatch, on=False)
    state["on"] = True
    PlayerBus.get().offline_mode_changed.emit(True)
    quick.wait_for("offlineBanner")
    quick.wait_for("text:Album 007")
    quick.wait_idle()
    assert quick.state()["albums"]["count"] == 2
    assert quick.find("text:Album 001") is None

    quick.tap("text:Album 012")
    quick.wait_for("text:Track 3 of Album 012")
    assert quick.find("text:Track 4 of Album 012") is None  # only what's downloaded
    quick.key("esc")
    quick.wait_idle()

    _section(quick, "Artists")
    quick.wait_for("text:Air")  # synthesized from the downloaded albums' artists

    quick.tap("searchButton")
    quick.wait_idle()
    quick.type("012")
    quick.wait_for("text:Track 1 of Album 012", timeout=5)
    assert not any(c[0] == "search_all" for c in quick.provider.calls)

    state["on"] = False
    PlayerBus.get().offline_mode_changed.emit(False)
    quick.wait_until(lambda: quick.find("offlineBanner") is None)


def test_theme_switch_recolours_live(quick):
    from jellytoast.settings import get_settings
    from jellytoast.theme_presets import THEME_FAMILIES, apply_theme_family

    s = get_settings()
    saved = (s.theme_family or "jellytoast", s.theme_mode or "dark", bool(s.frosted))
    try:
        before = quick.eval("theme.background.toString()")
        _open_settings(quick)
        quick.wait_for("settingsPage")
        quick.wait_idle()
        # Switch to whichever luminance isn't current (another test may
        # have left it on light).
        target = "dark" if (s.theme_mode or "dark") == "light" else "light"
        quick.tap("themeModeRow")
        quick.tap("text:" + target.capitalize())
        assert s.theme_mode == target
        quick.wait_until(lambda: quick.eval("theme.background.toString()") != before)
        labels = [f.label for f in THEME_FAMILIES.values()]
        fam_key, fam = next(
            (k, f)
            for k, f in THEME_FAMILIES.items()
            if not any(o != f.label and o.startswith(f.label) for o in labels)
            and not any(f.label != o and f.label in o for o in labels)
        )
        quick.tap("themeRow")
        quick.tap(f"text:{fam.label}")
        assert s.theme_family == fam_key
    finally:
        apply_theme_family(*saved)


def test_home_rails_and_shuffle_library(quick):
    from jellytoast.player_state import QueueKind

    _section(quick, "Home")
    quick.wait_for("rail_latest")
    quick.wait_for("text:Album 149")  # newest first
    assert quick.find("rail_favAlbums") is not None
    spy = _Spy("queue_play_now")
    try:
        quick.tap("shuffleLibrary")
        quick.wait_until(lambda: spy.got["queue_play_now"])
    finally:
        spy.close()
    items, _start, ctx = spy.got["queue_play_now"][-1]
    assert ctx.kind == QueueKind.SHUFFLE and len(items) == 90
    quick.tap("text:Album 149")
    quick.wait_for("trackList")
    assert quick.state()["page"] == "AlbumPage"


def test_quick_connect_sign_in(qapp, fake_provider, monkeypatch, request):
    from jellytoast.quick.session import Session

    _signed_out(qapp, fake_provider, monkeypatch)
    monkeypatch.setattr(Session, "QC_POLL_MS", 400)  # code stays up ~0.8 s
    quick = request.getfixturevalue("quick")
    quick.get("serverField").setProperty("text", "jf.example")
    quick.tap("quickConnectButton")
    quick.wait_for("text:482915")
    quick.wait_until(lambda: quick.state()["page"] == "LibraryGridPage", timeout=5)
    assert ("qc_finish", "s3cret") in fake_provider.calls


def test_album_sort_menu(quick):
    from jellytoast.settings import get_settings

    s = get_settings()
    saved = (s.library_sort_by, s.library_sort_order)
    try:
        assert quick.find("azRail", visible=False) is not None
        _options(quick, "sortRow", "text:Date added")
        quick.wait_until(
            lambda: ("MusicAlbum", "DateCreated,SortName", "Ascending") in quick.provider.sorts
        )
        assert s.library_sort_by == "DateCreated,SortName"  # shared with the desktop library
        _options(quick, "sortDirection")
        _close_options(quick)
        quick.wait_for("text:Album 149")  # the fake reverses on Descending
        assert s.library_sort_order == "descending"
        assert not quick.find("azRail", visible=False).isVisible()  # no letters for dates
        _options(quick, "sortRow", "text:Album artist")
        _close_options(quick)
        quick.wait_until(lambda: quick.find("azRail", visible=False).isVisible())
    finally:
        s.library_sort_by, s.library_sort_order = saved


def test_more_menu_list_view_and_shuffle(quick):
    from jellytoast.settings import get_settings

    s = get_settings()
    saved = s.library_view_mode
    try:
        _options(quick, "view_list")
        _close_options(quick)
        quick.wait_for("libraryList")
        assert s.library_view_mode == "list"
        quick.wait_for("text:Album 000")
        quick.tap("text:Album 000")
        quick.wait_for("trackList")
        quick.key("esc")
        quick.wait_idle()
        _options(quick, "view_grid")
        _close_options(quick)
        quick.wait_for("albumGrid")
        spy = _Spy("queue_play_now")
        try:
            _options(quick, "shuffleAllRow")
            quick.wait_until(lambda: spy.got["queue_play_now"])
        finally:
            spy.close()
    finally:
        s.library_view_mode = saved


def test_library_picker_scopes_the_grid(quick):
    from jellytoast import library_selection as _ls

    libs = quick.engine.rootContext().contextProperty("libraries")
    libs.refresh()
    quick.wait_until(lambda: libs.property("multiple"))
    _options(quick, "libraryRow", "text:Vinyl rips")  # untick → only "Music" (libA, even albums)
    _close_options(quick)
    quick.wait_until(lambda: _ls.selected_ids() == ["libA"])
    quick.wait_for("text:Album 002")
    quick.wait_idle()
    assert quick.find("text:Album 001") is None
    _options(quick, "libraryRow", "libAll")
    _close_options(quick)
    quick.wait_for("text:Album 001")


def test_artist_page_album_sort(quick):
    from jellytoast.settings import get_settings

    s = get_settings()
    saved = (s.artist_album_sort, s.artist_album_sort_desc)
    try:
        s.artist_album_sort, s.artist_album_sort_desc = "year", True
        _section(quick, "Artists")
        quick.tap("text:Caribou")  # albums 3, 9, 15 … — years 1990 + n % 30
        quick.wait_for("text:Album 0")
        quick.wait_idle()

        def first():
            return quick.eval("stack.currentItem.lib.get(0).name")

        assert first() == "Album 027"  # newest first (2017), ties by name
        _options(quick, "sortDirection")  # oldest first
        quick.wait_until(lambda: first() == "Album 003")  # 1993
        _options(quick, "sortRow", "text:Name")
        quick.wait_until(lambda: first() == "Album 003")  # name, A → Z
        _options(quick, "sortDirection")
        quick.wait_until(lambda: first() == "Album 147")  # name, Z → A
        _close_options(quick)
        assert (s.artist_album_sort, s.artist_album_sort_desc) == ("name", True)
    finally:
        s.artist_album_sort, s.artist_album_sort_desc = saved


def test_artists_grid_or_list(quick):
    from jellytoast.settings import get_settings

    s = get_settings()
    saved = (s.artist_view_mode, s.library_view_mode)
    try:
        s.artist_view_mode, s.library_view_mode = "grid", "grid"
        _section(quick, "Artists")
        quick.wait_for("text:Caribou")
        _options(quick, "view_list")
        _close_options(quick)
        quick.wait_for("libraryList")
        assert s.artist_view_mode == "list" and s.library_view_mode == "grid"  # its own
        quick.tap("text:Caribou")
        quick.wait_idle()
        assert quick.state()["title"] == "Caribou"
        quick.key("esc")
        quick.wait_idle()
        _options(quick, "view_grid")
        _close_options(quick)
        quick.wait_for("libraryGrid")
        assert s.artist_view_mode == "grid"
    finally:
        s.artist_view_mode, s.library_view_mode = saved


# ── playlists: add, create, remove, rename, delete ──


def _sheet_row(quick, name):
    quick.wait_for(name)
    quick.tap(name)


def test_add_album_to_existing_playlist(quick):
    quick.long_press("text:Album 004")
    _sheet_row(quick, "addToPlaylistRow")
    quick.wait_for("text:Playlist 2")
    quick.tap("text:Playlist 2")
    quick.wait_until(lambda: any(c[0] == "add_to_playlist" for c in quick.provider.calls))
    call = next(c for c in quick.provider.calls if c[0] == "add_to_playlist")
    assert call[1] == "pl2" and len(call[2]) == 6  # the whole album
    quick.wait_for("text:Added 6 songs to Playlist 2")


def test_new_playlist_from_a_song(quick):
    quick.tap("text:Album 001")
    quick.wait_for("text:Track 2 of Album 001")
    quick.long_press("text:Track 2 of Album 001")
    _sheet_row(quick, "addToPlaylistRow")
    _sheet_row(quick, "newPlaylistRow")
    quick.get("newPlaylistName").setProperty("text", "Road trip")
    quick.tap("createPlaylist")
    quick.wait_until(lambda: any(c[0] == "create_playlist" for c in quick.provider.calls))
    call = next(c for c in quick.provider.calls if c[0] == "create_playlist")
    assert call[1] == "Road trip" and call[2] == ["al1-t2"]


def test_remove_song_from_playlist_page(quick):
    _section(quick, "Playlists")
    quick.tap("text:Playlist 1")
    quick.wait_for("trackList")
    quick.wait_idle()
    first = quick.find("text:Track")  # any track row
    name = first.property("text")
    quick.long_press(f"text:{name}")
    _sheet_row(quick, "removeFromPlaylistRow")
    quick.wait_until(lambda: any(c[0] == "remove_from_playlist" for c in quick.provider.calls))
    call = next(c for c in quick.provider.calls if c[0] == "remove_from_playlist")
    assert call[1] == "pl1" and len(call[2]) == 1
    assert quick.find(f"text:{name}") is None or quick.find("text:Removed from playlist")


def test_rename_and_delete_playlist(quick):
    _section(quick, "Playlists")
    quick.wait_for("text:Playlist 3")
    quick.long_press("text:Playlist 3")
    _sheet_row(quick, "renamePlaylistRow")
    quick.wait_for("renameDialog")
    quick.get("renameField").setProperty("text", "Gym")
    quick.tap("text:Save")
    quick.wait_until(lambda: ("rename_playlist", "pl3", "Gym") in quick.provider.calls)

    quick.long_press("text:Playlist 0")
    _sheet_row(quick, "deletePlaylistRow")
    quick.wait_for("deletePlaylistDialog")
    quick.tap("text:OK")
    quick.wait_until(lambda: ("delete_playlist", "pl0") in quick.provider.calls)
    quick.wait_until(lambda: quick.find("text:Playlist 0") is None)  # list refreshed


def test_now_playing_add_to_playlist(quick):
    quick.fake_now_playing("Song", "Someone", item_id="al3-t1", album_id="al3")
    quick.wait_for("miniPlayer")
    quick.tap("text:Someone")
    quick.wait_until(lambda: quick.state()["nowPlaying"])
    quick.tap("npMore")
    _sheet_row(quick, "npAddToPlaylist")
    quick.wait_for("text:Playlist 1")
    quick.tap("text:Playlist 1")
    quick.wait_until(lambda: ("add_to_playlist", "pl1", ["al3-t1"]) in quick.provider.calls)


def test_queue_drag_to_reorder(quick):
    quick.fake_now_playing("Song", "Someone", item_id="al4-t1")
    PlayerBus.get().queue_changed.emit(quick.provider.get_album_tracks("al4"), 0)
    quick.wait_for("miniPlayer")
    quick.tap("text:Someone")
    quick.wait_until(lambda: quick.state()["nowPlaying"])
    quick.tap("queueToggle")
    quick.wait_for("text:Track 2 of Album 004")
    quick.wait_still("text:Track 2 of Album 004")
    spy = _Spy("queue_move_item")
    try:
        row = quick.get("text:Track 2 of Album 004")
        handles = [
            h for h in quick.items() if h.objectName() == "dragHandle" and quick._on_screen(h)
        ]
        # the handle on the same row as "Track 2"
        ry = quick.point(row).y()
        handle = min(handles, key=lambda h: abs(quick.point(h).y() - ry))
        quick.drag(handle, dy=2 * 60 + 5, ms=400, hold_end=100)  # two rows down
        quick.wait_until(lambda: spy.got["queue_move_item"])
    finally:
        spy.close()
    assert spy.got["queue_move_item"][-1] == (1, 3)


# ── internet radio ──


def test_radio_play_add_remove(quick):
    from jellytoast.player_state import QueueKind

    _section(quick, "Radio")
    quick.wait_for("text:KEXP")
    spy = _Spy("queue_play_now")
    try:
        quick.tap("text:KEXP")
        quick.wait_until(lambda: spy.got["queue_play_now"])
    finally:
        spy.close()
    items, _i, ctx = spy.got["queue_play_now"][-1]
    assert (
        ctx.kind == QueueKind.INTERNET_RADIO
        and items[0]["streamUrl"] == "https://kexp.example/stream"
    )

    quick.tap("browsePopular")
    quick.wait_for("popularSheet")
    from jellytoast.radio_presets import POPULAR_STATIONS

    first = POPULAR_STATIONS[0]["name"]
    quick.tap(f"text:{first}")
    quick.wait_until(lambda: any(c[0] == "create_station" for c in quick.provider.calls))
    quick.wait_for(f"text:{first}")  # now in your list

    quick.long_press("text:KEXP")
    quick.tap("removeStation")
    quick.wait_until(lambda: ("delete_station", "r1") in quick.provider.calls)
    quick.wait_until(lambda: quick.find("text:KEXP") is None)


def test_now_playing_shows_live_radio(quick):
    from jellytoast import radio_state
    from jellytoast.player_state import QueueContext, QueueKind

    radio_state.init()
    quick.fake_now_playing("KEXP", "", item_id="r1")
    PlayerBus.get().queue_context_changed.emit(
        QueueContext(kind=QueueKind.INTERNET_RADIO, source_id="r1", source_label="KEXP")
    )
    quick.wait_for("miniPlayer")
    quick.tap("miniPlayer")
    quick.wait_until(lambda: quick.state()["nowPlaying"])
    quick.wait_for("liveBadge")
    assert quick.find("seekBar") is None
    PlayerBus.get().queue_context_changed.emit(QueueContext())  # back to music
    quick.wait_until(lambda: quick.find("liveBadge") is None)
