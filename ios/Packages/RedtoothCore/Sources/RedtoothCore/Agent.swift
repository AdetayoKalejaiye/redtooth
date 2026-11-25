import Foundation

/// File search the app provides (folder grants indexed with SQLite FTS5).
public protocol FileSearch: Sendable {
    func search(_ q: FileQuery) -> [FileHit]
    func recent(_ n: Int) -> [FileHit]
    func source(_ hit: FileHit) -> FileSource?
    func indexing() -> (done: Int, queued: Int, complete: Bool)?
}

/// What iOS lets an app know about audio output: the current route only.
public protocol AudioPort: Sendable {
    func currentOutput() -> (name: String, bluetooth: Bool)
}

/// "Daniel's headphones" -> the output name that appeared when they were picked last time.
public protocol AudioMemory: Sendable {
    func remember(ownerPhone: String?, ownerName: String, outputName: String)
    func lookup(ownerPhone: String?) -> String?
}

public final class AgentContext: @unchecked Sendable {
    public var person: Person?
    public var file: FileHit?
    public var files: [FileHit] = []
    public var device: Device?
    public var pending: Question?
    public var lastTransferId: String?
    /// Who we were trying to reach when the user opened the route picker (to remember the choice).
    public var audioOwner: Person?
    var updated = Date()
    func touch() { updated = Date() }
    var stale: Bool { Date().timeIntervalSince(updated) > 1800 }
}

/// The agent on iPhone (port of agent.py). File flows match the desktop and Android exactly.
/// Headphones differ: iOS doesn't let apps pair or switch Bluetooth audio, so the agent works out
/// which device is meant, nudges its owner if it isn't paired yet, and asks the UI to show the
/// system route picker with that device named.
public final class Agent: @unchecked Sendable {
    public let me: Person
    public let people: PeopleResolver
    public let directory: Directory
    public let files: FileSearch
    public let audio: AudioPort
    public let audioMemory: AudioMemory
    public let sender: Sender
    public var ctx = AgentContext()
    private var routeTasks: [String: Task<Endpoint?, Never>] = [:]
    private let lock = NSLock()
    private var forcedPerson: Person?
    /// What you open, send and pick (feeds ranking; answers to "which file?" are remembered).
    public var usage: UsageStore?
    /// This iPhone's Redtooth id, so "send it to my phone" means another device.
    public var thisDeviceId: String?
    /// Extra way to reach a device when no LAN endpoint answers (the app sets Wi-Fi Aware here).
    public var routeFallback: (@Sendable (Device) async -> Endpoint?)?

    static let destOrder = ["phone": 0, "laptop": 1, "tablet": 2, "desktop": 3]
    static let kindNoun = ["presentation": "deck", "document": "document", "spreadsheet": "spreadsheet", "pdf": "PDF", "image": "photo", "video": "video"]

    public init(me: Person, people: PeopleResolver, directory: Directory, files: FileSearch, audio: AudioPort, audioMemory: AudioMemory, sender: Sender) {
        var self_ = me; self_.isSelf = true
        self.me = self_; self.people = people; self.directory = directory; self.files = files
        self.audio = audio; self.audioMemory = audioMemory; self.sender = sender
        sender.route = { [weak self] d in await self?.route(for: d) }
    }

    public func knownNames() -> [String] { people.contacts.flatMap { [$0.name, $0.name.split(separator: " ").first.map(String.init) ?? $0.name] + $0.nicknames } }

    public func refreshPeople() async {
        let phones = people.contacts.flatMap(\.phones)
        if let p = try? await directory.lookup(phones: phones) { people.profiles = p }
    }

    /// Start checking routes now; the sender awaits the same result.
    public func prefetchRoute(_ d: Device) {
        lock.lock(); defer { lock.unlock() }
        if routeTasks[d.id] == nil { routeTasks[d.id] = Task { await Routes.best(d) } }
    }

    func route(for d: Device) async -> Endpoint? {
        lock.lock(); let task = routeTasks.removeValue(forKey: d.id); lock.unlock()
        let prefetched: Endpoint? = await task?.value
        if let prefetched { return prefetched }
        let fresh: Endpoint? = await Routes.best(d)
        if let fresh { return fresh }
        guard let fallback = routeFallback else { return nil }
        return await fallback(d)
    }

