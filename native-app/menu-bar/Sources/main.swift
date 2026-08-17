import Cocoa
import ServiceManagement

final class AppDelegate: NSObject, NSApplicationDelegate, NSMenuDelegate {

    private var statusItem: NSStatusItem!
    private var controller: ServerController?

    // Menu items that need dynamic updates
    private let toggleItem = NSMenuItem()
    private let toggleSwitch = SwitchControl()
    private let loginItem = NSMenuItem()
    private let chatItem = NSMenuItem()
    private let endpointItem = NSMenuItem()
    /// Startup progress ("Loading model (42%)", "Loading chat interface").
    private let statusLine = NSMenuItem()

    private var menuRefreshTimer: Timer?

    private let spinner = SpinnerIcon()
    /// Total bytes of model + draft weights, the denominator of the
    /// load-progress estimate. Reset when the server goes down.
    private var expectedModelBytes: UInt64?
    private var chatLaunchStarted: Date?

    private static let chatBundleId = "com.quickbot.chat"
    /// ⌘; and ⌘⇧; while Quickbot is off (Quickbot Chat owns them while on).
    private let hotkeys = GlobalHotkeys()

    /// Name of the lucide icon currently shown ("bot" or "bot-off").
    /// Published to NSUserDefaults for external inspection/diagnostics.
    private(set) var currentIconName = ""

