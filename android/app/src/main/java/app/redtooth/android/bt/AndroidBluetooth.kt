package app.redtooth.android.bt

import android.annotation.SuppressLint
import android.bluetooth.BluetoothA2dp
import android.bluetooth.BluetoothAdapter
import android.bluetooth.BluetoothClass
import android.bluetooth.BluetoothDevice
import android.bluetooth.BluetoothManager
import android.bluetooth.BluetoothProfile
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.media.AudioDeviceInfo
import android.media.AudioManager
import android.os.Build
import app.redtooth.core.AudioOutput
import app.redtooth.core.BluetoothPort
import app.redtooth.core.Device
import app.redtooth.core.DeviceNames
import app.redtooth.core.Json
import app.redtooth.core.RememberedStore
import java.util.concurrent.ConcurrentHashMap
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * Android Bluetooth for the agent.
 *
 * - Discovery and pairing are public APIs (startDiscovery, createBond).
 * - There's no public call to connect A2DP; after bonding, Android normally connects a headset by itself.
 *   We wait for that, then try BluetoothA2dp.connect via reflection, which some devices still allow.
 *   If neither works, connect() returns false and the panel offers Bluetooth settings.
 * - Media follows the connected A2DP device; there's no public API to force the route, so
 *   setAudioOutput() only reports whether it already is the output.
 *
 * Callers must hold BLUETOOTH_SCAN and BLUETOOTH_CONNECT (the panel requests them).
 */
@SuppressLint("MissingPermission")
class AndroidBluetooth(private val ctx: Context) : BluetoothPort {
    private val adapter: BluetoothAdapter? = (ctx.getSystemService(Context.BLUETOOTH_SERVICE) as BluetoothManager).adapter
    private val audio = ctx.getSystemService(Context.AUDIO_SERVICE) as AudioManager
    @Volatile private var a2dp: BluetoothA2dp? = null

    init {
        adapter?.getProfileProxy(ctx, object : BluetoothProfile.ServiceListener {
            override fun onServiceConnected(profile: Int, proxy: BluetoothProfile) { a2dp = proxy as BluetoothA2dp }
            override fun onServiceDisconnected(profile: Int) { a2dp = null }
        }, BluetoothProfile.A2DP)
    }

    val available get() = adapter?.isEnabled == true

    private fun kindOf(d: BluetoothDevice): String {
        val cls = d.bluetoothClass ?: return "other"
        return when (cls.majorDeviceClass) {
            BluetoothClass.Device.Major.AUDIO_VIDEO -> when (cls.deviceClass) {
                BluetoothClass.Device.AUDIO_VIDEO_LOUDSPEAKER, BluetoothClass.Device.AUDIO_VIDEO_PORTABLE_AUDIO, BluetoothClass.Device.AUDIO_VIDEO_HIFI_AUDIO -> "speaker"
                else -> "headphones"
            }
            BluetoothClass.Device.Major.PHONE -> "phone"
            BluetoothClass.Device.Major.COMPUTER -> "laptop"
            else -> "other"
        }
    }

    private fun toDevice(d: BluetoothDevice, source: String, rssi: Int? = null): Device {
        val connected = a2dp?.connectedDevices?.any { it.address == d.address } == true
        return DeviceNames.enrich(Device(
            id = "bt:${d.address}", name = d.name ?: d.address, kind = kindOf(d), address = d.address,
            paired = d.bondState == BluetoothDevice.BOND_BONDED, connected = connected,
            discoverable = source == "nearby", rssi = rssi, sources = mutableListOf(source),
        ))
    }

    override fun paired(): List<Device> = adapter?.bondedDevices?.map { toDevice(it, "paired") } ?: emptyList()

    /** Discoverable devices in range (headphones in pairing mode). Blocks for [seconds]. */
    override fun scan(seconds: Double): List<Device> {
        val ad = adapter ?: return emptyList()
        if (seconds <= 0) return emptyList()
        val found = ConcurrentHashMap<String, Device>()
        val rx = object : BroadcastReceiver() {
            override fun onReceive(c: Context, i: Intent) {
                val d: BluetoothDevice = if (Build.VERSION.SDK_INT >= 33) i.getParcelableExtra(BluetoothDevice.EXTRA_DEVICE, BluetoothDevice::class.java) ?: return
                else @Suppress("DEPRECATION") (i.getParcelableExtra(BluetoothDevice.EXTRA_DEVICE) ?: return)
                val rssi = i.getShortExtra(BluetoothDevice.EXTRA_RSSI, Short.MIN_VALUE).toInt().takeIf { it != Short.MIN_VALUE.toInt() }
                found[d.address] = toDevice(d, "nearby", rssi)
            }
        }
        ctx.registerReceiver(rx, IntentFilter(BluetoothDevice.ACTION_FOUND))
        try {
            ad.startDiscovery()
            Thread.sleep((seconds * 1000).toLong())
        } finally {
            ad.cancelDiscovery()
            ctx.unregisterReceiver(rx)
        }
        return found.values.toList()
    }

