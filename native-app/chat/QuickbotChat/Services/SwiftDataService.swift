//
//  SwiftDataService.swift
//  Quickbot Chat
//
//  Created by Augustinas Malinauskas on 10/12/2023.
//

import Foundation

/// Persistence service with the same API surface the app used with
/// SwiftData. SwiftData's @Model macro requires a compiler plugin that
/// ships only with Xcode, so this stores the object graph as JSON in
/// Application Support instead.
final actor SwiftDataService {
    static let shared = SwiftDataService()

    private var languageModels: [LanguageModelSD] = []
    private var conversations: [ConversationSD] = []
    private var completions: [CompletionInstructionSD] = []

    private let storeURL: URL

    init() {
        let appSupport = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first!
            .appendingPathComponent("Quickbot Chat", isDirectory: true)
        try? FileManager.default.createDirectory(at: appSupport, withIntermediateDirectories: true)
        storeURL = appSupport.appendingPathComponent("store.json")
        loadSync()
    }

    // MARK: - Persistence

    private struct MessageDTO: Codable {
        var id: UUID
        var content: String
        var role: String
        var done: Bool
        var error: Bool
        var createdAt: Date
        var image: Data?
    }

    private struct ConversationDTO: Codable {
        var id: UUID
        var name: String
        var createdAt: Date
        var updatedAt: Date
        var modelName: String?
        var messages: [MessageDTO]
    }

    private struct LanguageModelDTO: Codable {
        var name: String
        var imageSupport: Bool
        var modelProvider: ModelProvider?
    }

    private struct CompletionDTO: Codable {
        var id: UUID
        var name: String
        var keyboardCharacterStr: String
        var instruction: String
        var order: Int
        var modelTemperature: Float?
    }

    private struct StoreDTO: Codable {
        var models: [LanguageModelDTO]
        var conversations: [ConversationDTO]
        var completions: [CompletionDTO]
    }

    private nonisolated func loadStore(from url: URL) -> StoreDTO? {
        guard let data = try? Data(contentsOf: url) else { return nil }
        let decoder = JSONDecoder()
        decoder.dateDecodingStrategy = .iso8601
        return try? decoder.decode(StoreDTO.self, from: data)
    }

    private func loadSync() {
        guard let store = loadStore(from: storeURL) else { return }

        languageModels = store.models.map {
            LanguageModelSD(name: $0.name, imageSupport: $0.imageSupport, modelProvider: $0.modelProvider ?? .local)
        }

        conversations = store.conversations.map { dto in
            let conversation = ConversationSD(name: dto.name, updatedAt: dto.updatedAt)
            conversation.id = dto.id
            conversation.createdAt = dto.createdAt
            conversation.model = languageModels.first { $0.name == dto.modelName }
            for messageDTO in dto.messages.sorted(by: { $0.createdAt < $1.createdAt }) {
                let message = MessageSD(
                    content: messageDTO.content,
                    role: messageDTO.role,
                    done: messageDTO.done,
                    error: messageDTO.error,
                    image: messageDTO.image
                )
                message.id = messageDTO.id
                message.createdAt = messageDTO.createdAt
                message.conversation = conversation
            }
            return conversation
        }

        completions = store.completions.map { dto in
            let completion = CompletionInstructionSD(
                name: dto.name,
                keyboardCharacterStr: dto.keyboardCharacterStr,
                instruction: dto.instruction,
                order: dto.order,
                modelTemperature: dto.modelTemperature ?? 0.8
            )
            completion.id = dto.id
            return completion
        }
    }

    private func save() throws {
        let store = StoreDTO(
            models: languageModels.map {
                LanguageModelDTO(name: $0.name, imageSupport: $0.imageSupport, modelProvider: $0.modelProvider)
            },
            conversations: conversations.map { conversation in
                ConversationDTO(
                    id: conversation.id,
                    name: conversation.name,
                    createdAt: conversation.createdAt,
                    updatedAt: conversation.updatedAt,
                    modelName: conversation.model?.name,
                    messages: conversation.messages.map {
                        MessageDTO(
                            id: $0.id,
                            content: $0.content,
                            role: $0.role,
                            done: $0.done,
                            error: $0.error,
                            createdAt: $0.createdAt,
                            image: $0.image
                        )
                    }
                )
            },
            completions: completions.map {
                CompletionDTO(
                    id: $0.id,
                    name: $0.name,
                    keyboardCharacterStr: $0.keyboardCharacterStr,
                    instruction: $0.instruction,
                    order: $0.order,
                    modelTemperature: $0.modelTemperature
                )
            }
        )

        let encoder = JSONEncoder()
        encoder.dateEncodingStrategy = .iso8601
        let data = try encoder.encode(store)
        try data.write(to: storeURL, options: .atomic)
    }
}

