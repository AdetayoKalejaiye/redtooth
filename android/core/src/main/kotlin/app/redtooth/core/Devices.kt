package app.redtooth.core

/*
 * Find the device a person means, search-first (port of devices.py):
 *   remembered -> registered to their profile -> paired -> discoverable nearby.
 * Outcomes: known (connect), confirm ("Are Daniel's the Sony XM5s?"), choose, pairing_mode.
 */

data class AudioOutput(val name: String, val address: String? = null, val transport: String = "builtin")

/** What the platform can do with Bluetooth. Android implements this; tests use a fake. */
interface BluetoothPort {
    fun paired(): List<Device>
    fun scan(seconds: Double): List<Device>
    fun pair(dev: Device): Boolean
    fun connect(dev: Device): Boolean
    fun disconnect(dev: Device): Boolean
    fun audioOutput(): AudioOutput
    fun setAudioOutput(dev: Device): Boolean

    fun outputMatches(out: AudioOutput, dev: Device): Boolean {
        if (out.address != null && dev.address != null) return normAddr(out.address) == normAddr(dev.address)
        return out.name.isNotEmpty() && (dev.name.contains(out.name, true) || out.name.contains(dev.name, true))
    }

    fun verifyAudioOutput(dev: Device, timeoutMs: Long = 6000): Boolean {
        val deadline = System.currentTimeMillis() + timeoutMs
        var switched = false
        while (System.currentTimeMillis() < deadline) {
            if (outputMatches(audioOutput(), dev)) return true
            if (!switched) { switched = true; runCatching { setAudioOutput(dev) } }
            Thread.sleep(300)
        }
        return false
    }
}

interface RememberedStore {
    fun remember(dev: Device, ownerPhone: String?, ownerName: String?)
    fun remembered(ownerPhone: String? = null): List<Device>
    fun forget(deviceId: String) {}
}

class MemoryRemembered : RememberedStore {
    private val items = LinkedHashMap<String, Device>()
    override fun remember(dev: Device, ownerPhone: String?, ownerName: String?) {
        items.remove(dev.id)
        items[dev.id] = dev.copy(ownerPhone = ownerPhone, ownerName = ownerName, sources = mutableListOf("remembered"))
    }
    override fun remembered(ownerPhone: String?) = items.values.reversed().filter { ownerPhone == null || it.ownerPhone == ownerPhone }.map { it.copy(sources = mutableListOf("remembered")) }
    override fun forget(deviceId: String) { items.remove(deviceId) }
}

fun normAddr(a: String?): String? {
    if (a == null) return null
    val h = a.filter { it.isLetterOrDigit() }.uppercase()
    return if (h.length == 12) h.chunked(2).joinToString(":") else a.uppercase()
}

private val MODEL_NAMES = listOf(
    Triple("WH-?1000XM5", "Sony WH-1000XM5", "Sony XM5s"), Triple("WH-?1000XM4", "Sony WH-1000XM4", "Sony XM4s"),
    Triple("WF-?1000XM5", "Sony WF-1000XM5", "Sony XM5 earbuds"), Triple("AirPods Max", "AirPods Max", "AirPods Max"),
    Triple("AirPods Pro", "AirPods Pro", "AirPods Pro"), Triple("AirPods", "AirPods", "AirPods"),
    Triple("QC ?45|QuietComfort 45", "Bose QuietComfort 45", "Bose QC45s"), Triple("QC ?Ultra|QuietComfort Ultra", "Bose QuietComfort Ultra", "Bose QC Ultras"),
    Triple("Galaxy Buds", "Samsung Galaxy Buds", "Galaxy Buds"), Triple("Pixel Buds", "Google Pixel Buds", "Pixel Buds"),
    Triple("Beats|Powerbeats|Studio Buds", "Beats", "Beats"), Triple("JBL ?Flip", "JBL Flip", "JBL Flip"), Triple("Jabra", "Jabra headset", "Jabra headset"),
)

object DeviceNames {
    fun friendly(name: String): Pair<String?, String?> =
        MODEL_NAMES.firstOrNull { Regex(it.first, RegexOption.IGNORE_CASE).containsMatchIn(name) }?.let { it.second to it.third } ?: (null to null)

    fun classify(name: String, hint: String = ""): String {
        val h = hint.lowercase(); val n = name.lowercase()
        return when {
            listOf("headset", "headphone").any { it in h } -> "headphones"
            "speaker" in h -> "speaker"
            "phone" in h -> "phone"
            "computer" in h || "laptop" in h -> "laptop"
            Regex("airpods|buds|wh-|wf-|xm\\d|qc ?\\d|quietcomfort|headphone|headset|beats|jabra|earbud|momentum|sennheiser").containsMatchIn(n) -> "headphones"
            Regex("speaker|soundlink|flip|charge|boom|soundbar|homepod|sonos").containsMatchIn(n) -> "speaker"
            Regex("iphone|pixel|galaxy s|android|phone").containsMatchIn(n) -> "phone"
            Regex("macbook|laptop|thinkpad|surface|xps|notebook").containsMatchIn(n) -> "laptop"
            Regex("ipad|tablet|tab s").containsMatchIn(n) -> "tablet"
            else -> "other"
        }
    }

