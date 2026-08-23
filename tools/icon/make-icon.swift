// Renders the app icon. Run via tools/icon/build.sh; output is AppleMusicMCP.icns.
//
// Generated rather than hand-drawn so the icon is reproducible and reviewable
// as source. Deliberately NOT red-and-pink: this is not an Apple product, and
// an icon that mimics Apple Music's would imply it is one.

import AppKit

let sizes = [16, 32, 64, 128, 256, 512, 1024]
let out = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "AppleMusicMCP.iconset"
try? FileManager.default.createDirectory(
    atPath: out, withIntermediateDirectories: true)

func draw(size: Int) -> NSImage {
    let side = CGFloat(size)
    let image = NSImage(size: NSSize(width: side, height: side))
    image.lockFocus()
    defer { image.unlockFocus() }

    // macOS icons sit inside a rounded square with a margin, not edge to edge.
    let inset = side * 0.08
    let rect = NSRect(x: inset, y: inset, width: side - inset * 2, height: side - inset * 2)
    let radius = rect.width * 0.225           // Big Sur squircle proportion
    let body = NSBezierPath(roundedRect: rect, xRadius: radius, yRadius: radius)

    NSGradient(
        colors: [
            NSColor(srgbRed: 0.42, green: 0.35, blue: 0.85, alpha: 1),
            NSColor(srgbRed: 0.24, green: 0.20, blue: 0.60, alpha: 1),
        ]
    )?.draw(in: body, angle: -90)

    // A soft top highlight, the way Apple's own icons catch light.
    NSGraphicsContext.current?.saveGraphicsState()
    body.addClip()
    NSGradient(
        colors: [
            NSColor(white: 1, alpha: 0.22),
            NSColor(white: 1, alpha: 0.0),
        ]
    )?.draw(in: NSRect(x: rect.minX, y: rect.midY, width: rect.width, height: rect.height / 2),
            angle: -90)
    NSGraphicsContext.current?.restoreGraphicsState()

    let glyphSide = rect.width * 0.52
    if let symbol = NSImage(
        systemSymbolName: "music.note", accessibilityDescription: "Apple Music MCP") {
        let config = NSImage.SymbolConfiguration(
            pointSize: glyphSide, weight: .semibold)
        if let glyph = symbol.withSymbolConfiguration(config) {
            let g = glyph.size
            let scale = min(glyphSide / g.width, glyphSide / g.height)
            let drawn = NSSize(width: g.width * scale, height: g.height * scale)
            let origin = NSPoint(
                x: rect.midX - drawn.width / 2, y: rect.midY - drawn.height / 2)
            // Tint to white. A symbol drawn directly keeps its own colour
            // (black), so it is redrawn into a template and filled sourceAtop.
            let tinted = NSImage(size: drawn, flipped: false) { bounds in
                glyph.draw(in: bounds)
                NSColor.white.set()
                bounds.fill(using: .sourceAtop)
                return true
            }
            tinted.draw(
                in: NSRect(origin: origin, size: drawn),
                from: .zero, operation: .sourceOver, fraction: 1.0)
        }
    }
    return image
}

func write(_ image: NSImage, to path: String) {
    guard let tiff = image.tiffRepresentation,
          let rep = NSBitmapImageRep(data: tiff),
          let png = rep.representation(using: .png, properties: [:]) else { return }
    try? png.write(to: URL(fileURLWithPath: path))
}

for size in sizes {
    let image = draw(size: size)
    if size <= 512 {
        write(image, to: "\(out)/icon_\(size)x\(size).png")
    }
    if size >= 32 {
        write(image, to: "\(out)/icon_\(size / 2)x\(size / 2)@2x.png")
    }
}
print("wrote \(out)")
