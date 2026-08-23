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

struct Page: Decodable {
    var id: String
    var title: String
    var body: String?
    var bullets: [Bullet]?
    var options: [Option]?
    /// Button that performs this page's work. Absent on pages that only read.
    var action: String?
    /// Button that moves on once the work is done (or immediately, if none).
    var next: String?
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
        image = NSWorkspace.shared.icon(forFile: iconPath)
    }
    if image == nil, let symbol {
        image = NSImage(
            systemSymbolName: symbol, accessibilityDescription: nil)?
            .withSymbolConfiguration(.init(pointSize: size, weight: .regular))
    }
    guard let image else { return nil }
    image.size = NSSize(width: size, height: size)
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
    private var resultStack: NSStackView?
    private var ranCurrentPage = false

    init(plan: Plan) { self.plan = plan }

    private var page: Page { plan.pages[index] }
    private var isLast: Bool { index == plan.pages.count - 1 }

    func present() {
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
        actionButton = nil
        nextButton = nil
        spinner = nil
        ranCurrentPage = false

        let body = NSStackView()
        body.orientation = .vertical
        body.alignment = .leading
        body.spacing = Metrics.related
        body.translatesAutoresizingMaskIntoConstraints = false

        // Splash carries the app icon; step pages carry a "Step n of m" label.
        if index == 0 {
            if let icon = iconView(iconPath: plan.icon, symbol: "music.note", size: 72) {
                body.addArrangedSubview(icon)
                body.setCustomSpacing(Metrics.related * 2, after: icon)
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
        body.addArrangedSubview(title)
        body.setCustomSpacing(Metrics.related, after: title)

        if let text = page.body, !text.isEmpty {
            let label = wrappingLabel(
                text, style: .body, colour: .labelColor, width: Metrics.contentWidth)
            body.addArrangedSubview(label)
            body.setCustomSpacing(Metrics.group, after: label)
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

let application = NSApplication.shared
application.setActivationPolicy(.regular)
let delegate = AppDelegate(plan: plan)
application.delegate = delegate
application.run()
