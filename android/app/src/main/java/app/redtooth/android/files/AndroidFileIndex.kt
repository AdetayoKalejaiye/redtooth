package app.redtooth.android.files

import android.content.ContentUris
import android.content.Context
import android.content.Intent
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import android.net.Uri
import android.provider.DocumentsContract
import android.provider.DocumentsContract.Document
import android.provider.MediaStore
import app.redtooth.core.FileHit
import app.redtooth.core.FileQuery
import app.redtooth.core.FileSearch
import app.redtooth.core.FileSource
import app.redtooth.core.IndexProgress
import java.io.FileInputStream
import java.io.InputStream
import java.util.zip.ZipInputStream

/**
 * File search on Android.
 *
 * Android apps can't read every file. Redtooth indexes:
 *  - folders the user grants once (Documents, Downloads, a Drive folder…) via the system folder picker,
 *    with text from .docx/.pptx/.xlsx/.txt/.md/.csv; other types by name,
 *  - photos and videos (MediaStore) by name and date, for "the photos from last week".
 * Files edited most recently are indexed first. Keyword search uses SQLite FTS4 (guaranteed on
 * Android; FTS5 isn't). Meaning-based (E5) search isn't on Android yet.
 */
class AndroidFileIndex(private val ctx: Context) : FileSearch {
    private val db = Helper(ctx).writableDatabase
    private val prefs = ctx.getSharedPreferences("folders", Context.MODE_PRIVATE)
    @Volatile private var progress = IndexProgress(0, 0, true)
    @Volatile private var worker: Thread? = null

    private class Helper(ctx: Context) : SQLiteOpenHelper(ctx, "files.db", null, 1) {
        override fun onCreate(db: SQLiteDatabase) {
            db.execSQL("CREATE TABLE files (id INTEGER PRIMARY KEY, uri TEXT UNIQUE, name TEXT, ext TEXT, kind TEXT, size INTEGER, mtime REAL, sig TEXT, root TEXT)")
            db.execSQL("CREATE INDEX files_mtime ON files(ext, mtime)")
            db.execSQL("CREATE TABLE chunks (id INTEGER PRIMARY KEY, file_id INTEGER, label TEXT)")
            db.execSQL("CREATE VIRTUAL TABLE fts USING fts4(name, text, tokenize=unicode61)")
        }
        override fun onUpgrade(db: SQLiteDatabase, o: Int, n: Int) {}
    }

    // ------------------------------------------------------------------ folders
    val folders: List<Uri> get() = (prefs.getStringSet("trees", emptySet()) ?: emptySet()).map(Uri::parse)

    /** Keep access to a folder the user picked (ACTION_OPEN_DOCUMENT_TREE) and index it. */
    fun addFolder(tree: Uri) {
        ctx.contentResolver.takePersistableUriPermission(tree, Intent.FLAG_GRANT_READ_URI_PERMISSION)
        prefs.edit().putStringSet("trees", (prefs.getStringSet("trees", emptySet()) ?: emptySet()) + tree.toString()).apply()
        reindex()
    }

    override fun indexing() = progress

    data class Entry(val uri: Uri, val name: String, val size: Long, val mtime: Double, val root: String)

    /** Background pass: discover, then index new/changed files newest first, then drop what's proven gone. */
    @Synchronized fun reindex() {
        if (worker?.isAlive == true) return
        worker = Thread({
            val found = mutableListOf<Entry>()
            val complete = mutableSetOf<String>()
            for (tree in folders) {
                try { walk(tree, DocumentsContract.getTreeDocumentId(tree), found); complete += tree.toString() } catch (_: Exception) {}
            }
            try { media(found); complete += "media" } catch (_: Exception) {}
            val known = HashMap<String, String>()
            db.rawQuery("SELECT uri, sig FROM files", null).use { c -> while (c.moveToNext()) known[c.getString(0)] = c.getString(1) }
            val todo = found.filter { known[it.uri.toString()] != "${it.size}:${it.mtime}" }.sortedByDescending { it.mtime }
            progress = IndexProgress(0, todo.size, false)
            todo.forEachIndexed { i, e ->
                runCatching { index(e) }
                if (i % 20 == 0) progress = IndexProgress(i + 1, todo.size, false)
            }
            val seen = found.map { it.uri.toString() }.toSet()
            db.rawQuery("SELECT uri, root FROM files", null).use { c ->
                val gone = mutableListOf<String>()
                while (c.moveToNext()) if (c.getString(0) !in seen && c.getString(1) in complete) gone += c.getString(0)
                gone.forEach(::delete)
            }
            progress = IndexProgress(todo.size, todo.size, true)
        }, "redtooth-index").apply { priority = Thread.MIN_PRIORITY; isDaemon = true; start() }
    }

