import CryptoKit
import Foundation
import Network

// MARK: - Receiving

public final class IncomingOffer: @unchecked Sendable, Identifiable {
    public let id: String
    public let name: String
    public let size: Int64
    public let mime: String
    public let fingerprint: String
    public let senderName: String
    public let senderPhone: String
    public var received: Int64 = 0
    public var status = "waiting" // waiting | receiving | saved | declined | failed | interrupted | cancelled
    public var savedURL: URL?
    init(id: String, name: String, size: Int64, mime: String, fingerprint: String, senderName: String, senderPhone: String) {
        self.id = id; self.name = name; self.size = size; self.mime = mime; self.fingerprint = fingerprint
        self.senderName = senderName; self.senderPhone = senderPhone
    }
}

/// Always-listening receiver: verifies each chunk as it arrives, journals the verified offset so an
/// interrupted transfer resumes, and checks the whole-file SHA-256 before saving and confirming.
public final class Receiver: @unchecked Sendable {
    public let deviceId: String
    public let deviceName: String
    public let inbox: URL
    private let partial: URL
    private let acceptor: @Sendable (IncomingOffer) async -> Bool
    private let events: @Sendable (IncomingOffer) -> Void
    private let acceptTimeout: Double
    private var offers: [String: IncomingOffer] = [:]
    private let lock = NSLock()

    public init(deviceId: String, deviceName: String, inbox: URL, acceptTimeout: Double = 120,
                acceptor: @escaping @Sendable (IncomingOffer) async -> Bool, events: @escaping @Sendable (IncomingOffer) -> Void = { _ in }) {
        self.deviceId = deviceId; self.deviceName = deviceName; self.inbox = inbox
        self.partial = inbox.appendingPathComponent(".redtooth-partial", isDirectory: true)
        self.acceptor = acceptor; self.events = events; self.acceptTimeout = acceptTimeout
        try? FileManager.default.createDirectory(at: partial, withIntermediateDirectories: true)
    }

    /// Serve one connection (LAN or Wi-Fi Aware).
    public func handle(_ ch: ByteChannel) async {
        defer { ch.close() }
        do {
            let first = try await Frames.read(ch)
            switch first.type {
            case "PING": try await Frames.write(ch, ["type": "PONG", "device_id": deviceId, "name": deviceName])
            case "OFFER": try await receive(first.header, ch)
            default: break
            }
        } catch {}
    }

    private func journalURL(_ id: String) -> URL { partial.appendingPathComponent(id + ".json") }
    private func partURL(_ id: String) -> URL { partial.appendingPathComponent(id + ".part") }
    private func loadJournal(_ id: String) -> JSONObject? {
        guard let d = try? Data(contentsOf: journalURL(id)) else { return nil }
        return (try? JSONSerialization.jsonObject(with: d)) as? JSONObject
    }
    private func saveJournal(_ id: String, _ j: JSONObject) {
        if let d = try? JSONSerialization.data(withJSONObject: j) { try? d.write(to: journalURL(id), options: .atomic) }
    }

    private func offer(for id: String, _ h: JSONObject) -> IncomingOffer {
        lock.lock(); defer { lock.unlock() }
        if let o = offers[id] { return o }
        let from = h.obj("from") ?? [:]
        let o = IncomingOffer(id: id, name: Receiver.safeName(h.str("name") ?? "file"), size: h.int64("size") ?? 0, mime: h.str("mime") ?? "",
                              fingerprint: h.str("fingerprint") ?? "", senderName: from.str("name") ?? "Someone", senderPhone: from.str("phone") ?? "")
        offers[id] = o
        return o
    }

