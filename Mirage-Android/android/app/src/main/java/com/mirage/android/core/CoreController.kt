package com.mirage.android.core

import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.content.ServiceConnection
import android.os.IBinder
import kotlinx.coroutines.flow.MutableStateFlow

/**
 * CoreController: App (UI) 侧的控制层客户端。
 * 通过 AIDL 绑定 :core 进程的 CoreService, 所有内核操作都走这里。
 */
object CoreController {

    private var service: ICoreService? = null
    private var bound = false
    private var ctx: Context? = null

    /** 内核运行状态 (UI 轮询/回调更新)。 */
    val running = MutableStateFlow(false)

    private val conn = object : ServiceConnection {
        override fun onServiceConnected(name: ComponentName?, binder: IBinder?) {
            service = ICoreService.Stub.asInterface(binder)
            bound = true
        }
        override fun onServiceDisconnected(name: ComponentName?) {
            service = null
            bound = false
        }
    }

    /** 绑定 core 进程服务 (每次进入 UI 时调用)。 */
    fun bind(context: Context) {
        if (bound) return
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

    // ── 状态 ──
    fun isRunning(): Boolean = call { it.isRunning() } ?: false
    fun isHealthy(): Boolean = call { it.isHealthy() } ?: false
    fun latencyMs(): Long = call { it.latencyMs() } ?: -1
    fun getStats(): DoubleArray? = call { it.stats }
    fun recentLogs(): Array<String> = call { it.recentLogs() } ?: emptyArray()
    fun getBuiltinDomains(): Array<String> = call { it.builtinDomains } ?: emptyArray()
    fun getBuiltinIpCount(): Long = call { it.builtinIpCount } ?: 0
    fun testNode(uri: String, timeoutMs: Int): Long = call { it.testNode(uri, timeoutMs) } ?: -1
    fun registerCallback(cb: ICoreCallback) { call { it.registerCallback(cb) } }
    fun unregisterCallback(cb: ICoreCallback) { call { it.unregisterCallback(cb) } }
}
