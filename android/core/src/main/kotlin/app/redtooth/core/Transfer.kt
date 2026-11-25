package app.redtooth.core

import java.io.BufferedInputStream
import java.io.BufferedOutputStream
import java.io.DataInputStream
import java.io.File
import java.io.IOException
import java.io.RandomAccessFile
import java.net.InetSocketAddress
import java.net.ServerSocket
import java.net.Socket
import java.security.MessageDigest
import java.util.UUID
import java.util.concurrent.Callable
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.Executors
import java.util.concurrent.Future
import java.util.concurrent.TimeUnit

// --------------------------------------------------------------------------- receiving

data class IncomingOffer(
    val id: String,
    val name: String,
    val size: Long,
    val mime: String,
    val fingerprint: String,
    val senderName: String,
    val senderPhone: String,
    val toDevice: String,
    @Volatile var received: Long = 0,
    @Volatile var status: String = "waiting", // waiting | receiving | saved | declined | failed | interrupted | cancelled
    @Volatile var savedPath: String = "",
)

/** Decides whether to accept an offer. Blocks until the user answers (notification or panel). */
fun interface Acceptor {
    fun decide(offer: IncomingOffer): Boolean
}

/**
 * Always-listening receiver: no "receive mode". Verifies each chunk as it arrives, journals the
 * verified offset so an interrupted transfer resumes, and checks the full SHA-256 before saving
 * and confirming the save.
 */
