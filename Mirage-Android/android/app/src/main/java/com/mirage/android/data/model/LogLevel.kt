package com.mirage.android.data.model

/**
 * 日志过滤级别 (采用标准的日志等级严重度分层模型):
 * - ALL (-1): 输出所有日志无任何过滤
 * - TRACE (0): 输出所有 TRACE, DEBUG, INFO, WARN, ERROR 及系统底层日志
 * - DEBUG (1): 输出所有 DEBUG, INFO, WARN, ERROR 及系统底层日志
 * - INFO (2): 仅输出常态运行日志 (INFO, WARN, ERROR)
 * - WARN (3): 仅输出潜在问题与故障日志 (WARN, ERROR)
 * - ERROR (4): 仅输出致命错误与异常 (ERROR)
 */
enum class LogLevel(val label: String, val severity: Int) {
    ALL("全部", -1),
    TRACE("TRACE", 0),
    DEBUG("DEBUG", 1),
    INFO("INFO", 2),
    WARN("WARN", 3),
    ERROR("ERROR", 4);

    companion object {
        fun parseSeverity(line: String): Int {
            val upper = line.uppercase()
            return when {
                upper.contains(" ERROR ") || upper.contains("[ERROR]") || upper.contains("FATAL") || upper.contains("EXCEPTION") || upper.contains("FAILED") || upper.contains("失败") -> 4
                upper.contains(" WARN ") || upper.contains("[WARN]") || upper.contains("WARNING") -> 3
                upper.contains(" TRACE ") || upper.contains("[TRACE]") -> 0
                upper.contains(" DEBUG ") || upper.contains("[DEBUG]") || upper.contains("[TUN-") -> 1
                else -> 2 // 默认未打标系统日志 (如 [core], [loader], [app]) 归为 INFO (2)
            }
        }
    }

    fun matches(line: String): Boolean {
        if (this == ALL) return true
        val lineSeverity = parseSeverity(line)
        return lineSeverity >= this.severity
    }
}
