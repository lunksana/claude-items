package com.mirage.android.data.model

/**
 * 分流规则模型。
 * 支持 Domain (suffix/exact/keyword/regex), IP-CIDR, GeoSite (tag), GeoIP (code)
 * 动作: direct (直连) / proxy (代理) / block (拦截)
 */
data class Rule(
    val type: String = "domain", // "domain" | "cidr" | "geosite" | "geoip"
    val kind: String = "suffix", // "suffix" | "exact" | "keyword" | "regex" | "cidr" | "geosite" | "geoip"
    val pattern: String,
    val action: String = "direct", // "direct" | "proxy" | "block"
    val hits: Long = 0,           // 命中次数 (运行时统计)
) {
    val isDirect: Boolean get() = action == "direct"
    val isBlock: Boolean get() = action == "block" || action == "reject"

    val kindDisplayName: String get() = when (kind.lowercase()) {
        "geosite" -> "GEOSITE 规则集"
        "geoip" -> "GEOIP 国家/IP集"
        "exact" -> "DOMAIN 精确"
        "keyword" -> "DOMAIN-KEYWORD"
        "regex" -> "DOMAIN-REGEX"
        "cidr" -> "IP-CIDR"
        else -> "DOMAIN-SUFFIX"
    }

    val actionDisplayName: String get() = when (action.lowercase()) {
        "direct" -> "直连"
        "block", "reject" -> "拦截"
        else -> "代理"
    }
}
