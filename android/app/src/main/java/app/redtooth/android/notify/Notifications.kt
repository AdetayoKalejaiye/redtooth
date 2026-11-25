package app.redtooth.android.notify

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.net.Uri
import app.redtooth.android.Graph
import app.redtooth.android.R
import app.redtooth.android.ui.MainActivity
import app.redtooth.core.IncomingOffer

/** Accept or decline straight from the notification; progress and "Saved" follow in the same slot. */
object Notifications {
    const val SERVICE_ID = 1
    private const val CH_OFFERS = "offers"
    private const val CH_SERVICE = "service"
    private const val CH_NUDGES = "nudges"

    private fun nm(ctx: Context) = ctx.getSystemService(NotificationManager::class.java)
    private fun idFor(offerId: String) = 1000 + (offerId.hashCode() and 0xffff)

    fun createChannels(ctx: Context) {
        nm(ctx).createNotificationChannels(listOf(
            NotificationChannel(CH_OFFERS, "Incoming files", NotificationManager.IMPORTANCE_HIGH).apply { description = "Someone wants to send you a file" },
            NotificationChannel(CH_NUDGES, "Connection requests", NotificationManager.IMPORTANCE_HIGH).apply { description = "Someone wants to connect to your headphones" },
            NotificationChannel(CH_SERVICE, "Ready to receive", NotificationManager.IMPORTANCE_MIN),
        ))
    }

    private fun openPanel(ctx: Context) = PendingIntent.getActivity(ctx, 0, Intent(ctx, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE)

    fun service(ctx: Context): Notification = Notification.Builder(ctx, CH_SERVICE)
        .setSmallIcon(R.drawable.ic_redtooth).setContentTitle("Redtooth is ready to receive").setContentIntent(openPanel(ctx)).setOngoing(true).build()

    private fun action(ctx: Context, offerId: String, accept: Boolean): Notification.Action {
        val i = Intent(ctx, OfferActionReceiver::class.java).putExtra("id", offerId).putExtra("accept", accept)
        val pi = PendingIntent.getBroadcast(ctx, (offerId + accept).hashCode(), i, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        return Notification.Action.Builder(null, if (accept) "Accept" else "Decline", pi).build()
    }

    fun showOffer(ctx: Context, o: IncomingOffer) {
        val n = Notification.Builder(ctx, CH_OFFERS).setSmallIcon(R.drawable.ic_redtooth)
            .setContentTitle("${o.senderName} wants to send you a file").setContentText("${o.name} · ${human(o.size)}")
            .setCategory(Notification.CATEGORY_MESSAGE).setContentIntent(openPanel(ctx))
            .addAction(action(ctx, o.id, false)).addAction(action(ctx, o.id, true)).setAutoCancel(false).build()
        nm(ctx).notify(idFor(o.id), n)
    }

    fun progress(ctx: Context, o: IncomingOffer) {
        val pct = if (o.size > 0) (100 * o.received / o.size).toInt() else 0
        nm(ctx).notify(idFor(o.id), Notification.Builder(ctx, CH_OFFERS).setSmallIcon(R.drawable.ic_redtooth).setOnlyAlertOnce(true)
            .setContentTitle("Receiving ${o.name}").setContentText("From ${o.senderName}").setProgress(100, pct, false).setOngoing(true).build())
    }

    fun saved(ctx: Context, o: IncomingOffer, uri: Uri?) {
        val view = uri?.let { PendingIntent.getActivity(ctx, idFor(o.id), Intent(Intent.ACTION_VIEW, it).addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION), PendingIntent.FLAG_IMMUTABLE) }
        nm(ctx).notify(idFor(o.id), Notification.Builder(ctx, CH_OFFERS).setSmallIcon(R.drawable.ic_redtooth).setOnlyAlertOnce(true)
            .setContentTitle("Saved ${o.name}").setContentText("From ${o.senderName} · in Downloads/Redtooth").setContentIntent(view ?: openPanel(ctx)).setAutoCancel(true).build())
    }

    fun cancelOffer(ctx: Context, offerId: String) = nm(ctx).cancel(idFor(offerId))

    fun nudge(ctx: Context, msg: Map<String, Any?>) {
        val text = msg["text"] as? String ?: "${msg["from"]} wants to connect to your ${msg["kind"] ?: "headphones"}."
        nm(ctx).notify(2, Notification.Builder(ctx, CH_NUDGES).setSmallIcon(R.drawable.ic_redtooth).setContentTitle(text)
            .setContentText("Hold the power button until the light flashes to enter pairing mode.").setContentIntent(openPanel(ctx)).setAutoCancel(true).build())
    }

    fun human(n: Long): String = when {
        n < 1024 -> "$n B"
        n < 1024 * 1024 -> "%.1f KB".format(n / 1024.0)
        n < 1024L * 1024 * 1024 -> "%.1f MB".format(n / 1048576.0)
        else -> "%.1f GB".format(n / 1073741824.0)
    }
}

class OfferActionReceiver : BroadcastReceiver() {
    override fun onReceive(ctx: Context, intent: Intent) {
        val id = intent.getStringExtra("id") ?: return
        Graph.broker.resolve(id, intent.getBooleanExtra("accept", false))
    }
}
