"""Mean R,G,B of a PNG via NSBitmapImageRep."""
import sys
import AppKit

rep = AppKit.NSBitmapImageRep.imageRepWithContentsOfFile_(sys.argv[1])
w, h = rep.pixelsWide(), rep.pixelsHigh()
step = max(1, w // 60), max(1, h // 60)
rs = gs = bs = n = 0
for x in range(0, w, step[0]):
    for y in range(0, h, step[1]):
        c = rep.colorAtX_y_(x, y)
        rs += c.redComponent(); gs += c.greenComponent(); bs += c.blueComponent()
        n += 1
print("%s mean RGB: %.3f %.3f %.3f (n=%d, %dx%d)" %
      (sys.argv[2] if len(sys.argv) > 2 else "", rs/n, gs/n, bs/n, n, w, h))