    private fun walk(tree: Uri, docId: String, out: MutableList<Entry>) {
        val children = DocumentsContract.buildChildDocumentsUriUsingTree(tree, docId)
        val cols = arrayOf(Document.COLUMN_DOCUMENT_ID, Document.COLUMN_DISPLAY_NAME, Document.COLUMN_MIME_TYPE, Document.COLUMN_LAST_MODIFIED, Document.COLUMN_SIZE)
        ctx.contentResolver.query(children, cols, null, null, null)?.use { c ->
            while (c.moveToNext()) {
                val id = c.getString(0); val name = c.getString(1) ?: continue
                if (name.startsWith(".")) continue
                if (c.getString(2) == Document.MIME_TYPE_DIR) {
                    if (name !in SKIP_DIRS) walk(tree, id, out)
                } else if (kindOf(name) != "other") {
                    out += Entry(DocumentsContract.buildDocumentUriUsingTree(tree, id), name, c.getLong(4), c.getLong(3) / 1000.0, tree.toString())
                }
            }
        }
    }

    private fun media(out: MutableList<Entry>) {
        for (base in listOf(MediaStore.Images.Media.EXTERNAL_CONTENT_URI, MediaStore.Video.Media.EXTERNAL_CONTENT_URI)) {
            val cols = arrayOf(MediaStore.MediaColumns._ID, MediaStore.MediaColumns.DISPLAY_NAME, MediaStore.MediaColumns.SIZE, MediaStore.MediaColumns.DATE_MODIFIED)
            ctx.contentResolver.query(base, cols, null, null, "${MediaStore.MediaColumns.DATE_MODIFIED} DESC")?.use { c ->
                while (c.moveToNext()) out += Entry(ContentUris.withAppendedId(base, c.getLong(0)), c.getString(1) ?: continue, c.getLong(2), c.getLong(3).toDouble(), "media")
            }
        }
    }

    private fun index(e: Entry) {
        val chunks = try { extract(e) } catch (_: Exception) { emptyList() }
        db.beginTransaction()
        try {
            delete(e.uri.toString())
            val ext = "." + e.name.substringAfterLast('.', "").lowercase()
            db.execSQL("INSERT INTO files(uri,name,ext,kind,size,mtime,sig,root) VALUES(?,?,?,?,?,?,?,?)",
                arrayOf(e.uri.toString(), e.name, ext, kindOf(e.name), e.size, e.mtime, "${e.size}:${e.mtime}", e.root))
            val fid = db.rawQuery("SELECT last_insert_rowid()", null).use { it.moveToFirst(); it.getLong(0) }
            for ((label, text) in listOf("File name" to "") + chunks) {
                db.execSQL("INSERT INTO chunks(file_id,label) VALUES(?,?)", arrayOf(fid, label))
                val cid = db.rawQuery("SELECT last_insert_rowid()", null).use { it.moveToFirst(); it.getLong(0) }
                db.execSQL("INSERT INTO fts(docid,name,text) VALUES(?,?,?)", arrayOf(cid, nameWords(e.name), text))
            }
            db.setTransactionSuccessful()
        } finally {
            db.endTransaction()
        }
    }

    private fun delete(uri: String) {
        val fid = db.rawQuery("SELECT id FROM files WHERE uri=?", arrayOf(uri)).use { if (it.moveToFirst()) it.getLong(0) else null } ?: return
        db.execSQL("DELETE FROM fts WHERE docid IN (SELECT id FROM chunks WHERE file_id=?)", arrayOf(fid))
        db.execSQL("DELETE FROM chunks WHERE file_id=?", arrayOf(fid))
        db.execSQL("DELETE FROM files WHERE id=?", arrayOf(fid))
    }

