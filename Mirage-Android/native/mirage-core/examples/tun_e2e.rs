//! 端到端测试入口: 在 netns 里创建 TUN → 起 TunStack → 打 mirage-rs lite-server。
//!
//! 配合 `scripts/test-e2e.sh` 使用。流程:
//!   scripts/test-e2e.sh setup    # 建 netns + veth + NAT + 起本地 lite-server
//!   ip netns exec mirage-test cargo run --example tun_e2e -- \
//!       --server 10.99.0.1 --port 8443 --password test1234
//!
//! 验证: 在 netns 里 `curl https://www.google.com` (DNS 走 fake-IP → 隧道)。

use std::os::fd::RawFd;
use std::os::unix::io::AsRawFd;
use std::os::unix::io::FromRawFd;

use clap::Parser;
use mirage_core::engine::{Engine, NodeInfo};
use mirage_core::tun::TunConfig;
use mirage_core::tun::TunStack;
use tracing_subscriber::EnvFilter;

#[derive(Parser)]
struct Args {
    #[arg(long, default_value = "10.99.0.1")]
    server: String,
    #[arg(long, default_value = "8443")]
    port: u16,
    #[arg(long, default_value = "test1234")]
    password: String,
    #[arg(long, default_value = "www.apple.com")]
    sni: String,
}

/// 创建 Linux TUN 设备 (需要 root/CAP_NET_ADMIN)。
fn create_tun(name: &str) -> std::io::Result<(RawFd, String)> {
    use std::os::unix::io::FromRawFd;
    let dev = std::fs::OpenOptions::new().read(true).write(true).open("/dev/net/tun")?;
    let tun_fd = dev.as_raw_fd();
    // TUNSETIFF
    const TUNSETIFF: libc::c_ulong = 0x4004_54ca;
    const IFF_TUN: libc::c_int = 0x0001;
    const IFF_NO_PI: libc::c_int = 0x1000;
    #[repr(C)]
    struct IfReq {
        name: [u8; 16],
        flags: libc::c_short,
        _pad: [u8; 22],
    }
    let mut req = IfReq { name: [0u8; 16], flags: (IFF_TUN | IFF_NO_PI) as libc::c_short, _pad: [0u8; 22] };
    let name_bytes = name.as_bytes();
    req.name[..name_bytes.len()].copy_from_slice(name_bytes);
    let ret = unsafe { libc::ioctl(tun_fd, TUNSETIFF, &mut req as *mut _ as *mut libc::c_void) };
    if ret < 0 {
        return Err(std::io::Error::last_os_error());
    }
    let name = std::str::from_utf8(&req.name)
        .map(|s| s.trim_end_matches('\0').to_string())
        .unwrap_or_else(|_| name.to_string());
    // dup 出可被 tokio 用的 fd
    let dup_fd = unsafe { libc::dup(tun_fd) };
    drop(dev);
    Ok((dup_fd, name))
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    let args = Args::parse();
    tracing_subscriber::fmt()
        .with_env_filter(EnvFilter::from_default_env().add_directive("mirage_core=debug".parse()?))
        .init();

    // 1. 建 TUN
    let (tun_fd, _name) = create_tun("mirage0")?;
    println!("[e2e] TUN 设备已创建 fd={tun_fd}");

    // 2. 建引擎 (指向本地 lite-server)
    let node = NodeInfo {
        tag: "proxy".into(),
        server: args.server.clone(),
        server_port: args.port,
        password: args.password.clone(),
        sni: args.sni.clone(),
        pool_size: 4,
        pfs: false,
    };
    let engine = Engine::new(&node)?;
    println!("[e2e] 引擎已构建 (server={}:{})", args.server, args.port);

    // 3. 起 TUN 引擎
    let cfg = TunConfig::default();
    let stack = TunStack::start(engine.clone(), cfg, tun_fd).await?;
    println!("[e2e] TUN 引擎已启动 (198.18.0.1, DNS 198.18.0.1)");

    // 4. 保活
    tokio::signal::ctrl_c().await.ok();
    stack.stop();
    println!("[e2e] 已停止");
    Ok(())
}
