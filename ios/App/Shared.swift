import Foundation
import RedtoothCore

/// Settings shared with the share extension and the Control Center control (App Group).
enum Shared {
    static let group = "group.app.redtooth"
    static let defaults = UserDefaults(suiteName: group) ?? .standard
    static var container: URL { FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: group) ?? FileManager.default.temporaryDirectory }

    static var directoryURL: String {
        get { defaults.string(forKey: "directory") ?? "http://localhost:8790" }
        set { defaults.set(newValue, forKey: "directory") }
    }
    static var token: String? { get { defaults.string(forKey: "token") } set { defaults.set(newValue, forKey: "token") } }
    static var myPhone: String? { get { defaults.string(forKey: "phone") } set { defaults.set(newValue, forKey: "phone") } }
    static var myName: String? { get { defaults.string(forKey: "name") } set { defaults.set(newValue, forKey: "name") } }
    /// Country for numbers saved without a country code: chosen at sign-up, defaulting to the region setting.
    static var country: String {
        get { defaults.string(forKey: "country") ?? detectedCountry }
        set { defaults.set(newValue, forKey: "country"); Phones.country = newValue }
    }
    /// Bluetooth outputs you've marked as yours (iOS can't list paired devices, so you mark the one playing now).
    struct MyDevice: Codable, Hashable { var name: String; var kind: String; var alias: String? }
    static var myDevices: [MyDevice] {
        get { (try? JSONDecoder().decode([MyDevice].self, from: defaults.data(forKey: "my_devices") ?? Data())) ?? [] }
        set { defaults.set(try? JSONEncoder().encode(newValue), forKey: "my_devices") }
    }

    static var detectedCountry: String {
        let r = Locale.current.region?.identifier.uppercased() ?? ""
        return Phones.byISO[r] != nil ? r : Phones.defaultCountry
    }
    static var deviceId: String {
        if let id = defaults.string(forKey: "device_id") { return id }
        let id = "ios_" + UUID().uuidString.prefix(8).lowercased()
        defaults.set(id, forKey: "device_id")
        return id
    }

    /// The people list the share extension uses (it can't read Contacts quickly or call the directory on every share).
    static func savePeople(_ people: [Person]) {
        let rows: [[String: Any]] = people.compactMap { p in
            guard let prof = p.profile else { return nil }
            return ["id": p.contact.id, "name": p.name, "phone": prof.phone, "devices": prof.devices.map(\.json)]
        }
        if let d = try? JSONSerialization.data(withJSONObject: rows) { try? d.write(to: container.appendingPathComponent("people.json")) }
    }

    static func loadPeople() -> [(id: String, name: String, phone: String, devices: [Device])] {
        guard let d = try? Data(contentsOf: container.appendingPathComponent("people.json")),
              let rows = try? JSONSerialization.jsonObject(with: d) as? [[String: Any]] else { return [] }
        return rows.compactMap { r in
            guard let id = r["id"] as? String, let name = r["name"] as? String, let phone = r["phone"] as? String else { return nil }
            return (id, name, phone, ((r["devices"] as? [[String: Any]]) ?? []).map { Device($0) })
        }
    }
}
