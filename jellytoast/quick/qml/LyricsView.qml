// Lyrics for the playing track. Synced lyrics follow the song (current line
// bright, centred); tap a line to jump there. Flicking pauses the follow.
import QtQuick
import QtQuick.Controls

Item {
    id: root
    objectName: "lyricsView"
    property bool active: false
    onActiveChanged: if (active) lyrics.load(player.itemId)
    Connections {
        target: player
        function onChanged() { if (root.active) lyrics.load(player.itemId) }
    }

    property bool following: true
    Timer { id: refollow; interval: 4000; onTriggered: root.following = true }

    Label {
        anchors.centerIn: parent
        visible: !lyrics.loading && list.count === 0
        color: theme.textDim
        text: qsTr("No lyrics for this song")
    }
    BusyIndicator { anchors.centerIn: parent; running: lyrics.loading; visible: running }

    ListView {
        id: list
        anchors.fill: parent
        clip: true
        model: lyrics
        spacing: 14
        readonly property int playing: lyrics.synced ? lyrics.lineAt(player.position) : -1
        currentIndex: root.following ? playing : currentIndex
        highlightFollowsCurrentItem: true
        preferredHighlightBegin: height * 0.35
        preferredHighlightEnd: height * 0.35 + 40
        highlightRangeMode: root.following && lyrics.synced ? ListView.ApplyRange : ListView.NoHighlightRange
        highlightMoveDuration: 350
        boundsBehavior: Flickable.DragOverBounds
        topMargin: 24
        bottomMargin: 24
        onMovingChanged: if (moving && dragging) { root.following = false; refollow.restart() }
        onDraggingChanged: if (dragging) { root.following = false; refollow.restart() }

        delegate: Text {
            required property int index
            required property string line
            required property int start
            readonly property bool now: index === list.playing
            width: list.width
            wrapMode: Text.WordWrap
            text: line || "♪"
            color: !lyrics.synced || now ? theme.text : theme.textFaint
            font.pixelSize: 22
            font.weight: now ? Font.Bold : Font.DemiBold
            Behavior on color { ColorAnimation { duration: 200 } }
            TapHandler {
                enabled: lyrics.synced
                onTapped: { player.seek(parent.start); root.following = true }
            }
        }
    }
}
