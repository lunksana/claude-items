package com.mirage.android

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.content.pm.ServiceInfo
import android.net.VpnService
import android.os.Build
import android.os.IBinder
import android.os.ParcelFileDescriptor
import androidx.core.app.NotificationCompat
import com.mirage.android.core.GeoManager
import com.mirage.android.core.ICoreCallback
import com.mirage.android.core.ICoreService
import com.mirage.android.core.LogStore
import com.mirage.android.core.MirageNative
import com.mirage.android.core.NativeLoader
import com.mirage.android.core.NodeStore
import com.mirage.android.core.RuleStore
import com.mirage.android.core.SettingsStore
import com.mirage.android.core.TrafficStatsStore
import kotlinx.coroutines.*
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withPermit
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

    private var logJob: Job? = null
    private var notifJob: Job? = null
    private var trafficJob: Job? = null
    private var watchdogJob: Job? = null
    private var failoverRestartJob: Job? = null

    private fun cancelAllJobs() {
        logJob?.cancel(); logJob = null
        notifJob?.cancel(); notifJob = null
        trafficJob?.cancel(); trafficJob = null
        watchdogJob?.cancel(); watchdogJob = null
        failoverRestartJob?.cancel(); failoverRestartJob = null
    }

    override fun onCreate() {
        super.onCreate()
        setActive(this)
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        setActive(this)
        val action = intent?.action
        if (action == ACTION_STOP) {
            stopInternal()
            return START_NOT_STICKY
        }
        // startForegroundService 启动: 5 秒内必须 startForeground, 否则系统杀服务/崩溃
        startForegroundCompat()
        // 加载选中的内核 (自定义或内置)
        NativeLoader.load(this)
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

        // 注入规则、Geo 文件与 DNS 配置 (修复 M5: 自主完成全量注入，杜绝冷启动 AIDL 竞态)
        runCatching { GeoManager.loadGeoFilesToNative(this) }
        runCatching { MirageNative.setRules(RuleStore.toJson(this)) }
        runCatching {
            val dnsPrefs = getSharedPreferences("mirage_dns_prefs", Context.MODE_PRIVATE)
            val directDns = dnsPrefs.getString("direct_dns", "223.5.5.5") ?: "223.5.5.5"
            val remoteDns = dnsPrefs.getString("remote_dns", "1.1.1.1") ?: "1.1.1.1"
            MirageNative.setDnsServers(directDns, remoteDns)
        }

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
        runCatching { sendBroadcast(Intent(ACTION_VPN_STARTED).setPackage(packageName)) }

        // 文件日志: 内核+App 日志落盘 (adb run-as cat files/core.log 诊断用)
        logJob = scope.launch {
            while (isActive) {
                runCatching {
                    val logs = (LogStore.all() + MirageNative.recentLogs().toList()).joinToString("\n")
                    java.io.File(filesDir, "core.log").writeText(logs.takeLast(30000))
                }
                delay(3000)
            }
        }
        // 通知栏流量
        notifJob = scope.launch {
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
        // 流量统计持久化 (增量累加到今日/本月)
        TrafficStatsStore.prune(this@CoreService) // 启动时清理一次 30 天前旧数据
        trafficJob = scope.launch {
            var lastUp = -1L; var lastDown = -1L
            while (isActive) {
                runCatching {
                    val st = MirageNative.getStats()
                    if (st.size >= 2) {
                        val up = st[0].toLong(); val down = st[1].toLong()
                        if (lastUp >= 0 && lastDown >= 0 && up >= lastUp && down >= lastDown) {
                            TrafficStatsStore.add(this@CoreService, up - lastUp, down - lastDown)
                        }
                        lastUp = up; lastDown = down
                    }
                }
                delay(10000)
            }
        }
        // 断线自动重连 / 节点 failover watchdog
        watchdogJob = startFailoverWatchdog()
        return 0
    }

    /** 断线检测 + 自动重连 + failover watchdog。 */
    private fun startFailoverWatchdog(): Job = scope.launch {
        var consecutiveFailures = 0
        while (isActive) {
            val interval = SettingsStore.getCheckIntervalSec(this@CoreService).toLong().coerceAtLeast(5)
            delay(interval * 1000)
            if (!MirageNative.isRunning()) continue
            // 修复 M1: Fail-Closed (异常/JNI失败时视为不健康，防止假死与自愈失效)
            val healthy = runCatching { MirageNative.isHealthy() }.getOrDefault(false)
            if (healthy) { consecutiveFailures = 0; continue }

            consecutiveFailures++
            LogStore.append("[failover] 检测到连接异常 (第 $consecutiveFailures 次)")
            if (!SettingsStore.isAutoReconnect(this@CoreService)) continue

            // 连续 2 次异常才触发 failover (避免瞬时抖动)
            if (consecutiveFailures >= 2) {
                doFailover()
                consecutiveFailures = 0
            }
        }
    }

    /** failover: 测活选最优节点 (best) 或换下一个 (next), 然后热切换。 */
    private suspend fun doFailover() {
        val nodes = NodeStore.getNodes(this)
        if (nodes.size <= 1) {
            // 单节点: 完整重启连接 (撤 TUN 后重建, 清 stale 隧道)
            LogStore.append("[failover] 仅一个节点, 完整重启连接")
            runCatching { MirageNative.stop() }
            runCatching { tunFd?.close() }; tunFd = null
            // 修复 S2: 纳入 failoverRestartJob 统一管理, 支持手动停止时立即 cancel
            failoverRestartJob?.cancel()
            failoverRestartJob = scope.launch {
                delay(3000)
                if (isActive && !MirageNative.isRunning()) startInternal()
            }
            return
        }
        val mode = SettingsStore.getFailoverMode(this)
        LogStore.append("[failover] 触发节点切换 (mode=$mode, ${nodes.size} 个节点)")
        val selectedUri = NodeStore.getSelectedUri(this)
        val sorted = if (mode == "best") {
            // 修复 M3: 并发并行测活 (各节点独立 3000ms 超时, 避免 N*5s 阻塞 watchdog 导致监控停摆)。
            // 每个 testNode 是完整协议握手 (引擎+拨号), 用信号量限流防 N 个并发握手同时打服务器
            // (thundering herd) —— 与内核 pool 的 on-demand 信号量同理。
            val sem = Semaphore(4)
            withContext(Dispatchers.IO) {
                nodes.map { n ->
                    async {
                        sem.withPermit {
                            val rtt = runCatching { MirageNative.testNode(n.uri, 3000) }.getOrDefault(-1L)
                            n to rtt
                        }
                    }
                }.awaitAll()
            }.filter { it.second >= 0 }.sortedBy { it.second }
        } else {
            // 顺序: 选当前之后的下一个
            val idx = nodes.indexOfFirst { it.uri == selectedUri }
            listOfNotNull(nodes.getOrNull(idx + 1) ?: nodes.firstOrNull()).map { it to 0L }
        }
        val best = sorted.firstOrNull() ?: return
        if (best.first.uri != selectedUri) {
            LogStore.append("[failover] 切换到: ${best.first.displayName} (${best.second}ms)")
            runCatching { MirageNative.setNode(best.first.uri) }
            val newIdx = nodes.indexOfFirst { it.uri == best.first.uri }
            if (newIdx >= 0) {
                NodeStore.setSelected(this, newIdx)
                callbacks.forEach { runCatching { it.onNodeChanged(newIdx, best.first.uri) } }
            }
        } else {
            // 最优还是当前 → 完整重启连接 (撤 TUN 后重建, 清 stale 隧道)
            LogStore.append("[failover] 当前节点仍最优, 完整重启连接")
            runCatching { MirageNative.stop() }
            runCatching { tunFd?.close() }; tunFd = null
            // 修复 S2: 纳入 failoverRestartJob 统一管理
            failoverRestartJob?.cancel()
            failoverRestartJob = scope.launch {
                delay(3000)
                if (isActive && !MirageNative.isRunning()) startInternal()
            }
        }
    }

    fun stopInternal() {
        log("[core] stop()")
        cancelAllJobs()
        runCatching { MirageNative.stop() }
        runCatching { tunFd?.close() }
        tunFd = null
        runCatching {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
                stopForeground(STOP_FOREGROUND_REMOVE)
            } else {
                @Suppress("DEPRECATION")
                stopForeground(true)
            }
        }
        runCatching { getSystemService(NotificationManager::class.java)?.cancel(1) }
        notifyState()
        runCatching { sendBroadcast(Intent(ACTION_VPN_STOPPED).setPackage(packageName)) }
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

    fun setPoolSizeInternal(poolSize: Int): Boolean {
        NodeStore.setPoolSize(this, poolSize)
        return if (MirageNative.isRunning()) {
            runCatching { MirageNative.setPoolSize(poolSize) }.getOrDefault(false)
        } else true
    }

    fun getPoolSizeInternal(): Int =
        if (MirageNative.isRunning()) MirageNative.getPoolSize() else NodeStore.getPoolSize(this)

    fun setRulesInternal(json: String): Boolean = MirageNative.setRules(json)
    fun setBlockQuicInternal(block: Boolean): Boolean = MirageNative.setBlockQuic(block)
    fun isBlockQuicInternal(): Boolean = MirageNative.isBlockQuic()
    fun clearDnsCacheInternal(): Boolean = MirageNative.clearDnsCache()
    fun setDnsServersInternal(directDns: String, remoteDns: String): Boolean =
        MirageNative.setDnsServers(directDns, remoteDns)
    fun getDirectDnsInternal(): String = MirageNative.getDirectDns()
    fun getRemoteDnsInternal(): String = MirageNative.getRemoteDns()

    fun isRunningInternal(): Boolean = MirageNative.isRunning()
    fun isHealthyInternal(): Boolean = MirageNative.isHealthy()
    fun latencyMsInternal(): Long = MirageNative.latencyMs()
    fun getStatsInternal(): DoubleArray = MirageNative.getStats()
    fun getConnectionsJsonInternal(): String = MirageNative.getConnectionsJson()
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
        val channel = NotificationChannel("mirage_status", "Mirage VPN 运行状态", NotificationManager.IMPORTANCE_DEFAULT).apply {
            description = "Mirage VPN 运行状态、流量监控与快捷断开"
            setShowBadge(false)
            setSound(null, null)
            enableVibration(false)
            enableLights(false)
        }
        getSystemService(NotificationManager::class.java)?.createNotificationChannel(channel)

        val pi = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val stopIntent = Intent(this, com.mirage.android.receiver.CoreActionReceiver::class.java).setAction(ACTION_STOP)
        val stopPi = PendingIntent.getBroadcast(
            this,
            1001,
            stopIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val notif: Notification = NotificationCompat.Builder(this, "mirage_status")
            .setContentTitle("Mirage 已连接")
            .setContentText("流量经 Mirage 隧道转发")
            .setSmallIcon(R.drawable.ic_notification_mirage)
            .setColor(0xFF2481CC.toInt())
            .setContentIntent(pi)
            .addAction(R.drawable.ic_notification_mirage, "断开连接", stopPi)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
            .build()

        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            val fgsType = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.UPSIDE_DOWN_CAKE) {
                ServiceInfo.FOREGROUND_SERVICE_TYPE_SPECIAL_USE
            } else {
                ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE
            }
            try {
                startForeground(1, notif, fgsType)
            } catch (e: Throwable) {
                try {
                    startForeground(1, notif, ServiceInfo.FOREGROUND_SERVICE_TYPE_CONNECTED_DEVICE)
                } catch (e2: Throwable) {
                    startForeground(1, notif)
                }
            }
        } else {
            startForeground(1, notif)
        }
    }

    private fun updateNotif(text: String) {
        val nm = getSystemService(NotificationManager::class.java) ?: return
        val pi = PendingIntent.getActivity(this, 0, Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE)
        val stopIntent = Intent(this, com.mirage.android.receiver.CoreActionReceiver::class.java).setAction(ACTION_STOP)
        val stopPi = PendingIntent.getBroadcast(
            this,
            1001,
            stopIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )

        val notif = NotificationCompat.Builder(this, "mirage_status")
            .setContentTitle("Mirage 已连接")
            .setContentText(text)
            .setSmallIcon(R.drawable.ic_notification_mirage)
            .setColor(0xFF2481CC.toInt())
            .setContentIntent(pi)
            .addAction(R.drawable.ic_notification_mirage, "断开连接", stopPi)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setPriority(NotificationCompat.PRIORITY_HIGH)
            .setVisibility(NotificationCompat.VISIBILITY_PUBLIC)
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

    fun setLogLevelInternal(level: String): Boolean {
        log("[core] 切换内核日志级别: $level")
        return MirageNative.setLogLevel(level)
    }

    fun setUdpMuxInternal(enabled: Boolean): Boolean {
        log("[core] 切换 UDP Mux: $enabled")
        return MirageNative.setUdpMux(enabled)
    }

    fun isUdpMuxInternal(): Boolean = MirageNative.isUdpMux()

    override fun onRevoke() {
        log("[core] 系统任务栏/设置断开 VPN 连接 (onRevoke)")
        stopInternal()
        super.onRevoke()
    }

    override fun onDestroy() {
        clearActive()
        log("[core] onDestroy()")
        cancelAllJobs()
        runCatching { MirageNative.stop() }
        runCatching { tunFd?.close() }
        tunFd = null
        scope.cancel()
        runCatching {
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.N) {
                stopForeground(STOP_FOREGROUND_REMOVE)
            } else {
                @Suppress("DEPRECATION")
                stopForeground(true)
            }
        }
        runCatching { getSystemService(NotificationManager::class.java)?.cancel(1) }
        super.onDestroy()
    }

    /** AIDL binder: App 跨进程控制的入口。 */
    private val binder = object : ICoreService.Stub() {
        override fun start(): Int = startInternal()
        override fun stop() = stopInternal()
        override fun setNode(uri: String): Boolean = setNodeInternal(uri)
        override fun setPoolSize(poolSize: Int): Boolean = setPoolSizeInternal(poolSize)
        override fun getPoolSize(): Int = getPoolSizeInternal()
        override fun setRules(json: String): Boolean = setRulesInternal(json)
        override fun getRuleHits(): String = MirageNative.getRuleHits()
        override fun resetRuleHits(): Boolean = MirageNative.resetRuleHits()
        override fun setLogLevel(level: String?): Boolean =
            level?.let { setLogLevelInternal(it) } ?: false
        override fun setBlockQuic(block: Boolean): Boolean = setBlockQuicInternal(block)
        override fun isBlockQuic(): Boolean = isBlockQuicInternal()
        override fun setUdpMux(enabled: Boolean): Boolean = setUdpMuxInternal(enabled)
        override fun isUdpMux(): Boolean = isUdpMuxInternal()
        override fun clearDnsCache(): Boolean = clearDnsCacheInternal()
        override fun setDnsServers(directDns: String?, remoteDns: String?): Boolean =
            setDnsServersInternal(directDns ?: "223.5.5.5", remoteDns ?: "1.1.1.1")
        override fun getDirectDns(): String = getDirectDnsInternal()
        override fun getRemoteDns(): String = getRemoteDnsInternal()
        override fun isRunning(): Boolean = isRunningInternal()
        override fun isHealthy(): Boolean = isHealthyInternal()
        override fun latencyMs(): Long = latencyMsInternal()
        override fun getStats(): DoubleArray = getStatsInternal()
        override fun getConnectionsJson(): String = getConnectionsJsonInternal()
        override fun recentLogs(): Array<String> = recentLogsInternal()
        override fun getBuiltinDomains(): Array<String> = getBuiltinDomainsInternal()
        override fun getBuiltinIpCount(): Long = getBuiltinIpCountInternal()
        override fun testNode(uri: String, timeoutMs: Int): Long = testNodeInternal(uri, timeoutMs)
        override fun loadGeoFiles(geositePath: String?, geoipPath: String?): String =
            GeoManager.loadGeoFilesToNative(this@CoreService, geositePath, geoipPath)
        override fun getGeoTags(): String =
            runCatching { MirageNative.getGeoTags() }.getOrDefault("{}")
        override fun registerCallback(cb: ICoreCallback?) = registerCallbackInternal(cb)
        override fun unregisterCallback(cb: ICoreCallback?) = unregisterCallbackInternal(cb)
    }

    override fun onBind(intent: Intent?): IBinder? = binder

    companion object {
        const val ACTION_STOP = "com.mirage.android.STOP"
        const val ACTION_VPN_STOPPED = "com.mirage.android.VPN_STOPPED"
        const val ACTION_VPN_STARTED = "com.mirage.android.VPN_STARTED"

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
            // ① 传统 protect (SO_MARK 路由)
            val ok = runCatching { inst.protect(fd) }.isSuccess
            // ② Android 14+/16 增强: protect(fd) 可能不足以让 socket 绕过 VPN。
            //    显式把 socket 绑定到**非 VPN 的底层网络** —— 注意: 不能用
            //    cm.activeNetwork (VPN 激活时它可能返回 VPN 网络, 绑上去反而走 TUN
            //    环路! 实机 DEBUG 日志证实隧道 SYN 进了 TUN)。遍历找第一个
            //    TRANSPORT_VPN=false 且 INTERNET 的真实网络绑定。
            var netOk = false
            runCatching {
                val cm = inst.getSystemService(android.net.ConnectivityManager::class.java)
                val realNet = cm.allNetworks.firstOrNull { net ->
                    val caps = cm.getNetworkCapabilities(net)
                    caps != null
                        && !caps.hasTransport(android.net.NetworkCapabilities.TRANSPORT_VPN)
                        && caps.hasCapability(android.net.NetworkCapabilities.NET_CAPABILITY_INTERNET)
                }
                if (realNet != null) {
                    // 修复 S3: 使用 ParcelFileDescriptor 必须在 close 前调用 detachFd() 剥离底层描述符所有权，
                    // 严禁关闭底层 socket，否则 Rust 随后 connect() 时会因 EBADF 失败！
                    val pfd = android.os.ParcelFileDescriptor.fromFd(fd)
                    try {
                        realNet.bindSocket(pfd.fileDescriptor)
                        netOk = true
                    } finally {
                        pfd.detachFd()
                        pfd.close()
                    }
                } else {
                    LogStore.append("[core] 未找到非 VPN 底层网络")
                }
            }
            // 修复 M6: 降低日志刷屏，仅在绑定或保护失败时记录关键告警
            if (!ok || !netOk) {
                LogStore.append("[core] protect fd=$fd 告警 (protectOk=$ok, bindNetOk=$netOk)")
            }
        }

        @JvmStatic
        fun setActive(s: CoreService) { active = s }

        @JvmStatic
        fun getActive(): CoreService? = active

        @JvmStatic
        fun clearActive() { active = null }
    }
}
