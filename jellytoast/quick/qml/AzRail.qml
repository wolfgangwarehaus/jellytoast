// A–Z scrubber on the right edge of a long grid / list: a thin grey line
// with a small marker for where you are. Quiet until the list moves or the
// rail is touched; hold and drag along it to jump by letter (a big letter
// bubble shows which).
import QtQuick
import QtQuick.Controls

Item {
    id: rail
    objectName: "azRail"
    property var model            // a LibraryModel (seekLetter / letterFound)
    property var view             // the GridView / ListView to position
    property bool reversed: false // the list runs Z → A
    readonly property var letters: {
        var l = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ".split("")
        return reversed ? l.reverse() : l
    }
    property string current: ""
    readonly property bool engaged: area.pressed
    readonly property bool awake: engaged || (view && view.moving)

    // Wide enough to grab; only the line itself is drawn.
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

    // The track: a thin grey line.
    Rectangle {
        id: track
        anchors.right: parent.right
        anchors.rightMargin: 4
        anchors.top: parent.top
        anchors.bottom: parent.bottom
        anchors.topMargin: 12
        anchors.bottomMargin: 12
        width: rail.engaged ? 3 : 2
        radius: width / 2
        color: theme.textFaint
        opacity: rail.engaged ? 0.9 : 0.55
    }
    // Where you are: a short, slightly brighter segment on the line —
    // under the finger while scrubbing, else the list's scroll position.
    Rectangle {
        id: marker
        readonly property real frac: rail.engaged
            ? Math.max(0, Math.min(1, (area.mouseY - track.y) / track.height))
            : (rail.view && rail.view.visibleArea.heightRatio < 1
               ? rail.view.visibleArea.yPosition / Math.max(0.0001, 1 - rail.view.visibleArea.heightRatio)
               : 0)
        x: track.x + track.width / 2 - width / 2
        y: track.y + Math.max(0, Math.min(1, frac)) * (track.height - height)
        width: rail.engaged ? 6 : 4
        height: 36
        radius: width / 2
        color: rail.engaged ? theme.accent : theme.textDim
    }

    MouseArea {
        id: area
        anchors.fill: parent
        anchors.leftMargin: -8
        preventStealing: true
        function pick(y) {
            var f = Math.max(0, Math.min(0.9999, (y - track.y) / track.height))
            var l = rail.letters[Math.floor(f * rail.letters.length)]
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
        anchors.rightMargin: 12
        y: Math.max(0, Math.min(rail.height - height, area.mouseY - height / 2))
        Text {
            anchors.centerIn: parent
            text: rail.current
            color: theme.onAccent
            font.pixelSize: 30
            font.weight: Font.Bold
        }
    }
}
