package com.mirage.android.ui

import android.content.Intent
import android.net.VpnService
import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import android.widget.CompoundButton
import android.widget.Switch
import android.widget.Toast
import android.widget.TextView
import androidx.core.content.ContextCompat
import androidx.fragment.app.Fragment
import androidx.lifecycle.lifecycleScope
import com.mirage.android.CoreService
import com.mirage.android.R
import com.mirage.android.core.CoreController
import com.mirage.android.core.NodeStore
import com.mirage.android.core.RuleStore
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext
import java.net.InetSocketAddress
import java.net.Socket

/**
 * 首页 (meow 风格): 顶栏开关 + 状态卡片 + 上传/下载流量卡片。
 */
class HomeFragment : Fragment() {

    override fun onCreateView(inflater: LayoutInflater, container: ViewGroup?, savedInstanceState: Bundle?): View {
        return inflater.inflate(R.layout.fragment_home, container, false)
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)
        view.findViewById<Switch>(R.id.connectSwitch).setOnCheckedChangeListener { _, checked ->
            if (checked) toggleConnection() else disconnect()
        }
        view.findViewById<Button>(R.id.connectBtn).setOnClickListener { toggleConnection() }
        view.findViewById<TextView>(R.id.currentNode).setOnClickListener {
            Toast.makeText(requireContext(), "节点管理: 请切换到底部「节点」Tab", Toast.LENGTH_SHORT).show()
        }
        startPolling()
    }

    private fun toggleConnection() {
        val sw = view?.findViewById<Switch>(R.id.connectSwitch) ?: return
        sw.isEnabled = false
        if (NodeStore.getNodes(requireContext()).isEmpty()) {
            Toast.makeText(requireContext(), "请先添加节点 (底部导航→节点)", Toast.LENGTH_LONG).show()
            sw.isChecked = false
            sw.isEnabled = true
            return
        }
        // 自动选择: 连接前测活选最优节点
        if (NodeStore.isAutoSelect(requireContext()) && NodeStore.getNodes(requireContext()).size > 1) {
            viewLifecycleOwner.lifecycleScope.launch {
                val method = NodeStore.getTestMethod(requireContext())
                val best = withContext(Dispatchers.IO) { autoSelectBest(method) }
                sw.isEnabled = true
                if (best != null) {
                    NodeStore.setSelected(requireContext(), best)
                }
                proceedConnect()
            }
            return
        }
        sw.isEnabled = true
        proceedConnect()
    }

    private fun disconnect() {
        // ① 同步停 Rust 引擎
        runCatching { CoreController.stop() }
        // ② 发 ACTION_STOP 让 CoreService 完整停止 (撤 TUN + 前台 + stopSelf)
        val stop = Intent(requireContext(), CoreService::class.java)
            .setAction(CoreService.ACTION_STOP)
        runCatching { requireContext().startService(stop) }
        runCatching { requireContext().stopService(stop) }
    }

    /** 测活选最优, 返回节点下标; 全失败返回 null。 */
    private fun autoSelectBest(method: String): Int? {
        val nodes = NodeStore.getNodes(requireContext())
        var bestIdx: Int? = null
        var bestRtt = Long.MAX_VALUE
        nodes.forEachIndexed { i, n ->
            val ok = try {
                when (method) {
                    "connect" -> CoreController.testNode(n.uri, 5000) >= 0
                    else -> {
                        val s = Socket()
                        val t0 = System.currentTimeMillis()
                        s.connect(InetSocketAddress(n.server, n.port.toIntOrNull() ?: 443), 4000)
                        val rtt = System.currentTimeMillis() - t0
                        s.close()
                        if (rtt < bestRtt) { bestRtt = rtt; bestIdx = i }
                        true
                    }
                }
            } catch (e: Exception) { false }
            if (method == "connect" && ok) {
                val rtt = CoreController.testNode(n.uri, 5000)
                if (rtt >= 0 && rtt < bestRtt) { bestRtt = rtt; bestIdx = i }
            }
        }
        return bestIdx
    }

    private fun proceedConnect() {
        runCatching { CoreController.setRules(RuleStore.toJson(requireContext())) }
        val intent = VpnService.prepare(requireContext())
        if (intent != null) {
            startActivityForResult(intent, REQ_VPN_PERMISSION)
            return
        }
        startVpn()
    }

    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode == REQ_VPN_PERMISSION && resultCode == android.app.Activity.RESULT_OK) {
            startVpn()
        }
    }

    private fun startVpn() {
        // 启动独立内核进程: onStartCommand 自动 startForeground + 建 TUN + 启动内核
        requireContext().startForegroundService(Intent(requireContext(), CoreService::class.java))
    }

    private fun startPolling() {
        lifecycleScope.launch {
            while (isActive) {
                refresh()
                delay(1000)
            }
        }
    }

    private fun refresh() {
        val v = view ?: return
        val running = CoreController.isRunning()

        // 开关与状态同步 (临时移除 listener, 避免断开瞬间 running 未及时变 false 导致重连循环)
        val sw = v.findViewById<Switch>(R.id.connectSwitch)
        if (sw.isChecked != running) {
            sw.setOnCheckedChangeListener(null)
            sw.isChecked = running
            sw.setOnCheckedChangeListener { _, checked -> if (checked) toggleConnection() else disconnect() }
        }
        v.findViewById<Button>(R.id.connectBtn).text =
            if (running) getString(R.string.disconnect) else getString(R.string.connect)

        // 状态卡片: 连接状态色
        val statusText = v.findViewById<TextView>(R.id.statusText)
        if (running) {
            statusText.text = "已连接"
            statusText.setTextColor(ContextCompat.getColor(requireContext(), R.color.meow_connected))
        } else {
            statusText.text = getString(R.string.status_idle)
            statusText.setTextColor(ContextCompat.getColor(requireContext(), R.color.meow_disconnected))
        }

        val curNode = v.findViewById<TextView>(R.id.currentNode)
        val uri = NodeStore.getSelectedUri(requireContext())
        curNode.text = if (uri.isNotEmpty()) "节点: ${NodeStore.defaultName(uri)}" else "节点: (无) → 点击添加"

        // 流量卡片
        val stats = runCatching { CoreController.getStats() }.getOrNull()
        if (stats != null && stats.size >= 7) {
            v.findViewById<TextView>(R.id.upRate).text = fmtRate(stats[2])
            v.findViewById<TextView>(R.id.downRate).text = fmtRate(stats[3])
            v.findViewById<TextView>(R.id.totalFlow).text = "累计: ↑${fmtBytes(stats[0])} / ↓${fmtBytes(stats[1])}"
            v.findViewById<TextView>(R.id.connsInfo).text =
                "TCP ${stats[4].toInt()} · UDP ${stats[5].toInt()} · DNS ${stats[6].toLong()}"
        }
    }

    private fun fmtBytes(b: Double): String = when {
        b >= 1 shl 30 -> "%.2fG".format(b / (1 shl 30))
        b >= 1 shl 20 -> "%.1fM".format(b / (1 shl 20))
        b >= 1 shl 10 -> "%.1fK".format(b / (1 shl 10))
        else -> "%.0fB".format(b)
    }

    private fun fmtRate(bps: Double): String {
        val b = bps.coerceAtLeast(0.0)
        return when {
            b >= 1 shl 20 -> "%.2f MB/s".format(b / (1 shl 20))
            b >= 1 shl 10 -> "%.1f KB/s".format(b / (1 shl 10))
            else -> "%.0f B/s".format(b)
        }
    }

    companion object {
        private const val REQ_VPN_PERMISSION = 100
    }
}
