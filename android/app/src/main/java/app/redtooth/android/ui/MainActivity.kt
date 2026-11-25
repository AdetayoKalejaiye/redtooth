package app.redtooth.android.ui

import android.Manifest
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import android.speech.RecognizerIntent
import androidx.activity.ComponentActivity
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.activity.result.contract.ActivityResultContracts
import androidx.activity.viewModels
import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.foundation.layout.*
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material.icons.filled.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import app.redtooth.android.Graph
import app.redtooth.android.notify.Notifications
import app.redtooth.core.AUDIO_KINDS
import app.redtooth.core.Device
import app.redtooth.core.FileHit
import app.redtooth.core.IncomingOffer
import app.redtooth.core.Person
import app.redtooth.core.Phones
import app.redtooth.core.COUNTRIES
import app.redtooth.core.Reply
import app.redtooth.core.Transfer

class MainActivity : ComponentActivity() {
    private val vm: PanelViewModel by viewModels()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        handle(intent)
        setContent {
            val dark = isSystemInDarkTheme()
            MaterialTheme(colorScheme = if (dark) dynamicDarkColorSchemeOr() else dynamicLightColorSchemeOr()) {
                Surface(Modifier.fillMaxSize(), color = MaterialTheme.colorScheme.surfaceContainerLowest) {
                    val s by vm.state.collectAsStateWithLifecycle()
                    if (!s.registered) Onboarding(vm, s) else Panel(vm, s)
                }
            }
        }
    }

    override fun onNewIntent(intent: Intent) { super.onNewIntent(intent); handle(intent) }

    private fun handle(i: Intent?) {
        i?.getStringExtra(EXTRA_SELECT)?.let { vm.select(Uri.parse(it)) }
        when (i?.getStringExtra("open")) {  // app shortcuts (long-press the icon) and the notification
            "scan" -> vm.scan()
            "devices" -> vm.openDevices(true)
        }
    }

    @Composable private fun dynamicLightColorSchemeOr() = if (Build.VERSION.SDK_INT >= 31) dynamicLightColorScheme(LocalContext.current) else lightColorScheme()
    @Composable private fun dynamicDarkColorSchemeOr() = if (Build.VERSION.SDK_INT >= 31) dynamicDarkColorScheme(LocalContext.current) else darkColorScheme()

    companion object { const val EXTRA_SELECT = "select_uri" }
}

private val PERMISSIONS = buildList {
    add(Manifest.permission.READ_CONTACTS)
    if (Build.VERSION.SDK_INT >= 31) { add(Manifest.permission.BLUETOOTH_SCAN); add(Manifest.permission.BLUETOOTH_CONNECT) } else add(Manifest.permission.ACCESS_FINE_LOCATION)
    if (Build.VERSION.SDK_INT >= 33) { add(Manifest.permission.NEARBY_WIFI_DEVICES); add(Manifest.permission.POST_NOTIFICATIONS); add(Manifest.permission.READ_MEDIA_IMAGES); add(Manifest.permission.READ_MEDIA_VIDEO) }
}.toTypedArray()

