// jellytoast — Qt Quick phone front end (see dev/QT_QUICK_PLAN.md).
// Context properties from jellytoast/quick: `albums` (AlbumsModel),
// `player` (PlayerController), `theme` (Theme), `signedIn` (bool).
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
    palette.text: theme.text
    palette.window: theme.surface
    palette.base: theme.surface
    palette.highlight: theme.accent
    palette.highlightedText: theme.onAccent

    function openAlbum(album) {
        stack.push(albumPage, { album: album })
    }

    // Back: Now Playing first, then the page stack. (Esc / Back key / button.)
    function back() {
        if (nowPlaying.shown) {
            nowPlaying.close()
            return true
        }
        if (stack.depth > 1) {
            stack.pop()
            return true
        }
        return false
    }

    Shortcut {
        sequences: [StandardKey.Back, "Esc", "Back"]
        onActivated: win.back()
    }

    ColumnLayout {
        anchors.fill: parent
        spacing: 0

        Item {
            id: topBar
            Layout.fillWidth: true
            Layout.preferredHeight: 52
            IconButton {
                anchors.left: parent.left
                anchors.leftMargin: 4
                anchors.verticalCenter: parent.verticalCenter
                iconName: "back"
                visible: stack.depth > 1
                onClicked: win.back()
            }
            Label {
                anchors.centerIn: parent
                width: parent.width - 120
                horizontalAlignment: Text.AlignHCenter
                text: stack.currentItem ? stack.currentItem.title : ""
                color: theme.text
                elide: Text.ElideRight
                font.pixelSize: 18
                font.weight: Font.DemiBold
            }
        }

        StackView {
            id: stack
            objectName: "stack"
            Layout.fillWidth: true
            Layout.fillHeight: true
            clip: true
            initialItem: AlbumsPage {
                onOpenAlbum: album => win.openAlbum(album)
            }

            pushEnter: Transition {
                ParallelAnimation {
                    NumberAnimation { property: "x"; from: stack.width * 0.3; to: 0; duration: 220; easing.type: Easing.OutCubic }
                    NumberAnimation { property: "opacity"; from: 0; to: 1; duration: 160 }
                }
            }
            pushExit: Transition {
                NumberAnimation { property: "opacity"; from: 1; to: 0; duration: 160 }
            }
            popEnter: Transition {
                NumberAnimation { property: "opacity"; from: 0; to: 1; duration: 160 }
            }
            popExit: Transition {
                ParallelAnimation {
                    NumberAnimation { property: "x"; from: 0; to: stack.width * 0.3; duration: 200; easing.type: Easing.InCubic }
                    NumberAnimation { property: "opacity"; from: 1; to: 0; duration: 200 }
                }
            }
        }

        MiniPlayer {
            Layout.fillWidth: true
            Layout.preferredHeight: height
            onOpen: nowPlaying.open()
        }
    }

    Component {
        id: albumPage
        AlbumPage { }
    }

    NowPlaying {
        id: nowPlaying
        width: win.width
        height: win.height
        z: 10
        onGoToAlbum: album => {
            var cur = stack.currentItem
            if (!(cur && cur.album && cur.album.id === album.id))
                win.openAlbum(album)
        }
    }
}