    fun enrich(d: Device, hint: String = ""): Device {
        if (d.kind == "other" || d.kind.isEmpty()) d.kind = classify(d.name, hint)
        if (d.model == null) { val (m, s) = friendly(d.name); d.model = m; d.shortModel = s }
        return d
    }
}

private val KIND_GROUPS = mapOf("headphones" to setOf("headphones", "earbuds"), "earbuds" to setOf("headphones", "earbuds"), "speaker" to setOf("speaker"))

fun kindOk(d: Device, kind: String?) = if (kind == null) d.kind in AUDIO_KINDS else d.kind in (KIND_GROUPS[kind] ?: setOf(kind))

fun hintOk(d: Device, hint: String?): Boolean {
    if (hint == null) return false
    val h = hint.lowercase()
    return listOfNotNull(d.name, d.model, d.shortModel).any { h in it.lowercase() }
}

class DeviceResolver(private val bt: BluetoothPort, private val store: RememberedStore, private val me: Person, private val scanSeconds: Double = 4.0) {
    data class Survey(var remembered: List<Device> = emptyList(), var registered: List<Device> = emptyList(), var paired: List<Device> = emptyList(), var nearby: List<Device> = emptyList())
    data class Outcome(val kind: String, val device: Device? = null, val candidates: List<Device> = emptyList(), val verified: Boolean = false, val reason: String = "", val ms: Double = 0.0)

    private fun ownerPhone(o: Person) = if (o.isSelf) me.phone else o.phone
    private fun same(a: Device, b: Device) = a.id == b.id || (a.address != null && normAddr(a.address) == normAddr(b.address))
    private fun merge(d: Device, o: Device): Device {
        o.sources.forEach { if (it !in d.sources) d.sources.add(it) }
        d.paired = d.paired || o.paired; d.connected = d.connected || o.connected; d.discoverable = d.discoverable || o.discoverable
        if (d.rssi == null) d.rssi = o.rssi
        return d
    }

    private fun ownedByOther(d: Device, phone: String?) = store.remembered().any { same(it, d) && it.ownerPhone != null && it.ownerPhone != phone }

    fun find(owner: Person, kind: String?, hint: String?): Outcome {
        val t0 = System.nanoTime()
        val ms = { (System.nanoTime() - t0) / 1e6 }
        val phone = ownerPhone(owner)
        val s = Survey()
        s.paired = bt.paired()
        s.remembered = if (phone != null) store.remembered(phone).filter { kindOk(it, kind) } else emptyList()
        s.registered = owner.profile?.devices?.filter { kindOk(it, kind) } ?: emptyList()
        for (src in listOf(s.remembered, s.registered)) for (d in src) s.paired.firstOrNull { same(d, it) }?.let { return Outcome("known", merge(d, it), ms = ms()) }
        if (owner.isSelf) {
            val own = s.paired.filter { kindOk(it, kind) && !ownedByOther(it, phone) && (hint == null || hintOk(it, hint)) }
                .sortedWith(compareBy({ !it.connected }, { -(it.rssi ?: -100) }))
            if (own.isNotEmpty()) return Outcome("known", own[0], ms = ms())
        }
        s.nearby = bt.scan(scanSeconds)
        for (src in listOf(s.registered, s.remembered)) for (d in src) s.nearby.firstOrNull { same(d, it) }?.let { return Outcome("known", merge(d, it), ms = ms()) }
        val c = plausible(owner, kind, hint, s)
        return when {
            c.isEmpty() -> Outcome("pairing_mode", ms = ms())
            c.size == 1 || c[0].first - c[1].first >= 0.25 -> Outcome("confirm", c[0].second, c.map { it.second }, ms = ms())
            else -> Outcome("choose", null, c.take(4).map { it.second }, ms = ms())
        }
    }

    fun plausible(owner: Person, kind: String?, hint: String?, s: Survey): List<Pair<Double, Device>> {
        val phone = ownerPhone(owner)
        val pool = s.nearby.filter { it.discoverable && kindOk(it, kind) && !ownedByOther(it, phone) }
        val regModels = s.registered.map { (it.model ?: it.name).lowercase() }.toSet()
        var scored = pool.map { d ->
            var sc = 0.1
            if (d.name.contains(owner.first, true)) sc += 0.6
            if (hintOk(d, hint)) sc += 0.3
            if ((d.model ?: d.name).lowercase() in regModels) sc += 0.5
            if ((d.rssi ?: -99) > -65) sc += 0.1
            sc to d
        }
        if (scored.size == 1) scored = listOf(scored[0].first + 0.2 to scored[0].second)
        return scored.filter { (_, d) -> !("'s " in d.name && !d.name.contains(owner.first, true)) }.sortedByDescending { it.first }
    }

    fun connect(dev: Device, owner: Person): Outcome {
        val t0 = System.nanoTime()
        if (!dev.paired) { if (!bt.pair(dev)) return Outcome("failed", dev, reason = "pair"); dev.paired = true }
        if (!bt.connect(dev)) return Outcome("failed", dev, reason = "connect")
        dev.connected = true
        val verified = if (dev.kind in AUDIO_KINDS) bt.verifyAudioOutput(dev) else true
        store.remember(dev, ownerPhone(owner), owner.name)
        return Outcome("connected", dev, verified = verified, ms = (System.nanoTime() - t0) / 1e6)
    }
}
