//
//  PanelManager.swift
//  Quickbot Chat
//
//  Created by Augustinas Malinauskas on 12/02/2024.
//

#if os(macOS)
import SwiftUI
import Carbon
import AsyncAlgorithms

final actor Printer {
    func write(_ message: String) {
        Clipboard.shared.setString(message)
        usleep(50000)
        Accessibility.simulatePasteCommand()
    }
}

class PanelManager: NSObject, NSApplicationDelegate {
    static var shared: PanelManager?

    var targetApplication: NSRunningApplication?
    var lastPrintApplication: NSRunningApplication?
    var panel: FloatingPanel!
    var completionsPanelVM = CompletionsPanelVM()
    @MainActor var allowPrinting = true
    let printer = Printer()

    /// The app starts silently: no Dock icon (LSUIElement) and the main
    /// chat window hidden. The window is shown only on explicit request —
    /// the "Show chat window" menu item in the Quickbot menu bar app, a
    /// reopen, or submitting a prompt from the invocation panel.
    static let showWindowNotification = Notification.Name("com.quickbot.chat.showWindow")
    static let hideWindowNotification = Notification.Name("com.quickbot.chat.hideWindow")
    @MainActor private(set) weak var chatWindow: NSWindow?
    @MainActor private var pendingShow = CommandLine.arguments.contains("--show-window")
    @MainActor private var didHideInitialWindow = false
    @MainActor private var closeInterceptor: WindowCloseInterceptor?

    override init() {
        super.init()
        PanelManager.shared = self

        DistributedNotificationCenter.default().addObserver(
            forName: Self.showWindowNotification, object: nil, queue: .main
        ) { [weak self] _ in
            Task { @MainActor in self?.presentChatWindow() }
        }

        DistributedNotificationCenter.default().addObserver(
            forName: Self.hideWindowNotification, object: nil, queue: .main
        ) { [weak self] _ in
            Task { @MainActor in self?.chatWindow?.performClose(nil) }
        }

        NotificationCenter.default.addObserver(
            forName: NSWindow.didBecomeKeyNotification, object: nil, queue: .main
        ) { [weak self] notification in
            Task { @MainActor in
                guard let self, let window = notification.object as? NSWindow,
                      window === self.chatWindow else { return }
                // SwiftUI may replace the window delegate during setup, so
                // (re)install the close interceptor once the window is up.
                self.installCloseInterceptor(on: window)
            }
        }

        NotificationCenter.default.addObserver(
            forName: NSWindow.willCloseNotification, object: nil, queue: .main
        ) { [weak self] notification in
            Task { @MainActor in
                guard let self, let window = notification.object as? NSWindow,
                      window === self.chatWindow else { return }
                // Back to silent background mode
                NSApp.setActivationPolicy(.accessory)
            }
        }

        Task {
            await handleNewMessages()
        }
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }

    /// Called by WindowAccessor as soon as the main window exists,
    /// before it is ordered front — so hiding it at launch has no flicker.
    @MainActor func register(chatWindow window: NSWindow) {
        chatWindow = window
        if !didHideInitialWindow && !pendingShow {
            didHideInitialWindow = true
            // SwiftUI orders the window front *after* this callback, so an
            // immediate orderOut would be undone. Make it invisible now
            // (no frame gets painted) and take it off screen next tick.
            window.alphaValue = 0
            DispatchQueue.main.async {
                window.orderOut(nil)
                window.alphaValue = 1
            }
            return
        }
        if pendingShow {
            pendingShow = false
            didHideInitialWindow = true
            presentChatWindow()
        }
    }

    /// Closing the chat window would let SwiftUI destroy it, and there is no
    /// reliable way to make a WindowGroup recreate a window from AppKit. So
    /// the close button and ⌘W are converted into "hide": the window lives
    /// for the whole app lifetime and can always be shown again.
    @MainActor private func installCloseInterceptor(on window: NSWindow) {
        if let interceptor = closeInterceptor, window.delegate === interceptor { return }
        let interceptor = WindowCloseInterceptor(wrapping: window.delegate, panelManager: self)
        closeInterceptor = interceptor
        window.delegate = interceptor
    }

    /// The chat window shows like the invocation panel does: the app stays
    /// an accessory the whole time, so no Dock icon ever appears.
    @MainActor func presentChatWindow() {
        if let window = chatWindow {
            if window.isMiniaturized { window.deminiaturize(nil) }
            window.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
        } else {
            // The window was closed and released; a reopen makes SwiftUI
            // recreate it, and register(chatWindow:) presents it.
            pendingShow = true
            let configuration = NSWorkspace.OpenConfiguration()
            configuration.activates = true
            NSWorkspace.shared.openApplication(at: Bundle.main.bundleURL, configuration: configuration)
        }
    }

    @MainActor func hideChatWindow() {
        chatWindow?.orderOut(nil)
        NSApp.setActivationPolicy(.accessory)
    }

    /// ⌘⇧; behaves like the panel's ⌘;: shows the chat window when hidden,
    /// hides it when visible.
    @MainActor func toggleChatWindow() {
        if let window = chatWindow, window.isVisible {
            hideChatWindow()
        } else {
            presentChatWindow()
        }
    }
    
