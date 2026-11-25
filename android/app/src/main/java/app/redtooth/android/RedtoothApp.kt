package app.redtooth.android

import android.app.Application
import android.content.Context
import android.os.Build
import app.redtooth.android.bt.AndroidBluetooth
import app.redtooth.android.bt.PrefsRemembered
import app.redtooth.android.files.AndroidFileIndex
import app.redtooth.android.net.WifiAwareLink
import app.redtooth.android.notify.Notifications
import app.redtooth.core.Agent
import app.redtooth.core.Contact
import app.redtooth.core.HttpDirectory
import app.redtooth.core.IncomingOffer
import app.redtooth.core.PeopleResolver
import app.redtooth.core.Person
import app.redtooth.core.Phones
import app.redtooth.core.Reply
import app.redtooth.core.Sender
import app.redtooth.core.Transfer
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import java.util.UUID
import java.util.concurrent.CompletableFuture
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit

class RedtoothApp : Application() {
    override fun onCreate() {
        super.onCreate()
        Graph.init(this)
        Notifications.createChannels(this)
    }
}

/** Registration and preferences. */
class Settings(ctx: Context) {
    private val p = ctx.getSharedPreferences("redtooth", Context.MODE_PRIVATE)
    var name: String get() = p.getString("name", "") ?: ""; set(v) = p.edit().putString("name", v).apply()
    var phone: String get() = p.getString("phone", "") ?: ""; set(v) = p.edit().putString("phone", Phones.normalize(v)).apply()
    var directoryUrl: String get() = p.getString("directory", BuildConfig.DEFAULT_DIRECTORY) ?: BuildConfig.DEFAULT_DIRECTORY; set(v) = p.edit().putString("directory", v).apply()
    var token: String? get() = p.getString("token", null); set(v) = p.edit().putString("token", v).apply()
    val deviceId: String get() = p.getString("device_id", null) ?: ("and_" + UUID.randomUUID().toString().take(8)).also { p.edit().putString("device_id", it).apply() }
    val deviceName: String get() = "${name.substringBefore(' ').ifEmpty { "My" }}'s ${Build.MODEL}"
    var sharedDeviceIds: Set<String> get() = p.getStringSet("shared", emptySet()) ?: emptySet(); set(v) = p.edit().putStringSet("shared", v).apply()
    val registered get() = name.isNotEmpty() && phone.isNotEmpty() && token != null
}

/** Incoming offers wait here until the user answers from the notification or the panel. */
class AcceptBroker {
    private val pending = ConcurrentHashMap<String, CompletableFuture<Boolean>>()
    fun ask(offer: IncomingOffer, timeoutS: Long = 120): Boolean {
        val f = pending.computeIfAbsent(offer.id) { CompletableFuture() }
        Graph.offers.value = Graph.offers.value + (offer.id to offer)
        Notifications.showOffer(Graph.app, offer)
        return try { f.get(timeoutS, TimeUnit.SECONDS) } catch (_: Exception) { false } finally { pending.remove(offer.id) }
    }
    fun resolve(id: String, accept: Boolean) { pending[id]?.complete(accept); Notifications.cancelOffer(Graph.app, id) }
}

/** Process-wide objects. Built once; the agent is rebuilt when contacts or registration change. */
object Graph {
    lateinit var app: Application
    lateinit var settings: Settings
    lateinit var directory: HttpDirectory
    lateinit var bluetooth: AndroidBluetooth
    lateinit var files: AndroidFileIndex
    lateinit var sender: Sender
    lateinit var aware: WifiAwareLink
    val broker = AcceptBroker()
    @Volatile var agent: Agent? = null

    /** UI streams. */
    val replies = MutableSharedFlow<Reply>(extraBufferCapacity = 16)
    val status = MutableStateFlow<String?>(null)
    val transfers = MutableStateFlow<Map<String, Transfer>>(emptyMap())
    val offers = MutableStateFlow<Map<String, IncomingOffer>>(emptyMap())
    val nudges = MutableSharedFlow<Map<String, Any?>>(extraBufferCapacity = 8)

    fun init(a: Application) {
        app = a
        settings = Settings(a)
        directory = HttpDirectory(settings.directoryUrl, settings.token)
        bluetooth = AndroidBluetooth(a)
        files = AndroidFileIndex(a)
        aware = WifiAwareLink(a)
        sender = Sender(settings.name, settings.phone, onUpdate = { t -> transfers.value = transfers.value + (t.id to t) })
        sender.connect = { ep -> if (ep.kind == "wifi_aware") aware.connect(ep.host) else java.net.Socket().apply { connect(java.net.InetSocketAddress(ep.host, ep.port), 5000) } }
    }

    fun me() = Person(Contact("c_me", settings.name, listOf(settings.phone)), isSelf = true)

    /** (Re)build the agent with the current contacts. Call off the main thread. */
    fun buildAgent(contacts: List<Contact>): Agent {
        val people = PeopleResolver(contacts, me())
        val a = Agent(me(), people, directory, bluetooth, files, PrefsRemembered(app), sender, onEvent = { kind, v ->
            when (kind) {
                "reply" -> replies.tryEmit(v as Reply)
                "status" -> status.value = v as String
            }
        })
        a.routeFallback = { d -> aware.endpointFor(d.id) }
        a.refreshPeople()
        agent = a
        return a
    }
}
