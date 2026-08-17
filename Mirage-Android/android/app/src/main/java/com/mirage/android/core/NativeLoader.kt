package com.mirage.android.core

import android.content.Context
import android.util.Log

/**
 * 动态原生内核加载器: 支持从 App 私有目录动态加载指定的 .so 内核，失败时安全降级至内置内核。
 */
object NativeLoader {

    @Volatile
    private var isLoaded = false

    @Volatile
    private var activeCoreDisplayName = "内置内核"

    @Volatile
    private var loadedVersion = ""

    fun isLoaded(): Boolean = isLoaded
    fun getActiveCoreName(): String = activeCoreDisplayName
    fun getLoadedVersion(): String = loadedVersion

    @Synchronized
    fun load(context: Context): Boolean {
        if (isLoaded) return true

        val coreManager = CoreManager.getInstance(context)
        val activeCore = coreManager.getActiveCore()

        if (!activeCore.isBuiltin && activeCore.filePath != null) {
            val file = activeCore.file
            if (file != null && file.exists()) {
                try {
                    Log.i("Mirage", "[loader] 尝试加载自定义内核: ${activeCore.name} (${file.absolutePath})")
                    System.load(file.absolutePath)
                    isLoaded = true
                    activeCoreDisplayName = activeCore.name
                    loadedVersion = runCatching { MirageNative.version() }.getOrDefault("未知")
                    Log.i("Mirage", "[loader] 自定义内核加载成功! 版本: $loadedVersion")
                    LogStore.append("[loader] 自定义内核已载入: ${activeCore.name} ($loadedVersion)")
                    return true
                } catch (e: Throwable) {
                    Log.e("Mirage", "[loader] 自定义内核加载失败，自动回退到内置内核: ${e.message}", e)
                    LogStore.append("[loader] 自定义内核加载失败 (${e.message})，正在回退内置内核...")
                }
            }
        }

        // 默认/回退加载打包在 APK 内的原生库
        try {
            Log.i("Mirage", "[loader] 加载 APK 内置 libmirage_jni.so...")
            System.loadLibrary("mirage_jni")
            isLoaded = true
            activeCoreDisplayName = "内置默认内核"
            loadedVersion = runCatching { MirageNative.version() }.getOrDefault("未知")
            Log.i("Mirage", "[loader] 内置内核加载成功! 版本: $loadedVersion")
            LogStore.append("[loader] 内置内核已载入: $loadedVersion")
            return true
        } catch (e: Throwable) {
            Log.e("Mirage", "[loader] 内置内核加载致命错误: ${e.message}", e)
            LogStore.append("[loader] 内置内核加载失败: ${e.message}")
            return false
        }
    }
}
