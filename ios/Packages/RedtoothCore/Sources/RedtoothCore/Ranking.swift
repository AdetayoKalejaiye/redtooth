import Foundation

/// The ranking rules shared with the desktop (redtooth/files/index.py, usage.py), checked against
/// ranking_fixtures.json from tools/make_ranking_fixtures.py.
public enum Ranking {
    public static let stop: Set<String> = ["the", "and", "for", "about", "with", "from", "that", "this", "file", "doc", "document", "deck", "my", "our", "your"]

    public static func words(_ text: String) -> [String] {
        var out: [String] = [], cur = ""
        for ch in text {
            if ch.isLetter || ch.isNumber || ch == "_" { cur.append(ch) } else if !cur.isEmpty { out.append(cur); cur = "" }
        }
        if !cur.isEmpty { out.append(cur) }
        return out
    }

    public static func queryWords(_ text: String) -> [String] { words(text).map { $0.lowercased() }.filter { $0.count >= 3 && !stop.contains($0) } }

    static func stem(_ w: String) -> String { w.count > 4 && w.hasSuffix("s") ? String(w.dropLast()) : w }

    static func nameWords(_ name: String) -> String {
        let s = (name as NSString).deletingPathExtension
        let stemName = s.isEmpty ? name : s
        let spaced = stemName.replacingOccurrences(of: "([a-z])([A-Z])", with: "$1 $2", options: .regularExpression)
        return "\(stemName) \(spaced.replacingOccurrences(of: "[_.]+", with: " ", options: .regularExpression))"
    }

    /// "term paper" beats a file that only says "term" or only "paper". Same numbers as the desktop.
    public static func coverageBonus(_ qws: [String], name: String, chunk: String) -> Double {
        guard !qws.isEmpty else { return 0 }
        let compactQ = qws.joined()
        let nwords = Set(words(nameWords(name).lowercased()).map(stem))
        let compactName = name.lowercased().replacingOccurrences(of: "[\\s_\\-.]+", with: "", options: .regularExpression)
        let want = qws.map(stem)
        var inName = Double(want.filter { nwords.contains($0) }.count) / Double(want.count)
        if qws.count >= 2 && compactName.contains(compactQ) { inName = 1 }
        let text = chunk.lowercased()
        let twords = Set(words(text).map(stem))
        let inText = text.isEmpty ? 0 : Double(want.filter { twords.contains($0) }.count) / Double(want.count)
        let joined = qws.joined(separator: " ")
        let phrase = qws.count >= 2 && (nameWords(name).lowercased().contains(joined) ||
            text.replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression).contains(joined))
        var bonus = 0.015 * inName + (inName == 1 && qws.count >= 2 ? 0.03 : 0)
        bonus += 0.008 * inText + (inText == 1 && qws.count >= 2 ? 0.006 : 0)
        bonus += phrase ? 0.01 : 0
        return bonus
    }

    /// Newer files win ties: halves every six weeks.
    public static func recency(_ mtime: Double, now: Double = Date().timeIntervalSince1970, weight: Double = 0.006) -> Double {
        weight * pow(0.5, max(0, (now - mtime) / 86400) / 42)
    }
}

/// What you use: files sent or picked in Redtooth and which file you picked for a request before.
/// Two-week half-life; kept as a small JSON file in the App Group.
public final class UsageStore: @unchecked Sendable {
    struct Event: Codable { let path: String, kind: String, at: Double }
    struct Saved: Codable { var events: [Event]; var picks: [String: [String: Int]] }
    public static let halfLifeDays = 14.0
    static let weights = ["sent": 1.0, "picked": 0.8, "opened": 0.6]

    private let url: URL?
    private let now: () -> Double
    private var data = Saved(events: [], picks: [:])
    private let lock = NSLock()

    public init(url: URL? = nil, now: @escaping () -> Double = { Date().timeIntervalSince1970 }) {
        self.url = url; self.now = now
        if let url, let d = try? Data(contentsOf: url), let s = try? JSONDecoder().decode(Saved.self, from: d) { data = s }
    }

    public static func normQuery(_ q: String) -> String {
        Ranking.words(q.lowercased()).map { $0.count > 4 && $0.hasSuffix("s") ? String($0.dropLast()) : $0 }.joined(separator: " ")
    }

    public func record(_ path: String, kind: String) {
        lock.lock(); data.events.append(Event(path: path, kind: kind, at: now())); lock.unlock(); save()
    }

    public func recordPick(_ query: String, path: String) {
        let q = Self.normQuery(query)
        guard !q.isEmpty else { return }
        lock.lock()
        data.picks[q, default: [:]][path, default: 0] += 1
        data.events.append(Event(path: path, kind: "picked", at: now()))
        lock.unlock()
        save()
    }

    /// path -> (usage 0..1, picked-for-this-request 0..1, reasons)
    public func signals(_ paths: [String], query: String = "") -> [String: (Double, Double, [String])] {
        lock.lock(); defer { lock.unlock() }
        let t = now(), q = Self.normQuery(query)
        var out: [String: (Double, Double, [String])] = [:]
        for p in paths {
            var raw = 0.0
            var why: [String] = []
            for e in data.events where e.path == p {
                raw += (Self.weights[e.kind] ?? 0.5) * pow(0.5, max(0, t - e.at) / 86400 / Self.halfLifeDays)
                if e.kind == "sent" && !why.contains("sent before") { why.append("sent before") }
            }
            let n = data.picks[q]?[p] ?? 0
            let pick = 1 - pow(0.5, Double(n))
            if pick > 0 { why.insert("you picked it for this before", at: 0) }
            out[p] = (1 - exp(-raw), pick, why)
        }
        return out
    }

    private func save() {
        guard let url else { return }
        lock.lock()
        let cutoff = now() - 180 * 86400
        data.events.removeAll { $0.at < cutoff }
        let d = try? JSONEncoder().encode(data)
        lock.unlock()
        try? d?.write(to: url, options: .atomic)
    }
}
