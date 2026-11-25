package app.redtooth.core

import java.time.Duration
import java.time.LocalTime
import java.time.ZonedDateTime

/*
 * Port of redtooth/intent.py and redtooth/timeparse.py. Behaviour is pinned to the Python
 * version by parity fixtures (src/test/resources/intent_fixtures.json, generated from it).
 * Python's \w is Unicode-aware; the patterns spell out [\p{L}\p{N}_] so results match on the
 * JVM and on Android alike.
 */
private val I = RegexOption.IGNORE_CASE

val DEVICE_WORDS: LinkedHashMap<String, List<String>> = linkedMapOf(
    "headphones" to listOf("headphones", "headphone", "headset", "cans", "earphones", "earbuds", "buds", "airpods", "xm5s", "xm5", "xm4s", "xm4", "qc45s", "qc45", "beats"),
    "speaker" to listOf("speaker", "speakers", "soundbar", "homepod", "sonos"),
    "laptop" to listOf("laptop", "macbook", "notebook", "computer", "pc", "mac"),
    "desktop" to listOf("desktop", "imac", "workstation"),
    "phone" to listOf("phone", "iphone", "pixel", "galaxy", "mobile", "cell"),
    "tablet" to listOf("ipad", "tablet"),
)
val MODEL_HINTS = listOf("airpods max", "airpods pro", "airpods", "xm5", "xm4", "sony", "bose", "qc45", "beats", "jabra", "galaxy buds", "pixel buds", "jbl")
val FILE_KINDS: LinkedHashMap<String, Pair<List<String>, Set<String>>> = linkedMapOf(
    "presentation" to (listOf("deck", "decks", "presentation", "presentations", "slides", "slide deck", "keynote", "powerpoint", "pitch") to setOf(".pptx", ".ppt", ".key", ".odp")),
    "document" to (listOf("doc", "docs", "document", "documents", "memo", "write-up", "writeup", "draft", "letter", "word doc") to setOf(".docx", ".doc", ".odt", ".pages", ".md", ".txt", ".rtf", ".pdf")),
    "spreadsheet" to (listOf("spreadsheet", "sheet", "sheets", "excel", "budget", "model", "csv") to setOf(".xlsx", ".xls", ".csv", ".numbers", ".ods")),
    "pdf" to (listOf("pdf", "pdfs") to setOf(".pdf")),
    "image" to (listOf("photo", "photos", "picture", "pictures", "image", "images", "screenshot", "screenshots", "pic", "pics") to setOf(".jpg", ".jpeg", ".png", ".heic", ".gif", ".webp")),
    "video" to (listOf("video", "videos", "recording", "clip", "movie") to setOf(".mp4", ".mov", ".mkv", ".webm")),
    "audio" to (listOf("song", "track", "audio file", "voice memo") to setOf(".mp3", ".m4a", ".wav", ".flac")),
)
private val TOPICAL_KIND_WORDS = setOf("budget", "pitch", "memo", "letter", "recording", "model", "screenshot", "keynote")
val PRONOUN_PEOPLE = setOf("he", "him", "his", "she", "her", "hers", "they", "them", "their")
private val NOT_NAMES = setOf("it", "that", "what", "let", "there", "here", "who", "he", "she", "where", "today", "everyone", "someone", "one")
private val STOP = """a an the my me i to for of on in at with and or is was were be this that these those it its from by about
    please can could would you hey ok okay just some file files thing stuff one i'd i'm send share find search look
    for show open get give edited modified updated changed worked made created saved wrote last latest recent new
    over across which where whats what's called named titled regarding re related topic copy version instead
    connect friend friends mine our his her their him them up are those these yes no""".split(Regex("\\s+")).filter { it.isNotEmpty() }.toSet()
private val CONFIRM = Regex("^(yes|yeah|yep|yup|ya|sure|correct|right|ok|okay|exactly|that'?s (right|them|it|correct|the one)|those|that one|affirmative|do it|go ahead)\\b", I)
private val DENY = Regex("^(no|nope|nah|wrong|not (those|that|them|it|these)|neither|cancel)\\b", I)
private val ORDINALS = mapOf("first" to 0, "1st" to 0, "one" to 0, "second" to 1, "2nd" to 1, "two" to 1, "third" to 2, "3rd" to 2, "three" to 2, "fourth" to 3, "last" to -1)
private val REL = Regex("\\bmy (friend|coworker|colleague|brother|sister|mom|dad|partner|roommate|boss)\\b", I)

