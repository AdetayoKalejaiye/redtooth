package app.redtooth.android.net

import android.annotation.SuppressLint
import android.content.Context
import android.content.pm.PackageManager
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import android.net.wifi.aware.AttachCallback
import android.net.wifi.aware.DiscoverySessionCallback
import android.net.wifi.aware.PeerHandle
import android.net.wifi.aware.PublishConfig
import android.net.wifi.aware.PublishDiscoverySession
import android.net.wifi.aware.SubscribeConfig
import android.net.wifi.aware.SubscribeDiscoverySession
import android.net.wifi.aware.WifiAwareManager
import android.net.wifi.aware.WifiAwareNetworkInfo
import android.net.wifi.aware.WifiAwareNetworkSpecifier
import android.net.wifi.aware.WifiAwareSession
import android.os.Handler
import android.os.HandlerThread
import app.redtooth.core.Endpoint
import app.redtooth.core.sha256Hex
import java.io.IOException
import java.net.ServerSocket
import java.net.Socket
import java.util.concurrent.CompletableFuture
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.TimeUnit

/**
 * Direct phone-to-phone links with Wi-Fi Aware (NAN) when there's no shared network.
 *
 * Every Redtooth phone publishes and subscribes to the "redtooth" service, with its device id
 * as service info, so nearby peers are known by id. To send:
 *   1. the sender messages the peer "connect:<its id>",
 *   2. the peer opens a ServerSocket and requests a Wi-Fi Aware network to the sender (PSK + port),
 *   3. the sender requests the matching network, learns the peer's IPv6 address and port from
 *      WifiAwareNetworkInfo, and opens a socket on that network.
 * The same Redtooth protocol then runs over that socket. Android-to-Android only for now; iPhones
 * use Wi-Fi Aware with their own pairing step, and reach Android over the LAN instead.
 *
 * Needs NEARBY_WIFI_DEVICES (Android 13+) or location below that.
 */
@SuppressLint("MissingPermission")
class WifiAwareLink(private val ctx: Context) {
    val supported = ctx.packageManager.hasSystemFeature(PackageManager.FEATURE_WIFI_AWARE)
    private val mgr: WifiAwareManager? = if (supported) ctx.getSystemService(WifiAwareManager::class.java) else null
    private val cm = ctx.getSystemService(ConnectivityManager::class.java)
    private val handler = Handler(HandlerThread("redtooth-aware").apply { start() }.looper)
    private var session: WifiAwareSession? = null
    private var publish: PublishDiscoverySession? = null
    private var subscribe: SubscribeDiscoverySession? = null
    private val peers = ConcurrentHashMap<String, PeerHandle>() // device id -> handle (from our subscription)
    private var myId = ""
    private var onIncoming: (Socket) -> Unit = {}

    private fun psk(a: String, b: String) = sha256Hex("redtooth-aware:${listOf(a, b).sorted().joinToString(":")}".toByteArray()).take(32)

    fun start(deviceId: String, incoming: (Socket) -> Unit) {
        val m = mgr ?: return
        if (!m.isAvailable) return
        myId = deviceId
        onIncoming = incoming
        m.attach(object : AttachCallback() {
            override fun onAttached(s: WifiAwareSession) {
                session = s
                s.publish(PublishConfig.Builder().setServiceName(SERVICE).setServiceSpecificInfo(myId.toByteArray()).build(), object : DiscoverySessionCallback() {
                    override fun onPublishStarted(p: PublishDiscoverySession) { publish = p }
                    override fun onMessageReceived(peer: PeerHandle, message: ByteArray) {
                        val text = String(message)
                        if (text.startsWith("connect:")) acceptFrom(peer, text.removePrefix("connect:"))
                    }
                }, handler)
                s.subscribe(SubscribeConfig.Builder().setServiceName(SERVICE).build(), object : DiscoverySessionCallback() {
                    override fun onSubscribeStarted(sub: SubscribeDiscoverySession) { subscribe = sub }
                    override fun onServiceDiscovered(peer: PeerHandle, info: ByteArray, filter: MutableList<ByteArray>?) {
                        peers[String(info)] = peer
                    }
                    override fun onServiceLost(peer: PeerHandle, reason: Int) { peers.entries.removeIf { it.value == peer } }
                }, handler)
            }
        }, handler)
    }

