package com.mirage.android.core

import java.util.concurrent.ConcurrentLinkedDeque

/** 内存日志环 (App 日志面板)。 */
object LogStore {
    private val logs = ConcurrentLinkedDeque<String>()
    private const val CAP = 500

    fun append(line: String) {
        logs.addLast(line)
        while (logs.size > CAP) logs.pollFirst()
    }

    fun all(): List<String> = logs.toList()

    fun clear() = logs.clear()
}