    private func receive(_ h: JSONObject, _ ch: ByteChannel) async throws {
        guard let rawId = h.str("id"), let size = h.int64("size") else { return }
        let id = String(rawId.prefix(64).filter { $0.isLetter || $0.isNumber || $0 == "_" || $0 == "-" })
        let o = offer(for: id, h)
        var offset: Int64 = 0
        if let j = loadJournal(id), j.str("fingerprint") == o.fingerprint, j.bool("accepted") == true {
            offset = j.int64("verified") ?? 0
        } else {
            o.status = "waiting"; events(o)
            try await Frames.write(ch, ["type": "WAITING"])
            let acceptor = self.acceptor, timeout = acceptTimeout
            let ok = (try? await withTimeout(timeout) { await acceptor(o) }) ?? false
            guard ok else {
                o.status = "declined"; events(o)
                try await Frames.write(ch, ["type": "DECLINE", "reason": "declined"])
                return
            }
            saveJournal(id, ["fingerprint": o.fingerprint, "accepted": true, "verified": 0, "name": o.name, "size": size])
        }

        let part = partURL(id)
        if !FileManager.default.fileExists(atPath: part.path) { FileManager.default.createFile(atPath: part.path, contents: nil) }
        let fh = try FileHandle(forUpdating: part)
        defer { try? fh.close() }
        try fh.truncate(atOffset: UInt64(offset))
        var full = SHA256()
        try fh.seek(toOffset: 0)
        var left = offset
        while left > 0 { // re-hash the verified prefix so the whole-file check still covers it
            guard let d = try fh.read(upToCount: Int(min(left, 4 << 20))), !d.isEmpty else { break }
            full.update(data: d); left -= Int64(d.count)
        }
        try fh.seek(toOffset: UInt64(offset))
        let tAccept = Date()
        try await Frames.write(ch, ["type": "ACCEPT", "offset": offset, "hashes": Frames.chunkHashes])
        o.status = "receiving"; o.received = offset; events(o)
        var tFirst: Date?
        var expected = offset, unsynced: Int64 = 0, lastEvent = Date.distantPast
        func checkpoint(_ v: Int64) {
            try? fh.synchronize()
            var j = loadJournal(id) ?? [:]
            j["verified"] = v
            saveJournal(id, j)
        }
        while true {
            let f: Frames.Frame
            do { f = try await Frames.read(ch) } catch { checkpoint(expected); o.status = "interrupted"; events(o); throw error }
            switch f.type {
            case "DATA":
                guard f.header.int64("offset") == expected, f.header.str("s256") == Frames.s256(f.payload) else {
                    checkpoint(expected)
                    try await Frames.write(ch, ["type": "ERROR", "reason": "chunk_mismatch", "resume_offset": expected])
                    return
                }
                if tFirst == nil { tFirst = Date() }
                try fh.write(contentsOf: f.payload)
                full.update(data: f.payload)
                expected += Int64(f.payload.count); unsynced += Int64(f.payload.count)
                if unsynced >= 8 << 20 { checkpoint(expected); unsynced = 0 }
                o.received = expected
                if Date().timeIntervalSince(lastEvent) > 0.15 { lastEvent = Date(); events(o) }
            case "END":
                let digest = full.finalize().map { String(format: "%02x", $0) }.joined()
                guard expected == size, digest == f.header.str("sha256") else {
                    try? FileManager.default.removeItem(at: part); try? FileManager.default.removeItem(at: journalURL(id))
                    o.status = "failed"; events(o)
                    try await Frames.write(ch, ["type": "ERROR", "reason": "hash_mismatch", "resume_offset": 0])
                    return
                }
                try fh.synchronize()
                try fh.close()
                let dest = Receiver.unique(inbox, o.name)
                try FileManager.default.moveItem(at: part, to: dest)
                try? FileManager.default.removeItem(at: journalURL(id))
                let tSaved = Date(), first = tFirst ?? tSaved
                o.status = "saved"; o.savedURL = dest; events(o)
                try await Frames.write(ch, [
                    "type": "SAVED", "name": dest.lastPathComponent, "sha256": digest,
                    "accept_to_first_byte_ms": first.timeIntervalSince(tAccept) * 1000, "first_byte_to_saved_ms": tSaved.timeIntervalSince(first) * 1000,
                    "resumed_from": offset,
                ])
                return
            case "CANCEL":
                o.status = "cancelled"; events(o); return
            default:
                throw WireError.unexpected(f.type ?? "frame")
            }
        }
    }

