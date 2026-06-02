# PyReality

基于 Python 的抗审查代理，融合 Shadow-TLS 和 Reality 两种协议的核心思路：

- **零延迟认证**：Poly1305 token 嵌入 TLS ClientHello 的 `legacy_session_id`，服务端在第一个数据包即完成身份验证，不产生额外 RTT
- **零延迟伪装**：预先缓存真实站点的 TLS 1.3 握手记录，GFW 探测时本地直接回放，响应时延与真实站点完全一致
- **多浏览器指纹轮换**：每次连接随机选择 Chrome / Firefox / Safari 三种 TLS 指纹（不同密码套件顺序、扩展集合与排列），避免固定 JA3 成为可统计识别的流量标识
- **加密信道**：ChaCha20-Poly1305 + HKDF 会话密钥；双向使用独立密钥（c2s / s2c），消除 nonce 复用攻击面；密钥从 ClientHello 的 `client_random` 派生，无需额外传输 salt
- **防重放**：token 内含 8 字节随机 nonce，服务端维护时间桶缓存，60 秒窗口内的重放 ClientHello 一律走伪装路径
- **TCP Brutal**：服务端可选的固定速率拥塞控制，在高丢包跨境链路上维持稳定吞吐；仅需在服务端（Linux VPS）安装内核模块，客户端无需任何额外配置
- **连接池**：客户端预建 N 条已认证隧道，SOCKS5 请求到来时零等待取用
- **内置 DNS 转发器**：本地监听 UDP，国内域名直接查询国内 DNS（223.5.5.5），境外域名通过隧道 DNS-over-TCP 查询（8.8.8.8），屏蔽域名返回 NXDOMAIN；与分流规则复用同一份路由表，将系统 DNS 指向本地端口即可消除 DNS 泄漏
- **TProxy 域名嗅探**：透明代理模式下读取连接初始字节，提取 TLS SNI 或 HTTP Host 字段，将原始目标 IP 升级为域名后再做路由匹配，使 GEOSITE / DOMAIN-SUFFIX 等规则在 TProxy 模式下同样生效
- **域名 + IP 分流**：规则内嵌配置，支持精确/后缀/关键词/正则/CIDR/GeoSite/GeoIP，正则匹配使用字面量预筛跳过无关主机名
- **Web 管理面板**：服务端内嵌 HTTP 面板，实时展示活跃连接（客户端 IP、目标、时长、上下行流量）、域名转发分布，支持单独断开连接或封锁 IP（立即终止已有连接并拒绝后续连接）

---

## 要求

| 项目 | 最低版本 |
|---|---|
| Python | 3.9（推荐 3.10+ 以获得更好性能） |
| cryptography | 42.0.0 |
| uvloop（可选） | 任意 |
| tcp_brutal 内核模块（可选，仅服务端） | Linux 内核 ≥ 4.9 |

```bash
pip install cryptography uvloop   # uvloop 仅 Linux/macOS 有效
```

---

## 快速部署

提供两种部署方式，选其一即可：

### 方式一：一键 bash 脚本

适合快速上手，无需 Python 环境预装，`curl` 即可运行：

```bash
bash install.sh
# 或从远程直接运行
bash <(curl -fsSL https://your-server/install.sh)
```

向导依次完成（服务端）：

1. 系统检测、Python 3.10+ 及依赖安装
2. 基础参数配置（端口、密码、伪装域名）
3. TCP Brutal 检测与安装（可选），可用时询问速率
4. Web 管理面板配置（监听地址、端口、访问令牌，可选启用）
5. systemd 系统服务安装（可选，需 root）

向导依次完成（客户端）：

1. 系统检测、Python 3.10+ 及依赖安装
2. 基础参数配置（服务端地址、密码、本地 SOCKS5 端口）
3. 本地 DNS 转发器配置（可选）
4. TProxy 透明代理端口配置（可选，仅设置 `tproxy_port`）
5. systemd 系统服务安装（可选，需 root）

> **TProxy 注意**：`install.sh` 仅在配置文件中写入 `tproxy_port`，不生成防火墙规则。需另行配置 iptables/nftables，或运行 `python setup.py` 由向导自动生成脚本（见方式二）。

### 方式二：Python 交互向导（完整配置）

