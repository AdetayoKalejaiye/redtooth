import AVFoundation
import Foundation
import Observation
import RedtoothCore
import SwiftUI
import UserNotifications

/// One line in the panel's feed.
struct FeedItem: Identifiable {
    enum Who { case user, agent }
    let id = UUID()
    let who: Who
    var text: String
    var reply: Reply?
}

@MainActor
@Observable
final class AppModel: NSObject, UNUserNotificationCenterDelegate {
    static let shared = AppModel()

    var feed: [FeedItem] = []
    var busy = false
    var transfers: [String: Transfer] = [:]
    var incoming: [IncomingOffer] = []
    var routeHint: String?           // set → the panel shows the route picker with this name
    var onboarded = Shared.myPhone != nil && Shared.token != nil
    var lastMetrics: [String: Double] = [:]

    let files = FileIndex()
    private(set) var agent: Agent?
    private(set) var directory: HTTPDirectory
    private var receiver: Receiver?
    private let lan = LANListener()
    private var pendingAccepts: [String: CheckedContinuation<Bool, Never>] = [:]
    #if canImport(WiFiAware)
    private var aware: AnyObject?
    #endif

    override init() {
        directory = HTTPDirectory(base: Shared.directoryURL, token: Shared.token)
        super.init()
    }

    // MARK: Start-up

    private var started = false

