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

    MenuItem {
        text: qsTr("Play")
        visible: !menu.isTrack
        height: visible ? implicitHeight : 0
        onTriggered: actions.run(menu.item.kind, menu.item.id, menu.item.name, "play")
    }
    MenuItem {
        text: qsTr("Shuffle")
        visible: !menu.isTrack
        height: visible ? implicitHeight : 0
        onTriggered: actions.run(menu.item.kind, menu.item.id, menu.item.name, "shuffle")
    }
    MenuItem {
        text: qsTr("Play next")
        onTriggered: menu.isTrack ? menu.queueRow(menu.item.row, "next")
                                  : actions.run(menu.item.kind, menu.item.id, menu.item.name, "next")
    }
    MenuItem {
        text: qsTr("Add to queue")
        onTriggered: menu.isTrack ? menu.queueRow(menu.item.row, "end")
                                  : actions.run(menu.item.kind, menu.item.id, menu.item.name, "end")
    }
}
