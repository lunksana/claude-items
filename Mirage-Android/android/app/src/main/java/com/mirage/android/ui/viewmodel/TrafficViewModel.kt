package com.mirage.android.ui.viewmodel

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.mirage.android.data.model.LogLevel
import com.mirage.android.data.model.TrafficStats
import com.mirage.android.data.repository.VpnRepository
import kotlinx.coroutines.flow.*
import kotlinx.coroutines.launch

class TrafficViewModel(application: Application) : AndroidViewModel(application) {

    private val vpnRepo = VpnRepository.getInstance(application)

    val stats: StateFlow<TrafficStats> = vpnRepo.trafficStats
    val latencyMs: StateFlow<Long> = vpnRepo.latencyMs
    val rawLogs: StateFlow<List<String>> = vpnRepo.logs

    private val _selectedLogLevel = MutableStateFlow(LogLevel.ALL)
    val selectedLogLevel: StateFlow<LogLevel> = _selectedLogLevel.asStateFlow()

    val filteredLogs: StateFlow<List<String>> = combine(rawLogs, selectedLogLevel) { logs, level ->
        if (level == LogLevel.ALL) logs else logs.filter { level.matches(it) }
    }.stateIn(viewModelScope, SharingStarted.Eagerly, emptyList())

    private val _historyUp = MutableStateFlow<List<Float>>(emptyList())
    val historyUp: StateFlow<List<Float>> = _historyUp.asStateFlow()

    private val _historyDown = MutableStateFlow<List<Float>>(emptyList())
    val historyDown: StateFlow<List<Float>> = _historyDown.asStateFlow()

    private val maxHistoryPoints = 60
    private val upList = ArrayDeque<Float>()
    private val downList = ArrayDeque<Float>()

    init {
        viewModelScope.launch {
            stats.collect { s ->
                upList.addLast(s.upRate.toFloat())
                downList.addLast(s.downRate.toFloat())
                while (upList.size > maxHistoryPoints) upList.removeFirst()
                while (downList.size > maxHistoryPoints) downList.removeFirst()
                _historyUp.value = upList.toList()
                _historyDown.value = downList.toList()
            }
        }
    }

    fun setLogLevel(level: LogLevel) {
        _selectedLogLevel.value = level
        vpnRepo.setLogLevel(level)
    }

    fun clearLogs() {
        vpnRepo.clearLogs()
    }
}
