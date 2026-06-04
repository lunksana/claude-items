package com.pyrealiy.proxy.data

import org.json.JSONObject

data class Profile(
    val id: String = java.util.UUID.randomUUID().toString(),
    val name: String = "PyReality Server",
    val serverHost: String = "",
    val serverPort: Int = 443,
    val password: String = "",
    val sni: String = "www.apple.com",      // TLS 伪装域名
    val remoteDns: String = "8.8.8.8",      // 境外 DNS（经隧道）
    val localDns: String = "223.5.5.5",     // 国内 DNS（直连）
    val bypassCn: Boolean = true,           // 国内流量直连
) {
    fun toJson(): JSONObject = JSONObject().apply {
        put("id", id); put("name", name)
        put("serverHost", serverHost); put("serverPort", serverPort)
        put("password", password); put("sni", sni)
        put("remoteDns", remoteDns); put("localDns", localDns)
        put("bypassCn", bypassCn)
    }

    companion object {
        fun fromJson(j: JSONObject) = Profile(
            id         = j.optString("id", java.util.UUID.randomUUID().toString()),
            name       = j.optString("name", "PyReality Server"),
            serverHost = j.optString("serverHost", ""),
            serverPort = j.optInt("serverPort", 443),
            password   = j.optString("password", ""),
            sni        = j.optString("sni", "www.apple.com"),
            remoteDns  = j.optString("remoteDns", "8.8.8.8"),
            localDns   = j.optString("localDns", "223.5.5.5"),
            bypassCn   = j.optBoolean("bypassCn", true),
        )
    }
}
