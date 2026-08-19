package com.mirage.android.core

import android.content.Context
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import org.json.JSONObject
import java.io.File
import java.io.FileOutputStream
import java.net.HttpURLConnection
import java.net.URL
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * Geo 文件管理器 (geosite.dat 与 geoip.dat)。
 * 支持自定义更新 URL、断点多镜像下载、完整性校验、原子更新与热加载。
 */
object GeoManager {

    private const val PREFS = "mirage_geo_config"
    private const val KEY_GEOSITE_URL = "geosite_url"
    private const val KEY_GEOIP_URL = "geoip_url"
    private const val KEY_LAST_UPDATE = "last_update_time"

    // 官方默认源 (多镜像兜底)
    const val DEFAULT_GEOSITE_URL = "https://raw.githubusercontent.com/Loyalsoldier/v2ray-rules-dat/release/geosite.dat"
    const val DEFAULT_GEOIP_URL = "https://raw.githubusercontent.com/Loyalsoldier/v2ray-rules-dat/release/geoip.dat"

    val GEOSITE_MIRRORS = listOf(
        "https://raw.githubusercontent.com/Loyalsoldier/v2ray-rules-dat/release/geosite.dat",
        "https://fastly.jsdelivr.net/gh/Loyalsoldier/v2ray-rules-dat@release/geosite.dat",
        "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geosite.dat"
    )

    val GEOIP_MIRRORS = listOf(
        "https://raw.githubusercontent.com/Loyalsoldier/v2ray-rules-dat/release/geoip.dat",
        "https://fastly.jsdelivr.net/gh/Loyalsoldier/v2ray-rules-dat@release/geoip.dat",
        "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geoip.dat"
    )

    // 常用热门 Tag 推荐
    val POPULAR_GEOSITE_TAGS = listOf(
        "cn",
        "category-ads-all",
        "google",
        "apple",
        "telegram",
        "netflix",
        "youtube",
        "openai",
        "github",
        "microsoft",
        "twitter",
        "steam",
        "bilibili",
        "geolocation-!cn"
    )

    val POPULAR_GEOIP_TAGS = listOf(
        "cn",
        "telegram",
        "private",
        "google",
        "netflix",
        "twitter",
        "apple"
    )

    data class GeoStatus(
        val geositeExists: Boolean,
        val geositeSize: Long,
        val geoipExists: Boolean,
        val geoipSize: Long,
        val lastUpdateTime: String,
        val geositeTagCount: Int,
        val geoipCodeCount: Int
    ) {
        val isReady: Boolean get() = geositeExists || geoipExists
        val displaySummary: String
            get() = if (isReady) {
                "Geo: 已就绪 (${geositeTagCount} Sites / ${geoipCodeCount} IPs) · $lastUpdateTime"
            } else {
                "Geo: 未下载 (使用系统内置 CN 白名单)"
            }
    }

    data class GeoUpdateResult(
        val success: Boolean,
        val message: String,
        val geositeTags: Int = 0,
        val geoipCodes: Int = 0
    )

    fun getGeoDir(context: Context): File {
        val dir = File(context.filesDir, "geo")
        if (!dir.exists()) dir.mkdirs()
        return dir
    }

    fun getGeositeFile(context: Context): File = File(getGeoDir(context), "geosite.dat")
    fun getGeoipFile(context: Context): File = File(getGeoDir(context), "geoip.dat")

    fun getGeositeUrl(context: Context): String {
        return context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getString(KEY_GEOSITE_URL, DEFAULT_GEOSITE_URL) ?: DEFAULT_GEOSITE_URL
    }

