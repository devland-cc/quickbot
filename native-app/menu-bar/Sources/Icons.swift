import Cocoa

/// Menu bar icons: lucide `bot` (on) and `bot-off` (off).
/// https://lucide.dev/icons/bot  ·  https://lucide.dev/icons/bot-off
enum Icons {

    static let size = NSSize(width: 18, height: 18)

    static let on: NSImage = load(named: "bot")
    static let off: NSImage = load(named: "bot-off")

    /// Single mapping between server state and tray icon.
    /// Running => bot. Any other state => bot-off.
    static func image(forRunning running: Bool) -> NSImage {
        running ? on : off
    }

    static func name(forRunning running: Bool) -> String {
        running ? "bot" : "bot-off"
    }

    /// Loads the vector PDF from the bundle, with fallbacks for running outside it.
    static func load(named name: String) -> NSImage {
        for url in candidateURLs(for: name) {
            if let image = NSImage(contentsOf: url) {
                image.size = size
                image.isTemplate = true // follows the menu bar's dark/light mode
                return image
            }
        }

        // Last resort: SF Symbol, so the app is never left without an icon.
        let symbol = name == "bot" ? "brain" : "brain.head.profile"
        let image = NSImage(systemSymbolName: symbol, accessibilityDescription: name)
            ?? NSImage(size: size)
        image.size = size
        image.isTemplate = true
        return image
    }

    private static func candidateURLs(for name: String) -> [URL] {
        var urls: [URL] = []
        if let bundled = Bundle.main.url(forResource: name, withExtension: "pdf") {
            urls.append(bundled)
        }
        let exeDir = URL(fileURLWithPath: CommandLine.arguments[0]).deletingLastPathComponent()
        urls.append(exeDir.appendingPathComponent("\(name).pdf"))
        urls.append(exeDir.appendingPathComponent("../Resources/\(name).pdf"))
        if let env = ProcessInfo.processInfo.environment["QUICKBOT_ICON_DIR"] {
            urls.append(URL(fileURLWithPath: env).appendingPathComponent("\(name).pdf"))
        }
        return urls
    }
}
