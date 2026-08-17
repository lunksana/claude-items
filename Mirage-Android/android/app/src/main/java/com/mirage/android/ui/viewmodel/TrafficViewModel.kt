package com.mirage.android.ui.viewmodel

import android.app.Application
import androidx.lifecycle.AndroidViewModel
import androidx.lifecycle.viewModelScope
import com.mirage.android.data.model.TrafficStats
import com.mirage.android.data.repository.VpnRepository
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.launch

class TrafficViewModel(application: Application) : AndroidViewModel(application) {

    private val vpnRepo = VpnRepository.getInstance(application)

    val stats: StateFlow<TrafficStats> = vpnRepo.trafficStats
    val latencyMs: StateFlow<Long> = vpnRepo.latencyMs
    val logs: StateFlow<List<String>> = vpnRepo.logs

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

    fun clearLogs() {
        vpnRepo.clearLogs()
    }
}
