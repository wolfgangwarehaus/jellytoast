// A song row: optional cover or track number, title / artist, length.
// Swipe left to add to the queue; long-press for the menu.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

SwipeDelegate {
    id: row
    property string trackId
    property string title
    property string subtitle
    property string length
    property int number: 0
    property string cover: ""
    property bool showCover: false
    // The placeholder glyph while the art is missing ("user" for an artist).
    property string coverGlyph: "music_note"
    readonly property bool current: trackId !== "" && trackId === player.itemId
    signal queued()
    signal menuRequested(real x, real y)

    height: 60
    focusPolicy: Qt.NoFocus
    hoverEnabled: false
    padding: 0
    leftPadding: 12
    rightPadding: 16

    background: Rectangle {
        color: theme.background
        Rectangle {
            anchors.fill: parent
            color: theme.text
            opacity: row.pressed ? 0.08 : 0
        }
    }

    swipe.right: Rectangle {
        width: parent.width
        height: parent.height
        color: theme.accent
        Row {
            anchors.right: parent.right
            anchors.rightMargin: 20
            anchors.verticalCenter: parent.verticalCenter
            spacing: 8
            Icon { name: "queue"; tint: theme.onAccent; size: 20; anchors.verticalCenter: parent.verticalCenter }
            Label { text: qsTr("Add to queue"); color: theme.onAccent; font.pixelSize: 14; font.weight: Font.DemiBold }
        }
    }
    swipe.onCompleted: {
        row.queued()
        swipe.close()
    }

    onPressAndHold: row.menuRequested(width / 2, height / 2)

    contentItem: RowLayout {
        spacing: 12
        Item {
            Layout.preferredWidth: row.showCover ? 44 : 28
            Layout.preferredHeight: 44
            Cover {
                anchors.fill: parent
                visible: row.showCover
                radius: 4
                pixels: 132
                glyph: row.coverGlyph
                source: row.cover
            }
            Label {
                anchors.centerIn: parent
                visible: !row.showCover && !row.current
                text: row.number > 0 ? row.number : ""
                color: theme.textDim
                font.pixelSize: 13
                font.features: { "tnum": 1 }
            }
            Icon {
                anchors.centerIn: parent
                visible: !row.showCover && row.current
                name: player.playing ? "volume" : "pause"
                tint: theme.accent
                size: 18
            }
        }
        ColumnLayout {
            Layout.fillWidth: true
            spacing: 1
            Label {
                Layout.fillWidth: true
                text: row.title
                color: row.current ? theme.accent : theme.text
                elide: Text.ElideRight
                font.pixelSize: 15
            }
            Label {
                Layout.fillWidth: true
                visible: row.subtitle !== ""
                text: row.subtitle
                color: theme.textDim
                elide: Text.ElideRight
                font.pixelSize: 12
            }
        }
        Label {
            visible: row.length !== ""
            text: row.length
            color: theme.textDim
            font.pixelSize: 13
            font.features: { "tnum": 1 }
        }
    }
}
