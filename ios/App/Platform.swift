import AVFoundation
import Contacts
import Foundation
import RedtoothCore
import UserNotifications

/// The current audio route: all iOS tells an app about Bluetooth audio.
final class SystemAudio: AudioPort, @unchecked Sendable {
    func currentOutput() -> (name: String, bluetooth: Bool) {
        let outs = AVAudioSession.sharedInstance().currentRoute.outputs
        guard let o = outs.first else { return ("iPhone", false) }
        let bt: Set<AVAudioSession.Port> = [.bluetoothA2DP, .bluetoothLE, .bluetoothHFP]
        return (o.portName, bt.contains(o.portType))
    }
}

/// “Daniel's headphones” → the output name that showed up when they were picked.
final class DefaultsAudioMemory: AudioMemory, @unchecked Sendable {
    func remember(ownerPhone: String?, ownerName: String, outputName: String) {
        var m = Shared.defaults.dictionary(forKey: "audio_memory") as? [String: String] ?? [:]
        m[ownerPhone ?? ownerName] = outputName
        Shared.defaults.set(m, forKey: "audio_memory")
    }
    func lookup(ownerPhone: String?) -> String? {
        guard let ownerPhone else { return nil }
        return (Shared.defaults.dictionary(forKey: "audio_memory") as? [String: String])?[ownerPhone]
    }
}

enum ContactsLoader {
    static func load() async -> [Contact] {
        let store = CNContactStore()
        guard (try? await store.requestAccess(for: .contacts)) == true else { return [] }
        let keys: [CNKeyDescriptor] = [CNContactGivenNameKey, CNContactFamilyNameKey, CNContactNicknameKey, CNContactPhoneNumbersKey, CNContactRelationsKey] as [CNKeyDescriptor]
        var out: [Contact] = []
        let req = CNContactFetchRequest(keysToFetch: keys)
        req.sortOrder = .userDefault
        try? store.enumerateContacts(with: req) { c, _ in
            let name = [c.givenName, c.familyName].filter { !$0.isEmpty }.joined(separator: " ")
            guard !name.isEmpty, !c.phoneNumbers.isEmpty else { return }
            let relation = c.contactRelations.first?.label.map { CNLabeledValue<CNContactRelation>.localizedString(forLabel: $0).lowercased() }
            out.append(Contact(id: c.identifier, name: name, phones: c.phoneNumbers.map { Phones.normalize($0.value.stringValue) },
                               nicknames: c.nickname.isEmpty ? [] : [c.nickname], relation: relation))
        }
        return out
    }
}

// MARK: - Notifications: accept from the lock screen

enum Notify {
    static let offerCategory = "REDTOOTH_OFFER"
    static let pairingCategory = "REDTOOTH_PAIRING"

    static func setUp(delegate: UNUserNotificationCenterDelegate) {
        let c = UNUserNotificationCenter.current()
        c.delegate = delegate
        let accept = UNNotificationAction(identifier: "accept", title: "Accept", options: [])
        let decline = UNNotificationAction(identifier: "decline", title: "Decline", options: [.destructive])
        c.setNotificationCategories([
            UNNotificationCategory(identifier: offerCategory, actions: [accept, decline], intentIdentifiers: []),
            UNNotificationCategory(identifier: pairingCategory, actions: [], intentIdentifiers: []),
        ])
        c.requestAuthorization(options: [.alert, .sound, .badge]) { _, _ in }
    }

    static func offer(_ o: IncomingOffer) {
        let content = UNMutableNotificationContent()
        content.title = "\(o.senderName) wants to send you a file"
        content.body = "\(o.name) · \(ByteCountFormatter.string(fromByteCount: o.size, countStyle: .file))"
        content.categoryIdentifier = offerCategory
        content.userInfo = ["offer": o.id]
        content.interruptionLevel = .timeSensitive
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: o.id, content: content, trigger: nil))
    }

    static func saved(_ o: IncomingOffer) {
        let content = UNMutableNotificationContent()
        content.title = "Saved \(o.name)"
        content.body = "From \(o.senderName). Open Files › Redtooth."
        UNUserNotificationCenter.current().removeDeliveredNotifications(withIdentifiers: [o.id])
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: o.id + ".saved", content: content, trigger: nil))
    }

    static func nudge(_ n: [String: Any]) {
        let content = UNMutableNotificationContent()
        content.title = (n["from"] as? String).map { "\($0) wants to connect" } ?? "Redtooth"
        content.body = n["text"] as? String ?? "Put your headphones in pairing mode."
        content.categoryIdentifier = pairingCategory
        UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil))
    }
}
