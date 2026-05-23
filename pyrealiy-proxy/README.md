# PyReality

基于 Python 的抗审查代理，融合 Shadow-TLS 和 Reality 两种协议的核心思路：

- **零延迟认证**：将 Poly1305 token 嵌入 TLS ClientHello 的 `legacy_session_id`，服务端在第一个数据包即完成身份验证，不产生额外 RTT
- **零延迟伪装**：预先缓存真实站点的 TLS 握手记录，GFW 探测时本地直接回放，响应时延与真实站点完全一致
- **加密信道**：ChaCha20-Poly1305 + HKDF 会话密钥，每条连接密钥独立
- **TCP Brutal**：可选的固定速率拥塞控制，在高丢包跨境链路上维持稳定吞吐
- **连接池**：客户端预建 N 条已认证隧道，SOCKS5 请求到来时零等待取用
- **域名 + IP 分流**：规则内嵌在配置文件中，支持精确/后缀/关键词/CIDR/GeoSite/GeoIP，后缀匹配使用 Bloom Filter 预过滤

---

## 要求

| 项目 | 最低版本 |
|---|---|
| Python | 3.9 |
| cryptography | 42.0.0 |
| uvloop（可选） | 任意 |
| tcp_brutal 内核模块（可选） | Linux 内核 ≥ 4.9 |

```bash
pip install cryptography uvloop   # uvloop 仅 Linux/macOS 有效
```

---

## 快速部署

运行交互式向导，自动检测环境、安装 TCP Brutal、生成配置文件：

```bash
python setup.py
```

向导会询问部署角色（服务端 / 客户端），客户端配置时还会引导完成分流规则选择（列出常用 GeoSite/GeoIP tag 供逐项勾选），最终写出完整的配置文件。

---

## 手动配置

### 服务端（墙外 VPS）

编辑 `config_server.json`：

```json
{
    "listen_host": "0.0.0.0",
    "listen_port": 443,
    "password": "your-strong-password-here",
    "camouflage_host": "www.apple.com",
    "camouflage_port": 443,
    "brutal_rate_bps": 0
}
```

| 字段 | 说明 |
|---|---|
| `listen_port` | 监听端口，建议 443 |
| `password` | 连接密码，客户端必须一致 |
| `camouflage_host` | 伪装域名，服务端会从此站点缓存 TLS 握手记录用于回放 |
| `camouflage_port` | 伪装站点端口，通常 443 |
| `brutal_rate_bps` | TCP Brutal 单连接速率（字节/秒），0 表示禁用 |

启动：

```bash
python server.py                        # 默认读 config_server.json
python server.py /path/to/config.json   # 指定配置文件
```

---

### 客户端（本地）

编辑 `config_client.json`：

```json
{
    "socks5_host": "0.0.0.0",
    "socks5_port": 1080,
    "server_host": "your.server.ip",
    "server_port": 443,
    "password": "your-strong-password-here",
    "camouflage_host": "www.apple.com",
    "brutal_rate_bps": 8000000,
    "brutal_pool_size": 20,

    "geosite_dir": ".geosite",
    "geosite_update_days": 7,

    "geosite_sources": [
        {
            "name": "loyalsoldier",
            "url": "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geosite.dat"
        }
    ],
    "geoip_sources": [
        {
            "name": "loyalsoldier",
            "url": "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geoip.dat"
        }
    ],

    "rules": [
        "GEOSITE,loyalsoldier:category-ads-all,REJECT",

        "GEOSITE,loyalsoldier:private,DIRECT",
        "GEOIP,loyalsoldier:private,DIRECT",

        "GEOSITE,loyalsoldier:cn,DIRECT",
        "GEOSITE,loyalsoldier:apple-cn,DIRECT",
        "GEOSITE,loyalsoldier:google-cn,DIRECT",

        "GEOIP,loyalsoldier:cn,DIRECT",

        "IP-CIDR,127.0.0.0/8,DIRECT",
        "IP-CIDR,10.0.0.0/8,DIRECT",
        "IP-CIDR,172.16.0.0/12,DIRECT",
        "IP-CIDR,192.168.0.0/16,DIRECT",

        "FINAL,PROXY"
    ]
}
```

| 字段 | 说明 |
|---|---|
| `socks5_host` | SOCKS5 监听地址，`0.0.0.0` 允许局域网内其他设备使用 |
| `socks5_port` | SOCKS5 监听端口 |
| `server_host/port` | 服务端地址 |
| `password` | 与服务端一致 |
| `camouflage_host` | 与服务端一致 |
| `brutal_rate_bps` | 单连接速率，0 禁用；建议 5–10 Mbps |
| `brutal_pool_size` | 预建连接数，总吞吐 ≈ `pool_size × rate_bps` |
| `geosite_dir` | GeoSite/GeoIP 缓存目录，默认 `.geosite` |
| `geosite_update_days` | 全局默认刷新周期（天），默认 7 |
| `geosite_sources` | geosite 源列表，见下方说明 |
| `geoip_sources` | geoip 源列表，格式与 `geosite_sources` 相同 |
| `rules` | 分流规则，见下方说明 |