    // MARK: Entry points

    public func ask(_ text: String, now: Date = Date(), timeZone: TimeZone = .current) async -> Reply {
        let t0 = Date()
        if ctx.stale { ctx = AgentContext() }
        let intent = IntentParser.parse(text, knownNames: knownNames(), now: now, timeZone: timeZone)
        if let q = ctx.pending, let r = await maybeAnswer(q, intent, text) { return r }
        var r: Reply
        switch intent.action {
        case "connect": r = await connect(intent)
        case "disconnect": r = Reply("Use the audio menu to switch back to this iPhone.", action: .showRoutePicker(hint: "iPhone"))
        case "send": r = await send(intent)
        case "find": r = find(intent)
        case "retarget": r = await retarget(intent)
        case "confirm", "deny", "choose": r = Reply("There's nothing waiting on an answer right now.", ok: false)
        default: r = Reply("Try “Send Daniel the deck I edited this morning”, “Find the deck about customer churn”, or “Connect to Daniel's headphones”.", ok: false)
        }
        if r.timings["total_ms"] == nil { r.timings["total_ms"] = Date().timeIntervalSince(t0) * 1000 }
        return r
    }

    public func answer(questionId: String, optionId: String) async -> Reply {
        guard let q = ctx.pending, q.id == questionId else { return Reply("That question has expired.", ok: false) }
        guard let opt = q.options.first(where: { $0.id == optionId }) else { return Reply("That isn't one of the options.", ok: false) }
        return await resolvePending(q, opt)
    }

    public func selectFile(_ hit: FileHit) -> Reply {
        ctx.file = hit; ctx.files = [hit]; ctx.touch()
        return Reply("\(hit.name) is ready to send. Who should get it?", files: [hit])
    }

    public func sendFile(personId: String, deviceId: String? = nil) async -> Reply {
        guard let p = people.byId(personId) else { return Reply("Unknown person.", ok: false) }
        guard let hit = ctx.file else {
            ctx.person = p
            destinations(p).forEach(prefetchRoute)
            return Reply("Which file should I send to \(p.first)?", person: p)
        }
        return startSend(hit, p, p.profile?.devices.first { $0.id == deviceId })
    }

    /// The app calls this when the audio route changes, so "Daniel's headphones" resolves next time.
    public func noteRouteChanged() {
        let out = audio.currentOutput()
        guard out.bluetooth, let owner = ctx.audioOwner else { return }
        audioMemory.remember(ownerPhone: owner.isSelf ? me.phone : owner.phone, ownerName: owner.name, outputName: out.name)
        ctx.audioOwner = nil
    }

    // MARK: Pending questions

    private func maybeAnswer(_ q: Question, _ intent: Intent, _ text: String) async -> Reply? {
        let opts = q.options.filter { $0.id != "no" && $0.id != "none" }
        if intent.action == "choose", let i = intent.choice, !opts.isEmpty {
            return await resolvePending(q, i >= -opts.count && i < opts.count ? opts[(i + opts.count) % opts.count] : opts.last!)
        }
        if intent.action == "confirm", let o = q.options.first(where: { $0.id == "yes" }) ?? (q.options.count == 1 ? q.options[0] : nil) { return await resolvePending(q, o) }
        if intent.action == "deny", let o = q.options.first(where: { $0.id == "no" || $0.id == "none" }) { return await resolvePending(q, o) }
        if ["unknown", "find", "choose"].contains(intent.action) {
            let low = text.lowercased()
            var best: Option?, bestS = 0.0
            for o in q.options {
                let ws = o.label.lowercased().replacingOccurrences(of: "—", with: " ").split(separator: " ").map(String.init).filter { $0.count > 2 }
                var s = Double(ws.filter { low.contains($0) }.count) / Double(max(1, ws.count))
                s = max(s, PeopleResolver.ratio(low, o.label.lowercased()) * 0.8)
                if s > bestS { best = o; bestS = s }
            }
            if let best, bestS >= 0.34 { return await resolvePending(q, best) }
        }
        return nil
    }

