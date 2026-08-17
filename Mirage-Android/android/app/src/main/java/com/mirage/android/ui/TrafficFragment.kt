package com.mirage.android.ui

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.graphics.Color
import android.os.Bundle
import android.text.Spannable
import android.text.SpannableStringBuilder
import android.text.style.ForegroundColorSpan
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import android.widget.Toast
import androidx.fragment.app.Fragment
import androidx.fragment.app.viewModels
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import androidx.recyclerview.widget.LinearLayoutManager
import com.mirage.android.R
import com.mirage.android.data.model.LogLevel
import com.mirage.android.databinding.FragmentTrafficBinding
import com.mirage.android.ui.adapter.ConnectionAdapter
import com.mirage.android.ui.viewmodel.TrafficViewModel
import kotlinx.coroutines.launch

/**
 * 流量监测 Tab: 实时平滑双线曲线 + 连接度量 + 实时连接信息监控 + 日志级别过滤控制台。
 */
class TrafficFragment : Fragment() {

    private var _binding: FragmentTrafficBinding? = null
    private val binding get() = _binding!!

    private val viewModel: TrafficViewModel by viewModels()
    private lateinit var connAdapter: ConnectionAdapter

    override fun onCreateView(
        inflater: LayoutInflater,
        container: ViewGroup?,
        savedInstanceState: Bundle?
    ): View {
        _binding = FragmentTrafficBinding.inflate(inflater, container, false)
        return binding.root
    }

    override fun onViewCreated(view: View, savedInstanceState: Bundle?) {
        super.onViewCreated(view, savedInstanceState)

        setupConnectionsRecycler()
        setupToggleMode()
        setupLogLevelChips()

        binding.btnClearLogs.setOnClickListener {
            viewModel.clearLogs()
            Toast.makeText(requireContext(), "日志已清空", Toast.LENGTH_SHORT).show()
        }

        binding.btnCopyLogs.setOnClickListener {
            if (binding.boxLogs.visibility == View.VISIBLE) {
                val logs = viewModel.filteredLogs.value.joinToString("\n")
                if (logs.isNotBlank()) {
                    val cm = requireContext().getSystemService(Context.CLIPBOARD_SERVICE) as? ClipboardManager
                    cm?.setPrimaryClip(ClipData.newPlainText("Mirage Logs", logs))
                    Toast.makeText(requireContext(), "已复制当前日志到剪贴板", Toast.LENGTH_SHORT).show()
                } else {
                    Toast.makeText(requireContext(), "当前没有可复制的日志", Toast.LENGTH_SHORT).show()
                }
            } else {
                val conns = viewModel.connections.value.joinToString("\n") {
                    "${it.protocol} | ${it.target} | ${it.outbound} | ${it.status} | ↑${it.upFormatted} ↓${it.downFormatted}"
                }
                if (conns.isNotBlank()) {
                    val cm = requireContext().getSystemService(Context.CLIPBOARD_SERVICE) as? ClipboardManager
                    cm?.setPrimaryClip(ClipData.newPlainText("Mirage Connections", conns))
                    Toast.makeText(requireContext(), "已复制连接信息到剪贴板", Toast.LENGTH_SHORT).show()
                } else {
                    Toast.makeText(requireContext(), "当前没有活跃连接信息", Toast.LENGTH_SHORT).show()
                }
            }
        }

        observeState()
    }

    private fun setupConnectionsRecycler() {
        connAdapter = ConnectionAdapter()
        binding.recyclerConnections.layoutManager = LinearLayoutManager(requireContext())
        binding.recyclerConnections.adapter = connAdapter
    }

    private fun setupToggleMode() {
        binding.toggleViewMode.addOnButtonCheckedListener { _, checkedId, isChecked ->
            if (!isChecked) return@addOnButtonCheckedListener
            if (checkedId == R.id.btnModeLogs) {
                binding.boxLogs.visibility = View.VISIBLE
                binding.boxConnections.visibility = View.GONE
            } else {
                binding.boxLogs.visibility = View.GONE
                binding.boxConnections.visibility = View.VISIBLE
            }
        }
    }

