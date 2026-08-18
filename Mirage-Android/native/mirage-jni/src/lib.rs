//! Mirage-Android JNI 包装层。
//!
//! Kotlin 侧通过 `System.loadLibrary("mirage_jni")` 加载, 调用:
//! ```kotlin
//! object MirageCore {
//!     external fun start(tunFd: Int, server: String, port: Int, password: String,
//!                        sni: String, poolSize: Int, pfs: Boolean): Int
//!     external fun stop()
//!     external fun isRunning(): Boolean
//!     external fun isHealthy(): Boolean
//!     external fun latencyMs(): Long
//!     external fun setLogCallback(cb: (String) -> Unit)
//! }
//! ```
//!

use jni::objects::{JClass, JObject, JString};
use jni::sys::{jboolean, jint, jlong, jstring};
use jni::JNIEnv;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex, Once};

use mirage_core::engine::{Engine, NodeInfo};
use mirage_core::tun::{TunConfig, TunStack};
use mirage_core::node_uri::NodeUri;

pub mod protect_queue;

// ── 全局运行时状态 ──────────────────────────────────────────────────────────

static STARTED: AtomicBool = AtomicBool::new(false);
/// 引擎与 TUN 栈句柄 (stop 时置空)。
static RUNTIME: Mutex<Option<Arc<RunState>>> = Mutex::new(None);
static CURRENT_POOL_SIZE: std::sync::atomic::AtomicUsize = std::sync::atomic::AtomicUsize::new(8);
static CURRENT_NODE_URI: Mutex<Option<String>> = Mutex::new(None);

struct RunState {
    engine: Arc<Engine>,
    stack: Arc<TunStack>,
    /// tokio runtime 的保活句柄 (stop 时 drop)。
    rt: Arc<tokio::runtime::Runtime>,
}


// ── 工具 ───────────────────────────────────────────────────────────────────

// ── JNI 导出 ───────────────────────────────────────────────────────────────

use tracing_subscriber::layer::SubscriberExt;
use tracing_subscriber::util::SubscriberInitExt;
use tracing_subscriber::reload;
use tracing_subscriber::EnvFilter;

type ReloadHandle = reload::Handle<EnvFilter, tracing_subscriber::Registry>;
static RELOAD_HANDLE: Mutex<Option<ReloadHandle>> = Mutex::new(None);

/// 把 tracing 输出同时写进 App 日志面板 (monitor::global_logger) 与 stderr。
#[derive(Clone)]
struct PanelWriter;
impl<'a> tracing_subscriber::fmt::MakeWriter<'a> for PanelWriter {
    type Writer = PanelWriter;
    fn make_writer(&'a self) -> Self::Writer {
        PanelWriter
    }
}
impl std::io::Write for PanelWriter {
    fn write(&mut self, buf: &[u8]) -> std::io::Result<usize> {
        let s = String::from_utf8_lossy(buf);
        let _ = mirage_core::monitor::recent_logs_push(&s);
        std::io::stderr().write_all(buf)?;
        Ok(buf.len())
    }
    fn flush(&mut self) -> std::io::Result<()> {
        std::io::stderr().flush()
    }
}

/// 初始化日志系统 (幂等)。
fn init_logging() {
    static LOG_INIT: Once = Once::new();
    LOG_INIT.call_once(|| {
        let default_filter = EnvFilter::try_from_default_env()
            .unwrap_or_else(|_| "mirage_core=info,mirage_jni=info".into());
        let (filter_layer, reload_handle) = reload::Layer::new(default_filter);
        if let Ok(mut lock) = RELOAD_HANDLE.lock() {
            *lock = Some(reload_handle);
        }

        let _ = tracing_subscriber::registry()
            .with(filter_layer)
            .with(
                tracing_subscriber::fmt::layer()
                    .with_ansi(false)
                    .with_writer(PanelWriter),
            )
            .try_init();

        // panic hook: Rust panic 会 abort 进程 (闪退), 至少把堆栈写进日志便于定位
        std::panic::set_hook(Box::new(|info| {
            eprintln!("[mirage-jni] PANIC: {info}");
            let bt = std::backtrace::Backtrace::force_capture();
            eprintln!("[mirage-jni] stack:\n{bt}");
        }));
    });
}

