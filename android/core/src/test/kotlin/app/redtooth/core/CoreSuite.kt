package app.redtooth.core

import java.io.File
import java.net.InetSocketAddress
import java.net.Socket
import java.nio.file.Files
import java.time.OffsetDateTime
import java.time.ZonedDateTime
import java.util.concurrent.TimeUnit

/*
 * Core test suite. Dependency-free so it runs anywhere a JVM does:
 *   ./gradlew :core:test           (via CoreSuiteTest, JUnit)
 *   or compile and run CoreSuite.main directly.
 * Interop tests need the desktop repo's Python package on the path (REDTOOTH_PY=python3, cwd = repo root);
 * they are skipped when it isn't available.
 */
object CoreSuite {
    private val failures = mutableListOf<String>()
    private var passed = 0
    private var skipped = 0

    fun check(name: String, block: () -> Unit) {
        try { block(); passed++; println("  ✓ $name") } catch (e: SkipException) { skipped++; println("  - $name (skipped: ${e.message})") } catch (e: Throwable) {
            failures += "$name: ${e.message ?: e}"; println("  ✗ $name: ${e.message ?: e}")
        }
    }

    class SkipException(msg: String) : Exception(msg)

    fun eq(a: Any?, b: Any?, what: String = "") { if (a != b) throw AssertionError("$what expected <$b> but was <$a>") }
    fun ok(c: Boolean, what: String) { if (!c) throw AssertionError(what) }

    @JvmStatic
    fun main(args: Array<String>) {
        val failed = runAll()
        if (failed > 0) System.exit(1)
    }

    fun runAll(): Int {
        failures.clear(); passed = 0; skipped = 0
        println("Redtooth core")
        json(); phones(); countries(); ranking(); intents(); people(); agentFlows(); transfers(); interop()
        println("\n$passed passed, ${failures.size} failed, $skipped skipped")
        failures.forEach { println("FAIL $it") }
        return failures.size
    }

    // ------------------------------------------------------------------ basics
    private fun json() = check("json round trip") {
        val v = mapOf("a" to 1L, "b" to listOf(true, null, 2.5, "x\"y\n"), "c" to mapOf("ü" to "ß"))
        eq(Json.decode(Json.encode(v)), v)
        eq((Json.obj("""{"n": -12, "f": 1e3, "s": "é"}""")["s"]), "é")
    }

    private fun phones() = check("phone normalization and hashes match the desktop") {
        val expected = mapOf(
            "(555) 014-2000" to ("+15550142000" to "2c5f5762ea8a2911"), "+44 20 7946 0958" to ("+442079460958" to "14f6459315e84598"),
            "0049 30 1234567" to ("+49301234567" to "7f6a8bcfa4ab7486"), "15550100" to ("+115550100" to "d3fa465069710934"),
        )
        for ((raw, e) in expected) { eq(Phones.normalize(raw), e.first, raw); eq(Phones.hash(raw).take(16), e.second, "hash $raw") }
    }

    @Suppress("UNCHECKED_CAST")
    private fun countries() {
        val text = javaClass.getResourceAsStream("/phone_fixtures.json")?.bufferedReader()?.readText()
            ?: File("src/test/resources/phone_fixtures.json").takeIf { it.exists() }?.readText()
            ?: File("android/core/src/test/resources/phone_fixtures.json").readText()
        val fx = Json.obj(text)
        val cases = fx.list("cases")!!.map { it as Map<String, Any?> }
        check("country rules match the desktop on ${cases.size} numbers") {
            val bad = cases.filter { Phones.normalize(it.str("raw")!!, it.str("country")) != it.str("e164") }
                .map { "${it.str("raw")} (${it.str("country")}): ${Phones.normalize(it.str("raw")!!, it.str("country"))} != ${it.str("e164")}" }
            eq(bad, emptyList<String>(), "mismatches")
        }
        check("country table matches the desktop") {
            val table = fx.list("countries")!!.map { it as Map<String, Any?> }
            eq(COUNTRIES.map { listOf(it.iso, it.cc, it.trunk, it.idd) }, table.map { listOf(it.str("iso"), it.str("cc"), it.str("trunk"), it.str("idd")) })
        }
    }

