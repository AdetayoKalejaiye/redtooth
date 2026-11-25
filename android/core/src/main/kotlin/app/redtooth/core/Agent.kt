package app.redtooth.core

import java.time.ZonedDateTime
import java.util.UUID
import java.util.concurrent.Callable
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors
import java.util.concurrent.Future
import java.util.concurrent.TimeUnit

/** File search the platform provides (Android: an FTS index over granted folders + MediaStore). */
interface FileSearch {
    fun search(q: FileQuery): List<FileHit>
    fun recent(n: Int = 4): List<FileHit>
    fun source(hit: FileHit): FileSource?
    fun indexing(): IndexProgress? = null
}

data class IndexProgress(val done: Int, val queued: Int, val complete: Boolean)

class Context {
    var person: Person? = null
    var file: FileHit? = null
    var files: List<FileHit> = emptyList()
    var device: Device? = null
    var pending: Question? = null
    var lastTransferId: String? = null
    var updated: Long = System.currentTimeMillis()
    fun touch() { updated = System.currentTimeMillis() }
    fun stale() = System.currentTimeMillis() - updated > 30 * 60_000
}

/**
 * The agent (port of agent.py): turns a request into actions, asks only specific questions,
 * and carries the selected file, person and device forward. Calls block; Android invokes it
 * from a background dispatcher. Asynchronous replies (a device that showed up after a pairing
 * nudge) arrive through [onEvent] as ("reply", Reply).
 */
