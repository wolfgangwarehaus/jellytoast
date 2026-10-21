// One album: header (cover, title, artist, play / shuffle) + its tracks.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Jellytoast

Item {
    id: page
    property var album: ({})
    property string title: ""

    TracksModel { id: tracks }
    Component.onCompleted: tracks.load(album.id, album.name)

    ListView {
        id: list
        objectName: "trackList"
        anchors.fill: parent
        clip: true
        model: tracks
        boundsBehavior: Flickable.DragOverBounds
        maximumFlickVelocity: 8000
        flickDeceleration: 2500
        pixelAligned: true
        bottomMargin: 12
        ScrollIndicator.vertical: ScrollIndicator { }

        header: Column {
            width: list.width
            topPadding: 4
            bottomPadding: 12
            spacing: 10

            Cover {
                anchors.horizontalCenter: parent.horizontalCenter
                width: Math.min(parent.width - 96, 320)
                height: width
                radius: 12
                source: page.album.cover
            }
            Label {
                width: parent.width - 32
                anchors.horizontalCenter: parent.horizontalCenter
                horizontalAlignment: Text.AlignHCenter
                text: page.album.name
                color: theme.text
                wrapMode: Text.WordWrap
                maximumLineCount: 2
                elide: Text.ElideRight
                font.pixelSize: 20
                font.weight: Font.DemiBold
            }
            Label {
                width: parent.width - 32
                anchors.horizontalCenter: parent.horizontalCenter
                horizontalAlignment: Text.AlignHCenter
                text: [page.album.artist, page.album.year, (tracks ? tracks.summary : "")].filter(s => s).join(" · ")
                color: theme.textDim
                elide: Text.ElideRight
                font.pixelSize: 13
            }
            Row {
                anchors.horizontalCenter: parent.horizontalCenter
                spacing: 12
                PillButton {
                    iconName: "play"
                    text: qsTr("Play")
                    filled: true
                    enabled: list.count > 0
                    onClicked: tracks.play(0)
                }
                PillButton {
                    iconName: "shuffle"
                    text: qsTr("Shuffle")
                    enabled: list.count > 0
                    onClicked: tracks.shuffle()
                }
            }
            BusyIndicator {
                anchors.horizontalCenter: parent.horizontalCenter
                running: tracks ? tracks.loading : false
                visible: running
            }
        }

        delegate: ItemDelegate {
            id: row
            required property int index
            required property string trackId
            required property string name
            required property string artist
            required property int number
            required property string length
            readonly property bool current: trackId === player.itemId

            width: list.width
            height: 56
            focusPolicy: Qt.NoFocus
            hoverEnabled: false
            background: Rectangle {
                color: theme.text
                opacity: row.pressed ? 0.08 : 0
            }
            onClicked: tracks.play(index)

            contentItem: RowLayout {
                spacing: 12
                Item {
                    Layout.preferredWidth: 28
                    Layout.fillHeight: true
                    Label {
                        anchors.centerIn: parent
                        visible: !row.current
                        text: row.number > 0 ? row.number : ""
                        color: theme.textDim
                        font.pixelSize: 13
                        font.features: { "tnum": 1 }
                    }
                    Icon {
                        anchors.centerIn: parent
                        visible: row.current
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
                        text: row.name
                        color: row.current ? theme.accent : theme.text
                        elide: Text.ElideRight
                        font.pixelSize: 15
                    }
                    Label {
                        Layout.fillWidth: true
                        visible: row.artist !== ""
                        text: row.artist
                        color: theme.textDim
                        elide: Text.ElideRight
                        font.pixelSize: 12
                    }
                }
                Label {
                    text: row.length
                    color: theme.textDim
                    font.pixelSize: 13
                    font.features: { "tnum": 1 }
                }
            }
        }
    }
}