    private func resolvePending(_ q: Question, _ opt: Option) async -> Reply {
        ctx.pending = nil
        switch q.kind {
        case "choose_file":
            let hit = opt.payload as! FileHit
            ctx.file = hit; ctx.touch()
            if let query = q.state["query"] as? String, !query.isEmpty { usage?.recordPick(query, path: hit.path) }
            if let p = q.state["person"] as? Person { return startSend(hit, p, q.state["device"] as? Device) }
            return Reply("Selected \(hit.name). Who should I send it to?", files: [hit])
        case "choose_person":
            let p = opt.payload as! Person
            ctx.person = p; forcedPerson = p
            let intent = q.state["intent"] as! Intent
            switch intent.action {
            case "connect": return await connect(intent)
            case "retarget": return await retarget(intent)
            default: return await send(intent)
            }
        default:
            return Reply("Okay.")
        }
    }

    // MARK: People

    private func person(_ intent: Intent, defaultSelf: Bool) async -> (Person?, Reply?) {
        if let p = forcedPerson { forcedPerson = nil; return (p, nil) }
        if intent.pronoun || (intent.person == nil && ctx.person != nil && !defaultSelf) {
            return ctx.person.map { ($0, nil) } ?? (nil, Reply("Who do you mean?", ok: false))
        }
        guard let name = intent.person else { return defaultSelf ? (me, nil) : (nil, Reply("Who should I send it to?", ok: false)) }
        if people.profiles.isEmpty { await refreshPeople() }
        let c = people.resolve(name, relation: intent.relation)
        guard !c.isEmpty else { return (nil, Reply("I couldn't find “\(name.capitalized)” in your contacts.", ok: false)) }
        if c[0].person.isSelf { return (me, nil) }
        if PeopleResolver.isAmbiguous(c) {
            let opts = c.prefix(3).map { cand in
                Option(id: cand.person.contact.id, label: cand.person.name,
                       detail: [cand.person.contact.relation, cand.person.nearby ? "nearby" : nil, cand.person.hasApp ? "uses Redtooth" : nil].compactMap { $0 }.joined(separator: " · "),
                       payload: cand.person)
            }
            let q = Question(id: Self.newId("q_"), kind: "choose_person", prompt: "Which \(name.capitalized)?", options: Array(opts), state: ["intent": intent])
            ctx.pending = q
            return (nil, Reply(q.prompt, question: q))
        }
        ctx.person = c[0].person; ctx.touch()
        return (c[0].person, nil)
    }

    // MARK: Headphones (iOS: pick in the system route picker)

    private func connect(_ intent: Intent) async -> Reply {
        let (owner0, err) = await person(intent, defaultSelf: true)
        if let err { return err }
        let owner = owner0!
        let kind = intent.deviceKind.flatMap { audioKinds.contains($0) ? $0 : nil } ?? "headphones"
        if let k = intent.deviceKind, appKinds.contains(k) { return Reply("I can send files to \(possessive(owner.first)) \(k). To play audio, ask for headphones or a speaker.", ok: false) }
        let whose = owner.isSelf ? "your" : possessive(owner.first)
        let registered = owner.profile?.devices.first { $0.kind == kind || (kind == "headphones" && $0.kind == "earbuds") }
        let remembered = audioMemory.lookup(ownerPhone: owner.isSelf ? me.phone : owner.phone)
        let label = remembered ?? registered.map { $0.shortModel ?? $0.label } ?? "\(whose) \(kind)"
        let out = audio.currentOutput()
        if out.bluetooth, let remembered, out.name.compare(remembered, options: .caseInsensitive) == .orderedSame {
            return Reply("Audio is already playing through \(whose) \(remembered).", status: "Audio output")
        }
        ctx.audioOwner = owner
        if remembered != nil {
            return Reply("Choose \(label) in the audio menu.", status: "Waiting for you", person: owner, action: .showRoutePicker(hint: label))
        }
        // Not paired with this iPhone before: only Settings can pair, and the headphones must be in pairing mode.
        var nudged = false
        if !owner.isSelf, owner.hasApp, let phone = owner.phone {
            nudged = (try? await directory.nudge(toPhone: phone, payload: ["type": "pairing_mode", "from": me.name, "kind": kind,
                                                                         "text": "\(me.name) wants to connect to your \(kind). Put them in pairing mode."])) ?? false
        }
        let who = owner.isSelf ? "Put them" : "Ask \(owner.first) to put them"
        let tail = nudged ? " I've sent \(owner.first) a notification." : ""
        return Reply("If \(label) aren't paired with this iPhone yet: \(who) in pairing mode, then pick them in Settings › Bluetooth.\(tail) If they are, choose them in the audio menu.",
                     status: "Waiting for you", person: owner, action: .showRoutePicker(hint: label))
    }

