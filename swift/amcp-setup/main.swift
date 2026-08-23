// amcp-setup — the first-run wizard for Apple Music MCP.
//
// A splash that says what will happen, then one page per step, each carried
// out as you reach it rather than all at once at the end. Someone who stops
// halfway is left in a known state, and a step that fails says so on the page
// that caused it instead of in a summary after everything.
//
// Presentation only. This process draws windows and relays button presses; it
// installs nothing, knows nothing about LaunchAgents or config files, and needs
// no permissions of its own. Every decision and every action stays in Python,
// where it is tested and audited.
//
// Protocol — line-delimited JSON both ways. Python speaks first:
//
//   {"title":"...","icon":"/path/App.app","pages":[ ...Page... ]}
//
// then, whenever this side runs a step:
//
//   -> {"type":"run","page":"helper","selected":["claude-desktop"]}
//   <- {"type":"result","ok":true,"lines":["✓ Installed"]}
//
// and at the end exactly one of:
//
//   -> {"type":"finished"}
//   -> {"type":"cancel"}
//
// Cancel, closing the window and ⌘Q are the same message, and it is the one
// that stops: a wizard dismissed by accident must never read as consent.

import AppKit

// --- protocol ---------------------------------------------------------------

struct Option: Decodable {
    var id: String
    var label: String
    var detail: String?
    var note: String?
    var checked: Bool?
    var iconPath: String?
    var symbol: String?
}

struct Bullet: Decodable {
    var label: String
    var detail: String?
    var symbol: String?
    var iconPath: String?
}

struct Link: Decodable {
    var label: String
    var url: String
}

struct Page: Decodable {
    var id: String
    var title: String
    var body: String?
    var bullets: [Bullet]?
    var options: [Option]?
    /// Things the user could actually say once this is set up. Shown as
    /// speech bubbles: on a permission page, the most useful explanation of
    /// what you are granting is an example of what it lets you do.
    var examples: [String]?
    /// The quieter half: caveats, limits, how to undo it. Set below the
    /// examples in secondary text, so the page leads with what you get.
    var footer: String?
    var links: [Link]?
    /// Button that performs this page's work. Absent on pages that only read.
    var action: String?
    /// Button that moves on once the work is done (or immediately, if none).
    var next: String?
    /// Button that moves on WITHOUT doing the work. Only on optional steps --
    /// its presence is what tells the user a step can be declined on its own,
    /// as opposed to Cancel, which abandons the whole wizard.
    var skip: String?
}

struct Plan: Decodable {
    var title: String?
    var icon: String?
    var pages: [Page]
}

struct StepResult: Decodable {
    var ok: Bool
    var lines: [String]?
}

// --- stdio ------------------------------------------------------------------

/// Blocking line reader over stdin. Used on a background queue only.
final class LineReader {
    private var buffer = Data()
    private let handle = FileHandle.standardInput

    func next() -> Data? {
        while true {
            if let index = buffer.firstIndex(of: UInt8(ascii: "\n")) {
                let line = buffer[buffer.startIndex..<index]
                buffer = buffer[buffer.index(after: index)...]
                return Data(line)
            }
            let chunk = handle.availableData
            if chunk.isEmpty {
                guard !buffer.isEmpty else { return nil }
                let rest = buffer
                buffer = Data()
                return rest
            }
            buffer.append(chunk)
        }
    }
}

let reader = LineReader()
let stdio = DispatchQueue(label: "amcp-setup.stdio")

func send(_ object: [String: Any]) {
    guard let data = try? JSONSerialization.data(withJSONObject: object),
          var text = String(data: data, encoding: .utf8) else { return }
    text.append("\n")
    FileHandle.standardOutput.write(Data(text.utf8))
}

func finish(_ type: String) -> Never {
    send(["type": type])
    exit(0)
}

// --- metrics ----------------------------------------------------------------
//
// Human Interface Guidelines: 20pt margins, 8pt between related controls,
// 20pt between groups, 12pt between buttons.

/// One accent colour, matching the app icon. Enough to look like ours rather
/// than a stock template, without imitating any Apple product's palette.
private enum Brand {
    static let tint = NSColor(srgbRed: 0.42, green: 0.35, blue: 0.85, alpha: 1)

