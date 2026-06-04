package com.pyrealiy.proxy.data

import android.content.Context
import android.content.SharedPreferences
import org.json.JSONArray

class AppPrefs(ctx: Context) {
    private val prefs: SharedPreferences =
        ctx.getSharedPreferences("pyrealiy_prefs", Context.MODE_PRIVATE)

    var activeProfileId: String?
        get() = prefs.getString("active_profile_id", null)
        set(v) = prefs.edit().putString("active_profile_id", v).apply()

    fun loadProfiles(): MutableList<Profile> {
        val json = prefs.getString("profiles", "[]") ?: "[]"
        return try {
            val arr = JSONArray(json)
            (0 until arr.length()).map { Profile.fromJson(arr.getJSONObject(it)) }.toMutableList()
        } catch (_: Exception) { mutableListOf() }
    }

    fun saveProfiles(profiles: List<Profile>) {
        val arr = JSONArray(profiles.map { it.toJson() })
        prefs.edit().putString("profiles", arr.toString()).apply()
    }

    fun activeProfile(): Profile? {
        val id = activeProfileId ?: return null
        return loadProfiles().find { it.id == id }
    }

    fun upsertProfile(p: Profile) {
        val list = loadProfiles()
        val idx = list.indexOfFirst { it.id == p.id }
        if (idx >= 0) list[idx] = p else list.add(p)
        saveProfiles(list)
    }

    fun deleteProfile(id: String) {
        val list = loadProfiles().filter { it.id != id }
        saveProfiles(list)
        if (activeProfileId == id) activeProfileId = null
    }
}
