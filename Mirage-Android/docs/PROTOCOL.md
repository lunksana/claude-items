# Mirage 协议 (客户端视角)

> 本文从**移动客户端实现**角度描述 Mirage 协议, 与 `mirage-core` 的 vendored 代码一一
> 对应。服务端细节见 Mirage-rs 仓库。**协议升级时以 vendored 代码为准** (见
> `native/mirage-core/src/vendor/SYNC.md`)。

## 1. 连接建立 (隧道)

客户端 → 服务端: 一条普通 TCP 连接 (经 `VpnService.protect` 保护)。

### 1.1 伪装 TLS 握手

客户端先发一个**字节级仿真真实浏览器**的 TLS 1.3 ClientHello (经 `tls_raw.rs` 构造,
从真实站点的 ClientHello 模板 + 随机化):

```
[ClientHello 仿真字节] (SNI = 配置的 camouflage_host, 如 www.apple.com)
[64 字节 fake tail]    (build_fake_client_tail, 填充到 TLS record 边界)
```

服务端的行为:
- 校验通过 → 继续 Mirage 握手 (加密信道)
- 校验失败 → **转发到真实站点** (camouflage), 主动探测看到真站响应

### 1.2 加密信道握手 (全部在 TLS 仿真流之上)

```
客户端 → 服务端: 会话令牌 (hello_auth: 口令派生 + 时间戳抗重放, 16B tag)
                 (PFS 开时: 客户端 X25519 临时公钥, pfs.rs)
服务端 → 客户端: 密钥派生确认 + TIME_SYNC (时间同步, 加密信道内下发)
协商: cipher agility (两端有 AES-NI → AES-256-GCM, 否则 ChaCha20-Poly1305)
```

密钥派生: 口令 + HKDF → 会话密钥; PFS 时再混入 ECDH 共享秘密。

### 1.3 隧道池 (WarmPool)

- 客户端维护一个**预热 TCP 隧道池** (`pool_size`, 默认 4): 启动即建, 空闲复用
- 每条连接走 Zero-RTT: 直接从池里取一条隧道, 发目标头即用 (无额外握手延迟)
- 网络变化 (移动端 Wi-Fi↔蜂窝) 时旧隧道失效: 靠 stale 探测 + 首写重试兜底
  (移动端裁剪掉了 netlink 监听)

## 2. 数据面

### 2.1 TCP

```
[2B 目标长度][目标 host:port]   ← 首帧, 服务端据此远程解析并连接
[加密分帧数据流]                ← ChaCha20-Poly1305 / AES-256-GCM 帧
[close_notify]                  ← 半关闭传播 (FIN 语义)
```

目标:
- 域名 (客户端 fake-IP 反查得到) → 服务端**远程解析** (抗 DNS 污染)
- 裸 IP → 服务端直接连

### 2.2 UDP

```
隧道首帧: [0x00]  (UDP 模式哨兵)
每帧:     [2B bodyLen][ATYP][ADDR][2B port][payload]
          ATYP=0x03 域名 → 服务端远程解析
          ATYP=0x01/0x04 IP → 服务端直接构造地址
```

- 移动端每个 UDP 流 (client,dst) 一条隧道 (与上游 per-flow 一致; UDP mux 是服务端/
  透明网关侧特性, 移动端 v1 不启用 — 服务端两者都支持)
- 回程: 服务端把 payload 原样发回隧道, 客户端解帧后**伪源** (原目标地址) 构包回 TUN

## 3. fake-IP DNS

```
App 查 DNS → 198.19.0.53 (TUN DNS) → 引擎应答 198.18.0.0/16 内的 fake-IP
之后 App 连 fake-IP → 引擎反查域名 → 隧道携带域名 → 服务端干净网络解析
```

- AAAA 查询返回空 answer (NOERROR), 强制回落 IPv4 (fake-IP 只做 v4)
- DNS 应答的 Question 段只回显到 QNAME 结尾 (EDNS 附加段必须剥离, 否则格式非法)

## 4. 协议版本演进

| 特性 | 移动端支持 |
|---|---|
| TLS 1.3 ClientHello 仿真 | ✅ (三 profile 轮换 + 后量子 key_share) |
| 多浏览器 Profile 轮换 | ✅ |
| ChaCha20-Poly1305 | ✅ |
| AES-256-GCM (cipher agility) | ✅ |
| PFS (一次性 X25519) | ✅ (须与服务端同开) |
| UDP 中继 | ✅ |
| UDP mux (v0.9.0) | ⬜ 未启用 (服务端兼容, 移动端走 legacy per-flow) |
| TLS record padding (v0.8.0) | ⬜ 未启用 (默认关) |
| TCP Brutal CC | ⬜ 移动端无内核模块, 不支持 (配置会被安全忽略) |
| WireGuard / SS 上游 | ⬜ 服务端特性, 客户端不涉及 |

**客户端与服务端的互通要求**: 密码 + camouflage_host(SNI) 一致; PFS 两端同开;
端口一致。
