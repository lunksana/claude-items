package com.mirage.android.service

import android.content.Intent
import android.graphics.drawable.Icon
import android.net.ConnectivityManager
import android.net.NetworkCapabilities
import android.os.Build
import android.service.quicksettings.Tile
import android.service.quicksettings.TileService
import androidx.annotation.RequiresApi
import com.mirage.android.CoreService
import com.mirage.android.MainActivity
import com.mirage.android.R
import com.mirage.android.core.NodeStore

/**
 * 任务栏 / 下拉控制中心快捷设置磁贴 (Quick Settings Tile)。
 * 允许用户直接在系统下拉控制中心磁贴中一键开关 Mirage VPN。
 */
@RequiresApi(Build.VERSION_CODES.N)
class MirageTileService : TileService() {

    override fun onStartListening() {
        super.onStartListening()
        updateTileState(isVpnRunning())
    }

    override fun onClick() {
        super.onClick()
        val running = isVpnRunning()
        if (running) {
            // 断开连接
            val stopIntent = Intent(this, CoreService::class.java).setAction(CoreService.ACTION_STOP)
            runCatching { startService(stopIntent) }
            val stopBroadcast = Intent(CoreService.ACTION_STOP).setPackage(packageName)
            runCatching { sendBroadcast(stopBroadcast) }
            updateTileState(false)
        } else {
            // 检查是否有节点
            val selected = NodeStore.getSelectedUri(this)
            if (selected.isBlank()) {
                val appIntent = Intent(this, MainActivity::class.java).apply {
                    flags = Intent.FLAG_ACTIVITY_NEW_TASK
                }
                startActivityAndCollapse(appIntent)
                return
            }
            val startIntent = Intent(this, CoreService::class.java).apply {
                putExtra("uri", selected)
                putExtra("pool_size", NodeStore.getPoolSize(this@MirageTileService))
            }
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                startForegroundService(startIntent)
            } else {
                startService(startIntent)
            }
            updateTileState(true)
        }
    }

    private fun isVpnRunning(): Boolean {
        val cm = getSystemService(ConnectivityManager::class.java) ?: return false
        return cm.allNetworks.any { net ->
            val caps = cm.getNetworkCapabilities(net)
            caps != null && caps.hasTransport(NetworkCapabilities.TRANSPORT_VPN)
        }
    }

    private fun updateTileState(active: Boolean) {
        val tile = qsTile ?: return
        if (active) {
            tile.state = Tile.STATE_ACTIVE
            tile.label = "Mirage (已连接)"
            tile.icon = Icon.createWithResource(this, R.drawable.ic_notification_mirage)
        } else {
            tile.state = Tile.STATE_INACTIVE
            tile.label = "Mirage (未连接)"
            tile.icon = Icon.createWithResource(this, R.drawable.ic_notification_mirage)
        }
        tile.updateTile()
    }
}
