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
) {
    fun label(): String = model ?: name

    fun toMap(): Map<String, Any?> = linkedMapOf(
        "id" to id, "name" to name, "kind" to kind, "address" to address, "model" to model, "short_model" to shortModel,
        "owner_phone" to ownerPhone, "owner_name" to ownerName, "paired" to paired, "connected" to connected,
        "discoverable" to discoverable, "rssi" to rssi, "has_app" to hasApp, "endpoints" to endpoints.map { it.toMap() },
        "wifi_direct" to wifiDirect, "sources" to sources, "last_seen" to lastSeen, "label" to label(),
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
            lastSeen = m.dbl("last_seen"),
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
)

object Phones {
    private const val DEFAULT_CC = "1"

    /** E.164-style normalization, identical to the desktop's normalize_phone. */
    fun normalize(raw: String, defaultCc: String = DEFAULT_CC): String {
        if (raw.isBlank()) return ""
        val plus = raw.trim().startsWith("+")
        val digits = raw.filter { it.isDigit() }
        if (plus) return "+$digits"
        if (digits.startsWith("00")) return "+" + digits.substring(2)
        if (digits.length == 10 && defaultCc == "1") return "+1$digits"
        if (digits.length == 11 && digits.startsWith("1") && defaultCc == "1") return "+$digits"
        return "+$defaultCc$digits"
    }

    /** Contact discovery sends these hashes, never raw numbers. */
    fun hash(phone: String): String = sha256Hex(("redtooth:" + normalize(phone)).toByteArray())
}

fun sha256Hex(b: ByteArray): String = MessageDigest.getInstance("SHA-256").digest(b).joinToString("") { "%02x".format(it) }

fun possessive(name: String) = if (name.endsWith("s")) "$name'" else "$name's"
