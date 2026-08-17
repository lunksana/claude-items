package com.mirage.android

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Intent
import android.net.VpnService
import android.os.IBinder
import android.os.ParcelFileDescriptor
import androidx.core.app.NotificationCompat
import com.mirage.android.core.ICoreCallback
import com.mirage.android.core.ICoreService
import com.mirage.android.core.LogStore
import com.mirage.android.core.MirageNative
import com.mirage.android.core.NodeStore
import com.mirage.android.core.RuleStore
import kotlinx.coroutines.*
import java.net.InetAddress
import java.util.concurrent.CopyOnWriteArrayList

/**
 * 独立内核进程 (:core) 的 CoreService。
 *
 * - 继承 VpnService: 在本进程创建 TUN
 * - 实现 ICoreService (AIDL): App 跨进程控制内核
 * - 内部通过 JNI 驱动 Rust 内核 (mirage-core)
 *
 * App (UI) 只做: bindService + 调 ICoreService 接口, 不直接碰 JNI。
 */
class CoreService : VpnService() {

    private var tunFd: ParcelFileDescriptor? = null
    private val scope = CoroutineScope(Dispatchers.Default + SupervisorJob())
    private val callbacks = CopyOnWriteArrayList<ICoreCallback>()

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        setActive(this)
        val action = intent?.action
        if (action == ACTION_STOP) {
            stopInternal()
            return START_NOT_STICKY
        }
        // startForegroundService 启动: 5 秒内必须 startForeground, 否则系统杀服务/崩溃
        startForegroundCompat()
        val uri = intent?.getStringExtra("uri")
        val poolSize = intent?.getIntExtra("pool_size", -1) ?: -1
        // 直接驱动启动 (建 TUN + 内核), 不依赖 UI 后续 AIDL 调用
        startInternal(uri, poolSize)
        return START_STICKY
    }

    // ── ICoreService 实现 ────────────────────────────────────────────────

    fun log(msg: String) {
        LogStore.append(msg)
        callbacks.forEach { runCatching { it.onLog(msg) } }
    }

    fun startInternal(uriOverride: String? = null, poolSizeOverride: Int = -1): Int {
        if (MirageNative.isRunning()) {
            notifyState()
            return 0
        }
        val uri = if (!uriOverride.isNullOrBlank()) uriOverride else NodeStore.getSelectedUri(this)
        if (uri.isEmpty()) {
            log("[core] 无选中节点")
            return -1
        }
        // 已授权检查 (包级授权, 与 UI 进程同包)
        if (VpnService.prepare(this) != null) {
            log("[core] VPN 未授权")
            return -2
        }

        val builder = Builder()
        builder.setSession("Mirage")
        builder.addAddress("198.18.0.1", 32)
        builder.addRoute("0.0.0.0", 0)
        builder.addRoute("198.18.0.0", 15)
        builder.addDnsServer(InetAddress.getByName("198.19.0.53"))
        builder.setMtu(1500)

        val fd = try { builder.establish() } catch (e: Exception) {
            log("[core] TUN establish 异常: ${e.message}")
            return -3
        } ?: run {
            log("[core] TUN establish 返回 null")
            return -4
        }
        tunFd = fd
        startForegroundCompat()

        // 注入规则
        runCatching { MirageNative.setRules(RuleStore.toJson(this)) }

        val poolSize = if (poolSizeOverride > 0) poolSizeOverride else NodeStore.getPoolSize(this)
        log("[core] 开始启动内核 (uri=${uri.take(30)}..., poolSize=$poolSize)")
        val rc = MirageNative.start(fd.fd, uri, poolSize)
        if (rc != 0) {
            log("[core] 内核启动失败 rc=$rc")
            runCatching { fd.close() }
            tunFd = null
            notifyState()
            return rc
        }
        log("[core] 内核已启动")
        notifyState()

        // 文件日志: 内核+App 日志落盘 (adb run-as cat files/core.log 诊断用)
        scope.launch {
            while (isActive) {
                runCatching {
                    val logs = (LogStore.all() + MirageNative.recentLogs().toList()).joinToString("\n")
                    java.io.File(filesDir, "core.log").writeText(logs.takeLast(30000))
                }
                delay(3000)
            }
        }
        // 通知栏流量
        scope.launch {
            var last = ""
            while (isActive) {
                runCatching {
                    val st = MirageNative.getStats()
                    if (st.size >= 7) {
                        val t = "↑ ${fmtRate(st[2])}  ↓ ${fmtRate(st[3])}  ·  ${fmtBytes(st[0])}/${fmtBytes(st[1])}"
                        if (t != last) { last = t; updateNotif(t) }
                    }
                }
                delay(2000)
            }
        }
        return 0
    }

    fun stopInternal() {
        log("[core] stop()")
        runCatching { MirageNative.stop() }
        runCatching { tunFd?.close() }
        tunFd = null
        runCatching { stopForeground(STOP_FOREGROUND_REMOVE) }
        runCatching { getSystemService(NotificationManager::class.java)?.cancel(1) }
        notifyState()
        stopSelf()
    }

    fun setNodeInternal(uri: String): Boolean {
        // 运行时热切换: 更新持久化 + 通知内核
        val idx = NodeStore.getNodes(this).indexOfFirst { it.uri == uri }
        if (idx >= 0) NodeStore.setSelected(this, idx)
        return if (MirageNative.isRunning()) {
            runCatching { MirageNative.setNode(uri) }.getOrDefault(false)
        } else true
    }

    fun setRulesInternal(json: String): Boolean = MirageNative.setRules(json)

    fun isRunningInternal(): Boolean = MirageNative.isRunning()
    fun isHealthyInternal(): Boolean = MirageNative.isHealthy()
    fun latencyMsInternal(): Long = MirageNative.latencyMs()
    fun getStatsInternal(): DoubleArray = MirageNative.getStats()
    fun recentLogsInternal(): Array<String> =
        (LogStore.all() + MirageNative.recentLogs().toList()).toTypedArray()
    fun getBuiltinDomainsInternal(): Array<String> = MirageNative.getBuiltinDomains()
    fun getBuiltinIpCountInternal(): Long = MirageNative.getBuiltinIpCount()
    fun testNodeInternal(uri: String, timeoutMs: Int): Long = MirageNative.testNode(uri, timeoutMs)

    fun registerCallbackInternal(cb: ICoreCallback?) {
        if (cb != null && !callbacks.contains(cb)) {
            callbacks.add(cb)
            // 注册后立即推一次当前运行状态
            runCatching { cb.onStateChanged(MirageNative.isRunning()) }
        }
    }

    fun unregisterCallbackInternal(cb: ICoreCallback?) { callbacks.remove(cb) }

    private fun notifyState() {
        val running = MirageNative.isRunning()
        callbacks.forEach { runCatching { it.onStateChanged(running) } }
    }

    // ── 前台通知 ─────────────────────────────────────────────────────────

    private fun startForegroundCompat() {
        val channel = NotificationChannel("mirage", "Mirage VPN", NotificationManager.IMPORTANCE_LOW)
            .also { getSystemService(NotificationManager::class.java)?.createNotificationChannel(it) }
        val pi = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE)
        val stopIntent = Intent(this, CoreService::class.java).setAction(ACTION_STOP)
        val stopPi = PendingIntent.getService(this, 1, stopIntent, PendingIntent.FLAG_IMMUTABLE)

        val notif: Notification = NotificationCompat.Builder(this, "mirage")
            .setContentTitle("Mirage 已连接")
            .setContentText("流量经 Mirage 隧道转发")
            .setSmallIcon(android.R.drawable.ic_lock_lock)
            .setContentIntent(pi)
            .addAction(android.R.drawable.ic_menu_close_clear_cancel, "断开", stopPi)
            .setOngoing(true)
            .build()
        startForeground(1, notif)
    }

    private fun updateNotif(text: String) {
        val nm = getSystemService(NotificationManager::class.java) ?: return
        val pi = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE)
        val stopIntent = Intent(this, CoreService::class.java).setAction(ACTION_STOP)
        val stopPi = PendingIntent.getService(this, 1, stopIntent, PendingIntent.FLAG_IMMUTABLE)

        val notif = NotificationCompat.Builder(this, "mirage")
            .setContentTitle("Mirage 已连接")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.ic_lock_lock)
            .setContentIntent(pi)
            .addAction(android.R.drawable.ic_menu_close_clear_cancel, "断开", stopPi)
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

    override fun onDestroy() {
        clearActive()
        log("[core] onDestroy()")
        runCatching { MirageNative.stop() }
        runCatching { tunFd?.close() }
        tunFd = null
        scope.cancel()
        runCatching { stopForeground(STOP_FOREGROUND_REMOVE) }
        runCatching { getSystemService(NotificationManager::class.java)?.cancel(1) }
        super.onDestroy()
    }

    /** AIDL binder: App 跨进程控制的入口。 */
    private val binder = object : ICoreService.Stub() {
        override fun start(): Int = startInternal()
        override fun stop() = stopInternal()
        override fun setNode(uri: String): Boolean = setNodeInternal(uri)
        override fun setRules(json: String): Boolean = setRulesInternal(json)
        override fun isRunning(): Boolean = isRunningInternal()
        override fun isHealthy(): Boolean = isHealthyInternal()
        override fun latencyMs(): Long = latencyMsInternal()
        override fun getStats(): DoubleArray = getStatsInternal()
        override fun recentLogs(): Array<String> = recentLogsInternal()
        override fun getBuiltinDomains(): Array<String> = getBuiltinDomainsInternal()
        override fun getBuiltinIpCount(): Long = getBuiltinIpCountInternal()
        override fun testNode(uri: String, timeoutMs: Int): Long = testNodeInternal(uri, timeoutMs)
        override fun registerCallback(cb: ICoreCallback?) = registerCallbackInternal(cb)
        override fun unregisterCallback(cb: ICoreCallback?) = unregisterCallbackInternal(cb)
    }

    override fun onBind(intent: Intent?): IBinder? = binder

    companion object {
        const val ACTION_STOP = "com.mirage.android.STOP"

        /** 当前活跃实例 (Rust protect 回调用: VpnService.protect 防隧道环路)。 */
        @Volatile
        private var active: CoreService? = null

        @JvmStatic
        fun protectFd(fd: Int) {
            val inst = active
            if (inst == null) {
                LogStore.append("[core] protect 失败: active 未设置!")
                return
            }
            val ok = runCatching { inst.protect(fd) }.isSuccess
            LogStore.append("[core] protect fd=$fd ok=$ok")
        }

        @JvmStatic
        fun setActive(s: CoreService) { active = s }

        @JvmStatic
        fun clearActive() { active = null }
    }
}