    fun stop() { publish?.close(); subscribe?.close(); session?.close(); peers.clear() }

    /** A route the sender can use if this device is nearby over Wi-Fi Aware. */
    fun endpointFor(deviceId: String): Endpoint? = if (peers.containsKey(deviceId)) Endpoint("wifi_aware", deviceId, 0) else null

    /** Publisher side: open a listening socket and the data path to the peer that asked. */
    private fun acceptFrom(peer: PeerHandle, peerId: String) {
        val p = publish ?: return
        val server = ServerSocket(0)
        val spec = WifiAwareNetworkSpecifier.Builder(p, peer).setPskPassphrase(psk(myId, peerId)).setPort(server.localPort).build()
        val req = NetworkRequest.Builder().addTransportType(NetworkCapabilities.TRANSPORT_WIFI_AWARE).setNetworkSpecifier(spec).build()
        val cb = object : ConnectivityManager.NetworkCallback() {}
        cm.requestNetwork(req, cb, handler, 30_000)
        Thread {
            try {
                server.soTimeout = 30_000
                val sock = server.accept()
                onIncoming(object : Socket() {
                    override fun getInputStream() = sock.getInputStream()
                    override fun getOutputStream() = sock.getOutputStream()
                    override fun setSoTimeout(timeout: Int) { sock.soTimeout = timeout }
                    override fun setReceiveBufferSize(size: Int) { sock.receiveBufferSize = size }
                    override fun close() { sock.close(); runCatching { cm.unregisterNetworkCallback(cb) } }
                })
            } catch (_: IOException) {
                runCatching { cm.unregisterNetworkCallback(cb) }
            } finally {
                server.close()
            }
        }.start()
    }

    /** Subscriber side: ask the peer to listen, bring up the data path, and connect. */
    fun connect(peerId: String, timeoutS: Long = 20): Socket {
        val sub = subscribe ?: throw IOException("Wi-Fi Aware not running")
        val peer = peers[peerId] ?: throw IOException("peer not nearby")
        sub.sendMessage(peer, 1, "connect:$myId".toByteArray())
        val spec = WifiAwareNetworkSpecifier.Builder(sub, peer).setPskPassphrase(psk(myId, peerId)).build()
        val req = NetworkRequest.Builder().addTransportType(NetworkCapabilities.TRANSPORT_WIFI_AWARE).setNetworkSpecifier(spec).build()
        val ready = CompletableFuture<Pair<Network, WifiAwareNetworkInfo>>()
        val cb = object : ConnectivityManager.NetworkCallback() {
            override fun onCapabilitiesChanged(network: Network, caps: NetworkCapabilities) {
                val info = caps.transportInfo as? WifiAwareNetworkInfo ?: return
                if (info.peerIpv6Addr != null && info.port > 0) ready.complete(network to info)
            }
            override fun onUnavailable() { ready.completeExceptionally(IOException("Wi-Fi Aware link unavailable")) }
        }
        cm.requestNetwork(req, cb, handler, (timeoutS * 1000).toInt())
        val (network, info) = try { ready.get(timeoutS, TimeUnit.SECONDS) } catch (e: Exception) { runCatching { cm.unregisterNetworkCallback(cb) }; throw IOException("Wi-Fi Aware: ${e.message}") }
        val sock = network.socketFactory.createSocket(info.peerIpv6Addr, info.port)
        return object : Socket() { // closing the socket also releases the Wi-Fi Aware data path
            override fun getInputStream() = sock.getInputStream()
            override fun getOutputStream() = sock.getOutputStream()
            override fun setSoTimeout(timeout: Int) { sock.soTimeout = timeout }
            override fun setSendBufferSize(size: Int) { sock.sendBufferSize = size }
            override fun close() { sock.close(); runCatching { cm.unregisterNetworkCallback(cb) } }
        }
    }

    companion object { const val SERVICE = "redtooth" }
}
