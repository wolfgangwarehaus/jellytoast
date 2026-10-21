// Bottom mini player. Tap (or swipe up) to open Now Playing.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: mini
    objectName: "miniPlayer"
    signal open()

    height: player.hasTrack ? 76 : 0
    visible: player.hasTrack
    color: theme.surface

    Rectangle {  // progress hairline
        anchors.top: parent.top
        height: 2
        width: player.duration > 0 ? parent.width * player.position / player.duration : 0
        color: theme.accent
    }

    TapHandler { onTapped: mini.open() }
    DragHandler {
        id: drag
        target: null
        xAxis.enabled: false
        onActiveChanged: if (!active && centroid.position.y - centroid.pressPosition.y < -24) mini.open()
    }

    RowLayout {
        anchors.fill: parent
        anchors.leftMargin: 12
        anchors.rightMargin: 4
        spacing: 12

        Cover {
            Layout.preferredWidth: 52
            Layout.preferredHeight: 52
            radius: 6
            pixels: 160
            source: player.cover
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
        IconButton {
            iconName: player.playing ? "pause" : "play"
            iconSize: 28
            onClicked: player.playPause()
        }
        IconButton {
            iconName: "next"
            iconSize: 26
            onClicked: player.next()
        }
    }
}