    override fun pair(dev: Device): Boolean {
        val d = adapter?.getRemoteDevice(dev.address ?: return false) ?: return false
        if (d.bondState == BluetoothDevice.BOND_BONDED) return true
        val done = CountDownLatch(1)
        var ok = false
        val rx = object : BroadcastReceiver() {
            override fun onReceive(c: Context, i: Intent) {
                val state = i.getIntExtra(BluetoothDevice.EXTRA_BOND_STATE, BluetoothDevice.ERROR)
                if (state == BluetoothDevice.BOND_BONDED) { ok = true; done.countDown() }
                if (state == BluetoothDevice.BOND_NONE) done.countDown()
            }
        }
        ctx.registerReceiver(rx, IntentFilter(BluetoothDevice.ACTION_BOND_STATE_CHANGED))
        try {
            adapter.cancelDiscovery()
            if (!d.createBond()) return d.bondState == BluetoothDevice.BOND_BONDED
            done.await(30, TimeUnit.SECONDS)
        } finally {
            ctx.unregisterReceiver(rx)
        }
        return ok
    }

    override fun connect(dev: Device): Boolean {
        val d = adapter?.getRemoteDevice(dev.address ?: return false) ?: return false
        fun isConnected() = a2dp?.connectedDevices?.any { it.address == d.address } == true
        repeat(16) { if (isConnected()) return true; Thread.sleep(500) } // Android usually connects a freshly paired headset itself
        try { // Not public API; allowed on some versions and devices, refused on others.
            BluetoothA2dp::class.java.getMethod("connect", BluetoothDevice::class.java).invoke(a2dp, d)
        } catch (_: Throwable) {
        }
        repeat(10) { if (isConnected()) return true; Thread.sleep(500) }
        return false
    }

    override fun disconnect(dev: Device): Boolean = try {
        val d = adapter!!.getRemoteDevice(dev.address)
        BluetoothA2dp::class.java.getMethod("disconnect", BluetoothDevice::class.java).invoke(a2dp, d) as? Boolean ?: false
    } catch (_: Throwable) { false }

    override fun audioOutput(): AudioOutput {
        val outs = audio.getDevices(AudioManager.GET_DEVICES_OUTPUTS)
        val bt = outs.firstOrNull { it.type == AudioDeviceInfo.TYPE_BLUETOOTH_A2DP || (Build.VERSION.SDK_INT >= 31 && it.type == AudioDeviceInfo.TYPE_BLE_HEADSET) }
        if (bt != null) return AudioOutput(bt.productName.toString(), if (Build.VERSION.SDK_INT >= 28) bt.address else null, "bluetooth")
        val wired = outs.firstOrNull { it.type == AudioDeviceInfo.TYPE_WIRED_HEADPHONES || it.type == AudioDeviceInfo.TYPE_USB_HEADSET }
        return AudioOutput(wired?.productName?.toString() ?: "Phone speaker", null, if (wired != null) "usb" else "builtin")
    }

    override fun setAudioOutput(dev: Device): Boolean = outputMatches(audioOutput(), dev)
}

/** Remembered device associations ("Daniel's headphones are the XM5s at 88:C9:…"). */
class PrefsRemembered(ctx: Context) : RememberedStore {
    private val p = ctx.getSharedPreferences("remembered", Context.MODE_PRIVATE)

    @Synchronized override fun remember(dev: Device, ownerPhone: String?, ownerName: String?) {
        val d = dev.copy(ownerPhone = ownerPhone, ownerName = ownerName, sources = mutableListOf("remembered"))
        p.edit().putString(dev.id, Json.encode(d.toMap() + ("last_used" to System.currentTimeMillis()))).apply()
    }

    @Synchronized override fun remembered(ownerPhone: String?): List<Device> = p.all.values.mapNotNull { v ->
        runCatching { Json.obj(v as String) }.getOrNull()
    }.sortedByDescending { (it["last_used"] as? Number)?.toLong() ?: 0L }.map { Device.from(it) }
        .filter { ownerPhone == null || it.ownerPhone == ownerPhone }

    @Synchronized override fun forget(deviceId: String) { p.edit().remove(deviceId).apply() }
}
