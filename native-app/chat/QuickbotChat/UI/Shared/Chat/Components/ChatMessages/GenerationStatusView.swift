//
//  GenerationStatusView.swift
//  Quickbot Chat
//
//  Ephemeral status line under the message being generated: current stage
//  (proxy search steps, thinking, writing), elapsed time and a live token
//  rate estimate. Disappears when the response completes.
//

import SwiftUI

struct GenerationStatusView: View {
    var message: MessageSD
    private var store = ConversationStore.shared

    init(message: MessageSD) {
        self.message = message
    }

    private var stage: String {
        if let status = store.liveStatus {
            return status
        }
        if message.content.isEmpty {
            return "Waiting for the model…"
        }
        if message.hasThink && !message.thinkComplete {
            return "Thinking…"
        }
        return "Writing…"
    }

    var body: some View {
        TimelineView(.periodic(from: .now, by: 1)) { _ in
            HStack(spacing: 4) {
                Text(line)
                    .font(.footnote)
                    .foregroundStyle(.secondary)
                    .contentTransition(.numericText())
            }
        }
        .padding(.top, 2)
    }

    private var line: String {
        var parts = [stage]
        if let startedAt = store.generationStartedAt {
            let elapsed = Int(-startedAt.timeIntervalSinceNow)
            parts.append(String(format: "%d:%02d", elapsed / 60, elapsed % 60))
        }
        if let rate = store.liveTokensPerSecond {
            parts.append(String(format: "~%.0f tok/s", rate))
        }
        return parts.joined(separator: " · ")
    }
}
