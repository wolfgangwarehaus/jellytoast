// Up next: the play queue. Tap to jump; swipe left to remove.
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: root
    objectName: "queueView"
    property bool active: false
    onActiveChanged: if (active) list.positionViewAtIndex(Math.max(0, queue.current), ListView.Beginning)

    Label {
        anchors.centerIn: parent
        visible: list.count === 0
        color: theme.textDim
        text: qsTr("The queue is empty")
    }
    ListView {
        id: list
        anchors.fill: parent
        clip: true
        model: queue
        boundsBehavior: Flickable.DragAndOvershootBounds
        maximumFlickVelocity: theme.scrollMaxVelocity
        flickDeceleration: theme.scrollDeceleration
        pressDelay: 100  // a scroll that starts at once never "presses" the item under it
        delegate: SwipeDelegate {
            id: qrow
            required property int index
            required property string name
            required property string artist
            required property string cover
            required property bool current
            width: list.width
            height: 60
            padding: 0
            focusPolicy: Qt.NoFocus
            hoverEnabled: false
            opacity: index < queue.current ? 0.5 : 1
            background: Rectangle {
                color: theme.background
                Rectangle { anchors.fill: parent; color: theme.text; opacity: qrow.pressed ? 0.08 : 0 }
            }
            onClicked: queue.jump(index)
            swipe.right: Rectangle {
                width: parent.width; height: parent.height
                color: "#c0392b"
                Label {
                    anchors.right: parent.right; anchors.rightMargin: 20
                    anchors.verticalCenter: parent.verticalCenter
                    text: qsTr("Remove"); color: "white"; font.pixelSize: 14; font.weight: Font.DemiBold
                }
            }
            swipe.onCompleted: queue.remove(index)
            contentItem: RowLayout {
                spacing: 12
                Cover { Layout.preferredWidth: 44; Layout.preferredHeight: 44; radius: 4; pixels: 132; source: qrow.cover }
                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 1
                    Label {
                        Layout.fillWidth: true
                        text: qrow.name
                        color: qrow.current ? theme.accent : theme.text
                        elide: Text.ElideRight
                        font.pixelSize: 15
                        font.weight: qrow.current ? Font.DemiBold : Font.Normal
                    }
                    Label {
                        Layout.fillWidth: true
                        text: qrow.artist
                        color: theme.textDim
                        elide: Text.ElideRight
                        font.pixelSize: 12
                    }
                }
                Icon {
                    visible: qrow.current
                    name: player.playing ? "volume" : "pause"
                    tint: theme.accent
                    size: 18
                }
            }
        }
    }
}
