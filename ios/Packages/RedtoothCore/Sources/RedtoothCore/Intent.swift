import Foundation

// Port of redtooth/intent.py and timeparse.py (via the Kotlin port, which passes the parity fixtures).
// The same fixtures run in Tests/RedtoothCoreTests/ParserParityTests.swift.

// MARK: - Small regex helper over NSRegularExpression (ICU: \w and \b are Unicode-aware, like Python).

struct RX {
    let re: NSRegularExpression
    init(_ pattern: String, ci: Bool = false) {
        re = try! NSRegularExpression(pattern: pattern, options: ci ? [.caseInsensitive] : [])
    }
    struct M {
        let s: String
        let r: NSTextCheckingResult
        func g(_ i: Int) -> String? {
            let nr = r.range(at: i)
            guard nr.location != NSNotFound, let rr = Range(nr, in: s) else { return nil }
            return String(s[rr])
        }
        var value: String { g(0) ?? "" }
        var range: Range<String.Index> { Range(r.range, in: s)! }
    }
    func first(_ s: String) -> M? { re.firstMatch(in: s, range: NSRange(s.startIndex..., in: s)).map { M(s: s, r: $0) } }
    func all(_ s: String) -> [M] { re.matches(in: s, range: NSRange(s.startIndex..., in: s)).map { M(s: s, r: $0) } }
    func test(_ s: String) -> Bool { first(s) != nil }
    func replaceAll(_ s: String, _ with: String) -> String { re.stringByReplacingMatches(in: s, range: NSRange(s.startIndex..., in: s), withTemplate: with) }
    func replaceFirst(_ s: String, _ with: String) -> String {
        guard let m = first(s) else { return s }
        return s.replacingCharacters(in: m.range, with: with)
    }
    /// Text before the first match (Python: re.split(...)[0]).
    func before(_ s: String) -> String { first(s).map { String(s[..<$0.range.lowerBound]) } ?? s }
    static func escape(_ s: String) -> String { NSRegularExpression.escapedPattern(for: s) }
}

// MARK: - Vocabulary

public let deviceWords: [(String, [String])] = [
    ("headphones", ["headphones", "headphone", "headset", "cans", "earphones", "earbuds", "buds", "airpods", "xm5s", "xm5", "xm4s", "xm4", "qc45s", "qc45", "beats"]),
    ("speaker", ["speaker", "speakers", "soundbar", "homepod", "sonos"]),
    ("laptop", ["laptop", "macbook", "notebook", "computer", "pc", "mac"]),
    ("desktop", ["desktop", "imac", "workstation"]),
    ("phone", ["phone", "iphone", "pixel", "galaxy", "mobile", "cell"]),
    ("tablet", ["ipad", "tablet"]),
]
let modelHints = ["airpods max", "airpods pro", "airpods", "xm5", "xm4", "sony", "bose", "qc45", "beats", "jabra", "galaxy buds", "pixel buds", "jbl"]
public let fileKinds: [(String, [String], Set<String>)] = [
    ("presentation", ["deck", "decks", "presentation", "presentations", "slides", "slide deck", "keynote", "powerpoint", "pitch"], [".pptx", ".ppt", ".key", ".odp"]),
    ("document", ["doc", "docs", "document", "documents", "memo", "write-up", "writeup", "draft", "letter", "word doc"], [".docx", ".doc", ".odt", ".pages", ".md", ".txt", ".rtf", ".pdf"]),
    ("spreadsheet", ["spreadsheet", "sheet", "sheets", "excel", "budget", "model", "csv"], [".xlsx", ".xls", ".csv", ".numbers", ".ods"]),
    ("pdf", ["pdf", "pdfs"], [".pdf"]),
    ("image", ["photo", "photos", "picture", "pictures", "image", "images", "screenshot", "screenshots", "pic", "pics"], [".jpg", ".jpeg", ".png", ".heic", ".gif", ".webp"]),
    ("video", ["video", "videos", "recording", "clip", "movie"], [".mp4", ".mov", ".mkv", ".webm"]),
    ("audio", ["song", "track", "audio file", "voice memo"], [".mp3", ".m4a", ".wav", ".flac"]),
]
let topicalKindWords: Set<String> = ["budget", "pitch", "memo", "letter", "recording", "model", "screenshot", "keynote"]
public let pronounPeople: Set<String> = ["he", "him", "his", "she", "her", "hers", "they", "them", "their"]
let notNames: Set<String> = ["it", "that", "what", "let", "there", "here", "who", "he", "she", "where", "today", "everyone", "someone", "one"]
let stopWords: Set<String> = Set("""
a an the my me i to for of on in at with and or is was were be this that these those it its from by about
please can could would you hey ok okay just some file files thing stuff one i'd i'm send share find search look
for show open get give edited modified updated changed worked made created saved wrote last latest recent new
over across which where whats what's called named titled regarding re related topic copy version instead
connect friend friends mine our his her their him them up are those these yes no
""".split(whereSeparator: \.isWhitespace).map(String.init))
let confirmRX = RX("^(yes|yeah|yep|yup|ya|sure|correct|right|ok|okay|exactly|that'?s (right|them|it|correct|the one)|those|that one|affirmative|do it|go ahead)\\b", ci: true)
let denyRX = RX("^(no|nope|nah|wrong|not (those|that|them|it|these)|neither|cancel)\\b", ci: true)
let ordinals = ["first": 0, "1st": 0, "one": 0, "second": 1, "2nd": 1, "two": 1, "third": 2, "3rd": 2, "three": 2, "fourth": 3, "last": -1]
let relRX = RX("\\bmy (friend|coworker|colleague|brother|sister|mom|dad|partner|roommate|boss)\\b", ci: true)
private let W = "[\\p{L}\\p{N}_]"

