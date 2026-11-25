package app.redtooth.core

import java.security.MessageDigest

val AUDIO_KINDS = setOf("headphones", "earbuds", "speaker")
val APP_KINDS = setOf("phone", "laptop", "desktop", "tablet")

data class Endpoint(val kind: String, val host: String, val port: Int) {
    fun toMap(): Map<String, Any?> = mapOf("kind" to kind, "host" to host, "port" to port)

    companion object {
        fun from(m: Map<String, Any?>) = Endpoint(m.str("kind") ?: "lan", m.str("host") ?: "", (m.long("port") ?: 0).toInt())
    }
}

/** Same fields as the desktop's Device, so profiles round-trip through the directory. */
data class Device(
    val id: String,
    var name: String,
    var kind: String = "other",
    var address: String? = null,
    var model: String? = null,
    var shortModel: String? = null,
    var ownerPhone: String? = null,
    var ownerName: String? = null,
    var paired: Boolean = false,
    var connected: Boolean = false,
    var discoverable: Boolean = false,
    var rssi: Int? = null,
    var hasApp: Boolean = false,
    var endpoints: List<Endpoint> = emptyList(),
    var wifiDirect: Boolean = false,
    var sources: MutableList<String> = mutableListOf(),
    var lastSeen: Double? = null,
    var present: Boolean? = null,
    var alias: String? = null, // what its owner calls it ("Rob's speaker")
) {
    fun label(): String = alias ?: model ?: name

    fun toMap(): Map<String, Any?> = linkedMapOf(
        "id" to id, "name" to name, "kind" to kind, "address" to address, "model" to model, "short_model" to shortModel,
        "owner_phone" to ownerPhone, "owner_name" to ownerName, "paired" to paired, "connected" to connected,
        "discoverable" to discoverable, "rssi" to rssi, "has_app" to hasApp, "endpoints" to endpoints.map { it.toMap() },
        "wifi_direct" to wifiDirect, "sources" to sources, "last_seen" to lastSeen, "present" to present, "alias" to alias, "label" to label(),
    )

    companion object {
        @Suppress("UNCHECKED_CAST")
        fun from(m: Map<String, Any?>) = Device(
            id = m.str("id") ?: "", name = m.str("name") ?: "", kind = m.str("kind") ?: "other", address = m.str("address"),
            model = m.str("model"), shortModel = m.str("short_model"), ownerPhone = m.str("owner_phone"), ownerName = m.str("owner_name"),
            paired = m.bool("paired") ?: false, connected = m.bool("connected") ?: false, discoverable = m.bool("discoverable") ?: false,
            rssi = m.long("rssi")?.toInt(), hasApp = m.bool("has_app") ?: false,
            endpoints = (m.list("endpoints") ?: emptyList()).mapNotNull { (it as? Map<String, Any?>)?.let(Endpoint::from) },
            wifiDirect = m.bool("wifi_direct") ?: false,
            sources = (m.list("sources") ?: emptyList()).mapNotNull { it as? String }.toMutableList(),
            lastSeen = m.dbl("last_seen"), present = m.bool("present"), alias = m.str("alias"),
        )
    }
}

data class Profile(val phone: String, val name: String, val devices: List<Device> = emptyList()) {
    fun toMap(): Map<String, Any?> = mapOf("phone" to phone, "name" to name, "devices" to devices.map { it.toMap() })

    companion object {
        @Suppress("UNCHECKED_CAST")
        fun from(m: Map<String, Any?>) = Profile(m.str("phone") ?: "", m.str("name") ?: "", (m.list("devices") ?: emptyList()).mapNotNull { (it as? Map<String, Any?>)?.let(Device::from) })
    }
}

data class Contact(
    val id: String,
    val name: String,
    val phones: List<String> = emptyList(),
    val nicknames: List<String> = emptyList(),
    val relation: String? = null,
    val lastInteraction: Double? = null,
)