pub fn update_log_level(level: &str) -> bool {
    init_logging();
    let filter_str = match level.to_ascii_lowercase().as_str() {
        "trace" => "mirage_core=trace,mirage_jni=trace",
        "debug" => "mirage_core=debug,mirage_jni=debug",
        "warn" => "mirage_core=warn,mirage_jni=warn",
        "error" => "mirage_core=error,mirage_jni=error",
        _ => "mirage_core=info,mirage_jni=info",
    };
    if let Ok(lock) = RELOAD_HANDLE.lock() {
        if let Some(handle) = lock.as_ref() {
            if let Ok(new_filter) = EnvFilter::try_new(filter_str) {
                let ok = handle.reload(new_filter).is_ok();
                tracing::info!("[mirage-jni] 日志过滤级别已切换为: {level} (filter: {filter_str})");
                return ok;
            }
        }
    }
    false
}

/// `boolean setLogLevel(String level)` — 动态热切换 Rust 日志级别 (trace/debug/info/warn/error)。
#[no_mangle]
pub extern "system" fn Java_com_mirage_android_core_MirageNative_setLogLevel(
    mut env: JNIEnv,
    _class: JClass,
    level: JString,
) -> jboolean {
    let level_str: String = match env.get_string(&level) {
        Ok(s) => s.into(),
        Err(_) => return 0,
    };
    if update_log_level(&level_str) {
        1
    } else {
        0
    }
}

