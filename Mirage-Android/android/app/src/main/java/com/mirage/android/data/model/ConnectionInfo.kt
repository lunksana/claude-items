package com.mirage.android.data.model

import org.json.JSONObject

/**
 * 活跃 / 最近连接实体模型。
 */
data class ConnectionInfo(
    val id: Long,
    val protocol: String,
    val target: String,
    val outbound: String,
    val status: String,
    val upBytes: Long,
    val downBytes: Long,
    val startTime: Long,
    val durationSecs: Long
) {
    val upFormatted: String get() = formatBytes(upBytes)
    val downFormatted: String get() = formatBytes(downBytes)
    val isClosed: Boolean get() = status == "已断开"
    val isDirect: Boolean get() = outbound.contains("直连")

    companion object {
        fun fromJson(json: JSONObject): ConnectionInfo {
            return ConnectionInfo(
                id = json.optLong("id", 0),
                protocol = json.optString("protocol", "TCP"),
                target = json.optString("target", "-"),
                outbound = json.optString("outbound", "隧道代理"),
                status = json.optString("status", "已连接"),
                upBytes = json.optLong("up_bytes", 0),
                downBytes = json.optLong("down_bytes", 0),
                startTime = json.optLong("start_time", 0),
                durationSecs = json.optLong("duration_secs", 0)
            )
        }

        private fun formatBytes(bytes: Long): String {
            val b = bytes.toDouble()
            return when {
                b >= 1024 * 1024 * 1024 -> "%.1f GB".format(b / (1024 * 1024 * 1024))
                b >= 1024 * 1024 -> "%.1f MB".format(b / (1024 * 1024))
                b >= 1024 -> "%.1f KB".format(b / 1024)
                else -> "${bytes} B"
            }
        }
    }
}
