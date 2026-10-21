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
    // Albums can show as a list too (⋮ → Show as list; the desktop setting).
    readonly property bool listMode: prefs.viewMode === "list" && lib.sortable
    readonly property bool round: kind === "artist"
    readonly property bool scrubbable: (kind === "album" || kind === "artist") && session.signedIn
    signal openItem(string kind, var item)

    readonly property int gutter: 12
    readonly property int columns: Math.max(2, Math.floor(width / 200))
    readonly property string itemKind: kind === "artist" ? "artist" : kind === "playlist" ? "playlist" : "album"

    LibraryModel { id: own; kind: page.kind; parentId: page.parentId }
    Component.onCompleted: if (!source && session.signedIn) own.reload()

    Label {
        anchors.centerIn: parent
        visible: session.signedIn && !page.lib.loading && page.lib.count === 0
        color: theme.textDim
        text: qsTr("Nothing here yet")
    }

    GridView {
        id: grid
        objectName: page.kind === "album" && !page.parentId ? "albumGrid" : "libraryGrid"
        anchors.fill: parent
        anchors.leftMargin: page.gutter
        anchors.rightMargin: page.gutter
        visible: session.signedIn && !page.listMode
        clip: true
        model: visible ? page.lib : null
        cellWidth: width / page.columns
        cellHeight: cellWidth + (page.round ? 34 : 46)
        cacheBuffer: cellHeight * 4
        boundsBehavior: Flickable.DragOverBounds
        maximumFlickVelocity: 8000
        flickDeceleration: 2500
        pixelAligned: true
        reuseItems: true
        bottomMargin: 8

        header: page.kind === "artistAlbums" ? artistHeader : page.kind === "favAlbums" ? favHeader : null

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
                onLongPressed: itemMenu.show(Object.assign(cell.info(), { favorite: page.lib.get(cell.index).favorite }),
                                             cell, point.position.x, point.position.y)
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
        id: favHeader
        Item {
            width: grid.width
            height: 64
            PillButton {
                objectName: "likedSongs"
                anchors.centerIn: parent
                iconName: "favorite_filled"
                text: qsTr("Liked songs")
                onClicked: page.openItem("likedSongs", {})
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

    ListView {
        id: rows
        objectName: "libraryList"
        anchors.fill: parent
        visible: session.signedIn && page.listMode
        clip: true
        model: visible ? page.lib : null
        boundsBehavior: Flickable.DragOverBounds
        maximumFlickVelocity: 8000
        flickDeceleration: 2500
        pixelAligned: true
        reuseItems: true
        cacheBuffer: 600
        delegate: ItemDelegate {
            id: lrow
            required property int index
            required property string itemId
            required property string name
            required property string subtitle
            required property string year
            required property string cover
            width: rows.width
            height: 68
            focusPolicy: Qt.NoFocus
            hoverEnabled: false
            leftPadding: 12
            rightPadding: 16
            background: Rectangle { color: theme.text; opacity: lrow.pressed ? 0.08 : 0 }
            function info() {
                return { kind: page.itemKind, id: lrow.itemId, name: lrow.name, artist: lrow.subtitle,
                         year: lrow.year, cover: lrow.cover }
            }
            onClicked: page.openItem(page.itemKind, lrow.info())
            onPressAndHold: itemMenu.show(Object.assign(lrow.info(), { favorite: page.lib.get(lrow.index).favorite }),
                                          lrow, width / 2, height / 2)
            contentItem: Row {
                spacing: 12
                Cover {
                    width: 52; height: 52
                    anchors.verticalCenter: parent.verticalCenter
                    radius: 6
                    pixels: 156
                    source: lrow.cover
                }
                Column {
                    anchors.verticalCenter: parent.verticalCenter
                    width: lrow.availableWidth - 64
                    spacing: 2
                    Label {
                        width: parent.width
                        text: lrow.name
                        color: theme.text
                        elide: Text.ElideRight
                        font.pixelSize: 15
                        font.weight: Font.Medium
                    }
                    Label {
                        width: parent.width
                        text: [lrow.subtitle, lrow.year].filter(s => s).join(" · ")
                        color: theme.textDim
                        elide: Text.ElideRight
                        font.pixelSize: 12
                    }
                }
            }
        }
        footer: Item {
            width: rows.width
            height: page.lib.loading ? 56 : 0
            BusyIndicator { anchors.centerIn: parent; running: page.lib.loading; visible: running }
        }
    }

    AzRail {
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        anchors.margins: 4
        // Letters mean something in name / album-artist order (not dates).
        visible: page.scrubbable && page.lib.scrubbable
        model: page.lib
        view: page.listMode ? rows : grid
    }

    ItemMenu {
        id: itemMenu
        onFavoriteChanged: (id, on) => {
            page.lib.markFavorite(id, on)
            if (page.kind === "favAlbums" && !on)
                page.lib.reload()
        }
    }
}
