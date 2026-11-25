package app.redtooth.core

import java.io.File
import kotlin.math.exp
import kotlin.math.max
import kotlin.math.pow

/**
 * The ranking rules shared with the desktop (redtooth/files/index.py, usage.py): how much of the request
 * a file covers, a small recency prior, and what you actually use. Checked against
 * ranking_fixtures.json from tools/make_ranking_fixtures.py.
 */
object Ranking {
    val STOP = setOf("the", "and", "for", "about", "with", "from", "that", "this", "file", "doc", "document", "deck", "my", "our", "your")
    private val WORD = Regex("[\\p{L}\\p{N}_]+")

    fun words(text: String): List<String> = WORD.findAll(text).map { it.value }.toList()

    /** Query words that count: 3+ letters, not filler. */
    fun queryWords(text: String): List<String> = words(text).map { it.lowercase() }.filter { it.length >= 3 && it !in STOP }

    fun stem(w: String) = if (w.length > 4 && w.endsWith("s")) w.dropLast(1) else w

    fun nameWords(name: String): String {
        val stemName = name.substringBeforeLast('.').ifEmpty { name }
        val spaced = stemName.replace(Regex("([a-z])([A-Z])"), "$1 $2")
        return "$stemName ${spaced.replace(Regex("[_.]+"), " ")}"
    }

    /** "term paper" beats a file that only says "term" or only "paper". Same numbers as the desktop. */
    fun coverageBonus(qws: List<String>, name: String, chunk: String): Double {
        if (qws.isEmpty()) return 0.0
        val compactQ = qws.joinToString("")
        val nwords = words(nameWords(name).lowercase()).map(::stem).toSet()
        val compactName = name.lowercase().replace(Regex("[\\s_\\-.]+"), "")
        val want = qws.map(::stem)
        var inName = want.count { it in nwords }.toDouble() / want.size
        if (qws.size >= 2 && compactQ in compactName) inName = 1.0
        val text = chunk.lowercase()
        val twords = words(text).map(::stem).toSet()
        val inText = if (text.isNotEmpty()) want.count { it in twords }.toDouble() / want.size else 0.0
        val phrase = qws.size >= 2 && (qws.joinToString(" ") in nameWords(name).lowercase() || qws.joinToString(" ") in text.replace(Regex("\\s+"), " "))
        var bonus = 0.015 * inName + (if (inName == 1.0 && qws.size >= 2) 0.03 else 0.0)
        bonus += 0.008 * inText + (if (inText == 1.0 && qws.size >= 2) 0.006 else 0.0)
        bonus += if (phrase) 0.01 else 0.0
        return bonus
    }

    /** Newer files win ties: halves every six weeks. */
    fun recency(mtime: Double, nowS: Double = System.currentTimeMillis() / 1000.0, weight: Double = 0.006): Double =
        weight * 0.5.pow(max(0.0, (nowS - mtime) / 86400) / 42)

    /** With a re-ranker: its relevance leads; the earlier ranking, usage and recency adjust it. */
    fun rerankScore(prob: Double, fusedNorm: Double, usage: Double, pick: Double, ageDays: Double): Double =
        0.70 * prob + 0.20 * fusedNorm + 0.15 * usage + 0.30 * pick + 0.03 * 0.5.pow(ageDays / 42)
}

/**
 * What you use: files sent or picked in Redtooth, files the OS says you opened (the app adds those),
 * and which file you picked for a request before. Two-week half-life. Kept as a small JSON file.
 */
class UsageStore(private val file: File? = null, private val nowS: () -> Double = { System.currentTimeMillis() / 1000.0 }) {
    private data class Event(val path: String, val kind: String, val at: Double)
    private val events = mutableListOf<Event>()
    private val picks = mutableMapOf<String, MutableMap<String, Int>>() // query -> path -> times
    /** Recently opened files from the platform (Android: MediaStore / the app's own opens). */
    @Volatile var osRecent: () -> Map<String, Double> = { emptyMap() }

    init { load() }

    @Synchronized fun record(path: String, kind: String) { events += Event(path, kind, nowS()); save() }

    @Synchronized fun recordPick(query: String, path: String) {
        val q = normQuery(query)
        if (q.isEmpty()) return
        picks.getOrPut(q) { mutableMapOf() }.merge(path, 1, Int::plus)
        events += Event(path, "picked", nowS())
        save()
    }

    /** path -> (usage 0..1, picked-for-this-request 0..1, reasons). */
    @Synchronized fun signals(paths: List<String>, query: String = ""): Map<String, Triple<Double, Double, List<String>>> {
        val now = nowS()
        val recent = runCatching { osRecent() }.getOrDefault(emptyMap())
        val q = normQuery(query)
        return paths.associateWith { p ->
            var raw = 0.0
            val why = mutableListOf<String>()
            for (e in events) if (e.path == p) {
                raw += (WEIGHTS[e.kind] ?: 0.5) * 0.5.pow(max(0.0, now - e.at) / 86400 / HALF_LIFE_DAYS)
                if (e.kind == "sent" && "sent before" !in why) why += "sent before"
            }
            recent[p]?.let { raw += WEIGHTS.getValue("opened") * 0.5.pow(max(0.0, now - it) / 86400 / HALF_LIFE_DAYS); why += "opened recently" }
            val n = picks[q]?.get(p) ?: 0
            val pick = 1.0 - 0.5.pow(n)
            if (pick > 0) why.add(0, "you picked it for this before")
            Triple(1.0 - exp(-raw), pick, why.toList())
        }
    }

    private fun load() {
        val f = file ?: return
        if (!f.exists()) return
        runCatching {
            val o = Json.obj(f.readText())
            (o["events"] as? List<*>)?.forEach { e -> (e as? Map<*, *>)?.let { events += Event(it["path"] as String, it["kind"] as String, (it["at"] as Number).toDouble()) } }
            (o["picks"] as? Map<*, *>)?.forEach { (q, m) -> picks[q as String] = (m as Map<*, *>).entries.associate { it.key as String to (it.value as Number).toInt() }.toMutableMap() }
        }
    }

    private fun save() {
        val f = file ?: return
        val cutoff = nowS() - 180 * 86400  // forget after six months
        events.removeAll { it.at < cutoff }
        runCatching {
            f.parentFile?.mkdirs()
            f.writeText(Json.encode(mapOf("events" to events.map { mapOf("path" to it.path, "kind" to it.kind, "at" to it.at) }, "picks" to picks)))
        }
    }

    companion object {
        const val HALF_LIFE_DAYS = 14.0
        val WEIGHTS = mapOf("sent" to 1.0, "picked" to 0.8, "opened" to 0.6)
        fun normQuery(q: String) = Ranking.words(q.lowercase()).joinToString(" ") { if (it.length > 4 && it.endsWith("s")) it.dropLast(1) else it }
    }
}