/// `int start(int tunFd, String uri, int poolSize)` — uri 为 `mirage://密码@host:port?sni=...`。
///
/// 返回 0 = 成功, 负数 = 错误码。
#[no_mangle]
pub extern "system" fn Java_com_mirage_android_core_MirageNative_start(
    mut env: JNIEnv,
    _class: JClass,
    tun_fd: jint,
    uri: JString,
    pool_size: jint,
) -> jint {
    init_logging();
    if STARTED.swap(true, Ordering::SeqCst) {
        // 幂等: 已在跑, 返回 0 (Kotlin 应先 stop)
        return 0;
    }

    let uri_str: String = match env.get_string(&uri) {
        Ok(s) => s.into(),
        Err(e) => {
            tracing::error!("读取 URI 失败: {e}");
            STARTED.store(false, Ordering::SeqCst);
            return -1;
        }
    };
    let node_uri = match NodeUri::parse(&uri_str) {
        Ok(u) => u,
        Err(e) => {
            tracing::error!("节点 URI 解析失败: {e}");
            STARTED.store(false, Ordering::SeqCst);
            return -1;
        }
    };
    let mut node = NodeInfo::from_node_uri(&node_uri);
    if pool_size > 0 {
        CURRENT_POOL_SIZE.store(pool_size as usize, Ordering::Relaxed);
        node.pool_size = pool_size as usize;
    } else {
        node.pool_size = CURRENT_POOL_SIZE.load(Ordering::Relaxed);
    }
    *CURRENT_NODE_URI.lock().unwrap_or_else(|e| e.into_inner()) = Some(uri_str);

    // 注册 protect 回调: **同步 JNI 调用** Kotlin 侧 `MirageNative.protectFd(fd)`。
    // 必须在 socket connect 之前完成 (SO_MARK 影响路由), 轮询队列来不及。
    let vm = match env.get_java_vm() {
        Ok(v) => v,
        Err(e) => {
            tracing::error!("获取 JavaVM 失败: {e}");
            STARTED.store(false, Ordering::SeqCst);
            return -5;
        }
    };
    let vm = std::sync::Arc::new(vm);
    let class = match env.find_class("com/mirage/android/core/MirageNative") {
        Ok(c) => c,
        Err(e) => {
            tracing::error!("查找 MirageNative 类失败: {e}");
            STARTED.store(false, Ordering::SeqCst);
            return -6;
        }
    };
    let class_ref = match env.new_global_ref(&class) {
        Ok(r) => r,
        Err(e) => {
            tracing::error!("创建类全局引用失败: {e}");
            STARTED.store(false, Ordering::SeqCst);
            return -7;
        }
    };
    let vm_for_protect = std::sync::Arc::clone(&vm);
    let class_for_protect = class_ref.clone();
    mirage_core::protect::set_protect_callback(Box::new(move |fd| {
        use jni::objects::JValue;
        // 从 tokio worker 线程 attach JVM 并同步调用 Java 侧 protect。
        // (attach_current_thread 每次 attach/detach, 建连频率低, 开销可接受)
        let r = vm_for_protect.attach_current_thread().and_then(|mut env| {
            // jni 0.21: call_static_method(class, name, sig, args)
            env.call_static_method(
                &class_for_protect,
                "protectFd",
                "(I)V",
                &[JValue::Int(fd)],
            )
        });
        if let Err(e) = r {
            eprintln!("[mirage-jni] protect({fd}) 失败: {e}");
        }
    }));
    // 队列方式保留 (备用, 不再使用)
    crate::protect_queue::clear();

    // 专用 tokio runtime (独立线程, 不占主线程)。必须先建 runtime 再在其内部
    // 调 Engine::new —— WarmPool::new 会 tokio::spawn 预热任务, 无 runtime 上下文
    // 直接 panic (JNI 边界 panic = abort = App 闪退, 真机实测踩坑)。
    let rt = match tokio::runtime::Builder::new_multi_thread()
        .worker_threads(2)
        .enable_all()
        .thread_name("mirage-core")
        .build()
    {
        Ok(r) => Arc::new(r),
        Err(e) => {
            tracing::error!("tokio runtime 创建失败: {e}");
            STARTED.store(false, Ordering::SeqCst);
            return -4;
        }
    };

    let rt2 = rt.clone();
    let result = rt2.block_on(async move {
        // Engine::new + TunStack::start 都在 runtime 内执行
        let engine = match Engine::new(&node) {
            Ok(e) => e,
            Err(e) => {
                tracing::error!("引擎初始化失败: {e}");
                return Err(e);
            }
        };
        let cfg = TunConfig::default();
        let stack = TunStack::start(engine.clone(), cfg, tun_fd as i32).await?;
        Ok((engine, stack))
    });

    let (engine, stack) = match result {
        Ok(v) => v,
        Err(e) => {
            tracing::error!("引擎/TUN 启动失败: {e}");
            STARTED.store(false, Ordering::SeqCst);
            return -3;
        }
    };

    *RUNTIME.lock().unwrap_or_else(|e| e.into_inner()) = Some(Arc::new(RunState {
        engine,
        stack,
        rt,
    }));
    tracing::info!("MirageCore 已启动");
    0
}

/// `void stop()`
#[no_mangle]
pub extern "system" fn Java_com_mirage_android_core_MirageNative_stop(
    _env: JNIEnv,
    _class: JClass,
) {
    if let Some(state) = RUNTIME.lock().unwrap_or_else(|e| e.into_inner()).take() {
        state.stack.stop();
        // drop runtime (停止 tokio worker)
        drop(state);
        STARTED.store(false, Ordering::SeqCst);
        tracing::info!("MirageCore 已停止");
    }
}

/// `boolean isRunning()`
#[no_mangle]
pub extern "system" fn Java_com_mirage_android_core_MirageNative_isRunning(
    _env: JNIEnv,
    _class: JClass,
) -> jboolean {
    STARTED.load(Ordering::SeqCst) as jboolean
}

/// `boolean isHealthy()`
#[no_mangle]
pub extern "system" fn Java_com_mirage_android_core_MirageNative_isHealthy(
    _env: JNIEnv,
    _class: JClass,
) -> jboolean {
    let guard = RUNTIME.lock().unwrap_or_else(|e| e.into_inner());
    match guard.as_ref() {
        Some(s) => s.engine.is_healthy() as jboolean,
        None => 0,
    }
}

