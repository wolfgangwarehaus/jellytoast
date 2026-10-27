// Home: shuffle everything, then rails of albums — recently added,
// recently played, most played, favourites, and a random pick.
import QtQuick
import QtQuick.Controls
import Jellytoast

Item {
    id: page
    objectName: "homePage"
    property string title: qsTr("Home")
    readonly property bool loading: false
    signal openItem(string kind, var item)

    Flickable {
        id: flick
        anchors.fill: parent
        contentHeight: col.implicitHeight + 24
        boundsBehavior: Flickable.DragAndOvershootBounds
        maximumFlickVelocity: theme.scrollMaxVelocity
        flickDeceleration: theme.scrollDeceleration
        pressDelay: 100  // a scroll that starts at once never "presses" the item under it
        clip: true

        Column {
            id: col
            width: flick.width
            spacing: 8
            topPadding: 4

            // (Shuffle my library lives in the ⋮ menu — "Shuffle all".)
            Repeater {
                model: [
                    { kind: "latest", label: qsTr("Recently added") },
                    { kind: "recent", label: qsTr("Recently played") },
                    { kind: "frequent", label: qsTr("Most played") },
                    { kind: "favAlbums", label: qsTr("Favorites") },
                    { kind: "random", label: qsTr("Something different") }
                ]
                delegate: Column {
                    id: rail
                    required property var modelData
                    width: col.width
                    spacing: 6
                    visible: lib.count > 0 || lib.loading
                    LibraryModel { id: lib; kind: rail.modelData.kind }
                    Component.onCompleted: lib.reload()

                    Label {
                        // Lined up with the tiles below it.
                        leftPadding: Math.round(row.centredX)
                        topPadding: 10
                        text: rail.modelData.label
                        color: theme.text
                        font.pixelSize: 17
                        font.weight: Font.DemiBold
                    }
                    ListView {
                        flickDeceleration: theme.scrollDeceleration
                        pressDelay: 100  // a scroll that starts at once never "presses" the item under it
                        maximumFlickVelocity: theme.scrollMaxVelocity
                        id: row
                        objectName: "rail_" + rail.modelData.kind
                        width: parent.width
                        // Whole tiles plus a peek of the neighbours, so the rail
                        // reads as scrollable (august, 2026-10-27): `cols` tiles
                        // centred, ~a sixth of a tile showing past each side.
                        // The side margins ARE that centred offset, so the first
                        // pair, every swipe and the last pair all sit in the same
                        // place — everything always lines up.
                        readonly property int cols: Math.max(2, Math.floor(width / 175))
                        readonly property real tileW: (width - (cols + 1) * spacing) / (cols + 0.36)
                        readonly property real pitch: tileW + spacing
                        // Where a settled tile starts, mid-rail: the group of
                        // `cols` tiles centred in the view.
                        readonly property real centredX: (width - cols * tileW - (cols - 1) * spacing) / 2
                        height: Math.round(tileW) + 50
                        orientation: ListView.Horizontal
                        spacing: 14
                        leftMargin: centredX
                        rightMargin: centredX
                        function settle() {
                            var maxX = Math.max(-leftMargin, contentWidth + rightMargin - width)
                            var i = Math.round((contentX + centredX) / pitch)
                            var target = i <= 0 ? -leftMargin : Math.min(i * pitch - centredX, maxX)
                            if (Math.abs(target - contentX) > 0.5) {
                                snapAnim.to = target
                                snapAnim.restart()
                            }
                        }
                        // Edge vignette: the peeking neighbours fade toward the
                        // page background (75% at the screen edge, so they still
                        // read as "more this way") up to where the whole tiles
                        // start. Parented to the view itself, not
                        // its content, so they stay put while it scrolls.
                        Rectangle {
                            parent: row
                            z: 2
                            anchors.left: parent.left
                            anchors.top: parent.top
                            anchors.bottom: parent.bottom
                            width: Math.round(row.centredX)
                            gradient: Gradient {
                                orientation: Gradient.Horizontal
                                GradientStop { position: 0.0; color: Qt.rgba(theme.background.r, theme.background.g, theme.background.b, 0.75) }
                                GradientStop { position: 1.0; color: "transparent" }
                            }
                        }
                        Rectangle {
                            parent: row
                            z: 2
                            anchors.right: parent.right
                            anchors.top: parent.top
                            anchors.bottom: parent.bottom
                            width: Math.round(row.centredX)
                            gradient: Gradient {
                                orientation: Gradient.Horizontal
                                GradientStop { position: 0.0; color: "transparent" }
                                GradientStop { position: 1.0; color: Qt.rgba(theme.background.r, theme.background.g, theme.background.b, 0.75) }
                            }
                        }
                        onMovementStarted: snapAnim.stop()  // a new grab wins
                        onMovementEnded: settle()
                        NumberAnimation on contentX {
                            id: snapAnim
                            running: false
                            duration: 220
                            easing.type: Easing.OutCubic
                        }
                        clip: true
                        model: lib
                        boundsBehavior: Flickable.DragAndOvershootBounds
                        delegate: Item {
                            id: tile
                            required property int index
                            required property string itemId
                            required property string name
                            required property string subtitle
                            required property string year
                            required property string cover
                            width: row.tileW
                            height: row.height
                            function info() {
                                return { kind: "album", id: tile.itemId, name: tile.name, artist: tile.subtitle,
                                         year: tile.year, cover: tile.cover }
                            }
                            Column {
                                anchors.fill: parent
                                spacing: 4
                                scale: tap.pressed ? 0.97 : 1
                                Behavior on scale { NumberAnimation { duration: 90 } }
                                Cover {
                                    width: row.tileW; height: row.tileW; source: tile.cover
                                    pixels: row.tileW > 140 ? 400 : 320
                                }
                                Label {
                                    width: parent.width
                                    text: tile.name
                                    color: theme.text
                                    elide: Text.ElideRight
                                    font.pixelSize: 13
                                    font.weight: Font.Medium
                                }
                                Label {
                                    width: parent.width
                                    text: tile.subtitle
                                    color: theme.textDim
                                    elide: Text.ElideRight
                                    font.pixelSize: 12
                                }
                            }
                            TapHandler {
                                id: tap
                                onTapped: page.openItem("album", tile.info())
                                onLongPressed: menu.show(Object.assign(tile.info(), { favorite: lib.get(tile.index).favorite }),
                                                         tile, point.position.x, point.position.y)
                            }
                        }
                    }
                }
            }
        }
    }
    ItemMenu { id: menu }
}