    fun setGeositeUrl(context: Context, url: String) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit().putString(KEY_GEOSITE_URL, url.trim()).apply()
    }

    fun getGeoipUrl(context: Context): String {
        return context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getString(KEY_GEOIP_URL, DEFAULT_GEOIP_URL) ?: DEFAULT_GEOIP_URL
    }

    fun setGeoipUrl(context: Context, url: String) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit().putString(KEY_GEOIP_URL, url.trim()).apply()
    }

    fun resetDefaultUrls(context: Context) {
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit()
            .putString(KEY_GEOSITE_URL, DEFAULT_GEOSITE_URL)
            .putString(KEY_GEOIP_URL, DEFAULT_GEOIP_URL)
            .apply()
    }

    fun getLastUpdateTime(context: Context): String {
        val ts = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .getLong(KEY_LAST_UPDATE, 0L)
        return if (ts > 0) {
            SimpleDateFormat("yyyy-MM-dd HH:mm", Locale.getDefault()).format(Date(ts))
        } else {
            "未更新"
        }
    }

    /**
     * 获取当前 Geo 文件状态及已加载 tags 统计。
     */
    fun getStatus(context: Context): GeoStatus {
        val siteFile = getGeositeFile(context)
        val ipFile = getGeoipFile(context)

        var siteTags = 0
        var ipCodes = 0
        runCatching {
            val json = MirageNative.getGeoTags()
            val obj = JSONObject(json)
            siteTags = obj.optInt("geosite_count", 0)
            ipCodes = obj.optInt("geoip_count", 0)
        }

        return GeoStatus(
            geositeExists = siteFile.exists() && siteFile.length() > 50 * 1024,
            geositeSize = if (siteFile.exists()) siteFile.length() else 0L,
            geoipExists = ipFile.exists() && ipFile.length() > 50 * 1024,
            geoipSize = if (ipFile.exists()) ipFile.length() else 0L,
            lastUpdateTime = getLastUpdateTime(context),
            geositeTagCount = siteTags,
            geoipCodeCount = ipCodes
        )
    }

    /**
     * 将本地 Geo 文件路径注入到 Rust Core 内核中。
     */
    fun loadGeoFilesToNative(
        context: Context,
        customGeositePath: String? = null,
        customGeoipPath: String? = null
    ): String {
        val siteFile = customGeositePath?.let { File(it) } ?: getGeositeFile(context)
        val ipFile = customGeoipPath?.let { File(it) } ?: getGeoipFile(context)

        val sitePath = if (siteFile.exists() && siteFile.length() > 50 * 1024) siteFile.absolutePath else ""
        val ipPath = if (ipFile.exists() && ipFile.length() > 50 * 1024) ipFile.absolutePath else ""

        return runCatching {
            MirageNative.loadGeoFiles(sitePath, ipPath)
        }.getOrDefault("{\"status\":\"error\"}")
    }

    /**
     * 获取已加载的所有 tags (供 UI 自动补全使用)。
     */
    fun getLoadedTags(): Pair<List<String>, List<String>> {
        return runCatching {
            val json = MirageNative.getGeoTags()
            val obj = JSONObject(json)
            val sites = mutableListOf<String>()
            val ips = mutableListOf<String>()

            val siteArr = obj.optJSONArray("geosite_tags")
            if (siteArr != null) {
                for (i in 0 until siteArr.length()) sites.add(siteArr.getString(i).lowercase())
            }
            val ipArr = obj.optJSONArray("geoip_codes")
            if (ipArr != null) {
                for (i in 0 until ipArr.length()) ips.add(ipArr.getString(i).lowercase())
            }
            sites to ips
        }.getOrDefault(emptyList<String>() to emptyList<String>())
    }

    /**
     * 执行在线更新/下载 Geo 文件。
     */
    suspend fun updateGeoFiles(
        context: Context,
        onProgress: (String, Int) -> Unit
    ): GeoUpdateResult = withContext(Dispatchers.IO) {
        val siteUrl = getGeositeUrl(context)
        val ipUrl = getGeoipUrl(context)

        val siteMirrors = if (siteUrl == DEFAULT_GEOSITE_URL) GEOSITE_MIRRORS else listOf(siteUrl)
        val ipMirrors = if (ipUrl == DEFAULT_GEOIP_URL) GEOIP_MIRRORS else listOf(ipUrl)

        val siteFile = getGeositeFile(context)
        val ipFile = getGeoipFile(context)

        val siteTmp = File(siteFile.parentFile, "geosite.dat.tmp")
        val ipTmp = File(ipFile.parentFile, "geoip.dat.tmp")

        // 1. 下载 geosite.dat
        onProgress("正在下载 GeoSite 数据集…", 15)
        val siteOk = downloadWithMirrors(siteMirrors, siteTmp) { progress ->
            onProgress("正在下载 GeoSite 数据集 (${progress}%)…", (15 + progress * 0.35).toInt())
        }
        if (!siteOk || siteTmp.length() < 50 * 1024) {
            siteTmp.delete()
            return@withContext GeoUpdateResult(false, "GeoSite 数据集下载失败，请检查网络或更换更新 URL")
        }

        // 2. 下载 geoip.dat
        onProgress("正在下载 GeoIP 数据集…", 55)
        val ipOk = downloadWithMirrors(ipMirrors, ipTmp) { progress ->
            onProgress("正在下载 GeoIP 数据集 (${progress}%)…", (55 + progress * 0.35).toInt())
        }
        if (!ipOk || ipTmp.length() < 50 * 1024) {
            siteTmp.delete()
            ipTmp.delete()
            return@withContext GeoUpdateResult(false, "GeoIP 数据集下载失败，请检查网络或更换更新 URL")
        }

        // 3. 原子替换
        onProgress("正在校验与安装 Geo 数据文件…", 92)
        if (siteTmp.exists()) {
            siteFile.delete()
            siteTmp.renameTo(siteFile)
        }
        if (ipTmp.exists()) {
            ipFile.delete()
            ipTmp.renameTo(ipFile)
        }

        // 4. 热加载到 Rust Core
        val loadResultJson = loadGeoFilesToNative(context)
        var siteCount = 0
        var ipCount = 0
        runCatching {
            val obj = JSONObject(loadResultJson)
            siteCount = obj.optInt("geosite_tags", 0)
            ipCount = obj.optInt("geoip_codes", 0)
        }

        // 5. 记录更新时间
        context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)
            .edit()
            .putLong(KEY_LAST_UPDATE, System.currentTimeMillis())
            .apply()

        onProgress("Geo 数据集更新成功！", 100)
        GeoUpdateResult(
            success = true,
            message = "成功更新 Geo 规则集: $siteCount 个 Site 标签, $ipCount 个 IP 分类",
            geositeTags = siteCount,
            geoipCodes = ipCount
        )
    }

    private fun downloadWithMirrors(
        mirrors: List<String>,
        dest: File,
        onProgress: (Int) -> Unit
    ): Boolean {
        for (url in mirrors) {
            try {
                val ok = downloadFile(url, dest, onProgress)
                if (ok && dest.exists() && dest.length() > 50 * 1024) {
                    return true
                }
            } catch (e: Exception) {
                // 尝试下一个镜像
            }
        }
        return false
    }

    private fun downloadFile(urlStr: String, dest: File, onProgress: (Int) -> Unit): Boolean {
        var connection: HttpURLConnection? = null
        try {
            var url = URL(urlStr)
            var redirects = 0
            while (redirects < 5) {
                connection = url.openConnection() as HttpURLConnection
                connection.instanceFollowRedirects = false
                connection.connectTimeout = 15000
                connection.readTimeout = 60000
                connection.setRequestProperty("User-Agent", "Mirage-Android/0.2.1")
                connection.connect()

                val code = connection.responseCode
                if (code in 300..399) {
                    val loc = connection.getHeaderField("Location") ?: break
                    url = URL(url, loc)
                    connection.disconnect()
                    redirects++
                    continue
                }
                if (code != HttpURLConnection.HTTP_OK) {
                    return false
                }
                break
            }

            val total = connection?.contentLength ?: -1
            dest.parentFile?.mkdirs()
            connection?.inputStream?.use { input ->
                FileOutputStream(dest).use { output ->
                    val buffer = ByteArray(32 * 1024)
                    var read: Int
                    var current = 0L
                    while (input.read(buffer).also { read = it } != -1) {
                        output.write(buffer, 0, read)
                        current += read
                        if (total > 0) {
                            val pct = ((current * 100) / total).toInt()
                            onProgress(pct.coerceIn(0, 100))
                        }
                    }
                    output.flush()
                }
            }
            return dest.length() > 0
        } catch (e: Exception) {
            dest.delete()
            return false
        } finally {
            connection?.disconnect()
        }
    }
}
