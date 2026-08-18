package com.mirage.android.core

/**
 * mirage-core 的 JNI 接口 (对应 native/mirage-jni)。
 * 库由 NativeLoader 动态加载。
 */
object MirageNative {
    init {
        // 如果外部尚未通过 NativeLoader.load() 加载，则尝试默认载入内置库
        if (!NativeLoader.isLoaded()) {
            runCatching { System.loadLibrary("mirage_jni") }
        }
    }

    /** 启动 TUN 引擎。返回 0 = 成功, 负数 = 错误码。uri 形如 mirage://密码@host:端口?sni=... */
    external fun start(tunFd: Int, uri: String, poolSize: Int): Int

    /** 停止引擎 (幂等)。 */
    external fun stop()

    external fun isRunning(): Boolean
    external fun isHealthy(): Boolean

    /** RTT 毫秒, -1 = 未知 */
    external fun latencyMs(): Long

    /** 取最近日志行 */
    external fun recentLogs(): Array<String>

    /** 取待 protect 的 socket fd (VpnService.protect 必须在 Java 侧调用) */
    external fun drainProtectFds(): IntArray

    external fun version(): String

    /** 动态设置 Rust 日志等级 (trace/debug/info/warn/error)。 */
    external fun setLogLevel(level: String): Boolean

    /** 设置自定义分流规则 (JSON: domains_direct/domains_proxy/cidrs_direct/cidrs_proxy)。 */
    external fun setRules(json: String): Boolean

    /** 设置是否全局屏蔽 QUIC (UDP 443)。 */
    external fun setBlockQuic(block: Boolean): Boolean

    /** 查询当前是否开启 QUIC 屏蔽。 */
    external fun isBlockQuic(): Boolean

    /**
     * 流量统计: [up_total, down_total, up_rate(B/s), down_rate(B/s),
     * tcp_conns, udp_flows, dns_queries]。
     */
    external fun getStats(): DoubleArray

    /** 获取活跃与最近连接监控列表 (JSON 字符串)。 */
    external fun getConnectionsJson(): String

    /** 完整协议握手测活: 返回 RTT 毫秒, -1 = 不可用。 */
    external fun testNode(uri: String, timeoutMs: Int): Long

    /** 运行时热切换节点 (无需断开)。 */
    external fun setNode(uri: String): Boolean

    /** 运行时热更新连接池容量。 */
    external fun setPoolSize(poolSize: Int): Boolean

    /** 获取当前连接池容量。 */
    external fun getPoolSize(): Int

    /** 内置国内域名列表 (规则界面展示)。 */
    external fun getBuiltinDomains(): Array<String>

    /** 内置中国 IP 段数量。 */
    external fun getBuiltinIpCount(): Long

    /**
     * Rust 侧同步调用 (隧道 socket connect 前): 把 fd 交给当前活跃的 VpnService protect。
     * 必须同步返回 (protect 设置 SO_MARK 影响路由, 晚了会导致隧道流量进 TUN 环路)。
     */
    @JvmStatic
    fun protectFd(fd: Int) {
        com.mirage.android.CoreService.protectFd(fd)
    }
}
