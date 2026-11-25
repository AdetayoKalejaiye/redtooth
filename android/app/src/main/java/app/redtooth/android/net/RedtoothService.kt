package app.redtooth.android.net

import android.app.Service
import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.provider.MediaStore
import app.redtooth.android.Graph
import app.redtooth.android.notify.Notifications
import app.redtooth.core.Device
import app.redtooth.core.Endpoint
import app.redtooth.core.IncomingOffer
import app.redtooth.core.Receiver
import java.io.File
import java.net.Inet4Address
import java.net.NetworkInterface

/**
 * Always-on receiver, so nobody has to put this phone in "receive mode":
 *  - listens for offers on the LAN (port 47800) and over Wi-Fi Aware,
 *  - keeps this device's addresses current in the directory, along with headphones the user shared,
 *  - polls the directory inbox for nudges ("Alex wants to connect to your headphones").
 * Runs as a connectedDevice foreground service with a quiet notification.
 */
class RedtoothService : Service() {
    private var receiver: Receiver? = null
    @Volatile private var running = false
    private var port = 0

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        val n = Notifications.service(this)
        if (Build.VERSION.SDK_INT >= 29) startForeground(Notifications.SERVICE_ID, n, ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE)
        else startForeground(Notifications.SERVICE_ID, n)
        if (!running) start()
        return START_STICKY
    }

    private fun start() {
        running = true
        val s = Graph.settings
        val inbox = File(getExternalFilesDir(null), "Received").apply { mkdirs() }
        val rx = Receiver(s.deviceId, s.deviceName, inbox, { offer -> Graph.broker.ask(offer) }, events = ::onOffer)
        port = try { rx.start(47800) } catch (_: Exception) { rx.start(0) }
        receiver = rx
        Graph.aware.start(s.deviceId) { socket -> Thread { rx.handle(socket) }.start() }
        Thread(::heartbeat, "redtooth-heartbeat").apply { isDaemon = true; start() }
    }

    private fun onOffer(o: IncomingOffer) {
        Graph.offers.value = Graph.offers.value + (o.id to o)
        when (o.status) {
            "receiving" -> Notifications.progress(this, o)
            "saved" -> { val uri = publishToDownloads(File(o.savedPath)); Notifications.saved(this, o, uri) }
            "declined", "cancelled", "failed" -> Notifications.cancelOffer(this, o.id)
        }
    }

    /** Received files land in Downloads/Redtooth where every app and the Files app can see them. */
    private fun publishToDownloads(f: File): android.net.Uri? {
        if (Build.VERSION.SDK_INT < 29 || !f.exists()) return null
        val values = ContentValues().apply {
            put(MediaStore.Downloads.DISPLAY_NAME, f.name)
            put(MediaStore.Downloads.RELATIVE_PATH, "Download/Redtooth")
            put(MediaStore.Downloads.IS_PENDING, 1)
        }
        val uri = contentResolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, values) ?: return null
        contentResolver.openOutputStream(uri)?.use { out -> f.inputStream().use { it.copyTo(out) } }
        contentResolver.update(uri, ContentValues().apply { put(MediaStore.Downloads.IS_PENDING, 0) }, null, null)
        f.delete()
        return uri
    }

    private fun heartbeat() {
        while (running) {
            try {
                val s = Graph.settings
                if (s.registered) {
                    val me = Device(s.deviceId, s.deviceName, "phone", model = Build.MODEL, hasApp = true, wifiDirect = Graph.aware.supported,
                        endpoints = lanAddresses().map { Endpoint("lan", it, port) } + listOfNotNull(Endpoint("wifi_aware", s.deviceId, 0).takeIf { Graph.aware.supported }))
                    val shared = Graph.bluetooth.paired().filter { it.id in s.sharedDeviceIds }.map { it.copy(connected = false, sources = mutableListOf()) }
                    Graph.directory.setDevices(s.phone, listOf(me) + shared)
                    Graph.directory.inbox().forEach { msg ->
                        Graph.nudges.tryEmit(msg)
                        Notifications.nudge(this, msg)
                    }
                    Graph.agent?.refreshPeople()
                }
            } catch (_: Exception) {
            }
            Thread.sleep(20_000)
        }
    }

    override fun onDestroy() {
        running = false
        receiver?.stop()
        Graph.aware.stop()
        super.onDestroy()
    }

    companion object {
        fun start(ctx: Context) {
            val i = Intent(ctx, RedtoothService::class.java)
            if (Build.VERSION.SDK_INT >= 26) ctx.startForegroundService(i) else ctx.startService(i)
        }

        fun lanAddresses(): List<String> = NetworkInterface.getNetworkInterfaces().toList()
            .filter { it.isUp && !it.isLoopback && !it.isVirtual }
            .flatMap { it.inetAddresses.toList() }
            .filterIsInstance<Inet4Address>().filter { it.isSiteLocalAddress }.map { it.hostAddress!! }
    }
}
