package com.mirage.android.core

import android.util.Log
import java.util.concurrent.ConcurrentLinkedDeque

/** 内存日志环 (App 日志面板 + Android Logcat 输出)。 */
object LogStore {
    private val logs = ConcurrentLinkedDeque<String>()
    private const val CAP = 500

    fun append(line: String) {
        Log.i("Mirage", line)
        logs.addLast(line)
        while (logs.size > CAP) logs.pollFirst()
    }

    fun all(): List<String> = logs.toList()

    fun clear() = logs.clear()
}
