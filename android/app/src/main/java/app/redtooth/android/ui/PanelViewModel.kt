package app.redtooth.android.ui

import android.app.Application
import android.net.Uri
import android.provider.ContactsContract
import android.provider.OpenableColumns
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import app.redtooth.android.Graph
import app.redtooth.android.net.RedtoothService
import app.redtooth.core.AUDIO_KINDS
import app.redtooth.core.Contact
import app.redtooth.core.Device
import app.redtooth.core.FileHit
import app.redtooth.core.Person
import app.redtooth.core.Phones
import app.redtooth.core.Question
import app.redtooth.core.Reply
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

sealed interface FeedItem {
    data class User(val text: String) : FeedItem
    data class Agent(val reply: Reply) : FeedItem
}

data class PanelState(
    val registered: Boolean = false,
    val ready: Boolean = false,
    val audioOutput: String = "Phone speaker",
    val audioIsBluetooth: Boolean = false,
    val audioDevices: List<Device> = emptyList(),
    val sharedIds: Set<String> = emptySet(),
    val aliases: Map<String, String> = emptyMap(),
    val paired: List<Device> = emptyList(),
    val nearby: List<Device> = emptyList(),
    val scanning: Boolean = false,
    val radio: String = "on",
    val devicesOpen: Boolean = false,
    val startAtBoot: Boolean = true,
    val people: List<Person> = emptyList(),
    val feed: List<FeedItem> = emptyList(),
    val selected: FileHit? = null,
    val busy: String? = null,
    val hasFolders: Boolean = false,
    val verificationSent: Boolean = false,
    val codeKind: String = "sms",     // "sms": we texted a code; "link": the number is taken, enter a link code
    val linkCode: String? = null,
    val error: String? = null,
)

/** Drives the panel. All agent calls run on the IO dispatcher; the agent itself lives in the tested core. */
class PanelViewModel(app: Application) : AndroidViewModel(app) {
    val state = MutableStateFlow(PanelState(registered = Graph.settings.registered, hasFolders = Graph.files.folders.isNotEmpty()))

    init {
        viewModelScope.launch { Graph.replies.collect { r -> push(FeedItem.Agent(r)); refresh() } }
        viewModelScope.launch { Graph.status.collect { s -> state.update { it.copy(busy = s) } } }
        if (Graph.settings.registered) start()
    }

    private fun push(item: FeedItem) = state.update { it.copy(feed = (it.feed + item).takeLast(40)) }

    fun start() = viewModelScope.launch(Dispatchers.IO) {
        Graph.buildAgent(loadContacts())
        RedtoothService.start(getApplication())
        Graph.files.reindex()
        state.update { it.copy(ready = true) }
        refresh()
    }

    fun refresh() = viewModelScope.launch(Dispatchers.IO) {
        val agent = Graph.agent ?: return@launch
        val bt = Graph.bluetooth
        val out = runCatching { bt.audioOutput() }.getOrNull()
        val paired = runCatching { bt.paired() }.getOrDefault(emptyList())
        // Main card: only audio devices connected right now; everything else is in Your devices.
        val audio = paired.filter { it.kind in AUDIO_KINDS && it.connected && !(out != null && bt.outputMatches(out, it)) }
        Graph.refreshMine(agent)
        state.update {
            it.copy(
                audioOutput = out?.name ?: "Phone speaker", audioIsBluetooth = out?.transport == "bluetooth",
                audioDevices = audio, sharedIds = Graph.settings.sharedDeviceIds, aliases = Graph.settings.deviceAliases,
                paired = paired.sortedWith(compareBy({ it.kind !in AUDIO_KINDS }, { !it.connected }, { it.name })),
                radio = runCatching { bt.radioState() }.getOrDefault("on"), startAtBoot = Graph.settings.startAtBoot,
                people = agent.people.allPeople().filter { p -> p.hasApp || p.nearby }.take(8),
                selected = agent.ctx.file, hasFolders = Graph.files.folders.isNotEmpty(), busy = null,
            )
        }
    }

