// A grid of albums / artists / playlists (or an artist's / genre's albums).
import QtQuick
import QtQuick.Controls
import Jellytoast

Item {
    // The top bar and mini player float over the page as frosted glass, so
    // content scrolls under them; these keep it clear at rest (see Main).
    readonly property real insetTop: StackView.view ? StackView.view.insetTop : 0
    readonly property real insetBottom: StackView.view ? StackView.view.insetBottom : 0
    id: page
    property string kind: "album"

    // The long-press menu's item: the tile's own fields plus the model's
    // live ones (favourite; for playlists, whether it's ours to delete and
    // the file it came from).
    function menuInfo(base, index) {
        var g = page.lib.get(index)
        return Object.assign(base, { favorite: g.favorite, canDelete: g.canDelete,
                                     sourceFile: g.sourceFile })
    }
    property string parentId: ""
    property string title: qsTr("Albums")
    property var source: null           // a ready LibraryModel (Albums uses the shared one)
    readonly property var lib: source ? source : own
    readonly property bool loading: lib.loading
    // Albums can show as a list too (⋮ → Show as list; the desktop setting).
    readonly property bool listMode: kind === "artist" ? prefs.artistViewMode === "list"
                                                       : prefs.viewMode === "list" && lib.sortable
    readonly property bool round: kind === "artist"
    readonly property bool scrubbable: (kind === "album" || kind === "artist") && session.signedIn
    signal openItem(string kind, var item)

    readonly property int gutter: 12
    // By width, and never so few that a tile is taller than ~¾ of the page:
    // in landscape three 250 px tiles left every title below the fold.
    readonly property int columns: Math.max(2, Math.floor(width / 200),
                                            Math.ceil((width - 2 * gutter) / Math.max(120, height * 0.75)))
    readonly property string itemKind: kind === "artist" ? "artist" : kind === "playlist" ? "playlist" : "album"

    LibraryModel { id: own; kind: page.kind; parentId: page.parentId }
    Component.onCompleted: if (!source && session.signedIn) own.reload()

    Label {
        anchors.centerIn: parent
        visible: session.signedIn && !page.lib.loading && page.lib.count === 0 && !page.lib.error
        color: theme.textDim
        text: qsTr("Nothing here yet")
    }
    // Couldn't reach the server (it keeps retrying on its own too).
    Column {
        objectName: "loadError"
        anchors.centerIn: parent
        spacing: 14
        visible: session.signedIn && page.lib.error && page.lib.count === 0
        Label {
            anchors.horizontalCenter: parent.horizontalCenter
            color: theme.textDim
            text: qsTr("Couldn't reach the server")
        }
        PillButton {
            objectName: "retryButton"
            anchors.horizontalCenter: parent.horizontalCenter
            text: qsTr("Try again")
            onClicked: page.lib.retry()
        }
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
        boundsBehavior: Flickable.DragAndOvershootBounds
        maximumFlickVelocity: theme.scrollMaxVelocity
        flickDeceleration: theme.scrollDeceleration
        pressDelay: 100  // a scroll that starts at once never "presses" the item under it
        reuseItems: true
        topMargin: page.insetTop
        bottomMargin: 8 + page.insetBottom

        // An artist with nothing to play gets no Play / Shuffle (they did
        // nothing over "Nothing here yet"); kept while loading so the page
        // doesn't jump when the albums land.
        header: page.kind === "artistAlbums" && (page.lib.count > 0 || page.lib.loading) ? artistHeader
              : page.kind === "favAlbums" ? favHeader : null

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
                    glyph: page.round ? "user" : "music_note"
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
                onLongPressed: itemMenu.show(page.menuInfo(cell.info(), cell.index),
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
        topMargin: page.insetTop
        bottomMargin: page.insetBottom
        model: visible ? page.lib : null
        boundsBehavior: Flickable.DragAndOvershootBounds
        maximumFlickVelocity: theme.scrollMaxVelocity
        flickDeceleration: theme.scrollDeceleration
        pressDelay: 100  // a scroll that starts at once never "presses" the item under it
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
            onPressAndHold: itemMenu.show(page.menuInfo(lrow.info(), lrow.index),
                                          lrow, width / 2, height / 2)
            contentItem: Row {
                spacing: 12
                Cover {
                    width: 52; height: 52
                    anchors.verticalCenter: parent.verticalCenter
                    radius: page.round ? width / 2 : 6
                    glyph: page.round ? "user" : "music_note"
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
        anchors.topMargin: 4 + page.insetTop
        anchors.bottomMargin: 4 + page.insetBottom
        // Centred in the gap right of the art: grid gutter + tile padding.
        inset: (page.gutter + 6) / 2
        // Letters mean something in name / album-artist order (not dates).
        visible: page.scrubbable && page.lib.scrubbable
        model: page.lib
        view: page.listMode ? rows : grid
        reversed: page.lib.sortable && page.lib.sortDescending
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
