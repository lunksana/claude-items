package com.mirage.android.data.repository

import android.content.Context
import android.content.Intent
import com.mirage.android.CoreService
import com.mirage.android.core.CoreController
import com.mirage.android.core.ICoreCallback
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
    private val dnsRepo = DnsRepository.getInstance(context)

    private val _vpnState = MutableStateFlow<VpnState>(VpnState.Disconnected)
    val vpnState: StateFlow<VpnState> = _vpnState.asStateFlow()

    private val _trafficStats = MutableStateFlow(TrafficStats())
    val trafficStats: StateFlow<TrafficStats> = _trafficStats.asStateFlow()

    private val _latencyMs = MutableStateFlow(-1L)
    val latencyMs: StateFlow<Long> = _latencyMs.asStateFlow()

    private val _logs = MutableStateFlow<List<String>>(emptyList())
    val logs: StateFlow<List<String>> = _logs.asStateFlow()

    private val _connections = MutableStateFlow<List<com.mirage.android.data.model.ConnectionInfo>>(emptyList())
    val connections: StateFlow<List<com.mirage.android.data.model.ConnectionInfo>> = _connections.asStateFlow()

    private val prefs = context.getSharedPreferences("mirage_vpn_prefs", Context.MODE_PRIVATE)
    private val _isBlockQuic = MutableStateFlow(prefs.getBoolean("block_quic", true))
    val isBlockQuic: StateFlow<Boolean> = _isBlockQuic.asStateFlow()

    private val scope = CoroutineScope(Dispatchers.Main + SupervisorJob())
    private var telemetryJob: Job? = null

    private val callback = object : ICoreCallback.Stub() {
        override fun onStateChanged(running: Boolean) {
            scope.launch {
                if (running) {
                    dnsRepo.applyDns()
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
                val current = _logs.value.toMutableList()
                current.add(line)
                _logs.value = current.takeLast(150)
            }
        }
    }

    init {
        CoreController.bind(context)
        CoreController.registerCallback(callback)
        startTelemetry()
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
        // 注入规则与 DNS 配置
        ruleRepo.applyRules()
        dnsRepo.applyDns()
        // 启动前清理残留 Fake-IP 与 DNS 缓存
        runCatching { CoreController.clearDnsCache() }

        val intent = Intent(context, CoreService::class.java).apply {
            putExtra("uri", selected.uri)
            putExtra("pool_size", nodeRepo.getPoolSize())
        }
        context.startForegroundService(intent)
        startTelemetry()
    }

    fun stopVpn() {
        _vpnState.value = VpnState.Stopping
        runCatching { CoreController.clearDnsCache() }
        runCatching { CoreController.stop() }
        val stopIntent = Intent(context, CoreService::class.java).setAction(CoreService.ACTION_STOP)
        runCatching { context.startService(stopIntent) }
        runCatching { context.stopService(stopIntent) }
        _vpnState.value = VpnState.Disconnected
        _connections.value = emptyList()
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
        _logs.value = emptyList()
    }

    fun setLogLevel(level: com.mirage.android.data.model.LogLevel): Boolean {
        val levelStr = when (level) {
            com.mirage.android.data.model.LogLevel.ALL, com.mirage.android.data.model.LogLevel.DEBUG -> "debug"
            com.mirage.android.data.model.LogLevel.TRACE -> "trace"
            com.mirage.android.data.model.LogLevel.INFO -> "info"
            com.mirage.android.data.model.LogLevel.WARN -> "warn"
            com.mirage.android.data.model.LogLevel.ERROR -> "error"
        }
        return CoreController.setLogLevel(levelStr)
    }

    fun setBlockQuic(block: Boolean): Boolean {
        _isBlockQuic.value = block
        prefs.edit().putBoolean("block_quic", block).apply()
        return CoreController.setBlockQuic(block)
    }

    private fun startTelemetry() {
        telemetryJob?.cancel()
        telemetryJob = scope.launch(Dispatchers.IO) {
            while (isActive) {
                val isRunning = CoreController.isRunning()
                if (isRunning) {
                    if (_vpnState.value !is VpnState.Connected) {
                        withContext(Dispatchers.Main) {
                            _vpnState.value = VpnState.Connected(nodeRepo.getSelectedNode())
                        }
                    }
                    val statsArr = CoreController.getStats()
                    if (statsArr != null && statsArr.size >= 7) {
                        _trafficStats.value = TrafficStats.fromArray(statsArr)
                    }
                    val lat = CoreController.latencyMs()
                    _latencyMs.value = lat

                    val remoteLogs = CoreController.recentLogs().toList()
                    if (remoteLogs.isNotEmpty()) {
                        _logs.value = remoteLogs.takeLast(150)
                    }

                    val connJson = CoreController.getConnectionsJson()
                    if (connJson.isNotBlank() && connJson != "[]") {
                        val parsedList = mutableListOf<com.mirage.android.data.model.ConnectionInfo>()
                        runCatching {
                            val arr = org.json.JSONArray(connJson)
                            for (i in 0 until arr.length()) {
                                parsedList.add(com.mirage.android.data.model.ConnectionInfo.fromJson(arr.getJSONObject(i)))
                            }
                        }
                        _connections.value = parsedList
                    } else {
                        _connections.value = emptyList()
                    }
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
