package app.redtooth.core

import java.io.IOException
import java.net.HttpURLConnection


/** Phone-number profile directory (see redtooth/directory.py for the server). */
interface Directory {
    fun setDevices(phone: String, devices: List<Device>)
    fun lookup(phones: List<String>): Map<String, Profile>
    fun nudge(toPhone: String, payload: Map<String, Any?>): Boolean
    fun inbox(): List<Map<String, Any?>> = emptyList()
}

class HttpDirectory(baseUrl: String, @Volatile var token: String? = null, private val timeoutMs: Int = 5000) : Directory {
    private val base = baseUrl.trimEnd('/')

    class DirectoryException(val code: Int, msg: String) : IOException(msg)

    private fun req(method: String, path: String, body: Map<String, Any?>? = null): Map<String, Any?> {
        val c = java.net.URI(base + path).toURL().openConnection() as HttpURLConnection
        try {
            c.requestMethod = method
            c.connectTimeout = timeoutMs
            c.readTimeout = timeoutMs
            token?.let { c.setRequestProperty("Authorization", "Bearer $it") }
            if (body != null) {
                c.doOutput = true
                c.setRequestProperty("Content-Type", "application/json")
                c.outputStream.use { it.write(Json.encode(body).toByteArray()) }
            }
            val code = c.responseCode
            if (code !in 200..299) throw DirectoryException(code, "directory $method $path -> $code")
            return Json.obj(c.inputStream.bufferedReader().readText())
        } finally {
            c.disconnect()
        }
    }

    fun startVerification(phone: String) { req("POST", "/v1/verify/start", mapOf("phone" to phone)) }

    fun verify(phone: String, code: String, name: String): String =
        req("POST", "/v1/verify/finish", mapOf("phone" to phone, "code" to code, "name" to name)).str("token")!!.also { token = it }

    /** Directories without phone verification: first claim wins (409 if the number is taken, 403 if it needs a texted code). */
    fun register(phone: String, name: String): String =
        req("POST", "/v1/register", mapOf("phone" to phone, "name" to name)).str("token")!!.also { token = it }

    /** A one-time code for adding another of your devices (it enters the code where a texted code would go). */
    fun linkCode(): String = req("POST", "/v1/me/link", emptyMap<String, Any?>()).str("code")!!

    override fun setDevices(phone: String, devices: List<Device>) {
        req("PUT", "/v1/me/devices", mapOf("devices" to devices.map { it.toMap() }))
    }

    override fun lookup(phones: List<String>): Map<String, Profile> {
        val byHash = phones.associateBy { Phones.hash(it) }
        val res = req("POST", "/v1/lookup", mapOf("hashes" to byHash.keys.toList()))
        val profiles = res.map("profiles") ?: emptyMap()
        @Suppress("UNCHECKED_CAST")
        return profiles.mapNotNull { (h, p) -> byHash[h]?.let { Phones.normalize(it) to Profile.from(p as Map<String, Any?>) } }.toMap()
    }

    override fun nudge(toPhone: String, payload: Map<String, Any?>): Boolean =
        req("POST", "/v1/nudge", mapOf("to" to Phones.hash(toPhone), "payload" to payload)).bool("ok") == true

    @Suppress("UNCHECKED_CAST")
    override fun inbox(): List<Map<String, Any?>> = (req("GET", "/v1/me/inbox").list("messages") ?: emptyList()).map { it as Map<String, Any?> }
}

/** In-memory directory for tests and offline demos. */
class MemoryDirectory : Directory {
    val profiles = mutableMapOf<String, Profile>() // by phone hash
    val inboxes = mutableMapOf<String, MutableList<Map<String, Any?>>>()

    fun register(phone: String, name: String) { profiles[Phones.hash(phone)] = Profile(Phones.normalize(phone), name, profiles[Phones.hash(phone)]?.devices ?: emptyList()) }

    override fun setDevices(phone: String, devices: List<Device>) {
        val p = profiles[Phones.hash(phone)] ?: Profile(Phones.normalize(phone), phone)
        devices.forEach { it.ownerPhone = p.phone; it.ownerName = p.name; if ("directory" !in it.sources) it.sources.add("directory") }
        profiles[Phones.hash(phone)] = p.copy(devices = devices)
    }

    override fun lookup(phones: List<String>) = phones.mapNotNull { ph -> profiles[Phones.hash(ph)]?.let { Phones.normalize(ph) to it } }.toMap()

    override fun nudge(toPhone: String, payload: Map<String, Any?>): Boolean {
        if (Phones.hash(toPhone) !in profiles) return false
        inboxes.getOrPut(Phones.hash(toPhone)) { mutableListOf() }.add(payload)
        return true
    }
}