启动：

```bash
python client.py                        # 默认读 config_client.json
python client.py /path/to/config.json   # 指定配置文件
```

将系统代理设为 `SOCKS5 <本机IP>:1080` 即可使用，局域网内其他设备同样可以指向该地址。

---

## 透明代理（TProxy）

TProxy 工作在网络层，由内核将匹配流量直接转交给代理进程，无需应用程序配置代理地址。适合用于软路由/网关，或希望对整台机器所有流量透明代理的场景。

### 启用方式

在 `config_client.json` 中设置 `tproxy_port`（0 表示禁用）：

```json
"tproxy_port": 7893
```

客户端会同时监听 SOCKS5 端口和 TProxy 端口，两者共用同一份规则和连接池。

> 需要以 **root** 或具备 `CAP_NET_ADMIN` capability 的权限运行。

### 防火墙规则

运行 `python setup.py` 配置客户端时选择启用 TProxy，向导会自动检测系统防火墙工具（iptables / nftables），生成两个脚本：

```bash
sudo bash tproxy_rules.sh     # 应用规则（重启后失效）
sudo bash tproxy_cleanup.sh   # 清除规则
```

也可参照以下命令手动配置（以端口 7893、服务端 IP 为 `1.2.3.4` 为例）。两种方式的 `ip rule` / `ip route` 路由命令完全相同，区别仅在防火墙规则语法。

**第一步：路由（iptables 和 nftables 通用）**

```bash
ip rule add fwmark 0x1 table 100
ip route add local 0.0.0.0/0 dev lo table 100
```

**iptables**

```bash
# PREROUTING：拦截转发流量（局域网设备）
iptables -t mangle -N PYREALIY
iptables -t mangle -A PYREALIY -d 127.0.0.0/8 -j RETURN
iptables -t mangle -A PYREALIY -d 10.0.0.0/8 -j RETURN
iptables -t mangle -A PYREALIY -d 172.16.0.0/12 -j RETURN
iptables -t mangle -A PYREALIY -d 192.168.0.0/16 -j RETURN
iptables -t mangle -A PYREALIY -p tcp -j TPROXY --tproxy-mark 0x1/0x1 --on-port 7893
iptables -t mangle -A PREROUTING -j PYREALIY

# OUTPUT：拦截本机自身流量（全局模式）
iptables -t mangle -N PYREALIY_LOCAL
iptables -t mangle -A PYREALIY_LOCAL -d 127.0.0.0/8 -j RETURN
iptables -t mangle -A PYREALIY_LOCAL -d 10.0.0.0/8 -j RETURN
iptables -t mangle -A PYREALIY_LOCAL -d 172.16.0.0/12 -j RETURN
iptables -t mangle -A PYREALIY_LOCAL -d 192.168.0.0/16 -j RETURN
iptables -t mangle -A PYREALIY_LOCAL -d 1.2.3.4 -j RETURN   # 排除服务端，防止环路
iptables -t mangle -A PYREALIY_LOCAL -p tcp -j MARK --set-mark 0x1/0x1
iptables -t mangle -A OUTPUT -j PYREALIY_LOCAL
```

**nftables**

```bash
nft -f - << 'EOF'
table ip pyrealiy {
    chain prerouting {
        type filter hook prerouting priority mangle; policy accept;
        ip daddr { 127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 } return
        meta l4proto tcp tproxy to :7893 meta mark set 0x1
    }
    chain output {
        type route hook output priority mangle; policy accept;
        ip daddr { 127.0.0.0/8, 10.0.0.0/8, 172.16.0.0/12, 192.168.0.0/16 } return
        ip daddr 1.2.3.4 return
        meta l4proto tcp meta mark set 0x1
    }
}
EOF
```

清除 nftables 规则：

```bash
nft delete table ip pyrealiy
ip rule del fwmark 0x1 table 100
ip route del local 0.0.0.0/0 dev lo table 100
```

如需同时为局域网设备代理，还需开启 IP 转发：

```bash
echo 1 > /proc/sys/net/ipv4/ip_forward
# 永久生效：在 /etc/sysctl.conf 添加 net.ipv4.ip_forward = 1
```

### 规则说明

