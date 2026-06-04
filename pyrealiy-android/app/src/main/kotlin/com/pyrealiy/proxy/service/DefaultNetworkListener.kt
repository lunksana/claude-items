package com.pyrealiy.proxy.service

import android.content.Context
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.net.NetworkRequest
import com.pyrealiy.proxy.core.Logger

/**
 * 跟踪宿主非-VPN 网络（WiFi / 蜂窝），用于喂给 VpnService.setUnderlyingNetworks()。
 *
 * 为什么必须做：仅调用 `protect(fd)` 不足以让 protected 流量走外网。
 * 在 Xiaomi / HyperOS 等带 per-network firewall 的设备上，没绑 underlying network
 * 的 protected 包会被静默丢弃——表现就是握手能通、之后数据不流动。
 *
 * 为什么不用 registerDefaultNetworkCallback：VPN 起来后会变成"系统默认网络"，
 * 回调会把我们自己的 VPN 报给我们，setUnderlyingNetworks(VPN) 形成路由环路，
 * 整条链路崩。必须用 NetworkRequest 显式过滤 NOT_VPN。
 */
object DefaultNetworkListener {

    private const val TAG = "NetListener"

    private var cm: ConnectivityManager? = null
    private var callback: ConnectivityManager.NetworkCallback? = null

    @Volatile
    var current: Network? = null
        private set

    fun start(context: Context, onChange: (Network?) -> Unit) {
        if (callback != null) return
        val mgr = context.getSystemService(ConnectivityManager::class.java) ?: return
        cm = mgr
        val cb = object : ConnectivityManager.NetworkCallback() {
            override fun onAvailable(network: Network) {
                if (current == network) return        // 同一张网络重复 onAvailable，无视
                current = network
                Logger.d(TAG, "underlying network available: $network")
                onChange(network)
            }
            override fun onLost(network: Network) {
                if (current == network) {
                    current = null
                    Logger.d(TAG, "underlying network lost: $network")
                    onChange(null)
                }
            }
        }
        // NOT_VPN 必须显式声明，否则回调会把我们自己的 VPN 当作"默认网络"报回来
        val req = NetworkRequest.Builder()
            .addCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET)
            .addCapability(NetworkCapabilities.NET_CAPABILITY_NOT_VPN)
            .build()
        runCatching { mgr.registerNetworkCallback(req, cb) }
            .onFailure { Logger.w(TAG, "registerNetworkCallback failed", it) }
        callback = cb
    }

    fun stop() {
        runCatching { callback?.let { cm?.unregisterNetworkCallback(it) } }
        callback = null
        cm = null
        current = null
    }
}