/// `long latencyMs()` — RTT 毫秒, -1 = 未知
#[no_mangle]
pub extern "system" fn Java_com_mirage_android_core_MirageNative_latencyMs(
    _env: JNIEnv,
    _class: JClass,
) -> jlong {
    let guard = RUNTIME.lock().unwrap_or_else(|e| e.into_inner());
    match guard.as_ref() {
        Some(s) => s.engine.latency_ms().map(|v| v as jlong).unwrap_or(-1),
        None => -1,
    }
}

/// `String[] recentLogs()` — 最近日志 (Kotlin 日志面板)
#[no_mangle]
pub extern "system" fn Java_com_mirage_android_core_MirageNative_recentLogs(
    mut env: JNIEnv,
    _class: JClass,
) -> jni::sys::jobjectArray {
    let logs = mirage_core::monitor::recent_logs();
    let arr = match env.new_object_array(logs.len() as i32, "java/lang/String", JObject::null()) {
        Ok(a) => a,
        Err(_) => return jni::sys::jobjectArray::default(),
    };
    if arr.is_null() {
        return arr.into_raw();
    }
    for (i, l) in logs.iter().enumerate() {
        let s = env.new_string(l).unwrap_or_else(|_| JString::from(JObject::null()));
        if !s.is_null() {
            let _ = env.set_object_array_element(&arr, i as i32, s);
        }
    }
    arr.into_raw()
}

/// 供 Kotlin 获取待 protect 的 fd 队列 (VpnService.protect 必须在 Java 侧调用)。
/// `int[] drainProtectFds()`
#[no_mangle]
pub extern "system" fn Java_com_mirage_android_core_MirageNative_drainProtectFds(
    mut env: JNIEnv,
    _class: JClass,
) -> jni::sys::jintArray {
    let fds = crate::protect_queue::drain();
    let arr = match env.new_int_array(fds.len() as i32) {
        Ok(a) => a,
        Err(_) => return jni::sys::jintArray::default(),
    };
    let raw: Vec<jint> = fds.into_iter().map(|v| v as jint).collect();
    if !raw.is_empty() {
        let _ = env.set_int_array_region(&arr, 0, &raw);
    }
    arr.into_raw()
}

/// `double[] getStats()` — 流量统计: [up_total, down_total, up_rate, down_rate,
/// tcp_conns, udp_flows, dns_queries] (速率单位 B/s)。
#[no_mangle]
pub extern "system" fn Java_com_mirage_android_core_MirageNative_getStats(
    mut env: JNIEnv,
    _class: JClass,
) -> jni::sys::jdoubleArray {
    let (up, down, up_rate, down_rate) = mirage_core::monitor::sample();
    let tcp = mirage_core::tun::tcp::TCP_ACTIVE.load(std::sync::atomic::Ordering::Relaxed) as f64;
    let udp = mirage_core::tun::udp::flow_count_global() as f64;
    let dns = mirage_core::tun::dns::DNS_QUERIES.load(std::sync::atomic::Ordering::Relaxed) as f64;
    let data = [up as f64, down as f64, up_rate, down_rate, tcp, udp, dns];
    match env.new_double_array(data.len() as i32) {
        Ok(arr) => {
            let _ = env.set_double_array_region(&arr, 0, &data);
            arr.into_raw()
        }
        Err(_) => jni::sys::jdoubleArray::default(),
    }
}

