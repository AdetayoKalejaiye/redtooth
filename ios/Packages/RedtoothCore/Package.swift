// swift-tools-version:5.10
// RedtoothCore: protocol, transfers, directory client, request parser, people resolution and the agent.
// Builds for iOS (the app and share extension) and macOS (so `swift test` can talk to the Python desktop).
import PackageDescription

let package = Package(
    name: "RedtoothCore",
    platforms: [.iOS(.v17), .macOS(.v14)],
    products: [.library(name: "RedtoothCore", targets: ["RedtoothCore"])],
    targets: [
        .target(name: "RedtoothCore"),
        .testTarget(name: "RedtoothCoreTests", dependencies: ["RedtoothCore"], resources: [.copy("Resources/intent_fixtures.json"), .copy("Resources/phone_fixtures.json")]),
    ]
)