@Composable
fun Panel(vm: PanelViewModel, s: PanelState) {
    val ctx = LocalContext.current
    val transfers by Graph.transfers.collectAsStateWithLifecycle()
    val offers by Graph.offers.collectAsStateWithLifecycle()
    val perms = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { vm.refresh() }
    val pickFolder = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocumentTree()) { it?.let(vm::addFolder) }
    val pickFile = rememberLauncherForActivityResult(ActivityResultContracts.OpenDocument()) { it?.let(vm::select) }
    val dictate = rememberLauncherForActivityResult(ActivityResultContracts.StartActivityForResult()) { r ->
        r.data?.getStringArrayListExtra(RecognizerIntent.EXTRA_RESULTS)?.firstOrNull()?.let(vm::ask)
    }
    LaunchedEffect(Unit) { perms.launch(PERMISSIONS) }
    var text by remember { mutableStateOf("") }
    val list = rememberLazyListState()
    LaunchedEffect(s.feed.size) { if (s.feed.isNotEmpty()) list.animateScrollToItem(list.layoutInfo.totalItemsCount) }

    Column(Modifier.fillMaxSize().systemBarsPadding().imePadding().padding(12.dp), verticalArrangement = Arrangement.spacedBy(10.dp)) {
        // "Send it to my phone" for a phone without Redtooth: the system Bluetooth share (the other phone shows Accept).
        LaunchedEffect(Unit) {
            Graph.bluetoothShares.collect { hit ->
                val send = Intent(Intent.ACTION_SEND).setType("*/*").putExtra(Intent.EXTRA_STREAM, Uri.parse(hit.path)).addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
                runCatching { ctx.startActivity(Intent(send).setPackage("com.android.bluetooth")) }
                    .onFailure { ctx.startActivity(Intent.createChooser(send, "Send ${hit.name} with Bluetooth")) }
            }
        }
        AudioCard(s, vm, openSettings = { ctx.startActivity(Intent(Settings.ACTION_BLUETOOTH_SETTINGS)) })
        if (s.devicesOpen) DevicesSheet(s, vm)
        PeopleCard(s.people, onSend = { p -> if (s.selected == null) { vm.sendAfterPick = p; pickFile.launch(arrayOf("*/*")) } else vm.sendTo(p) })
        s.selected?.let { SelectedFile(it) }
        val indexing by produceState(Graph.files.indexing()) { while (true) { value = Graph.files.indexing(); kotlinx.coroutines.delay(1000) } }
        if (!s.hasFolders) AssistChip(onClick = { pickFolder.launch(null) }, label = { Text("Choose folders Redtooth can search") }, leadingIcon = { Icon(Icons.Default.FolderOpen, null) })
        else if (!indexing.complete) Text("Indexing · ${"%,d".format(indexing.done)} of ${"%,d".format(indexing.queued)} files", style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
        LazyColumn(Modifier.weight(1f), state = list, verticalArrangement = Arrangement.spacedBy(8.dp)) {
            items(offers.values.filter { it.status == "waiting" }) { o -> OfferCard(o, vm::respond) }
            items(s.feed) { item ->
                when (item) {
                    is FeedItem.User -> UserBubble(item.text)
                    is FeedItem.Agent -> ReplyCard(item.reply, transfers[item.reply.transferId], vm)
                }
            }
        }
        Row(Modifier.fillMaxWidth().clip(RoundedCornerShape(24.dp)).background(MaterialTheme.colorScheme.surfaceContainerHigh).padding(start = 16.dp, end = 4.dp), verticalAlignment = Alignment.CenterVertically) {
            TextField(text, { text = it }, Modifier.weight(1f), placeholder = { Text("Search or ask") }, singleLine = true,
                colors = TextFieldDefaults.colors(focusedContainerColor = Color.Transparent, unfocusedContainerColor = Color.Transparent, focusedIndicatorColor = Color.Transparent, unfocusedIndicatorColor = Color.Transparent))
            IconButton(onClick = { dictate.launch(Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).putExtra(RecognizerIntent.EXTRA_LANGUAGE_MODEL, RecognizerIntent.LANGUAGE_MODEL_FREE_FORM)) }) { Icon(Icons.Default.Mic, "Dictate") }
            FilledIconButton(onClick = { vm.ask(text); text = "" }, enabled = text.isNotBlank()) { Icon(Icons.AutoMirrored.Filled.Send, "Go") }
        }
    }
}

