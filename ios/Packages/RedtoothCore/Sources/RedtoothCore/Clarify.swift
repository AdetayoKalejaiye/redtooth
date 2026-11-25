import Foundation

/// When to ask "which file?", and asking about what differs (same rules as redtooth/clarify.py).
public enum Clarify {
    static let versionWords = ["final", "draft", "revised", "edited", "signed", "submitted", "old", "new", "copy", "backup", "v1", "v2", "v3", "v4",
                               "notes", "outline", "rough"]
    static let typeNames = [".docx": "Word doc", ".doc": "Word doc", ".pdf": "PDF", ".pptx": "deck", ".key": "Keynote deck", ".xlsx": "spreadsheet",
                            ".csv": "CSV", ".txt": "text file", ".md": "notes file", ".pages": "Pages doc", ".odt": "document", ".rtf": "document"]

    public static func ambiguous(_ hits: [FileHit], hasTopic: Bool) -> Bool {
        guard hits.count >= 2 else { return false }
        if !hasTopic { return true }
        let a = hits[0], b = hits[1]
        if a.reasons.first?.hasPrefix("you picked it") == true { return false }
        if let ca = a.confidence, let cb = b.confidence {
            if ca < 0.15 { return true }
            return cb >= 0.3 && (a.score - b.score) < 0.15
        }
        return b.score >= 0.85
    }

    static func words(_ name: String, keepCase: Bool = false) -> [String] {
        var stem = (name as NSString).deletingPathExtension
        if stem.isEmpty { stem = name }
        stem = stem.replacingOccurrences(of: "([a-z])([A-Z])", with: "$1 $2", options: .regularExpression)
        let ws = stem.components(separatedBy: CharacterSet(charactersIn: " \t\n_-.()[]")).filter { !$0.isEmpty }
        return keepCase ? ws : ws.map { $0.lowercased() }
    }

    static func core(_ name: String) -> String {
        words(name).filter { w in !versionWords.contains(w) && w.range(of: "^(\\d{1,4}|v\\d+|copy)$", options: .regularExpression) == nil }.joined(separator: " ")
    }

    public static func folder(_ path: String) -> String {
        let parts = path.split(omittingEmptySubsequences: false, whereSeparator: { $0 == "/" || $0 == "\\" }).map(String.init)
        if parts.count >= 3 { return parts[(parts.count - 3)..<(parts.count - 1)].joined(separator: " › ") }
        return parts.count == 2 ? parts[0] : ""
    }

    static func join(_ p: [String]) -> String { p.count == 2 ? "\(p[0]) or \(p[1])" : p.dropLast().joined(separator: ", ") + " or " + p.last! }

    /// The date label the desktop uses ("9:12 AM", "Yesterday 4:05 PM", "Mon 9:12 AM", "Nov 3"), in en-US form.
    public static func whenLabel(_ mtime: Double, now: Date = Date(), timeZone: TimeZone = .current) -> String {
        var cal = Calendar(identifier: .gregorian); cal.timeZone = timeZone
        let d = Date(timeIntervalSince1970: mtime)
        func fmt(_ f: String) -> String { let df = DateFormatter(); df.locale = Locale(identifier: "en_US_POSIX"); df.timeZone = timeZone; df.dateFormat = f; return df.string(from: d) }
        let t = fmt("h:mm a")
        if cal.isDate(d, inSameDayAs: now) { return t }
        if let y = cal.date(byAdding: .day, value: -1, to: now), cal.isDate(d, inSameDayAs: y) { return "Yesterday \(t)" }
        if now.timeIntervalSince(d) < 7 * 86400 { return fmt("EEE ") + t }
        return fmt("MMM d")
    }

    public static func distinguish(_ hits: [FileHit], now: Date = Date(), timeZone: TimeZone = .current) -> (String, [String]) {
        let hs = Array(hits.prefix(3))
        let exts = hs.map { $0.ext.lowercased() }
        let cores = hs.map { core($0.name) }
        let ws = hs.map { Set(words($0.name)) }
        let dates = hs.map { whenLabel($0.mtime, now: now, timeZone: timeZone) }
        if Set(cores).count == 1 && Set(exts).count == hs.count {
            let phr = exts.map { "the \(typeNames[$0] ?? String($0.drop(while: { $0 == "." })).uppercased())" }
            return ("Which one: \(join(phr))?", phr)
        }
        if Set(cores).count == 1 || ws.dropFirst().allSatisfy({ $0.intersection(ws[0]).count >= max(1, ws[0].count / 2) }) {
            let tags = ws.map { w in versionWords.first { w.contains($0) } }
            let phr: [String]
            if tags.allSatisfy({ $0 != nil }) && Set(tags.compactMap { $0 }).count == tags.count {
                phr = tags.indices.map { i in "the \(tags[i]!) (\(i == 0 ? "edited " : "")\(dates[i]))" }
            } else if Set(dates).count == dates.count {
                phr = dates.map { "the one from \($0)" }
            } else {
                let folders = hs.map { folder($0.path) }
                phr = Set(folders).count == folders.count ? folders.map { "the one in \($0)" } : hs.map { ($0.name as NSString).deletingPathExtension }
            }
            return ("Which one: \(join(phr))?", phr)
        }
        let common = ws.dropFirst().reduce(ws[0]) { $0.intersection($1) }
        let phr = hs.map { h -> String in
            var seen = Set<String>()
            let own = words(h.name, keepCase: true).filter { !common.contains($0.lowercased()) && seen.insert($0).inserted }
            var label = String(own.joined(separator: " ").prefix(40)).trimmingCharacters(in: .whitespaces)
            if label.isEmpty { label = (h.name as NSString).deletingPathExtension }
            return "“\(label)”"
        }
        return ("Which one: \(join(phr))?", phr)
    }

    public static func optionDetail(_ h: FileHit, now: Date = Date(), timeZone: TimeZone = .current) -> String {
        var bits = ["Edited \(whenLabel(h.mtime, now: now, timeZone: timeZone))"]
        let f = folder(h.path)
        if !f.isEmpty { bits.append(f) }
        if let r = h.reasons.first { bits.append(r) } else if !h.snippetLabel.isEmpty { bits.append(h.snippetLabel) }
        return bits.joined(separator: " · ")
    }
}