    private fun run(userText: String?, call: () -> Reply) = viewModelScope.launch {
        userText?.let { push(FeedItem.User(it)) }
        val r = withContext(Dispatchers.IO) { runCatching(call).getOrElse { e -> Reply("Something went wrong: ${e.message}", ok = false) } }
        push(FeedItem.Agent(r))
        if (r.action == "bluetooth_share") r.files.firstOrNull()?.let { Graph.bluetoothShares.tryEmit(it) }
        refresh()
    }

    fun ask(text: String) { if (text.isNotBlank()) Graph.agent?.let { a -> run(text) { a.ask(text) } } }
    fun answer(q: Question, optionId: String, label: String) { Graph.agent?.let { a -> run(label) { a.answer(q.id, optionId) } } }
    fun connect(d: Device) { Graph.agent?.let { a -> run(null) { a.connectDevice(d.id) } } }
    fun sendTo(p: Person) { Graph.agent?.let { a -> run(null) { a.sendFile(p.contact.id) } } }
    fun cancel(id: String) { Graph.sender.cancel(id) }
    fun respond(offerId: String, accept: Boolean) = Graph.broker.resolve(offerId, accept)

    /** Set when the user tapped a person before choosing a file; the pick then sends to them. */
    @Volatile var sendAfterPick: Person? = null

