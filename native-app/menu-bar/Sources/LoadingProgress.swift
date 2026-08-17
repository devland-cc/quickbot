import Cocoa
import Darwin

/// Utilities backing the startup progress UI: the model-load percentage
/// estimate and the spinning menu bar icon.
enum LoadingProgress {

    /// Physical memory footprint of a process (what Activity Monitor shows).
    /// While mlx loads a model, this grows roughly linearly up to the total
    /// weight size, so footprint / weightBytes ≈ load progress.
    static func processFootprint(pid: pid_t) -> UInt64? {
        var info = rusage_info_current()
        let result = withUnsafeMutablePointer(to: &info) {
            $0.withMemoryRebound(to: (rusage_info_t?).self, capacity: 1) {
                proc_pid_rusage(pid, RUSAGE_INFO_CURRENT, $0)
            }
        }
        guard result == 0 else { return nil }
        return info.ri_phys_footprint
    }

    /// Total size in bytes of all files under a directory.
    static func directoryBytes(_ path: String) -> UInt64 {
        let url = URL(fileURLWithPath: path)
        guard let enumerator = FileManager.default.enumerator(
            at: url, includingPropertiesForKeys: [.fileSizeKey]
        ) else { return 0 }
        var total: UInt64 = 0
        for case let file as URL in enumerator {
            total += UInt64((try? file.resourceValues(forKeys: [.fileSizeKey]))?.fileSize ?? 0)
        }
        return total
    }
}

/// Spinning-arc animation shown in place of the menu bar icon while
/// Quickbot is starting (a lightweight take on the classic Lottie loader).
final class SpinnerIcon {
    private static let frameCount = 12
    private static let frames: [NSImage] = (0..<frameCount).map { index in
        arcImage(rotationDegrees: CGFloat(index) / CGFloat(frameCount) * 360)
    }

    private var timer: Timer?
    private var index = 0

    var isSpinning: Bool { timer != nil }

    func start(on button: NSStatusBarButton?) {
        guard timer == nil, let button else { return }
        button.image = Self.frames[0]
        index = 1
        let timer = Timer(timeInterval: 1.0 / 12.0, repeats: true) { [weak self, weak button] _ in
            guard let self else { return }
            button?.image = Self.frames[self.index % Self.frameCount]
            self.index += 1
        }
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
    }

    func stop() {
        timer?.invalidate()
        timer = nil
    }

    private static func arcImage(rotationDegrees: CGFloat) -> NSImage {
        let image = NSImage(size: NSSize(width: 18, height: 18), flipped: false) { _ in
            let path = NSBezierPath()
            // 300° arc with a 60° gap; the gap position rotates each frame.
            path.appendArc(
                withCenter: NSPoint(x: 9, y: 9), radius: 6.5,
                startAngle: -rotationDegrees, endAngle: -rotationDegrees + 300
            )
            path.lineWidth = 2
            path.lineCapStyle = .round
            NSColor.black.setStroke()
            path.stroke()
            return true
        }
        image.isTemplate = true
        return image
    }
}
