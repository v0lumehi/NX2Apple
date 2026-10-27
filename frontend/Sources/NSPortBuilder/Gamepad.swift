import AppKit
import GameController

/// A game controller drives the library, so a Mac on a TV needs no keyboard. The buttons are named as the game names
/// them, by position (dm/host/front_mac.m pad_read): A on the right, B at the bottom, X at the top, Y on the left.
/// Left and right on the D-pad or the left stick move through the collection, A plays, X opens the launch options,
/// B goes back. Input counts only while the launcher is the active app: a game it started is in front then, and
/// GameController gives a controller's input to the app in front, so the launcher never acts behind a running game.
final class LauncherGamepad: ObservableObject {
    enum Action: Equatable { case left, right, a, b, x, y }

    @Published private(set) var connected = false
    var perform: (Action) -> Void = { _ in }
    private var held: Set<String> = []   // edges, not levels: one press, one action
    private var observers: [NSObjectProtocol] = []

    init() {
        let center = NotificationCenter.default
        observers = [NSNotification.Name.GCControllerDidConnect, .GCControllerDidDisconnect].map {
            center.addObserver(forName: $0, object: nil, queue: .main) { [weak self] _ in self?.attach() }
        }
        attach()
    }

    deinit { observers.forEach(NotificationCenter.default.removeObserver) }

    private func attach() {
        let pads = GCController.controllers().compactMap(\.extendedGamepad)
        connected = !pads.isEmpty
        for pad in pads { pad.valueChangedHandler = { [weak self] pad, _ in self?.read(pad) } }
    }

    private func read(_ pad: GCExtendedGamepad) {
        let x = pad.dpad.xAxis.value != 0 ? pad.dpad.xAxis.value : pad.leftThumbstick.xAxis.value
        let state: [(String, Bool, Action)] = [
            ("left", x < -0.5, .left), ("right", x > 0.5, .right),
            ("a", pad.buttonB.isPressed, .a), ("b", pad.buttonA.isPressed, .b),   // by position: right A, bottom B, top X, left Y
            ("x", pad.buttonY.isPressed, .x), ("y", pad.buttonX.isPressed, .y),
        ]
        for (key, down, action) in state {
            if down, !held.contains(key) {
                held.insert(key)
                DispatchQueue.main.async { if NSApp.isActive { self.perform(action) } }
            } else if !down {
                held.remove(key)
            }
        }
    }
}
