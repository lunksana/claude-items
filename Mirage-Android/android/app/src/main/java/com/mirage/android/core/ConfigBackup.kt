package com.mirage.android.core

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject

/**
 * 配置导入/导出/备份: 节点 + 规则 + 设置 → JSON。
 */
object ConfigBackup {

    /** 导出全部配置为 JSON 字符串。 */
    fun export(ctx: Context): String {
        val root = JSONObject()
        // 节点
        val nodes = JSONArray()
        for (n in NodeStore.getNodes(ctx)) {
            nodes.put(JSONObject().put("uri", n.uri).put("name", n.name))
        }
        root.put("nodes", nodes)
        root.put("selected_index", NodeStore.getSelected(ctx))
        // 规则
        val rules = JSONArray()
        for (r in RuleStore.getRules(ctx)) {
            rules.put(JSONObject()
                .put("type", r.type).put("kind", r.kind)
                .put("pattern", r.pattern).put("action", r.action))
        }
        root.put("rules", rules)
        root.put("default_action", RuleStore.getDefaultAction(ctx))
        // 设置
        root.put("auto_reconnect", SettingsStore.isAutoReconnect(ctx))
        root.put("failover_mode", SettingsStore.getFailoverMode(ctx))
        root.put("test_method", NodeStore.getTestMethod(ctx))
        root.put("pool_size", NodeStore.getPoolSize(ctx))
        root.put("app", "mirage-android")
        root.put("version", 1)
        return root.toString(2)
    }

    /** 导入配置。返回 (节点数, 规则数)。 */
    fun import(ctx: Context, json: String): Pair<Int, Int> {
        val root = JSONObject(json)
        // 节点
        var nodeCount = 0
        root.optJSONArray("nodes")?.let { arr ->
            for (i in 0 until arr.length()) {
                val o = arr.optJSONObject(i) ?: continue
                val uri = o.optString("uri", "")
                if (uri.startsWith("mirage://")) {
                    NodeStore.addNode(ctx, NodeStore.Node(uri, o.optString("name", NodeStore.defaultName(uri))))
                    nodeCount++
                }
            }
        }
        if (root.has("selected_index")) {
            val idx = root.optInt("selected_index", -1)
            val size = NodeStore.getNodes(ctx).size
            if (idx in 0 until size) NodeStore.setSelected(ctx, idx)
        }
        // 规则
        var ruleCount = 0
        root.optJSONArray("rules")?.let { arr ->
            for (i in 0 until arr.length()) {
                val o = arr.optJSONObject(i) ?: continue
                RuleStore.addRule(ctx, RuleStore.Rule(
                    o.optString("type", "domain"),
                    o.optString("kind", "suffix"),
                    o.optString("pattern", ""),
                    o.optString("action", "proxy")))
                ruleCount++
            }
        }
        root.optString("default_action")?.let { RuleStore.setDefaultAction(ctx, it) }
        // 设置
        if (root.has("auto_reconnect")) SettingsStore.setAutoReconnect(ctx, root.optBoolean("auto_reconnect"))
        if (root.has("failover_mode")) SettingsStore.setFailoverMode(ctx, root.optString("failover_mode"))
        if (root.has("test_method")) NodeStore.setTestMethod(ctx, root.optString("test_method"))
        if (root.has("pool_size")) NodeStore.setPoolSize(ctx, root.optInt("pool_size", 4))
        return Pair(nodeCount, ruleCount)
    }
}
