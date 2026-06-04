package com.pyrealiy.proxy.ui

import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.widget.ImageButton
import android.widget.ScrollView
import android.widget.TextView
import android.widget.Toast
import androidx.appcompat.app.AppCompatActivity
import com.pyrealiy.proxy.R
import android.content.Intent
import com.pyrealiy.proxy.core.Logger
import com.pyrealiy.proxy.core.SessionLogStore
import java.util.concurrent.atomic.AtomicBoolean
import kotlin.concurrent.thread

/**
 * 实时显示 Logger 缓冲。
 *
 * 性能：
 *  - 仅渲染最后 VIEW_LINES 行，避免给 TextView 灌大段文本
 *  - 监听器触发 UI 刷新时做 150ms 合并，防止日志洪水期间 ANR
 *  - 复制全部日志在后台线程 join，再切回主线程写剪贴板
 */
class LogActivity : AppCompatActivity() {

    private companion object {
        const val VIEW_LINES = 300       // 视图只显示最后 300 行
        const val REFRESH_MIN_MS = 150L  // 两次 UI 刷新最小间隔
    }

    private lateinit var scroll: ScrollView
    private lateinit var text: TextView
    private val ui = Handler(Looper.getMainLooper())
    private val refreshPending = AtomicBoolean(false)

    /** 节流：每 150ms 最多一次 UI 刷新 */
    private val refresh: () -> Unit = {
        if (refreshPending.compareAndSet(false, true)) {
            ui.postDelayed({
                refreshPending.set(false)
                renderLogs()
            }, REFRESH_MIN_MS)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_log)
        scroll = findViewById(R.id.logScroll)
        text   = findViewById(R.id.logText)
        findViewById<ImageButton>(R.id.backBtn).setOnClickListener { finish() }
        findViewById<ImageButton>(R.id.clearBtn).setOnClickListener { Logger.clear() }
        findViewById<ImageButton>(R.id.copyBtn).setOnClickListener { copyAllToClipboard() }
        findViewById<ImageButton>(R.id.exportBtn).setOnClickListener { exportWithHistory() }
        renderLogs()
    }

    override fun onResume() {
        super.onResume()
        Logger.addListener(refresh)
        renderLogs()
    }

    override fun onPause() {
        super.onPause()
        Logger.removeListener(refresh)
        ui.removeCallbacksAndMessages(null)  // 清掉队列里残留的 refresh
    }

    private fun renderLogs() {
        val snap = Logger.snapshot()
        // 只渲染尾部 VIEW_LINES 行
        val view = if (snap.size <= VIEW_LINES) snap else snap.subList(snap.size - VIEW_LINES, snap.size)
        text.text = view.joinToString("\n")
        scroll.post { scroll.fullScroll(ScrollView.FOCUS_DOWN) }
    }

    /** 复制全部日志：join 在后台线程，避免主线程卡顿 */
    private fun copyAllToClipboard() {
        thread {
            val snap = Logger.snapshot()
            val all = snap.joinToString("\n")
            ui.post {
                runCatching {
                    val cm = getSystemService(Context.CLIPBOARD_SERVICE) as ClipboardManager
                    cm.setPrimaryClip(ClipData.newPlainText("PyReality logs", all))
                    Toast.makeText(this, "已复制 ${snap.size} 行日志", Toast.LENGTH_SHORT).show()
                }
            }
        }
    }

    /** 导出当前 + 历史（最近 3 次会话），通过系统分享 sheet 分发 */
    private fun exportWithHistory() {
        thread {
            val dump = SessionLogStore.exportAll(this, Logger.snapshot())
            val sessions = SessionLogStore.listSessions(this).size
            ui.post {
                runCatching {
                    val intent = Intent(Intent.ACTION_SEND).apply {
                        type = "text/plain"
                        putExtra(Intent.EXTRA_SUBJECT, "PyReality logs")
                        putExtra(Intent.EXTRA_TEXT, dump)
                    }
                    startActivity(Intent.createChooser(intent, "导出日志（当前 + $sessions 份历史）"))
                }
            }
        }
    }
}