    // MARK: Files

    public func destinations(_ p: Person) -> [Device] {
        (p.profile?.devices ?? []).filter { appKinds.contains($0.kind) && !$0.endpoints.isEmpty }.sorted { (Self.destOrder[$0.kind] ?? 9) < (Self.destOrder[$1.kind] ?? 9) }
    }

    private func pickDestination(_ p: Person, _ kind: String?) -> Device? {
        destinations(p).first { kind == nil || $0.kind == kind || (kind == "laptop" && $0.kind == "desktop") }
    }

    private func ambiguous(_ fq: FileQuery, _ hits: [FileHit]) -> Bool {
        Clarify.ambiguous(hits, hasTopic: !fq.topic.isEmpty || !fq.names.isEmpty)
    }

    private func fileQuestion(_ fq: FileQuery, _ hits: [FileHit], _ state: [String: Any]) -> Question {
        let noun = fq.kinds.first.flatMap { Self.kindNoun[$0] } ?? "file"
        let n = min(3, hits.count)
        let prompt: String
        if let tl = fq.timeLabel, fq.topic.isEmpty, fq.names.isEmpty {
            prompt = "Which \(noun)? \(n == 2 ? "Two" : n == 3 ? "Three" : "\(n)") were edited \(tl)."
        } else {
            prompt = Clarify.distinguish(hits).0 // says what differs: the final or the draft, the PDF or the Word doc…
        }
        let opts = hits.prefix(3).map { h in
            Option(id: "f" + h.id, label: (h.name as NSString).deletingPathExtension, detail: Clarify.optionDetail(h), payload: h)
        }
        var state = state
        state["query"] = ([fq.topic] + fq.names).joined(separator: " ").trimmingCharacters(in: .whitespaces)
        let q = Question(id: Self.newId("q_"), kind: "choose_file", prompt: prompt, options: Array(opts), state: state)
        ctx.pending = q
        return q
    }

    private func indexNote() -> String {
        guard let p = files.indexing(), !p.complete, p.queued > 0 else { return "" }
        return " I'm still indexing older files (\(p.done.formatted()) of \(p.queued.formatted())), so I may have missed some."
    }

    private func send(_ intent: Intent) async -> Reply {
        let t0 = Date()
        let fq = intent.file ?? { var q = FileQuery(); q.refersPrevious = true; return q }()
        // Resolve the recipient and search at the same time; check routes as soon as the person is known.
        async let hitsTask: [FileHit] = (fq.refersPrevious || fq.isEmpty) ? [ctx.file].compactMap { $0 } : files.search(fq)
        let (person0, err) = await person(intent, defaultSelf: false)
        if let p = person0, !p.isSelf { destinations(p).forEach(prefetchRoute) }
        let hits = await hitsTask
        let findMs = Date().timeIntervalSince(t0) * 1000
        if var err {
            err.question?.state["intent"] = intent
            if var q = ctx.pending, q.kind == "choose_person" { q.state["intent"] = intent; ctx.pending = q }
            if !hits.isEmpty && !ambiguous(fq, hits) { ctx.file = hits[0] }
            err.timings["find_match_ms"] = findMs
            return err
        }
        var person = person0!
        if hits.isEmpty {
            let msg = fq.refersPrevious ? "Which file? Nothing is selected yet." : "I couldn't find a \(fq.describe()).\(indexNote())"
            return Reply(msg, files: files.recent(4), person: person, ok: false, timings: ["find_match_ms": findMs])
        }
        var dest: Device?
        if person.isSelf {
            // "Send it to my laptop": your other devices running Redtooth (iPhones can't send plain Bluetooth files).
            let prof = (try? await directory.lookup(phones: [me.phone].compactMap { $0 }))?[me.phone ?? ""]
            if let prof { person = Person(contact: me.contact, profile: prof, isSelf: true) }
            let want = intent.deviceKind ?? "laptop"
            dest = (prof?.devices ?? []).first { appKinds.contains($0.kind) && !$0.endpoints.isEmpty && $0.id != thisDeviceId && ($0.kind == want || (want == "laptop" && $0.kind == "desktop")) }
            if dest == nil {
                return Reply("I don't see your \(want) running Redtooth. Start Redtooth on it (same Wi-Fi), or use AirDrop.", files: Array(hits.prefix(1)), ok: false)
            }
        } else {
            dest = pickDestination(person, intent.deviceKind)
        }
        if ambiguous(fq, hits) {
            let q = fileQuestion(fq, hits, ["person": person, "device": dest as Any])
            return Reply(q.prompt, status: "Waiting for you", question: q, files: Array(hits.prefix(3)), person: person, timings: ["find_match_ms": findMs])
        }
        var r = startSend(hits[0], person, dest)
        r.timings["find_match_ms"] = findMs
        return r
    }

