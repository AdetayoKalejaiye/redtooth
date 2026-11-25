import Compression
import Foundation
import PDFKit
import RedtoothCore
import SQLite3

/// On-device file search for iPhone. iOS only lets an app read folders the user picked
/// (Files › a folder, iCloud Drive, On My iPhone) — so the index covers those folder grants plus
/// files shared into Redtooth. Same ranking idea as the desktop: name-weighted bm25 over
/// names and extracted text, then recency and file-type filters from the parsed request.
final class FileIndex: FileSearch, @unchecked Sendable {
    private var db: OpaquePointer?
    private let q = DispatchQueue(label: "redtooth.index")
    private var progress = (done: 0, queued: 0, complete: true)
    private(set) var fts = "fts5"

    init(path: URL = Shared.container.appendingPathComponent("index.sqlite")) {
        sqlite3_open(path.path, &db)
        exec("PRAGMA journal_mode=WAL")
        exec("CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY, path TEXT UNIQUE, folder TEXT, name TEXT, ext TEXT, size INTEGER, mtime REAL)")
        exec("CREATE TABLE IF NOT EXISTS folders(id TEXT PRIMARY KEY, bookmark BLOB, name TEXT)")
        // FTS5 is in the system SQLite on current iOS; fall back to FTS4 just in case.
        if !exec("CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(name, body, tokenize='unicode61 remove_diacritics 2')") {
            fts = "fts4"
            exec("CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts4(name, body, tokenize=unicode61)")
        }
    }

    @discardableResult
    private func exec(_ sql: String) -> Bool { sqlite3_exec(db, sql, nil, nil, nil) == SQLITE_OK }

    private func rows(_ sql: String, _ args: [Any?] = [], _ each: (OpaquePointer) -> Void) {
        var st: OpaquePointer?
        guard sqlite3_prepare_v2(db, sql, -1, &st, nil) == SQLITE_OK, let st else { return }
        defer { sqlite3_finalize(st) }
        let T = unsafeBitCast(-1, to: sqlite3_destructor_type.self)
        for (i, a) in args.enumerated() {
            let n = Int32(i + 1)
            switch a {
            case let s as String: sqlite3_bind_text(st, n, s, -1, T)
            case let d as Double: sqlite3_bind_double(st, n, d)
            case let v as Int64: sqlite3_bind_int64(st, n, v)
            case let v as Int: sqlite3_bind_int64(st, n, Int64(v))
            case let b as Data: _ = b.withUnsafeBytes { sqlite3_bind_blob(st, n, $0.baseAddress, Int32(b.count), T) }
            default: sqlite3_bind_null(st, n)
            }
        }
        while sqlite3_step(st) == SQLITE_ROW { each(st) }
    }

    private static func text(_ st: OpaquePointer, _ i: Int32) -> String { sqlite3_column_text(st, i).map { String(cString: $0) } ?? "" }

    // MARK: Folder grants

    func addFolder(_ url: URL) throws {
        guard url.startAccessingSecurityScopedResource() else { throw CocoaError(.fileReadNoPermission) }
        defer { url.stopAccessingSecurityScopedResource() }
        let bm = try url.bookmarkData(options: .minimalBookmark, includingResourceValuesForKeys: nil, relativeTo: nil)
        q.sync { rows("INSERT OR REPLACE INTO folders(id, bookmark, name) VALUES(?,?,?)", [url.path, bm, url.lastPathComponent]) { _ in } }
    }

    func folders() -> [(id: String, name: String, url: URL?)] {
        var out: [(String, String, URL?)] = []
        q.sync {
            rows("SELECT id, bookmark, name FROM folders") { st in
                let len = Int(sqlite3_column_bytes(st, 1))
                let bm = sqlite3_column_blob(st, 1).map { Data(bytes: $0, count: len) }
                var stale = false
                let url = bm.flatMap { try? URL(resolvingBookmarkData: $0, bookmarkDataIsStale: &stale) }
                out.append((Self.text(st, 0), Self.text(st, 2), url))
            }
        }
        return out
    }

