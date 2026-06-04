package com.pyrealiy.proxy.service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.Intent
import android.net.ConnectivityManager
import android.net.VpnService
import android.os.Build
import android.os.ParcelFileDescriptor
import com.pyrealiy.proxy.core.Logger
import com.pyrealiy.proxy.core.SessionLogStore
import com.pyrealiy.proxy.R
import com.pyrealiy.proxy.data.AppPrefs
import com.pyrealiy.proxy.data.Profile
import com.pyrealiy.proxy.ui.MainActivity
import kotlinx.coroutines.*
import java.io.FileInputStream
import java.io.FileOutputStream
import java.util.concurrent.ConcurrentHashMap

/**
 * PyReality VPN 服务
 *
 * 工作流程：
 *  1. VpnService.Builder 创建 TUN 接口（10.10.0.1/32），路由 0.0.0.0/0
 *  2. 将 TUN 的文件描述符 (FD) 移交给底层 Rust smoltcp 模块。
 *  3. 底层解析出 TCP 流后，通过回调抛出 Unix Domain Socket 的 FD。
 *  4. Kotlin 将拿到的 FD 包装为流，对接到 ProxyTunnel 进行加密转发。
 *  5. 其他 UDP 丢弃（可扩展）
 *
 * 为何需要 VpnService（TUN）？
 *  Android 沙箱阻止普通 app 在网络层拦截他人流量。
 *  VpnService 是 Android 提供的唯一合法接口：它给 app 一个 TUN 文件描述符，
 *  内核把设备所有出站 IP 包路由到这个虚拟接口，app 读包→处理→写回，
 *  再经 protect() 的真实 socket 发向网络，形成完整的透明代理链路。
 */
class ProxyVpnService : VpnService() {

    companion object {
        const val ACTION_START  = "com.pyrealiy.proxy.START"
        const val ACTION_STOP   = "com.pyrealiy.proxy.STOP"
        const val NOTIF_ID      = 1
        const val CHANNEL_ID    = "pyrealiy_vpn"
        private const val TAG   = "ProxyVpnService"
        private const val MTU   = 1500

        @Volatile var isRunning = false
    }

    private var tunPfd: ParcelFileDescriptor? = null
    private val scope     = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private lateinit var profile: Profile
    private val stopping  = java.util.concurrent.atomic.AtomicBoolean(false)

