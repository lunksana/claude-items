package com.mirage.android.ui

import android.graphics.Color
import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.Toast
import androidx.core.content.ContextCompat
import androidx.fragment.app.Fragment
import androidx.fragment.app.activityViewModels
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import com.mirage.android.MainActivity
import com.mirage.android.R
import com.mirage.android.data.model.VpnState
import com.mirage.android.databinding.FragmentHomeBinding
import com.mirage.android.ui.viewmodel.HomeViewModel
import kotlinx.coroutines.launch

/**
 * 首页: 现代 Material 3 卡片布局 + 响应式状态流绑定。
 */
class HomeFragment : Fragment() {

    private var _binding: FragmentHomeBinding? = null
    private val binding get() = _binding!!

    private val viewModel: HomeViewModel by activityViewModels()

    override fun onCreateView(
        inflater: LayoutInflater,
        container: ViewGroup?,
        savedInstanceState: Bundle?
    ): View {
        _binding = FragmentHomeBinding.inflate(inflater, container, false)
        return binding.root
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)

        updateVersionBadge()

        binding.tvVersion.setOnClickListener {
            showVersionDetailsDialog()
        }

        binding.tvAppSubtitle.setOnClickListener {
            showVersionDetailsDialog()
        }

        binding.connectSwitch.setOnCheckedChangeListener { _, isChecked ->
            if (isChecked && !viewModel.vpnState.value.isRunning) {
                performConnect()
            } else if (!isChecked && viewModel.vpnState.value.isRunning) {
                viewModel.disconnect()
            }
        }

        binding.btnBackup.setOnClickListener { showBackupDialog() }
        binding.btnRestore.setOnClickListener { showRestoreDialog() }

        binding.connectBtn.setOnClickListener {
            if (viewModel.vpnState.value.isRunning) {
                viewModel.disconnect()
            } else {
                performConnect()
            }
        }

        binding.nodeSelectCard.setOnClickListener {
            (activity as? MainActivity)?.navigateToTab(1)
        }

        binding.dnsCard.setOnClickListener {
            showDnsConfigDialog()
        }

