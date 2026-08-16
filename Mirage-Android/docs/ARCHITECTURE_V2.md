# Mirage-Android 架构 (v2): 内核独立进程 + App 控制层

> 参考 meow (AIDL + 独立引擎进程) 与 sing-box (核心独立 + 配置下发) 的设计。

## 1. 目标架构

```
┌──────────────────────────────────────────────────────┐
│ App 进程 (com.mirage.android)                         │
│  ┌──────────────────────────────────────────────┐    │
│  │ UI (首页/节点/规则/流量) + CoreController     │    │
│  │   · 纯控制层: 发指令 / 收状态, 不含任何内核逻辑 │    │
│  └──────────────────┬───────────────────────────┘    │
│                     │ AIDL Binder                    │
│                     ▼                                │
│  CoreController: ICoreService 客户端                 │
│    bindService → :core 进程的 CoreService            │
└────────────────────┬─────────────────────────────────┘
                     │ (跨进程 IPC: AIDL)
┌────────────────────▼─────────────────────────────────┐
│ :core 进程 (com.mirage.android:core)                 │
│  CoreService : VpnService + ICoreService.Stub        │
│   · 创建 TUN (VpnService.Builder.establish)          │
│   · 持有 Rust 内核 (JNI, 本进程内加载)               │
│   · 控制入口: start/stop/setNode/setRules            │
│   · 状态出口: stats/logs (AIDL 回调)                 │
│  ┌──────────────────────────────────────────────┐    │
│  │ Rust 内核 (mirage-core via JNI)              │    │
│  │  TUN 引擎 + 协议内核 + 分流                   │    │
│  └──────────────────────────────────────────────┘    │
└──────────────────────────────────────────────────────┘
```

## 2. 设计原则 (对比 v1)

| 维度 | v1 (当前) | v2 (目标) |
|---|---|---|
| 内核位置 | App 进程内 (JNI in-process) | **独立 :core 进程** |
| App 角色 | 直接调 JNI | **纯控制层** (AIDL 发指令) |
| TUN 归属 | App 的 VpnService | **CoreService (core 进程)** |
| 规则注入 | setRules (JNI) | AIDL setRules → JNI |
| 节点切换 | 重新连接 (stop+start) | **运行时 setNode 热切换** |
| 状态获取 | UI 轮询 JNI getStats | AIDL 轮询/回调 |
| 崩溃隔离 | 内核崩 = App 崩 | **内核崩不影响 UI** |

## 3. IPC 接口 (AIDL)

```aidl
// ICoreService.aidl
interface ICoreService {
    // ── 控制 ──
    int start();                       // 建 TUN + 启动内核 (用当前配置)
    void stop();                       // 停止 (撤 TUN)
    boolean setNode(String uri);       // 运行时注入/切换节点
    boolean setRules(String json);     // 注入路由规则 (现有格式)
    void setAutoSelect(boolean on, String method);  // 自动选节点
    String testNode(String uri, int timeoutMs);     // 测活
    boolean isRunning();

    // ── 状态 ──
    double[] getStats();               // [up,down,upRate,downRate,tcp,udp,dns]
    long getLatencyMs();
    String[] recentLogs();
    String[] getBuiltinDomains();
    long getBuiltinIpCount();

    // ── 事件 (UI 订阅) ──
    void registerCallback(ICoreCallback cb);
    void unregisterCallback(ICoreCallback cb);
}

interface ICoreCallback {
    void onStateChanged(boolean running);
    void onLog(String line);
    void onStats(double[] stats);
}
```

## 4. Rust 内核改造点

1. **节点热切换**: `Java_..._setNode(uri)` — 重建 Engine 并替换 TunStack 引用的引擎
   (TunStack 持有 Arc<Engine>, 用 RwLock<Arc<Engine>> 支持运行时替换)
2. **进程独立**: JNI 在 :core 进程加载, App 进程不再加载 libmirage_jni
3. **事件回调**: Rust → CoreService → AIDL 回调 (日志/状态推送到 UI)

## 5. 实施阶段

- [ ] P1: Rust setNode 热切换 + 现有接口整理
- [ ] P2: AIDL 接口 + CoreService (:core 进程, VpnService + JNI)
- [ ] P3: UI 迁移: CoreController 替换直接 JNI 调用
- [ ] P4: 事件回调 + 状态推送
- [ ] P5: 构建 + 真机验证 (连接/断开/热切节点/规则注入)