data class FileQuery(
    val kinds: MutableList<String> = mutableListOf(),
    val exts: MutableSet<String> = mutableSetOf(),
    var topic: String = "",
    var since: Double? = null,
    var until: Double? = null,
    var timeLabel: String? = null,
    var refersPrevious: Boolean = false,
    var names: List<String> = emptyList(),
) {
    fun describe(): String {
        val noun = kinds.firstOrNull()?.let { mapOf("presentation" to "deck", "spreadsheet" to "spreadsheet", "image" to "photo")[it] ?: it } ?: "file"
        return listOfNotNull(noun, topic.takeIf { it.isNotEmpty() }?.let { "about “$it”" }, timeLabel?.let { "edited $it" }).joinToString(" ")
    }

    fun isEmpty() = kinds.isEmpty() && topic.isEmpty() && since == null && names.isEmpty()
}

data class Intent(
    val action: String, // connect | disconnect | send | find | retarget | confirm | deny | choose | unknown
    val text: String,
    var person: String? = null,
    var pronoun: Boolean = false,
    val relation: String? = null,
    val deviceKind: String? = null,
    val deviceHint: String? = null,
    val file: FileQuery? = null,
    val choice: Int? = null,
    val instead: Boolean = false,
)

fun relationHint(text: String): String? {
    val m = REL.find(text) ?: return null
    val rel = m.groupValues[1].lowercase()
    return if (rel == "colleague") "coworker" else rel
}

private fun norm(text: String): String {
    var t = text.replace("’", "'").replace("‘", "'").replace("“", "\"").replace("”", "\"")
    t = t.replaceFirst(Regex("^\\s*(hey|hi|ok|okay)?\\s*(redtooth|claude|siri)?[,!]?\\s*", I), "")
    t = t.replaceFirst(Regex("^\\s*(please|can you|could you|would you|i want to|i'd like to|let's|i need to|help me)\\s+", I), "")
    t = t.replaceFirst(Regex("^\\s*(please)\\s+", I), "")
    return t.trim().trimEnd('?', '.', '!')
}

private fun findWord(t: String, words: Iterable<String>): String? =
    words.sortedByDescending { it.length }.firstOrNull { Regex("\\b" + Regex.escape(it) + "\\b").containsMatchIn(t) }

