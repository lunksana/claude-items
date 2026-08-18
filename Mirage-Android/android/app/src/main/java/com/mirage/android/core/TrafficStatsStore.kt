package com.mirage.android.core

import android.content.Context
import org.json.JSONArray
import org.json.JSONObject

/**
 * 流量统计持久化: 按天/月聚合会话流量。
 * 数据源: CoreService 定期读内核 getStats 增量, 累加到当日/当月。
 */
object TrafficStatsStore {

    private const val PREFS = "mirage_traffic"
    private const val KEY_DAILY = "daily_json"       // {"YYYY-MM-DD": {"up":x,"down":x}}
    private const val KEY_MONTHLY = "monthly_json"   // {"YYYY-MM": {"up":x,"down":x}}

    private fun today(): String = java.text.SimpleDateFormat("yyyy-MM-dd", java.util.Locale.US).format(java.util.Date())
    private fun thisMonth(): String = java.text.SimpleDateFormat("yyyy-MM", java.util.Locale.US).format(java.util.Date())

    fun getToday(ctx: Context): Pair<Long, Long> = getDay(ctx, today())
    fun getThisMonth(ctx: Context): Pair<Long, Long> = getMonth(ctx, thisMonth())

    fun getDay(ctx: Context, day: String): Pair<Long, Long> {
        val prefs = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val json = prefs.getString(KEY_DAILY, "{}") ?: "{}"
        return runCatching {
            val o = JSONObject(json).optJSONObject(day)
            if (o != null) Pair(o.optLong("up", 0), o.optLong("down", 0)) else Pair(0L, 0L)
        }.getOrDefault(Pair(0L, 0L))
    }

    fun getMonth(ctx: Context, month: String): Pair<Long, Long> {
        val prefs = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val json = prefs.getString(KEY_MONTHLY, "{}") ?: "{}"
        return runCatching {
            val o = JSONObject(json).optJSONObject(month)
            if (o != null) Pair(o.optLong("up", 0), o.optLong("down", 0)) else Pair(0L, 0L)
        }.getOrDefault(Pair(0L, 0L))
    }

    /** 累加会话流量增量到当日/当月。 */
    fun add(ctx: Context, upDelta: Long, downDelta: Long) {
        if (upDelta <= 0 && downDelta <= 0) return
        val prefs = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val t = today(); val m = thisMonth()
        val daily = prefs.getString(KEY_DAILY, "{}")?.let { runCatching { JSONObject(it) }.getOrNull() } ?: JSONObject()
        val monthly = prefs.getString(KEY_MONTHLY, "{}")?.let { runCatching { JSONObject(it) }.getOrNull() } ?: JSONObject()

        fun addTo(map: JSONObject, key: String, up: Long, down: Long) {
            val o = map.optJSONObject(key) ?: JSONObject().put("up", 0).put("down", 0)
            o.put("up", o.optLong("up") + up)
            o.put("down", o.optLong("down") + down)
            map.put(key, o)
        }
        addTo(daily, t, upDelta, downDelta)
        addTo(monthly, m, upDelta, downDelta)

        prefs.edit()
            .putString(KEY_DAILY, daily.toString())
            .putString(KEY_MONTHLY, monthly.toString())
            .apply()
    }

    /** 清理超过 N 天的旧数据。 */
    fun prune(ctx: Context, keepDays: Int = 30) {
        val prefs = ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
        val daily = prefs.getString(KEY_DAILY, "{}")?.let { runCatching { JSONObject(it) }.getOrNull() } ?: return
        val cutoff = System.currentTimeMillis() - keepDays * 86400000L
        val df = java.text.SimpleDateFormat("yyyy-MM-dd", java.util.Locale.US)
        val keys = daily.keys().asSequence().toList()
        var changed = false
        for (k in keys) {
            runCatching {
                if (df.parse(k)?.time ?: 0 < cutoff) { daily.remove(k); changed = true }
            }
        }
        if (changed) prefs.edit().putString(KEY_DAILY, daily.toString()).apply()
    }
}

/**
 * 应用设置 (自动重连 / failover 等)。
 */
object SettingsStore {
    private const val PREFS = "mirage_settings"

    fun isAutoReconnect(ctx: Context): Boolean =
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getBoolean("auto_reconnect", true)

    fun setAutoReconnect(ctx: Context, v: Boolean) {
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putBoolean("auto_reconnect", v).apply()
    }

    /** failover 方式: "best"(测活选最优) | "next"(顺序换下一个) */
    fun getFailoverMode(ctx: Context): String =
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getString("failover_mode", "best") ?: "best"

    fun setFailoverMode(ctx: Context, v: String) {
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putString("failover_mode", v).apply()
    }

    /** 重连检查间隔 (秒)。 */
    fun getCheckIntervalSec(ctx: Context): Int =
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).getInt("check_interval", 15)

    fun setCheckIntervalSec(ctx: Context, v: Int) {
        ctx.getSharedPreferences(PREFS, Context.MODE_PRIVATE).edit().putInt("check_interval", v).apply()
    }
}