    @Suppress("UNCHECKED_CAST")
    private fun ranking() {
        val text = javaClass.getResourceAsStream("/ranking_fixtures.json")?.bufferedReader()?.readText()
            ?: File("src/test/resources/ranking_fixtures.json").takeIf { it.exists() }?.readText()
            ?: File("android/core/src/test/resources/ranking_fixtures.json").readText()
        val fx = Json.obj(text)
        val now = java.time.Instant.ofEpochSecond((fx["now"] as Number).toLong()).atZone(java.time.ZoneOffset.UTC)
        fun hits(l: Any?) = (l as List<Map<String, Any?>>).map {
            FileHit("1", it.str("path")!!, it.str("name")!!, "document", it.str("ext")!!, 1, (it["mtime"] as Number).toDouble(),
                score = (it["score"] as Number).toDouble(), confidence = (it["confidence"] as? Number)?.toDouble(),
                reasons = (it["reasons"] as List<*>).map { r -> r as String })
        }
        check("ranking: coverage bonus matches the desktop") {
            val bad = fx.list("coverage")!!.map { it as Map<String, Any?> }.filter { c ->
                val got = Ranking.coverageBonus((c["qws"] as List<*>).map { it as String }, c.str("name")!!, c.str("chunk")!!)
                kotlin.math.abs(got - (c["bonus"] as Number).toDouble()) > 1e-6
            }.map { it.str("name") }
            eq(bad, emptyList<String>(), "mismatches")
            eq(Ranking.STOP.sorted(), fx.list("stop")!!.map { it as String }, "stop words")
        }
        check("ranking: “which one?” questions match the desktop") {
            for (q in fx.list("questions")!!.map { it as Map<String, Any?> }) {
                val hs = hits(q["hits"])
                eq(Clarify.distinguish(hs, now).first, q.str("question"), "question")
                eq(hs.map { Clarify.optionDetail(it, now) }, (q["details"] as List<*>).map { it as String }, "details")
            }
        }
        check("ranking: when to ask matches the desktop") {
            for (a in fx.list("ambiguous")!!.map { it as Map<String, Any?> }) eq(Clarify.ambiguous(hits(a["hits"]), a["topic"] as Boolean), a["ask"], "ask")
        }
        check("usage: sent beats opened, picks are remembered, everything fades") {
            var clock = 1_000_000.0
            val u = UsageStore(nowS = { clock })
            u.osRecent = { mapOf("/d/opened.docx" to clock - 86400) }
            u.record("/d/sent.docx", "sent")
            u.recordPick("term paper", "/d/final.docx")
            val s1 = u.signals(listOf("/d/sent.docx", "/d/opened.docx", "/d/final.docx", "/d/never.docx"), "Term Papers")
            ok(s1["/d/sent.docx"]!!.first > s1["/d/opened.docx"]!!.first, "sent > opened")
            ok(s1["/d/final.docx"]!!.second > 0 && s1["/d/final.docx"]!!.third[0].startsWith("you picked it"), "pick remembered")
            eq(s1["/d/never.docx"]!!.first, 0.0)
            clock += 60 * 86400
            ok(u.signals(listOf("/d/sent.docx"))["/d/sent.docx"]!!.first < s1["/d/sent.docx"]!!.first / 4, "fades")
        }
    }

    @Suppress("UNCHECKED_CAST")
    private fun intents() {
        val text = javaClass.getResourceAsStream("/intent_fixtures.json")?.bufferedReader()?.readText()
            ?: File("src/test/resources/intent_fixtures.json").takeIf { it.exists() }?.readText()
            ?: File("android/core/src/test/resources/intent_fixtures.json").readText()
        val fx = Json.obj(text)
        val now = OffsetDateTime.parse(fx.str("now")).toZonedDateTime()
        val names = fx.list("names")!!.map { it as String }
        val cases = fx.list("cases")!!.map { it as Map<String, Any?> }
        var mismatches = 0
        for (c in cases) {
            val i = IntentParser.parse(c.str("text")!!, names, now)
            val got = mutableMapOf<String, Any?>(
                "action" to i.action, "person" to i.person, "pronoun" to i.pronoun, "relation" to i.relation, "device_kind" to i.deviceKind,
                "device_hint" to i.deviceHint, "choice" to i.choice?.toLong(), "instead" to i.instead,
                "file" to i.file?.let { f -> mapOf("kinds" to f.kinds, "exts" to f.exts.sorted(), "topic" to f.topic, "since" to f.since, "until" to f.until,
                    "time_label" to f.timeLabel, "refers_previous" to f.refersPrevious, "names" to f.names) },
            )
            val want = c.filterKeys { it != "text" }
            val diffs = want.keys.filter { k -> !same(got[k], want[k]) }
            if (diffs.isNotEmpty()) { mismatches++; println("    “${c.str("text")}”: " + diffs.joinToString { "$it got ${got[it]} want ${want[it]}" }) }
        }
        check("intent parser matches the desktop on ${cases.size} requests") { eq(mismatches, 0, "mismatching requests") }
    }