class Receiver(
    val deviceId: String,
    val deviceName: String,
    val inbox: File,
    private val acceptor: Acceptor,
    private val events: (IncomingOffer) -> Unit = {},
    private val acceptTimeoutMs: Long = 120_000,
) {
    private val partial = File(inbox, ".redtooth-partial").apply { mkdirs() }
    val offers = ConcurrentHashMap<String, IncomingOffer>()
    private val pool = Executors.newCachedThreadPool { r -> Thread(r, "redtooth-rx").apply { isDaemon = true } }
    @Volatile private var server: ServerSocket? = null

    /** Listen on [port] (0 = any). Returns the bound port. */
    fun start(port: Int = 0, host: String = "0.0.0.0"): Int {
        val s = ServerSocket()
        s.reuseAddress = true
        s.bind(InetSocketAddress(host, port))
        server = s
        pool.execute {
            while (!s.isClosed) {
                val c = try { s.accept() } catch (e: IOException) { break }
                try { pool.execute { handle(c) } } catch (_: java.util.concurrent.RejectedExecutionException) { c.close(); break }
            }
        }
        return s.localPort
    }

    fun stop() { server?.close(); pool.shutdownNow() }

    /** Serve one connection (also usable for sockets from Wi-Fi Aware / Wi-Fi Direct networks). */
    fun handle(socket: Socket) {
        socket.use { s ->
            try {
                s.soTimeout = 30_000
                s.receiveBufferSize = 4 shl 20
                val input = DataInputStream(BufferedInputStream(s.getInputStream(), 1 shl 20))
                val out = BufferedOutputStream(s.getOutputStream())
                val first = Protocol.read(input)
                when (first.type) {
                    "PING" -> Protocol.write(out, mapOf("type" to "PONG", "device_id" to deviceId, "name" to deviceName))
                    "OFFER" -> receive(first.header, input, out)
                }
            } catch (_: IOException) {
            } catch (_: Protocol.ProtocolException) {
            }
        }
    }

    private fun journalFile(id: String) = File(partial, "$id.json")
    private fun partFile(id: String) = File(partial, "$id.part")
    private fun loadJournal(id: String): Map<String, Any?>? = try { Json.obj(journalFile(id).readText()) } catch (_: Exception) { null }
    private fun saveJournal(id: String, j: Map<String, Any?>) {
        val tmp = File(partial, "$id.json.tmp")
        tmp.writeText(Json.encode(j))
        if (!tmp.renameTo(journalFile(id))) { journalFile(id).delete(); tmp.renameTo(journalFile(id)) }
    }

    private fun receive(h: Map<String, Any?>, input: DataInputStream, out: BufferedOutputStream) {
        val id = (h.str("id") ?: return).take(64).filter { it.isLetterOrDigit() || it == '_' || it == '-' }
        val size = h.long("size") ?: return
        val from = h.map("from") ?: emptyMap()
        val offer = offers.getOrPut(id) {
            IncomingOffer(id, safeName(h.str("name") ?: "file"), size, h.str("mime") ?: "", h.str("fingerprint") ?: "",
                from.str("name") ?: "Someone", from.str("phone") ?: "", deviceId)
        }
        val journal = loadJournal(id)
        val resuming = journal != null && journal.str("fingerprint") == offer.fingerprint && journal.bool("accepted") == true
        var offset = 0L
        if (resuming) {
            offset = journal!!.long("verified") ?: 0L
            offer.status = "receiving"
        } else {
            offer.status = "waiting"; events(offer)
            Protocol.write(out, mapOf("type" to "WAITING"))
            val ok = decideWithTimeout(offer)
            if (!ok) {
                offer.status = "declined"; events(offer)
                Protocol.write(out, mapOf("type" to "DECLINE", "reason" to "declined"))
                return
            }
            saveJournal(id, mapOf("fingerprint" to offer.fingerprint, "accepted" to true, "verified" to 0, "name" to offer.name, "size" to size))
        }
        val part = partFile(id)
        RandomAccessFile(part, "rw").use { raf ->
            raf.setLength(offset)
            val full = MessageDigest.getInstance("SHA-256")
            val buf = ByteArray(4 shl 20)
            var left = offset
            raf.seek(0)
            while (left > 0) { // re-hash the verified prefix so the whole-file check still covers it
                val n = raf.read(buf, 0, minOf(left, buf.size.toLong()).toInt())
                if (n <= 0) break
                full.update(buf, 0, n); left -= n
            }
            raf.seek(offset)
            val tAccept = System.nanoTime()
            Protocol.write(out, mapOf("type" to "ACCEPT", "offset" to offset, "hashes" to Protocol.CHUNK_HASHES))
            offer.status = "receiving"; offer.received = offset; events(offer)
            var tFirst = 0L
            var expected = offset
            var unsynced = 0L
            var lastEvent = 0L
            while (true) {
                val f = try { Protocol.read(input) } catch (e: IOException) {
                    checkpoint(id, raf, expected); offer.status = "interrupted"; events(offer); throw e
                }
                when (f.type) {
                    "DATA" -> {
                        val good = (f.header.long("offset") ?: -1) == expected && f.header.str("s256") == Protocol.s256(f.payload)
                        if (!good) {
                            checkpoint(id, raf, expected)
                            Protocol.write(out, mapOf("type" to "ERROR", "reason" to "chunk_mismatch", "resume_offset" to expected))
                            return
                        }
                        if (tFirst == 0L) tFirst = System.nanoTime()
                        raf.write(f.payload); full.update(f.payload)
                        expected += f.payload.size; unsynced += f.payload.size
                        if (unsynced >= 8L shl 20) { checkpoint(id, raf, expected); unsynced = 0 }
                        offer.received = expected
                        val now = System.nanoTime()
                        if (now - lastEvent > 150_000_000) { lastEvent = now; events(offer) }
                    }
                    "END" -> {
                        val digest = full.digest().joinToString("") { "%02x".format(it) }
                        if (expected != size || digest != f.header.str("sha256")) {
                            raf.close(); part.delete(); journalFile(id).delete()
                            offer.status = "failed"; events(offer)
                            Protocol.write(out, mapOf("type" to "ERROR", "reason" to "hash_mismatch", "resume_offset" to 0))
                            return
                        }
                        raf.fd.sync()
                        raf.close()
                        val dest = unique(inbox, offer.name)
                        if (!part.renameTo(dest)) { part.copyTo(dest); part.delete() }
                        journalFile(id).delete()
                        val tSaved = System.nanoTime()
                        offer.status = "saved"; offer.savedPath = dest.path; events(offer)
                        val first = if (tFirst == 0L) tSaved else tFirst
                        Protocol.write(out, mapOf(
                            "type" to "SAVED", "name" to dest.name, "sha256" to digest,
                            "accept_to_first_byte_ms" to (first - tAccept) / 1e6, "first_byte_to_saved_ms" to (tSaved - first) / 1e6,
                            "resumed_from" to offset,
                        ))
                        return
                    }
                    "CANCEL" -> { offer.status = "cancelled"; events(offer); return }
                }
            }
        }
    }

    private fun decideWithTimeout(offer: IncomingOffer): Boolean {
        val f = pool.submit(Callable { acceptor.decide(offer) })
        return try { f.get(acceptTimeoutMs, TimeUnit.MILLISECONDS) } catch (_: Exception) { f.cancel(true); false }
    }

    private fun checkpoint(id: String, raf: RandomAccessFile, verified: Long) {
        try {
            raf.fd.sync()
            val j = (loadJournal(id) ?: emptyMap()).toMutableMap()
            j["verified"] = verified
            saveJournal(id, j)
        } catch (_: IOException) {
        }
    }

    companion object {
        fun safeName(name: String): String {
            val base = name.replace('\\', '/').substringAfterLast('/').trim().trimStart('.').ifEmpty { "file" }
            return base.filter { it !in "<>:\"|?*\u0000" }.take(200)
        }

        fun unique(dir: File, name: String): File {
            var f = File(dir, name)
            val stem = name.substringBeforeLast('.', name)
            val ext = if (name.contains('.')) "." + name.substringAfterLast('.') else ""
            var i = 1
            while (f.exists()) { f = File(dir, "$stem ($i)$ext"); i++ }
            return f
        }
    }
}