    // ------------------------------------------------------------------ text extraction
    private fun extract(e: Entry): List<Pair<String, String>> {
        val ext = e.name.substringAfterLast('.', "").lowercase()
        if (e.size > 150L * 1024 * 1024) return emptyList()
        return when (ext) {
            "txt", "md", "csv", "tsv" -> ctx.contentResolver.openInputStream(e.uri)?.use { split(String(readUpTo(it, 200_000)), "") } ?: emptyList()
            "docx" -> ooxml(e.uri) { it == "word/document.xml" }.flatMap { split(it.second, "") }
            "pptx" -> ooxml(e.uri) { Regex("ppt/slides/slide\\d+\\.xml").matches(it) }
                .sortedBy { Regex("\\d+").find(it.first)!!.value.toInt() }
                .flatMap { (n, t) -> split(t, "Slide " + Regex("\\d+").find(n)!!.value) }
            "xlsx" -> ooxml(e.uri) { it == "xl/sharedStrings.xml" }.flatMap { split(it.second, "") }
            else -> emptyList() // PDFs, images, video: by name (Android has no built-in PDF text extraction)
        }
    }

    private fun ooxml(uri: Uri, want: (String) -> Boolean): List<Pair<String, String>> {
        val out = mutableListOf<Pair<String, String>>()
        ctx.contentResolver.openInputStream(uri)?.use { raw ->
            ZipInputStream(raw).use { z ->
                while (true) {
                    val entry = z.nextEntry ?: break
                    if (want(entry.name)) {
                        val xml = String(readUpTo(z, 4_000_000))
                        val text = Regex("<(?:a:t|w:t|t)(?:\\s[^>]*)?>([^<]*)</").findAll(xml).joinToString(" ") { it.groupValues[1] }
                        out += entry.name to text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", "\"").replace("&apos;", "'")
                    }
                }
            }
        }
        return out
    }

    private fun split(text: String, label: String): List<Pair<String, String>> {
        val t = text.replace(Regex("[ \\t]+"), " ").trim()
        if (t.isEmpty()) return emptyList()
        return t.chunked(900).map { label to it }
    }

    // ------------------------------------------------------------------ search
    override fun search(q: FileQuery): List<FileHit> {
        val where = mutableListOf<String>(); val args = mutableListOf<String>()
        if (q.exts.isNotEmpty()) { where += "f.ext IN (${q.exts.joinToString(",") { "?" }})"; args += q.exts }
        q.since?.let { where += "f.mtime >= ?"; args += it.toString() }
        q.until?.let { where += "f.mtime < ?"; args += it.toString() }
        val text = (listOf(q.topic) + q.names).joinToString(" ").trim()
        val w = if (where.isEmpty()) "" else " AND " + where.joinToString(" AND ")
        if (text.isEmpty()) {
            return db.rawQuery("SELECT f.id,f.uri,f.name,f.kind,f.ext,f.size,f.mtime FROM files f WHERE 1=1$w ORDER BY f.mtime DESC LIMIT 8", args.toTypedArray()).use { c ->
                generateSequence { if (c.moveToNext()) hit(c, 1.0) else null }.toList()
            }
        }
        val terms = ftsTerms(text)
        if (terms.isEmpty()) return emptyList()
        val words = Regex("[\\p{L}\\p{N}]{2,}").findAll(text.lowercase()).map { it.value }.toList()
        val best = HashMap<Long, Pair<Double, Pair<String, String>>>() // file id -> score, (label, text)
        val rows = HashMap<Long, FileHit>()
        db.rawQuery(
            "SELECT f.id,f.uri,f.name,f.kind,f.ext,f.size,f.mtime, c.label, fts.text, fts.name FROM fts JOIN chunks c ON c.id = fts.docid JOIN files f ON f.id = c.file_id " +
                "WHERE fts MATCH ?$w LIMIT 400", (listOf(terms.joinToString(" OR ")) + args).toTypedArray(),
        ).use { c ->
            while (c.moveToNext()) {
                val fid = c.getLong(0)
                val name = c.getString(9).lowercase(); val body = (c.getString(8) ?: "").lowercase()
                // Name matches count most, as on the desktop (bm25 weights the name 10x there).
                val s = words.sumOf { wd -> (if (wd in name) 3.0 else 0.0) + minOf(3, Regex(Regex.escape(wd)).findAll(body).count()) * 1.0 } / maxOf(1, words.size)
                if (s > (best[fid]?.first ?: -1.0)) { best[fid] = s to (c.getString(7) to (c.getString(8) ?: "")); rows[fid] = hit(c, 0.0) }
            }
        }
        val top = best.values.maxOfOrNull { it.first }?.takeIf { it > 0 } ?: return emptyList()
        return best.entries.sortedByDescending { it.value.first }.take(8).map { (fid, v) ->
            rows[fid]!!.apply {
                score = v.first / top
                snippetLabel = if (v.second.first == "File name") "" else v.second.first
                snippet = v.second.second.take(140)
            }
        }.filter { it.score >= 0.45 }
    }

    private fun ftsTerms(text: String): List<String> = Regex("[\\p{L}\\p{N}][\\p{L}\\p{N}\\-.]*").findAll(text).mapNotNull { m ->
        val parts = Regex("[\\p{L}\\p{N}]+").findAll(m.value).map { it.value.lowercase() }.toList()
        when {
            parts.isEmpty() -> null
            parts.size > 1 || m.value.any(Char::isDigit) -> "\"" + parts.joinToString(" ") + "\"" // codes: PRJ-2041, Q4
            parts[0].length >= 4 -> parts[0] + "*"
            else -> parts[0]
        }
    }.toList()

    private fun hit(c: android.database.Cursor, score: Double) = FileHit(c.getLong(0).toString(), c.getString(1), c.getString(2), c.getString(3), c.getString(4), c.getLong(5), c.getDouble(6), score)

    override fun recent(n: Int) = db.rawQuery("SELECT f.id,f.uri,f.name,f.kind,f.ext,f.size,f.mtime FROM files f ORDER BY f.mtime DESC LIMIT ?", arrayOf(n.toString())).use { c ->
        generateSequence { if (c.moveToNext()) hit(c, 1.0) else null }.toList()
    }

    override fun source(hit: FileHit): FileSource = UriSource(ctx, Uri.parse(hit.path), hit.name, hit.size, (hit.mtime * 1000).toLong())

    fun hitFor(uri: Uri, name: String, size: Long, mtimeMs: Long) = FileHit(uri.toString(), uri.toString(), name, kindOf(name), "." + name.substringAfterLast('.', "").lowercase(), size, mtimeMs / 1000.0, 1.0)

    companion object {
        /** InputStream.readNBytes is API 33+; this works on every supported version. */
        fun readUpTo(input: InputStream, max: Int): ByteArray {
            val out = java.io.ByteArrayOutputStream()
            val buf = ByteArray(64 * 1024)
            while (out.size() < max) {
                val n = input.read(buf, 0, minOf(buf.size, max - out.size()))
                if (n < 0) break
                out.write(buf, 0, n)
            }
            return out.toByteArray()
        }

        val SKIP_DIRS = setOf("Android", "node_modules", ".thumbnails", "cache")
        fun nameWords(name: String) = name.substringBeforeLast('.').let { "$it " + it.replace(Regex("([a-z])([A-Z])"), "$1 $2").replace(Regex("[_.]+"), " ") }
        fun kindOf(name: String): String = when (name.substringAfterLast('.', "").lowercase()) {
            "pptx", "ppt", "key", "odp" -> "presentation"
            "docx", "doc", "odt", "pages", "rtf", "md", "txt" -> "document"
            "xlsx", "xls", "csv", "numbers", "ods", "tsv" -> "spreadsheet"
            "pdf" -> "pdf"
            "jpg", "jpeg", "png", "heic", "gif", "webp" -> "image"
            "mp4", "mov", "mkv", "webm" -> "video"
            "mp3", "m4a", "wav", "flac" -> "audio"
            else -> "other"
        }
    }
}

/** A content URI as something the core can send (and resume, by seeking when the provider allows it). */
class UriSource(private val ctx: Context, private val uri: Uri, override val name: String, override val size: Long, override val lastModified: Long) : FileSource {
    override val path: String get() = uri.toString()
    override fun open(): InputStream = ctx.contentResolver.openInputStream(uri) ?: throw java.io.IOException("can't open $name")
    override fun openAt(offset: Long): InputStream {
        val pfd = ctx.contentResolver.openFileDescriptor(uri, "r") ?: return super.openAt(offset)
        val s = FileInputStream(pfd.fileDescriptor)
        return try { s.channel.position(offset); object : java.io.FilterInputStream(s) { override fun close() { super.close(); pfd.close() } } }
        catch (_: Exception) { s.close(); pfd.close(); super.openAt(offset) }
    }
}
