//
//  ConversationSD.swift
//  Quickbot Chat
//
//  Created by Augustinas Malinauskas on 10/12/2023.
//

import Foundation
import Observation

/// Plain observable model (SwiftData's @Model requires a compiler plugin
/// that ships only with Xcode; persistence lives in SwiftDataService).
@Observable
final class ConversationSD: Identifiable {
    var id: UUID = UUID()

    var name: String
    var createdAt: Date
    var updatedAt: Date

    var model: LanguageModelSD?

    var messages: [MessageSD] = []

    init(name: String, updatedAt: Date = Date.now) {
        self.name = name
        self.updatedAt = updatedAt
        self.createdAt = updatedAt
    }
}

// MARK: - Sample data
extension ConversationSD {
    static let sample = [
        ConversationSD(name: "New Chat", updatedAt: Date.now),
        ConversationSD(name: "Presidential", updatedAt: Calendar.current.date(byAdding: .day, value: -1, to: Date.now)!),
        ConversationSD(name: "What is QFT?", updatedAt: Calendar.current.date(byAdding: .day, value: -2, to: Date.now)!)
    ]
}

// MARK: - Equatable / Hashable
extension ConversationSD: Equatable, Hashable {
    static func == (lhs: ConversationSD, rhs: ConversationSD) -> Bool {
        lhs.id == rhs.id
    }

    func hash(into hasher: inout Hasher) {
        hasher.combine(id)
    }
}

// MARK: - @unchecked Sendable
extension ConversationSD: @unchecked Sendable {
    /// We hide compiler warnings for concurency. We have to make sure to modify the data only via SwiftDataManager to ensure concurrent operations.
}
