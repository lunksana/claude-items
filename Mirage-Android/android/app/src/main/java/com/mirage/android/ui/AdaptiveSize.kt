package com.mirage.android.ui

import android.content.Context

/**
 * 自适应尺寸: 按屏幕短边 (dp) 动态放大字号/尺寸, 大屏自动更大。
 */
object AdaptiveSize {
    /** 按屏幕短边分级的基础字号增量。 */
    fun sp(context: Context, base: Float): Float {
        val dm = context.resources.displayMetrics
        val shortSideDp = minOf(dm.widthPixels, dm.heightPixels) / dm.density
        return when {
            shortSideDp >= 800 -> base + 8
            shortSideDp >= 600 -> base + 6
            shortSideDp >= 480 -> base + 4
            shortSideDp >= 400 -> base + 2
            else -> base
        }
    }

    /** dp → px。 */
    fun px(context: Context, dp: Int): Int =
        (dp * context.resources.displayMetrics.density).toInt()
}
