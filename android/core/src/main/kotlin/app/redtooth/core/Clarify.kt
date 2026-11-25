package app.redtooth.core

import java.time.ZonedDateTime

/**
 * When to ask "which file?", and asking about what actually differs (same rules as redtooth/clarify.py):
 * versions by their version word or date, formats by type, the same file by folder, different files by
 * the words only each has.
 */
object Clarify {
    val VERSION_WORDS = listOf("final", "draft", "revised", "edited", "signed", "submitted", "old", "new", "copy", "backup", "v1", "v2", "v3", "v4",
        "notes", "outline", "rough")
    val TYPE_NAMES = mapOf(".docx" to "Word doc", ".doc" to "Word doc", ".pdf" to "PDF", ".pptx" to "deck", ".key" to "Keynote deck",
        ".xlsx" to "spreadsheet", ".csv" to "CSV", ".txt" to "text file", ".md" to "notes file", ".pages" to "Pages doc", ".odt" to "document", ".rtf" to "document")

    fun ambiguous(hits: List<FileHit>, hasTopic: Boolean): Boolean {
        if (hits.size < 2) return false
        if (!hasTopic) return true
        val (a, b) = hits[0] to hits[1]
        if (a.reasons.firstOrNull()?.startsWith("you picked it") == true) return false
        val ca = a.confidence
        val cb = b.confidence
        if (ca != null && cb != null) {
            if (ca < 0.15) return true
            return cb >= 0.3 && (a.score - b.score) < 0.15
        }
        return b.score >= 0.85
    }

    private fun words(name: String, keepCase: Boolean = false): List<String> {
        val stem = name.substringBeforeLast('.').ifEmpty { name }.replace(Regex("([a-z])([A-Z])"), "$1 $2")
        val ws = stem.split(Regex("[\\s_\\-.()\\[\\]]+")).filter { it.isNotEmpty() }
        return if (keepCase) ws else ws.map { it.lowercase() }
    }

    private fun core(name: String) = words(name).filter { it !in VERSION_WORDS && !Regex("\\d{1,4}|v\\d+|copy").matches(it) }.joinToString(" ")

    fun folder(path: String): String {
        val parts = path.split('/', '\\')
        return when {
            parts.size >= 3 -> parts.subList(parts.size - 3, parts.size - 1).joinToString(" › ")
            parts.size == 2 -> parts[0]
            else -> ""
        }
    }

    private fun join(parts: List<String>) = if (parts.size == 2) "${parts[0]} or ${parts[1]}" else parts.dropLast(1).joinToString(", ") + " or " + parts.last()

    fun distinguish(hits: List<FileHit>, now: ZonedDateTime = ZonedDateTime.now()): Pair<String, List<String>> {
        val hs = hits.take(3)
        val exts = hs.map { it.ext.lowercase() }
        val cores = hs.map { core(it.name) }
        val ws = hs.map { words(it.name).toSet() }
        val dates = hs.map { Agent.whenLabel(it.mtime, now) }
        if (cores.toSet().size == 1 && exts.toSet().size == hs.size) {
            val phr = exts.map { "the ${TYPE_NAMES[it] ?: it.trimStart('.').uppercase()}" }
            return "Which one: ${join(phr)}?" to phr
        }
        if (cores.toSet().size == 1 || ws.drop(1).all { (it intersect ws[0]).size >= maxOf(1, ws[0].size / 2) }) {
            val tags = ws.map { w -> VERSION_WORDS.firstOrNull { it in w } }
            val phr = when {
                tags.all { it != null } && tags.toSet().size == tags.size ->
                    tags.indices.map { i -> "the ${tags[i]} (${if (i == 0) "edited " else ""}${dates[i]})" }
                dates.toSet().size == dates.size -> dates.map { "the one from $it" }
                else -> {
                    val folders = hs.map { folder(it.path) }
                    if (folders.toSet().size == folders.size) folders.map { "the one in $it" } else hs.map { it.name.substringBeforeLast('.') }
                }
            }
            return "Which one: ${join(phr)}?" to phr
        }
        val common = ws.reduce { a, b -> a intersect b }
        val phr = hs.map { h ->
            val own = words(h.name, keepCase = true).filter { it.lowercase() !in common }.distinct()
            val label = own.joinToString(" ").take(40).trim().ifEmpty { h.name.substringBeforeLast('.') }
            "“$label”"
        }
        return "Which one: ${join(phr)}?" to phr
    }

    fun optionDetail(h: FileHit, now: ZonedDateTime = ZonedDateTime.now()): String {
        val bits = mutableListOf("Edited ${Agent.whenLabel(h.mtime, now)}")
        folder(h.path).takeIf { it.isNotEmpty() }?.let { bits += it }
        when {
            h.reasons.isNotEmpty() -> bits += h.reasons[0]
            h.snippetLabel.isNotEmpty() -> bits += h.snippetLabel
        }
        return bits.joinToString(" · ")
    }
}