// --------------------------------------------------------------------------- sending

/** Something to send: a file, or (on Android) a content URI the user picked or shared. */
interface FileSource {
    val name: String
    val size: Long
    val lastModified: Long // ms since epoch
    val path: String
    fun open(): java.io.InputStream

    /** Stream from [offset]. Override when the source can seek. */
    fun openAt(offset: Long): java.io.InputStream {
        val s = open()
        var left = offset
        while (left > 0) {
            val n = s.skip(left)
            if (n <= 0) { if (s.read() < 0) break; left-- } else left -= n
        }
        return s
    }
}

class LocalFile(val file: File) : FileSource {
    override val name: String get() = file.name
    override val size: Long get() = file.length()
    override val lastModified: Long get() = file.lastModified()
    override val path: String get() = file.path
    override fun open(): java.io.InputStream = file.inputStream()
    override fun openAt(offset: Long): java.io.InputStream = java.io.FileInputStream(file).also { it.channel.position(offset) }
}

class Transfer(
    val id: String,
    val path: String,
    val name: String,
    val size: Long,
    val recipientFirst: String,
    val device: Device,
) {
    @Volatile var status: String = "preparing" // preparing | waiting | sending | interrupted | verifying | saved | declined | failed | cancelled
    @Volatile var sent: Long = 0
    @Volatile var rateBps: Double = 0.0
    @Volatile var route: Endpoint? = null
    @Volatile var error: String = ""
    @Volatile var savedName: String = ""
    @Volatile var resumes: Int = 0
    val timings = ConcurrentHashMap<String, Double>()

    val label: String
        get() = when (status) {
            "sending" -> if (size > 0) "Sending · ${(100 * sent / size)}%" else "Sending"
            "saved" -> "Saved to ${possessive(recipientFirst)} ${device.label()}"
            "waiting" -> "Waiting for $recipientFirst"
            "preparing" -> "Checking route"
            "interrupted" -> "Interrupted · resuming"
            "verifying" -> "Verifying"
            "declined" -> "Declined"
            "failed" -> "Couldn't send"
            "cancelled" -> "Cancelled"
            else -> status
        }
}

/** Picks the fastest endpoint that answers PING with the right device id; Wi-Fi Aware/Direct before LAN. */
object Routes {
    private val preference = mapOf("wifi_aware" to 0, "wifi_direct" to 0, "lan" to 1, "loopback" to 1)
    private val pool = Executors.newCachedThreadPool { r -> Thread(r, "redtooth-route").apply { isDaemon = true } }

