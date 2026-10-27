// Frosted glass — the phone's take on the desktop's frosted panes: what's
// behind (the `behind` item) shows through blurred, under a theme-tinted
// wash. There's no compositor blur behind a full-screen phone app, so the
// app blurs its own content instead: the top bar and mini player over the
// pages that scroll under them, the bottom sheets over the app.
//
// `behind` must NOT contain the pane (it renders `behind` into a texture).
import QtQuick
import QtQuick.Effects

Item {
    id: pane
    property Item behind: null
    property color tint: theme.background
    property real tintOpacity: 0.7
    property real radius: 0
    // Bind to anything that moves the pane without moving its own x / y (a
    // sheet sliding in) so the sampled region follows it.
    property real track: 0
    // Settings → Frosted glass off: a solid pane (no blur work at all).
    readonly property bool live: pane.behind !== null && prefs.frosted

    readonly property rect region: {
        // Re-evaluate when any of these change.
        void (pane.track + pane.x + pane.y + pane.width + pane.height)
        if (!pane.behind)
            return Qt.rect(0, 0, 0, 0)
        var p = pane.mapToItem(pane.behind, 0, 0)
        return Qt.rect(p.x, p.y, pane.width, pane.height)
    }

    ShaderEffectSource {
        id: src
        anchors.fill: parent
        sourceItem: pane.live ? pane.behind : null
        sourceRect: pane.region
        // Quarter resolution: cheaper, and a thin bar needs the stronger
        // smear (a blur can only spread as far as the strip it samples).
        textureSize: Qt.size(Math.max(1, Math.round(pane.width / 4)),
                             Math.max(1, Math.round(pane.height / 4)))
        live: true
        recursive: false
        visible: false
    }
    Rectangle {
        id: mask
        anchors.fill: parent
        radius: pane.radius
        layer.enabled: true
        visible: false
    }
    // Opaque base: the pages have no fill of their own, so without it the
    // sharp original under the pane showed straight through the blurred copy.
    Rectangle {
        anchors.fill: parent
        radius: pane.radius
        color: theme.background
    }
    MultiEffect {
        anchors.fill: parent
        visible: pane.live
        source: src
        blurEnabled: true
        blur: 1.0
        blurMax: 48
        blurMultiplier: 1.5
        saturation: 0.15
        maskEnabled: pane.radius > 0
        maskSource: mask
    }
    Rectangle {
        anchors.fill: parent
        radius: pane.radius
        color: pane.tint
        opacity: pane.live ? pane.tintOpacity : 1
    }
}