    // MARK: - Lifecycle

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory) // no Dock icon

        // Single instance: avoids two icons in the menu bar (e.g. `open -n`).
        // The old instance stays alive; the new one simply quits.
        let me = ProcessInfo.processInfo.processIdentifier
        let others = NSRunningApplication.runningApplications(
            withBundleIdentifier: Bundle.main.bundleIdentifier ?? "com.quickbot.app"
        ).filter { $0.processIdentifier != me }
        if !others.isEmpty {
            NSLog("Quickbot: already running, terminating this instance")
            NSApp.terminate(nil)
            return
        }

        let server = ServerController()
        server.onStateChange = { [weak self] state in
            self?.render(state)
        }
        controller = server

        buildStatusItem()
        buildMenu()

        // While Quickbot is off, ⌘; and ⌘⇧; open this menu instead of the
        // (terminated) chat app, revealing the off state.
        hotkeys.onPress = { [weak self] in
            self?.statusItem?.button?.performClick(nil)
        }

        render(.stopped)

        server.bootstrap()
    }

    func applicationWillTerminate(_ notification: Notification) {
        // controller may be nil if we quit early (duplicate instance).
        controller?.shutdown()
    }

    // MARK: - Status item

    private func buildStatusItem() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.behavior = .removalAllowed
        if let button = statusItem.button {
            button.imagePosition = .imageOnly
            button.toolTip = "Quickbot"
        }
        applyIcon(running: false)
    }

    /// Single point that sets the tray icon from the on/off state.
    private func applyIcon(running: Bool) {
        let name = Icons.name(forRunning: running)
        statusItem.button?.image = Icons.image(forRunning: running)
        statusItem.button?.image?.accessibilityDescription = name
        currentIconName = name
        // Mirrors the current icon for external inspection (tests/diagnostics).
        UserDefaults.standard.set(name, forKey: "currentIcon")
    }

    // MARK: - Menu

    private func buildMenu() {
        let menu = NSMenu()
        menu.delegate = self
        // Without this AppKit recalculates isEnabled on its own and ignores the
        // state we set (e.g. keeping the toggle disabled during "Stopping…").
        menu.autoenablesItems = false

        // Main toggle, first item: a real switch (like the Wi-Fi menu),
        // hosted in a custom view. Clicking it keeps the menu open, so the
        // state transition is visible live.
        let row = NSView(frame: NSRect(x: 0, y: 0, width: 240, height: 30))
        row.autoresizingMask = [.width]

        let label = NSTextField(labelWithString: "Quickbot")
        label.font = NSFont.boldSystemFont(ofSize: NSFont.systemFontSize)
        label.sizeToFit()
        label.frame.origin = NSPoint(x: 14, y: (row.frame.height - label.frame.height) / 2)
        row.addSubview(label)

        toggleSwitch.frame.origin = NSPoint(
            x: row.frame.width - toggleSwitch.frame.width - 14,
            y: (row.frame.height - toggleSwitch.frame.height) / 2
        )
        toggleSwitch.autoresizingMask = [.minXMargin]
        toggleSwitch.target = self
        toggleSwitch.action = #selector(switchToggled(_:))
        row.addSubview(toggleSwitch)

        toggleItem.view = row
        menu.addItem(toggleItem)

        statusLine.isEnabled = false
        statusLine.isHidden = true
        menu.addItem(statusLine)

        menu.addItem(.separator())

        // The ⌘⇧; equivalent is owned by Quickbot Chat (global hotkey); here
        // it is mainly advertisement, though it also fires while this menu
        // is open, doing the same thing.
        chatItem.title = "Show chat"
        chatItem.action = #selector(showChatWindow)
        chatItem.keyEquivalent = ";"
        chatItem.keyEquivalentModifierMask = [.command, .shift]
        chatItem.target = self
        chatItem.isEnabled = true
        menu.addItem(chatItem)

        endpointItem.title = "Copy API endpoint"
        endpointItem.action = #selector(copyEndpoint)
        endpointItem.target = self
        endpointItem.isEnabled = true
        menu.addItem(endpointItem)

        loginItem.title = "Start Quickbot at login"
        loginItem.action = #selector(toggleLoginItem)
        loginItem.target = self
        loginItem.isEnabled = true
        menu.addItem(loginItem)

        menu.addItem(.separator())

        let quitItem = NSMenuItem(title: "Quit Quickbot", action: #selector(quit), keyEquivalent: "q")
        quitItem.target = self
        quitItem.isEnabled = true
        menu.addItem(quitItem)

        statusItem.menu = menu

        // Diagnostics: publishes the built items (visible with `quickbot status -v`).
        UserDefaults.standard.set(menu.items.map { $0.title.isEmpty ? "—" : $0.title },
                                  forKey: "menuItems")
    }

    // Refreshes every second while the menu is open (uptime).
    func menuWillOpen(_ menu: NSMenu) {
        refreshMenuTexts()
        menuRefreshTimer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
            self?.refreshMenuTexts()
        }
        if let timer = menuRefreshTimer {
            RunLoop.current.add(timer, forMode: .common)
        }
    }

    func menuDidClose(_ menu: NSMenu) {
        menuRefreshTimer?.invalidate()
        menuRefreshTimer = nil
    }

    // MARK: - Actions

    @objc private func switchToggled(_ sender: SwitchControl) {
        if sender.isOn {
            controller?.start()
        } else {
            controller?.stop() // during startup this cancels it
        }
    }

    /// Brings up Quickbot Chat's main window, launching the app if needed.
    /// The chat app starts silently (LSUIElement, hidden window), so it is
    /// asked to show itself: via distributed notification when running, or
    /// via the --show-window launch argument otherwise.
    @objc private func showChatWindow() {
        let chatBundleId = "com.quickbot.chat"
        if !NSRunningApplication.runningApplications(withBundleIdentifier: chatBundleId).isEmpty {
            DistributedNotificationCenter.default().postNotificationName(
                Notification.Name("com.quickbot.chat.showWindow"),
                object: nil, userInfo: nil, deliverImmediately: true
            )
        } else if let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: chatBundleId) {
            let configuration = NSWorkspace.OpenConfiguration()
            configuration.arguments = ["--show-window"]
            configuration.activates = true
            NSWorkspace.shared.openApplication(at: url, configuration: configuration)
        } else {
            NSLog("Quickbot: Quickbot Chat.app not found")
        }
    }

    @objc private func copyEndpoint() {
        let pb = NSPasteboard.general
        pb.clearContents()
        pb.setString(endpoint, forType: .string)
    }

    @objc private func quit() {
        NSApp.terminate(nil)
    }

    /// Toggles automatic launch at login (ServiceManagement, macOS 13+).
    @objc private func toggleLoginItem() {
        let service = SMAppService.mainApp
        do {
            if service.status == .enabled {
                try service.unregister()
            } else {
                try service.register()
            }
        } catch {
            let alert = NSAlert()
            alert.messageText = "Could not change launch at login"
            alert.informativeText = error.localizedDescription
            alert.alertStyle = .warning
            NSApp.activate(ignoringOtherApps: true)
            alert.runModal()
        }
        refreshMenuTexts()
    }

    // MARK: - Rendering

    private var endpoint: String {
        controller?.status?.endpoint ?? "http://127.0.0.1:8080/v1"
    }

    private func render(_ state: ServerState) {
        // The icon is the central requirement: bot = on, bot-off = off —
        // except while starting, when a spinner takes its place.
        if state == .starting {
            spinner.start(on: statusItem.button)
            currentIconName = "loading"
            UserDefaults.standard.set("loading", forKey: "currentIcon")
            statusItem.button?.appearsDisabled = false
        } else {
            spinner.stop()
            applyIcon(running: state == .running)
            statusItem.button?.appearsDisabled = state == .stopping
        }
        if state == .stopped { expectedModelBytes = nil }
        UserDefaults.standard.set(state.label, forKey: "currentState")

        var tooltip = "Quickbot: \(state.label)"
        if case .failed(let msg) = state { tooltip += "\n\(msg)" }
        statusItem.button?.toolTip = tooltip

        updateChatIntegration(state)
        refreshMenuTexts()
    }

    /// The toggle governs the whole stack: with the server up, Quickbot Chat
    /// runs (silently) and owns ⌘;/⌘⇧;; with it down, the chat app is
    /// unloaded from memory, its menu entries disappear, and the shortcuts
    /// fall back to opening this menu.
    private var chatIntegrationServerUp: Bool?

    private func updateChatIntegration(_ state: ServerState) {
        let serverUp = (state == .running || state == .starting)
        chatItem.isHidden = !serverUp
        endpointItem.isHidden = !serverUp

        guard serverUp != chatIntegrationServerUp else { return }
        let previous = chatIntegrationServerUp
        chatIntegrationServerUp = serverUp

        if serverUp {
            hotkeys.unregister()
            launchChatAppIfNeeded()
        } else {
            // Only kill the chat app on an actual on→off transition; the
            // initial render(.stopped) before bootstrap adopts a running
            // server must not take a healthy chat down.
            if previous == true { quitChatApp() }
            hotkeys.register()
        }
    }

    private func launchChatAppIfNeeded() {
        guard NSRunningApplication.runningApplications(withBundleIdentifier: Self.chatBundleId).isEmpty,
              let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: Self.chatBundleId)
        else { return }
        chatLaunchStarted = Date()
        let configuration = NSWorkspace.OpenConfiguration()
        configuration.activates = false
        NSWorkspace.shared.openApplication(at: url, configuration: configuration)
    }

    private func quitChatApp() {
        NSRunningApplication.runningApplications(withBundleIdentifier: Self.chatBundleId)
            .forEach { $0.terminate() }
    }

    /// Text for the startup status line, or nil when there is nothing to report.
    private func startupStatusText(_ state: ServerState) -> String? {
        switch state {
        case .starting:
            if expectedModelBytes == nil, let status = controller?.status {
                let paths = [status.modelPath] + [status.draftModelPath].compactMap { $0 }
                expectedModelBytes = paths.reduce(0) { $0 + LoadingProgress.directoryBytes($1) }
            }
            if let pid = controller?.status?.pid,
               let expected = expectedModelBytes, expected > 0,
               let footprint = LoadingProgress.processFootprint(pid: pid) {
                let percent = min(99, Int(footprint &* 100 / expected))
                return "Loading model (\(percent)%)"
            }
            return "Loading model…"
        case .running:
            let chatApp = NSRunningApplication.runningApplications(withBundleIdentifier: Self.chatBundleId).first
            if let chatApp {
                if !chatApp.isFinishedLaunching { return "Loading chat interface" }
            } else if let started = chatLaunchStarted, Date().timeIntervalSince(started) < 10 {
                return "Loading chat interface"
            }
            return nil
        default:
            return nil
        }
    }

    private func refreshMenuTexts() {
        let state = controller?.state ?? .stopped

        // Switch: position mirrors the target state (on while starting, so
        // flipping it off cancels the startup). Setting isOn in code does
        // not fire the action. The green accent waits for everything to be
        // actually loaded.
        toggleSwitch.isOn = (state == .running || state == .starting)
        toggleSwitch.isEnabled = state != .stopping
        toggleSwitch.showsAccent = (state == .running)

        if let text = startupStatusText(state) {
            statusLine.attributedTitle = NSAttributedString(string: text, attributes: [
                .font: NSFont.systemFont(ofSize: 12),
                .foregroundColor: NSColor.secondaryLabelColor,
            ])
            statusLine.isHidden = false
        } else {
            statusLine.isHidden = true
        }

        loginItem.state = (SMAppService.mainApp.status == .enabled) ? .on : .off

        // Diagnostics: mirrors the switch exactly as the user sees it in the menu.
        let defaults = UserDefaults.standard
        defaults.set(toggleSwitch.isOn ? "On" : "Off", forKey: "toggleTitle")
        defaults.set(toggleSwitch.isEnabled, forKey: "toggleEnabled")
        defaults.set(statusItem.menu?.autoenablesItems ?? true, forKey: "menuAutoenables")
        defaults.set(statusItem.menu?.items.map { item in
            let title = item.title.isEmpty ? "—" : item.title
            return item.isHidden ? "\(title) (hidden)" : title
        } ?? [], forKey: "menuItems")
        defaults.set(statusLine.isHidden ? "" : (statusLine.attributedTitle?.string ?? ""),
                     forKey: "statusLine")
    }
}

// MARK: - Entry point

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
