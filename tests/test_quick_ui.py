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
        bar = quick.get("seekBar")
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


def _section(quick, label):
    quick.tap("sectionButton")
    quick.tap(f"text:{label}")
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
    quick.tap("settingsButton")
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
