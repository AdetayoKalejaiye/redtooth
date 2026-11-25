import Foundation

/// Phone-number profile directory (server: redtooth/directory.py).
public protocol Directory: Sendable {
    func setDevices(phone: String, devices: [Device]) async throws
    func lookup(phones: [String]) async throws -> [String: Profile]
    func nudge(toPhone: String, payload: JSONObject) async throws -> Bool
    func inbox() async throws -> [JSONObject]
}

public final class HTTPDirectory: Directory, @unchecked Sendable {
    public let base: String
    public var token: String?
    private let session: URLSession

    public init(base: String, token: String? = nil) {
        self.base = base.hasSuffix("/") ? String(base.dropLast()) : base
        self.token = token
        let cfg = URLSessionConfiguration.ephemeral
        cfg.timeoutIntervalForRequest = 5
        session = URLSession(configuration: cfg)
    }

    public struct HTTPError: Error { public let code: Int }

    private func req(_ method: String, _ path: String, _ body: JSONObject? = nil) async throws -> JSONObject {
        guard let url = URL(string: base + path) else { throw URLError(.badURL) }
        var r = URLRequest(url: url)
        r.httpMethod = method
        if let token { r.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        if let body {
            r.setValue("application/json", forHTTPHeaderField: "Content-Type")
            r.httpBody = try JSONSerialization.data(withJSONObject: body)
        }
        let (data, resp) = try await session.data(for: r)
        let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(code) else { throw HTTPError(code: code) }
        return (try JSONSerialization.jsonObject(with: data) as? JSONObject) ?? [:]
    }

    public func startVerification(phone: String) async throws { _ = try await req("POST", "/v1/verify/start", ["phone": phone]) }

    @discardableResult
    public func verify(phone: String, code: String, name: String) async throws -> String {
        let t = try await req("POST", "/v1/verify/finish", ["phone": phone, "code": code, "name": name]).str("token") ?? ""
        token = t
        return t
    }

    /// Only for directories started with --open-register (testing).
    @discardableResult
    public func register(phone: String, name: String) async throws -> String {
        let t = try await req("POST", "/v1/register", ["phone": phone, "name": name]).str("token") ?? ""
        token = t
        return t
    }

    public func setDevices(phone: String, devices: [Device]) async throws {
        _ = try await req("PUT", "/v1/me/devices", ["devices": devices.map(\.json)])
    }

    public func lookup(phones: [String]) async throws -> [String: Profile] {
        var byHash: [String: String] = [:]
        for p in phones { byHash[Phones.hash(p)] = Phones.normalize(p) }
        let res = try await req("POST", "/v1/lookup", ["hashes": Array(byHash.keys)])
        var out: [String: Profile] = [:]
        for (h, p) in res.obj("profiles") ?? [:] { if let phone = byHash[h], let j = p as? JSONObject { out[phone] = Profile(j) } }
        return out
    }

    public func nudge(toPhone: String, payload: JSONObject) async throws -> Bool {
        try await req("POST", "/v1/nudge", ["to": Phones.hash(toPhone), "payload": payload]).bool("ok") ?? false
    }

    public func inbox() async throws -> [JSONObject] {
        (try await req("GET", "/v1/me/inbox").arr("messages") ?? []).compactMap { $0 as? JSONObject }
    }
}

/// In-memory directory for tests and previews.
public final class MemoryDirectory: Directory, @unchecked Sendable {
    public var profiles: [String: Profile] = [:]
    public var inboxes: [String: [JSONObject]] = [:]
    public init() {}
    public func register(phone: String, name: String) { profiles[Phones.hash(phone)] = Profile(phone: Phones.normalize(phone), name: name, devices: profiles[Phones.hash(phone)]?.devices ?? []) }
    public func setDevices(phone: String, devices: [Device]) async throws {
        var p = profiles[Phones.hash(phone)] ?? Profile(phone: Phones.normalize(phone), name: phone)
        p.devices = devices.map { var d = $0; d.ownerPhone = p.phone; d.ownerName = p.name; return d }
        profiles[Phones.hash(phone)] = p
    }
    public func lookup(phones: [String]) async throws -> [String: Profile] {
        var out: [String: Profile] = [:]
        for ph in phones { if let p = profiles[Phones.hash(ph)] { out[Phones.normalize(ph)] = p } }
        return out
    }
    public func nudge(toPhone: String, payload: JSONObject) async throws -> Bool {
        guard profiles[Phones.hash(toPhone)] != nil else { return false }
        inboxes[Phones.hash(toPhone), default: []].append(payload)
        return true
    }
    public func inbox() async throws -> [JSONObject] { [] }
}
