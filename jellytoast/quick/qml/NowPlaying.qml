// Full-screen Now Playing. Slides up over the app; drag down to close.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: np
    objectName: "nowPlaying"
    property bool shown: false
    property string surface: "cover"  // | "lyrics" | "queue"
    signal goToAlbum(var album)
    signal goToArtist(var artist)
    signal openCast()

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
    function toggleSurface(s) { surface = surface === s ? "cover" : s }

    // Swallow taps so nothing underneath reacts.
    MouseArea { anchors.fill: parent }

    DragHandler {
        id: drag
        target: null
        enabled: np.surface === "cover"
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
                objectName: "castButton"
                iconName: "cast"
                tint: cast.active !== "" ? theme.accent : theme.text
                visible: cast.available
                onClicked: np.openCast()
            }
            IconButton {
                objectName: "npMore"
                iconName: "more_vert"
                enabled: player.albumId !== "" || player.artistId !== ""
                onClicked: moreMenu.popup(this, width - moreMenu.width, height)
                Menu {
                    id: moreMenu
                    MenuItem {
                        text: qsTr("Go to artist")
                        enabled: player.artistId !== ""
                        onTriggered: {
                            np.goToArtist({ id: player.artistId, name: player.artist })
                            np.close()
                        }
                    }
                    MenuItem {
                        text: qsTr("Go to album")
                        enabled: player.albumId !== ""
                        onTriggered: {
                            np.goToAlbum({ id: player.albumId, name: player.album, artist: player.artist,
                                           year: "", cover: player.cover })
                            np.close()
                        }
                    }
                }
            }
        }

        // ── surface: cover, or lyrics / queue in its place; never resized by text ──
        Item {
            id: surfaceBox
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
                opacity: np.surface === "cover" ? 1 : 0
                visible: opacity > 0
                Behavior on opacity { NumberAnimation { duration: 180 } }
            }
            LyricsView {
                anchors.fill: parent
                active: np.surface === "lyrics" && np.shown
                opacity: np.surface === "lyrics" ? 1 : 0
                visible: opacity > 0
                Behavior on opacity { NumberAnimation { duration: 180 } }
            }
            QueueView {
                anchors.fill: parent
                anchors.leftMargin: -np.side + 4
                anchors.rightMargin: -np.side + 4
                active: np.surface === "queue" && np.shown
                opacity: np.surface === "queue" ? 1 : 0
                visible: opacity > 0
                Behavior on opacity { NumberAnimation { duration: 180 } }
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

        // Format line — fixed height so it never moves the cover.
        Label {
            objectName: "formatLine"
            Layout.fillWidth: true
            Layout.preferredHeight: 18
            horizontalAlignment: Text.AlignHCenter
            text: player.formatLine
            color: player.formatLine.startsWith(qsTr("Casting")) ? theme.accent : theme.textDim
            elide: Text.ElideRight
            font.pixelSize: 12
        }

        SeekBar {
            objectName: "seekBar"
            Layout.fillWidth: true
            Layout.topMargin: 6
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

        // ── surface toggles ──
        RowLayout {
            Layout.fillWidth: true
            Layout.preferredHeight: 48
            Layout.leftMargin: -12
            Layout.rightMargin: -12
            IconButton {
                objectName: "lyricsToggle"
                iconName: "lyrics"
                tint: np.surface === "lyrics" ? theme.accent : theme.textDim
                onClicked: np.toggleSurface("lyrics")
            }
            Item { Layout.fillWidth: true }
            AbstractButton {
                id: sleepBtn
                objectName: "sleepButton"
                focusPolicy: Qt.NoFocus
                hoverEnabled: false
                implicitWidth: sleepRow.implicitWidth + 24
                implicitHeight: 48
                readonly property bool on: player.sleepLeft >= 0
                onClicked: sleepMenu.popup(sleepBtn, (width - sleepMenu.width) / 2, -sleepMenu.height)
                background: Rectangle { radius: 24; color: theme.text; opacity: sleepBtn.pressed ? 0.1 : 0 }
                contentItem: Item {
                    Row {
                        id: sleepRow
                        anchors.centerIn: parent
                        spacing: 6
                        Icon { name: "moon"; size: 22; tint: sleepBtn.on ? theme.accent : theme.textDim }
                        Label {
                            anchors.verticalCenter: parent.verticalCenter
                            visible: sleepBtn.on
                            text: player.sleepLeft > 0
                                  ? Math.floor(player.sleepLeft / 60) + ":" + ("0" + player.sleepLeft % 60).slice(-2)
                                  : qsTr("end of song")
                            color: theme.accent
                            font.pixelSize: 13
                            font.features: { "tnum": 1 }
                        }
                    }
                }
                Menu {
                    id: sleepMenu
                    objectName: "sleepMenu"
                    Repeater {
                        model: [15, 30, 45, 60, 90]
                        MenuItem {
                            required property int modelData
                            text: modelData < 60 ? qsTr("%1 minutes").arg(modelData)
                                 : modelData === 60 ? qsTr("1 hour") : qsTr("1 h 30 min")
                            onTriggered: player.startSleep(modelData)
                        }
                    }
                    MenuItem {
                        text: qsTr("After this song")
                        onTriggered: player.startSleep(0)
                    }
                    MenuItem {
                        text: qsTr("Turn off timer")
                        visible: sleepBtn.on
                        height: visible ? implicitHeight : 0
                        onTriggered: player.cancelSleep()
                    }
                }
            }
            Item { Layout.fillWidth: true }
            IconButton {
                objectName: "queueToggle"
                iconName: "queue"
                tint: np.surface === "queue" ? theme.accent : theme.textDim
                onClicked: np.toggleSurface("queue")
            }
        }
    }
}