/// `boolean setNode(String uri)` — 运行时热切换节点 (重建引擎并替换, 无需断开)。
/// 现有隧道保留到自然断开, 新连接/新流走新节点。
#[no_mangle]
pub extern "system" fn Java_com_mirage_android_core_MirageNative_setNode(
    mut env: JNIEnv,
    _class: JClass,
    uri: JString,
) -> jboolean {
    let uri_str: String = match env.get_string(&uri) {
        Ok(x) => x.into(),
        Err(_) => return 0,
    };
    let node_uri = match mirage_core::node_uri::NodeUri::parse(&uri_str) {
        Ok(u) => u,
        Err(_) => return 0,
    };
    let mut node = mirage_core::engine::NodeInfo::from_node_uri(&node_uri);
    node.pool_size = CURRENT_POOL_SIZE.load(Ordering::Relaxed);
    *CURRENT_NODE_URI.lock().unwrap_or_else(|e| e.into_inner()) = Some(uri_str);

    // 在建的 runtime 上重建引擎 (Engine::new 需要 tokio 上下文)
    let guard = RUNTIME.lock().unwrap_or_else(|e| e.into_inner());
    let Some(state) = guard.as_ref() else { return 0 };
    let result = state.rt.block_on(async move {
        mirage_core::engine::Engine::new(&node).map_err(|e| e.to_string())
    });
    match result {
        Ok(new_engine) => {
            // 同步注入最新规则
            state.stack.swap_engine(new_engine);
            1
        }
        Err(e) => {
            tracing::error!("setNode 引擎重建失败: {e}");
            0
        }
    }
}

/// `boolean setPoolSize(int poolSize)` — 动态热更新当前运行中的连接池容量。
#[no_mangle]
pub extern "system" fn Java_com_mirage_android_core_MirageNative_setPoolSize(
    _env: JNIEnv,
    _class: JClass,
    pool_size: jint,
) -> jboolean {
    if pool_size <= 0 {
        return 0;
    }
    let size = pool_size as usize;
    CURRENT_POOL_SIZE.store(size, Ordering::Relaxed);

    let guard = RUNTIME.lock().unwrap_or_else(|e| e.into_inner());
    if let Some(state) = guard.as_ref() {
        let uri_opt = CURRENT_NODE_URI.lock().unwrap_or_else(|e| e.into_inner()).clone();
        if let Some(uri_str) = uri_opt {
            if let Ok(node_uri) = mirage_core::node_uri::NodeUri::parse(&uri_str) {
                let mut node = mirage_core::engine::NodeInfo::from_node_uri(&node_uri);
                node.pool_size = size;
                let result = state.rt.block_on(async move {
                    mirage_core::engine::Engine::new(&node).map_err(|e| e.to_string())
                });
                match result {
                    Ok(new_engine) => {
                        state.stack.swap_engine(new_engine);
                        tracing::info!("MirageCore 已热更新连接池容量为: {size}");
                        return 1;
                    }
                    Err(e) => {
                        tracing::error!("setPoolSize 引擎重建失败: {e}");
                        return 0;
                    }
                }
            }
        }
    }
    1
}

/// `int getPoolSize()` — 获取当前内核连接池容量。
#[no_mangle]
pub extern "system" fn Java_com_mirage_android_core_MirageNative_getPoolSize(
    _env: JNIEnv,
    _class: JClass,
) -> jint {
    CURRENT_POOL_SIZE.load(Ordering::Relaxed) as jint
}

/// `long testNode(String uri, int timeoutMs)` — 完整协议握手测活。
/// 返回 RTT 毫秒; -1 = 节点不可用/握手失败。供自动节点选择用。
#[no_mangle]
pub extern "system" fn Java_com_mirage_android_core_MirageNative_testNode(
    mut env: JNIEnv,
    _class: JClass,
    uri: JString,
    timeout_ms: jint,
) -> jlong {
    use mirage_core::engine::NodeInfo;
    let uri_str: String = match env.get_string(&uri) {
        Ok(x) => x.into(),
        Err(_) => return -1,
    };
    let node_uri = match mirage_core::node_uri::NodeUri::parse(&uri_str) {
        Ok(u) => u,
        Err(_) => return -1,
    };
    let node = NodeInfo::from_node_uri(&node_uri);

    // 临时 runtime (测活在连接前, 无全局 runtime)
    let rt = match tokio::runtime::Builder::new_current_thread().enable_all().build() {
        Ok(r) => r,
        Err(_) => return -1,
    };
    let timeout = std::time::Duration::from_millis(timeout_ms.max(1000) as u64);
    let result = rt.block_on(async move {
        let engine = match mirage_core::engine::Engine::new(&node) {
            Ok(e) => e,
            Err(_) => return -1i64,
        };
        let outbound = match engine.outbounds.get(engine.default_tag()) {
            Some(o) => o.clone(),
            None => return -1,
        };
        let leaf = outbound.resolve_leaf();
        let mirage_core::proxy::outbound::OutboundNode::Mirage { pool, .. } = &*leaf else {
            return -1;
        };
        let t0 = std::time::Instant::now();
        match tokio::time::timeout(timeout, pool.get()).await {
            Ok(Ok(_)) => t0.elapsed().as_millis() as i64,
            _ => -1,
        }
    });
    result
}

