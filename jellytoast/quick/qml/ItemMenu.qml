// Long-press menu for an album / playlist / artist / track.
// open(item) where item = { kind, id, name, artist, ... }.
import QtQuick
import QtQuick.Controls

Menu {
    id: menu
    property var item: ({})
    signal goToArtist(var item)
    // Row-level queueing for tracks (set by the list that owns the row).
    property var queueRow: null

    function show(it, parentItem, x, y) {
        item = it
        popup(parentItem, x, y)
    }

    readonly property bool isTrack: item.kind === "track"
    readonly property bool radioable: ["album", "artist", "genre"].indexOf(item.kind) >= 0
    readonly property bool favable: (item.kind === "album" || item.kind === "artist") && item.id !== undefined
    signal favoriteChanged(string id, bool on)

    MenuItem {
        text: qsTr("Play")
        visible: !menu.isTrack && menu.item.kind !== "genre"
        height: visible ? implicitHeight : 0
        onTriggered: actions.run(menu.item.kind, menu.item.id, menu.item.name, "play")
    }
    MenuItem {
        text: qsTr("Shuffle")
        visible: !menu.isTrack && menu.item.kind !== "genre"
        height: visible ? implicitHeight : 0
        onTriggered: actions.run(menu.item.kind, menu.item.id, menu.item.name, "shuffle")
    }
    MenuItem {
        text: qsTr("Start radio")
        visible: menu.radioable
        height: visible ? implicitHeight : 0
        onTriggered: actions.startRadio(menu.item.kind, menu.item.id, menu.item.name)
    }
    MenuItem {
        text: qsTr("Play next")
        visible: menu.item.kind !== "genre"
        height: visible ? implicitHeight : 0
        onTriggered: menu.isTrack ? menu.queueRow(menu.item.row, "next")
                                  : actions.run(menu.item.kind, menu.item.id, menu.item.name, "next")
    }
    MenuItem {
        text: qsTr("Add to queue")
        visible: menu.item.kind !== "genre"
        height: visible ? implicitHeight : 0
        onTriggered: menu.isTrack ? menu.queueRow(menu.item.row, "end")
                                  : actions.run(menu.item.kind, menu.item.id, menu.item.name, "end")
    }
    MenuItem {
        text: menu.item.favorite ? qsTr("Remove from favorites") : qsTr("Add to favorites")
        visible: menu.favable
        height: visible ? implicitHeight : 0
        onTriggered: {
            actions.setFavorite(menu.item.id, !menu.item.favorite)
            menu.favoriteChanged(menu.item.id, !menu.item.favorite)
        }
    }
}
