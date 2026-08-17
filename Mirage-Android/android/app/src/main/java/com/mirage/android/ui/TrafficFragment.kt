package com.mirage.android.ui

import android.os.Bundle
import android.view.LayoutInflater
import android.view.View
import android.view.ViewGroup
import androidx.fragment.app.Fragment
import androidx.fragment.app.viewModels
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.lifecycleScope
import androidx.lifecycle.repeatOnLifecycle
import com.mirage.android.databinding.FragmentTrafficBinding
import com.mirage.android.ui.viewmodel.TrafficViewModel
import kotlinx.coroutines.launch

/**
 * 流量监测 Tab: 实时平滑双线曲线 + 连接度量 + 实时运行日志。
 */
class TrafficFragment : Fragment() {

    private var _binding: FragmentTrafficBinding? = null
    private val binding get() = _binding!!

    private val viewModel: TrafficViewModel by viewModels()

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

        binding.btnClearLogs.setOnClickListener {
            viewModel.clearLogs()
        }

        observeState()
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
                    viewModel.logs.collect { logLines ->
                        binding.trafficLogView.text = if (logLines.isNotEmpty()) {
                            logLines.joinToString("\n")
                        } else {
                            "暂无日志记录"
                        }
                        // 自动滚动到底部
                        binding.scrollLogs.post {
                            binding.scrollLogs.fullScroll(View.FOCUS_DOWN)
                        }
                    }
                }
            }
        }
    }

    override fun onDestroyView() {
        super.onDestroyView()
        _binding = null
    }
}