    private fun same(a: Any?, b: Any?): Boolean = when {
        a is Map<*, *> && b is Map<*, *> -> a.keys == b.keys && a.keys.all { same(a[it], b[it]) }
        a is List<*> && b is List<*> -> a.size == b.size && a.indices.all { same(a[it], b[it]) }
        a is Number && b is Number -> Math.abs(a.toDouble() - b.toDouble()) < 1e-3
        else -> a == b
    }

    private fun people() = check("“Daniel” resolves to the friend who is nearby and uses Redtooth") {
        val w = World()
        val c = w.agent.people.resolve("daniel", null)
        eq(c[0].person.name, "Daniel Ortiz"); ok(!PeopleResolver.isAmbiguous(c), "should not need to ask")
        eq(PeopleResolver.ratio("daniel", "danielle").let { Math.round(it * 1000) }, 857L, "difflib ratio")
    }

    // ------------------------------------------------------------------ agent with fake hardware
    class FakeBt : BluetoothPort {
        val radios = linkedMapOf<String, Device>() // id -> device (as seen over the air)
        val pairingMode = mutableSetOf<String>()
        val pairedIds = mutableSetOf<String>()
        val connectedIds = mutableSetOf<String>()
        var output = AudioOutput("Phone speaker")
        private fun view(d: Device, src: String) = d.copy(paired = d.id in pairedIds, connected = d.id in connectedIds, discoverable = d.id in pairingMode,
            ownerPhone = null, ownerName = null, sources = mutableListOf(src))
        override fun paired() = radios.values.filter { it.id in pairedIds }.map { view(it, "paired") }
        override fun scan(seconds: Double) = radios.values.filter { it.id in pairingMode }.map { view(it, "nearby") }
        override fun pair(dev: Device): Boolean { if (dev.id !in pairingMode) return false; pairedIds += dev.id; pairingMode -= dev.id; return true }
        override fun connect(dev: Device): Boolean { if (dev.id !in pairedIds) return false; connectedIds += dev.id; output = AudioOutput(dev.name, dev.address, "bluetooth"); return true }
        override fun disconnect(dev: Device): Boolean { connectedIds -= dev.id; return true }
        override fun audioOutput() = output
        override fun setAudioOutput(dev: Device) = (dev.id in connectedIds).also { if (it) output = AudioOutput(dev.name, dev.address, "bluetooth") }
    }

    class FakeFiles(val dir: File) : FileSearch {
        data class Doc(val file: File, val text: String)
        val docs = mutableListOf<Doc>()
        fun add(name: String, text: String, mtime: ZonedDateTime, bytes: Int = 4096) {
            val f = File(dir, name); f.writeBytes(ByteArray(bytes) { (it * 31).toByte() }); f.setLastModified(mtime.toInstant().toEpochMilli()); docs += Doc(f, text)
        }
        private fun hit(d: Doc, score: Double) = FileHit(d.file.path, d.file.path, d.file.name, "", "." + d.file.extension, d.file.length(), d.file.lastModified() / 1000.0, score)
        override fun search(q: FileQuery): List<FileHit> {
            val words = q.topic.lowercase().split(" ").filter { it.length > 2 }.map { it.trimEnd('s') }
            return docs.filter { d ->
                (q.exts.isEmpty() || ("." + d.file.extension) in q.exts) &&
                    (q.since == null || d.file.lastModified() / 1000.0 >= q.since!!) && (q.until == null || d.file.lastModified() / 1000.0 < q.until!!)
            }.map { d ->
                val hay = (d.file.name + " " + d.text).lowercase()
                d to if (words.isEmpty()) 1.0 else words.count { it in hay }.toDouble() / words.size
            }.filter { it.second > 0 }.sortedWith(compareBy({ -it.second }, { -it.first.file.lastModified() })).let { l ->
                val top = l.firstOrNull()?.second ?: 1.0
                l.map { (d, s) -> hit(d, s / top) }
            }
        }
        override fun recent(n: Int) = docs.sortedByDescending { it.file.lastModified() }.take(n).map { hit(it, 1.0) }
        override fun source(hit: FileHit): FileSource = LocalFile(File(hit.path))
    }

