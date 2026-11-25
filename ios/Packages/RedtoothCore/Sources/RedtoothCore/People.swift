import Foundation

/// Resolve a spoken name to a contact, ranked by name match, relation, app use and proximity (port of contacts.py).
public final class PeopleResolver: @unchecked Sendable {
    public let contacts: [Contact]
    public let me: Person
    public var profiles: [String: Profile] = [:] // normalized phone -> profile
    public var nearbyPhones: Set<String> = []

    public struct Candidate { public let person: Person; public let score: Double; public let nameScore: Double }
    public static let selfWords: Set<String> = ["my", "mine", "me", "i", "myself", "our"]

    public init(contacts: [Contact], me: Person) { self.contacts = contacts; self.me = me }

    public func person(for c: Contact) -> Person {
        Person(contact: c, profile: c.phones.lazy.compactMap { self.profiles[$0] }.first, nearby: c.phones.contains { nearbyPhones.contains($0) })
    }
    public func byId(_ id: String) -> Person? { contacts.first { $0.id == id }.map(person(for:)) }

    public func allPeople() -> [Person] {
        contacts.map(person(for:)).sorted {
            ($0.nearby ? 0 : 1, $0.hasApp ? 0 : 1, -($0.contact.lastInteraction ?? 0), $0.name) < ($1.nearby ? 0 : 1, $1.hasApp ? 0 : 1, -($1.contact.lastInteraction ?? 0), $1.name)
        }
    }

    public func resolve(_ query: String, relation: String? = nil) -> [Candidate] {
        let q = query.lowercased().trimmingCharacters(in: .whitespaces)
        if Self.selfWords.contains(q) { return [Candidate(person: me, score: 1, nameScore: 1)] }
        var out: [Candidate] = []
        for c in contacts {
            let ns = Self.nameScore(q, c)
            guard ns > 0 else { continue }
            let p = person(for: c)
            var s = ns
            if let relation, let r = c.relation, relation.lowercased() == r.lowercased() { s += 0.15 }
            if p.nearby { s += 0.12 }
            if p.hasApp { s += 0.08 }
            if c.lastInteraction != nil { s += 0.02 }
            out.append(Candidate(person: p, score: s, nameScore: ns))
        }
        return out.sorted { $0.score > $1.score }
    }

    public static func isAmbiguous(_ c: [Candidate]) -> Bool {
        guard c.count >= 2 else { return false }
        return c[1].nameScore >= 0.85 * c[0].nameScore && (c[0].score - c[1].score) < 0.12
    }

    static func nameScore(_ q: String, _ c: Contact) -> Double {
        let full = c.name.lowercased()
        let parts = full.split(whereSeparator: \.isWhitespace).map(String.init)
        if q == full { return 1 }
        if c.nicknames.contains(where: { $0.lowercased() == q }) { return 0.9 }
        if q == parts.first { return 0.88 }
        if parts.contains(q) { return 0.75 }
        if full.hasPrefix(q) && q.count >= 3 { return 0.7 }
        let best = (parts + [full]).map { ratio(q, $0) }.max() ?? 0
        return best >= 0.8 ? 0.6 * best : 0
    }

    /// difflib.SequenceMatcher.ratio()
    public static func ratio(_ a: String, _ b: String) -> Double {
        let a = Array(a), b = Array(b)
        if a.isEmpty && b.isEmpty { return 1 }
        func matches(_ a0: Int, _ a1: Int, _ b0: Int, _ b1: Int) -> Int {
            var best = 0, bi = a0, bj = b0
            for i in a0..<max(a0, a1) {
                for j in b0..<max(b0, b1) {
                    var k = 0
                    while i + k < a1 && j + k < b1 && a[i + k] == b[j + k] { k += 1 }
                    if k > best { best = k; bi = i; bj = j }
                }
            }
            if best == 0 { return 0 }
            return best + matches(a0, bi, b0, bj) + matches(bi + best, a1, bj + best, b1)
        }
        return 2.0 * Double(matches(0, a.count, 0, b.count)) / Double(a.count + b.count)
    }
}
