//
//  AssetColors.swift
//  Quickbot Chat
//
//  The Xcode build generated these color symbols from Assets.xcassets.
//  This SwiftPM build has no asset catalog compiler, so the palette is
//  defined in code (values copied from the original colorsets).
//

import SwiftUI

#if os(macOS)
import AppKit

private func dynamicColor(light: NSColor, dark: NSColor) -> Color {
    Color(nsColor: NSColor(name: nil) { appearance in
        appearance.bestMatch(from: [.darkAqua, .aqua]) == .darkAqua ? dark : light
    })
}

extension Color {
    static let label = Color(nsColor: .labelColor)
    static let bgCustom = dynamicColor(
        light: .white,
        dark: NSColor(srgbRed: 0.11, green: 0.11, blue: 0.12, alpha: 1)
    )
    static let grayCustom = Color(.sRGB, red: 0.557, green: 0.557, blue: 0.576, opacity: 1)
    static let gray2Custom = dynamicColor(
        light: NSColor(srgbRed: 0.676, green: 0.697, blue: 0.679, alpha: 1),
        dark: NSColor(srgbRed: 0.388, green: 0.388, blue: 0.400, alpha: 1)
    )
    static let gray3Custom = dynamicColor(
        light: NSColor(srgbRed: 0.792, green: 0.790, blue: 0.814, alpha: 1),
        dark: NSColor(srgbRed: 0.282, green: 0.282, blue: 0.290, alpha: 1)
    )
    static let gray4Custom = dynamicColor(
        light: NSColor(srgbRed: 0.820, green: 0.820, blue: 0.839, alpha: 1),
        dark: NSColor(srgbRed: 0.227, green: 0.227, blue: 0.235, alpha: 1)
    )
    static let gray5Custom = dynamicColor(
        light: NSColor(srgbRed: 0.898, green: 0.898, blue: 0.918, alpha: 1),
        dark: NSColor(srgbRed: 0.173, green: 0.173, blue: 0.180, alpha: 1)
    )
}

extension ShapeStyle where Self == Color {
    static var label: Color { .label }
    static var bgCustom: Color { .bgCustom }
    static var grayCustom: Color { .grayCustom }
    static var gray2Custom: Color { .gray2Custom }
    static var gray3Custom: Color { .gray3Custom }
    static var gray4Custom: Color { .gray4Custom }
    static var gray5Custom: Color { .gray5Custom }
}
#endif
