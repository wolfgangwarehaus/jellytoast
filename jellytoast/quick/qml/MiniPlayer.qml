// Bottom mini player. Tap (or swipe up) to open Now Playing. While casting
// it says where, and its cast button opens the card with Stop casting.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: mini
    objectName: "miniPlayer"
    signal open()
    signal openCast()
    readonly property bool casting: cast.active !== ""
    // What the bar floats over (the page stack): shows through, frosted.
    property Item frostBehind: null

    height: player.hasTrack ? 76 : 0
    visible: player.hasTrack
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
            RowLayout {
                Layout.fillWidth: true
                spacing: 4
                Icon {
                    visible: mini.casting
                    name: cast.activeKind === "group" ? "speaker_group"
                          : cast.activeKind === "display" ? "tv" : "speaker"
                    tint: theme.accent
                    size: 14
                }
                Label {
                    objectName: "miniSubtitle"
                    Layout.fillWidth: true
                    text: mini.casting ? cast.active : player.artist
                    color: mini.casting ? theme.accent : theme.textDim
                    elide: Text.ElideRight
                    font.pixelSize: 13
                }
            }
        }
        IconButton {
            objectName: "miniCast"
            visible: mini.casting
            iconName: "cast"
            iconSize: 24
            tint: theme.accent
            onClicked: mini.openCast()
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
