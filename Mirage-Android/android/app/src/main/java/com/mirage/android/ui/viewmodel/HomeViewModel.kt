package com.mirage.android.ui.viewmodel

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.mirage.android.data.model.Node
import com.mirage.android.data.model.TrafficStats
import com.mirage.android.data.model.VpnState
import com.mirage.android.data.repository.NodeRepository
import com.mirage.android.data.repository.VpnRepository
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch

class HomeViewModel(application: Application) : AndroidViewModel(application) {

    private val nodeRepo = NodeRepository.getInstance(application)
    private val vpnRepo = VpnRepository.getInstance(application)

    val vpnState: StateFlow<VpnState> = vpnRepo.vpnState
    val trafficStats: StateFlow<TrafficStats> = vpnRepo.trafficStats
    val latencyMs: StateFlow<Long> = vpnRepo.latencyMs

    val selectedNode: StateFlow<Node?> = combine(nodeRepo.nodes, nodeRepo.selectedIndex) { nodes, index ->
        if (index in nodes.indices) nodes[index] else null
    }.stateIn(viewModelScope, SharingStarted.Eagerly, nodeRepo.getSelectedNode())

    fun toggleConnection(onRequirePermission: () -> Unit, onProceedConnect: () -> Unit) {
        if (vpnState.value.isRunning) {
            disconnect()
        } else {
            val nodes = nodeRepo.nodes.value
            if (nodes.isEmpty()) {
                return
            }
            if (nodeRepo.isAutoSelect.value && nodes.size > 1) {
                viewModelScope.launch {
                    val best = nodeRepo.testAllNodes()
                    if (best != null) {
                        nodeRepo.setSelected(best)
                    }
                    onProceedConnect()
                }
            } else {
                onProceedConnect()
            }
        }
    }

    fun startVpn() {
        vpnRepo.startVpn()
    }

    fun disconnect() {
        vpnRepo.stopVpn()
    }

    fun checkCurrentState() {
        vpnRepo.checkCurrentState()
    }
}
