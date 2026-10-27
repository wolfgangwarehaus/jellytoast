// Full-screen Now Playing. Slides up over the app; drag down to close.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Effects

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

    // Backdrop: one colour picked from the cover (player.artTone), softened
    // for the theme — deep and muted on dark, pale on light — washing down
    // from the top into the page colour (august: a colour from the art, not
    // the art blurred). Crossfades between tracks.
    Rectangle {
        id: toneBackdrop
        objectName: "npBackdrop"
        anchors.fill: parent
        visible: prefs.frosted && player.hasTone
        readonly property bool lightTheme: theme.background.hslLightness > 0.5
        property color tone: {
            var c = player.artTone
            var h = c.hslHue < 0 ? 0 : c.hslHue
            var s = Math.min(c.hslSaturation, lightTheme ? 0.45 : 0.55)
            return Qt.hsla(h, s, lightTheme ? 0.84 : 0.26, 1)
        }
        Behavior on tone { ColorAnimation { duration: 600 } }
        gradient: Gradient {
            GradientStop { position: 0.0; color: toneBackdrop.tone }
            GradientStop { position: 0.55; color: Qt.tint(toneBackdrop.tone, Qt.rgba(theme.background.r, theme.background.g, theme.background.b, 0.55)) }
            GradientStop { position: 1.0; color: theme.background }
        }
    }

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
    // Landscape: the art (or lyrics / queue) on the left, the title and
    // controls beside it. One column it lost the cover entirely and the
    // queue / lyrics squeezed under the header (QA 2026-10-27).
    readonly property bool landscape: width > height

    GridLayout {
        anchors.fill: parent
        anchors.leftMargin: np.side
        anchors.rightMargin: np.side
        anchors.bottomMargin: np.landscape ? 12 : 24
        columns: np.landscape ? 2 : 1
        rowSpacing: 0
        columnSpacing: np.landscape ? 24 : 0

        // ── top row ──
        RowLayout {
            Layout.row: 0
            Layout.column: 0
            Layout.columnSpan: np.landscape ? 2 : 1
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
                enabled: player.hasTrack
                onClicked: npSheet.open()
            }
        }

        // Casting: where it's playing — tap for the speaker's volume / stop.
        AbstractButton {
            id: castPill
            objectName: "castPill"
            visible: cast.active !== ""
            Layout.row: 1
            Layout.column: 0
            Layout.columnSpan: np.landscape ? 2 : 1
            Layout.alignment: Qt.AlignHCenter
            Layout.preferredHeight: 34
            Layout.preferredWidth: Math.min(pillIcon.width + 8 + pillText.implicitWidth + 32, parent.width)
            Layout.bottomMargin: 6
            focusPolicy: Qt.NoFocus
            hoverEnabled: false
            onClicked: np.openCast()
            background: Rectangle {
                radius: height / 2
                color: theme.accent
                opacity: castPill.pressed ? 0.28 : 0.18
            }
            contentItem: Item {}
            Icon {
                id: pillIcon
                x: 16
                anchors.verticalCenter: parent.verticalCenter
                name: cast.activeKind === "group" ? "speaker_group"
                      : cast.activeKind === "display" ? "tv" : "speaker"
                tint: theme.accent
                size: 16
            }
            Label {
                id: pillText
                anchors.left: pillIcon.right
                anchors.leftMargin: 8
                anchors.right: parent.right
                anchors.rightMargin: 16
                anchors.verticalCenter: parent.verticalCenter
                text: qsTr("Playing on %1").arg(cast.active)
                color: theme.accent
                elide: Text.ElideRight
                font.pixelSize: 13
                font.weight: Font.DemiBold
            }
        }

        // ── surface: cover, or lyrics / queue in its place; never resized by text ──
        Item {
            id: surfaceBox
            Layout.row: 2
            Layout.column: 0
            Layout.rowSpan: np.landscape ? 8 : 1
            Layout.fillWidth: !np.landscape
            // Landscape: a square as tall as the screen allows, at most
            // ~45% of the width; the controls take the rest.
            Layout.preferredWidth: np.landscape ? Math.min(np.height - 56 - 12, (np.width - 2 * np.side) * 0.45) : -1
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
                // Landscape: stop at the column gap, not under the controls.
                anchors.rightMargin: np.landscape ? 0 : -np.side + 4
                active: np.surface === "queue" && np.shown
                opacity: np.surface === "queue" ? 1 : 0
                visible: opacity > 0
                Behavior on opacity { NumberAnimation { duration: 180 } }
            }
        }

        // Landscape: centres the right column's controls vertically.
        Item {
            visible: np.landscape
            Layout.row: 2
            Layout.column: 1
            Layout.fillHeight: true
        }

        // ── title row: heart · title / artist · (spacer) ──
        RowLayout {
            Layout.row: 3
            Layout.column: np.landscape ? 1 : 0
            Layout.fillWidth: true
            Layout.preferredHeight: 64
            Layout.leftMargin: -12
            Layout.rightMargin: -12
            Layout.topMargin: 8
            IconButton {
                iconName: player.favorite ? "favorite_filled" : "favorite_outline"
                // Nothing to favourite on a radio stream (keeps its space).
                opacity: player.live ? 0 : 1
                enabled: !player.live
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
            Layout.row: 4
            Layout.column: np.landscape ? 1 : 0
            Layout.fillWidth: true
            Layout.preferredHeight: 18
            horizontalAlignment: Text.AlignHCenter
            text: player.formatLine
            color: player.formatLine.startsWith(qsTr("Casting")) ? theme.accent : theme.textDim
            elide: Text.ElideRight
            font.pixelSize: 12
        }

        // Live radio has nothing to seek: a LIVE badge in the bar's place
        // (same height, so the cover never moves).
        Item {
            objectName: "liveBadge"
            Layout.row: 5
            Layout.column: np.landscape ? 1 : 0
            Layout.fillWidth: true
            Layout.preferredHeight: 48
            Layout.topMargin: 6
            visible: player.live
            Rectangle {
                anchors.centerIn: parent
                width: liveLabel.implicitWidth + 24
                height: 26
                radius: 13
                color: "transparent"
                border.width: 1
                border.color: theme.accent
                Label {
                    id: liveLabel
                    anchors.centerIn: parent
                    text: "● " + qsTr("LIVE")
                    color: theme.accent
                    font.pixelSize: 12
                    font.weight: Font.Bold
                    font.letterSpacing: 1
                }
            }
        }
        SeekBar {
            objectName: "seekBar"
            visible: !player.live
            Layout.row: 6
            Layout.column: np.landscape ? 1 : 0
            Layout.fillWidth: true
            Layout.topMargin: 6
            position: player.position
            duration: player.duration
            onSeek: ms => player.seek(ms)
        }

        // ── transport ──
        RowLayout {
            Layout.row: 7
            Layout.column: np.landscape ? 1 : 0
            Layout.fillWidth: true
            Layout.preferredHeight: np.landscape ? 72 : 88
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
            Layout.row: 8
            Layout.column: np.landscape ? 1 : 0
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

        Item {
            visible: np.landscape
            Layout.row: 9
            Layout.column: 1
            Layout.fillHeight: true
        }
    }

    // ⋮ — what you can do with the playing song.
    Sheet {
        id: npSheet
        objectName: "npSheet"
        Row {
            width: parent.width
            height: 64
            spacing: 14
            Cover {
                width: 56; height: 56
                anchors.verticalCenter: parent.verticalCenter
                radius: 8
                pixels: 168
                source: player.cover
            }
            Column {
                anchors.verticalCenter: parent.verticalCenter
                width: parent.width - 70
                spacing: 2
                Label {
                    width: parent.width
                    text: player.title
                    color: theme.text
                    elide: Text.ElideRight
                    font.pixelSize: 17
                    font.weight: Font.DemiBold
                }
                Label {
                    width: parent.width
                    text: player.artist
                    color: theme.textDim
                    elide: Text.ElideRight
                    font.pixelSize: 13
                }
            }
        }
        Rectangle { width: parent.width; height: 1; color: theme.textFaint; opacity: 0.4 }
        SheetRow {
            objectName: "npAddToPlaylist"
            iconName: "list"
            text: qsTr("Add to playlist…")
            visible: playlists.available && player.itemId !== ""
            onClicked: {
                npSheet.close()
                addToPlaylistSheet.show({ kind: "track", id: player.itemId, name: player.title,
                                          artist: player.artist, cover: player.cover })
            }
        }
        SheetRow {
            objectName: "npGoToArtist"
            iconName: "user"
            text: qsTr("Go to artist")
            visible: player.artistId !== ""
            onClicked: {
                npSheet.close()
                np.goToArtist({ id: player.artistId, name: player.artist })
                np.close()
            }
        }
        SheetRow {
            objectName: "npGoToAlbum"
            iconName: "grid"
            text: qsTr("Go to album")
            visible: player.albumId !== ""
            onClicked: {
                npSheet.close()
                np.goToAlbum({ id: player.albumId, name: player.album, artist: player.artist,
                               year: "", cover: player.cover })
                np.close()
            }
        }
    }
}