/// `String[] getBuiltinDomains()` — 内置国内域名列表 (规则界面展示)。
#[no_mangle]
pub extern "system" fn Java_com_mirage_android_core_MirageNative_getBuiltinDomains(
    mut env: JNIEnv,
    _class: JClass,
) -> jni::sys::jobjectArray {
    let list = mirage_core::direct::builtin_domains();
    let arr = match env.new_object_array(list.len() as i32, "java/lang/String", JObject::null()) {
        Ok(a) => a,
        Err(_) => return jni::sys::jobjectArray::default(),
    };
    for (i, d) in list.iter().enumerate() {
        if let Ok(s) = env.new_string(d) {
            let _ = env.set_object_array_element(&arr, i as i32, s);
        }
    }
    arr.into_raw()
}

/// `long getBuiltinIpCount()` — 内置中国 IP 段数量。
#[no_mangle]
pub extern "system" fn Java_com_mirage_android_core_MirageNative_getBuiltinIpCount(
    _env: JNIEnv,
    _class: JClass,
) -> jlong {
    mirage_core::direct::builtin_ip_count() as jlong
}

/// `boolean setRules(String json)` — 设置自定义分流规则 (App 启动/改规则时调用)。
#[no_mangle]
pub extern "system" fn Java_com_mirage_android_core_MirageNative_setRules(
    mut env: JNIEnv,
    _class: JClass,
    json: JString,
) -> jboolean {
    let json_str: String = match env.get_string(&json) {
        Ok(s) => s.into(),
        Err(_) => return 0,
    };
    if mirage_core::direct::set_custom_rules(&json_str) {
        1
    } else {
        0
    }
}

/// `String version()`
#[no_mangle]
pub extern "system" fn Java_com_mirage_android_core_MirageNative_version(
    env: JNIEnv,
    _class: JClass,
) -> jstring {
    let v = format!(
        "mirage-core {} / vendored {}",
        env!("CARGO_PKG_VERSION"),
        mirage_core::PROTOCOL_SYNC.lines().nth(2).unwrap_or("")
    );
    env.new_string(v).unwrap_or_else(|_| JString::from(JObject::null())).into_raw()
}

/// `String getConnectionsJson()` — 获取最近/活跃连接信息列表 (JSON 字符串)。
#[no_mangle]
pub extern "system" fn Java_com_mirage_android_core_MirageNative_getConnectionsJson(
    env: JNIEnv,
    _class: JClass,
) -> jstring {
    let json = mirage_core::monitor::get_connections_json();
    env.new_string(json).unwrap_or_else(|_| JString::from(JObject::null())).into_raw()
}

/// `boolean setBlockQuic(boolean block)` — 设置是否全局拦截 QUIC (UDP 443)。
#[no_mangle]
pub extern "system" fn Java_com_mirage_android_core_MirageNative_setBlockQuic(
    _env: JNIEnv,
    _class: JClass,
    block: jboolean,
) -> jboolean {
    mirage_core::direct::set_block_quic(block != 0);
    1
}

/// `boolean isBlockQuic()` — 查询当前是否开启 QUIC 拦截。
#[no_mangle]
pub extern "system" fn Java_com_mirage_android_core_MirageNative_isBlockQuic(
    _env: JNIEnv,
    _class: JClass,
) -> jboolean {
    if mirage_core::direct::is_block_quic() {
        1
    } else {
        0
    }
}
