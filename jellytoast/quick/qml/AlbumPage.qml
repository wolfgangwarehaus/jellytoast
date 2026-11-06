// One album or playlist: header (cover, title, artist, play / shuffle) +
// its tracks. Swipe a track left to queue it; long-press for the menu.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import Jellytoast

Item {
    // The top bar and mini player float over the page as frosted glass, so
    // content scrolls under them; these keep it clear at rest (see Main).
    readonly property real insetTop: StackView.view ? StackView.view.insetTop : 0
    readonly property real insetBottom: StackView.view ? StackView.view.insetBottom : 0
    id: page
    property var album: ({})
    property string kind: album.kind === "playlist" ? "playlist" : "album"
    property string title: ""
    readonly property bool loading: tracks.loading
    signal openItem(string kind, var item)

    TracksModel { id: tracks; kind: page.kind }
    Component.onCompleted: tracks.load(album.id, album.name)

    ListView {
        id: list
        objectName: "trackList"
        anchors.fill: parent
        clip: true
        model: tracks
        boundsBehavior: Flickable.DragAndOvershootBounds
        maximumFlickVelocity: theme.scrollMaxVelocity
        flickDeceleration: theme.scrollDeceleration
        pressDelay: 100  // a scroll that starts at once never "presses" the item under it
        topMargin: page.insetTop
        bottomMargin: 12 + page.insetBottom

        header: Item {
            width: list.width
            height: hcol.implicitHeight
            // A colour from the album's art behind its header (as on Now
            // Playing), reaching up under the frosted top bar and fading
            // into the track list.
            ToneBackdrop {
                objectName: "albumBackdrop"
                anchors.fill: parent
                anchors.topMargin: -page.insetTop
                source: page.album.cover || ""
            }
        Column {
            id: hcol
            width: parent.width
            topPadding: 4
            bottomPadding: 12
            spacing: 10

            Cover {
                anchors.horizontalCenter: parent.horizontalCenter
                // ≤ ~55% of the page height too: in landscape the cover
                // filled the screen and pushed the title + tracks below it.
                width: Math.min(parent.width - 96, 320, Math.max(140, list.height * 0.55))
                height: width
                radius: 12
                source: page.album.cover || ""
            }
            Label {
                width: parent.width - 32
                anchors.horizontalCenter: parent.horizontalCenter
                horizontalAlignment: Text.AlignHCenter
                text: page.album.name || ""
                color: theme.text
                wrapMode: Text.WordWrap
                maximumLineCount: 2
                elide: Text.ElideRight
                font.pixelSize: 20
                font.weight: Font.DemiBold
            }
            // Artist (tap → their page; back comes back here) · year · size.
            RowLayout {
                width: parent.width - 32
                anchors.horizontalCenter: parent.horizontalCenter
                spacing: 0
                Item { Layout.fillWidth: true }
                AbstractButton {
                    id: artistLink
                    objectName: "albumArtist"
                    readonly property bool shown: page.kind === "album" && !!page.album.artist
                    // The id comes with the loaded tracks (an album opened
                    // from search / Home only knows the name until then).
                    readonly property bool linked: shown && tracks.artistId !== ""
                    visible: shown
                    focusPolicy: Qt.NoFocus
                    hoverEnabled: false
                    // A finger-sized target: padding the text doesn't show
                    // (the negative margins take it back out of the layout).
                    topPadding: 6; bottomPadding: 6; leftPadding: 14; rightPadding: 14
                    Layout.leftMargin: -14; Layout.rightMargin: -14
                    Layout.maximumWidth: parent.width + 28
                    onClicked: if (linked) page.openItem("artist", { kind: "artist", id: tracks.artistId,
                                                                      name: page.album.artist })
                    contentItem: Label {
                        text: page.album.artist || ""
                        color: artistLink.linked ? theme.accent : theme.textDim
                        elide: Text.ElideRight
                        font.pixelSize: 13
                        opacity: artistLink.pressed ? 0.6 : 1
                    }
                    background: null
                }
                Label {
                    text: (artistLink.shown ? " · " : "")
                          + [page.album.year, (tracks ? tracks.summary : "")].filter(s => s).join(" · ")
                    color: theme.textDim
                    elide: Text.ElideRight
                    font.pixelSize: 13
                    Layout.fillWidth: false
                }
                Item { Layout.fillWidth: true }
            }
            // Download · Play · Shuffle — Play centred, the icons at the edges.
            Item {
                width: parent.width
                height: 48
                IconButton {
                    objectName: "downloadAlbum"
                    anchors.left: parent.left
                    anchors.leftMargin: 8
                    anchors.verticalCenter: parent.verticalCenter
                    iconName: "download"
                    tint: theme.textDim
                    onClicked: actions.download(page.album.id, page.album.name)
                }
                PillButton {
                    objectName: "playAll"
                    anchors.centerIn: parent
                    iconName: "play"
                    text: qsTr("Play")
                    filled: true
                    enabled: list.count > 0
                    onClicked: tracks.play(0)
                }
                IconButton {
                    objectName: "shuffleAll"
                    anchors.right: parent.right
                    anchors.rightMargin: 8
                    anchors.verticalCenter: parent.verticalCenter
                    iconName: "shuffle"
                    tint: theme.textDim
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
        }

        delegate: TrackRow {
            required property int index
            required trackId
            required property string name
            required property string artist
            required number
            required length
            width: list.width
            title: name
            subtitle: artist
            onClicked: tracks.play(index)
            onQueued: { tracks.queueRow(index, "end"); toast.show(qsTr("Added to queue")) }
            onMenuRequested: (x, y) => menu.show({ kind: "track", id: trackId, row: index, name: name, artist: artist || page.album.artist || "", cover: page.album.cover || "" }, this, x, y)
        }
    }

    ItemMenu {
        id: menu
        removeRow: page.kind === "playlist" ? (row => playlists.removeRow(tracks, row)) : null
        queueRow: (row, mode) => {
            tracks.queueRow(row, mode)
            toast.show(mode === "next" ? qsTr("Playing next") : qsTr("Added to queue"))
        }
    }
}