// MARK: - Types

public struct FileQuery: @unchecked Sendable {
    public var kinds: [String] = []
    public var exts: Set<String> = []
    public var topic = ""
    public var since: Double?
    public var until: Double?
    public var timeLabel: String?
    public var refersPrevious = false
    public var names: [String] = []
    public init() {}

    public var isEmpty: Bool { kinds.isEmpty && topic.isEmpty && since == nil && names.isEmpty }

    public func describe() -> String {
        let nouns = ["presentation": "deck", "spreadsheet": "spreadsheet", "image": "photo"]
        let noun = kinds.first.map { nouns[$0] ?? $0 } ?? "file"
        return [noun, topic.isEmpty ? nil : "about “\(topic)”", timeLabel.map { "edited \($0)" }].compactMap { $0 }.joined(separator: " ")
    }
}

public struct Intent: @unchecked Sendable {
    public var action: String // connect | disconnect | send | find | retarget | confirm | deny | choose | unknown
    public var text: String
    public var person: String?
    public var pronoun = false
    public var relation: String?
    public var deviceKind: String?
    public var deviceHint: String?
    public var file: FileQuery?
    public var choice: Int?
    public var instead = false
}

func relationHint(_ text: String) -> String? {
    guard let rel = relRX.first(text)?.g(1)?.lowercased() else { return nil }
    return rel == "colleague" ? "coworker" : rel
}

private func norm(_ text: String) -> String {
    var t = text.replacingOccurrences(of: "’", with: "'").replacingOccurrences(of: "‘", with: "'")
        .replacingOccurrences(of: "“", with: "\"").replacingOccurrences(of: "”", with: "\"")
    t = RX("^\\s*(hey|hi|ok|okay)?\\s*(redtooth|claude|siri)?[,!]?\\s*", ci: true).replaceFirst(t, "")
    t = RX("^\\s*(please|can you|could you|would you|i want to|i'd like to|let's|i need to|help me)\\s+", ci: true).replaceFirst(t, "")
    t = RX("^\\s*(please)\\s+", ci: true).replaceFirst(t, "")
    t = t.trimmingCharacters(in: .whitespacesAndNewlines)
    while let last = t.last, "?.!".contains(last) { t.removeLast() }
    return t
}

private func findWord(_ t: String, _ words: [String]) -> String? {
    for w in words.enumerated().sorted(by: { ($0.element.count, -$0.offset) > ($1.element.count, -$1.offset) }).map(\.element) {
        if RX("\\b" + RX.escape(w) + "\\b").test(t) { return w }
    }
    return nil
}

// MARK: - Parser

