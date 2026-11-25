package app.redtooth.core

import java.io.DataInputStream
import java.io.EOFException
import java.io.InputStream
import java.io.OutputStream
import java.net.InetSocketAddress
import java.net.Socket

/**
 * Redtooth wire format (docs/PROTOCOL.md): 4-byte big-endian header length, a JSON header,
 * then `len` payload bytes for DATA frames. Phones verify chunks with SHA-256 ("s256").
 */
object Protocol {
    const val MAX_HEADER = 64 * 1024
    const val MAX_CHUNK = 8 * 1024 * 1024
    const val CHUNK = 1024 * 1024
    val CHUNK_HASHES = listOf("s256")

    class Frame(val header: Map<String, Any?>, val payload: ByteArray = ByteArray(0)) {
        val type: String? get() = header.str("type")
    }

    class ProtocolException(msg: String) : Exception(msg)

    fun write(out: OutputStream, header: Map<String, Any?>, payload: ByteArray? = null, payloadLen: Int = payload?.size ?: 0) {
        val h = if (payloadLen > 0) header + ("len" to payloadLen) else header
        val raw = Json.encode(h).toByteArray(Charsets.UTF_8)
        val buf = ByteArray(4 + raw.size)
        buf[0] = (raw.size ushr 24).toByte(); buf[1] = (raw.size ushr 16).toByte(); buf[2] = (raw.size ushr 8).toByte(); buf[3] = raw.size.toByte()
        System.arraycopy(raw, 0, buf, 4, raw.size)
        out.write(buf)
        if (payload != null && payloadLen > 0) out.write(payload, 0, payloadLen)
        out.flush()
    }

    fun read(input: InputStream): Frame {
        val din = if (input is DataInputStream) input else DataInputStream(input)
        val n = din.readInt()
        if (n < 0 || n > MAX_HEADER) throw ProtocolException("header too large")
        val raw = ByteArray(n)
        din.readFully(raw)
        val header = Json.obj(String(raw, Charsets.UTF_8))
        val len = (header.long("len") ?: 0L).toInt()
        if (len < 0 || len > MAX_CHUNK) throw ProtocolException("chunk too large")
        val payload = ByteArray(len)
        if (len > 0) din.readFully(payload)
        return Frame(header, payload)
    }

    fun s256(b: ByteArray, len: Int = b.size): String {
        val md = java.security.MessageDigest.getInstance("SHA-256")
        md.update(b, 0, len)
        return md.digest().joinToString("") { "%02x".format(it) }
    }

    /** PING a device; returns round-trip ms if it answers with the expected id. */
    fun ping(host: String, port: Int, deviceId: String, timeoutMs: Int = 1500): Double? = try {
        val t0 = System.nanoTime()
        Socket().use { s ->
            s.connect(InetSocketAddress(host, port), timeoutMs)
            s.soTimeout = timeoutMs
            write(s.getOutputStream(), mapOf("type" to "PING"))
            val f = read(s.getInputStream())
            if (f.type == "PONG" && f.header.str("device_id") == deviceId) (System.nanoTime() - t0) / 1e6 else null
        }
    } catch (e: Exception) {
        if (e is EOFException || e is java.io.IOException || e is ProtocolException) null else throw e
    }
}