| 步骤 | 作用 |
|---|---|
| `ip rule` + `ip route` | 将带 `0x1` 标记的包路由到 loopback，让它们能被 PREROUTING 的 TPROXY 规则拦截 |
| PREROUTING TPROXY | 将经本机转发的 TCP 流量重定向到 TProxy 端口，保留原始目标地址 |
| OUTPUT MARK | 将本机自身发出的 TCP 流量打标记，触发上面的路由规则（仅全局模式） |
| 排除私有地址 | 局域网直接通信不走代理 |
| 排除服务端 IP | 防止代理进程连接服务端的流量被自身拦截，造成死循环 |

---

## 分流规则

规则写在 `config_client.json` 的 `rules` 数组中，从上到下依次匹配，第一条命中的规则生效。

| 规则类型 | 示例 | 说明 |
|---|---|---|
| `DOMAIN` | `DOMAIN,example.com,DIRECT` | 精确域名匹配 |
| `DOMAIN-SUFFIX` | `DOMAIN-SUFFIX,google.com,PROXY` | 后缀匹配（含本身及所有子域名） |
| `DOMAIN-KEYWORD` | `DOMAIN-KEYWORD,youtube,PROXY` | 域名中含关键词 |
| `IP-CIDR` | `IP-CIDR,192.168.0.0/16,DIRECT` | IPv4 CIDR |
| `GEOSITE` | `GEOSITE,[source:]tag,ACTION` | geosite.dat 中的 tag，可指定源 |
| `GEOIP` | `GEOIP,[source:]code,ACTION` | geoip.dat 中的国家/地区代码，可指定源 |
| `FINAL` | `FINAL,PROXY` | 默认动作，放最后 |

**动作：**

| 动作 | 说明 |
|---|---|
| `PROXY` | 走加密隧道 |
| `DIRECT` | 本地直连，不经过代理 |
| `REJECT` | 拒绝连接（适用于广告、追踪域名屏蔽） |

### GeoSite / GeoIP 多源配置

在 `geosite_sources` / `geoip_sources` 中声明源，每个源独立下载、独立刷新，支持为单个源设置独立的 `update_days`：

```json
"geosite_sources": [
    {
        "name": "loyalsoldier",
        "url": "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geosite.dat"
    },
    {
        "name": "v2fly",
        "url": "https://github.com/v2fly/domain-list-community/releases/latest/download/dlc.dat",
        "update_days": 3
    }
],
"geoip_sources": [
    {
        "name": "loyalsoldier",
        "url": "https://github.com/Loyalsoldier/v2ray-rules-dat/releases/latest/download/geoip.dat"
    }
]
```

规则中引用时用 `source:tag` 格式，省略 `source:` 则使用列表中第一个源：

```json
"rules": [
    "GEOSITE,loyalsoldier:category-ads-all,REJECT",
    "GEOSITE,loyalsoldier:cn,DIRECT",
    "GEOSITE,v2fly:apple-cn,DIRECT",
    "GEOIP,loyalsoldier:cn,DIRECT",
    "FINAL,PROXY"
]
```

缓存文件存放在 `geosite_dir` 目录下，元数据记录在 `meta.json` 中：

```
.geosite/
  meta.json                   下载时间等元数据
  site-loyalsoldier.dat       geosite 源（site- 前缀）
  site-v2fly.dat
  ip-loyalsoldier.dat         geoip 源（ip- 前缀）
```

**常用 tag（loyalsoldier 源）：**

| tag | 规则类型 | 内容 |
|---|---|---|
| `category-ads-all` | GEOSITE | 广告和追踪域名（配合 `REJECT`） |
| `private` | GEOSITE / GEOIP | 局域网 / 私有域名或地址 |
| `cn` | GEOSITE / GEOIP | 中国大陆域名 / IP |
| `apple-cn` | GEOSITE | 苹果在中国大陆的服务 |
| `google-cn` | GEOSITE | 谷歌在中国大陆的服务 |
| `gfw` | GEOSITE | 已知被 GFW 封锁的域名 |
| `geolocation-!cn` | GEOSITE | 常见境外域名（Google、GitHub 等） |

---

## TCP Brutal

TCP Brutal 是针对跨境链路设计的拥塞控制算法，以固定速率发送数据，不因丢包降速。

**安装（服务端和客户端均需要）：**

```bash
# 方式 1：DKMS（推荐，重启后自动加载）
apt install dkms linux-headers-$(uname -r)
git clone --depth=1 https://github.com/apernet/tcp-brutal
cd tcp-brutal && make dkms

# 方式 2：临时加载（重启后失效）
git clone --depth=1 https://github.com/apernet/tcp-brutal
cd tcp-brutal && make && insmod tcp_brutal.ko
```

或直接运行 `python setup.py`，向导会完成安装。

