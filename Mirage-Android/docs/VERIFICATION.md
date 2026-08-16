# 端到端验证记录

**日期**: 2026-08-14 · **协议版本**: Mirage-rs v0.9.2

## 结论

Mirage-Android 的移动端裁剪内核 (`mirage-core`) 已在 **systemd-nspawn 隔离容器**中完成端到端验证:
**TUN → smoltcp 用户态协议栈 → Mirage 隧道 → lite-server → 真实互联网**, HTTPS 全通。

## 测试拓扑 (systemd-nspawn, 完全隔离, 不动宿主配置)

```
[宿主] ve-mirage-* (10.88.0.1/24) ──veth── host0 (10.88.0.2/24) [nspawn 容器]
        ├─ lite-server (0.0.0.0:8443, 宿主跑, 不被 TUN 卷进去)
        └─ NAT → 外网 (仅容器出网)
  容器内: mirage0 (198.18.0.1/32) ──TUN引擎──▶ lite-server(10.88.0.1:8443)
        └─ 默认路由走 TUN, DNS=198.19.0.53 (fake-IP)
```

## 验证结果

| 项目 | 结果 |
|------|------|
| fake-IP DNS (198.19.0.53 → 198.18.0.x) | ✅ getent 返回 198.18.0.2 |
| HTTPS google.com (经隧道) | ✅ 200 |
| HTTPS cloudflare.com (经隧道) | ✅ 200 |
| HTTPS baidu.com (经隧道) | ✅ 200 |
| TCP 隧道流量统计 (↑/↓) | ✅ 正常 (↑883B ↓86.2K 等) |
| 连接建立/关闭 | ✅ [TUN-TCP] 日志正常 |

## 过程中发现并修复的关键问题 (对 iOS 移植同样重要)

1. **TUN DNS 地址不能是接口本地地址** — 分配给接口的地址被内核当本地地址直接投递 (回
   ICMP 端口不可达), 包不进 TUN。解法: DNS 地址用独立的 198.19.0.53 (fake-IP /16 之外)。
2. **IPv4/IPv6 头源目的地址写反** — 经典低级错误: IP 头 offset 12-15 是**源地址**、
   16-19 是**目的地址** (IPv6: 8-23 源, 24-39 目的)。写反导致回程包地址颠倒, 永远到不了
   客户端。有回归测试 `reply_ip_header_direction`。
3. **DNS 应答 Question 段回显** — 把整个 query[12..] (含 EDNS OPT 附加段) 当 Question
   复制会使应答格式非法, curl/glibc 拒绝。只回显到 QNAME 结尾 + QTYPE/QCLASS。
4. **smoltcp 必须开 `set_any_ip(true)`** — 透明代理场景 TUN 里目的地址是真实目标 (非本机
   地址), smoltcp 默认只认接口地址; any_ip 让 SYN 能进、SYN-ACK 能以任意源地址发出。
5. **测试环境的 lite-server 不能在 TUN 网络里** — 否则服务器的 CamouflagePool (连
   www.apple.com 取 TLS 模板) 也会走 TUN 造成递归。测试时 server 放宿主/干净网络。

## 复现

```bash
scripts/test-e2e.sh   # 需 root; 内部用 systemd-nspawn 隔离, 不动宿主网络配置
```