public enum IntentParser {
    public static func parse(_ text: String, knownNames: [String] = [], now: Date = Date(), timeZone: TimeZone = .current) -> Intent {
        let t = norm(text)
        let low = t.lowercased()
        let names = Set(knownNames.filter { !$0.isEmpty }.map { $0.lowercased() })

        let words = low.split(whereSeparator: \.isWhitespace)
        if words.count <= 4 && confirmRX.test(low) && !RX("\\b(send|connect|find)\\b").test(low) { return Intent(action: "confirm", text: text) }
        if words.count <= 4 && denyRX.test(low) { return Intent(action: "deny", text: text) }
        if let m = RX("^(?:the |number |option |#)?(\\d|first|second|third|fourth|last|1st|2nd|3rd)(?: one)?$").first(low), let v = m.g(1) {
            return Intent(action: "choose", text: text, choice: Int(v).map { $0 - 1 } ?? ordinals[v])
        }

        var deviceKind: String?
        for (kind, ws) in deviceWords where findWord(low, ws) != nil { deviceKind = kind; break }
        let deviceHint = findWord(low, modelHints)

        var person: String?
        var pronoun = false
        let rel = relationHint(low)
        if let rel {
            person = RX("\\bmy " + RX.escape(rel) + "s? ([a-z][\\p{L}\\p{N}_\\-]+?)(?:'s?\\b|\\b)").first(low.replacingOccurrences(of: "colleague", with: "coworker"))?.g(1)
        }
        let allDeviceWords = deviceWords.flatMap(\.1)
        if person == nil {
            for m in RX("\\b([a-z][\\p{L}\\p{N}_\\-]*)'s?\\b").all(low) {
                guard let cand = m.g(1) else { continue }
                if !notNames.contains(cand) && (names.isEmpty || names.contains(cand) || findWord(cand, allDeviceWords) == nil) { person = cand; break }
            }
        }
        if person == nil && !names.isEmpty {
            person = names.sorted { $0.count > $1.count }.first { RX("\\b" + RX.escape($0) + "\\b").test(low) }
        }
        if person == nil {
            let m = RX("\\b(?:send|share|give|forward|airdrop|text)\\s+([a-z][\\p{L}\\p{N}_\\-]+)\\s+(?:the|my|a|an|that|this|those|these|it|some|our)\\b").first(low)
                ?? RX("\\b(?:to|with)\\s+([a-z][\\p{L}\\p{N}_\\-]+)\\b(?!\\s*(?:laptop|phone|mac|ipad))").first(low)
            if let g = m?.g(1), !stopWords.contains(g), !pronounPeople.contains(g) { person = g }
        }
        if person == nil {
            if let pm = RX("\\b(his|her|their|him|them)\\b").first(low) { person = pm.g(1); pronoun = true }
            else if RX("\\bmy (own )?(headphones|airpods|earbuds|buds|speaker|laptop|phone)\\b").test(low) { person = "my" }
        }

        let instead = RX("\\binstead\\b|\\brather\\b").test(low)
        var action: String
        if RX("\\bdisconnect\\b").test(low) { action = "disconnect" }
        else if RX("^(send|share|airdrop|give|forward|transfer|drop|text|email)\\b").test(low) || RX("\\b(send|share|airdrop)\\b").test(low) { action = "send" }
        else if RX("^(find|search|look for|look up|where'?s|where is|locate|show me|pull up|open|get me|which)\\b").test(low) { action = "find" }
        else if RX("\\b(connect|pair|hook up|switch (?:audio|sound|output) to|play (?:it |this |music )?(?:on|through)|listen (?:on|with)|use)\\b").test(low) { action = "connect" }
        else if instead || (pronoun && deviceKind != nil) { action = "retarget" }
        else if deviceKind == "headphones" || deviceKind == "speaker" { action = "connect" }
        else { action = "unknown" }
        if action == "send" && instead && RX("\\b(it|that|this)\\b").test(low) { action = "retarget" }

        var fq: FileQuery?
        if ["send", "find", "retarget", "unknown"].contains(action) {
            var q = FileQuery()
            let tr = TimeParse.parse(t, now: now, timeZone: timeZone)
            q.since = tr.since; q.until = tr.until; q.timeLabel = tr.label
            let rest = tr.rest, rl = rest.lowercased()
            for (kind, ws, exts) in fileKinds where findWord(rl, ws) != nil { q.kinds.append(kind); q.exts.formUnion(exts) }
            q.names = RX("\"([^\"]+)\"").all(rest).compactMap { $0.g(1) }
            var topicSrc = RX("\\b(?:about|regarding|on the topic of|related to|re:|covering|on)\\s+(.+)$", ci: true).first(rest)?.g(1) ?? rest
            let recipients = (names.union(["him", "her", "them", "his", "their"])).map(RX.escape).joined(separator: "|")
            topicSrc = RX("\\b(?:to|with|for)\\s+(?:my |his |her |their )?(?:\(recipients))\\b", ci: true).before(topicSrc)
            if let p = person, !pronounPeople.contains(p), p != "my" {
                topicSrc = RX("\\b" + RX.escape(p) + "('s)?\\b", ci: true).replaceAll(topicSrc, " ")
            }
            let allKindWords = Set(fileKinds.flatMap(\.1) + allDeviceWords).subtracting(topicalKindWords)
            let toks = RX("\(W)[\\p{L}\\p{N}_\\-.]*").all(topicSrc).map(\.value).filter {
                let l = $0.lowercased(); return !stopWords.contains(l) && !allKindWords.contains(l) && !pronounPeople.contains(l)
            }
            q.topic = toks.joined(separator: " ").trimmingCharacters(in: .whitespaces)
            if q.topic.isEmpty && q.kinds.isEmpty && q.names.isEmpty && (RX("\\b(it|that|this|the file|same)\\b").test(low) || action == "retarget") { q.refersPrevious = true }
            if action == "unknown" && !q.isEmpty && person != nil { action = "send" }
            fq = q
        }
        return Intent(action: action, text: text, person: person, pronoun: pronoun || (person.map { pronounPeople.contains($0) } ?? false),
                      relation: rel, deviceKind: deviceKind, deviceHint: deviceHint, file: fq, choice: nil, instead: instead)
    }
}

