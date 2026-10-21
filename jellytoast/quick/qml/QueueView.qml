// Up next: the play queue. Tap to jump; swipe left to remove; drag the ≡
// handle to move a song.
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
    // Drag-to-reorder state (one row at a time).
    property int dragFrom: -1
    property int dragTo: -1

    ListView {
        id: list
        objectName: "queueList"
        anchors.fill: parent
        interactive: root.dragFrom < 0
        clip: true
        model: queue
        boundsBehavior: Flickable.DragAndOvershootBounds
        maximumFlickVelocity: theme.scrollMaxVelocity
        flickDeceleration: theme.scrollDeceleration
        pressDelay: 100  // a scroll that starts at once never "presses" the item under it
        // Where a dragged song will land.
        Rectangle {
            objectName: "dropLine"
            visible: root.dragFrom >= 0 && root.dragTo !== root.dragFrom
            z: 20
            x: 12
            width: list.width - 24
            height: 3
            radius: 1.5
            color: theme.accent
            y: (root.dragTo > root.dragFrom ? root.dragTo + 1 : root.dragTo) * 60 - 1
        }
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
            readonly property bool lifted: root.dragFrom === index
            z: lifted ? 10 : 0
            transform: Translate { y: qrow.lifted ? handleArea.dy : 0 }
            swipe.enabled: root.dragFrom < 0
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
                // Drag handle: hold and drag to move this song.
                Item {
                    objectName: "dragHandle"
                    Layout.preferredWidth: 44
                    Layout.fillHeight: true
                    Icon {
                        anchors.centerIn: parent
                        name: "menu"
                        tint: qrow.lifted ? theme.accent : theme.textDim
                        size: 20
                    }
                    MouseArea {
                        id: handleArea
                        anchors.fill: parent
                        preventStealing: true
                        property real startY: 0
                        property real dy: 0
                        onPressed: mouse => {
                            startY = mapToItem(list, mouse.x, mouse.y).y
                            dy = 0
                            root.dragFrom = qrow.index
                            root.dragTo = qrow.index
                        }
                        onPositionChanged: mouse => {
                            if (root.dragFrom < 0)
                                return
                            dy = mapToItem(list, mouse.x, mouse.y).y - startY
                            root.dragTo = Math.max(0, Math.min(list.count - 1,
                                                               qrow.index + Math.round(dy / qrow.height)))
                        }
                        onReleased: {
                            var from = root.dragFrom, to = root.dragTo
                            root.dragFrom = -1
                            root.dragTo = -1
                            dy = 0
                            if (from >= 0 && to >= 0 && to !== from)
                                queue.move(from, to)
                        }
                        onCanceled: { root.dragFrom = -1; root.dragTo = -1; dy = 0 }
                    }
                }
            }
            Rectangle {  // lifted look
                anchors.fill: parent
                visible: qrow.lifted
                color: "transparent"
                border.color: theme.accent
                border.width: 1
                radius: 6
            }
        }
    }
}