class Agent(
    val me: Person,
    val people: PeopleResolver,
    val directory: Directory,
    val bt: BluetoothPort,
    val files: FileSearch,
    val remembered: RememberedStore,
    val sender: Sender,
    private val onEvent: (String, Any?) -> Unit = { _, _ -> },
    scanSeconds: Double = 4.0,
) {
    val devices = DeviceResolver(bt, remembered, me.copy(isSelf = true), scanSeconds)
    var ctx = Context()
    private val pool = Executors.newCachedThreadPool { r -> Thread(r, "redtooth-agent").apply { isDaemon = true } }
    private val routes = ConcurrentHashMap<String, Future<Endpoint?>>()
    @Volatile private var watch: Thread? = null
    private var forcedPerson: Person? = null

    /** Extra way to reach a device when no LAN endpoint answers (Android: Wi-Fi Aware). */
    @Volatile var routeFallback: ((Device) -> Endpoint?)? = null

    init {
        sender.route = { routeFor(it) }
    }

    private val destOrder = mapOf("phone" to 0, "laptop" to 1, "tablet" to 2, "desktop" to 3)
    private val kindNoun = mapOf("presentation" to "deck", "document" to "document", "spreadsheet" to "spreadsheet", "pdf" to "PDF", "image" to "photo", "video" to "video")

    fun knownNames(): List<String> = people.contacts.flatMap { listOf(it.name, it.name.split(" ").first()) + it.nicknames }

    fun refreshPeople() {
        val phones = people.contacts.flatMap { it.phones }
        runCatching { people.profiles = if (phones.isEmpty()) emptyMap() else directory.lookup(phones) }
    }

    /** Start checking routes now; the sender awaits the same result. */
    fun prefetchRoute(d: Device): Future<Endpoint?> = routes.compute(d.id) { _, old -> if (old != null && !old.isDone) old else pool.submit(Callable { Routes.best(d) }) }!!

    fun routeFor(d: Device): Endpoint? =
        runCatching { (routes.remove(d.id) ?: prefetchRoute(d)).get(4, TimeUnit.SECONDS) }.getOrNull() ?: Routes.best(d) ?: routeFallback?.invoke(d)

    // ------------------------------------------------------------------ entry points
    fun ask(text: String, now: ZonedDateTime = ZonedDateTime.now()): Reply {
        val t0 = System.nanoTime()
        if (ctx.stale()) ctx = Context()
        val intent = IntentParser.parse(text, knownNames(), now)
        ctx.pending?.let { q -> maybeAnswer(q, intent, text)?.let { return it } }
        val r = when (intent.action) {
            "connect" -> connect(intent)
            "disconnect" -> disconnect()
            "send" -> send(intent)
            "find" -> find(intent)
            "retarget" -> retarget(intent)
            "confirm", "deny", "choose" -> Reply("There's nothing waiting on an answer right now.", ok = false)
            else -> Reply("Try “Connect to Daniel's headphones”, “Send Daniel the deck I edited this morning”, or “Find the deck about customer churn”.", ok = false)
        }
        r.timings.putIfAbsent("total_ms", (System.nanoTime() - t0) / 1e6)
        return r
    }

    fun answer(questionId: String, optionId: String): Reply {
        val q = ctx.pending
        if (q == null || q.id != questionId) return Reply("That question has expired.", ok = false)
        val opt = q.options.firstOrNull { it.id == optionId } ?: return Reply("That isn't one of the options.", ok = false)
        return resolvePending(q, opt)
    }

    fun selectFile(hit: FileHit): Reply {
        ctx.file = hit; ctx.files = listOf(hit); ctx.touch()
        return Reply("${hit.name} is ready to send. Who should get it?", files = listOf(hit))
    }

    fun sendFile(personId: String, deviceId: String? = null): Reply {
        val person = people.byId(personId) ?: return Reply("Unknown person.", ok = false)
        val hit = ctx.file
        if (hit == null) {
            ctx.person = person
            destinations(person).forEach { prefetchRoute(it) }
            return Reply("Which file should I send to ${person.first}?", person = person)
        }
        return startSend(hit, person, person.profile?.devices?.firstOrNull { it.id == deviceId })
    }

    fun connectDevice(deviceId: String): Reply {
        val pool = (bt.paired() + remembered.remembered()).associateBy { it.id }
        val dev = pool[deviceId] ?: return Reply("That device isn't around anymore.", ok = false)
        val rem = remembered.remembered().firstOrNull { it.id == deviceId }
        val owner = rem?.ownerPhone?.takeIf { it != me.phone }?.let { people.byPhone(it) } ?: me
        dev.paired = bt.paired().any { it.id == dev.id }
        return doConnect(dev, owner)
    }

    // ------------------------------------------------------------------ pending questions
    private fun maybeAnswer(q: Question, intent: Intent, text: String): Reply? {
        val opts = q.options.filter { it.id != "no" && it.id != "none" }
        if (intent.action == "choose" && intent.choice != null && opts.isNotEmpty()) {
            val i = intent.choice
            return resolvePending(q, if (i >= -opts.size && i < opts.size) opts[(i + opts.size) % opts.size] else opts.last())
        }
        if (intent.action == "confirm") (q.options.firstOrNull { it.id == "yes" } ?: q.options.singleOrNull())?.let { return resolvePending(q, it) }
        if (intent.action == "deny") q.options.firstOrNull { it.id == "no" || it.id == "none" }?.let { return resolvePending(q, it) }
        if (intent.action in setOf("unknown", "find", "choose") || (intent.action == "connect" && q.kind in setOf("choose_device", "confirm_device"))) {
            val low = text.lowercase()
            var best: Option? = null; var bestS = 0.0
            for (o in q.options) {
                val ws = o.label.lowercase().replace("—", " ").split(" ").filter { it.length > 2 }
                var s = ws.count { it in low }.toDouble() / maxOf(1, ws.size)
                s = maxOf(s, PeopleResolver.ratio(low, o.label.lowercase()) * 0.8)
                if (s > bestS) { best = o; bestS = s }
            }
            if (best != null && bestS >= 0.34) return resolvePending(q, best)
        }
        return null
    }

    private fun resolvePending(q: Question, opt: Option): Reply {
        ctx.pending = null
        val st = q.state
        return when (q.kind) {
            "choose_file" -> {
                val hit = opt.payload as FileHit
                ctx.file = hit; ctx.touch()
                val p = st["person"] as? Person
                if (p != null) startSend(hit, p, st["device"] as? Device) else Reply("Selected ${hit.name}. Who should I send it to?", files = listOf(hit))
            }
            "choose_person" -> {
                val p = opt.payload as Person
                ctx.person = p
                forcedPerson = p
                val intent = st["intent"] as Intent
                when (intent.action) { "connect" -> connect(intent); "retarget" -> retarget(intent); else -> send(intent) }
            }
            "confirm_device" -> {
                val owner = st["owner"] as Person
                val dev = st["device"] as Device
                if (opt.id == "yes") doConnect(dev, owner) else {
                    @Suppress("UNCHECKED_CAST") val rest = (st["candidates"] as? List<Device> ?: emptyList()).filter { it.id != dev.id }
                    if (rest.isNotEmpty()) askDevice(owner, rest, st) else pairingMode(owner, st["kind"] as? String, st["hint"] as? String)
                }
            }
            "choose_device" -> {
                val owner = st["owner"] as Person
                if (opt.id == "none") pairingMode(owner, st["kind"] as? String, st["hint"] as? String) else doConnect(opt.payload as Device, owner)
            }
            else -> Reply("Okay.")
        }
    }

    // ------------------------------------------------------------------ people
    private fun person(intent: Intent, defaultSelf: Boolean): Pair<Person?, Reply?> {
        forcedPerson?.let { forcedPerson = null; return it to null }
        if (intent.pronoun || (intent.person == null && ctx.person != null && !defaultSelf)) {
            return (ctx.person ?: return null to Reply("Who do you mean?", ok = false)) to null
        }
        val name = intent.person ?: return if (defaultSelf) me to null else null to Reply("Who should I send it to?", ok = false)
        if (people.profiles.isEmpty()) refreshPeople()
        val c = people.resolve(name, intent.relation)
        if (c.isEmpty()) return null to Reply("I couldn't find “${name.replaceFirstChar { it.uppercase() }}” in your contacts.", ok = false)
        if (c[0].person.isSelf) return me to null
        if (PeopleResolver.isAmbiguous(c)) {
            val opts = c.take(3).map { cand ->
                val bits = listOfNotNull(cand.person.contact.relation, "nearby".takeIf { cand.person.nearby }, "uses Redtooth".takeIf { cand.person.hasApp })
                Option(cand.person.contact.id, cand.person.name, bits.joinToString(" · "), cand.person)
            }
            val q = Question(newId("q_"), "choose_person", "Which ${name.replaceFirstChar { it.uppercase() }}?", opts, mutableMapOf("intent" to intent))
            ctx.pending = q
            return null to Reply(q.prompt, question = q)
        }
        ctx.person = c[0].person; ctx.touch()
        return c[0].person to null
    }

    // ------------------------------------------------------------------ connect
    private fun connect(intent: Intent): Reply {
        val (owner, err) = person(intent, defaultSelf = true)
        if (err != null) return err
        owner!!
        val kind = when { intent.deviceKind in AUDIO_KINDS -> intent.deviceKind!!; intent.deviceKind == null -> "headphones"; else -> intent.deviceKind }
        if (kind in APP_KINDS) return Reply("I can send files to ${possessive(owner.first)} $kind. To play audio, ask for headphones or a speaker.", ok = false)
        onEvent("status", "Searching")
        val out = devices.find(owner, kind, intent.deviceHint)
        val timings = mutableMapOf("find_device_ms" to out.ms)
        val r = when (out.kind) {
            "known" -> doConnect(out.device!!, owner)
            "confirm", "choose" -> askDevice(owner, out.candidates, mutableMapOf("owner" to owner, "kind" to kind, "hint" to intent.deviceHint, "candidates" to out.candidates))
            else -> pairingMode(owner, kind, intent.deviceHint)
        }
        r.timings.putAll(timings)
        return r
    }

    private fun askDevice(owner: Person, cands: List<Device>, st: MutableMap<String, Any?>): Reply {
        val whose = if (owner.isSelf) "yours" else possessive(owner.first)
        val q = if (cands.size == 1) {
            val d = cands[0]
            Question(newId("q_"), "confirm_device", "Are $whose the ${d.shortModel ?: d.model ?: d.name}?", listOf(Option("yes", "Yes"), Option("no", "No")), st.apply { put("device", d) })
        } else {
            val opts = cands.map { Option(it.id, it.model ?: it.name, "${it.name} · signal ${if ((it.rssi ?: -99) > -65) "strong" else "weak"}", it) } + Option("none", "None of these")
            Question(newId("q_"), "choose_device", "Which are $whose?", opts, st)
        }
        ctx.pending = q
        return Reply(q.prompt, question = q, person = owner, status = "Waiting for you")
    }

    private fun doConnect(dev: Device, owner: Person): Reply {
        val whose = if (owner.isSelf) "your" else possessive(owner.first)
        onEvent("status", "Connecting")
        val out = devices.connect(dev, owner)
        ctx.device = dev
        if (out.kind == "failed") {
            return if (out.reason == "pair") Reply("I couldn't pair with ${dev.label()}. Ask ${if (owner.isSelf) "yourself" else owner.first} to hold the power button until the light flashes, then try again.", ok = false, device = dev, status = "Not connected")
            else Reply("${whose.replaceFirstChar { it.uppercase() }} ${dev.label()} isn't responding. Make sure they're on and nearby, then try again.", ok = false, device = dev, status = "Not connected")
        }
        if (out.verified) return Reply("Connected to $whose ${dev.label()}. Audio is playing through them.", device = dev, person = owner, status = "Audio output", timings = mutableMapOf("connect_ms" to out.ms))
        return Reply("Connected to $whose ${dev.label()}, but audio is still going to ${bt.audioOutput().name}. Pick ${dev.label()} as the output.", device = dev, person = owner, status = "Connected", ok = false, timings = mutableMapOf("connect_ms" to out.ms))
    }

    private fun pairingMode(owner: Person, kind: String?, hint: String?): Reply {
        val k = kind ?: "headphones"
        val text = if (owner.isSelf) {
            "I don't see your $k. Put them in pairing mode (usually hold the power button until the light flashes) and I'll connect as soon as they appear."
        } else {
            val nudged = owner.hasApp && owner.phone != null && runCatching {
                directory.nudge(owner.phone!!, mapOf("type" to "pairing_mode", "from" to me.name, "kind" to k, "text" to "${me.name} wants to connect to your $k. Put them in pairing mode."))
            }.getOrDefault(false)
            "I don't see ${possessive(owner.first)} $k nearby. Ask ${owner.first} to put them in pairing mode.${if (nudged) " I've sent ${owner.first} a notification." else ""} I'll keep looking and connect when they show up."
        }
        startWatch(owner, k, hint)
        return Reply(text, person = owner, status = "Looking for ${if (owner.isSelf) "your" else possessive(owner.first)} $k")
    }

    private fun startWatch(owner: Person, kind: String, hint: String?, timeoutMs: Long = 90_000) {
        watch?.interrupt()
        watch = Thread({
            val deadline = System.currentTimeMillis() + timeoutMs
            try {
                while (System.currentTimeMillis() < deadline && !Thread.currentThread().isInterrupted) {
                    val out = devices.find(owner, kind, hint)
                    if (out.kind != "pairing_mode") {
                        val r = if (out.kind == "known") doConnect(out.device!!, owner)
                        else askDevice(owner, out.candidates, mutableMapOf("owner" to owner, "kind" to kind, "hint" to hint, "candidates" to out.candidates))
                        onEvent("reply", r)
                        return@Thread
                    }
                    Thread.sleep(2000)
                }
            } catch (_: InterruptedException) {
            }
        }, "redtooth-watch").apply { isDaemon = true; start() }
    }

    private fun disconnect(): Reply {
        val d = bt.paired().firstOrNull { it.connected && it.kind in AUDIO_KINDS && (ctx.device == null || it.id == ctx.device!!.id) }
            ?: return Reply("Nothing to disconnect. Audio is on ${bt.audioOutput().name}.")
        bt.disconnect(d)
        return Reply("Disconnected ${d.label()}.", status = "Disconnected")
    }

    // ------------------------------------------------------------------ files
    fun destinations(p: Person): List<Device> =
        (p.profile?.devices ?: emptyList()).filter { it.kind in APP_KINDS && it.endpoints.isNotEmpty() }.sortedBy { destOrder[it.kind] ?: 9 }

    private fun pickDestination(p: Person, kind: String?): Device? =
        destinations(p).filter { kind == null || it.kind == kind || (kind == "laptop" && it.kind == "desktop") }.firstOrNull()

    private fun ambiguous(fq: FileQuery, hits: List<FileHit>) = hits.size >= 2 && (fq.topic.isEmpty() && fq.names.isEmpty() || hits[1].score >= 0.85)

    private fun fileQuestion(fq: FileQuery, hits: List<FileHit>, st: MutableMap<String, Any?>): Question {
        val noun = fq.kinds.firstOrNull()?.let { kindNoun[it] } ?: "file"
        val n = minOf(3, hits.size)
        val prompt = if (fq.timeLabel != null) "Which $noun? ${mapOf(2 to "Two", 3 to "Three")[n] ?: n} were edited ${fq.timeLabel}." else "Which $noun did you mean?"
        val opts = hits.take(3).map { Option("f${it.id}", it.name.substringBeforeLast('.'), "Edited ${whenLabel(it.mtime)}" + if (it.snippetLabel.isNotEmpty()) " · ${it.snippetLabel}" else "", it) }
        return Question(newId("q_"), "choose_file", prompt, opts, st).also { ctx.pending = it }
    }

    private fun indexNote(): String {
        val p = files.indexing() ?: return ""
        return if (p.complete || p.queued == 0) "" else " I'm still indexing older files (${"%,d".format(p.done)} of ${"%,d".format(p.queued)}), so I may have missed some."
    }

    private fun send(intent: Intent): Reply {
        val t0 = System.nanoTime()
        val fq = intent.file ?: FileQuery(refersPrevious = true)
        // Resolve the recipient and search for the file at the same time; start checking routes as soon as the person is known.
        val personF = pool.submit(Callable { person(intent, defaultSelf = false).also { (p, _) -> if (p != null && !p.isSelf) destinations(p).forEach { prefetchRoute(it) } } })
        val hitsF = pool.submit(Callable { if (fq.refersPrevious || fq.isEmpty()) listOfNotNull(ctx.file) else files.search(fq) })
        val (person, err) = personF.get()
        val hits = hitsF.get()
        val findMs = (System.nanoTime() - t0) / 1e6
        if (err != null) {
            err.question?.state?.set("intent", intent)
            if (hits.isNotEmpty() && !ambiguous(fq, hits)) ctx.file = hits[0]
            err.timings["find_match_ms"] = findMs
            return err
        }
        person!!
        if (person.isSelf) return Reply("That's you. Who should I send it to?", ok = false)
        if (hits.isEmpty()) {
            val msg = if (fq.refersPrevious) "Which file? Nothing is selected yet." else "I couldn't find a ${fq.describe()}.${indexNote()}"
            return Reply(msg, files = files.recent(4), person = person, ok = false, timings = mutableMapOf("find_match_ms" to findMs))
        }
        val dest = pickDestination(person, intent.deviceKind)
        if (ambiguous(fq, hits)) {
            val q = fileQuestion(fq, hits, mutableMapOf("person" to person, "device" to dest))
            return Reply(q.prompt, question = q, files = hits.take(3), person = person, status = "Waiting for you", timings = mutableMapOf("find_match_ms" to findMs))
        }
        return startSend(hits[0], person, dest).also { it.timings["find_match_ms"] = findMs }
    }

    private fun startSend(hit: FileHit, person: Person, dest0: Device?): Reply {
        ctx.person = person; ctx.file = hit; ctx.touch()
        if (!person.hasApp) return Reply("${person.first} doesn't use Redtooth yet, so I can't deliver to their devices. Send them an invite and I'll remember their devices once they join.", person = person, files = listOf(hit), ok = false)
        val dest = dest0 ?: pickDestination(person, null)
            ?: return Reply("None of ${possessive(person.first)} devices are reachable right now. I'll keep the file selected.", person = person, files = listOf(hit), ok = false)
        val src = files.source(hit) ?: return Reply("I can't open ${hit.name} anymore.", ok = false)
        ctx.device = dest
        val t = sender.start(src, person.first, dest)
        ctx.lastTransferId = t.id
        return Reply("Sending ${hit.name} to ${possessive(person.first)} ${dest.label()}.", person = person, files = listOf(hit), transferId = t.id, status = "Waiting for ${person.first}")
    }

    private fun retarget(intent: Intent): Reply {
        if (intent.deviceKind in AUDIO_KINDS) return connect(intent)
        val (person, err) = person(intent, defaultSelf = false)
        if (err != null) return err
        person!!
        val hit = ctx.file ?: return Reply("Which file should I send?", ok = false)
        val dest = pickDestination(person, intent.deviceKind) ?: return Reply("${person.first} doesn't have a ${intent.deviceKind ?: "device"} set up with Redtooth.", person = person, ok = false)
        sender.transfers[ctx.lastTransferId ?: ""]?.let { prev ->
            if (prev.recipientFirst == person.first && prev.status !in setOf("saved", "declined", "failed", "cancelled")) sender.cancel(prev.id)
        }
        val r = startSend(hit, person, dest)
        if (r.ok && intent.instead) r.text = r.text.trimEnd('.') + " instead."
        return r
    }

    private fun find(intent: Intent): Reply {
        val t0 = System.nanoTime()
        val fq = intent.file ?: FileQuery()
        if (fq.isEmpty()) return Reply("What should I look for?", ok = false)
        val hits = files.search(fq)
        val ms = (System.nanoTime() - t0) / 1e6
        if (hits.isEmpty()) return Reply("I couldn't find a ${fq.describe()}.${indexNote()}", ok = false, timings = mutableMapOf("find_match_ms" to ms))
        ctx.files = hits
        if (ambiguous(fq, hits)) {
            val q = fileQuestion(fq, hits, mutableMapOf())
            return Reply("I found ${minOf(3, hits.size)} that could fit. ${q.prompt}", question = q, files = hits.take(3), timings = mutableMapOf("find_match_ms" to ms))
        }
        val top = hits[0]
        ctx.file = top; ctx.touch()
        val where = if (top.snippetLabel.isNotEmpty() && top.snippet.isNotEmpty()) " — ${top.snippetLabel}: “${top.snippet.take(80)}”" else ""
        return Reply("Found ${top.name}$where.${indexNote()}", files = hits.take(3), timings = mutableMapOf("find_match_ms" to ms))
    }

    companion object {
        fun newId(prefix: String) = prefix + UUID.randomUUID().toString().replace("-", "").take(12)

        fun whenLabel(mtime: Double, now: ZonedDateTime = ZonedDateTime.now()): String {
            val dt = java.time.Instant.ofEpochMilli((mtime * 1000).toLong()).atZone(now.zone)
            val t = java.time.format.DateTimeFormatter.ofPattern("h:mm a").format(dt)
            return when {
                dt.toLocalDate() == now.toLocalDate() -> t
                dt.toLocalDate() == now.toLocalDate().minusDays(1) -> "Yesterday $t"
                java.time.Duration.between(dt, now).toDays() < 7 -> java.time.format.DateTimeFormatter.ofPattern("EEE ").format(dt) + t
                else -> java.time.format.DateTimeFormatter.ofPattern("MMM d").format(dt)
            }
        }
    }
}
