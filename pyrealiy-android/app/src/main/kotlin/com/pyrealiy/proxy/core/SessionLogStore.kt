package com.pyrealiy.proxy.core

import android.content.Context
import java.io.File
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/**
 * 持久化最近若干次 VPN 会话的日志，方便事后导出排查。
 *
 * 文件存放：filesDir/logs/session-YYYY-MM-DD_HH-mm-ss.log
 * 仅保留最近 MAX_SESSIONS 份，旧的自动删除。
 *
 * 写盘时机：VPN 每次 stopVpn 时调一次 saveSession。
 */
object SessionLogStore {

    private const val MAX_SESSIONS = 3
    private const val DIR_NAME = "logs"
    private val fileFmt = SimpleDateFormat("yyyy-MM-dd_HH-mm-ss", Locale.US)

    private fun dir(context: Context): File {
        val d = File(context.filesDir, DIR_NAME)
        if (!d.exists()) d.mkdirs()
        return d
    }

    /** 把传入的内存日志快照落盘为一份新会话，并清理超额旧文件 */
    fun saveSession(context: Context, lines: List<String>) {
        if (lines.isEmpty()) return
        runCatching {
            val name = "session-${fileFmt.format(Date())}.log"
            File(dir(context), name).writeText(lines.joinToString("\n"))
            rotate(context)
        }
    }

    private fun rotate(context: Context) {
        val files = listSessions(context)
        if (files.size <= MAX_SESSIONS) return
        files.drop(MAX_SESSIONS).forEach { runCatching { it.delete() } }
    }

    /** 按时间倒序返回历史日志文件（最新的在前） */
    fun listSessions(context: Context): List<File> =
        dir(context).listFiles()?.sortedByDescending { it.lastModified() } ?: emptyList()

    /** 把当前内存日志 + 所有历史会话拼成完整 dump，用于一键导出 */
    fun exportAll(context: Context, currentLines: List<String>): String =
        buildString {
            append("=== 当前会话（内存中） ===\n")
            append(currentLines.joinToString("\n"))
            append("\n\n")
            listSessions(context).forEachIndexed { i, f ->
                append("=== 历史会话 #${i + 1}：${f.name} ===\n")
                append(runCatching { f.readText() }.getOrDefault("[read failed]"))
                append("\n\n")
            }
        }
}