    /// Re-index all granted folders: changed files only, newest first (same order as the desktop indexer).
    func refresh() async {
        let grants = folders()
        await Task.detached(priority: .utility) { [self] in
            for g in grants {
                guard let root = g.url, root.startAccessingSecurityScopedResource() else { continue }
                defer { root.stopAccessingSecurityScopedResource() }
                var seen: [String: (Int64, Double)] = [:]
                q.sync { rows("SELECT path, size, mtime FROM files WHERE folder = ?", [g.id]) { st in
                    seen[Self.text(st, 0)] = (sqlite3_column_int64(st, 1), sqlite3_column_double(st, 2)) } }
                var todo: [(URL, Int64, Double)] = [], present = Set<String>()
                let keys: [URLResourceKey] = [.isRegularFileKey, .fileSizeKey, .contentModificationDateKey]
                let en = FileManager.default.enumerator(at: root, includingPropertiesForKeys: keys, options: [.skipsHiddenFiles, .skipsPackageDescendants])
                while let u = en?.nextObject() as? URL {
                    guard let v = try? u.resourceValues(forKeys: Set(keys)), v.isRegularFile == true else { continue }
                    let size = Int64(v.fileSize ?? 0), m = v.contentModificationDate?.timeIntervalSince1970 ?? 0
                    present.insert(u.path)
                    if let old = seen[u.path], old.0 == size, abs(old.1 - m) < 1 { continue }
                    todo.append((u, size, m))
                }
                todo.sort { $0.2 > $1.2 }
                q.sync { progress = (0, todo.count, todo.isEmpty) }
                for (i, (u, size, m)) in todo.enumerated() {
                    let body = Self.extract(u)
                    q.sync {
                        exec("BEGIN")
                        rows("INSERT INTO files(path, folder, name, ext, size, mtime) VALUES(?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime=excluded.mtime",
                             [u.path, g.id, u.lastPathComponent, "." + u.pathExtension.lowercased(), size, m]) { _ in }
                        var rowid: Int64 = 0
                        rows("SELECT id FROM files WHERE path = ?", [u.path]) { rowid = sqlite3_column_int64($0, 0) }
                        rows("DELETE FROM fts WHERE rowid = ?", [rowid]) { _ in }
                        rows("INSERT INTO fts(rowid, name, body) VALUES(?,?,?)", [rowid, Self.nameWords(u.lastPathComponent), body]) { _ in }
                        exec("COMMIT")
                        progress.done = i + 1
                    }
                }
                // Forget only files proven gone from a folder we could read.
                let gone = Set(seen.keys).subtracting(present)
                q.sync { for p in gone { rows("DELETE FROM fts WHERE rowid = (SELECT id FROM files WHERE path = ?)", [p]) { _ in }; rows("DELETE FROM files WHERE path = ?", [p]) { _ in } } }
                q.sync { progress.complete = true }
            }
        }.value
    }

    static func nameWords(_ name: String) -> String {
        let base = (name as NSString).deletingPathExtension
        return base.replacingOccurrences(of: "[_\\-.]+", with: " ", options: .regularExpression)
            .replacingOccurrences(of: "([a-z])([A-Z])", with: "$1 $2", options: .regularExpression)
    }

    // MARK: FileSearch

    func search(_ fq: FileQuery) -> [FileHit] {
        var conds: [String] = [], args: [Any?] = []
        if !fq.exts.isEmpty { conds.append("f.ext IN (\(fq.exts.map { _ in "?" }.joined(separator: ",")))"); args += fq.exts.sorted() }
        if let s = fq.since { conds.append("f.mtime >= ?"); args.append(s) }
        if let u = fq.until { conds.append("f.mtime < ?"); args.append(u) }
        let words = (fq.topic + " " + fq.names.joined(separator: " ")).lowercased()
            .components(separatedBy: CharacterSet.alphanumerics.inverted).filter { $0.count > 1 && !Self.stop.contains($0) }
        var out: [FileHit] = []
        q.sync {
            if words.isEmpty {
                let w = conds.isEmpty ? "" : "WHERE " + conds.joined(separator: " AND ")
                rows("SELECT f.path, f.name, f.ext, f.size, f.mtime FROM files f \(w) ORDER BY f.mtime DESC LIMIT 10", args) { st in out.append(hit(st, 1, "", "")) }
                return
            }
            // Prefix match on each word, any word may match; bm25 weights the name 10x.
            let match = words.map { "\"\($0)\"*" }.joined(separator: " OR ")
            let rank = fts == "fts5" ? "bm25(fts, 10.0, 1.0)" : "0"
            let snip = fts == "fts5" ? "snippet(fts, 1, '', '', '…', 12)" : "''"
            let w = (["fts MATCH ?"] + conds).joined(separator: " AND ")
            var raw: [(FileHit, Double)] = []
            rows("SELECT f.path, f.name, f.ext, f.size, f.mtime, \(rank), \(snip) FROM fts JOIN files f ON f.id = fts.rowid WHERE \(w) ORDER BY \(rank) LIMIT 30", [match] + args) { st in
                let r = -sqlite3_column_double(st, 5)
                raw.append((hit(st, 0, Self.text(st, 6), "Text"), r))
            }
            let now = Date().timeIntervalSince1970
            let scored = raw.map { h, r -> (FileHit, Double) in (h, r + 0.5 / (1 + max(0, now - h.mtime) / 86400 / 30)) }.sorted { $0.1 > $1.1 }
            let top = max(scored.first?.1 ?? 1, 1e-9)
            out = scored.prefix(10).map { var h = $0.0; h.score = $0.1 / top; return h }
        }
        return out
    }

    private func hit(_ st: OpaquePointer, _ score: Double, _ snippet: String, _ label: String) -> FileHit {
        let path = Self.text(st, 0), ext = Self.text(st, 2)
        var h = FileHit(id: path, path: path, name: Self.text(st, 1), kind: Self.kind(ext), ext: ext, size: sqlite3_column_int64(st, 3), mtime: sqlite3_column_double(st, 4), score: score)
        h.snippet = snippet; h.snippetLabel = snippet.isEmpty ? "" : label
        return h
    }