    /** Share sheet or file picker: the file arrives selected. */
    fun select(uri: Uri) = viewModelScope.launch(Dispatchers.IO) {
        val cr = getApplication<Application>().contentResolver
        var name = "file"; var size = 0L
        cr.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME, OpenableColumns.SIZE), null, null, null)?.use { c -> if (c.moveToFirst()) { name = c.getString(0) ?: name; size = c.getLong(1) } }
        val hit = Graph.files.hitFor(uri, name, size, System.currentTimeMillis())
        val agent = Graph.agent ?: return@launch
        val r = agent.selectFile(hit)
        val target = sendAfterPick.also { sendAfterPick = null }
        val sent = target?.let { agent.sendFile(it.contact.id) }
        withContext(Dispatchers.Main) { push(FeedItem.Agent(sent ?: r)) }
        refresh()
    }

    fun addFolder(tree: Uri) = viewModelScope.launch(Dispatchers.IO) { Graph.files.addFolder(tree); refresh() }

    // ------------------------------------------------------------------ devices
    fun openDevices(open: Boolean) = state.update { it.copy(devicesOpen = open) }

    /** "Pair new device": discovery for ~10 s; headphones in pairing mode, phones on their Bluetooth screen. */
    fun scan() = viewModelScope.launch(Dispatchers.IO) {
        state.update { it.copy(scanning = true, nearby = emptyList()) }
        val found = runCatching { Graph.bluetooth.scan(10.0) }.getOrDefault(emptyList())
        state.update { it.copy(scanning = false, nearby = found) }
    }

    fun pair(d: Device) = run(null) {
        val ok = Graph.bluetooth.pair(d)
        state.update { s -> s.copy(nearby = s.nearby.filter { it.id != d.id }) }
        if (!ok) Reply("${d.name} didn't accept pairing. Put it back in pairing mode and try again.", ok = false)
        else if (d.kind in AUDIO_KINDS) Graph.agent?.connectDevice(d.id) ?: Reply("Paired ${d.name}.")
        else Reply("Paired ${d.name}.")
    }

    fun disconnect(d: Device) = run(null) {
        if (Graph.bluetooth.disconnect(d)) Reply("Disconnected ${d.label()}.", status = "Disconnected")
        else Reply("${d.label()} is still connected. Android doesn't let apps disconnect it on this phone; use Bluetooth settings.", ok = false)
    }

    /** Mark a paired device as yours (contacts can ask for it by name), with the name friends use. */
    fun setMine(d: Device, mine: Boolean, alias: String? = null) {
        val s = Graph.settings
        s.sharedDeviceIds = if (mine) s.sharedDeviceIds + d.id else s.sharedDeviceIds - d.id
        val a = s.deviceAliases.toMutableMap()
        if (mine && !alias.isNullOrBlank()) a[d.id] = alias.trim() else if (!mine || alias != null) a.remove(d.id)
        s.deviceAliases = a
        refresh()
    }

    fun setStartAtBoot(on: Boolean) { Graph.settings.startAtBoot = on; refresh() }

    // ------------------------------------------------------------------ onboarding
    fun requestCode(name: String, phone: String, directory: String, country: String) = viewModelScope.launch(Dispatchers.IO) {
        val s = Graph.settings
        s.country = country // before the phone number, which is read with it
        s.name = name.trim(); s.phone = phone; s.directoryUrl = directory.trim()
        Graph.init(getApplication()) // rebuild clients with the new settings
        // No phone verification on most directories: register straight away (first claim wins).
        val reg = runCatching { Graph.directory.register(s.phone, s.name) }
        reg.onSuccess { tok -> s.token = tok; Graph.init(getApplication()); state.update { st -> st.copy(registered = true, error = null) }; start(); return@launch }
        when ((reg.exceptionOrNull() as? app.redtooth.core.HttpDirectory.DirectoryException)?.code) {
            409 -> state.update { it.copy(verificationSent = true, codeKind = "link", error = null) }
            403 -> {
                val r = runCatching { Graph.directory.startVerification(s.phone) }
                state.update { it.copy(verificationSent = r.isSuccess, codeKind = "sms", error = r.exceptionOrNull()?.message) }
            }
            400 -> state.update { it.copy(error = "Check the number, including the country code.") }
            429 -> state.update { it.copy(error = "Too many attempts. Try again in a few minutes.") }
            else -> state.update { it.copy(error = "Couldn't reach the directory (${reg.exceptionOrNull()?.message}).") }
        }
    }

    /** Settings › Link another device: a code to enter on your other device. */
    fun getLinkCode() = viewModelScope.launch(Dispatchers.IO) {
        val r = runCatching { Graph.directory.linkCode() }
        state.update { it.copy(linkCode = r.getOrNull() ?: "Couldn't get a code (${r.exceptionOrNull()?.message})") }
    }

    fun verify(code: String) = viewModelScope.launch(Dispatchers.IO) {
        val s = Graph.settings
        val r = runCatching { Graph.directory.verify(s.phone, code.trim(), s.name) }
        r.onSuccess { tok -> s.token = tok; Graph.init(getApplication()); state.update { st -> st.copy(registered = true, error = null) }; start() }
        r.onFailure { e -> state.update { it.copy(error = "That code didn't work (${e.message}).") } }
    }

    private fun loadContacts(): List<Contact> {
        val out = LinkedHashMap<String, Contact>()
        val cr = getApplication<Application>().contentResolver
        runCatching {
            cr.query(ContactsContract.CommonDataKinds.Phone.CONTENT_URI,
                arrayOf(ContactsContract.CommonDataKinds.Phone.CONTACT_ID, ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME, ContactsContract.CommonDataKinds.Phone.NUMBER, ContactsContract.CommonDataKinds.Phone.LAST_TIME_CONTACTED),
                null, null, null)?.use { c ->
                while (c.moveToNext()) {
                    val id = "c_" + c.getString(0); val name = c.getString(1) ?: continue; val number = c.getString(2) ?: continue
                    val prev = out[id]
                    out[id] = Contact(id, name, ((prev?.phones ?: emptyList()) + Phones.normalize(number)).distinct(), lastInteraction = c.getLong(3).takeIf { it > 0 }?.div(1000.0))
                }
            }
        }
        return out.values.toList()
    }
}