    /// Appearance-aware, because a 12% tint that reads as a soft card in light
    /// mode disappears entirely in dark mode.
    static let bubble = NSColor(name: nil) { appearance in
        appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua
            ? tint.withAlphaComponent(0.30)
            : tint.withAlphaComponent(0.12)
    }
}

/// Draws its own rounded background.
///
/// NSBox was the obvious choice and was wrong: it sizes its contentView by
/// autoresizing, so under Auto Layout the box collapsed to nothing and the
/// bubbles never appeared. Drawing in draw(_:) also fixes dark mode for free --
/// the colour is resolved against the view's effective appearance each time,
/// which a CALayer background colour is not.
final class BubbleView: NSView {
    override func draw(_ dirtyRect: NSRect) {
        let path = NSBezierPath(roundedRect: bounds, xRadius: 14, yRadius: 14)
        Brand.bubble.setFill()
        path.fill()
    }

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        needsDisplay = true
    }
}

private enum Metrics {
    static let margin: CGFloat = 24
    static let related: CGFloat = 8
    static let group: CGFloat = 20
    static let button: CGFloat = 12
    static let indent: CGFloat = 26
    static let windowWidth: CGFloat = 560
    static var contentWidth: CGFloat { windowWidth - margin * 2 }
}

// --- shared views -----------------------------------------------------------

func wrappingLabel(_ text: String, style: NSFont.TextStyle, colour: NSColor,
                   width: CGFloat) -> NSTextField {
    let label = NSTextField(wrappingLabelWithString: text)
    label.font = .preferredFont(forTextStyle: style)
    label.textColor = colour
    // Without this a wrapping label reports its whole string as intrinsic
    // width, overflows the container, and the stack view centres what it
    // cannot fit -- which looks exactly like broken alignment.
    label.preferredMaxLayoutWidth = width
    label.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
    return label
}

func iconView(iconPath: String?, symbol: String?, size: CGFloat) -> NSImageView? {
    var image: NSImage?
    if let iconPath, FileManager.default.fileExists(atPath: iconPath) {
        // NSWorkspace returns the icon *for* a file, which for an .icns is the
        // generic document icon rather than the artwork inside it.
        image = iconPath.hasSuffix(".icns")
            ? NSImage(contentsOfFile: iconPath)
            : NSWorkspace.shared.icon(forFile: iconPath)
    }
    if image == nil, let symbol {
        image = NSImage(
            systemSymbolName: symbol, accessibilityDescription: nil)?
            .withSymbolConfiguration(.init(pointSize: size, weight: .regular))
    }
    guard let image else { return nil }
    // Deliberately NOT setting image.size: pinning it to the display size makes
    // AppKit ask for that one representation, and on a cold Icon Services cache
    // the small variant may not exist yet -- which renders as a blank square.
    // Leaving the full set in place lets it scale from whichever rep is ready.
    let view = NSImageView(image: image)
    view.imageScaling = .scaleProportionallyUpOrDown
    view.setAccessibilityElement(false)
    view.translatesAutoresizingMaskIntoConstraints = false
    NSLayoutConstraint.activate([
        view.widthAnchor.constraint(equalToConstant: size),
        view.heightAnchor.constraint(equalToConstant: size),
    ])
    view.setContentHuggingPriority(.required, for: .horizontal)
    return view
}

/// icon on the left, text block on the right, both pinned to a known width so
/// nothing can overflow and re-centre itself.
func mediaRow(icon: NSView?, content: NSView, width: CGFloat) -> NSView {
    // A fixed width keeps rows left-aligned as a block even when the enclosing
    // stack is centred, which is what the splash does.
    let row = NSStackView()
    row.orientation = .horizontal
    row.alignment = .top
    row.spacing = Metrics.related
    row.distribution = .fill
    if let icon { row.addArrangedSubview(icon) }
    row.addArrangedSubview(content)
    content.setContentHuggingPriority(.defaultLow, for: .horizontal)
    row.translatesAutoresizingMaskIntoConstraints = false
    row.widthAnchor.constraint(equalToConstant: width).isActive = true
    return row
}

// --- the wizard -------------------------------------------------------------

final class WizardController: NSObject, NSWindowDelegate {
    private let plan: Plan
    private var index = 0
    private var window: NSWindow?

