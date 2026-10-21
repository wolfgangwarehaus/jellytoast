// The library's albums — two-column grid, paged loading.
import QtQuick
import QtQuick.Controls

Item {
    id: page
    property string title: qsTr("Albums")
    signal openAlbum(var album)

    readonly property int gutter: 12
    readonly property int columns: Math.max(2, Math.floor(width / 200))

    Label {
        anchors.centerIn: parent
        width: parent.width - 2 * page.gutter
        visible: !signedIn
        wrapMode: Text.WordWrap
        horizontalAlignment: Text.AlignHCenter
        color: theme.textDim
        text: qsTr("Sign in with the regular jellytoast app first, then relaunch.")
    }

    GridView {
        id: grid
        objectName: "albumGrid"
        anchors.fill: parent
        anchors.leftMargin: page.gutter
        anchors.rightMargin: page.gutter
        visible: signedIn
        clip: true
        model: signedIn ? albums : null
        cellWidth: width / page.columns
        cellHeight: cellWidth + 46
        cacheBuffer: cellHeight * 4
        boundsBehavior: Flickable.DragOverBounds
        maximumFlickVelocity: 8000
        flickDeceleration: 2500
        pixelAligned: true
        reuseItems: true
        bottomMargin: 8

        ScrollIndicator.vertical: ScrollIndicator { }

        delegate: Item {
            id: cell
            required property string albumId
            required property string name
            required property string artist
            required property string year
            required property string cover
            width: grid.cellWidth
            height: grid.cellHeight

            Column {
                anchors.fill: parent
                anchors.margins: 6
                spacing: 4
                scale: tap.pressed ? 0.97 : 1
                Behavior on scale { NumberAnimation { duration: 90 } }

                Cover {
                    width: parent.width
                    height: width
                    source: cell.cover
                }
                Label {
                    width: parent.width
                    text: cell.name
                    color: theme.text
                    elide: Text.ElideRight
                    font.pixelSize: 14
                    font.weight: Font.Medium
                }
                Label {
                    width: parent.width
                    text: cell.artist
                    color: theme.textDim
                    elide: Text.ElideRight
                    font.pixelSize: 12
                }
            }

            TapHandler {
                id: tap
                onTapped: page.openAlbum({ id: cell.albumId, name: cell.name, artist: cell.artist,
                                           year: cell.year, cover: cell.cover })
            }
        }

        footer: Item {
            width: grid.width
            height: albums.loading ? 56 : 0
            BusyIndicator {
                anchors.centerIn: parent
                running: albums.loading
                visible: running
            }
        }
    }
}
