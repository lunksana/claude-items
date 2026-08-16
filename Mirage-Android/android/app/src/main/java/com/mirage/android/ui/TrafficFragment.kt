package com.mirage.android.ui

import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.TextView
import androidx.fragment.app.Fragment
import androidx.lifecycle.lifecycleScope
import com.mirage.android.R
import com.mirage.android.TrafficChart
import com.mirage.android.core.CoreController
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

/**
 * 流量监测 Tab: 实时速率 + 历史曲线 + 统计。
 */
class TrafficFragment : Fragment() {

    private val historyUp = ArrayDeque<Float>()
    private val historyDown = ArrayDeque<Float>()

    override fun onCreateView(inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?): View {
        return inflater.inflate(R.layout.fragment_traffic, container, false)
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        lifecycleScope.launch {
            while (isActive) {
                refresh()
                delay(1000)
            }
        }
    }

    private fun refresh() {
        val v = view ?: return
        val stats = runCatching { CoreController.getStats() }.getOrNull() ?: return
        if (stats.size < 7) return
        v.findViewById<TextView>(R.id.upRate).text = "↑ ${fmtRate(stats[2])}"
        v.findViewById<TextView>(R.id.downRate).text = "↓ ${fmtRate(stats[3])}"
        v.findViewById<TextView>(R.id.totalUp).text = "累计上行: ${fmtBytes(stats[0])}"
        v.findViewById<TextView>(R.id.totalDown).text = "累计下行: ${fmtBytes(stats[1])}"
        v.findViewById<TextView>(R.id.conns).text = "活跃连接: TCP ${stats[4].toInt()} / UDP ${stats[5].toInt()}"
        v.findViewById<TextView>(R.id.dnsCount).text = "DNS 查询: ${stats[6].toLong()}"
        // 日志显示 (内核 + App 日志合并)
        val logs = (com.mirage.android.core.LogStore.all() + CoreController.recentLogs().toList())
        v.findViewById<TextView>(R.id.trafficLogView).text = logs.takeLast(80).joinToString("\n")

        historyUp.addLast(stats[2].toFloat())
        historyDown.addLast(stats[3].toFloat())
        while (historyUp.size > 60) historyUp.removeFirst()
        while (historyDown.size > 60) historyDown.removeFirst()
        v.findViewById<TrafficChart>(R.id.chart).setData(historyUp.toList(), historyDown.toList())
    }

    private fun fmtBytes(b: Double): String = when {
        b >= 1 shl 30 -> "%.2f GB".format(b / (1 shl 30))
        b >= 1 shl 20 -> "%.1f MB".format(b / (1 shl 20))
        b >= 1 shl 10 -> "%.1f KB".format(b / (1 shl 10))
        else -> "%.0f B".format(b)
    }

    private fun fmtRate(bps: Double): String {
        val b = bps.coerceAtLeast(0.0)
        return when {
            b >= 1 shl 20 -> "%.2f MB/s".format(b / (1 shl 20))
            b >= 1 shl 10 -> "%.1f KB/s".format(b / (1 shl 10))
            else -> "%.0f B/s".format(b)
        }
    }
}
