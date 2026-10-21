// A–Z scrubber on the right edge of a long grid / list. Quiet until the
// list moves or the rail is touched; drag or tap a letter to jump there.
import QtQuick
import QtQuick.Controls

Item {
    id: rail
    objectName: "azRail"
    property var model            // a LibraryModel (seekLetter / letterFound)
    property var view             // the GridView / ListView to position
    readonly property var letters: "#ABCDEFGHIJKLMNOPQRSTUVWXYZ".split("")
    property string current: ""
    readonly property bool engaged: area.pressed
    readonly property bool awake: engaged || (view && view.moving)

    width: 28
    opacity: awake ? 1 : 0
    Behavior on opacity { NumberAnimation { duration: awake ? 120 : 600 } }

    Connections {
        target: rail.model
        function onLetterFound(letter, row) {
            if (letter === rail.current && rail.view)
                rail.view.positionViewAtIndex(row, GridView.Beginning)
        }
    }

    Rectangle {
        anchors.fill: column
        anchors.margins: -4
        radius: width / 2
        color: theme.surface
        opacity: rail.engaged ? 0.9 : 0.5
    }
    Column {
        id: column
        anchors.centerIn: parent
        Repeater {
            model: rail.letters
            Text {
                required property string modelData
                width: rail.width
                height: Math.min(18, (rail.height - 16) / rail.letters.length)
                horizontalAlignment: Text.AlignHCenter
                verticalAlignment: Text.AlignVCenter
                text: modelData
                color: modelData === rail.current && rail.engaged ? theme.accent : theme.textDim
                font.pixelSize: 10
                font.weight: Font.DemiBold
            }
        }
    }
    MouseArea {
        id: area
        anchors.fill: column
        anchors.leftMargin: -12
        preventStealing: true
        function pick(y) {
            var i = Math.max(0, Math.min(rail.letters.length - 1, Math.floor(y / (column.height / rail.letters.length))))
            var l = rail.letters[i]
            if (l !== rail.current || !pressed) {
                rail.current = l
                rail.model.seekLetter(l)
            }
        }
        onPressed: mouse => pick(mouse.y)
        onPositionChanged: mouse => pick(mouse.y)
    }
    // Big letter bubble while scrubbing.
    Rectangle {
        visible: rail.engaged
        width: 64; height: 64; radius: 32
        color: theme.accent
        anchors.right: parent.left
        anchors.rightMargin: 16
        y: Math.max(0, Math.min(rail.height - height, area.mouseY + column.y - height / 2))
        Text {
            anchors.centerIn: parent
            text: rail.current
            color: theme.onAccent
            font.pixelSize: 30
            font.weight: Font.Bold
        }
    }
}