    func recent(_ n: Int) -> [FileHit] {
        var out: [FileHit] = []
        q.sync { rows("SELECT path, name, ext, size, mtime FROM files ORDER BY mtime DESC LIMIT ?", [n]) { out.append(hit($0, 1, "", "")) } }
        return out
    }

    func source(_ hit: FileHit) -> FileSource? {
        // Open the folder grant that contains this file; the sender reads it while the scope is open.
        for g in folders() where hit.path.hasPrefix(g.id) {
            if let root = g.url, root.startAccessingSecurityScopedResource() { return FileSource(url: URL(fileURLWithPath: hit.path)) }
        }
        let u = URL(fileURLWithPath: hit.path)
        return FileManager.default.isReadableFile(atPath: u.path) ? FileSource(url: u) : nil
    }

    func indexing() -> (done: Int, queued: Int, complete: Bool)? { q.sync { progress } }

    static let stop: Set<String> = ["the", "a", "an", "and", "or", "of", "to", "for", "about", "on", "in", "with", "my", "file", "doc"]

    static func kind(_ ext: String) -> String {
        for (k, _, exts) in fileKinds where exts.contains(ext) { return k }
        return "file"
    }

    // MARK: Text extraction (first ~200 KB of text per file, like the desktop)

    static func extract(_ u: URL) -> String {
        let ext = u.pathExtension.lowercased()
        let limit = 200_000
        switch ext {
        case "txt", "md", "csv", "json", "rtf", "html", "swift", "py", "kt", "js", "ts":
            guard let fh = try? FileHandle(forReadingFrom: u), let d = try? fh.read(upToCount: limit) else { return "" }
            return String(decoding: d, as: UTF8.self)
        case "pdf":
            guard let doc = PDFDocument(url: u) else { return "" }
            var s = ""
            for i in 0..<min(doc.pageCount, 60) { s += (doc.page(at: i)?.string ?? "") + "\n"; if s.count > limit { break } }
            return s
        case "docx", "pptx", "xlsx":
            let prefix = ext == "docx" ? "word/document" : ext == "pptx" ? "ppt/slides/slide" : "xl/sharedStrings"
            let xml = Zip.entries(u, prefix: prefix, maxBytes: 8 << 20).joined(separator: "\n")
            return String(xml.replacingOccurrences(of: "<[^>]+>", with: " ", options: .regularExpression)
                .replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression).prefix(limit))
        default:
            return ""
        }
    }
}

/// Just enough ZIP reading for Office files: central directory → stored or deflated entries.
enum Zip {
    static func entries(_ url: URL, prefix: String, maxBytes: Int) -> [String] {
        guard let d = try? Data(contentsOf: url, options: .alwaysMapped), d.count > 22 else { return [] }
        func u16(_ o: Int) -> Int { Int(d[d.startIndex + o]) | Int(d[d.startIndex + o + 1]) << 8 }
        func u32(_ o: Int) -> Int { u16(o) | u16(o + 2) << 16 }
        var eocd = d.count - 22
        while eocd >= max(0, d.count - 65_557) && u32(eocd) != 0x0605_4b50 { eocd -= 1 }
        guard eocd >= 0, u32(eocd) == 0x0605_4b50 else { return [] }
        var p = u32(eocd + 16), out: [String] = [], total = 0
        for _ in 0..<u16(eocd + 10) {
            guard p + 46 <= d.count, u32(p) == 0x0201_4b50 else { break }
            let method = u16(p + 10), csize = u32(p + 20), usize = u32(p + 24), nlen = u16(p + 28), xlen = u16(p + 30), clen = u16(p + 32), local = u32(p + 42)
            let name = String(decoding: d[(d.startIndex + p + 46)..<(d.startIndex + p + 46 + nlen)], as: UTF8.self)
            p += 46 + nlen + xlen + clen
            guard name.hasPrefix(prefix), name.hasSuffix(".xml"), total + usize <= maxBytes, local + 30 <= d.count else { continue }
            let start = local + 30 + u16(local + 26) + u16(local + 28)
            guard start + csize <= d.count else { continue }
            let comp = d[(d.startIndex + start)..<(d.startIndex + start + csize)]
            let raw: Data? = method == 0 ? Data(comp) : method == 8 ? inflate(Data(comp), usize) : nil
            if let raw { out.append(String(decoding: raw, as: UTF8.self)); total += usize }
        }
        return out
    }

    /// ZIP stores raw DEFLATE, which is what COMPRESSION_ZLIB decodes.
    static func inflate(_ src: Data, _ size: Int) -> Data? {
        var dst = Data(count: max(size, 1))
        let n = dst.withUnsafeMutableBytes { d in src.withUnsafeBytes { s in
            compression_decode_buffer(d.bindMemory(to: UInt8.self).baseAddress!, size, s.bindMemory(to: UInt8.self).baseAddress!, src.count, nil, COMPRESSION_ZLIB)
        } }
        return n == size ? dst : nil
    }
}