    func start() async {
        guard !started else { return }
        Phones.country = Shared.country // before contacts are read
        Notify.setUp(delegate: self)
        NotificationCenter.default.addObserver(forName: AVAudioSession.routeChangeNotification, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in self?.agent?.noteRouteChanged() }
        }
        guard onboarded, let phone = Shared.myPhone else { return }
        started = true
        let contacts = await ContactsLoader.load()
        let me = Person(contact: Contact(id: "me", name: Shared.myName ?? "Me", phones: [phone]), isSelf: true)
        let people = PeopleResolver(contacts: contacts, me: me)
        let sender = Sender(meName: me.name, mePhone: phone) { t in Task { @MainActor in AppModel.shared.transfers[t.id] = t } }
        sender.connect = { ep in
            if ep.kind == "wifi_aware", let ch = await DirectLink.connect(Device(id: ep.host, name: ep.host, wifiAware: true)) { return ch }
            return try await NWByteChannel.connect(host: ep.host, port: ep.port)
        }
        let a = Agent(me: me, people: people, directory: directory, files: files, audio: SystemAudio(), audioMemory: DefaultsAudioMemory(), sender: sender)
        a.usage = UsageStore(url: Shared.container.appendingPathComponent("usage.json"))
        a.thisDeviceId = Shared.deviceId
        files.usage = a.usage
        a.routeFallback = { d in d.wifiAware && DirectLink.available ? Endpoint(kind: "wifi_aware", host: d.name, port: 0) : nil }
        agent = a
        startReceiving()
        await a.refreshPeople()
        Shared.savePeople(people.allPeople())
        try? await registerThisDevice()
        Task.detached(priority: .utility) { await self.files.refresh() }
        await checkInbox()
    }

    func registerThisDevice() async throws {
        guard let phone = Shared.myPhone else { return }
        let ip = Self.localIPv4()
        var d = Device(id: Shared.deviceId, name: UIDevice.current.name, kind: "phone", model: "iPhone", hasApp: true,
                       endpoints: ip.map { [Endpoint(kind: "lan", host: $0, port: lan.port)] } ?? [], wifiAware: DirectLink.available)
        d.apnsToken = Shared.defaults.string(forKey: "apns")
        // Your devices: what contacts can ask for by name ("connect to Adetayo's speaker").
        let mine = Shared.myDevices.map { m -> Device in
            var dev = Device(id: "out:" + m.name.lowercased(), name: m.name, kind: m.kind)
            dev.alias = m.alias
            return dev
        }
        try await directory.setDevices(phone: phone, devices: [d] + mine)
    }

    private func startReceiving() {
        let inbox = FileManager.default.urls(for: .documentDirectory, in: .userDomainMask)[0].appendingPathComponent("Redtooth")
        let rx = Receiver(deviceId: Shared.deviceId, deviceName: UIDevice.current.name, inbox: inbox, acceptor: { offer in
            await AppModel.shared.askToAccept(offer)
        }, events: { offer in
            Task { @MainActor in AppModel.shared.offerChanged(offer) }
        })
        receiver = rx
        try? lan.start(receiver: rx, port: 47800, bonjourName: UIDevice.current.name)
        #if canImport(WiFiAware)
        if #available(iOS 26.0, *) { let l = WifiAwareLink(); l.startListening(receiver: rx); aware = l }
        #endif
    }

    /// Restart listeners after returning to the foreground (iOS tears them down in the background).
    func foregrounded() {
        guard let rx = receiver else { return }
        lan.stop()
        try? lan.start(receiver: rx, port: 47800, bonjourName: UIDevice.current.name)
        Task { try? await registerThisDevice(); await checkInbox(); await files.refresh() }
    }

    // MARK: Accepting

    func askToAccept(_ offer: IncomingOffer) async -> Bool {
        incoming.removeAll { $0.id == offer.id }
        incoming.insert(offer, at: 0)
        Notify.offer(offer)
        return await withCheckedContinuation { pendingAccepts[offer.id] = $0 }
    }

    func decide(_ offerId: String, accept: Bool) {
        pendingAccepts.removeValue(forKey: offerId)?.resume(returning: accept)
        UNUserNotificationCenter.current().removeDeliveredNotifications(withIdentifiers: [offerId])
    }

    func offerChanged(_ o: IncomingOffer) {
        if let i = incoming.firstIndex(where: { $0.id == o.id }) { incoming[i] = o }
        if o.status == "saved" { Notify.saved(o) }
    }

    nonisolated func userNotificationCenter(_ c: UNUserNotificationCenter, didReceive r: UNNotificationResponse) async {
        guard let id = r.notification.request.content.userInfo["offer"] as? String else { return }
        let accept = r.actionIdentifier == "accept" || r.actionIdentifier == UNNotificationDefaultActionIdentifier
        await MainActor.run { AppModel.shared.decide(id, accept: accept) }
    }

    nonisolated func userNotificationCenter(_ c: UNUserNotificationCenter, willPresent n: UNNotification) async -> UNNotificationPresentationOptions {
        [.banner, .sound]
    }

    func checkInbox() async {
        for n in (try? await directory.inbox()) ?? [] where n.str("type") == "pairing_mode" { Notify.nudge(n) }
    }

    // MARK: Asking

    func ask(_ text: String) async {
        let t = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !t.isEmpty, let agent else { return }
        feed.append(FeedItem(who: .user, text: t))
        busy = true
        let r = await agent.ask(t)
        busy = false
        show(r)
    }

    func choose(_ q: Question, _ o: Option) async {
        guard let agent else { return }
        feed.append(FeedItem(who: .user, text: o.label))
        show(await agent.answer(questionId: q.id, optionId: o.id))
    }

    func send(_ hit: FileHit) { guard let agent else { return }; show(agent.selectFile(hit)) }

    private func show(_ r: Reply) {
        feed.append(FeedItem(who: .agent, text: r.text, reply: r))
        if case let .showRoutePicker(hint) = r.action { routeHint = hint }
        lastMetrics.merge(r.timings) { $1 }
        if let id = r.transferId, let t = agent?.sender.transfer(id) { transfers[id] = t }
    }

    // MARK: Your devices

    var myDevices: [Shared.MyDevice] = Shared.myDevices
    var openDevicesRequested = false
    var currentOutput: (name: String, bluetooth: Bool) { SystemAudio().currentOutput() }

    /// iOS doesn't let apps list paired Bluetooth devices, so you mark the one that's playing now as yours.
    func markCurrentOutputMine(kind: String, alias: String?) {
        let out = currentOutput
        guard out.bluetooth else { return }
        var list = Shared.myDevices.filter { $0.name != out.name }
        list.append(.init(name: out.name, kind: kind, alias: alias?.isEmpty == false ? alias : nil))
        Shared.myDevices = list; myDevices = list
        Task { try? await registerThisDevice() }
    }

    func updateMine(_ d: Shared.MyDevice, alias: String?, remove: Bool = false) {
        var list = Shared.myDevices.filter { $0.name != d.name }
        if !remove { list.append(.init(name: d.name, kind: d.kind, alias: alias?.isEmpty == false ? alias : nil)) }
        Shared.myDevices = list; myDevices = list
        Task { try? await registerThisDevice() }
    }

    // MARK: Onboarding

    func startVerification(phone: String, country: String) async throws {
        Shared.country = country
        try await directory.startVerification(phone: Phones.normalize(phone))
    }

    func finishVerification(phone: String, code: String, name: String) async throws {
        let p = Phones.normalize(phone)
        let token = try await directory.verify(phone: p, code: code, name: name)
        Shared.token = token; Shared.myPhone = p; Shared.myName = name
        onboarded = true
        await start()
    }

    func setDirectory(_ url: String) {
        Shared.directoryURL = url
        directory = HTTPDirectory(base: url, token: Shared.token)
    }

    static func localIPv4() -> String? {
        var addrs: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&addrs) == 0, let first = addrs else { return nil }
        defer { freeifaddrs(addrs) }
        for p in sequence(first: first, next: { $0.pointee.ifa_next }) {
            let ifa = p.pointee
            guard ifa.ifa_addr.pointee.sa_family == UInt8(AF_INET), String(cString: ifa.ifa_name) == "en0" else { continue }
            var host = [CChar](repeating: 0, count: Int(NI_MAXHOST))
            getnameinfo(ifa.ifa_addr, socklen_t(ifa.ifa_addr.pointee.sa_len), &host, socklen_t(host.count), nil, 0, NI_NUMERICHOST)
            return String(cString: host)
        }
        return nil
    }
}
