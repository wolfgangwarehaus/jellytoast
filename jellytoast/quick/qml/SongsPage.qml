// Every song, A–Z. Tap plays from that song; swipe left queues it.
import QtQuick
import QtQuick.Controls
import Jellytoast

Item {
    id: page
    property string kind: "song"  // | "favSongs"
    property string title: qsTr("Songs")
    readonly property bool loading: lib.loading
    LibraryModel { id: lib; kind: page.kind }
    Component.onCompleted: if (session.signedIn) lib.reload()

    ListView {
        id: list
        objectName: "songList"
        anchors.fill: parent
        clip: true
        model: session.signedIn ? lib : null
        boundsBehavior: Flickable.DragAndOvershootBounds
        maximumFlickVelocity: theme.scrollMaxVelocity
        flickDeceleration: theme.scrollDeceleration
        pressDelay: 100  // a scroll that starts at once never "presses" the item under it
        reuseItems: true
        cacheBuffer: 600

        delegate: TrackRow {
            required property int index
            required property string itemId
            required property string name
            required subtitle
            required cover
            width: list.width
            trackId: itemId
            title: name
            showCover: true
            onClicked: lib.playFrom(index)
            onQueued: { lib.queueRow(index, "end"); toast.show(qsTr("Added to queue")) }
            onMenuRequested: (x, y) => menu.show({ kind: "track", id: itemId, row: index, name: name, artist: subtitle, cover: cover }, this, x, y)
        }
        footer: Item {
            width: list.width
            height: lib.loading ? 56 : 0
            BusyIndicator { anchors.centerIn: parent; running: lib.loading; visible: running }
        }
    }
    AzRail {
        visible: page.kind === "song"
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        anchors.topMargin: 4
        anchors.bottomMargin: 4
        // Centred in the rows' 16 px right padding.
        inset: 8
        model: lib
        view: list
    }
    ItemMenu {
        id: menu
        queueRow: (row, mode) => {
            lib.queueRow(row, mode)
            toast.show(mode === "next" ? qsTr("Playing next") : qsTr("Added to queue"))
        }
    }
}