    private fun setupLogLevelChips() {
        binding.chipGroupLogLevel.setOnCheckedStateChangeListener { _, checkedIds ->
            val level = when (checkedIds.firstOrNull()) {
                R.id.chipLevelInfo -> LogLevel.INFO
                R.id.chipLevelWarn -> LogLevel.WARN
                R.id.chipLevelError -> LogLevel.ERROR
                R.id.chipLevelDebug -> LogLevel.DEBUG
                R.id.chipLevelTrace -> LogLevel.TRACE
                else -> LogLevel.ALL
            }
            viewModel.setLogLevel(level)
        }
    }

    private fun observeState() {
        viewLifecycleOwner.lifecycleScope.launch {
            viewLifecycleOwner.repeatOnLifecycle(Lifecycle.State.STARTED) {
                launch {
                    viewModel.stats.collect { stats ->
                        binding.upRate.text = stats.upRateFormatted
                        binding.downRate.text = stats.downRateFormatted
                        binding.totalUp.text = "累计上行: ${stats.upTotalFormatted}"
                        binding.totalDown.text = "累计下行: ${stats.downTotalFormatted}"
                        binding.conns.text = "活跃连接: TCP ${stats.tcpConns} · UDP ${stats.udpFlows}"
                        binding.dnsCount.text = "DNS 查询: ${stats.dnsQueries}"
                    }
                }
                launch {
                    viewModel.historyUp.collect { upList ->
                        binding.chart.setData(upList, viewModel.historyDown.value)
                    }
                }
                launch {
                    viewModel.historyDown.collect { downList ->
                        binding.chart.setData(viewModel.historyUp.value, downList)
                    }
                }
                launch {
                    viewModel.connections.collect { conns ->
                        connAdapter.submitList(conns)
                        binding.btnModeConns.text = if (conns.isNotEmpty()) "连接信息 (${conns.size})" else "连接信息"
                        binding.tvEmptyConnections.visibility = if (conns.isEmpty()) View.VISIBLE else View.GONE
                    }
                }
                launch {
                    viewModel.filteredLogs.collect { logLines ->
                        renderColoredLogs(logLines)
                        // 自动滚动到底部
                        binding.scrollLogs.post {
                            binding.scrollLogs.fullScroll(View.FOCUS_DOWN)
                        }
                    }
                }
            }
        }
    }

    private fun renderColoredLogs(logLines: List<String>) {
        if (logLines.isEmpty()) {
            binding.trafficLogView.text = "暂无匹配的日志记录"
            return
        }

        val builder = SpannableStringBuilder()
        for ((idx, line) in logLines.withIndex()) {
            val start = builder.length
            builder.append(line)
            val end = builder.length

            val upper = line.uppercase()
            val color = when {
                upper.contains("ERROR") || upper.contains("FATAL") || upper.contains("EXCEPTION") || upper.contains("FAILED") ->
                    Color.parseColor("#FF6B6B") // 亮红
                upper.contains("WARN") || upper.contains("WARNING") ->
                    Color.parseColor("#FFD166") // 琥珀黄
                upper.contains("OK=TRUE") || upper.contains("已启动") || upper.contains("成功") ->
                    Color.parseColor("#06D6A0") // 翡翠绿
                upper.contains("[CORE]") || upper.contains("[LOADER]") || upper.contains("[APP]") ->
                    Color.parseColor("#4CC9F0") // 天空蓝
                else ->
                    Color.parseColor("#CBD5E1") // 浅灰
            }

            builder.setSpan(ForegroundColorSpan(color), start, end, Spannable.SPAN_EXCLUSIVE_EXCLUSIVE)
            if (idx < logLines.size - 1) {
                builder.append("\n")
            }
        }
        binding.trafficLogView.text = builder
    }

    override fun onDestroyView() {
        super.onDestroyView()
        _binding = null
    }
}
