package com.mirage.android.core

import android.content.Context
import android.os.Build
import com.mirage.android.data.model.CoreInfo
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.io.InputStream
import java.util.UUID

/**
 * Mirage 内核管理器: 管理内置内核与自定义导入的 .so 内核。
 */
class CoreManager private constructor(private val context: Context) {

    private val coresDir = File(context.filesDir, "cores").apply { mkdirs() }
    private val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

    private val _cores = MutableStateFlow<List<CoreInfo>>(emptyList())
    val cores: StateFlow<List<CoreInfo>> = _cores.asStateFlow()

    private val _activeCoreId = MutableStateFlow(CoreInfo.BUILTIN_ID)
    val activeCoreId: StateFlow<String> = _activeCoreId.asStateFlow()

    init {
        loadCores()
    }

    private fun loadCores() {
        val raw = prefs.getString(KEY_CORES_JSON, "[]") ?: "[]"
        val customList = runCatching {
            val arr = JSONArray(raw)
            (0 until arr.length()).mapNotNull { i ->
                val o = arr.getJSONObject(i)
                val path = o.getString("file_path")
                val file = File(path)
                if (file.exists()) {
                    CoreInfo(
                        id = o.getString("id"),
                        name = o.getString("name"),
                        version = o.optString("version", "自定义版本"),
                        abi = o.optString("abi", "arm64-v8a"),
                        filePath = path,
                        fileSize = file.length(),
                        isBuiltin = false,
                        addedTime = o.optLong("added_time", System.currentTimeMillis())
                    )
                } else null
            }
        }.getOrDefault(emptyList())

        val all = listOf(CoreInfo.builtin()) + customList
        _cores.value = all

        val savedActiveId = prefs.getString(KEY_ACTIVE_CORE, CoreInfo.BUILTIN_ID) ?: CoreInfo.BUILTIN_ID
        _activeCoreId.value = if (all.any { it.id == savedActiveId }) savedActiveId else CoreInfo.BUILTIN_ID
    }

    private fun saveCustomCores(list: List<CoreInfo>) {
        val arr = JSONArray()
        for (c in list.filter { !it.isBuiltin }) {
            arr.put(
                JSONObject()
                    .put("id", c.id)
                    .put("name", c.name)
                    .put("version", c.version)
                    .put("abi", c.abi)
                    .put("file_path", c.filePath)
                    .put("added_time", c.addedTime)
            )
        }
        prefs.edit().putString(KEY_CORES_JSON, arr.toString()).apply()
    }

    fun getActiveCore(): CoreInfo {
        val id = _activeCoreId.value
        return _cores.value.firstOrNull { it.id == id } ?: CoreInfo.builtin()
    }

    fun setActiveCore(id: String): Boolean {
        if (_cores.value.any { it.id == id }) {
            _activeCoreId.value = id
            prefs.edit().putString(KEY_ACTIVE_CORE, id).apply()
            return true
        }
        return false
    }

    /**
     * 导入外部 .so 文件。
     */
    fun importCore(inputStream: InputStream, displayName: String): Result<CoreInfo> {
        val tempFile = File(coresDir, "temp_${System.currentTimeMillis()}.so")
        try {
            tempFile.outputStream().use { output ->
                inputStream.copyTo(output)
            }

            // 检查 ELF ABI 兼容性
            val abiResult = ElfChecker.checkAbi(tempFile)
            if (abiResult.isFailure) {
                tempFile.delete()
                return Result.failure(abiResult.exceptionOrNull() ?: IllegalArgumentException("非法 ELF 文件"))
            }

            val abi = abiResult.getOrThrow()
            if (!ElfChecker.isDeviceCompatible(abi)) {
                tempFile.delete()
                val devAbis = Build.SUPPORTED_ABIS.joinToString(", ")
                return Result.failure(
                    IllegalArgumentException("架构不兼容！该内核为 $abi，但当前设备支持: $devAbis")
                )
            }

            val id = UUID.randomUUID().toString()
            val targetFile = File(coresDir, "libmirage_$id.so")
            if (!tempFile.renameTo(targetFile)) {
                tempFile.copyTo(targetFile, overwrite = true)
                tempFile.delete()
            }

            val coreInfo = CoreInfo(
                id = id,
                name = displayName.ifBlank { "Mirage 内核 ($abi)" },
                version = "自定义版本 ($abi)",
                abi = abi,
                filePath = targetFile.absolutePath,
                fileSize = targetFile.length(),
                isBuiltin = false
            )

            val current = _cores.value.toMutableList()
            current.add(coreInfo)
            _cores.value = current
            saveCustomCores(current)

            return Result.success(coreInfo)
        } catch (e: Exception) {
            tempFile.delete()
            return Result.failure(e)
        }
    }

    fun deleteCore(id: String): Boolean {
        if (id == CoreInfo.BUILTIN_ID) return false
        val current = _cores.value.toMutableList()
        val item = current.firstOrNull { it.id == id } ?: return false

        item.file?.delete()
        current.remove(item)
        _cores.value = current
        saveCustomCores(current)

        if (_activeCoreId.value == id) {
            setActiveCore(CoreInfo.BUILTIN_ID)
        }
        return true
    }

    fun resetToBuiltin(): Boolean {
        return setActiveCore(CoreInfo.BUILTIN_ID)
    }

    companion object {
        private const val PREFS_NAME = "mirage_cores"
        private const val KEY_CORES_JSON = "cores_json"
        private const val KEY_ACTIVE_CORE = "active_core_id"

        @Volatile
        private var instance: CoreManager? = null

        fun getInstance(context: Context): CoreManager {
            return instance ?: synchronized(this) {
                instance ?: CoreManager(context.applicationContext).also { instance = it }
            }
        }
    }
}
