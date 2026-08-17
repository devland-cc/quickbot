import Cocoa
import QuartzCore

/// Small switch control shaped like NSSwitch, with a green "on" track.
/// NSSwitch offers no public tint API (it always follows the system accent
/// color), hence the custom control.
final class SwitchControl: NSControl {

    private static let size = NSSize(width: 38, height: 22)
    private static let knobInset: CGFloat = 2

    private let track = CALayer()
    private let knob = CALayer()

    /// Setting this programmatically does not send the action;
    /// only a click does.
    var isOn = false {
        didSet { updateAppearance(animated: true) }
    }

    /// While Quickbot is still loading (model, chat interface) the switch
    /// sits in the "on" position but without the green accent.
    var showsAccent = true {
        didSet { updateAppearance(animated: true) }
    }

    override var isEnabled: Bool {
        didSet { alphaValue = isEnabled ? 1 : 0.45 }
    }

    init() {
        super.init(frame: NSRect(origin: .zero, size: Self.size))
        wantsLayer = true

        track.frame = bounds
        track.cornerRadius = bounds.height / 2
        layer?.addSublayer(track)

        let diameter = bounds.height - Self.knobInset * 2
        knob.frame = NSRect(x: Self.knobInset, y: Self.knobInset,
                            width: diameter, height: diameter)
        knob.cornerRadius = diameter / 2
        knob.backgroundColor = NSColor.white.cgColor
        knob.shadowColor = NSColor.black.cgColor
        knob.shadowOpacity = 0.25
        knob.shadowOffset = CGSize(width: 0, height: -1)
        knob.shadowRadius = 1.5
        layer?.addSublayer(knob)

        updateAppearance(animated: false)
    }

    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    override func mouseDown(with event: NSEvent) {
        guard isEnabled else { return }
        isOn.toggle()
        sendAction(action, to: target)
    }

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        updateAppearance(animated: false)
    }

    private func updateAppearance(animated: Bool) {
        // Resolve dynamic colors against the current light/dark appearance.
        var trackColor = NSColor.systemGray.withAlphaComponent(0.4).cgColor
        effectiveAppearance.performAsCurrentDrawingAppearance {
            trackColor = ((isOn && showsAccent) ? NSColor.systemGreen
                               : NSColor.systemGray.withAlphaComponent(0.4)).cgColor
        }
        let diameter = bounds.height - Self.knobInset * 2
        let knobX = isOn ? bounds.width - Self.knobInset - diameter : Self.knobInset

        CATransaction.begin()
        CATransaction.setAnimationDuration(animated ? 0.18 : 0)
        CATransaction.setDisableActions(!animated)
        track.backgroundColor = trackColor
        knob.frame.origin.x = knobX
        CATransaction.commit()
    }
}
