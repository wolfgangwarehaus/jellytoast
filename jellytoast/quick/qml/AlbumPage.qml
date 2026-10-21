// One album or playlist: header (cover, title, artist, play / shuffle) +
// its tracks. Swipe a track left to queue it; long-press for the menu.
import QtQuick
import QtQuick.Controls
import Jellytoast

Item {
    id: page
    property var album: ({})
    property string kind: album.kind === "playlist" ? "playlist" : "album"
    property string title: ""
    readonly property bool loading: tracks.loading
    signal openItem(string kind, var item)

    TracksModel { id: tracks; kind: page.kind }
    Component.onCompleted: tracks.load(album.id, album.name)

    ListView {
        id: list
        objectName: "trackList"
        anchors.fill: parent
        clip: true
        model: tracks
        boundsBehavior: Flickable.DragOverBounds
        maximumFlickVelocity: 8000
        flickDeceleration: 2500
        pixelAligned: true
        bottomMargin: 12

        header: Column {
            width: list.width
            topPadding: 4
            bottomPadding: 12
            spacing: 10

            Cover {
                anchors.horizontalCenter: parent.horizontalCenter
                width: Math.min(parent.width - 96, 320)
                height: width
                radius: 12
                source: page.album.cover || ""
            }
            Label {
                width: parent.width - 32
                anchors.horizontalCenter: parent.horizontalCenter
                horizontalAlignment: Text.AlignHCenter
                text: page.album.name || ""
                color: theme.text
                wrapMode: Text.WordWrap
                maximumLineCount: 2
                elide: Text.ElideRight
                font.pixelSize: 20
                font.weight: Font.DemiBold
            }
            Label {
                width: parent.width - 32
                anchors.horizontalCenter: parent.horizontalCenter
                horizontalAlignment: Text.AlignHCenter
                text: [page.kind === "album" ? page.album.artist : "", page.album.year,
                       (tracks ? tracks.summary : "")].filter(s => s).join(" · ")
                color: theme.textDim
                elide: Text.ElideRight
                font.pixelSize: 13
            }
            // Download · Play · Shuffle — Play centred, the icons at the edges.
            Item {
                width: parent.width
                height: 48
                IconButton {
                    objectName: "downloadAlbum"
                    anchors.left: parent.left
                    anchors.leftMargin: 8
                    anchors.verticalCenter: parent.verticalCenter
                    iconName: "download"
                    tint: theme.textDim
                    onClicked: actions.download(page.album.id, page.album.name)
                }
                PillButton {
                    objectName: "playAll"
                    anchors.centerIn: parent
                    iconName: "play"
                    text: qsTr("Play")
                    filled: true
                    enabled: list.count > 0
                    onClicked: tracks.play(0)
                }
                IconButton {
                    objectName: "shuffleAll"
                    anchors.right: parent.right
                    anchors.rightMargin: 8
                    anchors.verticalCenter: parent.verticalCenter
                    iconName: "shuffle"
                    tint: theme.textDim
                    enabled: list.count > 0
                    onClicked: tracks.shuffle()
                }
            }
            BusyIndicator {
                anchors.horizontalCenter: parent.horizontalCenter
                running: tracks ? tracks.loading : false
                visible: running
            }
        }

        delegate: TrackRow {
            required property int index
            required trackId
            required property string name
            required property string artist
            required number
            required length
            width: list.width
            title: name
            subtitle: artist
            onClicked: tracks.play(index)
            onQueued: { tracks.queueRow(index, "end"); toast.show(qsTr("Added to queue")) }
            onMenuRequested: (x, y) => menu.show({ kind: "track", row: index, name: name, artist: artist || page.album.artist || "", cover: page.album.cover || "" }, this, x, y)
        }
    }

    ItemMenu {
        id: menu
        queueRow: (row, mode) => {
            tracks.queueRow(row, mode)
            toast.show(mode === "next" ? qsTr("Playing next") : qsTr("Added to queue"))
        }
    }
}