// MARK: - Language Models
extension SwiftDataService {
    func fetchModels() throws -> [LanguageModelSD] {
        return languageModels.sorted { $0.name < $1.name }
    }

    func saveModels(models: [LanguageModelSD]) throws {
        for model in models {
            if let existing = languageModels.first(where: { $0.name == model.name }) {
                existing.imageSupport = model.imageSupport
                existing.modelProvider = model.modelProvider
            } else {
                languageModels.append(model)
            }
        }
        try save()
    }

    func deleteModels() throws {
        languageModels.removeAll()
        try save()
    }
}

// MARK: - Conversations
extension SwiftDataService {
    func createConversation(_ conversation: ConversationSD) throws {
        if !conversations.contains(conversation) {
            conversations.append(conversation)
        }
        try save()
    }

    func renameConversation(_ conversation: ConversationSD) throws {
        try save()
    }

    func deleteConversation(_ conversation: ConversationSD) throws {
        conversations.removeAll { $0 == conversation }
        try save()
    }

    func updateConversation(_ conversation: ConversationSD) throws {
        conversation.updatedAt = .now
        if !conversations.contains(conversation) {
            conversations.append(conversation)
        }
        try save()
    }

    func fetchConversations() throws -> [ConversationSD] {
        return conversations.sorted { $0.updatedAt > $1.updatedAt }
    }

    func getConversation(_ conversationId: UUID) throws -> ConversationSD? {
        return conversations.first { $0.id == conversationId }
    }

    func deleteConversations() throws {
        conversations.removeAll()
        try save()
    }

    func deleteMessages() throws {
        for conversation in conversations {
            conversation.messages.removeAll()
        }
        try save()
    }

    func deleteConversations(_ date: Date) throws {
        let calendar = Calendar.current
        conversations.removeAll { calendar.isDate($0.createdAt, inSameDayAs: date) }
        try save()
    }
}


// MARK: - Messages
extension SwiftDataService {
    func fetchMessages(_ conversationId: UUID) throws -> [MessageSD] {
        let conversation = conversations.first { $0.id == conversationId }
        return (conversation?.messages ?? []).sorted { $0.createdAt < $1.createdAt }
    }

    func updateMessage(_ message: MessageSD) throws {
        try save()
    }

    func createMessage(_ mesasge: MessageSD) throws {
        // the message links itself into its conversation via the
        // hand-maintained inverse relationship
        try save()
    }
}

// MARK: - CompletionInstruction
extension SwiftDataService {
    func fetchCompletionInstructions() throws -> [CompletionInstructionSD] {
        return completions.sorted { $0.order < $1.order }
    }

    func updateCompletionInstructions(_ instructions: [CompletionInstructionSD]) throws {
        for index in instructions.indices {
            instructions[index].order = index
            if !completions.contains(instructions[index]) {
                completions.append(instructions[index])
            }
        }
        try save()
    }

    func deleteCompletionInstruction(_ instruction: CompletionInstructionSD) throws {
        completions.removeAll { $0 == instruction }
        try save()
    }
}

// MARK: - General
extension SwiftDataService {
    func deleteEverything() throws {
        conversations.removeAll()
        languageModels.removeAll()
        completions.removeAll()
        try save()
    }
}
