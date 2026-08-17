// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "QuickbotChat",
    platforms: [.macOS(.v14)],
    dependencies: [
        // Vendored: upstream ships #Preview blocks, which cannot compile
        // without Xcode's PreviewsMacros plugin (this project builds with
        // Command Line Tools only).
        .package(path: "Vendor/KeyboardShortcuts"),
        .package(url: "https://github.com/AugustDev/Magnet", revision: "4865f86d9baa24684dedacd6677beb2d8b30d88e"),
        .package(url: "https://github.com/AugustDev/Splash", revision: "c31eba0866102be9be29391dac641ecb46795702"),
        .package(url: "https://github.com/exyte/ActivityIndicatorView.git", from: "1.1.1"),
        .package(url: "https://github.com/gonzalezreal/swift-markdown-ui", from: "2.4.1"),
        .package(url: "https://github.com/ksemianov/WrappingHStack", from: "0.2.0"),
        .package(url: "https://github.com/apple/swift-async-algorithms.git", from: "1.0.0"),
    ],
    targets: [
        .executableTarget(
            name: "QuickbotChat",
            dependencies: [
                "KeyboardShortcuts",
                "Magnet",
                "Splash",
                "ActivityIndicatorView",
                .product(name: "MarkdownUI", package: "swift-markdown-ui"),
                "WrappingHStack",
                .product(name: "AsyncAlgorithms", package: "swift-async-algorithms"),
            ],
            path: "QuickbotChat"
        )
    ]
)
