//
//  QuickbotChatApp.swift
//  Quickbot Chat
//

import SwiftUI

#if os(macOS)
import KeyboardShortcuts
extension KeyboardShortcuts.Name {
    /// Invocation mode: summons the floating prompt panel from anywhere.
    static let togglePanelMode = Self("togglePanelMode", default: .init(.semicolon, modifiers: [.command]))
    /// Opens the main chat window from anywhere.
    static let showChatWindow = Self("showChatWindow", default: .init(.semicolon, modifiers: [.command, .shift]))
}
#endif

@main
struct QuickbotChatApp: App {
    @State private var appStore = AppStore.shared
#if os(macOS)
    @NSApplicationDelegateAdaptor(PanelManager.self) var panelManager
#endif

    var body: some Scene {
        WindowGroup {
            ApplicationEntry()
#if os(macOS)
                .onKeyboardShortcut(KeyboardShortcuts.Name.togglePanelMode, type: .keyDown) {
                    panelManager.togglePanel()
                }
                .onKeyboardShortcut(KeyboardShortcuts.Name.showChatWindow, type: .keyDown) {
                    panelManager.toggleChatWindow()
                }
                .onAppear {
                    NSWindow.allowsAutomaticWindowTabbing = false
                }
#endif
        }
#if os(macOS)
        .commands {
            Menus()
        }
#endif
#if os(macOS)
        Window("Keyboard Shortcuts", id: "keyboard-shortcuts") {
            KeyboardShortcutsDemo()
        }
#endif
    }
}
