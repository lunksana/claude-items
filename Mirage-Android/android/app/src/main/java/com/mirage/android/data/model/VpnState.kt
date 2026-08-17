package com.mirage.android.data.model

/**
 * VPN 连接状态。
 */
sealed class VpnState {
    object Disconnected : VpnState()
    object Connecting : VpnState()
    data class Connected(val node: Node? = null) : VpnState()
    object Stopping : VpnState()
    data class Error(val message: String) : VpnState()

    val isRunning: Boolean get() = this is Connected || this is Connecting
}
