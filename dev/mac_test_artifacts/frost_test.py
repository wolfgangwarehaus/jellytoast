"""Green-backdrop vibrancy test (PR219 mac QA, reboot #2).

Puts a pure-green NSWindow behind an NSVisualEffectView window
(behindWindow blending). If WindowServer vibrancy works, the effect
window's pixels sample the green behind it. Windows stay up ~90s so
the harness can screencapture the region.
"""
import AppKit
import Foundation

ws = AppKit.NSWorkspace.sharedWorkspace()
print("api reduceTransparency:", ws.accessibilityDisplayShouldReduceTransparency())
print("api increaseContrast:", ws.accessibilityDisplayShouldIncreaseContrast())

app = AppKit.NSApplication.sharedApplication()
app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyAccessory)

# Green backdrop window: Cocoa coords (bottom-left origin)
GREEN = ((200.0, 300.0), (600.0, 400.0))
green_win = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
    AppKit.NSMakeRect(GREEN[0][0], GREEN[0][1], GREEN[1][0], GREEN[1][1]),
    AppKit.NSWindowStyleMaskBorderless, AppKit.NSBackingStoreBuffered, False)
green_win.setBackgroundColor_(AppKit.NSColor.greenColor())
green_win.setLevel_(AppKit.NSNormalWindowLevel)
green_win.orderFrontRegardless()

# Effect window fully inside the green one
FX = ((300.0, 350.0), (400.0, 300.0))
fx_win = AppKit.NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
    AppKit.NSMakeRect(FX[0][0], FX[0][1], FX[1][0], FX[1][1]),
    AppKit.NSWindowStyleMaskBorderless, AppKit.NSBackingStoreBuffered, False)
fx_win.setOpaque_(False)
fx_win.setBackgroundColor_(AppKit.NSColor.clearColor())
fx = AppKit.NSVisualEffectView.alloc().initWithFrame_(
    AppKit.NSMakeRect(0, 0, FX[1][0], FX[1][1]))
fx.setBlendingMode_(AppKit.NSVisualEffectBlendingModeBehindWindow)
fx.setMaterial_(AppKit.NSVisualEffectMaterialHUDWindow)
fx.setState_(AppKit.NSVisualEffectStateActive)
fx_win.setContentView_(fx)
fx_win.setLevel_(AppKit.NSFloatingWindowLevel)
fx_win.orderFrontRegardless()

screen_h = AppKit.NSScreen.mainScreen().frame().size.height
def topleft(rect):
    (x, y), (w, h) = rect
    return "%d,%d,%d,%d" % (x, screen_h - (y + h), w, h)

# Regions for `screencapture -R` (top-left origin):
print("fx_region:", topleft(((FX[0][0] + 50, FX[0][1] + 50), (FX[1][0] - 100, FX[1][1] - 100))))
print("green_region:", topleft(((210.0, 320.0), (80.0, 360.0))))
print("READY")
import sys; sys.stdout.flush()

Foundation.NSRunLoop.currentRunLoop().runUntilDate_(
    Foundation.NSDate.dateWithTimeIntervalSinceNow_(90.0))
