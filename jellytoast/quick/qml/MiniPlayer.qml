// Bottom mini player. Tap (or swipe up) to open Now Playing.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Effects

Rectangle {
    id: mini
    objectName: "miniPlayer"
    signal open()
    // What the bar floats over (the page stack): shows through, frosted.
    property Item frostBehind: null

    height: player.hasTrack ? 76 : 0
    visible: player.hasTrack
    clip: true  // keeps the cover's glow inside the bar
    color: frostBehind ? "transparent" : theme.surface

    FrostedPane {
        anchors.fill: parent
        visible: mini.frostBehind !== null
        behind: mini.frostBehind
        tint: theme.surface
        tintOpacity: 0.55
    }

    // Progress: a rounded 3 px bar over a faint track, along the top edge.
    Rectangle {
        anchors.top: parent.top
        width: parent.width
        height: 3
        color: theme.textFaint
        opacity: 0.35
    }
    Rectangle {
        anchors.top: parent.top
        height: 3
        radius: 1.5
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

        Item {
            Layout.preferredWidth: 52
            Layout.preferredHeight: 52
            // A soft glow of the album's own colours behind the cover
            // (with Frosted glass; it's the cover itself, blurred).
            MultiEffect {
                anchors.centerIn: parent
                width: 84; height: 84
                visible: prefs.frosted && player.cover !== ""
                source: miniCover
                blurEnabled: true
                blur: 1.0
                blurMax: 32
                saturation: 0.3
                opacity: 0.75
            }
            Cover {
                id: miniCover
                anchors.fill: parent
                radius: 6
                pixels: 160
                source: player.cover
                layer.enabled: true
            }
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
