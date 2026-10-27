// Long-press sheet for an album / playlist / artist / genre / track:
// the item up top (cover, name, artist), then what you can do with it.
// show(item) where item = { kind, id, name, artist, cover, favorite, row }.
import QtQuick
import QtQuick.Controls

Sheet {
    id: menu
    objectName: "itemSheet"
    property var item: ({})
    // Row-level queueing for tracks (set by the list that owns the row).
    property var queueRow: null
    // Set by a playlist's page: remove a song row from that playlist.
    property var removeRow: null
    signal favoriteChanged(string id, bool on)

    // Same signature as the old popup menu: the anchor is no longer used.
    function show(it, parentItem, x, y) {
        item = it
        open()
    }

    readonly property bool isTrack: item.kind === "track"
    readonly property bool radioable: ["album", "artist", "genre"].indexOf(item.kind) >= 0
    readonly property bool favable: (item.kind === "album" || item.kind === "artist") && item.id !== undefined
    readonly property bool playable: !isTrack && item.kind !== "genre"

    function run(fn) {
        fn()
        close()
    }

    // ── what this is ──
    Row {
        width: parent.width
        height: 64
        spacing: 14
        Cover {
            id: art
            visible: (menu.item.cover || "") !== ""
            width: 56; height: 56
            anchors.verticalCenter: parent.verticalCenter
            radius: menu.item.kind === "artist" ? 28 : 8
            glyph: menu.item.kind === "artist" ? "user" : "music_note"
            pixels: 168
            source: menu.item.cover || ""
        }
        Column {
            anchors.verticalCenter: parent.verticalCenter
            width: parent.width - (art.visible ? art.width + 14 : 0)
            spacing: 2
            Label {
                width: parent.width
                text: menu.item.name || ""
                color: theme.text
                elide: Text.ElideRight
                font.pixelSize: 17
                font.weight: Font.DemiBold
            }
            Label {
                width: parent.width
                visible: text !== ""
                text: menu.item.artist || ""
                color: theme.textDim
                elide: Text.ElideRight
                font.pixelSize: 13
            }
        }
    }
    Rectangle { width: parent.width; height: 1; color: theme.textFaint; opacity: 0.4 }

    // ── actions ──
    SheetRow {
        iconName: "play"
        text: qsTr("Play")
        visible: menu.playable
        onClicked: menu.run(() => actions.run(menu.item.kind, menu.item.id, menu.item.name, "play"))
    }
    SheetRow {
        iconName: "shuffle"
        text: qsTr("Shuffle")
        visible: menu.playable
        onClicked: menu.run(() => actions.run(menu.item.kind, menu.item.id, menu.item.name, "shuffle"))
    }
    SheetRow {
        iconName: "music_note"
        text: qsTr("Start radio")
        visible: menu.radioable
        onClicked: menu.run(() => actions.startRadio(menu.item.kind, menu.item.id, menu.item.name))
    }
    SheetRow {
        iconName: "next"
        text: qsTr("Play next")
        visible: menu.item.kind !== "genre"
        onClicked: menu.run(() => menu.isTrack ? menu.queueRow(menu.item.row, "next")
                                               : actions.run(menu.item.kind, menu.item.id, menu.item.name, "next"))
    }
    SheetRow {
        iconName: "queue"
        text: qsTr("Add to queue")
        visible: menu.item.kind !== "genre"
        onClicked: menu.run(() => menu.isTrack ? menu.queueRow(menu.item.row, "end")
                                               : actions.run(menu.item.kind, menu.item.id, menu.item.name, "end"))
    }
    SheetRow {
        iconName: menu.item.favorite ? "favorite_filled" : "favorite_outline"
        text: menu.item.favorite ? qsTr("Remove from favorites") : qsTr("Add to favorites")
        visible: menu.favable
        onClicked: menu.run(() => {
            actions.setFavorite(menu.item.id, !menu.item.favorite)
            menu.favoriteChanged(menu.item.id, !menu.item.favorite)
        })
    }
    SheetRow {
        objectName: "addToPlaylistRow"
        iconName: "list"
        text: qsTr("Add to playlist…")
        visible: playlists.available && ["track", "album", "playlist", "artist"].indexOf(menu.item.kind) >= 0
                 && (menu.item.id || "") !== ""
        onClicked: menu.run(() => addToPlaylistSheet.show(menu.item))
    }
    SheetRow {
        objectName: "removeFromPlaylistRow"
        iconName: "win_close"
        text: qsTr("Remove from playlist")
        visible: menu.isTrack && menu.removeRow !== null && playlists.available
        onClicked: menu.run(() => menu.removeRow(menu.item.row))
    }
    SheetRow {
        objectName: "renamePlaylistRow"
        iconName: "settings"
        text: qsTr("Rename playlist")
        visible: menu.item.kind === "playlist" && playlists.available
                 && menu.item.canDelete !== false
        onClicked: menu.run(() => playlistDialogs.rename(menu.item))
    }
    SheetRow {
        objectName: "deletePlaylistRow"
        iconName: "win_close"
        text: qsTr("Delete playlist")
        visible: menu.item.kind === "playlist" && playlists.available
                 && menu.item.canDelete !== false
        tint: "#e57373"
        onClicked: menu.run(() => playlistDialogs.confirmDelete(menu.item))
    }
    SheetRow {
        iconName: "download"
        text: qsTr("Download")
        visible: ["album", "playlist", "artist"].indexOf(menu.item.kind) >= 0
        onClicked: menu.run(() => actions.download(menu.item.id, menu.item.name))
    }
}
