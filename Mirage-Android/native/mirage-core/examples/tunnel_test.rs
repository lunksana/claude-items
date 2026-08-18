// 用 mirage-core 直连服务器, 复现隧道握手 (定位 early eof 根因)
use mirage_core::engine::{Engine, NodeInfo};
use mirage_core::proxy::outbound::OutboundNode;

#[tokio::main]
async fn main() {
    let node = NodeInfo {
        tag: "proxy".into(),
        server: std::env::var("MIRAGE_SERVER").unwrap_or("117.55.230.75".into()),
        server_port: std::env::var("MIRAGE_PORT").unwrap_or("8443".into()).parse().unwrap(),
        password: std::env::var("MIRAGE_PWD").unwrap_or("d029c98fd9fd3104cebf7ebb2ce632cd".into()),
        sni: std::env::var("MIRAGE_SNI").unwrap_or("speedtest.net".into()),
        pool_size: 1,
        pfs: false,
    };
    eprintln!("[test] 连接 {}:{} (sni={})", node.server, node.server_port, node.sni);
    let engine = match Engine::new(&node) {
        Ok(e) => e,
        Err(e) => { eprintln!("[test] Engine 失败: {e}"); return; }
    };
    let outbound = engine.outbounds.get("proxy").expect("出站");
    let leaf = outbound.resolve_leaf();
    let OutboundNode::Mirage { pool, .. } = &*leaf else { return };
    // 等预热 (异步补货)
    tokio::time::sleep(std::time::Duration::from_millis(1500)).await;
    match tokio::time::timeout(std::time::Duration::from_secs(10), pool.get()).await {
        Ok(Ok(t)) => {
            eprintln!("[test] ✅ 隧道建立成功");
            drop(t);
        }
        Ok(Err(e)) => eprintln!("[test] ❌ 隧道失败: {e}"),
        Err(_) => eprintln!("[test] ❌ 隧道超时"),
    }
}
