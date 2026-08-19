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
        // DNS 配置
        val dnsPrefs = ctx.getSharedPreferences("mirage_dns_prefs", Context.MODE_PRIVATE)
        root.put("direct_dns", dnsPrefs.getString("direct_dns", "223.5.5.5") ?: "223.5.5.5")
        root.put("remote_dns", dnsPrefs.getString("remote_dns", "1.1.1.1") ?: "1.1.1.1")
        // Geo 配置
        root.put("geosite_url", GeoManager.getGeositeUrl(ctx))
        root.put("geoip_url", GeoManager.getGeoipUrl(ctx))
        // QUIC 设置
        val vpnPrefs = ctx.getSharedPreferences("mirage_vpn_prefs", Context.MODE_PRIVATE)
        root.put("block_quic", vpnPrefs.getBoolean("block_quic", true))
        // 设置
        root.put("auto_reconnect", SettingsStore.isAutoReconnect(ctx))
        root.put("failover_mode", SettingsStore.getFailoverMode(ctx))
        root.put("test_method", NodeStore.getTestMethod(ctx))
        root.put("pool_size", NodeStore.getPoolSize(ctx))
        root.put("app", "mirage-android")
        root.put("version", 2)
        return root.toString(2)
    }

    /** 导入配置。返回 (节点数, 规则数)。overwrite 为 true 则清空旧配置，为 false 则合并去重。 */
    fun import(ctx: Context, json: String, overwrite: Boolean = false): Pair<Int, Int> {
        val root = JSONObject(json)
        // 节点
        var nodeCount = 0
        if (overwrite) {
            NodeStore.saveNodes(ctx, emptyList())
        }
        val existingNodes = NodeStore.getNodes(ctx).toMutableList()
        root.optJSONArray("nodes")?.let { arr ->
            for (i in 0 until arr.length()) {
                val o = arr.optJSONObject(i) ?: continue
                val uri = o.optString("uri", "")
                if (uri.startsWith("mirage://")) {
                    if (!overwrite && existingNodes.any { it.uri == uri }) {
                        continue // 合并模式下去重
                    }
                    val node = NodeStore.Node(uri, o.optString("name", NodeStore.defaultName(uri)))
                    existingNodes.add(node)
                    NodeStore.addNode(ctx, node)
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
        if (overwrite) {
            RuleStore.saveRules(ctx, emptyList())
        }
        val existingRules = RuleStore.getRules(ctx).toMutableList()
        root.optJSONArray("rules")?.let { arr ->
            for (i in 0 until arr.length()) {
                val o = arr.optJSONObject(i) ?: continue
                val pat = o.optString("pattern", "")
                val kind = o.optString("kind", "suffix")
                if (pat.isBlank()) continue
                if (!overwrite && existingRules.any { it.pattern == pat && it.kind == kind }) {
                    continue // 合并模式下去重
                }
                val rule = RuleStore.Rule(
                    o.optString("type", "domain"),
                    kind,
                    pat,
                    o.optString("action", "proxy")
                )
                existingRules.add(rule)
                RuleStore.addRule(ctx, rule)
                ruleCount++
            }
        }
        root.optString("default_action")?.let { if (it.isNotEmpty()) RuleStore.setDefaultAction(ctx, it) }
        // DNS
        val directDns = root.optString("direct_dns", "")
        val remoteDns = root.optString("remote_dns", "")
        if (directDns.isNotBlank() || remoteDns.isNotBlank()) {
            val dnsRepo = com.mirage.android.data.repository.DnsRepository.getInstance(ctx)
            dnsRepo.setDns(
                if (directDns.isNotBlank()) directDns else dnsRepo.getDirectDns(),
                if (remoteDns.isNotBlank()) remoteDns else dnsRepo.getRemoteDns()
            )
        }
        // Geo 配置
        if (root.has("geosite_url")) {
            val u = root.optString("geosite_url", "")
            if (u.isNotBlank()) GeoManager.setGeositeUrl(ctx, u)
        }
        if (root.has("geoip_url")) {
            val u = root.optString("geoip_url", "")
            if (u.isNotBlank()) GeoManager.setGeoipUrl(ctx, u)
        }
        // QUIC
        if (root.has("block_quic")) {
            val bq = root.optBoolean("block_quic", true)
            val vpnRepo = com.mirage.android.data.repository.VpnRepository.getInstance(ctx)
            vpnRepo.setBlockQuic(bq)
        }
        // 设置
        if (root.has("auto_reconnect")) SettingsStore.setAutoReconnect(ctx, root.optBoolean("auto_reconnect"))
        if (root.has("failover_mode")) SettingsStore.setFailoverMode(ctx, root.optString("failover_mode"))
        if (root.has("test_method")) NodeStore.setTestMethod(ctx, root.optString("test_method"))
        if (root.has("pool_size")) NodeStore.setPoolSize(ctx, root.optInt("pool_size", 4))
        return Pair(nodeCount, ruleCount)
    }
}
