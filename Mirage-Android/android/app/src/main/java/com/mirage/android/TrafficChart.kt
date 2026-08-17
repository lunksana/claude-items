package com.mirage.android

import android.content.Context
import android.graphics.*
import android.util.AttributeSet
import android.view.View

/**
 * 现代化双线平滑速率图 (上行琥珀橙/绿色, 下行品牌蓝), 支持贝塞尔曲线与渐变区域填充。
 */
class TrafficChart @JvmOverloads constructor(
    context: Context,
    attrs: AttributeSet? = null,
    defStyleAttr: Int = 0
) : View(context, attrs, defStyleAttr) {

    private var upData: List<Float> = emptyList()
    private var downData: List<Float> = emptyList()

    private val upColor = 0xFFF29A00.toInt() // 琥珀橙
    private val downColor = 0xFF0077CC.toInt() // 品牌蓝
    private val gridColor = 0x18000000.toInt()

    private val upPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = upColor
        strokeWidth = 4f
        style = Paint.Style.STROKE
        strokeCap = Paint.Cap.ROUND
        strokeJoin = Paint.Join.ROUND
    }

    private val downPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = downColor
        strokeWidth = 4f
        style = Paint.Style.STROKE
        strokeCap = Paint.Cap.ROUND
        strokeJoin = Paint.Join.ROUND
    }

    private val upFillPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.FILL
    }

    private val downFillPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.FILL
    }

    private val gridPaint = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        color = gridColor
        strokeWidth = 1.5f
        pathEffect = DashPathEffect(floatArrayOf(10f, 10f), 0f)
    }

    fun setData(up: List<Float>, down: List<Float>) {
        upData = up
        downData = down
        invalidate()
    }

    override fun onDraw(canvas: Canvas) {
        super.onDraw(canvas)
        val w = width.toFloat()
        val h = height.toFloat()
        if (w <= 0 || h <= 0) return

        // 绘制水平虚线网格
        val gridLines = 4
        for (i in 1 until gridLines) {
            val y = h * i / gridLines
            canvas.drawLine(0f, y, w, y, gridPaint)
        }

        // 计算当前缩放最大值 (最小保留 10KB/s 防止空闲时跳动过大)
        val maxUp = upData.maxOrNull() ?: 0f
        val maxDown = downData.maxOrNull() ?: 0f
        val maxVal = maxOf(maxUp, maxDown, 10240f)

        // 绘制下行曲线及渐变填充
        drawSmoothCurve(canvas, downData, maxVal, downPaint, downFillPaint, downColor, w, h)
        // 绘制上行曲线及渐变填充
        drawSmoothCurve(canvas, upData, maxVal, upPaint, upFillPaint, upColor, w, h)
    }

    private fun drawSmoothCurve(
        canvas: Canvas,
        data: List<Float>,
        maxVal: Float,
        strokePaint: Paint,
        fillPaint: Paint,
        baseColor: Int,
        w: Float,
        h: Float
    ) {
        if (data.size < 2) return

        val strokePath = Path()
        val fillPath = Path()

        val points = mutableListOf<PointF>()
        val stepX = w / (data.size - 1).coerceAtLeast(1)
        val bottomY = h - 4f

        for (i in data.indices) {
            val x = i * stepX
            val ratio = (data[i] / maxVal).coerceIn(0f, 1f)
            val y = (h - 8f) - ((h - 20f) * ratio)
            points.add(PointF(x, y))
        }

        strokePath.moveTo(points[0].x, points[0].y)
        fillPath.moveTo(points[0].x, bottomY)
        fillPath.lineTo(points[0].x, points[0].y)

        for (i in 0 until points.size - 1) {
            val p0 = points[i]
            val p1 = points[i + 1]
            val controlX1 = p0.x + (p1.x - p0.x) / 2
            val controlY1 = p0.y
            val controlX2 = p0.x + (p1.x - p0.x) / 2
            val controlY2 = p1.y

            strokePath.cubicTo(controlX1, controlY1, controlX2, controlY2, p1.x, p1.y)
            fillPath.cubicTo(controlX1, controlY1, controlX2, controlY2, p1.x, p1.y)
        }

        fillPath.lineTo(points.last().x, bottomY)
        fillPath.close()

        // 填充半透明渐变
        val alphaColor = Color.argb(40, Color.red(baseColor), Color.green(baseColor), Color.blue(baseColor))
        val transparentColor = Color.argb(0, Color.red(baseColor), Color.green(baseColor), Color.blue(baseColor))
        fillPaint.shader = LinearGradient(0f, 0f, 0f, h, alphaColor, transparentColor, Shader.TileMode.CLAMP)

        canvas.drawPath(fillPath, fillPaint)
        canvas.drawPath(strokePath, strokePaint)
    }
}
