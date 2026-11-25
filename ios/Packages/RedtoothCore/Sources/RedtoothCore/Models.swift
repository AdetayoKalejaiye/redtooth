import CryptoKit
import Foundation

public let audioKinds: Set<String> = ["headphones", "earbuds", "speaker"]
public let appKinds: Set<String> = ["phone", "laptop", "desktop", "tablet"]

public typealias JSONObject = [String: Any]

extension Dictionary where Key == String, Value == Any {
    func str(_ k: String) -> String? { self[k] as? String }
    func int(_ k: String) -> Int? { (self[k] as? NSNumber)?.intValue }
    func int64(_ k: String) -> Int64? { (self[k] as? NSNumber)?.int64Value }
    func dbl(_ k: String) -> Double? { (self[k] as? NSNumber)?.doubleValue }
    func bool(_ k: String) -> Bool? { self[k] as? Bool }
    func obj(_ k: String) -> JSONObject? { self[k] as? JSONObject }
    func arr(_ k: String) -> [Any]? { self[k] as? [Any] }
}

public struct Endpoint: Hashable, Sendable {
    public var kind: String // lan | wifi_aware | loopback
    public var host: String
    public var port: Int
    public init(kind: String, host: String, port: Int) { self.kind = kind; self.host = host; self.port = port }
    public var json: JSONObject { ["kind": kind, "host": host, "port": port] }
    public init(_ j: JSONObject) { self.init(kind: j.str("kind") ?? "lan", host: j.str("host") ?? "", port: j.int("port") ?? 0) }
}

/// Same fields as the desktop's Device, so profiles round-trip through the directory.
public struct Device: Hashable, Sendable {
    public var id: String
    public var name: String
    public var kind: String = "other"
    public var address: String?
    public var model: String?
    public var shortModel: String?
    public var ownerPhone: String?
    public var ownerName: String?
    public var hasApp = false
    public var endpoints: [Endpoint] = []
    public var wifiAware = false
    public var apnsToken: String?

    public init(id: String, name: String, kind: String = "other", address: String? = nil, model: String? = nil, shortModel: String? = nil,
                hasApp: Bool = false, endpoints: [Endpoint] = [], wifiAware: Bool = false) {
        self.id = id; self.name = name; self.kind = kind; self.address = address; self.model = model; self.shortModel = shortModel
        self.hasApp = hasApp; self.endpoints = endpoints; self.wifiAware = wifiAware
    }

    public var label: String { model ?? name }

    public var json: JSONObject {
        var j: JSONObject = ["id": id, "name": name, "kind": kind, "has_app": hasApp, "endpoints": endpoints.map(\.json),
                             "wifi_direct": wifiAware, "sources": [String](), "label": label]
        if let address { j["address"] = address }
        if let model { j["model"] = model }
        if let shortModel { j["short_model"] = shortModel }
        if let ownerPhone { j["owner_phone"] = ownerPhone }
        if let ownerName { j["owner_name"] = ownerName }
        if let apnsToken { j["apns_token"] = apnsToken }
        return j
    }

    public init(_ j: JSONObject) {
        self.init(id: j.str("id") ?? "", name: j.str("name") ?? "", kind: j.str("kind") ?? "other", address: j.str("address"),
                  model: j.str("model"), shortModel: j.str("short_model"), hasApp: j.bool("has_app") ?? false,
                  endpoints: (j.arr("endpoints") ?? []).compactMap { ($0 as? JSONObject).map(Endpoint.init) }, wifiAware: j.bool("wifi_direct") ?? false)
        ownerPhone = j.str("owner_phone"); ownerName = j.str("owner_name"); apnsToken = j.str("apns_token")
    }
}

public struct Profile: Sendable {
    public var phone: String
    public var name: String
    public var devices: [Device]
    public init(phone: String, name: String, devices: [Device] = []) { self.phone = phone; self.name = name; self.devices = devices }
    public init(_ j: JSONObject) {
        self.init(phone: j.str("phone") ?? "", name: j.str("name") ?? "", devices: (j.arr("devices") ?? []).compactMap { ($0 as? JSONObject).map(Device.init) })
    }
}

