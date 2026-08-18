package com.mirage.android.data.repository

import android.content.Context
import com.mirage.android.core.CoreController
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow

data class DnsPreset(val name: String, val ip: String)

/**
 * DNS 配置仓库 (国内直连 DNS 与国外远程 DNS).
 */
class DnsRepository private constructor(context: Context) {

    private val prefs = context.applicationContext.getSharedPreferences("mirage_dns_prefs", Context.MODE_PRIVATE)

    private val _directDns = MutableStateFlow(prefs.getString(KEY_DIRECT_DNS, DEFAULT_DIRECT_DNS) ?: DEFAULT_DIRECT_DNS)
    val directDns: StateFlow<String> = _directDns.asStateFlow()

    private val _remoteDns = MutableStateFlow(prefs.getString(KEY_REMOTE_DNS, DEFAULT_REMOTE_DNS) ?: DEFAULT_REMOTE_DNS)
    val remoteDns: StateFlow<String> = _remoteDns.asStateFlow()

    companion object {
        const val KEY_DIRECT_DNS = "direct_dns"
        const val KEY_REMOTE_DNS = "remote_dns"

        const val DEFAULT_DIRECT_DNS = "223.5.5.5"
        const val DEFAULT_REMOTE_DNS = "1.1.1.1"

        val DOMESTIC_PRESETS = listOf(
            DnsPreset("阿里 DNS", "223.5.5.5"),
            DnsPreset("腾讯 DNSPod", "119.29.29.29"),
            DnsPreset("114 DNS", "114.114.114.114"),
            DnsPreset("百度 DNS", "180.76.76.76"),
            DnsPreset("火山 DNS", "180.184.1.1")
        )

        val FOREIGN_PRESETS = listOf(
            DnsPreset("Cloudflare", "1.1.1.1"),
            DnsPreset("Google", "8.8.8.8"),
            DnsPreset("Quad9", "9.9.9.9"),
            DnsPreset("OpenDNS", "208.67.222.222"),
            DnsPreset("AdGuard", "94.140.14.14")
        )

        @Volatile
        private var instance: DnsRepository? = null

        fun getInstance(context: Context): DnsRepository =
            instance ?: synchronized(this) {
                instance ?: DnsRepository(context).also { instance = it }
            }
    }

    fun getDirectDns(): String = _directDns.value
    fun getRemoteDns(): String = _remoteDns.value

    fun setDns(direct: String, remote: String): Boolean {
        val cleanDirect = direct.trim().ifEmpty { DEFAULT_DIRECT_DNS }
        val cleanRemote = remote.trim().ifEmpty { DEFAULT_REMOTE_DNS }

        prefs.edit()
            .putString(KEY_DIRECT_DNS, cleanDirect)
            .putString(KEY_REMOTE_DNS, cleanRemote)
            .apply()

        _directDns.value = cleanDirect
        _remoteDns.value = cleanRemote

        return applyDns()
    }

    fun applyDns(): Boolean {
        return CoreController.setDnsServers(_directDns.value, _remoteDns.value)
    }
}
