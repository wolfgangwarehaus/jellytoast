// Full-screen Now Playing. Slides up over the app; drag down to close.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: np
    objectName: "nowPlaying"
    property bool shown: false
    signal goToAlbum(var album)

    color: theme.background
    y: shown ? dragOffset : parent.height
    visible: y < parent.height
    property real dragOffset: 0

    Behavior on y {
        enabled: !drag.active
        NumberAnimation { duration: 260; easing.type: Easing.OutCubic }
    }

    function open() { dragOffset = 0; shown = true }
    function close() { shown = false; dragOffset = 0 }

    // Swallow taps so nothing underneath reacts.
    MouseArea { anchors.fill: parent }

    DragHandler {
        id: drag
        target: null
        xAxis.enabled: false
        onTranslationChanged: np.dragOffset = Math.max(0, translation.y)
        onActiveChanged: {
            if (active)
                return
            if (np.dragOffset > np.height * 0.2)
                np.close()
            else
                np.dragOffset = 0
        }
    }

    readonly property int side: 24

    ColumnLayout {
        anchors.fill: parent
        anchors.leftMargin: np.side
        anchors.rightMargin: np.side
        anchors.bottomMargin: 24
        spacing: 0

        // ── top row ──
        RowLayout {
            Layout.fillWidth: true
            Layout.preferredHeight: 56
            Layout.leftMargin: -12
            Layout.rightMargin: -12
            IconButton {
                iconName: "chevron_down"
                onClicked: np.close()
            }
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 0
                Label {
                    Layout.fillWidth: true
                    horizontalAlignment: Text.AlignHCenter
                    text: player.source ? qsTr("PLAYING FROM") : ""
                    color: theme.textDim
                    font.pixelSize: 10
                    font.letterSpacing: 1.2
                }
                Label {
                    Layout.fillWidth: true
                    horizontalAlignment: Text.AlignHCenter
                    text: player.source
                    color: theme.text
                    elide: Text.ElideRight
                    font.pixelSize: 13
                    font.weight: Font.Medium
                }
            }
            IconButton {
                iconName: "more_vert"
                enabled: player.albumId !== ""
                onClicked: moreMenu.popup(this, width - moreMenu.width, height)
                Menu {
                    id: moreMenu
                    MenuItem {
                        text: qsTr("Go to album")
                        onTriggered: {
                            np.goToAlbum({ id: player.albumId, name: player.album, artist: player.artist,
                                           year: "", cover: player.cover })
                            np.close()
                        }
                    }
                }
            }
        }

        // ── cover: as big as fits, square, never resized by text ──
        Item {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Cover {
                id: art
                objectName: "npCover"
                readonly property int edge: Math.min(parent.width, parent.height - 16)
                width: edge
                height: edge
                anchors.centerIn: parent
                radius: 12
                pixels: 1024
                source: player.bigCover
                fallback: player.cover
            }
        }

        // ── title row: heart · title / artist · (spacer) ──
        RowLayout {
            Layout.fillWidth: true
            Layout.preferredHeight: 64
            Layout.leftMargin: -12
            Layout.rightMargin: -12
            Layout.topMargin: 8
            IconButton {
                iconName: player.favorite ? "favorite_filled" : "favorite_outline"
                tint: player.favorite ? theme.accent : theme.text
                onClicked: player.toggleFavorite()
            }
            ColumnLayout {
                Layout.fillWidth: true
                spacing: 2
                Label {
                    Layout.fillWidth: true
                    horizontalAlignment: Text.AlignHCenter
                    text: player.title
                    color: theme.text
                    elide: Text.ElideRight
                    font.pixelSize: 20
                    font.weight: Font.DemiBold
                }
                Label {
                    Layout.fillWidth: true
                    horizontalAlignment: Text.AlignHCenter
                    text: player.artist
                    color: theme.textDim
                    elide: Text.ElideRight
                    font.pixelSize: 15
                }
            }
            Item { Layout.preferredWidth: 48 }
        }

        SeekBar {
            Layout.fillWidth: true
            Layout.topMargin: 12
            position: player.position
            duration: player.duration
            onSeek: ms => player.seek(ms)
        }

        // ── transport ──
        RowLayout {
            Layout.fillWidth: true
            Layout.preferredHeight: 88
            Layout.topMargin: 4
            IconButton {
                iconName: "shuffle"
                tint: player.shuffle ? theme.accent : theme.textDim
                onClicked: player.toggleShuffle()
            }
            Item { Layout.fillWidth: true }
            IconButton {
                iconName: "prev"
                iconSize: 32
                implicitWidth: 56; implicitHeight: 56
                onClicked: player.previous()
            }
            Item { Layout.fillWidth: true }
            AbstractButton {
                id: play
                implicitWidth: 76
                implicitHeight: 76
                focusPolicy: Qt.NoFocus
                hoverEnabled: false
                onClicked: player.playPause()
                background: Rectangle {
                    radius: width / 2
                    color: theme.text
                    scale: play.pressed ? 0.94 : 1
                    Behavior on scale { NumberAnimation { duration: 90 } }
                }
                contentItem: Item {
                    Icon {
                        anchors.centerIn: parent
                        name: player.playing ? "pause" : "play"
                        tint: theme.background
                        size: 34
                    }
                }
            }
            Item { Layout.fillWidth: true }
            IconButton {
                iconName: "next"
                iconSize: 32
                implicitWidth: 56; implicitHeight: 56
                onClicked: player.next()
            }
            Item { Layout.fillWidth: true }
            IconButton {
                iconName: player.repeat === "one" ? "repeat_one" : "repeat"
                tint: player.repeat !== "off" ? theme.accent : theme.textDim
                onClicked: player.cycleRepeat()
            }
        }
    }
}