    private func handleNewMessages() async {
        let timer = AsyncTimerSequence(interval: .seconds(0.1), clock: .continuous)
        for await _ in timer {
            // If user focused different application stop writing
            if lastPrintApplication != nil && lastPrintApplication?.localizedName != NSWorkspace.shared.runningApplications.first(where: {$0.isActive})?.localizedName {
                // dequeue all and stop execution
                await completionsPanelVM.cancel()
                _ = await completionsPanelVM.sentenceQueue.dequeueAll()
                lastPrintApplication = nil
                continue
            }
            
            // hold printing until user action and ensuring that your driving experience
            if await !allowPrinting {
                continue
            }
            
            let sentencesToConsume = await completionsPanelVM.sentenceQueue.dequeueAll().joined()
            
            if sentencesToConsume.isEmpty {
                continue
            }
            
            print("printing: \((sentencesToConsume)) \(Date())")
            lastPrintApplication = NSWorkspace.shared.runningApplications.first{$0.isActive}
            await printer.write(sentencesToConsume)
        }
    }
    
    
    @MainActor
    @objc func togglePanel() {
        // No Accessibility check here: the plain prompt panel works without
        // it. Completions (capturing selected text / typing back) are the
        // only features that need it, and the completions editor has its own
        // "Open Privacy Settings" button.
        targetApplication = NSWorkspace.shared.runningApplications.first{$0.isActive}

        Task {
            completionsPanelVM.selectedText = Accessibility.shared.getSelectedText()
            print("selected message", completionsPanelVM.selectedText as Any)
            
            if panel == nil || !panel.isVisible {
                showPanel()
                
                // subscribe to keybaord event to avoid beep
//                HotkeyService.shared.registerSingleUseEscape(modifiers: []) { [weak self] in
//                    self?.hidePanel()
//                }
                
                return
            }
            
            hidePanel()
        }
    }
    
    @MainActor
    @objc func hidePanel() {
        panel.orderOut(nil)
    }
    
    @MainActor
    @objc func showPanel() {
        createPanel()
        panel.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }
    
    @MainActor
    @objc func onSubmitMessage() {
        allowPrinting = true
        hidePanel()

        /// Submitting from the panel is an explicit request to see the chat
        presentChatWindow()
    }
    
    @MainActor
    @objc func onSubmitCompletion(scheduledTyping: Bool) {
        allowPrinting = true
        
        if scheduledTyping {
            self.allowPrinting = false
            HotkeyService.shared.registerSingleUseSpace(modifiers: []) { [weak self] in
                self?.allowPrinting = true
                self?.hidePanel()
            }
        } else {
            hidePanel()
        }
        targetApplication?.activate()
    }
    
    @MainActor
    func createPanel() {
        let contentView = PromptPanel(
            completionsPanelVM: completionsPanelVM,
            onSubmitPanel: onSubmitMessage,
            onSubmitCompletion: onSubmitCompletion,
            onLayoutUpdate: updatePanelSizeIfNeeded
        )
        let hostingView = NSHostingView(rootView: contentView)
        let idealSize = hostingView.fittingSize
        
        panel = FloatingPanel(
            contentRect: NSRect(x: 0, y: 0, width: idealSize.width, height: idealSize.height),
            backing: .buffered,
            defer: false
        )
        panel.contentView = hostingView
        panel.backgroundColor = .clear
        panel.center()
        panel.orderFront(nil)
    }
    
    @MainActor func updatePanelSizeIfNeeded() {
        guard let hostingView = panel.contentView as? NSHostingView<PromptPanel> else { return }
        
        DispatchQueue.main.async { [weak self] in
            guard let strongSelf = self else { return }
            let newSize = hostingView.fittingSize
            
            if newSize == .zero {
                return
            }
            
            if strongSelf.panel.frame.size != newSize {
                NSAnimationContext.runAnimationGroup({ context in
                    context.duration = 0.2
                    context.timingFunction = CAMediaTimingFunction(name: .easeOut)
                    
                    // Calculate the difference in height
                    let heightDifference = newSize.height - strongSelf.panel.frame.size.height
                    
                    // Adjust the y position to keep the bottom edge constant
                    let newY = strongSelf.panel.frame.origin.y - heightDifference
                    
                    strongSelf.panel.animator().setFrame(
                        NSRect(x: strongSelf.panel.frame.origin.x,
                               y: newY, // Use the new Y
                               width: newSize.width,
                               height: newSize.height),
                        display: true)
                }, completionHandler: {
                    print("Animation completed")
                })
            }
        }
    }
}

extension PanelManager {
    @MainActor func windowDidResignKey(_ notification: Notification) {
        if let panel = notification.object as? FloatingPanel, panel == self.panel {
            panel.close()
        }
    }
}

/// NSWindowDelegate proxy: answers `windowShouldClose` itself (hide instead
/// of close) and forwards every other delegate callback to SwiftUI's own
/// window delegate so its behaviors keep working.
final class WindowCloseInterceptor: NSObject, NSWindowDelegate {
    private weak var original: NSWindowDelegate?
    private weak var panelManager: PanelManager?

    init(wrapping original: NSWindowDelegate?, panelManager: PanelManager) {
        self.original = original
        self.panelManager = panelManager
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        MainActor.assumeIsolated {
            panelManager?.hideChatWindow()
        }
        return false
    }

    override func responds(to aSelector: Selector!) -> Bool {
        super.responds(to: aSelector) || (original?.responds(to: aSelector) ?? false)
    }

    override func forwardingTarget(for aSelector: Selector!) -> Any? {
        if original?.responds(to: aSelector) == true {
            return original
        }
        return super.forwardingTarget(for: aSelector)
    }
}
#endif
