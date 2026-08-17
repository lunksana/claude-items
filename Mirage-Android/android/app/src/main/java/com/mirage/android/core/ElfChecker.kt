package com.mirage.android.core

import android.os.Build
import java.io.File
import java.io.RandomAccessFile

/**
 * ELF 二进制架构校验器 (读取 .so 文件头 e_machine 字段)。
 */
object ElfChecker {

    private const val ELF_MAGIC_0 = 0x7F.toByte()
    private const val ELF_MAGIC_1 = 'E'.code.toByte()
    private const val ELF_MAGIC_2 = 'L'.code.toByte()
    private const val ELF_MAGIC_3 = 'F'.code.toByte()

    /**
     * 检查 .so 文件架构。
     * 返回对应 Android ABI 字符串 (如 "arm64-v8a", "x86_64", "armeabi-v7a", "x86")。
     */
    fun checkAbi(file: File): Result<String> {
        if (!file.exists() || file.length() < 52) {
            return Result.failure(IllegalArgumentException("文件不存在或大小异常"))
        }

        return try {
            RandomAccessFile(file, "r").use { raf ->
                val magic = ByteArray(4)
                raf.readFully(magic)
                if (magic[0] != ELF_MAGIC_0 || magic[1] != ELF_MAGIC_1 || magic[2] != ELF_MAGIC_2 || magic[3] != ELF_MAGIC_3) {
                    return Result.failure(IllegalArgumentException("不是合法的 ELF/SO 原生库文件 (Magic 不匹配)"))
                }

                // 读 ELF 编码方式 (byte 5: 1=Little Endian, 2=Big Endian)
                raf.seek(5)
                val isLittleEndian = raf.read() == 1

                // 读 e_machine 字段 (偏移 18)
                raf.seek(18)
                val b0 = raf.read()
                val b1 = raf.read()
                val eMachine = if (isLittleEndian) {
                    (b1 shl 8) or b0
                } else {
                    (b0 shl 8) or b1
                }

                val abi = when (eMachine) {
                    183 -> "arm64-v8a"   // EM_AARCH64
                    40 -> "armeabi-v7a"   // EM_ARM
                    62 -> "x86_64"        // EM_X86_64
                    3 -> "x86"            // EM_386
                    else -> "unknown ($eMachine)"
                }

                Result.success(abi)
            }
        } catch (e: Exception) {
            Result.failure(e)
        }
    }

    /**
     * 检查该 ABI 是否能在当前 Android 设备上运行。
     */
    fun isDeviceCompatible(abi: String): Boolean {
        return Build.SUPPORTED_ABIS.any { it.equals(abi, ignoreCase = true) }
    }
}
