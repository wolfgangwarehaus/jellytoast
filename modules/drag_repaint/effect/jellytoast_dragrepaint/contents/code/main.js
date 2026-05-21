/*
    jellytoast — full-repaint-on-drag KWin scripted effect.

    While a jellytoast window is being interactively moved, hold it under an
    in-progress transform. KWin renders any *transformed* window through
    paintGenericScreen() — a full-frame repaint — instead of the optimized
    paintSimpleScreen() partial-damage path. That partial-damage path is where
    the NVIDIA blur "stale rectangle" artifact lives (KWin bug 455526 / 457727),
    so forcing the full-repaint path for the duration of the drag avoids it.

    The transform must be an *in-progress* animate(), not a static set(): a
    completed set() lets KWin treat the window as a static transformed window
    and still optimize partial damage. An animation that is genuinely in
    progress is repainted by KWin every frame for its whole duration — the
    same continuous per-frame full repaint Wobbly Windows relies on. So we run
    a 6px Translation spread over an hour (~0.0017 px/s — an imperceptible
    drift) and cancel it the moment the drag ends.

    Caveat handled here: KWin's blur effect skips blur on *transformed*
    windows (shouldBlur(): `&& !WindowForceBlurRole`). Without compensation
    our own transform would drop the blur for the whole drag. So we set
    Effect.WindowForceBlurRole while the transform is held — the same trick
    the built-in `maximize` effect uses — and clear it when the drag ends.

    SPDX-License-Identifier: MIT
*/

/*global effects, animate, cancel, Effect, QEasingCurve, print */

"use strict";

function isJellytoast(window) {
    var cls = (window.windowClass || "").toLowerCase();
    var cap = (window.caption || "").toLowerCase();
    return cls.indexOf("jellytoast") !== -1 || cap.indexOf("jellytoast") !== -1;
}

function onMoveStart(window) {
    if (!isJellytoast(window)) {
        return;
    }
    if (window.jellytoastDragAnim !== undefined) {
        cancel(window.jellytoastDragAnim);
    }
    // Force blur ON before the transform lands — KWin's blur effect skips
    // transformed windows unless WindowForceBlurRole is set.
    window.setData(Effect.WindowForceBlurRole, true);
    // An in-progress animate() — KWin repaints an actively-animating window
    // every frame, forcing the full-repaint path for the whole drag.
    // Cancelled on move-finish, well before the hour elapses.
    window.jellytoastDragAnim = animate({
        window: window,
        duration: 3600000,
        curve: QEasingCurve.Linear,
        animations: [{
            type: Effect.Translation,
            from: { value1: 0, value2: 0 },
            to: { value1: 6, value2: 6 }
        }]
    });
}

function onMoveFinish(window) {
    if (window.jellytoastDragAnim !== undefined) {
        cancel(window.jellytoastDragAnim);
        window.jellytoastDragAnim = undefined;
        // Transform is gone — drop the force-blur override too.
        window.setData(Effect.WindowForceBlurRole, null);
    }
}

function manage(window) {
    window.windowStartUserMovedResized.connect(onMoveStart);
    window.windowFinishUserMovedResized.connect(onMoveFinish);
}

effects.windowAdded.connect(manage);
for (const window of effects.stackingOrder) {
    manage(window);
}

print("[jellytoast-dragrepaint] effect loaded");
