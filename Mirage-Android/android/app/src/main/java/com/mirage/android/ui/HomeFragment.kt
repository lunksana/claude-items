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

        binding.tvVersion.text = "v${com.mirage.android.BuildConfig.VERSION_NAME}"

        binding.connectSwitch.setOnCheckedChangeListener { _, isChecked ->
            if (isChecked && !viewModel.vpnState.value.isRunning) {
                performConnect()
            } else if (!isChecked && viewModel.vpnState.value.isRunning) {
                viewModel.disconnect()
            }
        }

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
                binding.statusText.text = "已连接"
                binding.statusText.setTextColor(ContextCompat.getColor(requireContext(), R.color.meow_connected))
                binding.connectBtn.text = getString(R.string.disconnect)
                binding.connectBtn.setBackgroundColor(Color.parseColor("#DC2626")) // 红
            }
            is VpnState.Connecting -> {
                binding.statusText.text = "连接中…"
                binding.statusText.setTextColor(ContextCompat.getColor(requireContext(), R.color.meow_upload))
                binding.connectBtn.text = "连接中…"
                binding.connectBtn.setBackgroundColor(ContextCompat.getColor(requireContext(), R.color.meow_upload))
            }
            is VpnState.Stopping -> {
                binding.statusText.text = "断开中…"
                binding.statusText.setTextColor(ContextCompat.getColor(requireContext(), R.color.meow_disconnected))
                binding.connectBtn.text = "断开中…"
            }
            is VpnState.Error -> {
                binding.statusText.text = "连接异常: ${state.message}"
                binding.statusText.setTextColor(Color.parseColor("#DC2626"))
                binding.connectBtn.text = getString(R.string.connect)
                binding.connectBtn.setBackgroundColor(ContextCompat.getColor(requireContext(), R.color.meow_blue))
            }
            is VpnState.Disconnected -> {
                binding.statusText.text = getString(R.string.status_idle)
                binding.statusText.setTextColor(ContextCompat.getColor(requireContext(), R.color.meow_disconnected))
                binding.connectBtn.text = getString(R.string.connect)
                binding.connectBtn.setBackgroundColor(ContextCompat.getColor(requireContext(), R.color.meow_blue))
            }
        }
    }

    override fun onDestroyView() {
        super.onDestroyView()
        _binding = null
    }
}
