import CryptoKit
import Foundation
import Network

/// A reliable byte stream: an NWConnection on the LAN, or a Wi-Fi Aware NetworkConnection in the app.
public protocol ByteChannel: AnyObject, Sendable {
    func send(_ data: Data) async throws
    func receive(exactly n: Int) async throws -> Data
    func close()
}

public enum WireError: Error, LocalizedError {
    case closed, tooLarge, badHeader, timeout, unexpected(String), refused(String)
    public var errorDescription: String? {
        switch self {
        case .closed: "connection closed"
        case .tooLarge: "frame too large"
        case .badHeader: "bad frame header"
        case .timeout: "timed out"
        case .unexpected(let s): "unexpected \(s)"
        case .refused(let s): s
        }
    }
}

/// Redtooth wire format (docs/PROTOCOL.md): 4-byte big-endian header length, JSON header, then `len` payload bytes.
public enum Frames {
    public static let maxHeader = 64 * 1024
    public static let maxChunk = 8 * 1024 * 1024
    public static let chunk = 1024 * 1024
    /// Chunk hashes this client verifies. Phones use SHA-256 ("s256"): neither platform ships BLAKE2.
    public static let chunkHashes = ["s256"]

    public struct Frame {
        public var header: JSONObject
        public var payload: Data
        public var type: String? { header.str("type") }
    }

    public static func encode(_ header: JSONObject, payload: Data? = nil) throws -> Data {
        var h = header
        if let payload, !payload.isEmpty { h["len"] = payload.count }
        let raw = try JSONSerialization.data(withJSONObject: h)
        var out = Data(capacity: 4 + raw.count + (payload?.count ?? 0))
        var n = UInt32(raw.count).bigEndian
        withUnsafeBytes(of: &n) { out.append(contentsOf: $0) }
        out.append(raw)
        if let payload { out.append(payload) }
        return out
    }

    public static func write(_ ch: ByteChannel, _ header: JSONObject, payload: Data? = nil) async throws {
        try await ch.send(encode(header, payload: payload))
    }

    public static func read(_ ch: ByteChannel) async throws -> Frame {
        let lenData = try await ch.receive(exactly: 4)
        let n = lenData.withUnsafeBytes { Int(UInt32(bigEndian: $0.loadUnaligned(as: UInt32.self))) }
        guard n > 0, n <= maxHeader else { throw WireError.tooLarge }
        let raw = try await ch.receive(exactly: n)
        guard let header = try JSONSerialization.jsonObject(with: raw) as? JSONObject else { throw WireError.badHeader }
        let len = header.int("len") ?? 0
        guard len >= 0, len <= maxChunk else { throw WireError.tooLarge }
        let payload = len > 0 ? try await ch.receive(exactly: len) : Data()
        return Frame(header: header, payload: payload)
    }

    public static func s256(_ d: Data) -> String { sha256Hex(d) }
}

/// NWConnection as a ByteChannel (TCP on the LAN).
public final class NWByteChannel: ByteChannel, @unchecked Sendable {
    public let connection: NWConnection
    private let queue = DispatchQueue(label: "redtooth.nw")

    public init(_ c: NWConnection) { connection = c }

    /// Connect to host:port and wait until ready (or fail).
    public static func connect(host: String, port: Int, timeout: TimeInterval = 5) async throws -> NWByteChannel {
        let c = NWConnection(host: NWEndpoint.Host(host), port: NWEndpoint.Port(integerLiteral: UInt16(port)), using: .tcp)
        let ch = NWByteChannel(c)
        try await ch.start(timeout: timeout)
        return ch
    }

    /// Start an accepted or created connection and wait for .ready.
    public func start(timeout: TimeInterval = 5) async throws {
        try await withCheckedThrowingContinuation { (cont: CheckedContinuation<Void, Error>) in
            let once = Once()
            connection.stateUpdateHandler = { state in
                switch state {
                case .ready: once.run { cont.resume() }
                case .failed(let e): once.run { cont.resume(throwing: e) }
                case .cancelled: once.run { cont.resume(throwing: WireError.closed) }
                case .waiting(let e): once.run { cont.resume(throwing: e) } // unreachable host: fail fast, the sender retries
                default: break
                }
            }
            connection.start(queue: queue)
            queue.asyncAfter(deadline: .now() + timeout) { once.run { cont.resume(throwing: WireError.timeout) } }
        }
    }

    public func send(_ data: Data) async throws {
        try await withCheckedThrowingContinuation { (cont: CheckedContinuation<Void, Error>) in
            connection.send(content: data, completion: .contentProcessed { err in
                if let err { cont.resume(throwing: err) } else { cont.resume() }
            })
        }
    }

    public func receive(exactly n: Int) async throws -> Data {
        var out = Data(capacity: n)
        while out.count < n {
            let need = n - out.count
            let part: Data = try await withCheckedThrowingContinuation { cont in
                connection.receive(minimumIncompleteLength: 1, maximumLength: need) { data, _, isComplete, err in
                    if let err { cont.resume(throwing: err); return }
                    if let data, !data.isEmpty { cont.resume(returning: data); return }
                    cont.resume(throwing: isComplete ? WireError.closed : WireError.unexpected("empty read"))
                }
            }
            out.append(part)
        }
        return out
    }

    public func close() { connection.cancel() }
}

/// Runs a closure at most once (continuation safety).
final class Once: @unchecked Sendable {
    private var done = false
    private let lock = NSLock()
    func run(_ f: () -> Void) {
        lock.lock(); defer { lock.unlock() }
        if !done { done = true; f() }
    }
}

/// Run `op` with a deadline.
public func withTimeout<T: Sendable>(_ seconds: Double, _ op: @escaping @Sendable () async throws -> T) async throws -> T {
    try await withThrowingTaskGroup(of: T.self) { g in
        g.addTask { try await op() }
        g.addTask { try await Task.sleep(nanoseconds: UInt64(seconds * 1e9)); throw WireError.timeout }
        let r = try await g.next()!
        g.cancelAll()
        return r
    }
}
