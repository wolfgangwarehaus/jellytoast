// jellytoast — Qt Quick phone front end, M0 proof (see dev/QT_QUICK_PLAN.md).
// Albums grid + mini player over the shared core. Context properties from
// jellytoast/quick: `albums` (AlbumsModel), `player` (PlayerController),
// `theme` (Theme), `signedIn` (bool).
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ApplicationWindow {
    id: win
    width: 402
    height: 740
    visible: true
    title: "jellytoast"
    color: theme.background
    palette.buttonText: theme.text
    palette.windowText: theme.text

    readonly property int gutter: 12
    readonly property int columns: Math.max(2, Math.floor(width / 200))

    header: Item {
        height: 52
        Label {
            anchors.centerIn: parent
            text: qsTr("Albums")
            color: theme.text
            font.pixelSize: 18
            font.weight: Font.DemiBold
        }
    }

    Label {
        anchors.centerIn: parent
        width: parent.width - 2 * win.gutter
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
        anchors.leftMargin: win.gutter
        anchors.rightMargin: win.gutter
        visible: signedIn
        clip: true
        model: signedIn ? albums : null
        cellWidth: width / win.columns
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
            required property string cover
            width: grid.cellWidth
            height: grid.cellHeight

            Column {
                anchors.fill: parent
                anchors.margins: 6
                spacing: 4

                Rectangle {
                    width: parent.width
                    height: width
                    radius: 8
                    color: theme.surface
                    clip: true
                    Image {
                        anchors.fill: parent
                        source: cell.cover
                        sourceSize.width: 512
                        sourceSize.height: 512
                        fillMode: Image.PreserveAspectCrop
                        asynchronous: true
                        cache: true
                        opacity: status === Image.Ready ? 1 : 0
                        Behavior on opacity { NumberAnimation { duration: 140 } }
                    }
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
                onTapped: player.playAlbum(cell.albumId, cell.name)
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

    footer: Rectangle {
        id: mini
        objectName: "miniPlayer"
        height: player.hasTrack ? 72 : 0
        visible: player.hasTrack
        color: theme.surface

        Rectangle {  // progress hairline
            anchors.top: parent.top
            height: 2
            width: player.duration > 0 ? parent.width * player.position / player.duration : 0
            color: theme.accent
        }

        RowLayout {
            anchors.fill: parent
            anchors.leftMargin: win.gutter
            anchors.rightMargin: win.gutter
            spacing: 12

            Image {
                Layout.preferredWidth: 52
                Layout.preferredHeight: 52
                source: player.cover
                sourceSize.width: 156
                sourceSize.height: 156
                fillMode: Image.PreserveAspectCrop
                asynchronous: true
            }
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 2
                Label {
                    Layout.fillWidth: true
                    text: player.title
                    color: theme.text
                    elide: Text.ElideRight
                    font.pixelSize: 15
                    font.weight: Font.Medium
                }
                Label {
                    Layout.fillWidth: true
                    text: player.artist
                    color: theme.textDim
                    elide: Text.ElideRight
                    font.pixelSize: 13
                }
            }
            ToolButton {
                focusPolicy: Qt.NoFocus
                text: player.playing ? "⏸" : "▶"
                font.pixelSize: 22
                onClicked: player.playPause()
            }
            ToolButton {
                focusPolicy: Qt.NoFocus
                text: "⏭"
                font.pixelSize: 22
                onClicked: player.next()
            }
        }
    }
}