    public static func safeName(_ name: String) -> String {
        var base = name.replacingOccurrences(of: "\\", with: "/").split(separator: "/").last.map(String.init) ?? "file"
        base = base.trimmingCharacters(in: .whitespaces)
        while base.hasPrefix(".") { base.removeFirst() }
        if base.isEmpty { base = "file" }
        return String(base.filter { !"<>:\"|?*\u{0}".contains($0) }.prefix(200))
    }

    public static func unique(_ dir: URL, _ name: String) -> URL {
        var u = dir.appendingPathComponent(name)
        let stem = (name as NSString).deletingPathExtension, ext = (name as NSString).pathExtension
        var i = 1
        while FileManager.default.fileExists(atPath: u.path) {
            u = dir.appendingPathComponent(ext.isEmpty ? "\(stem) (\(i))" : "\(stem) (\(i)).\(ext)"); i += 1
        }
        return u
    }
}

/// TCP listener for the LAN, advertised over Bonjour as _redtooth._tcp.
public final class LANListener: @unchecked Sendable {
    private var listener: NWListener?
    public private(set) var port: Int = 0

    public init() {}

    public func start(receiver: Receiver, port: Int = 47800, bonjourName: String? = nil) throws {
        let params = NWParameters.tcp
        params.allowLocalEndpointReuse = true
        let l: NWListener
        do { l = try NWListener(using: params, on: NWEndpoint.Port(integerLiteral: UInt16(port))) } catch { l = try NWListener(using: params) }
        if let bonjourName { l.service = NWListener.Service(name: bonjourName, type: "_redtooth._tcp") }
        let ready = DispatchSemaphore(value: 0)
        l.stateUpdateHandler = { state in if case .ready = state { ready.signal() }; if case .failed = state { ready.signal() } }
        l.newConnectionHandler = { conn in
            let ch = NWByteChannel(conn)
            Task { try? await ch.start(); await receiver.handle(ch) }
        }
        l.start(queue: DispatchQueue(label: "redtooth.listener"))
        _ = ready.wait(timeout: .now() + 5)
        self.port = Int(l.port?.rawValue ?? 0)
        listener = l
    }

    public func stop() { listener?.cancel() }
}

// MARK: - Sending

/// Something to send: a file URL (security-scoped when it came from a folder grant or the share sheet).
public struct FileSource: Sendable {
    public var url: URL
    public var name: String { url.lastPathComponent }
    public init(url: URL) { self.url = url }
    var size: Int64 {
        let v = try? url.resourceValues(forKeys: [.fileSizeKey])
        return Int64(v?.fileSize ?? 0)
    }
    var modified: Double {
        let v = try? url.resourceValues(forKeys: [.contentModificationDateKey])
        return v?.contentModificationDate?.timeIntervalSince1970 ?? 0
    }
}

public final class Transfer: @unchecked Sendable, Identifiable {
    public let id: String
    public let name: String
    public let size: Int64
    public let recipientFirst: String
    public let device: Device
    public internal(set) var status = "preparing"
    public internal(set) var sent: Int64 = 0
    public internal(set) var rateBps: Double = 0
    public internal(set) var route: Endpoint?
    public internal(set) var error = ""
    public internal(set) var resumes = 0
    public internal(set) var timings: [String: Double] = [:]
    var cancelled = false

    init(id: String, name: String, size: Int64, recipientFirst: String, device: Device) {
        self.id = id; self.name = name; self.size = size; self.recipientFirst = recipientFirst; self.device = device
    }

    public var progress: Double { status == "saved" ? 1 : (size > 0 ? Double(sent) / Double(size) : 0) }

    public var label: String {
        switch status {
        case "sending": return size > 0 ? "Sending · \(Int(100 * sent / size))%" : "Sending"
        case "saved": return "Saved to \(possessive(recipientFirst)) \(device.label)"
        case "waiting": return "Waiting for \(recipientFirst)"
        case "preparing": return "Checking route"
        case "interrupted": return "Interrupted · resuming"
        case "verifying": return "Verifying"
        case "declined": return "Declined"
        case "failed": return "Couldn't send"
        case "cancelled": return "Cancelled"
        default: return status
        }
    }
}

public enum Routes {
    static let preference = ["wifi_aware": 0, "lan": 1, "loopback": 1]