public struct Contact: Hashable, Sendable {
    public var id: String
    public var name: String
    public var phones: [String]
    public var nicknames: [String]
    public var relation: String?
    public var lastInteraction: Double?
    public init(id: String, name: String, phones: [String] = [], nicknames: [String] = [], relation: String? = nil, lastInteraction: Double? = nil) {
        self.id = id; self.name = name; self.phones = phones; self.nicknames = nicknames; self.relation = relation; self.lastInteraction = lastInteraction
    }
}

/// A resolved person: a contact plus their Redtooth profile, if they have one.
public struct Person: Sendable {
    public var contact: Contact
    public var profile: Profile?
    public var nearby = false
    public var isSelf = false
    public init(contact: Contact, profile: Profile? = nil, nearby: Bool = false, isSelf: Bool = false) {
        self.contact = contact; self.profile = profile; self.nearby = nearby; self.isSelf = isSelf
    }
    public var name: String { contact.name }
    public var first: String { contact.name.split(whereSeparator: \.isWhitespace).first.map(String.init) ?? contact.name }
    public var phone: String? { profile?.phone ?? contact.phones.first }
    public var hasApp: Bool { profile != nil }
}

public struct FileHit: Hashable, Sendable {
    public var id: String
    public var path: String // file URL path or a bookmark-resolved URL string
    public var name: String
    public var kind: String
    public var ext: String
    public var size: Int64
    public var mtime: Double
    public var score: Double = 0
    public var snippet = ""
    public var snippetLabel = ""
    public init(id: String, path: String, name: String, kind: String, ext: String, size: Int64, mtime: Double, score: Double = 0) {
        self.id = id; self.path = path; self.name = name; self.kind = kind; self.ext = ext; self.size = size; self.mtime = mtime; self.score = score
    }
}

public struct Option: @unchecked Sendable {
    public var id: String
    public var label: String
    public var detail = ""
    public var payload: Any?
}

public struct Question: @unchecked Sendable {
    public var id: String
    public var kind: String
    public var prompt: String
    public var options: [Option]
    public var state: [String: Any] = [:]
}

/// What the agent wants the UI to do beyond showing text.
public enum ReplyAction: Sendable, Equatable {
    case none
    case showRoutePicker(hint: String) // iOS: the system output picker, with the device to choose
    case openBluetoothSettings
}

public struct Reply: @unchecked Sendable {
    public var text: String
    public var status: String?
    public var question: Question?
    public var files: [FileHit] = []
    public var person: Person?
    public var transferId: String?
    public var ok = true
    public var action: ReplyAction = .none
    public var timings: [String: Double] = [:]
    public init(_ text: String, status: String? = nil, question: Question? = nil, files: [FileHit] = [], person: Person? = nil,
                transferId: String? = nil, ok: Bool = true, action: ReplyAction = .none, timings: [String: Double] = [:]) {
        self.text = text; self.status = status; self.question = question; self.files = files; self.person = person
        self.transferId = transferId; self.ok = ok; self.action = action; self.timings = timings
    }
}

public enum Phones {
    /// E.164-style normalization, identical to the desktop's normalize_phone.
    public static func normalize(_ raw: String, defaultCC: String = "1") -> String {
        let trimmed = raw.trimmingCharacters(in: .whitespaces)
        if trimmed.isEmpty { return "" }
        let digits = trimmed.filter(\.isASCIIDigitChar)
        if trimmed.hasPrefix("+") { return "+" + digits }
        if digits.hasPrefix("00") { return "+" + digits.dropFirst(2) }
        if digits.count == 10 && defaultCC == "1" { return "+1" + digits }
        if digits.count == 11 && digits.hasPrefix("1") && defaultCC == "1" { return "+" + digits }
        return "+" + defaultCC + digits
    }

    /// Contact discovery sends these hashes, never raw numbers.
    public static func hash(_ phone: String) -> String { sha256Hex(Data(("redtooth:" + normalize(phone)).utf8)) }
}

extension Character {
    var isASCIIDigitChar: Bool { ("0"..."9").contains(self) }
}

public func sha256Hex(_ d: Data) -> String { SHA256.hash(data: d).map { String(format: "%02x", $0) }.joined() }

public func possessive(_ name: String) -> String { name.hasSuffix("s") ? name + "'" : name + "'s" }