**速率设置建议：**

- 单连接速率：实际带宽的 1.5–2 倍（例如带宽 50 Mbps → 设 80 Mbps）
- 多连接总吞吐：`brutal_pool_size × brutal_rate_bps`
- 典型配置：20 连接 × 8 Mbps = 160 Mbps 上限

---

## 项目结构

```
pyrealiy-proxy/
├── server.py              服务端入口
├── client.py              客户端入口（SOCKS5 + 分流 + 连接池）
├── setup.py               交互式部署向导（含 GeoSite/GeoIP tag 选择）
├── config_server.json     服务端配置示例
├── config_client.json     客户端配置示例
└── core/
    ├── auth.py            Poly1305 认证包
    ├── bloom.py           Bloom Filter（用于域名后缀的快速成员判定）
    ├── brutal.py          TCP Brutal socket 选项封装
    ├── camouflage.py      服务端 TLS 伪装决策（认证 vs 回放探测）
    ├── conn_pool.py       客户端预建连接池（BrutalPool）
    ├── handshake_cache.py 服务端握手缓存池（8 份轮换 + 每日刷新）
    ├── hello_auth.py      ClientHello 中 session_id 的 token 生成与验证
    ├── geosite_cache.py   GeoSite/GeoIP 多源缓存管理（下载 / 刷新 / meta.json）
    ├── router.py          分流路由（规则匹配 + geosite.dat / geoip.dat 解析）
    ├── tproxy.py          TProxy 透明代理监听器（IP_TRANSPARENT socket，仅 Linux）
    ├── socks5.py          本地 SOCKS5 协议解析
    ├── tls_raw.py         手动构造 TLS 1.3 ClientHello 字节
    ├── tunnel.py          ChaCha20-Poly1305 加密信道（帧格式 + HKDF 密钥协商）
    └── utils.py           日志、地址打包、双向中继
```

---

## 协议设计

### 连接建立流程

```
客户端                                        服务端
  │                                             │
  │── TCP connect ──────────────────────────── │
  │                                             │
  │  ┌─ 构造含 Poly1305 token 的 ClientHello ─┐ │
  │  │  token 嵌入 legacy_session_id (32B)    │ │
  │  └────────────────────────────────────────┘ │
  │── TLS ClientHello ─────────────────────── ► │
  │                                             │ 验证 session_id
  │                                             │ ├─ 通过 → 进入代理模式
  │                                             │ └─ 失败 → 回放缓存握手记录
  │                                             │
  │◄── 16 字节随机盐（HKDF 输入）────────────── │  （代理模式）
  │                                             │
  ├─────── 加密信道建立（ChaCha20-Poly1305）────┤
  │                                             │
  │── [加密] 目标地址 ─────────────────────── ► │── TCP connect → 目标服务器
  │                                             │
  │◄══════════════ 双向加密中继 ══════════════► │◄══════════════ 透明中继 ══════════════►
```

### 帧格式

```
┌──────────┬──────────────────────────────┐
│ 2 字节   │ N + 16 字节                  │
│ 明文长度 │ ChaCha20-Poly1305 密文 + Tag │
└──────────┴──────────────────────────────┘
```

Nonce 由双方各自的计数器派生，不在线路上传输（减少开销，双方计数器保持同步）。

### GFW 探测应对

服务端启动时从伪装站点预取 8 份 TLS 1.2 握手记录（含真实证书和 ECDHE 签名），每次探测随机取一份回放。不修改 `server_random`，ServerKeyExchange 签名始终有效。缓存每 24 小时自动刷新。

---

## 常见问题

**Q: `OSError: [Errno 24] Too many open files`**

提升系统文件描述符限制：

```bash
# 当前会话临时生效
ulimit -n 65536

# 永久生效（写入 /etc/security/limits.conf）
* soft nofile 65536
* hard nofile 65536
```

客户端和服务端启动时会自动尝试提升至系统硬限制。

**Q: 如何验证 TCP Brutal 是否生效**

```bash
python3 -c "from core.brutal import is_available; print(is_available())"
# 输出 True 表示内核模块已加载

# 运行时检查连接的拥塞算法
ss -tin dst <对端IP> | grep brutal
```

**Q: 不想使用 geosite，只要简单规则**

删除 `geosite_sources` 和 `geoip_sources` 字段（或置为空数组），`rules` 中只写 `DOMAIN-SUFFIX`、`IP-CIDR` 等静态规则：

```json
"geosite_sources": [],
"geoip_sources": [],
"rules": [
    "DOMAIN-SUFFIX,cn,DIRECT",
    "IP-CIDR,192.168.0.0/16,DIRECT",
    "FINAL,PROXY"
]
```