object IntentParser {
    fun parse(text: String, knownNames: Iterable<String> = emptyList(), now: ZonedDateTime = ZonedDateTime.now()): Intent {
        val raw = text
        val t = norm(text)
        val low = t.lowercase()
        val names = knownNames.filter { it.isNotEmpty() }.map { it.lowercase() }.toSet()

        val words = low.split(Regex("\\s+")).filter { it.isNotEmpty() }
        if (words.size <= 4 && CONFIRM.containsMatchIn(low) && !Regex("\\b(send|connect|find)\\b").containsMatchIn(low)) return Intent("confirm", raw)
        if (words.size <= 4 && DENY.containsMatchIn(low)) return Intent("deny", raw)
        Regex("^(?:the |number |option |#)?(\\d|first|second|third|fourth|last|1st|2nd|3rd)(?: one)?$").find(low)?.let {
            val v = it.groupValues[1]
            return Intent("choose", raw, choice = if (v.all(Char::isDigit)) v.toInt() - 1 else ORDINALS[v])
        }

        var deviceKind: String? = null
        for ((kind, ws) in DEVICE_WORDS) if (findWord(low, ws) != null) { deviceKind = kind; break }
        val deviceHint = findWord(low, MODEL_HINTS)

        var person: String? = null
        var pronoun = false
        val rel = relationHint(low)
        if (rel != null) {
            Regex("\\bmy " + Regex.escape(rel) + "s? ([a-z][\\p{L}\\p{N}_\\-]+?)(?:'s?\\b|\\b)").find(low.replace("colleague", "coworker"))?.let { person = it.groupValues[1] }
        }
        val allDeviceWords = DEVICE_WORDS.values.flatten()
        if (person == null) {
            for (m in Regex("\\b([a-z][\\p{L}\\p{N}_\\-]*)'s?\\b").findAll(low)) {
                val cand = m.groupValues[1]
                // Python's 's? needs the apostrophe; Kotlin's regex is the same pattern.
                if (cand !in NOT_NAMES && (names.isEmpty() || cand in names || findWord(cand, allDeviceWords) == null)) { person = cand; break }
            }
        }
        if (person == null && names.isNotEmpty()) {
            person = names.sortedByDescending { it.length }.firstOrNull { Regex("\\b" + Regex.escape(it) + "\\b").containsMatchIn(low) }
        }
        if (person == null) {
            val m = Regex("\\b(?:send|share|give|forward|airdrop|text)\\s+([a-z][\\p{L}\\p{N}_\\-]+)\\s+(?:the|my|a|an|that|this|those|these|it|some|our)\\b").find(low)
                ?: Regex("\\b(?:to|with)\\s+([a-z][\\p{L}\\p{N}_\\-]+)\\b(?!\\s*(?:laptop|phone|mac|ipad))").find(low)
            if (m != null && m.groupValues[1] !in STOP && m.groupValues[1] !in PRONOUN_PEOPLE) person = m.groupValues[1]
        }
        if (person == null) {
            val pm = Regex("\\b(his|her|their|him|them)\\b").find(low)
            if (pm != null) { person = pm.groupValues[1]; pronoun = true }
            else if (Regex("\\bmy (own )?(headphones|airpods|earbuds|buds|speaker|laptop|phone)\\b").containsMatchIn(low)) person = "my"
        }

        val instead = Regex("\\binstead\\b|\\brather\\b").containsMatchIn(low)

        var action = when {
            Regex("\\bdisconnect\\b").containsMatchIn(low) -> "disconnect"
            Regex("^(send|share|airdrop|give|forward|transfer|drop|text|email)\\b").containsMatchIn(low) || Regex("\\b(send|share|airdrop)\\b").containsMatchIn(low) -> "send"
            Regex("^(find|search|look for|look up|where'?s|where is|locate|show me|pull up|open|get me|which)\\b").containsMatchIn(low) -> "find"
            Regex("\\b(connect|pair|hook up|switch (?:audio|sound|output) to|play (?:it |this |music )?(?:on|through)|listen (?:on|with)|use)\\b").containsMatchIn(low) -> "connect"
            instead || (pronoun && deviceKind != null) -> "retarget"
            deviceKind == "headphones" || deviceKind == "speaker" -> "connect"
            else -> "unknown"
        }
        if (action == "send" && instead && Regex("\\b(it|that|this)\\b").containsMatchIn(low)) action = "retarget"

        var fq: FileQuery? = null
        if (action in setOf("send", "find", "retarget", "unknown")) {
            fq = FileQuery()
            val tr = TimeParse.parse(t, now)
            fq.since = tr.since; fq.until = tr.until; fq.timeLabel = tr.label
            val rest = tr.rest
            val rl = rest.lowercase()
            for ((kind, pair) in FILE_KINDS) if (findWord(rl, pair.first) != null) { fq.kinds.add(kind); fq.exts.addAll(pair.second) }
            fq.names = Regex("\"([^\"]+)\"").findAll(rest).map { it.groupValues[1] }.toList()
            val about = Regex("\\b(?:about|regarding|on the topic of|related to|re:|covering|on)\\s+(.+)$", I).find(rest)
            var topicSrc = about?.groupValues?.get(1) ?: rest
            val recipients = (names + setOf("him", "her", "them", "his", "their")).joinToString("|") { Regex.escape(it) }
            topicSrc = topicSrc.split(Regex("\\b(?:to|with|for)\\s+(?:my |his |her |their )?(?:$recipients)\\b", I), limit = 2)[0]
            val p = person
            if (p != null && p !in PRONOUN_PEOPLE && p != "my") topicSrc = topicSrc.replace(Regex("\\b" + Regex.escape(p) + "('s)?\\b", I), " ")
            val allKindWords = (FILE_KINDS.values.flatMap { it.first } + allDeviceWords).toSet() - TOPICAL_KIND_WORDS
            val toks = Regex("[\\p{L}\\p{N}_][\\p{L}\\p{N}_\\-.]*").findAll(topicSrc).map { it.value }
                .filter { it.lowercase() !in STOP && it.lowercase() !in allKindWords && it.lowercase() !in PRONOUN_PEOPLE }.toList()
            fq.topic = toks.joinToString(" ").trim()
            if (fq.topic.isEmpty() && fq.kinds.isEmpty() && fq.names.isEmpty() && (Regex("\\b(it|that|this|the file|same)\\b").containsMatchIn(low) || action == "retarget")) fq.refersPrevious = true
            if (action == "unknown" && !fq.isEmpty() && person != null) action = "send"
        }
        return Intent(action, raw, person, pronoun || (person != null && person in PRONOUN_PEOPLE), rel, deviceKind, deviceHint, fq, null, instead)
    }
}

