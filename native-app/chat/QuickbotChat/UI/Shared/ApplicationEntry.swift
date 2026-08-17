//
//  ApplicationEntry.swift
//  Quickbot Chat
//
//  Created by Augustinas Malinauskas on 12/02/2024.
//

import SwiftUI

struct ApplicationEntry: View {
    @AppStorage("colorScheme") private var colorScheme: AppColorScheme = .system
    @State private var languageModelStore = LanguageModelStore.shared
    @State private var conversationStore = ConversationStore.shared
    @State private var completionsStore = CompletionsStore.shared
    @State private var appStore = AppStore.shared
    
    var body: some View {
        VStack {
            switch appStore.appState {
            case .chat:
                Chat(languageModelStore: languageModelStore, conversationStore: conversationStore, appStore: appStore)
            case .voice:
                Voice(languageModelStore: languageModelStore, conversationStore: conversationStore, appStore: appStore)
            }
        }
#if os(macOS)
        .background(WindowAccessor { window in
            PanelManager.shared?.register(chatWindow: window)
        })
#endif
        .task {
            Task.detached {
                /// learn the endpoint and model from the Quickbot server
                /// component before anything talks to the API
                await QuickbotService.shared.autoConfigure()

                async let loadModels: () = languageModelStore.loadModels()
                async let loadConversations: () = conversationStore.loadConversations()
                async let loadCompletions: () = completionsStore.load()
                
                do {
                    _ = try await loadModels
                    _ = try await loadConversations
                    _ = try await loadCompletions
                } catch {
                    print("Unexpected error: \(error).")
                }
            }
        }
        .preferredColorScheme(colorScheme.toiOSFormat)
    }
}

#if os(macOS)
/// Hands the hosting NSWindow to a callback as soon as the view is attached
/// to it — before the window is ordered front, so it can be hidden with no
/// flicker (used for the silent start).
private struct WindowAccessor: NSViewRepresentable {
    var onWindow: @MainActor (NSWindow) -> Void

    func makeNSView(context: Context) -> WindowAccessorView {
        let view = WindowAccessorView()
        view.onWindow = onWindow
        return view
    }

    func updateNSView(_ nsView: WindowAccessorView, context: Context) {}

    final class WindowAccessorView: NSView {
        var onWindow: (@MainActor (NSWindow) -> Void)?

        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            if let window {
                MainActor.assumeIsolated {
                    onWindow?(window)
                }
            }
        }
    }
}
#endif

