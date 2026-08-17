//
//  QuickbotService.swift
//  Quickbot Chat
//

import Foundation
import Combine

/// A chat message in the OpenAI-compatible wire format.
struct ChatMessage {
    enum Role: String {
        case system, user, assistant
    }

    var role: Role
    var content: String
    /// Base64-encoded images attached to this message (no data: prefix).
    var images: [String] = []
}

/// Client for the Quickbot server's OpenAI-compatible API, plus
/// auto-configuration from the server component's `serverctl`.
final class QuickbotService: @unchecked Sendable {
    static let shared = QuickbotService()

    static let fallbackEndpoint = "http://127.0.0.1:8080/v1"

    /// Base URL including the `/v1` prefix.
    private(set) var baseURL: URL

    /// Maps the friendly model name shown in the UI to the id the API
    /// expects (mlx serves models under their full filesystem path).
    private var modelIdsByName: [String: String] = [:]

    init() {
        baseURL = URL(string: Self.fallbackEndpoint)!
        initEndpoint()
    }

    // MARK: - Endpoint configuration

    func initEndpoint(url: String? = nil) {
        let stored = UserDefaults.standard.string(forKey: "serverEndpoint")
        if var endpoint = [url, stored, Self.fallbackEndpoint]
            .compactMap({ $0 })
            .filter({ !$0.isEmpty })
            .first {
            if !endpoint.contains("http") {
                endpoint = "http://" + endpoint
            }
            if endpoint.last == "/" {
                endpoint = String(endpoint.dropLast())
            }
            if let url = URL(string: endpoint) {
                baseURL = url
            }
        }
    }

    /// Directory of the Quickbot server component (owns `serverctl`).
    static var serverDirectory: URL {
        if let override = ProcessInfo.processInfo.environment["QUICKBOT_SERVER_DIR"] {
            return URL(fileURLWithPath: (override as NSString).expandingTildeInPath)
        }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Devland/_experimental/quickbot/server")
    }

    private struct ServerStatus: Decodable {
        let state: String
        let modelName: String?
        let endpoint: String?
    }

    /// Asks the server component for its endpoint and model via
    /// `serverctl status --json`, then updates the endpoint and the default
    /// model so the app configures itself. Returns true on success.
    @discardableResult
    func autoConfigure() async -> Bool {
        let serverctl = Self.serverDirectory.appendingPathComponent("serverctl")
        guard FileManager.default.isExecutableFile(atPath: serverctl.path) else { return false }

        let output: Data? = await withCheckedContinuation { continuation in
            DispatchQueue.global(qos: .utility).async {
                let process = Process()
                process.executableURL = serverctl
                process.arguments = ["status", "--json"]
                let pipe = Pipe()
                process.standardOutput = pipe
                process.standardError = FileHandle.nullDevice
                do {
                    try process.run()
                } catch {
                    continuation.resume(returning: nil)
                    return
                }
                let data = pipe.fileHandleForReading.readDataToEndOfFile()
                process.waitUntilExit()
                continuation.resume(returning: process.terminationStatus == 0 ? data : nil)
            }
        }

        guard let output,
              let status = try? JSONDecoder().decode(ServerStatus.self, from: output) else {
            return false
        }

        if let endpoint = status.endpoint {
            UserDefaults.standard.set(endpoint, forKey: "serverEndpoint")
            initEndpoint(url: endpoint)
        }
        if let modelName = status.modelName, !modelName.isEmpty {
            UserDefaults.standard.set(modelName, forKey: "defaultModel")
        }
        return true
    }

    // MARK: - Models

    private struct ModelsResponse: Decodable {
        struct Model: Decodable { let id: String }
        let data: [Model]
    }

    func getModels() async throws -> [LanguageModel] {
        var request = URLRequest(url: baseURL.appendingPathComponent("models"))
        request.timeoutInterval = 5
        let (data, _) = try await URLSession.shared.data(for: request)
        let response = try JSONDecoder().decode(ModelsResponse.self, from: data)

        var idsByName: [String: String] = [:]
        let models = response.data.map { model in
            let name = Self.friendlyName(forModelId: model.id)
            idsByName[name] = model.id
            // mlx_vlm serves vision models; if the loaded model is text-only
            // the server just rejects image content.
            return LanguageModel(name: name, provider: .local, imageSupport: true)
        }
        modelIdsByName = idsByName
        return models
    }