    // ── 生命周期 ───────────────────────────────────────────────────────────────

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> stopVpn()
            else        -> startVpn()
        }
        return START_STICKY
    }

    private fun startVpn() {
        try {
            startVpnInternal()
        } catch (e: Throwable) {
            Logger.e(TAG, "startVpn crashed", e)
            runCatching { stopVpn() }
        }
    }

    private fun startVpnInternal() {
        val prefs = AppPrefs(this)
        val p = prefs.activeProfile() ?: run {
            Logger.e(TAG, "No active profile"); stopSelf(); return
        }
        profile = p
        isRunning = true
        startForeground(NOTIF_ID, buildNotification())

        // 启动默认网络监听：网络变化时（WiFi↔蜂窝）随时把 underlying 重绑给 VPN，
        // 否则 protect(fd) 出来的流量在 Xiaomi/HyperOS 等设备会被静默丢弃。
        // 需要 ACCESS_NETWORK_STATE 权限，没有会抛 SecurityException——已用 runCatching 兜底。
        runCatching {
            DefaultNetworkListener.start(this) { network ->
                runCatching { setUnderlyingNetworks(network?.let { arrayOf(it) }) }
            }
        }.onFailure { Logger.w(TAG, "DefaultNetworkListener.start failed (continuing)", it) }

        tunPfd = buildTunInterface()

        // 首次显式绑一次：listener 的 onAvailable 可能晚于 establish()
        val initial = runCatching {
            DefaultNetworkListener.current
                ?: getSystemService(ConnectivityManager::class.java)?.activeNetwork
        }.getOrNull()
        initial?.let { runCatching { setUnderlyingNetworks(arrayOf(it)) } }
        Logger.d(TAG, "underlying network bound: $initial")

        // 移交 TUN 控制权给底层 Rust 引擎
        val fd = tunPfd!!.detachFd()
        scope.launch { 
            RustTun.startTunLoop(fd, object : RustTunCallback {
                override fun onNewConnection(localFd: Int, targetIp: String, targetPort: Int) {
                    handleNewTcpBridge(localFd, targetIp, targetPort)
                }
            }) 
        }
        Logger.i(TAG, "VPN started → ${p.serverHost}:${p.serverPort}")
    }

    private fun stopVpn() {
        // 幂等：onStartCommand(STOP) 与 onDestroy 都会调用本函数，二次进入直接返回
        if (!stopping.compareAndSet(false, true)) return
        isRunning = false

        // 把本次会话日志落盘（保留最近 3 份），方便事后导出
        runCatching { SessionLogStore.saveSession(this, Logger.snapshot()) }

        // 取消协程即可中断所有的中继任务
        runCatching { scope.coroutineContext.cancelChildren() }
        runCatching { DefaultNetworkListener.stop() }
        runCatching { tunPfd?.close() }
        tunPfd = null

        runCatching { stopForeground(STOP_FOREGROUND_REMOVE) }
        runCatching { stopSelf() }
        Logger.i(TAG, "VPN stopped")
    }

    override fun onDestroy() {
        runCatching { stopVpn() }
        super.onDestroy()
    }

    /** 用户启动了别的 VPN，系统通知我们让位 */
    override fun onRevoke() {
        Logger.i(TAG, "VPN revoked by system")
        runCatching { stopVpn() }
        super.onRevoke()
    }

    // ── TUN 接口构建 ────────────────────────────────────────────────────────────

    private fun buildTunInterface(): ParcelFileDescriptor {
        val builder = Builder()
            .setSession("PyReality")
            .setMtu(MTU)
            .addAddress("10.10.0.1", 32)
            .addRoute("0.0.0.0", 0)       // 接管所有 IPv4 流量
            .addDnsServer("8.8.8.8")      // 覆盖系统 DNS（由我们拦截 UDP 53）
            .addDnsServer("223.5.5.5")
            .setBlocking(true)
        // API 29+：标记 VPN 接口为非按流量计费，避免某些 app（YouTube/Spotify 等）
        // 把 VPN 当作 metered 网络而限制行为
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.Q) {
            builder.setMetered(false)
        }
        return builder.establish()!!
    }

    // ── TCP 桥接中继 (Rust 抛出回调) ─────────────────────────────────────────

    private fun handleNewTcpBridge(localFd: Int, targetIp: String, targetPort: Int) {
        val pfd = ParcelFileDescriptor.adoptFd(localFd)
        val localIn = java.io.FileInputStream(pfd.fileDescriptor)
        val localOut = java.io.FileOutputStream(pfd.fileDescriptor)

        // 为这个新连接开辟协程
        scope.launch {
            val tunnel = com.pyrealiy.proxy.core.ProxyTunnel(
                profile.serverHost, profile.serverPort, profile.password, profile.sni,
                protect = { s: java.net.Socket -> protect(s) }
            )
            try {
                // 1. 与远端 PyRealiy 服务器握手并建立加密隧道
                tunnel.connect(targetIp, targetPort)

                // 2. 双向转发
                // 协程 A: 读本地真实数据 -> 加密 -> 发远端
                val sendJob = launch {
                    val buf = ByteArray(8192)
                    while (true) {
                        val n = localIn.read(buf)
                        if (n <= 0) break
                        tunnel.send(buf.copyOf(n))
                    }
                    // 本地应用写完（如 HTTP 请求发送完毕，发送 FIN）
                    tunnel.sendCloseNotify()
                }

                // 协程 B: 读远端加密数据 -> 解密 -> 写入本地 (返回给 App)
                val recvJob = launch {
                    while (true) {
                        val data = tunnel.recv()
                        localOut.write(data)
                        localOut.flush()
                    }
                }

                // 任一方向断开，则结束转发
                sendJob.invokeOnCompletion { recvJob.cancel() }
                recvJob.invokeOnCompletion { sendJob.cancel() }
                
                sendJob.join()
                recvJob.join()

            } catch (e: Exception) {
                Logger.w(TAG, "Relay $targetIp:$targetPort failed: ${e.message}")
            } finally {
                runCatching { tunnel.close() }
                runCatching { pfd.close() }
            }
        }
    }

    // ── 前台通知 ───────────────────────────────────────────────────────────────

    private fun buildNotification(): Notification {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val nm = getSystemService(NotificationManager::class.java)
            if (nm.getNotificationChannel(CHANNEL_ID) == null) {
                nm.createNotificationChannel(
                    NotificationChannel(CHANNEL_ID, "PyReality VPN", NotificationManager.IMPORTANCE_LOW)
                )
            }
        }
        val stopIntent = PendingIntent.getService(
            this, 0,
            Intent(this, ProxyVpnService::class.java).setAction(ACTION_STOP),
            PendingIntent.FLAG_IMMUTABLE,
        )
        val mainIntent = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java),
            PendingIntent.FLAG_IMMUTABLE,
        )
        return Notification.Builder(this, CHANNEL_ID)
            .setContentTitle("PyReality")
            .setContentText("已连接 · ${profile.serverHost}")
            .setSmallIcon(R.drawable.ic_vpn)
            .setContentIntent(mainIntent)
            .addAction(Notification.Action.Builder(null, "断开", stopIntent).build())
            .setOngoing(true)
            .build()
    }
}
