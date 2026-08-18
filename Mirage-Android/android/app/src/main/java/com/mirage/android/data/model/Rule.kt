package com.mirage.android.data.model

/**
 * 分流规则模型。
 */
data class Rule(
    val type: String = "domain", // "domain" | "cidr"
    val kind: String = "suffix", // "suffix" | "exact" | "keyword" | "regex" | "cidr"
    val pattern: String,
    val action: String = "direct", // "direct" | "proxy"
    val hits: Long = 0,           // 命中次数 (运行时统计)
) {
    val isDirect: Boolean get() = action == "direct"

    val kindDisplayName: String get() = when (kind) {
        "exact" -> "DOMAIN 精确"
        "keyword" -> "DOMAIN-KEYWORD"
        "regex" -> "DOMAIN-REGEX"
        "cidr" -> "IP-CIDR"
        else -> "DOMAIN-SUFFIX"
    }

    val actionDisplayName: String get() = if (isDirect) "直连" else "代理"
}