    private func startSend(_ hit: FileHit, _ person: Person, _ dest0: Device?) -> Reply {
        ctx.person = person; ctx.file = hit; ctx.touch()
        guard person.hasApp else {
            return Reply("\(person.first) doesn't use Redtooth yet, so I can't deliver to their devices. You can still AirDrop it, or send them an invite.", files: [hit], person: person, ok: false)
        }
        guard let dest = dest0 ?? pickDestination(person, nil) else {
            return Reply("None of \(possessive(person.first)) devices are reachable right now. I'll keep the file selected.", files: [hit], person: person, ok: false)
        }
        guard let src = files.source(hit) else { return Reply("I can't open \(hit.name) anymore.", ok: false) }
        ctx.device = dest
        let t = sender.start(src, recipientFirst: person.first, device: dest)
        usage?.record(hit.path, kind: "sent")
        ctx.lastTransferId = t.id
        return Reply("Sending \(hit.name) to \(person.isSelf ? "your" : possessive(person.first)) \(dest.label).", status: "Waiting for \(person.first)", files: [hit], person: person, transferId: t.id)
    }

    private func retarget(_ intent: Intent) async -> Reply {
        if let k = intent.deviceKind, audioKinds.contains(k) { return await connect(intent) }
        let (person0, err) = await person(intent, defaultSelf: false)
        if let err { return err }
        let person = person0!
        guard let hit = ctx.file else { return Reply("Which file should I send?", ok: false) }
        guard let dest = pickDestination(person, intent.deviceKind) else {
            return Reply("\(person.first) doesn't have a \(intent.deviceKind ?? "device") set up with Redtooth.", person: person, ok: false)
        }
        if let id = ctx.lastTransferId, let prev = sender.transfer(id), prev.recipientFirst == person.first,
           !["saved", "declined", "failed", "cancelled"].contains(prev.status) { sender.cancel(prev.id) }
        var r = startSend(hit, person, dest)
        if r.ok && intent.instead { r.text = (r.text.hasSuffix(".") ? String(r.text.dropLast()) : r.text) + " instead." }
        return r
    }

    private func find(_ intent: Intent) -> Reply {
        let t0 = Date()
        let fq = intent.file ?? FileQuery()
        if fq.isEmpty { return Reply("What should I look for?", ok: false) }
        let hits = files.search(fq)
        let ms = Date().timeIntervalSince(t0) * 1000
        if hits.isEmpty { return Reply("I couldn't find a \(fq.describe()).\(indexNote())", ok: false, timings: ["find_match_ms": ms]) }
        ctx.files = hits
        if ambiguous(fq, hits) {
            let q = fileQuestion(fq, hits, [:])
            return Reply("I found \(min(3, hits.count)) that could fit. \(q.prompt)", question: q, files: Array(hits.prefix(3)), timings: ["find_match_ms": ms])
        }
        let top = hits[0]
        ctx.file = top; ctx.touch()
        let at = (!top.snippetLabel.isEmpty && !top.snippet.isEmpty) ? " — \(top.snippetLabel): “\(top.snippet.prefix(80))”" : ""
        return Reply("Found \(top.name)\(at).\(indexNote())", files: Array(hits.prefix(3)), timings: ["find_match_ms": ms])
    }

    // MARK: Helpers

    static func newId(_ prefix: String) -> String { prefix + UUID().uuidString.replacingOccurrences(of: "-", with: "").prefix(12).lowercased() }

    public static func whenLabel(_ mtime: Double, now: Date = Date()) -> String { Clarify.whenLabel(mtime, now: now) }
}
