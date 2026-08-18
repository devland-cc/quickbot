//
//  AppStore.swift
//  Quickbot Chat
//
//  Created by Augustinas Malinauskas on 11/12/2023.
//

import Foundation
import Combine
import SwiftUI

enum AppState {
    case chat
    case voice
}

@Observable
final class AppStore {
    static let shared = AppStore()
    
    private var cancellables = Set<AnyCancellable>()
    private var timer: Timer?
    private var pingInterval: TimeInterval = 5
    @MainActor var isReachable: Bool = true
    @MainActor var notifications: [NotificationMessage] = []
    @MainActor var menuBarIcon: String? = nil
    var appState: AppState = .chat

    init() {
        if let storedIntervalString = UserDefaults.standard.string(forKey: "pingInterval") {
            pingInterval = Double(storedIntervalString) ?? 5
            
            if pingInterval <= 0 {
                pingInterval = .infinity
            }
        }
        startCheckingReachability(interval: pingInterval)
    }
    
    deinit {
        stopCheckingReachability()
    }
    
    private func startCheckingReachability(interval: TimeInterval = 5) {
        timer = Timer.scheduledTimer(withTimeInterval: interval, repeats: true) { _ in
            Task { [weak self] in
                let status = await self?.reachable() ?? false
                self?.updateReachable(status)
                await self?.refreshModelsIfNeeded(nowReachable: status)
            }
        }
    }

    /// The model list is fetched once at launch. If the server was still
    /// loading its model at that moment (it takes ~40s after a toggle or
    /// login), the fetch fails and the app is left with no models — and no
    /// way to send anything. Reload the list whenever the server comes back
    /// from unreachable, and whenever it is up but the list is empty.
    private var wasReachable = true

    private func refreshModelsIfNeeded(nowReachable: Bool) async {
        let cameBack = nowReachable && !wasReachable
        wasReachable = nowReachable
        guard nowReachable else { return }
        let modelsEmpty = await MainActor.run { LanguageModelStore.shared.models.isEmpty }
        if cameBack || modelsEmpty {
            try? await LanguageModelStore.shared.loadModels()
        }
    }
    
    private func updateReachable(_ isReachable: Bool) {
        DispatchQueue.main.async {
            withAnimation {
                self.isReachable = isReachable
            }
        }
    }

    private func stopCheckingReachability() {
        timer?.invalidate()
        timer = nil
    }

    private func reachable() async -> Bool {
        let status = await QuickbotService.shared.reachable()
        if !status {
            // The server may have been (re)started or reconfigured since the
            // last check; ask the server component for its current endpoint.
            if await QuickbotService.shared.autoConfigure() {
                return await QuickbotService.shared.reachable()
            }
        }
        return status
    }
    
    @MainActor func uiLog(message: String, status: NotificationMessage.Status) {
        notifications = [NotificationMessage(message: message, status: status)] + notifications.suffix(5)
    }
}
