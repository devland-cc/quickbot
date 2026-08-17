import Foundation

enum ServerState: Equatable {
    case stopped
    case starting
    case running
    case stopping
    case failed(String)

    var isOn: Bool { self == .running }

    var isBusy: Bool { self == .starting || self == .stopping }

    var isFailed: Bool {
        if case .failed = self { return true }
        return false
    }

    /// Text shown in the menu's status item.
    var label: String {
        switch self {
        case .stopped:  return "Off"
        case .starting: return "Starting… (loading model)"
        case .running:  return "On"
        case .stopping: return "Stopping…"
        case .failed:   return "Failed"
        }
    }
}

/// Snapshot reported by `serverctl status --json`.
struct ServerStatus: Decodable {
    var state: String
    var pid: Int32?
    var adopted: Bool
    var startedAtEpoch: Double?
    var modelName: String
    var modelPath: String
    var draftModelPath: String?
    var endpoint: String
    var configFile: String
    var logFile: String
    var stopServerOnQuit: Bool
}

/// Location of the decoupled server component.
enum ServerEnvironment {
    static var directory: URL {
        if let env = ProcessInfo.processInfo.environment["QUICKBOT_SERVER_DIR"], !env.isEmpty {
            return URL(fileURLWithPath: (env as NSString).expandingTildeInPath)
        }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Devland/_experimental/quickbot/server")
    }

    static var serverctl: URL { directory.appendingPathComponent("serverctl") }
}

/// Thin client over `serverctl`. All process/port/PID logic lives in the
/// server component; this class only invokes it and tracks UI state.
final class ServerController {

    // MARK: - State

    private(set) var state: ServerState = .stopped {
        didSet {
            guard state != oldValue else { return }
            let s = state
            DispatchQueue.main.async { self.onStateChange?(s) }
        }
    }

    /// Last status snapshot from serverctl (nil until the first poll).
    private(set) var status: ServerStatus?

    var onStateChange: ((ServerState) -> Void)?

    // MARK: - Internals

    private var monitorTimer: DispatchSourceTimer?
    private let queue = DispatchQueue(label: "com.quickbot.server", qos: .utility)
    private var startDeadline: Date?

    /// Maximum time to wait for the model to load before considering it a failure.
    private let startupTimeout: TimeInterval = 900

    // MARK: - Public lifecycle

    /// Syncs the initial state (adopting any running server) and starts polling.
    func bootstrap() {
        queue.async {
            guard FileManager.default.isExecutableFile(atPath: ServerEnvironment.serverctl.path) else {
                let msg = "serverctl not found:\n\(ServerEnvironment.serverctl.path)"
                self.state = .failed(msg)
                return
            }
            self.pollStatus()
            self.startMonitor()
        }
    }

    func toggle() {
        switch state {
        case .running, .starting:
            stop()
        case .stopped, .failed:
            start()
        case .stopping:
            break // wait for it to finish
        }
    }

    func start() {
        queue.async { self.performStart() }
    }

    func stop() {
        queue.async { self.performStop() }
    }

    /// Called on app termination; stops the server only if configured to,
    /// and never one we merely adopted.
    func shutdown() {
        guard status?.stopServerOnQuit == true, status?.adopted == false,
              state == .running || state == .starting else { return }
        queue.sync { self.performStop() }
    }

    // MARK: - serverctl invocation

    @discardableResult
    private func runServerctl(_ arguments: [String]) -> (code: Int32, output: String) {
        let proc = Process()
        proc.executableURL = ServerEnvironment.serverctl
        proc.arguments = arguments
        let pipe = Pipe()
        proc.standardOutput = pipe
        proc.standardError = pipe

        do { try proc.run() } catch {
            return (-1, error.localizedDescription)
        }
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        proc.waitUntilExit()
        let output = String(data: data, encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        return (proc.terminationStatus, output)
    }

    private func fetchStatus() -> ServerStatus? {
        let result = runServerctl(["status", "--json"])
        guard result.code == 0, let data = result.output.data(using: .utf8) else { return nil }
        return try? JSONDecoder().decode(ServerStatus.self, from: data)
    }

    // MARK: - Actions

    private func performStart() {
        guard state == .stopped || state.isFailed else { return }
        let result = runServerctl(["start"])
        if result.code != 0 {
            state = .failed(result.output.isEmpty ? "Could not start the server." : result.output)
            return
        }
        startDeadline = Date().addingTimeInterval(startupTimeout)
        state = .starting
        pollStatus()
    }

    private func performStop() {
        guard state != .stopping else { return }
        state = .stopping
        _ = runServerctl(["stop"]) // blocks until the port is free (up to ~26s)
        state = .stopped
        pollStatus()
    }

    // MARK: - Monitoring

    private func startMonitor() {
        monitorTimer?.cancel()
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + 2, repeating: 3)
        timer.setEventHandler { [weak self] in self?.pollStatus() }
        timer.resume()
        monitorTimer = timer
    }

    private func pollStatus() {
        guard let snapshot = fetchStatus() else { return }
        status = snapshot

        switch snapshot.state {
        case "running":
            if state != .stopping { state = .running }

        case "starting":
            if state == .stopped || state.isFailed {
                // Server started outside the app: adopt it.
                startDeadline = Date().addingTimeInterval(startupTimeout)
                state = .starting
            } else if state == .starting, let deadline = startDeadline, Date() > deadline {
                _ = runServerctl(["stop"])
                state = .failed("Timed out starting the server.")
            }

        case "stopped":
            switch state {
            case .starting:
                state = .failed("The server exited during startup. See the log.")
            case .running, .stopping:
                state = .stopped
            case .stopped, .failed:
                break
            }

        default:
            break
        }
    }

    // MARK: - Menu info

    var pid: Int32? { status?.pid }
    var isAdopted: Bool { status?.adopted ?? false }

    var uptimeText: String? {
        guard state == .running, let epoch = status?.startedAtEpoch else { return nil }
        let seconds = Int(Date().timeIntervalSince(Date(timeIntervalSince1970: epoch)))
        let h = seconds / 3600, m = (seconds % 3600) / 60, s = seconds % 60
        return h > 0 ? String(format: "%dh %02dmin", h, m)
                     : (m > 0 ? String(format: "%dmin %02ds", m, s) : "\(s)s")
    }
}