// MARK: - Relative time

public enum TimeParse {
    public struct Result { public var since: Double?; public var until: Double?; public var label: String?; public var rest: String }
    static let weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]

    public static func parse(_ text: String, now: Date = Date(), timeZone: TimeZone = .current) -> Result {
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = timeZone
        let t = text.lowercased()
        func at(_ d: Date, _ h: Int, _ m: Int = 0) -> Date { cal.date(bySettingHour: h, minute: m, second: 0, of: d)! }
        func days(_ d: Date, _ n: Int) -> Date { cal.date(byAdding: .day, value: n, to: d)! }
        let today0 = at(now, 0), yday = days(now, -1)
        let weekday = (cal.component(.weekday, from: now) + 5) % 7 // Monday = 0, as in Python
        let rules: [(String, () -> (Date, Date, String))] = [
            ("\\bthis morning\\b", { (today0, cal.component(.hour, from: now) >= 12 ? min(now, at(now, 12)) : now, "this morning") }),
            ("\\bthis afternoon\\b", { (at(now, 12), min(now, at(now, 18)), "this afternoon") }),
            ("\\b(this evening|tonight)\\b", { (at(now, 17), now, "this evening") }),
            ("\\blast night\\b", { (at(yday, 17), at(now, 5), "last night") }),
            ("\\byesterday morning\\b", { (at(yday, 0), at(yday, 12), "yesterday morning") }),
            ("\\byesterday afternoon\\b", { (at(yday, 12), at(yday, 18), "yesterday afternoon") }),
            ("\\byesterday\\b", { (days(today0, -1), today0, "yesterday") }),
            ("\\b(today|earlier today)\\b", { (today0, now, "today") }),
            ("\\b(just now|a few minutes ago|a minute ago|minutes ago)\\b", { (now.addingTimeInterval(-1800), now, "in the last 30 minutes") }),
            ("\\b(an hour ago|an hour back|in the last hour)\\b", { (now.addingTimeInterval(-7200), now, "in the last couple of hours") }),
            ("\\b(recently|lately)\\b", { (now.addingTimeInterval(-3 * 86400), now, "recently") }),
            ("\\bthis week\\b", { (days(today0, -weekday), now, "this week") }),
            ("\\blast week\\b", { (days(today0, -(weekday + 7)), days(today0, -weekday), "last week") }),
            ("\\bthis month\\b", { (cal.date(from: cal.dateComponents([.year, .month], from: now))!, now, "this month") }),
        ]
        func cut(_ r: Range<String.Index>) -> String {
            // Indices come from the lowercased copy; lowercasing keeps lengths for the text we handle.
            let lo = t.distance(from: t.startIndex, to: r.lowerBound), hi = t.distance(from: t.startIndex, to: r.upperBound)
            let a = text.index(text.startIndex, offsetBy: lo), b = text.index(text.startIndex, offsetBy: hi)
            return (String(text[..<a]) + String(text[b...])).trimmingCharacters(in: .whitespaces)
        }
        for (pat, fn) in rules {
            guard let m = RX(pat).first(t) else { continue }
            let (s, u, label) = fn()
            return Result(since: s.timeIntervalSince1970, until: u.timeIntervalSince1970, label: label, rest: cut(m.range))
        }
        if let m = RX("\\b(?:on |last )?(" + weekdays.joined(separator: "|") + ")\\b").first(t), let name = m.g(1), let wd = weekdays.firstIndex(of: name) {
            let delta = ((weekday - wd) % 7 + 7) % 7
            let day = days(today0, -(delta == 0 ? 7 : delta))
            return Result(since: day.timeIntervalSince1970, until: days(day, 1).timeIntervalSince1970, label: "on " + name.capitalized, rest: cut(m.range))
        }
        if let m = RX("\\b(\\d+) (hours?|days?) ago\\b").first(t), let n = Double(m.g(1) ?? "") {
            let unit = (m.g(2) ?? "").hasPrefix("hour") ? 3600.0 : 86400.0
            let center = now.addingTimeInterval(-n * unit)
            return Result(since: center.addingTimeInterval(-unit).timeIntervalSince1970, until: center.addingTimeInterval(unit).timeIntervalSince1970, label: m.value, rest: cut(m.range))
        }
        return Result(since: nil, until: nil, label: nil, rest: text)
    }
}
