// Search as you type: artists, albums, songs.
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
    property string title: qsTr("Search")
    readonly property bool loading: results.loading
    signal openItem(string kind, var item)

    SearchModel { id: results }
    // Focus once pushed — StackView focuses the page itself after creation.
    StackView.onActivated: field.forceActiveFocus()

    TextField {
        id: field
        objectName: "searchField"
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.margins: 12
        anchors.topMargin: 12 + page.insetTop
        height: 48
        placeholderText: qsTr("Artists, albums, songs")
        placeholderTextColor: theme.textFaint
        color: theme.text
        font.pixelSize: 16
        leftPadding: 44
        inputMethodHints: Qt.ImhNoPredictiveText
        background: Rectangle { radius: 24; color: theme.surface }
        onTextChanged: results.query = text
        Icon {
            anchors.left: parent.left
            anchors.leftMargin: 14
            anchors.verticalCenter: parent.verticalCenter
            name: "search"
            tint: theme.textDim
            size: 20
        }
    }

    Label {
        anchors.centerIn: parent
        visible: results.query.trim() !== "" && !results.loading && results.count === 0
        color: theme.textDim
        text: qsTr("No matches")
    }
    BusyIndicator {
        anchors.top: field.bottom
        anchors.topMargin: 16
        anchors.horizontalCenter: parent.horizontalCenter
        running: results.loading && results.count === 0
        visible: running
    }

    ListView {
        id: list
        objectName: "searchResults"
        anchors.top: field.bottom
        anchors.topMargin: 8
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        bottomMargin: page.insetBottom
        clip: true
        model: results
        boundsBehavior: Flickable.DragAndOvershootBounds
        maximumFlickVelocity: theme.scrollMaxVelocity
        flickDeceleration: theme.scrollDeceleration
        pressDelay: 100  // a scroll that starts at once never "presses" the item under it
        section.property: "section"
        section.delegate: Label {
            required property string section
            width: list.width
            leftPadding: 16
            topPadding: 14
            bottomPadding: 4
            text: section
            color: theme.textDim
            font.pixelSize: 12
            font.weight: Font.DemiBold
            font.letterSpacing: 1
        }
        onMovingChanged: if (moving) field.focus = false

        delegate: TrackRow {
            required property int index
            required property string kind
            required property string itemId
            required property string name
            required subtitle
            required cover
            width: list.width
            trackId: kind === "track" ? itemId : ""
            coverGlyph: kind === "artist" ? "user" : "music_note"
            title: name
            showCover: true
            swipe.enabled: kind === "track"
            onClicked: {
                if (kind === "track")
                    results.play(index)
                else
                    page.openItem(kind, results.get(index))
            }
            onQueued: { results.queueRow(index, "end"); toast.show(qsTr("Added to queue")) }
            onMenuRequested: (x, y) => {
                var it = results.get(index)
                if (kind === "track")
                    menu.show({ kind: "track", id: itemId, row: index, name: name, artist: subtitle, cover: cover }, this, x, y)
                else
                    menu.show(it, this, x, y)
            }
        }
    }
    ItemMenu {
        id: menu
        queueRow: (row, mode) => {
            results.queueRow(row, mode)
            toast.show(mode === "next" ? qsTr("Playing next") : qsTr("Added to queue"))
        }
    }
}
