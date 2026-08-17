//
//  MessageSD.swift
//  Quickbot Chat
//
//  Created by Augustinas Malinauskas on 10/12/2023.
//

import Foundation
import Observation

@Observable
final class MessageSD: Identifiable {
    var id: UUID = UUID()

    /// The content may hold several <think>…</think> blocks (one per web
    /// search round), the last possibly still open while streaming.
    private var thinkSplit: (think: String, rest: String) {
        var think: [String] = []
        var rest = ""
        var remaining = Substring(content)
        while let open = remaining.range(of: "<think>") {
            rest += remaining[..<open.lowerBound]
            let inner = remaining[open.upperBound...]
            if let close = inner.range(of: "</think>") {
                think.append(String(inner[..<close.lowerBound]))
                remaining = inner[close.upperBound...]
            } else {
                think.append(String(inner))
                remaining = Substring("")
            }
        }
        rest += remaining
        return (think.joined(separator: "\n\n"), rest.trimmingCharacters(in: .whitespacesAndNewlines))
    }
    var think: String? {
        hasThink ? thinkSplit.think : nil
    }
    var hasThink: Bool {
        content.contains("<think>")
    }
    var thinkComplete: Bool {
        guard let lastOpen = content.range(of: "<think>", options: .backwards) else { return false }
        return content[lastOpen.upperBound...].contains("</think>")
    }
    var content: String
    var realContent: String? {
        guard hasThink else { return content }
        let rest = thinkSplit.rest
        return rest.isEmpty ? nil : rest
    }

    /// Generation metrics, stamped when the response completes.
    var generationDuration: Double?
    var promptTokens: Int?
    var completionTokens: Int?
    var tokensPerSecond: Double?

    /// "0:34 · 185 tokens · 13.5 tok/s" — nil when no metrics were recorded.
    var statsLine: String? {
        guard let duration = generationDuration else { return nil }
        var parts = [String(format: "%d:%02d", Int(duration) / 60, Int(duration) % 60)]
        if let tokens = completionTokens, tokens > 0 {
            parts.append("\(tokens) tokens")
        }
        if let rate = tokensPerSecond, rate > 0 {
            parts.append(String(format: "%.1f tok/s", rate))
        }
        return parts.joined(separator: " · ")
    }
    var role: String
    var done: Bool = false
    var error: Bool = false
    var createdAt: Date = Date.now
    var image: Data?

    /// Inverse relationship maintained by hand (SwiftData used to do it).
    var conversation: ConversationSD? {
        didSet {
            if oldValue !== conversation {
                oldValue?.messages.removeAll { $0 === self }
            }
            if let conversation, !conversation.messages.contains(where: { $0 === self }) {
                conversation.messages.append(self)
            }
        }
    }

    init(content: String, role: String, done: Bool = false, error: Bool = false, image: Data? = nil) {
        self.content = content
        self.role = role
        self.done = done
        self.error = error
        self.image = image
    }

    var model: String {
        conversation?.model?.name ?? ""
    }
}

extension MessageSD {
    static let sample: [MessageSD] = [
        .init(content: "How many quarks there are in SM?", role: "user"),
        .init(content: "There are 6 quarks in SM, each of them has an antiparticle and colour.", role: "assistant"),
        .init(content: "How elementary particle is defined in mathematics?", role: "user"),
        .init(content: "Elementary particle is defined as an irreducible representation of the poincase group.", role: "assistant")
    ]
}

// MARK: - Equatable / Hashable
extension MessageSD: Equatable, Hashable {
    static func == (lhs: MessageSD, rhs: MessageSD) -> Bool {
        lhs.id == rhs.id
    }

    func hash(into hasher: inout Hasher) {
        hasher.combine(id)
    }
}

// MARK: - @unchecked Sendable
extension MessageSD: @unchecked Sendable {
    /// We hide compiler warnings for concurency. We have to make sure to modify the data only via SwiftDataManager to ensure concurrent operations.
}
