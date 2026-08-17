//
//  PromptPanelVM.swift
//  Quickbot Chat
//
//  Created by Augustinas Malinauskas on 29/02/2024.
//

import SwiftUI
import Combine

@Observable
final class CompletionsPanelVM {
    var selectedText: String?
    var onReceiveText: (String) -> ()
    var messageResponse: String = ""
    var isReady = false
    let sentenceQueue = AsyncQueue<String>()
    private var generation: AnyCancellable?
    private var currentMessageBuffer: String = ""

    
    init(onReceiveText: @escaping (String) -> Void = {_ in}) {
        self.onReceiveText = onReceiveText
    }
    
    static func constructPrompt(completion: CompletionInstructionSD, selectedText: String) -> String {
        var prompt = completion.instruction
        
        if prompt.contains("{{text}}") {
            prompt.replace("{{text}}", with: selectedText)
        } else {
            prompt += " " + selectedText
        }
        
        return prompt
    }
    
    @MainActor
    func sendPrompt(completion: CompletionInstructionSD, model: LanguageModelSD)  {
        guard let selectedText = selectedText, !isReady else { return }
        let prompt = CompletionsPanelVM.constructPrompt(completion: completion, selectedText: selectedText)
        
        let messages: [ChatMessage] = [
            .init(role: .user, content: prompt)
        ]
        let temperature = Double(completion.modelTemperature ?? 0.8)
        currentMessageBuffer = ""
        messageResponse = ""

        Task {
            if await QuickbotService.shared.reachable() {
                generation = QuickbotService.shared.chat(model: model.name, messages: messages, temperature: temperature)
                    .sink(receiveCompletion: { [weak self] completion in
                        switch completion {
                        case .finished:
                            self?.handleComplete()
                        case .failure(let error):
                            self?.handleError(error.localizedDescription)
                        }
                    }, receiveValue: { [weak self] delta in
                        Task { @MainActor in
                            self?.handleReceive(delta)
                        }
                    })
            } else {
                self.handleError("Server unreachable")
            }
        }
    }

    @MainActor
    private func handleReceive(_ delta: String)  {
        Task {
            await sentenceQueue.enqueue(delta)
            self.messageResponse = self.messageResponse + delta
        }
    }
    
    @MainActor
    private func handleError(_ errorMessage: String) {
        print("error \(errorMessage)")
    }
    
    @MainActor
    private func handleComplete() {
        print("model response ", self.messageResponse)
    }
    
    @MainActor
    func cancel() {
        generation?.cancel()
    }
}