/** Relative time phrases -> [since, until) in local time (port of timeparse.py). */
object TimeParse {
    data class Result(val since: Double?, val until: Double?, val label: String?, val rest: String)

    private val WEEKDAYS = listOf("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")

    private fun at(day: ZonedDateTime, h: Int, m: Int = 0) = day.with(LocalTime.of(h, m)).withNano(0)
    private fun ts(d: ZonedDateTime) = d.toEpochSecond() + d.nano / 1e9

    fun parse(text: String, now: ZonedDateTime = ZonedDateTime.now()): Result {
        val t = text.lowercase()
        val today0 = at(now, 0)
        val yday = now.minusDays(1)
        val weekday = now.dayOfWeek.value - 1 // Monday = 0, as in Python
        val rules: List<Pair<String, () -> Triple<ZonedDateTime, ZonedDateTime, String>>> = listOf(
            "\\bthis morning\\b" to { Triple(today0, if (now.hour >= 12) minOf(now, at(now, 12)) else now, "this morning") },
            "\\bthis afternoon\\b" to { Triple(at(now, 12), minOf(now, at(now, 18)), "this afternoon") },
            "\\b(this evening|tonight)\\b" to { Triple(at(now, 17), now, "this evening") },
            "\\blast night\\b" to { Triple(at(yday, 17), at(now, 5), "last night") },
            "\\byesterday morning\\b" to { Triple(at(yday, 0), at(yday, 12), "yesterday morning") },
            "\\byesterday afternoon\\b" to { Triple(at(yday, 12), at(yday, 18), "yesterday afternoon") },
            "\\byesterday\\b" to { Triple(today0.minusDays(1), today0, "yesterday") },
            "\\b(today|earlier today)\\b" to { Triple(today0, now, "today") },
            "\\b(just now|a few minutes ago|a minute ago|minutes ago)\\b" to { Triple(now.minusMinutes(30), now, "in the last 30 minutes") },
            "\\b(an hour ago|an hour back|in the last hour)\\b" to { Triple(now.minusHours(2), now, "in the last couple of hours") },
            "\\b(recently|lately)\\b" to { Triple(now.minusDays(3), now, "recently") },
            "\\bthis week\\b" to { Triple(today0.minusDays(weekday.toLong()), now, "this week") },
            "\\blast week\\b" to { Triple(today0.minusDays(weekday + 7L), today0.minusDays(weekday.toLong()), "last week") },
            "\\bthis month\\b" to { Triple(today0.withDayOfMonth(1), now, "this month") },
        )
        for ((pat, fn) in rules) {
            val m = Regex(pat).find(t) ?: continue
            val (s, u, label) = fn()
            return Result(ts(s), ts(u), label, cut(text, m.range))
        }
        Regex("\\b(?:on |last )?(" + WEEKDAYS.joinToString("|") + ")\\b").find(t)?.let { m ->
            val wd = WEEKDAYS.indexOf(m.groupValues[1])
            val delta = ((weekday - wd) % 7 + 7) % 7
            val day = today0.minusDays((if (delta == 0) 7 else delta).toLong())
            return Result(ts(day), ts(day.plusDays(1)), "on " + m.groupValues[1].replaceFirstChar { it.uppercase() }, cut(text, m.range))
        }
        Regex("\\b(\\d+) (hours?|days?) ago\\b").find(t)?.let { m ->
            val n = m.groupValues[1].toLong()
            val unit = if (m.groupValues[2].startsWith("hour")) Duration.ofHours(1) else Duration.ofDays(1)
            val center = now.minus(unit.multipliedBy(n))
            return Result(ts(center.minus(unit)), ts(center.plus(unit)), m.value, cut(text, m.range))
        }
        return Result(null, null, null, text)
    }

    private fun cut(text: String, r: IntRange) = (text.substring(0, r.first) + text.substring(r.last + 1)).trim()
}
