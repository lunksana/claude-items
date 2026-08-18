package com.mirage.android.core

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject

/**
 * 多节点存储 (SharedPreferences + JSON)。
 * 支持: 添加 / 编辑 / 删除 / 选择当前节点。
 */
object NodeStore {

    data class Node(
        val uri: String,      // mirage:// 完整串
        val name: String,     // 显示名 (默认取 host:port)
    ) {
        /** 密码 (mirage://密码@host:port?sni=...) 含百分号解码。 */
        val password: String get() {
            val raw = uri.substringAfter("mirage://").substringBeforeLast("@")
            return percentDecode(raw)
        }
        val server: String get() {
            val hp = uri.substringAfter("mirage://").substringAfterLast("@").substringBefore("?")
            return hp.substringBeforeLast(":").removePrefix("[").removeSuffix("]")
        }
        val port: String get() {
            val hp = uri.substringAfter("mirage://").substringAfterLast("@").substringBefore("?")
            return hp.substringAfterLast(":").trimEnd('/')
        }
        /** SNI (伪装域名)。 */
        val sni: String get() {
            val q = uri.substringAfter("?", "")
            return q.split("&").firstOrNull { it.startsWith("sni=") }
                ?.substringAfter("=")?.let { percentDecode(it) } ?: ""
        }
        val displayName: String get() = if (name.isNotBlank()) name else "$server:$port"

        companion object {
            /** 百分号解码 (URI 里密码/SNI 的转义)。 */
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
                        sb.append(c); i++
                    }
                }
                return sb.toString()
            }

            /** 百分号编码 (密码/SNI 特殊字符)。 */
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

            /** 从拆分字段构造 URI。 */
            fun uriOf(server: String, port: String, password: String, sni: String): String {
                val host = if (server.contains(":")) "[$server]" else server
                val p = port.trim().ifEmpty { "443" }
                return "mirage://${percentEncode(password)}@$host:$p?sni=${percentEncode(sni)}"
            }
        }
    }

    private const val PREFS = "mirage_nodes"
    private const val KEY_NODES = "nodes_json"
    private const val KEY_SELECTED = "selected_index"
    private const val KEY_POOL = "pool_size"

    /** 迁移旧版单节点数据 (node_uri → 节点列表)。幂等。 */
    fun migrateLegacy(ctx: Context) {
        val prefs = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val legacy = prefs.getString("node_uri", null)
        if (!legacy.isNullOrBlank() && getNodes(ctx).isEmpty()) {
            val idx = addNode(ctx, Node(legacy, defaultName(legacy)))
            setSelected(ctx, idx)
            prefs.edit().remove("node_uri").apply()
        }
    }

    fun getNodes(ctx: Context): List<Node> {
        val raw = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getString(KEY_NODES, "[]") ?: "[]"
        return runCatching {
            val arr = JSONArray(raw)
            (0 until arr.length()).map { i ->
                val o = arr.getJSONObject(i)
                Node(o.getString("uri"), o.optString("name", ""))
            }
        }.getOrDefault(emptyList())
    }

    fun addNode(ctx: Context, node: Node): Int {
        val list = getNodes(ctx).toMutableList()
        list.add(node)
        saveNodes(ctx, list)
        return list.size - 1
    }

    fun updateNode(ctx: Context, index: Int, node: Node) {
        val list = getNodes(ctx).toMutableList()
        if (index in list.indices) {
            list[index] = node
            saveNodes(ctx, list)
        }
    }

    fun removeNode(ctx: Context, index: Int) {
        val list = getNodes(ctx).toMutableList()
        if (index in list.indices) {
            list.removeAt(index)
            saveNodes(ctx, list)
            val sel = getSelected(ctx)
            if (sel >= list.size) setSelected(ctx, (list.size - 1).coerceAtLeast(0))
        }
    }

    fun saveNodes(ctx: Context, list: List<Node>) {
        val arr = JSONArray()
        for (n in list) {
            arr.put(JSONObject().put("uri", n.uri).put("name", n.name))
        }
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit().putString(KEY_NODES, arr.toString()).apply()
    }

    fun getSelected(ctx: Context): Int =
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getInt(KEY_SELECTED, -1)

    fun setSelected(ctx: Context, index: Int) {
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit().putInt(KEY_SELECTED, index).apply()
    }

    /** 当前选中节点的 URI (无节点返回空串)。 */
    fun getSelectedUri(ctx: Context): String {
        val idx = getSelected(ctx)
        val nodes = getNodes(ctx)
        return if (idx in nodes.indices) nodes[idx].uri else ""
    }

    // ── 自动节点选择 ────────────────────────────────────────────────────
    private const val KEY_AUTO_SELECT = "auto_select"
    private const val KEY_TEST_METHOD = "test_method"  // "tcp" | "ping" | "connect"

    fun isAutoSelect(ctx: Context): Boolean =
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getBoolean(KEY_AUTO_SELECT, false)

    fun setAutoSelect(ctx: Context, enabled: Boolean) {
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putBoolean(KEY_AUTO_SELECT, enabled).apply()
    }

    fun getTestMethod(ctx: Context): String =
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(KEY_TEST_METHOD, "tcp") ?: "tcp"

    fun setTestMethod(ctx: Context, method: String) {
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putString(KEY_TEST_METHOD, method).apply()
    }

    fun getPoolSize(ctx: Context): Int =
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getInt(KEY_POOL, 4)

    fun setPoolSize(ctx: Context, size: Int) {
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putInt(KEY_POOL, size).apply()
    }

    /** 从 mirage:// 串生成显示名。 */
    fun defaultName(uri: String): String {
        val core = uri.substringAfter("mirage://").substringAfter("@").substringBefore("?")
        return core.substringBefore(":").removePrefix("[").removeSuffix("]")
    }
}