    /// PING a device; round-trip ms if it answers with the expected id.
    public static func ping(_ ep: Endpoint, deviceId: String) async -> Double? {
        let t0 = Date()
        guard let ch = try? await NWByteChannel.connect(host: ep.host, port: ep.port, timeout: 1.5) else { return nil }
        defer { ch.close() }
        guard (try? await Frames.write(ch, ["type": "PING"])) != nil,
              let f = try? await withTimeout(1.5, { try await Frames.read(ch) }),
              f.type == "PONG", f.header.str("device_id") == deviceId else { return nil }
        return Date().timeIntervalSince(t0) * 1000
    }

    /// The fastest LAN endpoint that answers as this device.
    public static func best(_ d: Device) async -> Endpoint? {
        let lan = d.endpoints.filter { $0.kind != "wifi_aware" }
        let results = await withTaskGroup(of: (Endpoint, Double?).self) { g in
            for ep in lan { g.addTask { (ep, await ping(ep, deviceId: d.id)) } }
            var out: [(Endpoint, Double)] = []
            for await (ep, rtt) in g { if let rtt { out.append((ep, rtt)) } }
            return out
        }
        return results.sorted { (preference[$0.0.kind] ?? 5, $0.1) < (preference[$1.0.kind] ?? 5, $1.1) }.first?.0
    }
}

public final class Sender: @unchecked Sendable {
    public let meName: String
    public let mePhone: String
    public var onUpdate: @Sendable (Transfer) -> Void
    /// How to reach a device; the Agent installs its pre-checked routes.
    public var route: @Sendable (Device) async -> Endpoint? = { await Routes.best($0) }
    /// Open a channel to an endpoint; the app adds Wi-Fi Aware.
    public var connect: @Sendable (Endpoint) async throws -> ByteChannel = { try await NWByteChannel.connect(host: $0.host, port: $0.port) }
    public private(set) var transfers: [String: Transfer] = [:]
    private var tasks: [String: Task<Void, Never>] = [:]
    private let lock = NSLock()
    public var maxAttempts = 8

    public init(meName: String, mePhone: String, onUpdate: @escaping @Sendable (Transfer) -> Void = { _ in }) {
        self.meName = meName; self.mePhone = mePhone; self.onUpdate = onUpdate
    }

    public func transfer(_ id: String) -> Transfer? { lock.lock(); defer { lock.unlock() }; return transfers[id] }

    @discardableResult
    public func start(_ src: FileSource, recipientFirst: String, device: Device) -> Transfer {
        let t = Transfer(id: "t_" + UUID().uuidString.replacingOccurrences(of: "-", with: "").prefix(12).lowercased(), name: src.name, size: src.size,
                         recipientFirst: recipientFirst, device: device)
        let task = Task { await self.run(t, src) }
        lock.lock(); transfers[t.id] = t; tasks[t.id] = task; lock.unlock()
        return t
    }

    public func wait(_ id: String) async { lock.lock(); let task = tasks[id]; lock.unlock(); await task?.value }

    public func cancel(_ id: String) {
        guard let t = transfer(id), !["saved", "declined", "failed", "cancelled"].contains(t.status) else { return }
        t.cancelled = true; t.status = "cancelled"; onUpdate(t)
        lock.lock(); tasks[id]?.cancel(); lock.unlock()
    }

    private func set(_ t: Transfer, _ s: String) { t.status = s; onUpdate(t) }

    private func run(_ t: Transfer, _ src: FileSource) async {
        let t0 = Date()
        let fullHash = Task.detached { () -> String in // hashed while we wait for acceptance
            var h = SHA256()
            guard let fh = try? FileHandle(forReadingFrom: src.url) else { return "" }
            defer { try? fh.close() }
            while let d = try? fh.read(upToCount: 4 << 20), !d.isEmpty { h.update(data: d) }
            return h.finalize().map { String(format: "%02x", $0) }.joined()
        }
        let fp = String(sha256Hex(Data("\(src.name):\(src.size):\(Int64(src.modified * 1e9))".utf8)).prefix(40))
        var delay = 0.3
        for _ in 0..<maxAttempts {
            if t.cancelled || Task.isCancelled { return }
            do {
                guard let ep = await route(t.device) else { throw WireError.refused("no route") }
                t.route = ep
                try await attempt(t, src, ep, fp, fullHash, t0)
                return
            } catch {
                if ["declined", "failed", "cancelled", "saved"].contains(t.status) { return }
                if t.sent > 0 { t.resumes += 1 }
                t.error = error.localizedDescription
                set(t, "interrupted")
                try? await Task.sleep(nanoseconds: UInt64(delay * 1e9))
                delay = min(delay * 2, 5)
            }
        }
        set(t, "failed")
    }

