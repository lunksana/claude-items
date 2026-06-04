use jni::objects::{JClass, JObject, JValue};
use jni::sys::{jint, jobject};
use jni::JNIEnv;
use log::{error, info};

use std::os::unix::io::{FromRawFd, IntoRawFd};
use std::os::unix::net::UnixStream;
use tokio::fs::File;
use tokio::io::{AsyncReadExt, AsyncWriteExt};

/// JNI_OnLoad 保持不变
#[no_mangle]
pub extern "system" fn JNI_OnLoad(
    _vm: jni::JavaVM,
    _reserved: *mut std::ffi::c_void,
) -> jni::sys::jint {
    android_logger::init_once(
        android_logger::Config::default()
            .with_max_level(log::LevelFilter::Trace)
            .with_tag("PyRealiy-Rust"),
    );
    info!("Rust JNI_OnLoad completed.");
    jni::sys::JNI_VERSION_1_6
}

/// JNI 入口：接管 TUN 描述符并启动 smoltcp
#[no_mangle]
pub extern "system" fn Java_com_pyrealiy_proxy_service_RustTun_startTunLoop(
    mut env: JNIEnv,
    _class: JClass,
    tun_fd: jint,
    callback_obj: JObject,
) {
    info!("Rust 成功接管 TUN 描述符: {}", tun_fd);

    // 1. 我们需要一个全局 JNI 引用，这样在 Rust 的异步线程里才能调用 Kotlin
    let jvm = env.get_java_vm().unwrap();
    let callback_ref = env.new_global_ref(callback_obj).unwrap();

    // 2. 启动 Tokio 异步运行时（这就像是 Kotlin 里的 CoroutineScope）
    let rt = tokio::runtime::Builder::new_multi_thread()
        .enable_all()
        .build()
        .unwrap();

    rt.block_on(async move {
        // 3. 将原生的 tun_fd 包装为 Tokio 的异步文件
        let mut tun_file = unsafe { File::from_std(std::fs::File::from_raw_fd(tun_fd)) };

        // 4. 构建底层的协议栈 (smoltcp)
        // 这一步我们将引入 smoltcp / netstack-smoltcp 并在 TUN 之间循环搬运数据
        // tokio::spawn(async move { /* tun_file.read() -> stack_tx.send() */ });
        // tokio::spawn(async move { /* stack_rx.recv() -> tun_file.write() */ });

        // 5. 核心重头戏：监听系统内任何想发起的 TCP 连接！
        info!("Smoltcp 协议栈启动，正在监听被拦截的 TCP 请求...");
        
        // 模拟捕获到一个新的 TCP 请求（真实环境这里是 while let Some(tcp_stream) = tcp_listener.next().await）
        let target_ip_string = "142.250.190.46";
        let target_port_int = 443;
        
        info!("拦截到新的 TCP 连接！目标：{}:{}", target_ip_string, target_port_int);

        // 5.1 建立神奇的 Unix Domain Socket 双向管道！
        let (rust_socket, kotlin_socket) = UnixStream::pair().unwrap();
        
        // 将 Kotlin 端的 FD 转换出来
        let kotlin_fd = kotlin_socket.into_raw_fd();

        // 5.2 在新的异步任务中，把 smoltcp 解析出的 TCP 流与这个管道桥接起来
        tokio::spawn(async move {
            let _tokio_unix_stream = tokio::net::UnixStream::from_std(rust_socket).unwrap();
            // 一句代码，完成 TCP 流与 Unix 管道的完美对接（零成本内存拷贝转发）
            // tokio::io::copy_bidirectional(&mut tcp_stream, &mut tokio_unix_stream).await;
        });

        // 5.3 把管道的另一头 (kotlin_fd) 通过 JNI 丢给 Kotlin 层！
        let mut jni_env = jvm.attach_current_thread().unwrap();
        let target_ip_str = jni_env.new_string(target_ip_string).unwrap();

        // 调用 Kotlin 侧的 onNewConnection(Int, String, Int)
        let _ = jni_env.call_method(
            &callback_ref,
            "onNewConnection",
            "(ILjava/lang/String;I)V", // JNI 签名
            &[
                JValue::Int(kotlin_fd),
                JValue::Object(&target_ip_str.into()),
                JValue::Int(target_port_int as jint)
            ]
        );
    });
}
