package com.pyrealiy.proxy.core

import android.util.Log
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * 应用内日志：同步写入 logcat + 进程内环形缓冲，
 * 供 LogActivity 实时显示。
 *
 * 上限 1000 行；超出从头丢弃。
 */
object Logger {
    private const val MAX_LINES = 300       // 控制内存：每行约 100B，~30KB 上限
    private val buffer = ArrayDeque<String>()
    private val listeners = mutableListOf<() -> Unit>()
    private val timeFmt = SimpleDateFormat("HH:mm:ss.SSS", Locale.US)

    fun d(tag: String, msg: String)                          { Log.d(tag, msg);    append('D', tag, msg, null) }
    fun i(tag: String, msg: String)                          { Log.i(tag, msg);    append('I', tag, msg, null) }
    fun w(tag: String, msg: String, t: Throwable? = null)    { Log.w(tag, msg, t); append('W', tag, msg, t) }
    fun e(tag: String, msg: String, t: Throwable? = null)    { Log.e(tag, msg, t); append('E', tag, msg, t) }

    private fun append(lvl: Char, tag: String, msg: String, t: Throwable?) {
        val ts = timeFmt.format(Date())
        val line = buildString {
            append('[').append(ts).append("] ").append(lvl).append('/').append(tag).append(": ").append(msg)
            if (t != null) { append('\n'); append(Log.getStackTraceString(t)) }
        }
        synchronized(buffer) {
            buffer.addLast(line)
            while (buffer.size > MAX_LINES) buffer.removeFirst()
        }
        // 仅触发监听者，listener 内部负责节流（避免高频日志期间 UI 洪水）
        val snap = synchronized(listeners) { if (listeners.isEmpty()) null else listeners.toList() }
        snap?.forEach { runCatching { it() } }
    }

    fun snapshot(): List<String> = synchronized(buffer) { buffer.toList() }
    fun clear() {
        synchronized(buffer) { buffer.clear() }
        synchronized(listeners) { listeners.toList() }.forEach { it() }
    }
    fun addListener(l: () -> Unit)    = synchronized(listeners) { listeners.add(l) }
    fun removeListener(l: () -> Unit) = synchronized(listeners) { listeners.remove(l) }
}