    private func attempt(_ t: Transfer, _ src: FileSource, _ ep: Endpoint, _ fp: String, _ fullHash: Task<String, Never>, _ t0: Date) async throws {
        let ch = try await connect(ep)
        defer { ch.close() }
        try await Frames.write(ch, ["type": "OFFER", "id": t.id, "name": t.name, "size": t.size, "mime": Sender.mime(t.name), "fingerprint": fp,
                                    "chunk": Frames.chunk, "from": ["name": meName, "phone": mePhone]])
        var accept: JSONObject = [:]
        loop: while true {
            let f = try await Frames.read(ch)
            switch f.type {
            case "WAITING": set(t, "waiting"); if t.timings["offered_ms"] == nil { t.timings["offered_ms"] = Date().timeIntervalSince(t0) * 1000 }
            case "DECLINE": set(t, "declined"); return
            case "ACCEPT": accept = f.header; break loop
            default: throw WireError.unexpected(f.type ?? "frame")
            }
        }
        guard ((accept.arr("hashes") as? [String]) ?? ["b2"]).contains("s256") else {
            t.error = "receiver can't verify SHA-256 chunks (update it)"; set(t, "failed"); return
        }
        let offset = accept.int64("offset") ?? 0
        let tAccept = Date()
        t.sent = offset
        set(t, "sending")
        let fh = try FileHandle(forReadingFrom: src.url)
        defer { try? fh.close() }
        try fh.seek(toOffset: UInt64(offset))
        var pos = offset, lastEvent = Date.distantPast
        while pos < t.size {
            if t.cancelled || Task.isCancelled { try? await Frames.write(ch, ["type": "CANCEL"]); return }
            guard let buf = try fh.read(upToCount: Frames.chunk), !buf.isEmpty else { break }
            try await Frames.write(ch, ["type": "DATA", "offset": pos, "s256": Frames.s256(buf)], payload: buf)
            pos += Int64(buf.count)
            t.sent = pos
            if Date().timeIntervalSince(lastEvent) > 0.12 {
                lastEvent = Date(); t.rateBps = Double(pos - offset) / max(1e-9, Date().timeIntervalSince(tAccept)); onUpdate(t)
            }
        }
        set(t, "verifying")
        try await Frames.write(ch, ["type": "END", "sha256": await fullHash.value])
        let r = try await withTimeout(120) { try await Frames.read(ch) }
        if r.type == "ERROR" { throw WireError.refused("receiver asked to resume from \(r.header.int64("resume_offset") ?? 0)") }
        guard r.type == "SAVED" else { throw WireError.unexpected("no save confirmation") }
        t.timings["accept_to_first_byte_ms"] = r.header.dbl("accept_to_first_byte_ms") ?? 0
        t.timings["first_byte_to_saved_ms"] = r.header.dbl("first_byte_to_saved_ms") ?? 0
        t.timings["transfer_total_ms"] = Date().timeIntervalSince(t0) * 1000
        t.sent = t.size
        set(t, "saved")
    }

    static func mime(_ name: String) -> String {
        switch (name as NSString).pathExtension.lowercased() {
        case "pdf": "application/pdf"
        case "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation"
        case "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        case "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        case "png": "image/png"
        case "jpg", "jpeg": "image/jpeg"
        case "heic": "image/heic"
        case "mp4": "video/mp4"
        case "mov": "video/quicktime"
        case "txt", "md": "text/plain"
        default: "application/octet-stream"
        }
    }
}