    class World(scenario: String = "registered") {
        val tmp: File = Files.createTempDirectory("rt-core").toFile()
        val now: ZonedDateTime = ZonedDateTime.now()
        val bt = FakeBt()
        val dir = MemoryDirectory()
        val files = FakeFiles(File(tmp, "docs").apply { mkdirs() })
        val accepted = mutableListOf<String>()
        val rxPhone = Receiver("d_phone", "Daniel's Pixel", File(tmp, "daniel-phone"), { o -> accepted += o.name; true })
        val rxLaptop = Receiver("d_laptop", "Daniel's ThinkPad", File(tmp, "daniel-laptop"), { true })
        val me = Person(Contact("c_me", "Alex Rivera", listOf("+15550100")), isSelf = true)
        val contacts = listOf(
            Contact("c_daniel", "Daniel Ortiz", listOf("+15550142000"), listOf("Dan"), "friend", 1.0),
            Contact("c_dkim", "Daniel Kim", listOf("+15550188000"), emptyList(), "coworker"),
            Contact("c_priya", "Priya Shah", listOf("+15550167000"), emptyList(), "coworker", 1.0),
        )
        val events = mutableListOf<Pair<String, Any?>>()
        val agent: Agent
        val xm5 = Device("bt:xm5", "WH-1000XM5", "headphones", address = "88:C9:E8:12:34:56")

        init {
            bt.radios["bt:pods"] = Device("bt:pods", "Alex's AirPods Pro", "headphones", address = "F0:B3:EC:01:02:03").also { DeviceNames.enrich(it) }
            bt.pairedIds += "bt:pods"
            bt.radios[xm5.id] = DeviceNames.enrich(xm5.copy())
            bt.radios["bt:jbl"] = DeviceNames.enrich(Device("bt:jbl", "JBL Flip 6", "speaker", address = "00:42:79:AA:BB:CC"))
            bt.pairingMode += "bt:jbl"
            if (scenario != "not_pairing") bt.pairingMode += xm5.id
            val phone = Device("d_phone", "Daniel's Pixel", "phone", model = "Pixel 9", hasApp = true, endpoints = listOf(Endpoint("lan", "127.0.0.1", rxPhone.start(0, "127.0.0.1"))))
            val laptop = Device("d_laptop", "Daniel's ThinkPad", "laptop", model = "ThinkPad X1", hasApp = true, endpoints = listOf(Endpoint("lan", "127.0.0.1", rxLaptop.start(0, "127.0.0.1"))))
            if (scenario != "no_app") {
                dir.register("+15550142000", "Daniel Ortiz")
                dir.setDevices("+15550142000", listOf(phone, laptop, xm5.copy(model = "Sony WH-1000XM5", shortModel = "Sony XM5s")))
            }
            val morning = now.withHour(minOf(9, maxOf(0, now.hour - 1))).withMinute(12)
            files.add("Q4 Board Update.pptx", "revenue pipeline hiring board", if (morning.isAfter(now)) now.minusMinutes(40) else morning)
            files.add("Sales Kickoff 2027.pptx", "territories quotas partner program", if (morning.isAfter(now)) now.minusMinutes(20) else morning.plusMinutes(5))
            files.add("Retention Plan.pptx", "keeping more customers renewals cancellations churn", now.minusDays(3))
            files.add("Offsite agenda.md", "strategy hiking", now.minusDays(1))
            val people = PeopleResolver(contacts, me)
            agent = Agent(me, people, dir, bt, files, MemoryRemembered(), Sender(me.name, me.phone!!), { k, v -> synchronized(events) { events += k to v } }, scanSeconds = 0.0)
            agent.refreshPeople()
            people.nearbyPhones = setOf("+15550142000")
        }

        fun waitTransfer(id: String): Transfer { agent.sender.await(id, 20_000); return agent.sender.transfers[id]!! }
        fun close() { rxPhone.stop(); rxLaptop.stop(); tmp.deleteRecursively() }
    }

