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
        boundsBehavior: Flickable.DragOverBounds
        maximumFlickVelocity: 8000
        flickDeceleration: 2500
        clip: true

        Column {
            id: col
            width: flick.width
            spacing: 8
            topPadding: 4

            PillButton {
                objectName: "shuffleLibrary"
                anchors.horizontalCenter: parent.horizontalCenter
                iconName: "shuffle"
                filled: true
                text: qsTr("Shuffle my library")
                onClicked: actions.shuffleLibrary()
            }

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
                        leftPadding: 16
                        topPadding: 10
                        text: rail.modelData.label
                        color: theme.text
                        font.pixelSize: 17
                        font.weight: Font.DemiBold
                    }
                    ListView {
                        id: row
                        objectName: "rail_" + rail.modelData.kind
                        width: parent.width
                        height: 190
                        orientation: ListView.Horizontal
                        spacing: 12
                        leftMargin: 16
                        rightMargin: 16
                        clip: true
                        model: lib
                        boundsBehavior: Flickable.DragOverBounds
                        delegate: Item {
                            id: tile
                            required property int index
                            required property string itemId
                            required property string name
                            required property string subtitle
                            required property string year
                            required property string cover
                            width: 140
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
                                Cover { width: 140; height: 140; source: tile.cover; pixels: 280 }
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