        observeState()
    }

    private fun performConnect() {
        val selected = viewModel.selectedNode.value
        if (selected == null) {
            Toast.makeText(requireContext(), "请先添加或选择节点 (Tab: 节点)", Toast.LENGTH_SHORT).show()
            binding.connectSwitch.isChecked = false
            return
        }

        (activity as? MainActivity)?.requestVpnPermissionAndConnect()
    }

    private fun observeState() {
        viewLifecycleOwner.lifecycleScope.launch {
            viewLifecycleOwner.repeatOnLifecycle(Lifecycle.State.STARTED) {
                launch {
                    viewModel.vpnState.collect { state ->
                        updateVpnUi(state)
                    }
                }
                launch {
                    viewModel.selectedNode.collect { node ->
                        binding.currentNode.text = if (node != null) {
                            "节点: ${node.displayName}"
                        } else {
                            "节点: (无) → 点击添加"
                        }
                    }
                }
                launch {
                    viewModel.trafficStats.collect { stats ->
                        binding.upRate.text = stats.upRateFormatted
                        binding.downRate.text = stats.downRateFormatted
                        binding.totalFlow.text =
                            "累计流量: ↑${stats.upTotalFormatted} / ↓${stats.downTotalFormatted}"
                        binding.connsInfo.text =
                            "活跃连接: TCP ${stats.tcpConns} · UDP ${stats.udpFlows} · DNS ${stats.dnsQueries}"
                        // 今日/本月用量 (持久化统计)
                        val today = com.mirage.android.core.TrafficStatsStore.getToday(requireContext())
                        val month = com.mirage.android.core.TrafficStatsStore.getThisMonth(requireContext())
                        binding.todayUsage.text =
                            "今日: ↑${fmtBytes(today.first.toDouble())} / ↓${fmtBytes(today.second.toDouble())} · 本月: ↑${fmtBytes(month.first.toDouble())} / ↓${fmtBytes(month.second.toDouble())}"
                    }
                }
                launch {
                    val dnsRepo = com.mirage.android.data.repository.DnsRepository.getInstance(requireContext())
                    dnsRepo.directDns.collect { direct ->
                        binding.tvDnsSummary.text = "国内: $direct · 国外: ${dnsRepo.getRemoteDns()}"
                    }
                }
                launch {
                    val dnsRepo = com.mirage.android.data.repository.DnsRepository.getInstance(requireContext())
                    dnsRepo.remoteDns.collect { remote ->
                        binding.tvDnsSummary.text = "国内: ${dnsRepo.getDirectDns()} · 国外: $remote"
                    }
                }
                launch {
                    viewModel.latencyMs.collect { rtt ->
                        if (rtt >= 0 && viewModel.vpnState.value is VpnState.Connected) {
                            binding.tvLatency.visibility = View.VISIBLE
                            binding.tvLatency.text = "隧道 RTT: ${rtt}ms"
                        } else {
                            binding.tvLatency.visibility = View.GONE
                        }
                    }
                }
            }
        }
    }

    private fun updateVpnUi(state: VpnState) {
        val running = state.isRunning
        if (binding.connectSwitch.isChecked != running) {
            binding.connectSwitch.isChecked = running
        }

        when (state) {
            is VpnState.Connected -> {
                binding.statusDot.backgroundTintList = android.content.res.ColorStateList.valueOf(ContextCompat.getColor(requireContext(), R.color.meow_connected))
                binding.statusText.text = "已连接 (加密隧道保护中)"
                binding.statusText.setTextColor(ContextCompat.getColor(requireContext(), R.color.meow_connected))
                binding.connectBtn.text = getString(R.string.disconnect)
                binding.connectBtn.setBackgroundColor(ContextCompat.getColor(requireContext(), R.color.meow_error))
            }
            is VpnState.Connecting -> {
                binding.statusDot.backgroundTintList = android.content.res.ColorStateList.valueOf(ContextCompat.getColor(requireContext(), R.color.meow_ginger))
                binding.statusText.text = "正在建立加密隧道…"
                binding.statusText.setTextColor(ContextCompat.getColor(requireContext(), R.color.meow_ginger))
                binding.connectBtn.text = "正在连接…"
                binding.connectBtn.setBackgroundColor(ContextCompat.getColor(requireContext(), R.color.meow_ginger))
            }
            is VpnState.Stopping -> {
                binding.statusDot.backgroundTintList = android.content.res.ColorStateList.valueOf(ContextCompat.getColor(requireContext(), R.color.meow_disconnected))
                binding.statusText.text = "正在断开连接…"
                binding.statusText.setTextColor(ContextCompat.getColor(requireContext(), R.color.meow_disconnected))
                binding.connectBtn.text = "断开中…"
            }
            is VpnState.Error -> {
                binding.statusDot.backgroundTintList = android.content.res.ColorStateList.valueOf(ContextCompat.getColor(requireContext(), R.color.meow_error))
                binding.statusText.text = "连接异常: ${state.message}"
                binding.statusText.setTextColor(ContextCompat.getColor(requireContext(), R.color.meow_error))
                binding.connectBtn.text = getString(R.string.connect)
                binding.connectBtn.setBackgroundColor(ContextCompat.getColor(requireContext(), R.color.meow_blue))
            }
            is VpnState.Disconnected -> {
                binding.statusDot.backgroundTintList = android.content.res.ColorStateList.valueOf(ContextCompat.getColor(requireContext(), R.color.meow_disconnected))
                binding.statusText.text = getString(R.string.status_idle)
                binding.statusText.setTextColor(ContextCompat.getColor(requireContext(), R.color.meow_disconnected))
                binding.connectBtn.text = getString(R.string.connect)
                binding.connectBtn.setBackgroundColor(ContextCompat.getColor(requireContext(), R.color.meow_blue))
            }
        }
    }

    fun updateVersionBadge() {
        val ctx = context ?: return
        val activeCore = com.mirage.android.core.CoreManager.getInstance(ctx).getActiveCore()
        val coreTag = if (activeCore.isBuiltin) "内置" else "自定义"
        _binding?.tvVersion?.text = "v${com.mirage.android.BuildConfig.VERSION_NAME} · $coreTag"
        _binding?.tvAppSubtitle?.text = "安全隧道代理 · Build ${com.mirage.android.BuildConfig.BUILD_TIME} (#${com.mirage.android.BuildConfig.VERSION_CODE})"
    }

    private fun showVersionDetailsDialog() {
        val ctx = requireContext()
        val activeCore = com.mirage.android.core.CoreManager.getInstance(ctx).getActiveCore()
        val coreDesc = if (activeCore.isBuiltin) "内置核心 (Mirage Rust Core)" else "自定义核心: ${activeCore.name}"
        val nativeVer = runCatching { com.mirage.android.core.MirageNative.version() }.getOrDefault("v0.2.1")

        val info = """
            📱 客户端版本: v${com.mirage.android.BuildConfig.VERSION_NAME}
            🔢 版本编号: Code ${com.mirage.android.BuildConfig.VERSION_CODE} (${com.mirage.android.BuildConfig.BUILD_TAG})
            📅 构建日期: ${com.mirage.android.BuildConfig.BUILD_TIME}
            ⚙️ 运行内核: $coreDesc ($nativeVer)
            🧩 架构对齐: arm64-v8a (16KB Page Aligned)
            🛡️ 兼容环境: Android 9.0 (API 28) ~ Android 16 (API 36+)
            🚀 核心特性: QUIC ICMP 端口不可达即时回退、WarmPool 预热池、全量 DNS 路由分流
        """.trimIndent()

        android.app.AlertDialog.Builder(ctx)
            .setTitle("版本与运行环境详情")
            .setMessage(info)
            .setPositiveButton("管理内核") { _, _ ->
                (activity as? MainActivity)?.showCoreManagerDialog()
            }
            .setNegativeButton("关闭", null)
            .show()
    }

    private fun showDnsConfigDialog() {
        DnsConfigDialog(requireContext()).show()
    }

    /** 备份配置: 显示导出的 JSON, 可复制/分享。 */
    private fun showBackupDialog() {
        val json = com.mirage.android.core.ConfigBackup.export(requireContext())
        val input = android.widget.EditText(requireContext()).apply {
            setText(json)
            setTextSize(12f)
            isSingleLine = false
            minLines = 8
        }
        android.app.AlertDialog.Builder(requireContext())
            .setTitle("备份配置 (节点 + 规则 + DNS + 设置)")
            .setView(input)
            .setPositiveButton("复制") { _, _ ->
                val cm = requireContext().getSystemService(android.content.ClipboardManager::class.java)
                cm.setPrimaryClip(android.content.ClipData.newPlainText("mirage-config", json))
                Toast.makeText(requireContext(), "配置已复制到剪贴板", Toast.LENGTH_SHORT).show()
            }
            .setNegativeButton("关闭", null)
            .show()
    }

    /** 恢复配置: 粘贴 JSON 导入 (支持合并或覆盖)。 */
    private fun showRestoreDialog() {
        val input = android.widget.EditText(requireContext()).apply {
            hint = "粘贴备份的 JSON 文本"
            setTextSize(12f)
            isSingleLine = false
            minLines = 8
        }
        android.app.AlertDialog.Builder(requireContext())
            .setTitle("恢复配置")
            .setView(input)
            .setPositiveButton("合并导入 (去重)") { _, _ ->
                val json = input.text.toString().trim()
                if (json.isEmpty()) { Toast.makeText(requireContext(), "内容为空", Toast.LENGTH_SHORT).show(); return@setPositiveButton }
                runCatching {
                    val (nodes, rules) = com.mirage.android.core.ConfigBackup.import(requireContext(), json, overwrite = false)
                    com.mirage.android.data.repository.NodeRepository.getInstance(requireContext()).reload()
                    com.mirage.android.data.repository.RuleRepository.getInstance(requireContext()).reload()
                    Toast.makeText(requireContext(), "已合并导入 $nodes 个新节点, $rules 条新规则", Toast.LENGTH_LONG).show()
                }.onFailure { e ->
                    Toast.makeText(requireContext(), "导入失败: ${e.message}", Toast.LENGTH_LONG).show()
                }
            }
            .setNeutralButton("覆盖全部") { _, _ ->
                val json = input.text.toString().trim()
                if (json.isEmpty()) { Toast.makeText(requireContext(), "内容为空", Toast.LENGTH_SHORT).show(); return@setNeutralButton }
                runCatching {
                    val (nodes, rules) = com.mirage.android.core.ConfigBackup.import(requireContext(), json, overwrite = true)
                    com.mirage.android.data.repository.NodeRepository.getInstance(requireContext()).reload()
                    com.mirage.android.data.repository.RuleRepository.getInstance(requireContext()).reload()
                    Toast.makeText(requireContext(), "已覆盖导入 $nodes 个节点, $rules 条规则", Toast.LENGTH_LONG).show()
                }.onFailure { e ->
                    Toast.makeText(requireContext(), "覆盖导入失败: ${e.message}", Toast.LENGTH_LONG).show()
                }
            }
            .setNegativeButton("取消", null)
            .show()
    }

    private fun fmtBytes(b: Double): String = when {
        b >= 1 shl 30 -> "%.2fG".format(b / (1 shl 30))
        b >= 1 shl 20 -> "%.1fM".format(b / (1 shl 20))
        b >= 1 shl 10 -> "%.1fK".format(b / (1 shl 10))
        else -> "%.0fB".format(b)
    }

    override fun onDestroyView() {
        super.onDestroyView()
        _binding = null
    }
}
