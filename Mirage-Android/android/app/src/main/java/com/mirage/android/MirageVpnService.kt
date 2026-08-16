package com.mirage.android

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Intent
import android.content.pm.PackageManager
import android.net.VpnService
import android.os.Build
import android.os.ParcelFileDescriptor
import androidx.core.app.NotificationCompat
import com.mirage.android.core.LogStore
import com.mirage.android.core.MirageNative
import com.mirage.android.core.NodeStore
import kotlinx.coroutines.*
import java.net.InetAddress

/**
 * VPN 服务: 建 TUN → 交 mirage-core → 定时 protect 隧道 socket。
 */
class MirageVpnService : VpnService() {

    private var tunFd: ParcelFileDescriptor? = null
    private var protectJob: Job? = null
    private var logJob: Job? = null
    private var notifJob: Job? = null
    private var lastNotifText: String? = null
    private val scope = CoroutineScope(Dispatchers.Default + SupervisorJob())

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        companion_active_set(this)
        intent?.action?.let { action ->
            when (action) {
                ACTION_STOP -> { stop(); return START_NOT_STICKY }
            }
        }
        startVpn()
        return START_STICKY
    }

    private fun startVpn() {
        val uri = NodeStore.getSelectedUri(this)
        if (uri.isEmpty()) {
            LogStore.append("[服务] 未选择节点, 请先在 App 里添加并选择 mirage:// 节点")
            stop()
            return
        }

        if (VpnService.prepare(this) != null) {
            LogStore.append("[服务] VPN 授权被撤销, 请重新授权")
            stop()
            return
        }

        val builder = Builder()
        builder.setSession("Mirage")
        // 与 mirage-core 的 TUN 配置严格一致 (见 engine.rs / tun/mod.rs)
        builder.addAddress("198.18.0.1", 32)          // TUN 接口地址
        builder.addRoute("0.0.0.0", 0)                // 全部流量走隧道
        builder.addRoute("198.18.0.0", 15)            // fake-IP 段 (冗余, 已被 0.0.0.0/0 覆盖)
        builder.addDnsServer(InetAddress.getByName("198.19.0.53")) // fake-IP DNS
        builder.setMtu(1500)

        val fd = try {
            builder.establish()
        } catch (e: Exception) {
            LogStore.append("[服务] 建立 TUN 失败: ${e.message}")
            stop()
            return
        } ?: run {
            LogStore.append("[服务] establish() 返回 null (未授权?)")
            stop()
            return
        }
        tunFd = fd
        startForegroundCompat()

        val rc = MirageNative.start(fd.fd, uri, NodeStore.getPoolSize(this))
        if (rc != 0) {
            LogStore.append("[服务] 引擎启动失败 (rc=$rc)")
            stop()
            return
        }
        LogStore.append("[服务] 已连接: ${MirageNative.version()}")

        // 定时 protect 隧道 socket (mirage-core 的隧道 socket 必须 protect, 否则环路)
        protectJob = scope.launch {
            while (isActive) {
                try {
                    val fds = MirageNative.drainProtectFds()
                    for (f in fds) {
                        runCatching { protect(f) }
                    }
                } catch (_: Throwable) {}
                delay(200)
            }
        }
        // 通知栏实时流量 (任务栏显示速率, 每 2s 更新)
        notifJob = scope.launch {
            while (isActive) {
                runCatching {
                    val stats = MirageNative.getStats()
                    if (stats.size >= 7) {
                        val text = "↑ ${fmtRate(stats[2])}  ↓ ${fmtRate(stats[3])}  ·  ${fmtBytes(stats[0])}/${fmtBytes(stats[1])}"
                        if (text != lastNotifText) {
                            lastNotifText = text
                            updateNotif(text)
                        }
                    }
                }
                delay(2000)
            }
        }
        // 定时收集状态到日志
        logJob = scope.launch {
            while (isActive) {
                try {
                    if (MirageNative.isRunning()) {
                        val lat = MirageNative.latencyMs()
                        if (lat >= 0) {
                            LogStore.append("[状态] 运行中, RTT=${lat}ms, healthy=${MirageNative.isHealthy()}")
                        }
                    }
                } catch (_: Throwable) {}
                delay(10_000)
            }
        }
    }

    private fun startForegroundCompat() {
        val channel = NotificationChannel(
            "mirage", "Mirage VPN", NotificationManager.IMPORTANCE_LOW
        ).also {
            getSystemService(NotificationManager::class.java)?.createNotificationChannel(it)
        }
        val pi = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE
        )
        val notif: Notification = NotificationCompat.Builder(this, "mirage")
            .setContentTitle("Mirage 已连接")
            .setContentText("流量经 Mirage 隧道转发")
            .setSmallIcon(android.R.drawable.ic_lock_lock)
            .setContentIntent(pi)
            .setOngoing(true)
            .build()
        startForeground(1, notif)
    }

    /** 更新通知内容 (任务栏实时流量)。 */
    private fun updateNotif(text: String) {
        val nm = getSystemService(NotificationManager::class.java) ?: return
        val pi = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE)
        val notif = NotificationCompat.Builder(this, "mirage")
            .setContentTitle("Mirage 已连接")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.ic_lock_lock)
            .setContentIntent(pi)
            .setOngoing(true)
            .build()
        nm.notify(1, notif)
    }

    private fun fmtRate(bps: Double): String {
        val b = bps.coerceAtLeast(0.0)
        return when {
            b >= 1 shl 20 -> "%.2fMB/s".format(b / (1 shl 20))
            b >= 1 shl 10 -> "%.1fKB/s".format(b / (1 shl 10))
            else -> "%.0fB/s".format(b)
        }
    }

    private fun fmtBytes(b: Double): String = when {
        b >= 1 shl 30 -> "%.1fG".format(b / (1 shl 30))
        b >= 1 shl 20 -> "%.1fM".format(b / (1 shl 20))
        b >= 1 shl 10 -> "%.1fK".format(b / (1 shl 10))
        else -> "%.0fB".format(b)
    }

    private fun stop() {
        LogStore.append("[服务] stop() 被调用")
        runCatching { MirageNative.stop() }
        protectJob?.cancel()
        logJob?.cancel()
        notifJob?.cancel()
        val fd = tunFd
        runCatching { fd?.close() }
        tunFd = null
        LogStore.append(if (fd != null) "[服务] TUN fd 已关闭" else "[服务] TUN fd 已为空 (无 fd 可关)")
        LogStore.append("[服务] 已断开")
        stopForeground(STOP_FOREGROUND_REMOVE)
        stopSelf()
    }

    private fun companion_active_set(inst: MirageVpnService) {
        // 简单方式: 直接给 companion 赋值 (通过公开静态字段函数)
        MirageVpnService.setActive(inst)
    }

    override fun onDestroy() {
        LogStore.append("[服务] onDestroy() 被调用")
        MirageVpnService.clearActive()
        runCatching { MirageNative.stop() }
        protectJob?.cancel()
        logJob?.cancel()
        notifJob?.cancel()
        scope.cancel()
        runCatching { tunFd?.close() }
        tunFd = null
        // ⚠️ 必须撤前台通知: 之前发现 VPN 已断但通知残留 (服务被不同方式结束时
        //    stop() 里的 stopForeground 可能没执行, 通知一直挂通知栏误导用户)
        runCatching { stopForeground(STOP_FOREGROUND_REMOVE) }
        // 兜底: 直接移除通知 (防止任何路径残留)
        runCatching { getSystemService(NotificationManager::class.java)?.cancel(1) }
        super.onDestroy()
    }

    companion object {
        const val ACTION_STOP = "com.mirage.android.STOP"

        /** 当前活跃的服务实例 (protect 回调用)。 */
        @Volatile
        private var active: MirageVpnService? = null

        /** Rust 隧道 socket 建立时同步调用: VpnService.protect(fd) 防环路。 */
        @JvmStatic
        fun protectFd(fd: Int) {
            active?.protect(fd) ?: runCatching { }
        }

        @JvmStatic
        fun setActive(inst: MirageVpnService) { active = inst }

        @JvmStatic
        fun clearActive() { active = null }

        /** 检查 VPN 授权状态 (用于 UI 显示)。 */
        fun isPrepared(ctx: android.content.Context): Boolean {
            return VpnService.prepare(ctx) == null
        }
    }
}
