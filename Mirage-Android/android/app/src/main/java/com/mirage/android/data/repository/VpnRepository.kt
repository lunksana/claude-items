package com.mirage.android.data.repository

import android.content.Context
import android.content.Intent
import com.mirage.android.CoreService
import com.mirage.android.core.CoreController
import com.mirage.android.core.ICoreCallback
import com.mirage.android.core.LogStore
import com.mirage.android.data.model.TrafficStats
import com.mirage.android.data.model.VpnState
import kotlinx.coroutines.*
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

/**
 * VPN 核心连接状态与遥测数据仓库。
 */
class VpnRepository(private val context: Context) {

    private val nodeRepo = NodeRepository.getInstance(context)
    private val ruleRepo = RuleRepository.getInstance(context)

    private val _vpnState = MutableStateFlow<VpnState>(VpnState.Disconnected)
    val vpnState: StateFlow<VpnState> = _vpnState.asStateFlow()

    private val _trafficStats = MutableStateFlow(TrafficStats())
    val trafficStats: StateFlow<TrafficStats> = _trafficStats.asStateFlow()

    private val _latencyMs = MutableStateFlow(-1L)
    val latencyMs: StateFlow<Long> = _latencyMs.asStateFlow()

    private val _logs = MutableStateFlow<List<String>>(emptyList())
    val logs: StateFlow<List<String>> = _logs.asStateFlow()

    private val scope = CoroutineScope(Dispatchers.Main + SupervisorJob())
    private var telemetryJob: Job? = null

    private val callback = object : ICoreCallback.Stub() {
        override fun onStateChanged(running: Boolean) {
            scope.launch {
                if (running) {
                    _vpnState.value = VpnState.Connected(nodeRepo.getSelectedNode())
                    startTelemetry()
                } else {
                    _vpnState.value = VpnState.Disconnected
                    stopTelemetry()
                }
            }
        }

        override fun onLog(line: String?) {
            if (!line.isNullOrBlank()) {
                LogStore.append(line)
                _logs.value = LogStore.all()
            }
        }
    }

    init {
        CoreController.bind(context)
        CoreController.registerCallback(callback)
        checkCurrentState()
    }

    fun checkCurrentState() {
        val isRunning = CoreController.isRunning()
        if (isRunning) {
            _vpnState.value = VpnState.Connected(nodeRepo.getSelectedNode())
            startTelemetry()
        } else {
            _vpnState.value = VpnState.Disconnected
            stopTelemetry()
        }
    }

    fun startVpn() {
        val selected = nodeRepo.getSelectedNode()
        if (selected == null) {
            _vpnState.value = VpnState.Error("请先选择或添加节点")
            return
        }

        _vpnState.value = VpnState.Connecting
        // 注入规则
        ruleRepo.applyRules()

        val intent = Intent(context, CoreService::class.java)
        context.startForegroundService(intent)
        startTelemetry()
    }

    fun stopVpn() {
        _vpnState.value = VpnState.Stopping
        runCatching { CoreController.stop() }
        val stopIntent = Intent(context, CoreService::class.java).setAction(CoreService.ACTION_STOP)
        runCatching { context.startService(stopIntent) }
        runCatching { context.stopService(stopIntent) }
        _vpnState.value = VpnState.Disconnected
        stopTelemetry()
    }

    fun switchNode(uri: String): Boolean {
        val success = CoreController.setNode(uri)
        if (success && _vpnState.value is VpnState.Connected) {
            _vpnState.value = VpnState.Connected(nodeRepo.getSelectedNode())
        }
        return success
    }

    fun clearLogs() {
        LogStore.clear()
        _logs.value = emptyList()
    }

    private fun startTelemetry() {
        telemetryJob?.cancel()
        telemetryJob = scope.launch(Dispatchers.IO) {
            while (isActive) {
                if (CoreController.isRunning()) {
                    val statsArr = CoreController.getStats()
                    if (statsArr != null && statsArr.size >= 7) {
                        _trafficStats.value = TrafficStats.fromArray(statsArr)
                    }
                    val lat = CoreController.latencyMs()
                    _latencyMs.value = lat

                    val nativeLogs = CoreController.recentLogs()
                    val allLogs = LogStore.all() + nativeLogs.toList()
                    _logs.value = allLogs.takeLast(100)
                } else {
                    if (_vpnState.value !is VpnState.Disconnected && _vpnState.value !is VpnState.Connecting) {
                        withContext(Dispatchers.Main) {
                            _vpnState.value = VpnState.Disconnected
                        }
                    }
                }
                delay(1000)
            }
        }
    }

    private fun stopTelemetry() {
        telemetryJob?.cancel()
        telemetryJob = null
        _trafficStats.value = TrafficStats()
        _latencyMs.value = -1L
    }

    fun destroy() {
        stopTelemetry()
        CoreController.unregisterCallback(callback)
        CoreController.unbind(context)
        scope.cancel()
    }

    companion object {
        @Volatile
        private var instance: VpnRepository? = null

        fun getInstance(context: Context): VpnRepository {
            return instance ?: synchronized(this) {
                instance ?: VpnRepository(context.applicationContext).also { instance = it }
            }
        }
    }
}
