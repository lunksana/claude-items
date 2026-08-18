package com.mirage.android.data.repository

import android.content.Context
import com.mirage.android.core.CoreController
import com.mirage.android.data.model.Rule
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import org.json.JSONArray
import org.json.JSONObject

/**
 * 路由规则仓库。
 */
class RuleRepository(private val context: Context) {

    private val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    private val _rules = MutableStateFlow<List<Rule>>(emptyList())
    val rules: StateFlow<List<Rule>> = _rules.asStateFlow()

    private val _defaultAction = MutableStateFlow("proxy")
    val defaultAction: StateFlow<String> = _defaultAction.asStateFlow()

    private val _builtinDomains = MutableStateFlow<List<String>>(emptyList())
    val builtinDomains: StateFlow<List<String>> = _builtinDomains.asStateFlow()

    private val _builtinIpCount = MutableStateFlow(0L)
    val builtinIpCount: StateFlow<Long> = _builtinIpCount.asStateFlow()

    init {
        loadData()
    }

    private fun loadData() {
        val raw = prefs.getString(KEY_RULES, "[]") ?: "[]"
        val list = runCatching {
            val arr = JSONArray(raw)
            (0 until arr.length()).map { i ->
                val o = arr.getJSONObject(i)
                Rule(
                    type = o.optString("type", "domain"),
                    kind = o.optString("kind", "suffix"),
                    pattern = o.getString("pattern"),
                    action = o.optString("action", "direct")
                )
            }
        }.getOrDefault(emptyList())

        _rules.value = list
        _defaultAction.value = prefs.getString(KEY_DEFAULT_ACTION, "proxy") ?: "proxy"
    }

    private fun saveRules(list: List<Rule>) {
        val arr = JSONArray()
        for (r in list) {
            arr.put(
                JSONObject()
                    .put("type", r.type)
                    .put("kind", r.kind)
                    .put("pattern", r.pattern)
                    .put("action", r.action)
            )
        }
        prefs.edit().putString(KEY_RULES, arr.toString()).apply()
    }

    /** 应用内核规则命中统计到规则列表 (不持久化, 运行时显示)。 */
    fun applyHits(hitsMap: Map<String, Long>) {
        _rules.value = _rules.value.map { r ->
            val key = "${r.kind}|${r.pattern}|${r.action}"
            r.copy(hits = hitsMap[key] ?: 0L)
        }
    }

    fun addRule(rule: Rule) {
        val current = _rules.value.toMutableList()
        current.add(rule)
        _rules.value = current
        saveRules(current)
    }

    fun updateRule(index: Int, rule: Rule) {
        val current = _rules.value.toMutableList()
        if (index in current.indices) {
            current[index] = rule
            _rules.value = current
            saveRules(current)
        }
    }

    fun removeRule(index: Int) {
        val current = _rules.value.toMutableList()
        if (index in current.indices) {
            current.removeAt(index)
            _rules.value = current
            saveRules(current)
        }
    }

    fun moveRule(from: Int, to: Int) {
        val current = _rules.value.toMutableList()
        if (from in current.indices && to in current.indices && from != to) {
            val item = current.removeAt(from)
            current.add(to, item)
            _rules.value = current
            saveRules(current)
        }
    }

    fun setDefaultAction(action: String) {
        _defaultAction.value = action
        prefs.edit().putString(KEY_DEFAULT_ACTION, action).apply()
    }

    fun toJson(): String {
        val arr = JSONArray()
        for (r in _rules.value) {
            arr.put(
                JSONObject()
                    .put("kind", r.kind)
                    .put("pattern", r.pattern.trim().lowercase())
                    .put("action", r.action)
            )
        }
        return JSONObject().put("rules", arr).toString()
    }

    fun refreshBuiltin() {
        runCatching {
            _builtinDomains.value = CoreController.getBuiltinDomains().toList()
            _builtinIpCount.value = CoreController.getBuiltinIpCount()
        }
    }

    fun applyRules(): Boolean {
        return CoreController.setRules(toJson())
    }

    companion object {
        private const val PREFS_NAME = "mirage_rules"
        private const val KEY_RULES = "rules_json"
        private const val KEY_DEFAULT_ACTION = "default_action"

        @Volatile
        private var instance: RuleRepository? = null

        fun getInstance(context: Context): RuleRepository {
            return instance ?: synchronized(this) {
                instance ?: RuleRepository(context.applicationContext).also { instance = it }
            }
        }
    }
}
