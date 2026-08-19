package com.mirage.android.receiver

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import com.mirage.android.CoreService

/**
 * 接收通知栏及系统快捷动作广播 (如点击通知栏「断开连接」按钮)。
 * 与 CoreService 运行在同一 :core 进程，直接触发 CoreService 停止。
 */
class CoreActionReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent?) {
        val action = intent?.action
        if (action == CoreService.ACTION_STOP) {
            val active = CoreService.getActive()
            if (active != null) {
                active.stopInternal()
            } else {
                val stopIntent = Intent(context, CoreService::class.java).setAction(CoreService.ACTION_STOP)
                runCatching { context.startService(stopIntent) }
            }
            // 发送全局 VPN 已停止广播同步 UI
            runCatching {
                context.sendBroadcast(Intent(CoreService.ACTION_VPN_STOPPED).setPackage(context.packageName))
            }
        }
    }
}
