package com.mirage.android.core

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject

/**
 * 自定义分流规则存储。
 * 规则类型: domain (域名/子域) / cidr (IP段或裸IP)
 * 动作: direct (直连) / proxy (代理)
 * 优先级: 自定义规则 > 内置国内规则 > 默认(代理)
 */
object RuleStore {

    data class Rule(
        val type: String,      // "domain" | "cidr" (展示用)
        val kind: String,      // Clash 匹配方式: suffix/exact/keyword/regex/cidr
        val pattern: String,   // 域名 或 IP/CIDR
        val action: String,    // "direct" | "proxy"
    )

    private const val PREFS = "mirage_rules"
    private const val KEY_RULES = "rules_json"
    private const val KEY_DEFAULT = "default_action"  // "proxy" | "direct"

    fun getRules(ctx: Context): List<Rule> {
        val raw = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getString(KEY_RULES, "[]") ?: "[]"
        return runCatching {
            val arr = JSONArray(raw)
            (0 until arr.length()).map { i ->
                val o = arr.getJSONObject(i)
                Rule(o.getString("type"), o.optString("kind", o.getString("type")),
                    o.getString("pattern"), o.getString("action"))
            }
        }.getOrDefault(emptyList())
    }

    fun addRule(ctx: Context, rule: Rule) {
        val list = getRules(ctx).toMutableList()
        list.add(rule)
        saveRules(ctx, list)
    }

    fun updateRule(ctx: Context, index: Int, rule: Rule) {
        val list = getRules(ctx).toMutableList()
        if (index in list.indices) {
            list[index] = rule
            saveRules(ctx, list)
        }
    }

    /** 上下移动规则 (from → to)。 */
    fun moveRule(ctx: Context, from: Int, to: Int) {
        val list = getRules(ctx).toMutableList()
        if (from in list.indices && to in list.indices) {
            val r = list.removeAt(from)
            list.add(to, r)
            saveRules(ctx, list)
        }
    }

    fun removeRule(ctx: Context, index: Int) {
        val list = getRules(ctx).toMutableList()
        if (index in list.indices) {
            list.removeAt(index)
            saveRules(ctx, list)
        }
    }

    fun saveRules(ctx: Context, list: List<Rule>) {
        val arr = JSONArray()
        for (r in list) {
            arr.put(JSONObject().put("type", r.type).put("kind", r.kind).put("pattern", r.pattern).put("action", r.action))
        }
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit().putString(KEY_RULES, arr.toString()).apply()
    }

    fun getDefaultAction(ctx: Context): String =
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString(KEY_DEFAULT, "proxy") ?: "proxy"

    fun setDefaultAction(ctx: Context, action: String) {
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putString(KEY_DEFAULT, action).apply()
    }

    /** 生成传给 Rust setRules 的 JSON (新格式: rules 数组 + kind)。 */
    fun toJson(ctx: Context): String {
        val arr = JSONArray()
        for (r in getRules(ctx)) {
            val kind = when (r.kind) {
                "exact" -> "exact"; "keyword" -> "keyword"
                "regex" -> "regex"; "cidr" -> "cidr"
                else -> "suffix"
            }
            arr.put(JSONObject()
                .put("kind", kind)
                .put("pattern", r.pattern.trim().lowercase())
                .put("action", r.action))
        }
        return JSONObject().put("rules", arr).toString()
    }
}