    private fun agentFlows() {
        check("a phone isn't an audio output; sending to my phone uses Bluetooth share") {
            val w = World()
            w.bt.radios["bt:s24"] = Device("bt:s24", "Alex's S24", "phone", address = "AA:BB:CC:00:11:22")
            w.bt.pairedIds += "bt:s24"
            val r = w.agent.connectDevice("bt:s24")
            ok(!r.ok && "is a phone" in r.text && "Audio is playing" !in r.text, r.text)
            val s = w.agent.ask("send the board update to my phone")
            ok(s.ok && s.action == "bluetooth_share" && "S24" in s.text && "Alex's S24" !in s.text, s.text)
            w.close()
        }
        check("audio is only claimed after reading the real output") {
            val w = World()
            class OutputOnly(val out: AudioOutput) : BluetoothPort {
                override fun paired() = emptyList<Device>(); override fun scan(seconds: Double) = emptyList<Device>()
                override fun pair(dev: Device) = false; override fun connect(dev: Device) = false; override fun disconnect(dev: Device) = false
                override fun audioOutput() = out; override fun setAudioOutput(dev: Device) = false
            }
            ok(OutputOnly(AudioOutput("Phone speaker")).verifyAudioOutput(Device("x", "Alex's AirPods Pro", address = "F0:B3:EC:01:02:03"), 400) == false, "elsewhere")
            ok(OutputOnly(AudioOutput("?", known = false)).verifyAudioOutput(Device("x", "Alex's AirPods Pro"), 400) == null, "can't read")
            ok(!w.bt.outputMatches(AudioOutput("Speaker (JBL Flip 6)", transport = "bluetooth"), Device("x", "JBL Flip")), "no substring matches")
            ok(w.bt.outputMatches(AudioOutput("Headphones (WH-CH720N)", transport = "bluetooth"), Device("x", "WH-CH720N")), "bracketed name")
            w.close()
        }
        check("connect to Daniel's registered headphones: no questions, verified output") {
            val w = World()
            val r = w.agent.ask("Hey, connect to my friend Daniel's headphones")
            ok(r.ok && r.status == "Audio output", r.text)
            eq(w.bt.output.address, w.xm5.address, "audio output")
            ok(w.agent.remembered.remembered("+15550142000").any { it.id == "bt:xm5" }, "remembered for next time")
            w.close()
        }
        check("Daniel without the app: “Are Daniel's the Sony XM5s?” then pair and remember") {
            val w = World("no_app")
            val r = w.agent.ask("connect to Daniel's headphones")
            eq(r.question?.prompt, "Are Daniel's the Sony XM5s?")
            val r2 = w.agent.ask("yes")
            ok(r2.ok && w.bt.output.address == w.xm5.address, r2.text)
            ok("bt:xm5" in w.bt.pairedIds, "paired through the OS")
            w.close()
        }
        check("headphones not in pairing mode: nudge Daniel, keep scanning, connect when they appear") {
            val w = World("not_pairing")
            val r = w.agent.ask("connect to Daniel's headphones")
            ok("pairing mode" in r.text && "notification" in r.text, r.text)
            ok(w.dir.inboxes.values.flatten().any { it["type"] == "pairing_mode" }, "nudge delivered")
            w.bt.pairingMode += "bt:xm5"
            val deadline = System.currentTimeMillis() + 8000
            while (System.currentTimeMillis() < deadline && synchronized(w.events) { w.events.none { it.first == "reply" } }) Thread.sleep(100)
            val reply = synchronized(w.events) { w.events.first { it.first == "reply" }.second as Reply }
            eq(reply.status, "Audio output", "async reply")
            w.close()
        }
        check("“Send Daniel the deck I edited this morning” asks which, then delivers to his phone") {
            val w = World()
            val r = w.agent.ask("Send Daniel the deck I edited this morning.")
            eq(r.question?.kind, "choose_file")
            eq(r.question!!.options.map { it.label }.toSet(), setOf("Q4 Board Update", "Sales Kickoff 2027"))
            eq(r.person?.name, "Daniel Ortiz")
            val r2 = w.agent.ask("the Q4 board one")
            ok(r2.transferId != null && "Q4 Board Update.pptx" in r2.text && "Pixel" in r2.text, r2.text)
            val t = w.waitTransfer(r2.transferId!!)
            eq(t.status, "saved", "transfer ${t.error}")
            eq(Sender.sha256File(File(w.tmp, "daniel-phone/Q4 Board Update.pptx")), Sender.sha256File(File(w.tmp, "docs/Q4 Board Update.pptx")), "bytes")
            ok(t.timings.containsKey("accept_to_first_byte_ms") && t.timings.containsKey("first_byte_to_saved_ms"), "timings")
            w.close()
        }
        check("“Send it to his laptop instead” keeps the file and person") {
            val w = World()
            w.agent.ask("Send Daniel the deck I edited this morning")
            val first = w.agent.ask("2").files.first().name
            val r = w.agent.ask("Send it to his laptop instead.")
            ok(r.ok && "ThinkPad" in r.text && first in r.text && r.text.endsWith("instead."), r.text)
            eq(w.waitTransfer(r.transferId!!).status, "saved")
            w.close()
        }
        check("“Find the deck about customer churn” finds the retention deck, then “send it to Daniel”") {
            val w = World()
            val r = w.agent.ask("Find the deck about customer churn.")
            eq(r.files.firstOrNull()?.name, "Retention Plan.pptx")
            val r2 = w.agent.ask("send it to Daniel")
            ok("Retention Plan.pptx" in r2.text, r2.text)
            w.close()
        }
    }

