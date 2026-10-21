// A grid of albums / artists / playlists (or an artist's / genre's albums).
import QtQuick
import QtQuick.Controls
import Jellytoast

Item {
    id: page
    property string kind: "album"
    property string parentId: ""
    property string title: qsTr("Albums")
    property var source: null           // a ready LibraryModel (Albums uses the shared one)
    readonly property var lib: source ? source : own
    readonly property bool loading: lib.loading
    readonly property bool round: kind === "artist"
    readonly property bool scrubbable: (kind === "album" || kind === "artist") && signedIn
    signal openItem(string kind, var item)

    readonly property int gutter: 12
    readonly property int columns: Math.max(2, Math.floor(width / 200))
    readonly property string itemKind: kind === "artist" ? "artist" : kind === "playlist" ? "playlist" : "album"

    LibraryModel { id: own; kind: page.kind; parentId: page.parentId }
    Component.onCompleted: if (!source && signedIn) own.reload()

    Label {
        anchors.centerIn: parent
        width: parent.width - 2 * page.gutter
        visible: !signedIn
        wrapMode: Text.WordWrap
        horizontalAlignment: Text.AlignHCenter
        color: theme.textDim
        text: qsTr("Sign in with the regular jellytoast app first, then relaunch.")
    }
    Label {
        anchors.centerIn: parent
        visible: signedIn && !page.lib.loading && page.lib.count === 0
        color: theme.textDim
        text: qsTr("Nothing here yet")
    }

    GridView {
        id: grid
        objectName: page.kind === "album" && !page.parentId ? "albumGrid" : "libraryGrid"
        anchors.fill: parent
        anchors.leftMargin: page.gutter
        anchors.rightMargin: page.gutter
        visible: signedIn
        clip: true
        model: signedIn ? page.lib : null
        cellWidth: width / page.columns
        cellHeight: cellWidth + (page.round ? 34 : 46)
        cacheBuffer: cellHeight * 4
        boundsBehavior: Flickable.DragOverBounds
        maximumFlickVelocity: 8000
        flickDeceleration: 2500
        pixelAligned: true
        reuseItems: true
        bottomMargin: 8

        header: page.kind === "artistAlbums" ? artistHeader : null

        delegate: Item {
            id: cell
            required property int index
            required property string itemId
            required property string name
            required property string subtitle
            required property string year
            required property string cover
            width: grid.cellWidth
            height: grid.cellHeight

            function info() {
                return { kind: page.itemKind, id: cell.itemId, name: cell.name, artist: cell.subtitle,
                         year: cell.year, cover: cell.cover }
            }

            Column {
                anchors.fill: parent
                anchors.margins: 6
                spacing: 4
                scale: tap.pressed ? 0.97 : 1
                Behavior on scale { NumberAnimation { duration: 90 } }

                Cover {
                    width: parent.width
                    height: width
                    radius: page.round ? width / 2 : 8
                    source: cell.cover
                }
                Label {
                    width: parent.width
                    horizontalAlignment: page.round ? Text.AlignHCenter : Text.AlignLeft
                    text: cell.name
                    color: theme.text
                    elide: Text.ElideRight
                    font.pixelSize: 14
                    font.weight: Font.Medium
                }
                Label {
                    width: parent.width
                    visible: !page.round
                    text: page.kind === "artistAlbums" ? cell.year : cell.subtitle
                    color: theme.textDim
                    elide: Text.ElideRight
                    font.pixelSize: 12
                }
            }

            TapHandler {
                id: tap
                onTapped: page.openItem(page.itemKind, cell.info())
                onLongPressed: itemMenu.show(cell.info(), cell, point.position.x, point.position.y)
            }
        }

        footer: Item {
            width: grid.width
            height: page.lib.loading ? 56 : 0
            BusyIndicator {
                anchors.centerIn: parent
                running: page.lib.loading
                visible: running
            }
        }
    }

    Component {
        id: artistHeader
        Row {
            width: grid.width
            height: 64
            spacing: 12
            leftPadding: (width - 252) / 2
            PillButton {
                anchors.verticalCenter: parent.verticalCenter
                iconName: "play"; text: qsTr("Play"); filled: true
                onClicked: actions.run("artist", page.parentId, page.title, "play")
            }
            PillButton {
                anchors.verticalCenter: parent.verticalCenter
                iconName: "shuffle"; text: qsTr("Shuffle")
                onClicked: actions.run("artist", page.parentId, page.title, "shuffle")
            }
        }
    }

    AzRail {
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        anchors.margins: 4
        visible: page.scrubbable
        model: page.lib
        view: grid
    }

    ItemMenu { id: itemMenu }
}
