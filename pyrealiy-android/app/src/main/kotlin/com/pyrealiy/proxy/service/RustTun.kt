package com.pyrealiy.proxy.service

import android.os.ParcelFileDescriptor
import com.pyrealiy.proxy.core.Logger
import java.io.FileDescriptor

/**
 * 这是 Kotlin 层与 Rust 层进行跨语言通信 (JNI) 的桥梁。
 */
object RustTun {

    // 静态代码块：当这个类被首次使用时，立刻加载我们刚才写的 C 动态链接库
    init {
        try {
            System.loadLibrary("pyrealiy_ffi")
            Logger.i("RustTun", "成功加载 Rust 动态库 libpyrealiy_ffi.so")
        } catch (e: Exception) {
            Logger.e("RustTun", "加载 Rust 动态库失败: ${e.message}", e)
        }
    }

    /**
     * 这是一个标记为 `external` 的方法，意味着它的实现在 Rust 层！
     * 
     * @param tunFd 这是 Android 系统的 VpnService 提供的虚拟网卡的文件描述符 (整数型)
     * @param callback 这是一个实现了 RustTunCallback 接口的对象，Rust 会调用里面的方法
     */
    external fun startTunLoop(tunFd: Int, callback: RustTunCallback)
}

/**
 * 这个接口供 Rust 调用，把底层 smoltcp 提取出的合法 TCP 桥接管道丢给 Kotlin！
 */
interface RustTunCallback {
    /**
     * 当 Rust 从 TUN 发现了手机想建立一个新的 TCP 连接时：
     * 
     * @param localFd Rust 在系统内核里为你建立的 Unix SocketPair 的其中一端
     * @param targetIp 手机真正想访问的目标 IP (比如 142.250.190.46)
     * @param targetPort 手机想访问的端口 (比如 443)
     */
    fun onNewConnection(localFd: Int, targetIp: String, targetPort: Int) {
        Logger.i("RustTun", "💥 收到来自 Rust 的底层 TCP 桥接！目标：$targetIp:$targetPort 描述符：$localFd")
        
        // 伪代码：
        // 1. 我们利用 FileDescriptor 反射，把 localFd 这个整数包装成合法的 Java FileDescriptor
        // 2. 将其转为 FileInputStream 和 FileOutputStream
        // 3. 将这两个 Stream 丢给你完美的 ProxyTunnel.kt 进行加密发送！
    }
}
