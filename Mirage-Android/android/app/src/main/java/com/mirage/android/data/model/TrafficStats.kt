package com.mirage.android.data.model

/**
 * 流量与连接统计模型。
 */
data class TrafficStats(
    val upTotal: Double = 0.0,
    val downTotal: Double = 0.0,
    val upRate: Double = 0.0,      // B/s
    val downRate: Double = 0.0,    // B/s
    val tcpConns: Int = 0,
    val udpFlows: Int = 0,
    val dnsQueries: Long = 0L,
    val timestamp: Long = System.currentTimeMillis()
) {
    val upRateFormatted: String get() = fmtRate(upRate)
    val downRateFormatted: String get() = fmtRate(downRate)
    val upTotalFormatted: String get() = fmtBytes(upTotal)
    val downTotalFormatted: String get() = fmtBytes(downTotal)

    companion object {
        fun fromArray(arr: DoubleArray): TrafficStats {
            if (arr.size < 7) return TrafficStats()
            return TrafficStats(
                upTotal = arr[0],
                downTotal = arr[1],
                upRate = arr[2],
                downRate = arr[3],
                tcpConns = arr[4].toInt(),
                udpFlows = arr[5].toInt(),
                dnsQueries = arr[6].toLong()
            )
        }

        fun fmtBytes(b: Double): String {
            val v = b.coerceAtLeast(0.0)
            return when {
                v >= 1 shl 30 -> "%.2f GB".format(v / (1 shl 30))
                v >= 1 shl 20 -> "%.1f MB".format(v / (1 shl 20))
                v >= 1 shl 10 -> "%.1f KB".format(v / (1 shl 10))
                else -> "%.0f B".format(v)
            }
        }

        fun fmtRate(bps: Double): String {
            val b = bps.coerceAtLeast(0.0)
            return when {
                b >= 1 shl 20 -> "%.2f MB/s".format(b / (1 shl 20))
                b >= 1 shl 10 -> "%.1f KB/s".format(b / (1 shl 10))
                else -> "%.0f B/s".format(b)
            }
        }
    }
}
