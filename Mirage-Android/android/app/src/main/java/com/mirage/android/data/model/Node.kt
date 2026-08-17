package com.mirage.android.data.model

/**
 * Mirage 节点模型。
 */
data class Node(
    val uri: String,
    val name: String = "",
    val latencyMs: Long? = null,
    val isTesting: Boolean = false,
    val testError: String? = null,
) {
    val server: String get() {
        val hp = uri.substringAfter("mirage://").substringAfterLast("@").substringBefore("?")
        return hp.substringBeforeLast(":").removePrefix("[").removeSuffix("]")
    }

    val port: String get() {
        val hp = uri.substringAfter("mirage://").substringAfterLast("@").substringBefore("?")
        return hp.substringAfterLast(":").trimEnd('/')
    }

    val password: String get() {
        val raw = uri.substringAfter("mirage://").substringBeforeLast("@")
        return percentDecode(raw)
    }

    val sni: String get() {
        val q = uri.substringAfter("?", "")
        return q.split("&").firstOrNull { it.startsWith("sni=") }
            ?.substringAfter("=")?.let { percentDecode(it) } ?: ""
    }

    val displayName: String get() = if (name.isNotBlank()) name else "$server:$port"

    companion object {
        fun percentDecode(s: String): String {
            if (!s.contains('%')) return s
            val sb = StringBuilder()
            var i = 0
            while (i < s.length) {
                val c = s[i]
                if (c == '%' && i + 2 < s.length) {
                    val hex = s.substring(i + 1, i + 3)
                    sb.append(hex.toIntOrNull(16)?.toChar() ?: c)
                    i += 3
                } else {
                    sb.append(c)
                    i++
                }
            }
            return sb.toString()
        }

        fun percentEncode(s: String): String {
            val sb = StringBuilder()
            for (c in s) {
                when {
                    c.isLetterOrDigit() || c in "-_.~" -> sb.append(c)
                    else -> sb.append('%').append(String.format("%02X", c.code))
                }
            }
            return sb.toString()
        }

        fun uriOf(server: String, port: String, password: String, sni: String): String {
            val host = if (server.contains(":")) "[$server]" else server
            val p = port.trim().ifEmpty { "443" }
            val s = sni.trim().ifEmpty { "www.apple.com" }
            return "mirage://${percentEncode(password)}@$host:$p?sni=${percentEncode(s)}"
        }

        fun defaultName(uri: String): String {
            val core = uri.substringAfter("mirage://").substringAfter("@").substringBefore("?")
            return core.substringBefore(":").removePrefix("[").removeSuffix("]")
        }
    }
}
