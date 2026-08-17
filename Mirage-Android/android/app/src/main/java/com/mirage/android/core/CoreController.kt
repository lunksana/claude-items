package com.mirage.android.core

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.os.IBinder
import kotlinx.coroutines.flow.MutableStateFlow
import java.util.concurrent.CopyOnWriteArraySet

/**
 * CoreController: App (UI) 侧的控制层客户端。
 * 通过 AIDL 绑定 :core 进程的 CoreService, 所有内核操作都走这里。
 */
object CoreController {

    private var service: ICoreService? = null
    private var bound = false
    private var ctx: Context? = null
    private val callbacks = CopyOnWriteArraySet<ICoreCallback>()

    /** 内核运行状态 (UI 轮询/回调更新)。 */
    val running = MutableStateFlow(false)

    private val conn = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, binder: IBinder?) {
            val s = ICoreService.Stub.asInterface(binder)
            service = s
            bound = true
            // 重新注册所有待生效回调
            callbacks.forEach { cb ->
                runCatching { s.registerCallback(cb) }
            }
            // 立即同步一次状态
            runCatching {
                val isRun = s.isRunning
                running.value = isRun
                callbacks.forEach { cb -> runCatching { cb.onStateChanged(isRun) } }
            }
        }

        override fun onServiceDisconnected(name: ComponentName?) {
            service = null
            bound = false
            running.value = false
            callbacks.forEach { cb -> runCatching { cb.onStateChanged(false) } }
        }
    }

    /** 绑定 core 进程服务 (每次进入 UI 时调用)。 */
    fun bind(context: Context) {
        if (bound && service != null) return
        ctx = context.applicationContext
        val intent = Intent(context, Class.forName("com.mirage.android.CoreService"))
        context.bindService(intent, conn, Context.BIND_AUTO_CREATE)
    }

    fun unbind(context: Context) {
        if (bound) {
            context.unbindService(conn)
            bound = false
            service = null
        }
    }

    fun isBound(): Boolean = bound && service != null

    private inline fun <T> call(block: (ICoreService) -> T): T? {
        val s = service ?: return null
        return try { block(s) } catch (e: Exception) { null }
    }

    // ── 控制 ──
    fun start(): Int = call { it.start() } ?: -100
    fun stop() { call { it.stop() } }
    fun setNode(uri: String): Boolean = call { it.setNode(uri) } ?: false
    fun setRules(json: String): Boolean = call { it.setRules(json) } ?: false
    fun setLogLevel(level: String): Boolean = call { it.setLogLevel(level) } ?: false

    // ── 状态 ──
    fun isRunning(): Boolean = call { it.isRunning() } ?: false
    fun isHealthy(): Boolean = call { it.isHealthy() } ?: false
    fun latencyMs(): Long = call { it.latencyMs() } ?: -1
    fun getStats(): DoubleArray? = call { it.stats }
    fun getConnectionsJson(): String = call { it.connectionsJson } ?: "[]"
    fun recentLogs(): Array<String> = call { it.recentLogs() } ?: emptyArray()
    fun getBuiltinDomains(): Array<String> = call { it.builtinDomains } ?: emptyArray()
    fun getBuiltinIpCount(): Long = call { it.builtinIpCount } ?: 0
    fun testNode(uri: String, timeoutMs: Int): Long = call { it.testNode(uri, timeoutMs) } ?: -1

    fun registerCallback(cb: ICoreCallback) {
        callbacks.add(cb)
        service?.let { s -> runCatching { s.registerCallback(cb) } }
    }

    fun unregisterCallback(cb: ICoreCallback) {
        callbacks.remove(cb)
        service?.let { s -> runCatching { s.unregisterCallback(cb) } }
    }
}
