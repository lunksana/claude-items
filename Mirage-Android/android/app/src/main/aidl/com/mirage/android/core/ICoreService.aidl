package com.mirage.android.core;

import com.mirage.android.core.ICoreCallback;

/**
 * Mirage 内核控制接口 (独立 :core 进程)。
 * App = 纯控制层, 通过本接口注入节点/规则、查询状态。
 */
interface ICoreService {
    /** 启动 VPN + 内核 (用当前节点配置)。返回 0=成功, 负数=错误码。 */
    int start();
    void stop();
    /** 运行时热切换节点 (mirage:// uri)。 */
    boolean setNode(String uri);
    /** 运行时热更新连接池容量。 */
    boolean setPoolSize(int poolSize);
    int getPoolSize();
    /** 注入路由规则 (JSON)。 */
    boolean setRules(String json);
    /** 动态设置日志等级 (trace/debug/info/warn/error)。 */
    boolean setLogLevel(String level);
    boolean setBlockQuic(boolean block);
    boolean isBlockQuic();
    boolean clearDnsCache();
    boolean setDnsServers(String directDns, String remoteDns);
    String getDirectDns();
    String getRemoteDns();
    boolean isRunning();
    boolean isHealthy();
    long latencyMs();
    /** [up,down,upRate,downRate,tcp,udp,dns] */
    double[] getStats();
    String getConnectionsJson();
    String[] recentLogs();
    String[] getBuiltinDomains();
    long getBuiltinIpCount();
    /** 完整握手测活, RTT ms 或 -1。 */
    long testNode(String uri, int timeoutMs);
    void registerCallback(ICoreCallback cb);
    void unregisterCallback(ICoreCallback cb);
}
