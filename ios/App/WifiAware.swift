import Foundation
import Network
import RedtoothCore
import SwiftUI

#if canImport(WiFiAware)
import DeviceDiscoveryUI
import WiFiAware

/// Wi-Fi Aware (iOS 26, iPhone 12 and later): a direct phone-to-phone link with no shared Wi-Fi
/// network. Apple requires a one-time pairing per pair of devices through the system UI
/// (DevicePairingView / DevicePicker); after that, paired devices find each other automatically.
///
/// UNVERIFIED: written against the iOS 26 SDK as documented (WWDC25 “Supercharge device connectivity
/// with Wi-Fi Aware”). It has not been compiled here; check names against the shipping SDK.
@available(iOS 26.0, *)
final class WifiAwareLink: @unchecked Sendable {
    static let serviceName = "_redtooth._tcp"
    private var listenTask: Task<Void, Never>?

    static var supported: Bool { WACapabilities.supportedFeatures.contains(.wifiAware) }
    static var publishable: WAPublishableService? { WAPublishableService.allServices[serviceName] }
    static var subscribable: WASubscribableService? { WASubscribableService.allServices[serviceName] }

    /// Accept Redtooth connections from paired devices and hand each one to the same Receiver as the LAN.
    func startListening(receiver: Receiver) {
        guard Self.supported, let service = Self.publishable else { return }
        listenTask = Task {
            do {
                try await NetworkListener(for: .wifiAware(.connecting(to: service, from: .allPairedDevices)),
                                          using: .parameters { TCP() })
                    .run { connection in
                        await receiver.handle(AwareChannel(connection))
                    }
            } catch {
                // Listener ends when the app is suspended; RedtoothApp restarts it on foreground.
            }
        }
    }

    func stop() { listenTask?.cancel() }

    /// Find a paired device that matches a Redtooth device (by the name it registered) and open a channel.
    static func connect(to device: Device, timeout: TimeInterval = 6) async throws -> ByteChannel {
        guard supported, let service = subscribable else { throw WireError.closed }
        let browser = NetworkBrowser(for: .wifiAware(.connecting(to: .allPairedDevices, from: service)))
        let endpoint = try await withTimeout(timeout) {
            try await browser.run { endpoints in
                if let e = endpoints.first(where: { ($0.device?.name ?? "").localizedCaseInsensitiveContains(device.name) }) ?? endpoints.first {
                    return .finish(e)
                }
                return .continue
            }
        }
        let conn = NetworkConnection(to: endpoint, using: .parameters { TCP() })
        return AwareChannel(conn)
    }
}

/// ByteChannel over the iOS 26 NetworkConnection (raw TCP bytes, so the Redtooth framing is unchanged).
@available(iOS 26.0, *)
final class AwareChannel: ByteChannel, @unchecked Sendable {
    let conn: NetworkConnection<TCP>
    init(_ c: NetworkConnection<TCP>) { conn = c }
    func send(_ data: Data) async throws { try await conn.send(data) }
    func receive(exactly n: Int) async throws -> Data { try await conn.receive(exactly: n).content }
    func close() {}
}

/// One-time pairing sheet: “Pair with Daniel's iPhone” — the system shows the code/confirmation.
@available(iOS 26.0, *)
struct AwarePairingButton: View {
    var body: some View {
        if WifiAwareLink.supported, let service = WifiAwareLink.subscribable {
            DevicePicker(.wifiAware(.connecting(to: .selected([]), from: service))) { _ in
            } label: {
                Label("Pair a nearby phone", systemImage: "antenna.radiowaves.left.and.right")
            } fallback: {
                EmptyView()
            }
        }
    }
}
#endif

/// What the rest of the app calls, so nothing else needs #if / @available.
enum DirectLink {
    static var available: Bool {
        #if canImport(WiFiAware)
        if #available(iOS 26.0, *) { return WifiAwareLink.supported }
        #endif
        return false
    }

    static func connect(_ device: Device) async -> ByteChannel? {
        #if canImport(WiFiAware)
        if #available(iOS 26.0, *), device.wifiAware { return try? await WifiAwareLink.connect(to: device) }
        #endif
        return nil
    }
}