/** A resolved person: a contact plus their Redtooth profile, if they have one. */
data class Person(val contact: Contact, val profile: Profile? = null, val nearby: Boolean = false, val isSelf: Boolean = false) {
    val name get() = contact.name
    val first get() = contact.name.trim().split(Regex("\\s+")).first()
    val phone: String? get() = profile?.phone ?: contact.phones.firstOrNull()
    val hasApp get() = profile != null
}

data class FileHit(
    val id: String,
    val path: String,
    val name: String,
    val kind: String,
    val ext: String,
    val size: Long,
    val mtime: Double,
    var score: Double = 0.0,
    var snippet: String = "",
    var snippetLabel: String = "",
    var confidence: Double? = null, // re-ranker relevance 0..1, when it ran
    var reasons: List<String> = emptyList(), // usage: "opened recently", "sent before", …
)

data class Option(val id: String, val label: String, val detail: String = "", val payload: Any? = null)

data class Question(val id: String, val kind: String, val prompt: String, val options: List<Option>, val state: MutableMap<String, Any?> = mutableMapOf())

data class Reply(
    var text: String,
    var status: String? = null,
    var question: Question? = null,
    var files: List<FileHit> = emptyList(),
    var device: Device? = null,
    var person: Person? = null,
    var transferId: String? = null,
    var ok: Boolean = true,
    var timings: MutableMap<String, Double> = mutableMapOf(),
    /** Something the app should do: "bluetooth_share" (send [files] with the system Bluetooth share sheet). */
    var action: String? = null,
)

/** Same rules as redtooth/phones.py: E.164 everywhere; the country only matters for numbers written without one. */
object Phones {
    const val DEFAULT_COUNTRY = "US"
    val byIso: Map<String, Country> = COUNTRIES.associateBy { it.iso }

    /** The user's country (the app sets it from the SIM / region at start-up). */
    @Volatile var country: String = DEFAULT_COUNTRY
        set(v) { if (v.uppercase() in byIso) field = v.uppercase() }

    fun normalize(raw: String, country: String? = null): String {
        if (raw.isEmpty()) return ""
        val c = byIso[(country ?: this.country).uppercase()] ?: byIso.getValue(DEFAULT_COUNTRY)
        val start = raw.indexOfFirst { it == '+' || it.isAsciiDigit() }  // skip labels like "tel:"
        if (start < 0) return ""
        val s = raw.substring(start)
        val digits = s.filter { it.isAsciiDigit() }
        if (digits.isEmpty()) return ""
        if (s.startsWith("+")) return "+$digits"
        for (idd in listOf(c.idd, "00")) {
            if (idd.isNotEmpty() && digits.startsWith(idd) && digits.length > idd.length + 6) return "+" + digits.substring(idd.length)
        }
        if (c.cc == "1") return if (digits.length == 11 && digits.startsWith("1")) "+$digits" else "+1$digits"
        if (c.trunk.isNotEmpty() && digits.startsWith(c.trunk)) return "+" + c.cc + digits.substring(c.trunk.length)
        if (c.trunk.isNotEmpty() && digits.startsWith(c.cc) && digits.length >= c.cc.length + 8) return "+$digits"
        return "+" + c.cc + digits
    }

    private fun Char.isAsciiDigit() = this in '0'..'9'

    /** Contact discovery sends these hashes, never raw numbers. */
    fun hash(phone: String): String = sha256Hex(("redtooth:" + normalize(phone)).toByteArray())
}

fun sha256Hex(b: ByteArray): String = MessageDigest.getInstance("SHA-256").digest(b).joinToString("") { "%02x".format(it) }

/** "your Alex's S24" reads badly: drop the owner's name from a device name when we already say whose it is. */
fun dropOwner(label: String, first: String): String {
    val l = label.lowercase(); val f = first.lowercase()
    return if (f.isNotEmpty() && (l.startsWith("$f's ") || l.startsWith("$f’s "))) label.substring(first.length + 3) else label
}

fun possessive(name: String) = if (name.endsWith("s")) "$name'" else "$name's"
