package com.mirage.android.data.model

/**
 * 日志过滤级别。
 */
enum class LogLevel(val label: String, val priority: Int) {
    ALL("全部", 0),
    INFO("INFO", 1),
    WARN("WARN", 2),
    ERROR("ERROR", 3),
    DEBUG("DEBUG", 4),
    TRACE("TRACE", 5);

    fun matches(line: String): Boolean {
        if (this == ALL) return true
        val upper = line.uppercase()
        return when (this) {
            INFO -> upper.contains("[INFO]") || upper.contains(" INFO ") || upper.contains("[CORE]") || upper.contains("[LOADER]") || upper.contains("[APP]") || upper.contains("OK=TRUE")
            WARN -> upper.contains("[WARN]") || upper.contains(" WARN ") || upper.contains("WARNING")
            ERROR -> upper.contains("[ERROR]") || upper.contains(" ERROR ") || upper.contains("FATAL") || upper.contains("EXCEPTION") || upper.contains("FAILED") || upper.contains("失败")
            DEBUG -> upper.contains("[DEBUG]") || upper.contains(" DEBUG ")
            TRACE -> upper.contains("[TRACE]") || upper.contains(" TRACE ")
            ALL -> true
        }
    }
}