    fun best(device: Device, timeoutMs: Int = 1500): Endpoint? {
        val futures = device.endpoints.map { ep -> ep to pool.submit(Callable { Protocol.ping(ep.host, ep.port, device.id, timeoutMs) }) }
        return futures.mapNotNull { (ep, f) -> runCatching { f.get(timeoutMs + 500L, TimeUnit.MILLISECONDS) }.getOrNull()?.let { Triple(preference[ep.kind] ?: 5, it, ep) } }
            .sortedWith(compareBy({ it.first }, { it.second })).firstOrNull()?.third
    }
}

class Sender(
    private val meName: String,
    private val mePhone: String,
    private val onUpdate: (Transfer) -> Unit = {},
    private val route: (Device) -> Endpoint? = { Routes.best(it) },
    private val maxAttempts: Int = 8,
    /** Opens a socket to an endpoint; Android overrides this for Wi-Fi Aware networks. */
    private val connect: (Endpoint) -> Socket = { ep -> Socket().apply { connect(InetSocketAddress(ep.host, ep.port), 5000) } },
) {
    val transfers = ConcurrentHashMap<String, Transfer>()
    private val threads = ConcurrentHashMap<String, Thread>()
    private val hashPool = Executors.newCachedThreadPool { r -> Thread(r, "redtooth-hash").apply { isDaemon = true } }

    fun start(file: File, recipientFirst: String, device: Device): Transfer = start(LocalFile(file), recipientFirst, device)

    fun start(file: FileSource, recipientFirst: String, device: Device): Transfer {
        val t = Transfer("t_" + UUID.randomUUID().toString().replace("-", "").take(12), file.path, file.name, file.size, recipientFirst, device)
        transfers[t.id] = t
        val th = Thread({ run(t, file) }, "redtooth-send-${t.id}").apply { isDaemon = true }
        threads[t.id] = th
        th.start()
        return t
    }

    fun cancel(id: String): Boolean {
        val t = transfers[id] ?: return false
        if (t.status in setOf("saved", "declined", "failed", "cancelled")) return false
        t.status = "cancelled"; onUpdate(t)
        threads[id]?.interrupt()
        return true
    }

    fun await(id: String, timeoutMs: Long = 60_000) { threads[id]?.join(timeoutMs) }

    private fun set(t: Transfer, status: String) { t.status = status; onUpdate(t) }

    private fun run(t: Transfer, file: FileSource) {
        val t0 = System.nanoTime()
        val fullHash: Future<String> = hashPool.submit(Callable { sha256Stream(file.open()) }) // hashed while waiting for acceptance
        val fp = sha256Hex("${file.name}:${file.size}:${file.lastModified * 1_000_000}".toByteArray()).take(40)
        var delay = 300L
        for (attempt in 0 until maxAttempts) {
            if (t.status == "cancelled") return
            try {
                val ep = route(t.device) ?: throw IOException("no route")
                t.route = ep
                attemptOnce(t, file, ep, fp, fullHash, t0)
                return
            } catch (e: InterruptedException) {
                return
            } catch (e: Exception) {
                if (t.status in setOf("declined", "failed", "cancelled", "saved")) return
                if (t.sent > 0) t.resumes++
                t.error = e.message ?: e.javaClass.simpleName
                set(t, "interrupted")
                try { Thread.sleep(delay) } catch (_: InterruptedException) { return }
                delay = minOf(delay * 2, 5000)
            }
        }
        set(t, "failed")
    }

    private fun attemptOnce(t: Transfer, file: FileSource, ep: Endpoint, fp: String, fullHash: Future<String>, t0: Long) {
        connect(ep).use { s ->
            s.sendBufferSize = 4 shl 20
            s.soTimeout = 300_000
            val input = DataInputStream(BufferedInputStream(s.getInputStream()))
            val out = BufferedOutputStream(s.getOutputStream(), 1 shl 20)
            Protocol.write(out, mapOf(
                "type" to "OFFER", "id" to t.id, "name" to t.name, "size" to t.size, "mime" to mime(t.name),
                "fingerprint" to fp, "chunk" to Protocol.CHUNK, "from" to mapOf("name" to meName, "phone" to mePhone),
            ))
            var hdr: Map<String, Any?>
            while (true) {
                val f = Protocol.read(input)
                when (f.type) {
                    "WAITING" -> { set(t, "waiting"); t.timings.putIfAbsent("offered_ms", (System.nanoTime() - t0) / 1e6) }
                    "DECLINE" -> { set(t, "declined"); return }
                    "ACCEPT" -> { hdr = f.header; break }
                    else -> throw IOException("unexpected ${f.type}")
                }
            }
            @Suppress("UNCHECKED_CAST")
            val hashes = (hdr["hashes"] as? List<String>) ?: listOf("b2")
            if ("s256" !in hashes) { t.error = "receiver can't verify SHA-256 chunks (update it)"; set(t, "failed"); return }
            val offset = hdr.long("offset") ?: 0L
            val tAccept = System.nanoTime()
            t.sent = offset
            set(t, "sending")
            s.soTimeout = 120_000
            file.openAt(offset).use { src ->
                val buf = ByteArray(Protocol.CHUNK)
                var pos = offset
                var lastEvent = 0L
                while (pos < t.size) {
                    if (Thread.currentThread().isInterrupted || t.status == "cancelled") {
                        Protocol.write(out, mapOf("type" to "CANCEL")); return
                    }
                    var n = 0
                    while (n < buf.size) { val r = src.read(buf, n, buf.size - n); if (r < 0) break; n += r }
                    if (n <= 0) break
                    Protocol.write(out, mapOf("type" to "DATA", "offset" to pos, "s256" to Protocol.s256(buf, n)), buf, n)
                    pos += n
                    t.sent = pos
                    val now = System.nanoTime()
                    if (now - lastEvent > 120_000_000) {
                        lastEvent = now
                        t.rateBps = (pos - offset) / maxOf(1e-9, (now - tAccept) / 1e9)
                        onUpdate(t)
                    }
                }
            }
            set(t, "verifying")
            Protocol.write(out, mapOf("type" to "END", "sha256" to fullHash.get()))
            s.soTimeout = 120_000
            val r = Protocol.read(input)
            if (r.type == "ERROR") throw IOException("receiver asked to resume from ${r.header.long("resume_offset")}")
            if (r.type != "SAVED") throw IOException("no save confirmation")
            t.rateBps = (t.size - offset) / maxOf(1e-9, (System.nanoTime() - tAccept) / 1e9)
            t.timings["accept_to_first_byte_ms"] = r.header.dbl("accept_to_first_byte_ms") ?: 0.0
            t.timings["first_byte_to_saved_ms"] = r.header.dbl("first_byte_to_saved_ms") ?: 0.0
            t.timings["transfer_total_ms"] = (System.nanoTime() - t0) / 1e6
            t.savedName = r.header.str("name") ?: t.name
            t.sent = t.size
            set(t, "saved")
        }
    }

    companion object {
        fun sha256File(f: File): String = sha256Stream(f.inputStream())

        fun sha256Stream(input: java.io.InputStream): String {
            val md = MessageDigest.getInstance("SHA-256")
            input.use { inp ->
                val buf = ByteArray(4 shl 20)
                while (true) { val n = inp.read(buf); if (n <= 0) break; md.update(buf, 0, n) }
            }
            return md.digest().joinToString("") { "%02x".format(it) }
        }

        fun mime(name: String): String = when (name.substringAfterLast('.', "").lowercase()) {
            "pdf" -> "application/pdf"
            "pptx" -> "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            "docx" -> "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            "xlsx" -> "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            "png" -> "image/png"; "jpg", "jpeg" -> "image/jpeg"; "heic" -> "image/heic"
            "mp4" -> "video/mp4"; "mov" -> "video/quicktime"; "txt", "md" -> "text/plain"; "csv" -> "text/csv"
            else -> "application/octet-stream"
        }
    }
}
