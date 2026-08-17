package com.mirage.android.data.model

import java.io.File

/**
 * Mirage 内核描述模型。
 */
data class CoreInfo(
    val id: String,
    val name: String,
    val version: String,
    val abi: String,
    val filePath: String? = null,
    val fileSize: Long = 0L,
    val isBuiltin: Boolean = false,
    val addedTime: Long = System.currentTimeMillis()
) {
    val file: File? get() = filePath?.let { File(it) }

    val formattedSize: String get() {
        if (isBuiltin || fileSize <= 0) return "内置 (ROM)"
        return when {
            fileSize >= 1 shl 20 -> "%.2f MB".format(fileSize.toDouble() / (1 shl 20))
            fileSize >= 1 shl 10 -> "%.1f KB".format(fileSize.toDouble() / (1 shl 10))
            else -> "$fileSize B"
        }
    }

    companion object {
        const val BUILTIN_ID = "builtin"

        fun builtin(version: String = "v0.9.2 (内置)"): CoreInfo {
            return CoreInfo(
                id = BUILTIN_ID,
                name = "内置默认内核",
                version = version,
                abi = "系统原生",
                filePath = null,
                isBuiltin = true
            )
        }
    }
}