    // ------------------------------------------------------------------ transfers
    private fun transfers() {
        check("interrupted transfer resumes from the last verified byte") {
            val tmp = Files.createTempDirectory("rt-tx").toFile()
            val src = File(tmp, "video.bin").apply { writeBytes(ByteArray(24 shl 20) { (it * 7 + it / 1024).toByte() }) }
            val rx = Receiver("dev", "Phone", File(tmp, "in"), { true })
            val port = rx.start(0, "127.0.0.1")
            var drops = 0
            // First connection dies after ~10 MB, like walking out of range.
            val flaky: (Endpoint) -> Socket = { ep ->
                val s = Socket(); s.connect(InetSocketAddress(ep.host, ep.port), 2000)
                if (drops++ == 0) object : Socket() {
                    override fun getInputStream() = s.getInputStream()
                    override fun getOutputStream() = object : java.io.FilterOutputStream(s.getOutputStream()) {
                        var n = 0L
                        override fun write(b: ByteArray, off: Int, len: Int) { n += len; if (n > 10L shl 20) { s.close(); throw java.io.IOException("dropped") }; out.write(b, off, len) }
                    }
                    override fun close() = s.close()
                    override fun setSendBufferSize(size: Int) {}
                    override fun setSoTimeout(timeout: Int) { s.soTimeout = timeout }
                } else s
            }
            val sender = Sender("Alex", "+15550100", route = { it.endpoints.first() }, connect = flaky)
            val t = sender.start(src, "Dan", Device("dev", "Phone", "phone", endpoints = listOf(Endpoint("lan", "127.0.0.1", port))))
            sender.await(t.id, 30_000)
            eq(t.status, "saved", "status (${t.error})")
            ok(t.resumes >= 1, "resumed")
            eq(Sender.sha256File(File(tmp, "in/video.bin")), Sender.sha256File(src), "bytes")
            rx.stop(); tmp.deleteRecursively()
        }
        check("declined offers are reported") {
            val tmp = Files.createTempDirectory("rt-dec").toFile()
            val src = File(tmp, "a.txt").apply { writeText("hi") }
            val rx = Receiver("dev", "Phone", File(tmp, "in"), { false })
            val port = rx.start(0, "127.0.0.1")
            val sender = Sender("Alex", "+1")
            val t = sender.start(src, "Dan", Device("dev", "Phone", "phone", endpoints = listOf(Endpoint("lan", "127.0.0.1", port))))
            sender.await(t.id)
            eq(t.status, "declined"); eq(t.label, "Declined")
            rx.stop(); tmp.deleteRecursively()
        }
    }

    // ------------------------------------------------------------------ cross-language
    private fun repoRoot(): File? = generateSequence(File("").absoluteFile) { it.parentFile }.firstOrNull { File(it, "redtooth/transfer/protocol.py").exists() }

    private fun python(vararg args: String): Process {
        val root = repoRoot() ?: throw SkipException("desktop repo not found")
        return ProcessBuilder(listOf(System.getenv("REDTOOTH_PY") ?: "python3") + args).directory(root).redirectErrorStream(false).start()
    }

