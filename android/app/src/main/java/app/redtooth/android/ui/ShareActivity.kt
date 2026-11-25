package app.redtooth.android.ui

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.service.quicksettings.Tile
import android.service.quicksettings.TileService
import android.app.PendingIntent
import app.redtooth.android.Graph

/** Share sheet target: hands the shared file to the panel, already selected. */
class ShareActivity : Activity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val uri: Uri? = if (Build.VERSION.SDK_INT >= 33) intent.getParcelableExtra(Intent.EXTRA_STREAM, Uri::class.java)
        else @Suppress("DEPRECATION") intent.getParcelableExtra(Intent.EXTRA_STREAM)
        if (uri != null) {
            // Keep read access after this activity finishes: the panel streams it when sending.
            runCatching { contentResolver.takePersistableUriPermission(uri, Intent.FLAG_GRANT_READ_URI_PERMISSION) }
            startActivity(Intent(this, MainActivity::class.java).putExtra(MainActivity.EXTRA_SELECT, uri.toString())
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_GRANT_READ_URI_PERMISSION).setData(uri))
        }
        finish()
    }
}

/** Quick Settings tile: shows the current audio output; tap opens the panel. */
class RedtoothTileService : TileService() {
    override fun onStartListening() {
        val tile = qsTile ?: return
        val out = runCatching { Graph.bluetooth.audioOutput() }.getOrNull()
        tile.label = "Redtooth"
        if (Build.VERSION.SDK_INT >= 29) tile.subtitle = out?.name ?: "Audio output"
        tile.state = if (out?.transport == "bluetooth") Tile.STATE_ACTIVE else Tile.STATE_INACTIVE
        tile.updateTile()
    }

    override fun onClick() {
        val i = Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        if (Build.VERSION.SDK_INT >= 34) startActivityAndCollapse(PendingIntent.getActivity(this, 0, i, PendingIntent.FLAG_IMMUTABLE))
        else @Suppress("DEPRECATION") startActivityAndCollapse(i)
    }
}