    static func friendlyName(forModelId id: String) -> String {
        guard id.contains("/") else { return id }
        return URL(fileURLWithPath: id).lastPathComponent
    }

    // MARK: - Health

    func reachable() async -> Bool {
        // The server exposes /health next to /v1.
        let healthURL = baseURL.deletingLastPathComponent().appendingPathComponent("health")
        var request = URLRequest(url: healthURL)
        request.timeoutInterval = 2
        if let (_, response) = try? await URLSession.shared.data(for: request),
           let http = response as? HTTPURLResponse, http.statusCode == 200 {
            return true
        }
        // Fallback for servers without /health.
        var modelsRequest = URLRequest(url: baseURL.appendingPathComponent("models"))
        modelsRequest.timeoutInterval = 2
        if let (_, response) = try? await URLSession.shared.data(for: modelsRequest),
           let http = response as? HTTPURLResponse, http.statusCode == 200 {
            return true
        }
        return false
    }

    // MARK: - Chat

    enum ChatError: LocalizedError {
        case httpError(Int, String)

        var errorDescription: String? {
            switch self {
            case .httpError(let code, let body):
                return "Server returned HTTP \(code). \(body)"
            }
        }
    }

    /// Streams a chat completion, emitting content deltas as they arrive.
    func chat(model: String, messages: [ChatMessage], temperature: Double) -> AnyPublisher<String, Error> {
        let subject = PassthroughSubject<String, Error>()
        let modelId = modelIdsByName[model] ?? model
        let url = baseURL.appendingPathComponent("chat/completions")

        var task: Task<Void, Never>?
        return subject
            .handleEvents(receiveSubscription: { _ in
                task = Task {
                    do {
                        var request = URLRequest(url: url)
                        request.httpMethod = "POST"
                        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
                        request.timeoutInterval = 3600
                        request.httpBody = try JSONSerialization.data(withJSONObject: [
                            "model": modelId,
                            "messages": messages.map(Self.encodeMessage),
                            "temperature": temperature,
                            "stream": true,
                        ])

                        let (bytes, response) = try await URLSession.shared.bytes(for: request)
                        if let http = response as? HTTPURLResponse, http.statusCode != 200 {
                            var body = ""
                            for try await line in bytes.lines {
                                body += line
                                if body.count > 500 { break }
                            }
                            throw ChatError.httpError(http.statusCode, body)
                        }

                        for try await line in bytes.lines {
                            guard line.hasPrefix("data: ") else { continue }
                            let payload = String(line.dropFirst(6))
                            if payload == "[DONE]" { break }
                            if let delta = Self.contentDelta(fromChunk: payload), !delta.isEmpty {
                                subject.send(delta)
                            }
                        }
                        subject.send(completion: .finished)
                    } catch {
                        if !Task.isCancelled {
                            subject.send(completion: .failure(error))
                        }
                    }
                }
            }, receiveCancel: {
                task?.cancel()
            })
            .eraseToAnyPublisher()
    }

    private static func encodeMessage(_ message: ChatMessage) -> [String: Any] {
        guard !message.images.isEmpty else {
            return ["role": message.role.rawValue, "content": message.content]
        }
        var parts: [[String: Any]] = [["type": "text", "text": message.content]]
        for image in message.images {
            parts.append([
                "type": "image_url",
                "image_url": ["url": "data:image/jpeg;base64,\(image)"],
            ])
        }
        return ["role": message.role.rawValue, "content": parts]
    }

    private static func contentDelta(fromChunk payload: String) -> String? {
        guard let data = payload.data(using: .utf8),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let choices = json["choices"] as? [[String: Any]],
              let delta = choices.first?["delta"] as? [String: Any] else {
            return nil
        }
        return delta["content"] as? String
    }
}
