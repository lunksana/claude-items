package com.mirage.android.ui.viewmodel

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.mirage.android.data.model.Node
import com.mirage.android.data.repository.NodeRepository
import com.mirage.android.data.repository.VpnRepository
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.launch

class NodesViewModel(application: Application) : AndroidViewModel(application) {

    private val nodeRepo = NodeRepository.getInstance(application)
    private val vpnRepo = VpnRepository.getInstance(application)

    val nodes: StateFlow<List<Node>> = nodeRepo.nodes
    val selectedIndex: StateFlow<Int> = nodeRepo.selectedIndex
    val isTestingAll: StateFlow<Boolean> = nodeRepo.isTestingAll
    val testMethod: StateFlow<String> = nodeRepo.testMethod
    val isAutoSelect: StateFlow<Boolean> = nodeRepo.isAutoSelect
    val poolSize: StateFlow<Int> = nodeRepo.poolSize

    fun setPoolSize(size: Int) {
        nodeRepo.setPoolSize(size)
    }

    fun selectNode(index: Int) {
        nodeRepo.setSelected(index)
        val selected = nodeRepo.getSelectedNode()
        if (selected != null && vpnRepo.vpnState.value.isRunning) {
            // 热切换
            vpnRepo.switchNode(selected.uri)
        }
    }

    fun addNode(uri: String, name: String): Int {
        val node = Node(uri = uri, name = name.ifEmpty { Node.defaultName(uri) })
        return nodeRepo.addNode(node)
    }

    fun updateNode(index: Int, uri: String, name: String) {
        val node = Node(uri = uri, name = name.ifEmpty { Node.defaultName(uri) })
        nodeRepo.updateNode(index, node)
        if (index == selectedIndex.value && vpnRepo.vpnState.value.isRunning) {
            vpnRepo.switchNode(node.uri)
        }
    }

    fun deleteNode(index: Int) {
        nodeRepo.removeNode(index)
    }

    fun testNode(index: Int) {
        viewModelScope.launch {
            nodeRepo.testNode(index)
        }
    }

    fun testAllNodes(onBestSelected: ((Node, Long) -> Unit)? = null) {
        viewModelScope.launch {
            val bestIndex = nodeRepo.testAllNodes()
            if (bestIndex != null && bestIndex in nodes.value.indices) {
                nodeRepo.setSelected(bestIndex)
                val best = nodes.value[bestIndex]
                onBestSelected?.invoke(best, best.latencyMs ?: 0L)
            }
        }
    }

    fun toggleAutoSelect() {
        nodeRepo.setAutoSelect(!isAutoSelect.value)
    }

    fun setTestMethod(method: String) {
        nodeRepo.setTestMethod(method)
    }

    fun importFromClipboard(text: String): Int {
        val extracted = nodeRepo.extractNodesFromText(text)
        if (extracted.isNotEmpty()) {
            extracted.forEach { nodeRepo.addNode(it) }
        }
        return extracted.size
    }
}