    private fun interop() {
        check("Kotlin phone sends to the Python desktop") {
            val tmp = Files.createTempDirectory("rt-ia").toFile()
            val p = python("tools/interop_peer.py", "receive", File(tmp, "in").path)
            try {
                val out = p.inputStream.bufferedReader()
                val port = (Json.obj(out.readLine() ?: throw SkipException("python peer didn't start")).long("port") ?: 0).toInt()
                val src = File(tmp, "Kotlin deck.pptx").apply { writeBytes(ByteArray(3 shl 20) { (it % 251).toByte() }) }
                val s = Sender("Kotlin phone", "+15550142000")
                val t = s.start(src, "Desktop", Device("py_desktop", "Python desktop", "laptop", endpoints = listOf(Endpoint("lan", "127.0.0.1", port))))
                s.await(t.id, 20_000)
                eq(t.status, "saved", "status (${t.error})")
                val saved = Json.obj(out.readLine())
                eq(Sender.sha256File(File(saved.str("saved")!!)), Sender.sha256File(src), "bytes")
            } finally { p.destroy(); tmp.deleteRecursively() }
        }
        check("Python desktop sends to the Kotlin phone (negotiates SHA-256 chunks)") {
            val tmp = Files.createTempDirectory("rt-ib").toFile()
            val rx = Receiver("k_phone", "Kotlin phone", File(tmp, "in"), { true })
            val port = rx.start(0, "127.0.0.1")
            val src = File(tmp, "From desktop.bin").apply { writeBytes(ByteArray(5 shl 20) { (it * 13).toByte() }) }
            val p = python("tools/interop_peer.py", "send", src.path, "127.0.0.1", port.toString(), "k_phone")
            try {
                ok(p.waitFor(30, TimeUnit.SECONDS), "python sender finished")
                val res = Json.obj(p.inputStream.bufferedReader().readText().trim().lines().last())
                eq(res.str("status"), "saved", "status ${res.str("error")} ${p.errorStream.bufferedReader().readText().takeLast(300)}")
                eq(Sender.sha256File(File(tmp, "in/From desktop.bin")), Sender.sha256File(src), "bytes")
            } finally { p.destroy(); rx.stop(); tmp.deleteRecursively() }
        }
        check("Kotlin directory client against the Python directory server") {
            val port = java.net.ServerSocket(0).use { it.localPort }
            val data = File.createTempFile("redtooth-dir", ".json").also { it.delete() }  // fresh: numbers can only be claimed once
            val p = python("-m", "redtooth.cli", "directory", "--host", "127.0.0.1", "--port", port.toString(), "--open-register", "--data", data.path)
            try {
                val alex = HttpDirectory("http://127.0.0.1:$port")
                var up = false
                for (i in 0 until 50) { try { alex.register("+15550100", "Alex Rivera"); up = true; break } catch (_: Exception) { Thread.sleep(100) } }
                if (!up) throw SkipException("directory didn't start")
                val dan = HttpDirectory("http://127.0.0.1:$port").apply { register("(555) 014-2000", "Daniel Ortiz") }
                dan.setDevices("+15550142000", listOf(Device("d_phone", "Daniel's Pixel", "phone", hasApp = true, endpoints = listOf(Endpoint("lan", "10.0.0.5", 47800))),
                    Device("bt:xm5", "WH-1000XM5", "headphones", address = "88:C9:E8:12:34:56", model = "Sony WH-1000XM5")))
                val found = alex.lookup(listOf("555-014-2000", "+15550188000"))
                eq(found.keys, setOf("+15550142000"), "lookup by hashed number")
                eq(found["+15550142000"]!!.devices.map { it.kind }, listOf("phone", "headphones"))
                eq(found["+15550142000"]!!.devices[0].endpoints[0].port, 47800)
                ok(alex.nudge("+15550142000", mapOf("type" to "pairing_mode", "from" to "Alex Rivera")), "nudge")
                eq(dan.inbox().first()["from"], "Alex Rivera")
                // first claim wins: the same number can't be registered again
                val again = runCatching { HttpDirectory("http://127.0.0.1:$port").register("+1 555 0100", "Mallory") }
                eq((again.exceptionOrNull() as? HttpDirectory.DirectoryException)?.code, 409, "taken number refused")
                // Alex adds a second device with a link code
                val code = alex.linkCode()
                ok(HttpDirectory("http://127.0.0.1:$port").verify("+15550100", code, "Alex Rivera").isNotEmpty(), "link code works")
            } finally { p.destroy(); data.delete() }
        }
    }
}