提供完整的规则选择、TProxy 防火墙脚本生成和多 init 系统支持：

```bash
python setup.py
```

向导依次完成：

1. 服务端 / 客户端参数配置
2. （仅服务端）TCP Brutal 检测与安装，可用时自动启用并询问速率
3. （仅服务端）Web 管理面板配置（监听地址、端口、访问令牌，可选启用）
4. （仅客户端）本地 DNS 转发器配置（监听端口、国内/境外 DNS 服务器）
5. （仅客户端）分流规则选择（列出常用 GeoSite/GeoIP tag 供逐项勾选）
6. （仅客户端）TProxy 透明代理配置：询问端口、代理范围（全局/仅转发）、防火墙工具（iptables/nftables），自动生成 `tproxy_rules.sh` / `tproxy_cleanup.sh`；若已配置 DNS 转发器，可同时启用 DNS 透明捕获
7. 系统服务安装（可选，支持 systemd / SysV init / OpenRC）

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
    "brutal_rate_bps": 0,
    "admin_host": "127.0.0.1",
    "admin_port": 8080,
    "admin_token": ""
}
```

| 字段 | 说明 |
|---|---|
| `listen_port` | 监听端口，建议 443 |
| `password` | 连接密码，客户端必须一致 |
| `camouflage_host` | 伪装域名，服务端从此站点缓存 TLS 1.3 握手记录用于回放 |
| `camouflage_port` | 伪装站点端口，通常 443 |
| `brutal_rate_bps` | TCP Brutal 单连接速率（字节/秒），0 表示禁用 |
| `admin_host` | 管理面板监听地址，建议 `127.0.0.1`（仅本机），`0.0.0.0` 需配合 `admin_token` 使用 |
| `admin_port` | 管理面板端口，0 表示不启用 |
| `admin_token` | 访问令牌，URL 中携带 `?token=xxx` 进行验证；留空则不验证 |

启动：

```bash
python server.py                        # 默认读 config_server.json
python server.py /path/to/config.json   # 指定配置文件
```

---

### Web 管理面板

服务端内嵌了一个轻量 HTTP 面板，设置 `admin_port` 后随服务端一同启动，无需额外进程。

**功能：**

| 页面区域 | 内容 |
|---|---|
| 活跃连接 | 连接 ID、客户端 IP、目标地址、在线时长、上下行流量；[断开] 关闭单条连接，[封锁] 同时加入黑名单 |
| 连接记录 | 最近 50 条已完成连接的快照（ID、目标、时长、流量、关闭时间）；即使连接生命周期短于刷新间隔也不会遗漏；可对已关闭连接的 IP 执行 [封锁] |
| 域名分布 | Top 30 目标域名 / IP，按连接次数降序，显示累计流量 |
| 封锁 IP | 黑名单列表，[解除] 恢复该 IP 的访问权限 |

封锁 IP 时，服务端会立即关闭该 IP 的所有已有连接，并拒绝后续连接（直至解除）。面板每秒刷新一次。

**排序：** 点击活跃连接、连接记录、域名分布任意列标题可按该列排序，再次点击反转升/降序，当前排序列高亮并显示 ▲/▼ 箭头。

**状态着色：**

| 字段 | 颜色 | 阈值 |
|---|---|---|
| 时长 | 琥珀 | 30 秒 – 5 分钟 |
| 时长 | 绿色 | > 5 分钟（长连接/持久隧道） |
| 流量 | 绿色 | > 1 MB |
| 流量 | 橙色 | > 10 MB |

**访问地址：**

```
http://<admin_host>:<admin_port>/?token=<admin_token>
```

`admin_token` 为空时省略 `?token=...` 直接访问。

**安全建议：**

- `admin_host` 建议设为 `127.0.0.1`，面板仅对本机开放
- 通过 SSH 端口转发在本地浏览器查看，无需将面板端口暴露到公网：

```bash
ssh -L 8080:127.0.0.1:8080 user@your-vps
# 然后本地访问 http://127.0.0.1:8080/?token=xxx
```

- 若需从外部访问，务必设置 `admin_token`，并考虑配合防火墙仅允许可信 IP

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
    "brutal_rate_bps": 0,
    "brutal_pool_size": 10,
    "tproxy_port": 0,

    "dns_listen_host": "127.0.0.1",
    "dns_listen_port": 5353,
    "cn_dns": "223.5.5.5",
    "remote_dns": "8.8.8.8",

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
| `brutal_rate_bps` | 固定为 0，客户端不启用 TCP Brutal |
| `brutal_pool_size` | 预建连接池大小，默认 10 |
| `tproxy_port` | TProxy 监听端口，0 禁用；需要 root / CAP_NET_ADMIN |
| `dns_listen_host` | DNS 转发器监听地址，建议 `127.0.0.1` |
| `dns_listen_port` | DNS 转发器端口，0 禁用；5353 无需 root，53 需要 root |
| `cn_dns` | 国内 DNS 服务器（DIRECT 域名走此 UDP 查询），默认 `223.5.5.5` |
| `remote_dns` | 境外 DNS 服务器（PROXY 域名通过隧道 TCP 查询），默认 `8.8.8.8` |
| `geosite_dir` | GeoSite/GeoIP 缓存目录，默认 `.geosite` |
| `geosite_update_days` | 全局默认刷新周期（天），默认 7 |
| `geosite_sources` | geosite 源列表，见下方说明 |
| `geoip_sources` | geoip 源列表，格式与 `geosite_sources` 相同 |
| `rules` | 分流规则，见下方说明 |

#### 高级调优字段（可选，默认值适合家用场景）

| 字段 | 默认 | 说明 |
|---|---|---|
| `access_log` | `false` | 是否打每连接的 dispatch INFO 日志（`PROXY/DIRECT/REJECT  host:port  [#N rule]`）。高并发下打开会显著拖慢吞吐，**建议生产关、调试时再开**。关闭后启动日志、错误日志不受影响 |
| `drain_threshold` | `65536` | 写缓冲达到该字节数才触发 `drain()`。家用百兆带宽 64KB 足够；**跨境长肥管道**（200ms RTT × 100Mbps = 2.5MB BDP）调到 `262144`（256KB）或 `1048576`（1MB）能让 pipeline 填满，吞吐更稳。代价：单连接内存占用上升、短包延迟可能略恶化 |

服务端 `config_server.json` 同样支持 `access_log` 和 `drain_threshold`，含义一致。

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

**域名嗅探**：TProxy 从内核拿到的是原始目标 IP，无法直接匹配 GEOSITE / DOMAIN-SUFFIX 等域名规则。客户端会读取连接初始字节（最多 1 KB，2 秒超时），从 TLS SNI 或 HTTP Host 头提取域名，然后用域名进行路由匹配，实际连接仍使用原始 IP，不会触发二次 DNS 解析。

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

### DNS 透明捕获（可选）

若同时启用了本地 DNS 转发器（`dns_listen_port: 5353`），可追加以下规则将所有 DNS 查询重定向到转发器，实现全局 DNS 防泄漏。`<uid>` 替换为运行代理进程的用户 UID（`id -u`），用于阻止转发器自身的上游查询形成环路。

**iptables**

```bash
# LAN 设备的 DNS
iptables -t nat -N PYREALIY_DNS
iptables -t nat -A PYREALIY_DNS -p udp --dport 53 -j REDIRECT --to-port 5353
iptables -t nat -A PREROUTING -j PYREALIY_DNS

# 本机的 DNS（排除代理进程自身，防止环路）
iptables -t nat -N PYREALIY_DNS_LOCAL
iptables -t nat -A PYREALIY_DNS_LOCAL -p udp --dport 53 -m owner --uid-owner <uid> -j RETURN
iptables -t nat -A PYREALIY_DNS_LOCAL -p udp --dport 53 -j REDIRECT --to-port 5353
iptables -t nat -A OUTPUT -j PYREALIY_DNS_LOCAL
```

**nftables**（独立 nat 表，REDIRECT 不能在 mangle 类型 chain 中使用）

```bash
nft -f - << 'EOF'
table ip pyrealiy_nat {
    chain prerouting {
        type nat hook prerouting priority dstnat; policy accept;
        meta l4proto udp th dport 53 redirect to :5353
    }
    chain output {
        type nat hook output priority dstnat; policy accept;
        meta l4proto udp th dport 53 skuid <uid> return
        meta l4proto udp th dport 53 redirect to :5353
    }
}
EOF
```

清除：`nft delete table ip pyrealiy_nat`

`setup.py` 配置 TProxy 时会询问是否同时启用 DNS 捕获，选择后自动将上述规则写入 `tproxy_rules.sh`，并把 `dns_listen_host` 改为 `0.0.0.0` 以接收 LAN 设备的 DNS 请求。

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

规则写在 `config_client.json` 的 `rules` 数组中，**按配置顺序从上到下扫描，首条命中即返回**（与 Clash / sing-box / Surge 行为一致）。后续规则不再执行。`FINAL` 放在最后作为默认动作。

| 规则类型 | 示例 | 说明 |
|---|---|---|
| `DOMAIN` | `DOMAIN,example.com,DIRECT` | 精确域名匹配（含本身，不含子域名）|
| `DOMAIN-SUFFIX` | `DOMAIN-SUFFIX,google.com,PROXY` | 后缀匹配（含本身及所有子域名）|
| `DOMAIN-KEYWORD` | `DOMAIN-KEYWORD,youtube,PROXY` | 主机名中含关键词即命中 |
| `DOMAIN-REGEX` | `DOMAIN-REGEX,^(.+\.)?google\.com$,PROXY` | 正则匹配（Python `re`，`search` 模式）|
| `IP-CIDR` | `IP-CIDR,192.168.0.0/16,DIRECT` | IPv4/IPv6 CIDR 匹配 |
| `GEOSITE` | `GEOSITE,[source:]tag,ACTION` | geosite.dat 中的 tag，可指定源 |
| `GEOIP` | `GEOIP,[source:]code,ACTION` | geoip.dat 中的国家/地区代码，支持 inverse_match |
| `FINAL` | `FINAL,PROXY` | 兜底动作，未命中任何规则时生效 |

> **TProxy 模式补充说明**：TProxy 只能从内核获得目标 IP，但客户端会在连接建立后立即嗅探初始字节提取 TLS SNI / HTTP Host，因此域名类规则（DOMAIN、DOMAIN-SUFFIX、GEOSITE 等）在 TProxy 模式下同样有效。无法嗅探到域名时（非 TLS/HTTP 协议）自动退化为 IP 规则匹配。

**动作：**

| 动作 | 说明 |
|---|---|
| `PROXY` | 走加密隧道 |
| `DIRECT` | 本地直连，不经过代理 |
| `REJECT` | 拒绝连接（适用于广告、追踪域名屏蔽） |

### 匹配顺序与性能

规则按**配置顺序**线性扫描，每条规则独立判断是否命中。每种规则类型自带内部优化以保证扫描成本最小化：

| 规则 | 单次判断成本 | 内部优化 |
|---|---|---|
| `DOMAIN` | O(1) | 字符串相等 |
| `DOMAIN-SUFFIX` | O(1) | `endswith` |
| `DOMAIN-KEYWORD` | O(1) | C 层 `in` |
| `DOMAIN-REGEX` | O(1)~O(P) | 固定字面量预筛 → 不含则跳过正则引擎 |
| `IP-CIDR` | O(1) | `IPAddress in IPNetwork` |
| `GEOSITE` | O(log·parts) | 内部 exact 集 + suffix 集（≥64 项启用 Bloom Filter）+ keyword 列表 |
| `GEOIP` | O(log n) | v4 排序后二分；v6 线性；inverse_match 通过 XOR 翻转语义 |

**关键点**：GEOSITE/GEOIP 这类"展开后有数万条目"的规则**只算一条规则**，其内部条目通过 Bloom Filter + 哈希表在 O(log·parts) 内完成判断，不会拖慢扫描。

**DOMAIN-REGEX** 启动时从每条正则中提取固定子串（如 `^(.+\.)?openai\.com$` 提取 `openai.com`），匹配时先用 C 层 `in` 操作检查该子串是否出现在主机名中，不包含则跳过正则引擎，包含才运行完整正则。绝大多数主机名在预筛阶段即可排除。

> **顺序建议**：把高频命中规则（如 `GEOSITE,cn,DIRECT`、`GEOIP,cn,DIRECT`）放靠前，可以让大多数请求在前几条规则就命中返回，减少后续扫描。
>
> **正则建议**：让 pattern 包含清晰的固定子串（如完整域名片段），预筛效果更好。纯通配符写法（如 `.*`）无法提取字面量，每次都要走正则引擎。

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

TCP Brutal 是针对跨境链路设计的拥塞控制算法，以固定速率发送数据，不因丢包降速。**仅需在服务端（Linux VPS）安装**，客户端无需任何内核模块。

**安装（仅服务端）：**

```bash
# 官方一键脚本（推荐，自动处理 DKMS 和开机加载）
bash <(curl -fsSL https://tcp.hy2.sh/)

# 或手动 DKMS 安装（持久化，重启后自动加载）
apt install dkms linux-headers-$(uname -r)
git clone --depth=1 https://github.com/apernet/tcp-brutal
cd tcp-brutal && make dkms
```

或直接运行 `python setup.py` 选择服务端角色，向导检测到模块可用时会自动启用并询问速率。

**速率设置建议：**

- 单连接速率建议设为实际上行带宽的 80%–100%（例如 VPS 带宽 100 Mbps → 设 80–100 Mbps）
- `config_server.json` 中的 `brutal_rate_bps` 控制服务端向客户端发送时的速率

---

## 系统服务

运行 `python setup.py` 时向导会询问是否安装为系统服务，自动检测 init 系统并生成对应的启动脚本（含 `ulimit -n 65536`）。

### systemd

适用于：Debian、Ubuntu、CentOS 8+、Fedora、Arch 等主流发行版。

若非 root 运行向导，脚本会写到当前目录（`pyrealiy-server.service`），手动安装：

```bash
sudo cp pyrealiy-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable pyrealiy-server   # 设置开机自启
sudo systemctl start  pyrealiy-server   # 立即启动
```

日常管理：

```bash
systemctl status  pyrealiy-server
systemctl restart pyrealiy-server
journalctl -u pyrealiy-server -f        # 实时日志
```

### SysV init

适用于：Debian 8 以下、CentOS 6、旧版 OpenWrt 等。

```bash
sudo cp pyrealiy-server.init /etc/init.d/pyrealiy-server
sudo chmod +x /etc/init.d/pyrealiy-server
sudo update-rc.d pyrealiy-server defaults   # Debian/Ubuntu
# 或
sudo chkconfig --add pyrealiy-server        # RHEL/CentOS
sudo service pyrealiy-server start
```

日常管理：

```bash
service pyrealiy-server status
service pyrealiy-server restart
tail -f /var/log/pyrealiy-server.log        # 实时日志
```

### OpenRC（Alpine Linux）

Alpine 默认使用 OpenRC，向导会自动识别并生成 `pyrealiy-server.openrc`：

```bash
sudo cp pyrealiy-server.openrc /etc/init.d/pyrealiy-server
sudo chmod +x /etc/init.d/pyrealiy-server
sudo rc-update add pyrealiy-server default  # 加入 default runlevel
sudo rc-service pyrealiy-server start
```

日常管理：

```bash
rc-service pyrealiy-server status
rc-service pyrealiy-server restart
tail -f /var/log/pyrealiy-server.log
```

### ulimit 处理方式

| init 系统 | 方式 | 说明 |
|---|---|---|
| systemd | `LimitNOFILE=65536`（`[Service]` 段） | systemd 原生方式，比 shell `ulimit` 更可靠 |
| SysV init | `ulimit -n 65536`（`start()` 函数内） | 在 `nohup` 前执行，子进程继承 |
| OpenRC | `start_pre()` 钩子内 `ulimit -n 65536` | fork 前执行，子进程继承 |

---

## 项目结构

```
pyrealiy-proxy/
├── server.py              服务端入口
├── client.py              客户端入口（SOCKS5 + 分流 + 连接池）
├── setup.py               交互式部署向导（规则选择 / TProxy / 系统服务安装）
├── config_server.json     服务端配置示例
├── config_client.json     客户端配置示例
└── core/
    ├── hello_auth.py      ClientHello token 生成与验证（Poly1305 + 时间戳掩码 + nonce 防重放）
    ├── camouflage.py      服务端 TLS 伪装决策（认证通过 → 代理模式；探测/重放 → 回放缓存）
    ├── handshake_cache.py 服务端握手缓存池（32 份 TLS 1.3 记录轮换 + 每小时刷新）
    ├── tls_raw.py         TLS ClientHello 构造（Chrome / Firefox / Safari 三档随机指纹）
    ├── tunnel.py          ChaCha20-Poly1305 加密信道（TLS 0x17 帧格式 + 双向独立密钥）
    ├── conn_pool.py       客户端预建连接池（BrutalPool，含超时 / 过期检测）
    ├── socks5.py          本地 SOCKS5 协议解析
    ├── router.py          分流路由（规则匹配 + geosite.dat / geoip.dat 解析）
    ├── geosite_cache.py   GeoSite/GeoIP 多源缓存管理（下载 / 刷新 / meta.json）
    ├── bloom.py           Bloom Filter（域名后缀预过滤，Kirsch-Mitzenmacher 双哈希）
    ├── sniffer.py         流量嗅探（TLS SNI + HTTP Host 提取域名，PrefixedReader）
    ├── dns_forwarder.py   DNS 转发器（分流查询 + DNS-over-TCP 隧道）
    ├── stats.py           服务端连接统计与 IP 封锁表（StatsStore）
    ├── admin.py           Web 管理面板（内嵌 HTTP 服务，排序 / 着色，无外部依赖）
    ├── brutal.py          TCP Brutal socket 选项封装
    ├── tproxy.py          TProxy 透明代理监听器（IP_TRANSPARENT socket，仅 Linux）
    └── utils.py           日志、地址打包、双向中继
```

---

## 协议设计

### 连接建立流程

```
客户端                                              服务端
  │                                                   │
  │── TCP connect ──────────────────────────────────► │
  │                                                   │
  │  ┌─ 随机选 Chrome/Firefox/Safari 指纹档案 ────── ┐ │
  │  │  构造 ClientHello，token 嵌入 session_id     │ │
  │  │  token = random(8B) + masked_ts(8B) + MAC(16B) │ │
  │  └────────────────────────────────────────────── ┘ │
  │── TLS ClientHello ─────────────────────────────► │
  │                                                   │ 验证 session_id token + nonce 防重放
  │                                                   │ ├─ 通过 → 回放缓存握手记录，进代理模式
  │                                                   │ └─ 失败/重放 → 回放缓存握手记录，关闭
  │                                                   │
  │◄── 缓存的真实 TLS 1.3 握手记录（ServerHello +  ── │
  │    CCS + EncryptedExtensions + Certificate +      │
  │    CertificateVerify + Finished）                 │
  │                                                   │
  │── CCS + 假 Finished（握手模拟完成）─────────────► │
  │                                                   │
  │  会话密钥由双方各自从 ClientHello 的              │
  │  client_random 派生，无需额外传输 salt            │
  │  c2s_key = HKDF(master, info="c2s")              │
  │  s2c_key = HKDF(master, info="s2c")              │
  │                                                   │
  │── [加密] 目标地址 ──────────────────────────────► │── TCP connect → 目标服务器
  │                                                   │
  │◄═══════════════ 双向加密中继 ═══════════════════► │◄════════════ 透明中继 ════════════►
```

### 帧格式（TLS 应用数据记录）

```
┌──────┬───────────┬────────────┬──────────────────────────────────┐
│ 0x17 │ 0x03 0x03 │ 2字节长度  │ ChaCha20-Poly1305 密文 + 16B Tag │
└──────┴───────────┴────────────┴──────────────────────────────────┘
  type   version     ciphertext   ciphertext（最大 16384+16 字节）
                     length
```

与 TLS 1.3 应用数据记录格式完全一致，握手完成后的流量对旁观者不可与真实 HTTPS 区分。Nonce 由双方各自的计数器派生，不在线路上传输。

### TLS 指纹轮换

每次连接从三种浏览器档案中随机选择一种，主要差异体现在 JA3 哈希的各个组成部分：

| 档案 | GREASE | 密码套件特征 | 扩展特征 |
|---|---|---|---|
| Chrome 120 | 密码套件首位 + 扩展首尾 + groups + key_share | 含 0x00FF (SCSV) | GREASE 扩展首尾各一个 |
| Firefox 121 | 无 | 含 CCA9/CCA8（ChaCha20+ECDSA）、CBC 套件、ffdhe 组 | 含 compress_certificate / record_size_limit |
| Safari 17 | 无 | 含更多 ECDSA-CBC 套件 | **renegotiation_info 位于首位**，含 compress_certificate |

Chrome 档案内部还有 GREASE 值随机性（从 16 个 RFC 8701 保留值中随机选），同一档案不同连接的 JA3 哈希也不相同。

### GFW 探测应对

服务端启动时并发从伪装站点预取 32 份 TLS 1.3 握手记录（ServerHello + CCS + EncryptedExtensions + Certificate + CertificateVerify + Finished），每次探测随机取一份回放。每小时自动在后台静默刷新，不影响进行中的连接。

**防重放机制**：服务端维护基于时间桶的 nonce 缓存，每个 token 含 8 字节真随机 nonce。60 秒窗口内重放的 ClientHello——即使 Poly1305 验证通过——也会走伪装路径而非代理路径，消除通过行为差异识别代理的攻击面。

---

## 常见问题

**Q: 如何配置系统 DNS 指向本地转发器**

启用 DNS 转发器（`dns_listen_port: 5353`）后，需要将操作系统 DNS 改为本地地址：

```bash
# Linux（临时，重启后失效）
echo "nameserver 127.0.0.1" | sudo tee /etc/resolv.conf

# systemd-resolved（永久）
# 编辑 /etc/systemd/resolved.conf，添加：
# [Resolve]
# DNS=127.0.0.1:5353
# 然后：sudo systemctl restart systemd-resolved
```

macOS：系统设置 → 网络 → DNS，添加 `127.0.0.1`（端口 5353 需借助 dnsmasq 中转，或直接监听端口 53）。

如果使用端口 53，Linux 上可能需要先关闭 `systemd-resolved` 占用：

```bash
sudo systemctl stop systemd-resolved
sudo systemctl disable systemd-resolved
```

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

**Q: 如何验证服务端 TCP Brutal 是否生效**

在服务端执行：

```bash
python3 -c "from core.brutal import is_available; print(is_available())"
# 输出 True 表示内核模块已加载

# 运行时检查连接的拥塞算法
ss -tin dst <客户端IP> | grep brutal
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


---

## 开发与测试

### 多版本 Python 兼容性测试

PyReality 目标支持 **Python 3.9+**。仓库附带跨版本冒烟测试脚本，跑一次能快速发现"在某个 Python 上语法不兼容"或"模块导入崩"这类回归。

#### 1. 安装 [uv](https://github.com/astral-sh/uv)（一次性）

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

#### 2. 跑全版本矩阵

```bash
bash scripts/test-py-matrix.sh
```

脚本会：

- 用 `uv python install` 确保 3.9 / 3.10 / 3.11 / 3.12 / 3.13 都装好
- 为每个版本分别在 `.venvs/py3.x` 建独立虚拟环境
- 装 `cryptography` 后对全部 16 个 `core.*` 模块 + `client.py` / `server.py` / `setup.py` 做 `import` 测试
- 任何版本失败立即用红色提示，并以 exit code 1 退出

#### 3. 跑子集（调试单个版本）

```bash
PY_VERSIONS="3.9 3.10" bash scripts/test-py-matrix.sh
```

#### 4. 默认开发版本

仓库根目录的 `.python-version` 指向 `3.12`。本地用 `uv venv` 不带参数会自动用这个版本。

### 添加新依赖时

如果引入 `cryptography` 以外的第三方包，同步更新 `scripts/test-py-matrix.sh` 里 `uv pip install` 一行。理想情况下未来迁移到 `pyproject.toml` + `uv sync`。

### 写新代码时的版本兼容性

PyReality 通过 `from __future__ import annotations` 让**类型注解**走字符串延迟求值，所以 `def f(x: int | None)` 在 3.9 也合法。但下面这些**运行时表达式**仍要求 3.10+：

```python
_T = int | None                  # ✗ 赋值语句右值，3.9 抛 TypeError
isinstance(x, int | str)          # ✗ isinstance 第二参，3.9 抛 TypeError
def f(): pass; type(f) | None     # ✗ 任何表达式位置的 |
```

3.9 兼容写法：

```python
from typing import Union, Optional
_T = Optional[int]                # 或 Union[int, None]
isinstance(x, (int, str))         # 用 tuple
```

矩阵测试脚本会拦下这类回归。
