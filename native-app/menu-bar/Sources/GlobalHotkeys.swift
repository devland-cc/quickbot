import Carbon.HIToolbox
import Foundation

/// Registers ⌘; and ⌘⇧; as global hotkeys while Quickbot is off, so
/// pressing them opens the menu and reveals the off state. While the server
/// is on these combos belong to Quickbot Chat (panel / chat window), so
/// they are unregistered here.
final class GlobalHotkeys {
    var onPress: (() -> Void)?

    private var hotKeyRefs: [EventHotKeyRef] = []
    private var eventHandler: EventHandlerRef?

    func register() {
        guard hotKeyRefs.isEmpty else { return }
        installHandlerIfNeeded()

        let semicolon = UInt32(kVK_ANSI_Semicolon)
        let combos: [(id: UInt32, modifiers: UInt32)] = [
            (1, UInt32(cmdKey)),
            (2, UInt32(cmdKey | shiftKey)),
        ]
        for combo in combos {
            var ref: EventHotKeyRef?
            let hotKeyID = EventHotKeyID(signature: OSType(0x51424F54), id: combo.id) // 'QBOT'
            let status = RegisterEventHotKey(
                semicolon, combo.modifiers, hotKeyID,
                GetApplicationEventTarget(), 0, &ref
            )
            if status == noErr, let ref {
                hotKeyRefs.append(ref)
            }
        }
    }

    func unregister() {
        hotKeyRefs.forEach { UnregisterEventHotKey($0) }
        hotKeyRefs.removeAll()
    }

    private func installHandlerIfNeeded() {
        guard eventHandler == nil else { return }
        var spec = EventTypeSpec(
            eventClass: OSType(kEventClassKeyboard),
            eventKind: UInt32(kEventHotKeyPressed)
        )
        InstallEventHandler(
            GetApplicationEventTarget(),
            { _, _, userData -> OSStatus in
                guard let userData else { return noErr }
                let hotkeys = Unmanaged<GlobalHotkeys>.fromOpaque(userData).takeUnretainedValue()
                DispatchQueue.main.async { hotkeys.onPress?() }
                return noErr
            },
            1, &spec,
            Unmanaged.passUnretained(self).toOpaque(),
            &eventHandler
        )
    }
}
