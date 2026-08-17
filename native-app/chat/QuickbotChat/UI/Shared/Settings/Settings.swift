//
//  Settings.swift
//  Quickbot Chat
//
//  Created by Augustinas Malinauskas on 28/12/2023.
//

import SwiftUI
import Combine

struct Settings: View {
    var languageModelStore = LanguageModelStore.shared
    var conversationStore = ConversationStore.shared
    var swiftDataService = SwiftDataService.shared
    
    @AppStorage("serverEndpoint") private var serverEndpoint: String = ""
    @AppStorage("webSearch") private var webSearch: Bool = true
    @AppStorage("modelThinking") private var modelThinking: Bool = false
    @AppStorage("systemPrompt") private var systemPrompt: String = ""
    @AppStorage("vibrations") private var vibrations: Bool = true
    @AppStorage("colorScheme") private var colorScheme = AppColorScheme.system
    @AppStorage("defaultModel") private var defaultModel: String = ""
    @AppStorage("appUserInitials") private var appUserInitials: String = ""
    @AppStorage("pingInterval") private var pingInterval: String = "5"
    @AppStorage("voiceIdentifier") private var voiceIdentifier: String = ""
    
    @StateObject private var speechSynthesiser = SpeechSynthesizer.shared
    
    @Environment(\.presentationMode) var presentationMode
    
    private let timer = Timer.publish(every: 5, on: .main, in: .common).autoconnect()
    @State private var cancellable: AnyCancellable?
    
    private func save() {
        // remove trailing slash
        if serverEndpoint.last == "/" {
            serverEndpoint = String(serverEndpoint.dropLast())
        }

        QuickbotService.shared.initEndpoint(url: serverEndpoint)
        Task {
            Haptics.shared.mediumTap()
            try? await languageModelStore.loadModels()
        }
        presentationMode.wrappedValue.dismiss()
    }

    private func checkServer() {
        Task {
            /// re-read endpoint and model from the Quickbot server component
            await QuickbotService.shared.autoConfigure()
            QuickbotService.shared.initEndpoint(url: serverEndpoint)
            serverStatus = await QuickbotService.shared.reachable()
            try? await languageModelStore.loadModels()
        }
    }
    
    private func deleteAll() {
        Task {
            try? await conversationStore.deleteAllConversations()
            try? await languageModelStore.deleteAllModels()
        }
    }
    
    @State var serverStatus: Bool?
    var body: some View {
        SettingsView(
            serverEndpoint: $serverEndpoint,
            webSearch: $webSearch,
            modelThinking: $modelThinking,
            systemPrompt: $systemPrompt,
            vibrations: $vibrations,
            colorScheme: $colorScheme,
            defaultModel: $defaultModel,
            appUserInitials: $appUserInitials,
            pingInterval: $pingInterval,
            voiceIdentifier: $voiceIdentifier,
            save: save,
            checkServer: checkServer,
            deleteAll: deleteAll,
            languageModels: languageModelStore.models,
            voices: speechSynthesiser.voices
        )
        .frame(maxWidth: 700)
        #if os(visionOS)
        .frame(minWidth: 600, minHeight: 800)
        #endif
        .onChange(of: defaultModel) { _, modelName in
            languageModelStore.setModel(modelName: modelName)
        }
        .onAppear {
            /// refresh voices in the background
            cancellable = timer.sink { _ in
                speechSynthesiser.fetchVoices()
            }
        }
        .onDisappear {
            cancellable?.cancel()
        }
    }
}

