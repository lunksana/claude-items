package com.mirage.android

import android.content.Context
import android.graphics.Canvas
import android.graphics.Paint
import android.graphics.Path
import android.view.View

/** 简单双线速率图 (上行绿, 下行蓝)。 */
class TrafficChart @JvmOverloads constructor(
    context: Context, attrs: android.util.AttributeSet? = null
) : View(context, attrs) {

    private var upData: List<Float> = emptyList()
    private var downData: List<Float> = emptyList()
    private val upPaint = Paint().apply { color = 0xFF10B981.toInt(); strokeWidth = 3f; style = Paint.Style.STROKE }
    private val downPaint = Paint().apply { color = 0xFF3B82F6.toInt(); strokeWidth = 3f; style = Paint.Style.STROKE }
    private val gridPaint = Paint().apply { color = 0x22000000.toInt(); strokeWidth = 1f }

    fun setData(up: List<Float>, down: List<Float>) {
        upData = up
        downData = down
        invalidate()
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val w = width.toFloat(); val h = height.toFloat()
        for (i in 1..3) {
            val y = h * i / 4
            canvas.drawLine(0f, y, w, y, gridPaint)
        }
        fun drawLine(data: List<Float>, paint: Paint) {
            if (data.size < 2) return
            val max = data.maxOrNull()?.coerceAtLeast(1f) ?: 1f
            val path = Path()
            for (i in data.indices) {
                val x = w * i / (data.size - 1)
                val y = h - (h * 0.9f) * (data[i] / max).coerceIn(0f, 1f) - h * 0.05f
                if (i == 0) path.moveTo(x, y) else path.lineTo(x, y)
            }
            canvas.drawPath(path, paint)
        }
        drawLine(upData, upPaint)
        drawLine(downData, downPaint)
    }
}
