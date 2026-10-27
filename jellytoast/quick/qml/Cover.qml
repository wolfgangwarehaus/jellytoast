// Album art with rounded corners. `fallback` (an already-loaded smaller
// image) shows underneath until `source` is ready, so art never blinks.
// Sources of the form image://cover/<id>/<px> load through `covers` (the
// app's image cache — see jellytoast/quick/covers.py) with the corners
// rounded *in the cached image*: no per-tile shader layer, which kept
// stalling frames as tiles scrolled in. Anything else (a radio logo URL,
// a file) loads directly, square.
import QtQuick

Item {
    id: root
    property url source
    property url fallback
    property int radius: 8
    property int pixels: 512
    // Faint glyph on the placeholder (art missing or still loading), like
    // the desktop grids: "user" for artists, a music note otherwise.
    property string glyph: "music_note"
    // A gentle pulse while the art is on its way, so "loading" doesn't read
    // as "missing"; it settles after a while (art that never comes).
    property bool pulse: true

    component Art: Image {
        id: img
        property url want
        property int px: 512
        // Corner radius in the image's own pixels.
        readonly property int rpx: root.width > 0 ? Math.round(root.radius * px / root.width) : 0
        readonly property bool isCover: String(want).startsWith("image://cover/")
        // Wait for layout (width) so the first request already has the radius.
        // image://cover/<id>/<px> — the id, at THIS cover's own size (a list
        // row needs a smaller image than a grid tile of the same album).
        readonly property string itemId: {
            var rest = String(want).substring(14)
            return rest.substring(0, rest.lastIndexOf("/"))
        }
        readonly property string key: isCover && root.width > 0 && itemId !== ""
                                      ? itemId + "/" + px + "/" + rpx : ""
        property bool ready: false
        function refresh() { ready = key !== "" && covers.request(key) }
        // Already loaded → show at once. Otherwise ask only if this tile is
        // still here a moment later: tiles a fling just flies past never
        // queue a download ahead of the ones you stop on.
        function arm() {
            ready = key !== "" && covers.cached(key)
            if (!ready && key !== "")
                settle.restart()
        }
        Timer { id: settle; interval: 80; onTriggered: img.refresh() }
        // Withdraw the previous cover's queued download when this tile is
        // recycled for another album (or goes away).
        property string held: ""
        onKeyChanged: {
            if (held !== "" && held !== key && !ready)
                covers.release(held)
            held = key
            arm()
        }
        Component.onCompleted: { held = key; arm() }
        Component.onDestruction: if (held !== "" && !ready) covers.release(held)
        Connections {
            target: covers
            enabled: img.key !== "" && !img.ready
            function onReady(k) { if (k === img.key) img.ready = true }
        }
        source: !isCover ? want : (ready && key !== "" ? "image://coverpx/" + key : "")
        sourceSize.width: px
        sourceSize.height: px
        fillMode: Image.PreserveAspectCrop
        asynchronous: !isCover
        cache: false
        smooth: true
        mipmap: false
    }

    Rectangle {  // placeholder until the art arrives
        id: ph
        anchors.fill: parent
        radius: root.radius
        color: theme.surface
        visible: main.status !== Image.Ready
        property bool gaveUp: false
        readonly property bool loading: root.pulse && String(root.source) !== ""
                                        && main.status !== Image.Ready && !gaveUp
        Connections {
            target: root
            function onSourceChanged() { ph.gaveUp = false }
        }
        Timer { interval: 12000; running: ph.loading; onTriggered: ph.gaveUp = true }
        SequentialAnimation {
            running: ph.loading && ph.visible
            loops: Animation.Infinite
            onRunningChanged: if (!running) ph.opacity = 1
            OpacityAnimator { target: ph; from: 1; to: 0.55; duration: 750; easing.type: Easing.InOutSine }
            OpacityAnimator { target: ph; from: 0.55; to: 1; duration: 750; easing.type: Easing.InOutSine }
        }
        Icon {
            anchors.centerIn: parent
            visible: root.glyph !== ""
            name: root.glyph
            size: Math.max(14, Math.round(Math.min(root.width, root.height) * 0.32))
            tint: theme.text
            opacity: 0.34
        }
    }
    Art {
        anchors.fill: parent
        want: root.fallback
        px: 160
        visible: String(root.fallback) !== "" && main.status !== Image.Ready
    }
    Art {
        id: main
        anchors.fill: parent
        want: root.source
        px: root.pixels
        opacity: status === Image.Ready ? 1 : 0
        Behavior on opacity { NumberAnimation { duration: 140 } }
    }
}