@Composable
private fun AudioCard(s: PanelState, vm: PanelViewModel, openSettings: () -> Unit) {
    ElevatedCard(shape = RoundedCornerShape(24.dp)) {
        Column(Modifier.padding(16.dp)) {
            if (s.radio == "off") {
                Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(bottom = 10.dp)) {
                    Text("Bluetooth is off.", Modifier.weight(1f), color = MaterialTheme.colorScheme.error)
                    TextButton(onClick = openSettings) { Text("Turn on") }
                }
            }
            Row(verticalAlignment = Alignment.CenterVertically) {
                Box(Modifier.size(40.dp).clip(CircleShape).background(MaterialTheme.colorScheme.primary), contentAlignment = Alignment.Center) {
                    Icon(if (s.audioIsBluetooth) Icons.Default.Headphones else Icons.Default.VolumeUp, null, tint = MaterialTheme.colorScheme.onPrimary)
                }
                Spacer(Modifier.width(12.dp))
                Column(Modifier.weight(1f)) {
                    Text("Playing through", style = MaterialTheme.typography.labelMedium, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    Text(s.audioOutput, style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold, maxLines = 1, overflow = TextOverflow.Ellipsis)
                    Text(s.busy ?: if (s.audioIsBluetooth) "Bluetooth" else "This phone", style = MaterialTheme.typography.labelMedium,
                        color = if (s.busy != null) MaterialTheme.colorScheme.primary else MaterialTheme.colorScheme.onSurfaceVariant)
                }
            }
            // Only what's connected right now.
            s.audioDevices.take(3).forEach { d ->
                HorizontalDivider(Modifier.padding(vertical = 8.dp))
                DeviceRow(d, s, subtitle = "Connected") {
                    FilledTonalButton(onClick = { vm.connect(d) }) { Text("Use") }
                    TextButton(onClick = { vm.disconnect(d) }) { Text("Disconnect") }
                }
            }
            HorizontalDivider(Modifier.padding(vertical = 8.dp))
            val mineCount = s.sharedIds.count { id -> s.paired.any { it.id == id } }
            Row(Modifier.fillMaxWidth().clip(RoundedCornerShape(12.dp)).clickable { vm.openDevices(true) }.padding(vertical = 6.dp), verticalAlignment = Alignment.CenterVertically) {
                Icon(Icons.Default.Headphones, null, Modifier.size(20.dp)); Spacer(Modifier.width(10.dp))
                Column(Modifier.weight(1f)) {
                    Text("Your devices", fontWeight = FontWeight.SemiBold)
                    Text("${if (mineCount > 0) "$mineCount marked as yours" else "Mark which are yours"} · ${s.paired.size} paired with this phone",
                        style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                }
                Icon(Icons.Default.ChevronRight, null)
            }
            Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(top = 6.dp)) {
                FilledTonalButton(onClick = vm::scan, enabled = !s.scanning && s.radio != "off") { Text(if (s.scanning) "Looking…" else if (s.nearby.isNotEmpty()) "Look again" else "Pair new device") }
                Spacer(Modifier.width(10.dp))
                Text(if (s.scanning) "Headphones: pairing mode. Phones: open their Bluetooth screen." else if (s.nearby.isNotEmpty()) "${s.nearby.size} ready to pair" else "",
                    style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            s.nearby.take(6).forEach { d ->
                HorizontalDivider(Modifier.padding(vertical = 6.dp))
                DeviceRow(d, s, subtitle = kindLabel(d.kind) + if ((d.rssi ?: -99) > -65) " · close by" else " · in range") {
                    Button(onClick = { vm.pair(d) }) { Text("Pair") }
                }
            }
            if (s.busy == "Not connected") TextButton(onClick = openSettings) { Text("Open Bluetooth settings") }
        }
    }
}

private fun kindLabel(k: String) = mapOf("headphones" to "Headphones", "earbuds" to "Earbuds", "speaker" to "Speaker", "phone" to "Phone", "laptop" to "Computer",
    "desktop" to "Computer", "tablet" to "Tablet")[k] ?: "Device"

@Composable
private fun DeviceRow(d: Device, s: PanelState, subtitle: String, actions: @Composable RowScope.() -> Unit) {
    Row(verticalAlignment = Alignment.CenterVertically) {
        Icon(when (d.kind) { "speaker" -> Icons.Default.Speaker; "phone" -> Icons.Default.Smartphone; "laptop", "desktop" -> Icons.Default.Laptop; else -> Icons.Default.Headphones }, null, Modifier.size(20.dp))
        Spacer(Modifier.width(10.dp))
        Column(Modifier.weight(1f)) {
            Text(s.aliases[d.id] ?: d.label(), maxLines = 1, overflow = TextOverflow.Ellipsis, fontWeight = FontWeight.Medium)
            Text(subtitle, style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
        actions()
    }
}

/** Your devices (claimed, with the name friends use) and everything paired with this phone. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun DevicesSheet(s: PanelState, vm: PanelViewModel) {
    ModalBottomSheet(onDismissRequest = { vm.openDevices(false) }) {
        LazyColumn(Modifier.padding(horizontal = 20.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            item {
                Text("Your devices", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
                Text("The headphones, speakers and phone that belong to you. A contact who uses Redtooth can then say “connect to ${Graph.settings.name.substringBefore(' ')}'s speaker” and their Redtooth knows exactly which one you mean. Nothing is shared until you add it here.",
                    style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
            }
            val mine = s.paired.filter { it.id in s.sharedIds }
            if (mine.isEmpty()) item { Text("None yet. Pick yours from the list below.", style = MaterialTheme.typography.bodySmall) }
            items(mine) { d ->
                var alias by remember(d.id) { mutableStateOf(s.aliases[d.id] ?: "") }
                Column {
                    DeviceRow(d, s, subtitle = kindLabel(d.kind) + if (d.connected) " · Connected" else " · Not connected") {
                        if (d.kind in AUDIO_KINDS) {
                            if (d.connected) TextButton(onClick = { vm.disconnect(d) }) { Text("Disconnect") } else FilledTonalButton(onClick = { vm.connect(d) }) { Text("Connect") }
                        }
                    }
                    Row(verticalAlignment = Alignment.CenterVertically, modifier = Modifier.padding(start = 30.dp)) {
                        OutlinedTextField(alias, { alias = it }, Modifier.weight(1f), singleLine = true, label = { Text("Name friends use") },
                            placeholder = { Text("${Graph.settings.name.substringBefore(' ')}'s ${kindLabel(d.kind).lowercase()}") })
                        TextButton(onClick = { vm.setMine(d, true, alias) }) { Text("Save") }
                        TextButton(onClick = { vm.setMine(d, false) }) { Text("Remove") }
                    }
                }
            }
            item {
                HorizontalDivider(Modifier.padding(vertical = 8.dp))
                Text("Paired with this phone", style = MaterialTheme.typography.titleMedium, fontWeight = FontWeight.SemiBold)
            }
            items(s.paired.filter { it.id !in s.sharedIds }) { d ->
                DeviceRow(d, s, subtitle = kindLabel(d.kind) + if (d.connected) " · Connected" else " · Not connected") {
                    if (d.kind in AUDIO_KINDS && !d.connected) TextButton(onClick = { vm.connect(d) }) { Text("Connect") }
                    if (d.kind in AUDIO_KINDS && d.connected) TextButton(onClick = { vm.disconnect(d) }) { Text("Disconnect") }
                    TextButton(onClick = { vm.setMine(d, true) }) { Text("This is mine") }
                }
            }
            item {
                HorizontalDivider(Modifier.padding(vertical = 8.dp))
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Column(Modifier.weight(1f)) {
                        Text("Start when the phone starts", fontWeight = FontWeight.Medium)
                        Text("Keeps Redtooth ready to receive files, like the desktop tray app.", style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                    Switch(s.startAtBoot, vm::setStartAtBoot)
                }
                Spacer(Modifier.height(24.dp))
            }
        }
    }
}

@Composable
private fun PeopleCard(people: List<Person>, onSend: (Person) -> Unit) {
    ElevatedCard(shape = RoundedCornerShape(24.dp)) {
        Column(Modifier.padding(16.dp)) {
            Text("People", style = MaterialTheme.typography.titleSmall)
            Spacer(Modifier.height(8.dp))
            if (people.isEmpty()) Text("No one who uses Redtooth is nearby yet", color = MaterialTheme.colorScheme.onSurfaceVariant, style = MaterialTheme.typography.bodySmall)
            LazyRow(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                items(people) { p ->
                    Column(Modifier.width(72.dp).clip(RoundedCornerShape(16.dp)).clickable { onSend(p) }.padding(6.dp), horizontalAlignment = Alignment.CenterHorizontally) {
                        Box(contentAlignment = Alignment.TopEnd) {
                            Box(Modifier.size(48.dp).clip(CircleShape).background(MaterialTheme.colorScheme.tertiaryContainer), contentAlignment = Alignment.Center) {
                                Text(p.name.split(" ").take(2).joinToString("") { it.take(1) }.uppercase(), fontWeight = FontWeight.SemiBold)
                            }
                            if (p.nearby) Box(Modifier.size(12.dp).clip(CircleShape).background(Color(0xFF34C759)))
                        }
                        Text(p.first, style = MaterialTheme.typography.labelMedium, maxLines = 1)
                        Text("Send file", style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.primary)
                    }
                }
            }
        }
    }
}

@Composable private fun SelectedFile(f: FileHit) = OutlinedCard(shape = RoundedCornerShape(16.dp)) {
    Row(Modifier.padding(12.dp), verticalAlignment = Alignment.CenterVertically) {
        Icon(Icons.Default.Description, null)
        Spacer(Modifier.width(10.dp))
        Column { Text("Selected", style = MaterialTheme.typography.labelSmall); Text(f.name, fontWeight = FontWeight.SemiBold, maxLines = 1, overflow = TextOverflow.Ellipsis) }
    }
}

@Composable private fun UserBubble(text: String) = Box(Modifier.fillMaxWidth(), contentAlignment = Alignment.CenterEnd) {
    Text(text, Modifier.clip(RoundedCornerShape(18.dp, 18.dp, 4.dp, 18.dp)).background(MaterialTheme.colorScheme.primary).padding(horizontal = 14.dp, vertical = 8.dp), color = MaterialTheme.colorScheme.onPrimary)
}

@Composable
private fun ReplyCard(r: Reply, t: Transfer?, vm: PanelViewModel) {
    Card(shape = RoundedCornerShape(18.dp), colors = CardDefaults.cardColors(containerColor = MaterialTheme.colorScheme.surfaceContainer)) {
        Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text(r.text)
            r.question?.let { q ->
                q.options.forEach { o ->
                    val yes = o.id == "yes"
                    (if (yes) ButtonDefaults.buttonColors() else ButtonDefaults.filledTonalButtonColors()).let { colors ->
                        Button(onClick = { vm.answer(q, o.id, o.label) }, colors = colors, modifier = Modifier.fillMaxWidth(), shape = RoundedCornerShape(12.dp)) {
                            Column(Modifier.fillMaxWidth()) {
                                Text(o.label, fontWeight = FontWeight.SemiBold)
                                if (o.detail.isNotEmpty()) Text(o.detail, style = MaterialTheme.typography.labelSmall)
                            }
                        }
                    }
                }
            }
            if (r.question == null && r.transferId == null) r.files.take(3).forEach { f ->
                Row(verticalAlignment = Alignment.CenterVertically) {
                    Icon(Icons.Default.InsertDriveFile, null, Modifier.size(18.dp)); Spacer(Modifier.width(8.dp))
                    Column { Text(f.name, fontWeight = FontWeight.Medium); if (f.snippet.isNotEmpty()) Text(f.snippet, style = MaterialTheme.typography.labelSmall, maxLines = 1, overflow = TextOverflow.Ellipsis) }
                }
            }
            t?.let { TransferRow(it, vm::cancel) }
            val ms = r.timings["find_match_ms"] ?: r.timings["find_device_ms"]
            if (ms != null) Text("Match ${"%.0f".format(ms)} ms" + (r.timings["connect_ms"]?.let { " · Connect ${"%.0f".format(it)} ms" } ?: ""), style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
        }
    }
}

@Composable
private fun TransferRow(t: Transfer, cancel: (String) -> Unit) {
    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Text(t.label, Modifier.weight(1f), style = MaterialTheme.typography.labelLarge,
                color = when (t.status) { "saved" -> Color(0xFF248A3D); "declined", "failed" -> MaterialTheme.colorScheme.error; else -> MaterialTheme.colorScheme.primary })
            if (t.status in setOf("waiting", "sending", "preparing", "interrupted")) TextButton(onClick = { cancel(t.id) }) { Text("Cancel") }
        }
        LinearProgressIndicator(progress = { if (t.status == "saved") 1f else if (t.size > 0) t.sent.toFloat() / t.size else 0f }, Modifier.fillMaxWidth())
        val tm = t.timings
        if (tm.containsKey("accept_to_first_byte_ms")) Text("${t.route?.kind?.replace('_', ' ') ?: ""} · Accept → first byte ${"%.0f".format(tm["accept_to_first_byte_ms"])} ms · → saved ${"%.0f".format(tm["first_byte_to_saved_ms"])} ms",
            style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}

@Composable
private fun OfferCard(o: IncomingOffer, respond: (String, Boolean) -> Unit) = Card(shape = RoundedCornerShape(18.dp)) {
    Column(Modifier.padding(14.dp)) {
        Text("${o.senderName} wants to send you ${o.name}", fontWeight = FontWeight.SemiBold)
        Text(Notifications.human(o.size), style = MaterialTheme.typography.labelSmall)
        Row(horizontalArrangement = Arrangement.spacedBy(8.dp), modifier = Modifier.padding(top = 8.dp)) {
            OutlinedButton(onClick = { respond(o.id, false) }, Modifier.weight(1f)) { Text("Decline") }
            Button(onClick = { respond(o.id, true) }, Modifier.weight(1f)) { Text("Accept") }
        }
    }
}

@Composable
fun Onboarding(vm: PanelViewModel, s: PanelState) {
    var name by remember { mutableStateOf(Graph.settings.name) }
    var phone by remember { mutableStateOf(Graph.settings.phone) }
    var dir by remember { mutableStateOf(Graph.settings.directoryUrl) }
    var code by remember { mutableStateOf("") }
    var country by remember { mutableStateOf(Graph.settings.country) }
    Column(Modifier.fillMaxSize().systemBarsPadding().padding(24.dp), verticalArrangement = Arrangement.spacedBy(12.dp)) {
        Text("Redtooth", fontSize = 28.sp, fontWeight = FontWeight.Bold)
        Text("Friends find your devices by your phone number. We'll text you a code to confirm it's yours.", color = MaterialTheme.colorScheme.onSurfaceVariant)
        OutlinedTextField(name, { name = it }, label = { Text("Your name") }, modifier = Modifier.fillMaxWidth())
        CountryPicker(country) { country = it }
        val cc = Phones.byIso[country]?.cc ?: "1"
        OutlinedTextField(phone, { phone = it }, label = { Text("Phone number") }, prefix = { if (!phone.trimStart().startsWith("+")) Text("+$cc ") },
            keyboardOptions = androidx.compose.foundation.text.KeyboardOptions(keyboardType = androidx.compose.ui.text.input.KeyboardType.Phone),
            supportingText = { Text("Contacts saved without a country code are read as ${Phones.byIso[country]?.name ?: country}.") },
            modifier = Modifier.fillMaxWidth())
        OutlinedTextField(dir, { dir = it }, label = { Text("Directory") }, modifier = Modifier.fillMaxWidth(), supportingText = { Text("Your group's Redtooth directory") })
        if (!s.verificationSent) Button(onClick = { vm.requestCode(name, phone, dir, country) }, enabled = name.isNotBlank() && phone.isNotBlank(), modifier = Modifier.fillMaxWidth()) { Text("Send code") }
        else {
            OutlinedTextField(code, { code = it }, label = { Text("Code") }, modifier = Modifier.fillMaxWidth())
            Button(onClick = { vm.verify(code) }, enabled = code.length >= 4, modifier = Modifier.fillMaxWidth()) { Text("Verify") }
        }
        s.error?.let { Text(it, color = MaterialTheme.colorScheme.error) }
    }
}

/** Country for phone numbers: searchable dropdown of every country Redtooth knows the rules for. */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun CountryPicker(iso: String, onPick: (String) -> Unit) {
    var open by remember { mutableStateOf(false) }
    var query by remember { mutableStateOf("") }
    val current = Phones.byIso[iso]
    ExposedDropdownMenuBox(open, { open = it }) {
        OutlinedTextField(
            value = if (open) query else current?.let { "${flag(it.iso)} ${it.name} (+${it.cc})" } ?: iso,
            onValueChange = { query = it; open = true },
            label = { Text("Country") },
            trailingIcon = { ExposedDropdownMenuDefaults.TrailingIcon(open) },
            singleLine = true,
            modifier = Modifier.menuAnchor(MenuAnchorType.PrimaryEditable).fillMaxWidth(),
        )
        val q = query.trim().lowercase().removePrefix("+")
        val matches = COUNTRIES.sortedBy { it.name }.filter { q.isEmpty() || it.name.lowercase().startsWith(q) || it.iso.lowercase() == q || it.cc == q }
        ExposedDropdownMenu(open, { open = false; query = "" }) {
            matches.take(60).forEach { c ->
                DropdownMenuItem(text = { Text("${flag(c.iso)} ${c.name}  +${c.cc}") }, onClick = { onPick(c.iso); open = false; query = "" })
            }
        }
    }
}

private fun flag(iso: String) = iso.uppercase().map { String(Character.toChars(0x1F1E6 + (it - 'A'))) }.joinToString("")
