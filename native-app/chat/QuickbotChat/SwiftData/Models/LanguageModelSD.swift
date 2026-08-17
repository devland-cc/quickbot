//
//  ModelSD.swift
//  Quickbot Chat
//
//  Created by Augustinas Malinauskas on 10/12/2023.
//

import Foundation
import Observation

@Observable
final class LanguageModelSD: Identifiable {
    var name: String
    var isAvailable: Bool = false
    var imageSupport: Bool = false
    var modelProvider: ModelProvider? = ModelProvider.local

    var id: String { name }

    init(name: String, imageSupport: Bool = false, modelProvider: ModelProvider) {
        self.name = name
        self.imageSupport = imageSupport
        self.modelProvider = modelProvider
    }

    var isNotAvailable: Bool {
        isAvailable == false
    }
}

// MARK: - Helpers
extension LanguageModelSD {
    var prettyName: String {
        guard let modelName = name.components(separatedBy: ":").first else {
            return name
        }

        return modelName.capitalized
    }

    var prettyVersion: String {
        let components = name.components(separatedBy: ":")
        if components.count >= 2 {
            return components[1]
        }
        return ""
    }

    var supportsImages: Bool {
        if imageSupport {
            return true
        }

        /// older technique to detect image modality
        /// @deprecated
        let imageSupportedModels = ["llava"]
        for modelName in imageSupportedModels {
            if name.contains(modelName) {
                return true
            }
        }
        return false
    }

    static let sample: [LanguageModelSD] = [
        .init(name: "Llama:latest", modelProvider: .local),
        .init(name: "Mistral:latest", modelProvider: .local)
    ]
}

// MARK: - Equatable / Hashable
extension LanguageModelSD: Equatable, Hashable {
    static func == (lhs: LanguageModelSD, rhs: LanguageModelSD) -> Bool {
        lhs.name == rhs.name
    }

    func hash(into hasher: inout Hasher) {
        hasher.combine(name)
    }
}

// MARK: - @unchecked Sendable
extension LanguageModelSD: @unchecked Sendable {
    /// We hide compiler warnings for concurency. We have to make sure to modify the data only via SwiftDataManager to ensure concurrent operations.
}