    // Rebuilt per page.
    private var checkboxes: [(id: String, button: NSButton)] = []
    private var actionButton: NSButton?
    private var nextButton: NSButton?
    private var spinner: NSProgressIndicator?
    private var skipButton: NSButton?
    private var openableLinks: [ObjectIdentifier: URL] = [:]
    private var resultStack: NSStackView?
    private var ranCurrentPage = false

    init(plan: Plan) { self.plan = plan }

    private var page: Page { plan.pages[index] }
    private var isLast: Bool { index == plan.pages.count - 1 }

    var snapshotView: NSView? { window?.contentView }

    func present(at page: Int = 0) {
        index = min(max(page, 0), plan.pages.count - 1)
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: Metrics.windowWidth, height: 460),
            styleMask: [.titled, .closable],
            backing: .buffered, defer: false)
        window.title = plan.title ?? "Setup"
        window.delegate = self
        window.isReleasedWhenClosed = false
        window.center()
        self.window = window
        render()
        window.makeKeyAndOrderFront(nil)
    }

    // --- page construction ---

    private func render() {
        checkboxes = []
        openableLinks = [:]
        actionButton = nil
        nextButton = nil
        skipButton = nil
        spinner = nil
        ranCurrentPage = false

        let body = NSStackView()
        body.orientation = .vertical
        body.alignment = .leading
        body.spacing = Metrics.related
        body.translatesAutoresizingMaskIntoConstraints = false

        // Splash carries the app icon; step pages carry a "Step n of m" label.
        if index == 0 {
            // Apple centres the icon and title on a welcome screen, then
            // left-aligns the detail beneath it.
            body.alignment = .centerX
            if let icon = iconView(iconPath: plan.icon, symbol: "music.note", size: 84) {
                body.addArrangedSubview(icon)
                body.setCustomSpacing(Metrics.group, after: icon)
            }
        } else {
            let steps = plan.pages.count - 2      // splash and summary excluded
            if index <= steps {
                let caption = wrappingLabel(
                    "Step \(index) of \(steps)", style: .caption1,
                    colour: .secondaryLabelColor, width: Metrics.contentWidth)
                body.addArrangedSubview(caption)
            }
        }

        let title = wrappingLabel(
            page.title, style: index == 0 ? .largeTitle : .title2,
            colour: .labelColor, width: Metrics.contentWidth)
        if index == 0 { title.alignment = .center }
        body.addArrangedSubview(title)
        body.setCustomSpacing(Metrics.related, after: title)

        if let text = page.body, !text.isEmpty {
            let label = wrappingLabel(
                text, style: .body, colour: .labelColor, width: Metrics.contentWidth)
            if index == 0 { label.alignment = .center }
            body.addArrangedSubview(label)
            body.setCustomSpacing(Metrics.group, after: label)
        }

        for example in page.examples ?? [] {
            let bubble = makeExample(example)
            body.addArrangedSubview(bubble)
            body.setCustomSpacing(6, after: bubble)
        }
        if page.examples?.isEmpty == false, let last = body.arrangedSubviews.last {
            body.setCustomSpacing(Metrics.group, after: last)
        }

        if let footer = page.footer, !footer.isEmpty {
            let label = wrappingLabel(
                footer, style: .callout, colour: .secondaryLabelColor,
                width: Metrics.contentWidth)
            if index == 0 { label.alignment = .center }
            body.addArrangedSubview(label)
            body.setCustomSpacing(Metrics.group, after: label)
        }

        for link in page.links ?? [] {
            if let view = makeLink(link) {
                body.addArrangedSubview(view)
                body.setCustomSpacing(Metrics.related, after: view)
            }
        }

        for bullet in page.bullets ?? [] {
            body.addArrangedSubview(makeBullet(bullet))
        }
        for option in page.options ?? [] {
            body.addArrangedSubview(makeOption(option))
        }

        let results = NSStackView()
        results.orientation = .vertical
        results.alignment = .leading
        results.spacing = 4
        results.translatesAutoresizingMaskIntoConstraints = false
        resultStack = results
        body.setCustomSpacing(Metrics.group, after: body.arrangedSubviews.last ?? body)
        body.addArrangedSubview(results)

        let scroll = NSScrollView()
        scroll.hasVerticalScroller = true
        scroll.autohidesScrollers = true
        scroll.drawsBackground = false
        scroll.translatesAutoresizingMaskIntoConstraints = false
        let document = NSView()
        document.translatesAutoresizingMaskIntoConstraints = false
        document.addSubview(body)
        scroll.documentView = document

        let buttons = makeButtons()
        let root = NSView()
        root.addSubview(scroll)
        root.addSubview(buttons)

        NSLayoutConstraint.activate([
            document.widthAnchor.constraint(equalTo: scroll.contentView.widthAnchor),
            body.topAnchor.constraint(equalTo: document.topAnchor, constant: Metrics.margin),
            body.leadingAnchor.constraint(
                equalTo: document.leadingAnchor, constant: Metrics.margin),
            body.trailingAnchor.constraint(
                lessThanOrEqualTo: document.trailingAnchor, constant: -Metrics.margin),
            body.bottomAnchor.constraint(equalTo: document.bottomAnchor),

            scroll.topAnchor.constraint(equalTo: root.topAnchor),
            scroll.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            scroll.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            scroll.bottomAnchor.constraint(
                equalTo: buttons.topAnchor, constant: -Metrics.related),

            buttons.leadingAnchor.constraint(
                equalTo: root.leadingAnchor, constant: Metrics.margin),
            buttons.trailingAnchor.constraint(
                equalTo: root.trailingAnchor, constant: -Metrics.margin),
            buttons.bottomAnchor.constraint(
                equalTo: root.bottomAnchor, constant: -Metrics.margin),
        ])
        window?.contentView = root

        // Fit the window to the page rather than scrolling a fixed box: the
        // splash was taller than 460pt, so its title was cut off at the top.
        root.layoutSubtreeIfNeeded()
        let needed = body.fittingSize.height
            + Metrics.margin * 2
            + buttons.fittingSize.height
            + Metrics.related
            + Metrics.margin
        let height = min(max(needed, 360), 760)
        window?.setContentSize(NSSize(width: Metrics.windowWidth, height: height))
        window?.center()
    }

    /// A speech bubble holding something the user might say.
    private func makeExample(_ text: String) -> NSView {
        let inset = NSSize(width: 14, height: 10)
        let label = wrappingLabel(
            "\u{201C}\(text)\u{201D}", style: .body, colour: .labelColor,
            width: Metrics.contentWidth - inset.width * 2)
        label.translatesAutoresizingMaskIntoConstraints = false

        let bubble = BubbleView()
        bubble.translatesAutoresizingMaskIntoConstraints = false
        bubble.addSubview(label)
        NSLayoutConstraint.activate([
            label.leadingAnchor.constraint(
                equalTo: bubble.leadingAnchor, constant: inset.width),
            label.trailingAnchor.constraint(
                equalTo: bubble.trailingAnchor, constant: -inset.width),
            label.topAnchor.constraint(equalTo: bubble.topAnchor, constant: inset.height),
            label.bottomAnchor.constraint(
                equalTo: bubble.bottomAnchor, constant: -inset.height),
            bubble.widthAnchor.constraint(
                lessThanOrEqualToConstant: Metrics.contentWidth),
        ])
        return bubble
    }

    /// A text link that opens in the browser.
    ///
    /// https only, and validated here rather than trusted: the plan arrives on
    /// stdin, and a process that opens whatever URL it is handed is a more
    /// useful thing to compromise than one that does not.
    private func makeLink(_ link: Link) -> NSView? {
        guard let url = URL(string: link.url), url.scheme == "https" else { return nil }

        let button = NSButton(title: link.label, target: self, action: #selector(openLink(_:)))
        button.bezelStyle = .inline
        button.isBordered = false
        button.contentTintColor = .linkColor
        button.toolTip = link.url
        button.attributedTitle = NSAttributedString(
            string: link.label,
            attributes: [
                .foregroundColor: NSColor.linkColor,
                .underlineStyle: NSUnderlineStyle.single.rawValue,
                .font: NSFont.preferredFont(forTextStyle: .body),
            ])
        openableLinks[ObjectIdentifier(button)] = url
        return button
    }

    @objc private func openLink(_ sender: NSButton) {
        guard let url = openableLinks[ObjectIdentifier(sender)] else { return }
        NSWorkspace.shared.open(url)
    }

    private func makeBullet(_ bullet: Bullet) -> NSView {
        let text = NSStackView()
        text.orientation = .vertical
        text.alignment = .leading
        text.spacing = 2
        let width = Metrics.contentWidth - Metrics.indent

        text.addArrangedSubview(
            wrappingLabel(bullet.label, style: .body, colour: .labelColor, width: width))
        if let detail = bullet.detail, !detail.isEmpty {
            text.addArrangedSubview(
                wrappingLabel(
                    detail, style: .caption1, colour: .secondaryLabelColor, width: width))
        }
        let icon = iconView(iconPath: bullet.iconPath, symbol: bullet.symbol, size: 20)
        return mediaRow(icon: icon, content: text, width: Metrics.contentWidth)
    }

    private func makeOption(_ option: Option) -> NSView {
        let text = NSStackView()
        text.orientation = .vertical
        text.alignment = .leading
        text.spacing = 2
        let width = Metrics.contentWidth - Metrics.indent - Metrics.related

        let checkbox = NSButton(checkboxWithTitle: option.label, target: nil, action: nil)
        checkbox.state = (option.checked ?? false) ? .on : .off
        checkboxes.append((option.id, checkbox))
        text.addArrangedSubview(checkbox)

        var help: [String] = []
        for (value, colour) in [
            (option.detail, NSColor.secondaryLabelColor),
            (option.note, NSColor.systemOrange),
        ] {
            guard let value, !value.isEmpty else { continue }
            help.append(value)
            let label = wrappingLabel(value, style: .caption1, colour: colour, width: width - 18)
            label.setAccessibilityElement(false)
            let indented = NSStackView(views: [label])
            indented.orientation = .horizontal
            indented.edgeInsets = NSEdgeInsets(top: 0, left: 18, bottom: 0, right: 0)
            text.addArrangedSubview(indented)
        }
        if !help.isEmpty { checkbox.setAccessibilityHelp(help.joined(separator: ". ")) }

        let icon = iconView(iconPath: option.iconPath, symbol: option.symbol, size: 26)
        return mediaRow(icon: icon, content: text, width: Metrics.contentWidth)
    }

    private func makeButtons() -> NSView {
        let row = NSStackView()
        row.orientation = .horizontal
        row.spacing = Metrics.button
        row.translatesAutoresizingMaskIntoConstraints = false

        let progress = NSProgressIndicator()
        progress.style = .spinning
        progress.controlSize = .small
        progress.isDisplayedWhenStopped = false
        progress.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            progress.widthAnchor.constraint(equalToConstant: 16),
            progress.heightAnchor.constraint(equalToConstant: 16),
        ])
        spinner = progress
        row.addArrangedSubview(progress)

        let spacer = NSView()
        spacer.setContentHuggingPriority(.defaultLow, for: .horizontal)
        row.addArrangedSubview(spacer)

        if !isLast {
            let cancel = NSButton(title: "Cancel", target: self, action: #selector(cancel))
            cancel.bezelStyle = .rounded
            cancel.keyEquivalent = "\u{1b}"
            row.addArrangedSubview(cancel)
        }

        if let skip = page.skip, page.action != nil {
            let button = NSButton(title: skip, target: self, action: #selector(advance))
            button.bezelStyle = .rounded
            skipButton = button
            row.addArrangedSubview(button)
        }

        if let action = page.action {
            let button = NSButton(title: action, target: self, action: #selector(runAction))
            button.bezelStyle = .rounded
            button.keyEquivalent = "\r"
            actionButton = button
            row.addArrangedSubview(button)
        } else {
            let button = NSButton(
                title: page.next ?? (isLast ? "Done" : "Continue"),
                target: self, action: #selector(advance))
            button.bezelStyle = .rounded
            button.keyEquivalent = "\r"
            nextButton = button
            row.addArrangedSubview(button)
        }
        return row
    }

    // --- running a step ---

    @objc private func runAction() {
        guard !ranCurrentPage else { return advance() }
        actionButton?.isEnabled = false
        spinner?.startAnimation(nil)

        let selected = checkboxes.filter { $0.button.state == .on }.map(\.id)
        let request: [String: Any] = [
            "type": "run", "page": page.id, "selected": selected,
        ]
        stdio.async {
            send(request)
            let line = reader.next()
            let result: StepResult
            if let line, let decoded = try? JSONDecoder().decode(StepResult.self, from: line) {
                result = decoded
            } else {
                // Losing the other side mid-step is a failure, not a success.
                result = StepResult(ok: false, lines: ["✗ setup stopped responding"])
            }
            DispatchQueue.main.async { self.finished(result) }
        }
    }

    private func finished(_ result: StepResult) {
        spinner?.stopAnimation(nil)
        ranCurrentPage = true

        for line in result.lines ?? [] {
            let colour: NSColor =
                line.hasPrefix("✗") ? .systemRed
                : line.hasPrefix("•") ? .secondaryLabelColor : .labelColor
            resultStack?.addArrangedSubview(
                wrappingLabel(line, style: .callout, colour: colour,
                              width: Metrics.contentWidth))
        }
        // Checkboxes describe work already done; freeze them.
        for (_, box) in checkboxes { box.isEnabled = false }

        // The step has run, so declining it is no longer one of the choices.
        skipButton?.isHidden = true
        actionButton?.title = isLast ? "Done" : "Continue"
        actionButton?.isEnabled = true
    }

    @objc private func advance() {
        if isLast { finish("finished") }
        index += 1
        render()
    }

    @objc private func cancel() { finish("cancel") }

    func windowWillClose(_ notification: Notification) { finish("cancel") }
}

// --- application ------------------------------------------------------------

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var controller: WizardController?
    private let plan: Plan

    init(plan: Plan) { self.plan = plan }

    func applicationDidFinishLaunching(_ notification: Notification) {
        installMenu()
        let controller = WizardController(plan: plan)
        controller.present()
        self.controller = controller
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    func applicationWillTerminate(_ notification: Notification) { send(["type": "cancel"]) }

    /// Without a main menu ⌘Q and ⌘W do nothing, and the window cannot be
    /// dismissed from the keyboard -- which reads as a broken app.
    private func installMenu() {
        let name = ProcessInfo.processInfo.processName
        let appMenu = NSMenu()
        appMenu.addItem(
            withTitle: "Close", action: #selector(NSWindow.performClose(_:)), keyEquivalent: "w")
        appMenu.addItem(.separator())
        appMenu.addItem(
            withTitle: "Quit \(name)",
            action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q")
        let item = NSMenuItem()
        item.submenu = appMenu
        let main = NSMenu()
        main.addItem(item)
        NSApp.mainMenu = main
    }
}

// --- entry ------------------------------------------------------------------

guard let first = reader.next(),
      let plan = try? JSONDecoder().decode(Plan.self, from: first),
      !plan.pages.isEmpty else {
    FileHandle.standardError.write(Data("amcp-setup: could not read the plan\n".utf8))
    finish("cancel")
}

// --render <path> [--page N]: lay the page out offscreen, write a PNG, exit.
// Iterating on a UI by asking someone to look at it is slow and imprecise; this
// makes the layout inspectable directly.
if let flag = CommandLine.arguments.firstIndex(of: "--render"),
   CommandLine.arguments.count > flag + 1 {
    let path = CommandLine.arguments[flag + 1]
    var wanted = 0
    if let pageFlag = CommandLine.arguments.firstIndex(of: "--page"),
       CommandLine.arguments.count > pageFlag + 1 {
        wanted = Int(CommandLine.arguments[pageFlag + 1]) ?? 0
    }
    let app = NSApplication.shared
    app.setActivationPolicy(.prohibited)
    let controller = WizardController(plan: plan)
    controller.present(at: min(wanted, plan.pages.count - 1))
    guard let view = controller.snapshotView else { exit(1) }
    view.layoutSubtreeIfNeeded()
    guard let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { exit(1) }
    view.cacheDisplay(in: view.bounds, to: rep)
    guard let png = rep.representation(using: .png, properties: [:]) else { exit(1) }
    try? png.write(to: URL(fileURLWithPath: path))
    exit(0)
}

let application = NSApplication.shared
application.setActivationPolicy(.regular)
let delegate = AppDelegate(plan: plan)
application.delegate = delegate
application.run()
