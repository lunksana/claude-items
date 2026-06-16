# Mirage

基于 Python 的抗审查代理，融合 Shadow-TLS 和 Reality 两种协议的核心思路：

- **零延迟认证**：Poly1305 token 嵌入 TLS ClientHello 的 `legacy_session_id`，服务端在第一个数据包即完成身份验证，不产生额外 RTT
- **零延迟伪装**：预先缓存真实站点的 TLS 1.3 握手记录，GFW 探测时本地直接回放，响应时延与真实站点完全一致
- **多浏览器指纹轮换**：每次连接随机选择 Chrome / Firefox / Safari 三种 TLS 指纹（不同密码套件顺序、扩展集合与排列），避免固定 JA3 成为可统计识别的流量标识
- **加密信道**：ChaCha20-Poly1305 + HKDF 会话密钥；双向使用独立密钥（c2s / s2c），消除 nonce 复用攻击面；密钥从 ClientHello 的 `client_random` 派生，无需额外传输 salt
- **防重放**：token 内含 8 字节随机 nonce，服务端维护时间桶缓存，60 秒窗口内的重放 ClientHello 一律走伪装路径
- **TCP Brutal**：服务端可选的固定速率拥塞控制，在高丢包跨境链路上维持稳定吞吐；仅需在服务端（Linux VPS）安装内核模块，客户端无需任何额外配置
- **多节点与自适应选路（sing-box 风格）**：客户端可配置多组服务端节点 + `urltest` / `fallback` / `selector` 组。`urltest` 自动选当前 median 握手延迟最低的节点（带 tolerance 防抖避免抖动期频繁切换），`fallback` 按声明顺序在前者不健康时切到后备，`selector` 则由用户通过 Clash API 手动指定节点。分流规则可把动作直接指向节点或组的 tag，实现"流媒体走美国节点、国内直连、其余自动"这种粒度。
- **每节点独立连接池 + 被动延迟采集**：每个 `mirage` 节点独占一份预建隧道池。每次 build 的真实握手耗时作为延迟样本回灌到滚动窗口（median 抗抖），urltest 组的决策始终基于近期实测数据，无需独立 ping 探测；长时间无流量时由后台 `HealthCheck` 主动 probe 兜底。SOCKS5 请求到达时从相应池零等待取用。
- **内置 DNS 转发器**：本地监听 UDP，按分流规则决定每条 DNS 查询的出口；命中 `direct` 出口走 UDP 直查国内 DNS（默认 223.5.5.5），命中具体 `mirage` 节点（或经组解析到的节点）走该节点独占的 DNS-over-TCP pipeline 查询境外 DNS（默认 8.8.8.8），命中 `block` 出口返回 NXDOMAIN。与流量规则复用同一份路由表，将系统 DNS 指向本地端口即可消除 DNS 泄漏。
- **TProxy 域名嗅探**：透明代理模式下读取连接初始字节，提取 TLS SNI 或 HTTP Host 字段，将原始目标 IP 升级为域名后再做路由匹配，使 GEOSITE / DOMAIN-SUFFIX 等规则在 TProxy 模式下同样生效
- **域名 + IP 分流**：规则内嵌配置，支持精确/后缀/关键词/正则/CIDR/GeoSite/GeoIP，正则匹配使用字面量预筛跳过无关主机名。一条规则写多个条件字段时默认 OR（任一命中），显式 `"mode": "and"` 则全部满足才命中。
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

唯一入口：`install.sh` 交互式向导。

```bash
sudo bash install.sh
```

启动后选 `[1] 服务端 / [2] 客户端 / [3] 两端都装`，向导自动检测环境、安装依赖、生成配置、注册 systemd。

**服务端流程**：

1. 系统检测、Python 3.10+ 及 `cryptography` / `uvloop` 安装
2. 监听端口（默认 443）、密码（自动 `openssl rand` 24 字符或手输）、伪装 SNI（自动 `openssl s_client -tls1_3` 探测）
3. TCP Brutal 检测（可选，自建 VPS 推荐）
4. 写 `config_server.json` + 注册 `mirage-server.service`
5. **末尾自动打印对应客户端配置**（含公网 IP、密码、SNI），可直接复制到客户端机器

**客户端流程**：

1. 系统检测 + 依赖安装
2. 服务端地址 / 端口 / 密码 / 伪装 SNI / SOCKS5 监听口（默认 1080）
3. **路由模板**三选一：
   - 国内外分流（推荐）：geosite:cn + 内网 → 直连；其余走代理
   - 全代理：所有流量走 proxy 出口
   - 自定义：生成空 rules，安装后用户自行编辑
4. **DNS 方案**三选一：
   - 国内外分流（推荐，默认）：国内域名查 `119.29.29.29` 直连；其余通过 VPS 转发到 `1.1.1.1:53`
   - 全代理：所有 DNS 走 proxy 隧道（隐私优先）
   - 不启用：系统继续用 `/etc/resolv.conf`
5. Clash 兼容 API（可选，默认开；自动生成 secret，Yacd / metacubexd 可直接登录）
6. 写 `config_client.json` + 注册 `mirage-client.service`

**热加载**：改完 `config_client.json` 后 `systemctl reload mirage-client` 即生效（不重 bind socket、不断连接）。详见 [0.4.25 CHANGELOG](CHANGELOG.md#0425---2026-06-11)。

---

## 手动配置

不想用 `install.sh` 向导也可手编。仓库提供两份**详细带注释的示例文件**作为参考：

| 文件 | 用途 |
|---|---|
| `config_server.example.jsonc` | 服务端完整字段说明（JSONC 格式，VSCode 等编辑器原生支持高亮） |
| `config_client.example.jsonc` | 客户端 schema_v1 完整示例（含 DoH/DoT、Clash API、tuning 全部 section） |

JSONC = JSON with Comments。**Python 的 `json.load()` 不接受注释**，所以要用时先去掉注释保存为 `.json`：

```bash
# 一行命令：去注释 + 保存
python3 -c "
import re, json, sys
src = open('config_client.example.jsonc').read()
src = re.sub(r'/\*.*?\*/', '', src, flags=re.S)
src = re.sub(r'//[^\n]*', '', src)
json.dump(json.loads(src), open('config_client.json','w'), indent=4)
"

# 或编辑器里直接删注释另存为 .json
```

### 服务端（墙外 VPS）

参考 `config_server.example.jsonc`。最小配置：

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

客户端使用 **schema_version=1**（推荐）。8 个顶层 key 在该 schema 周期内永不新增（见下方 **schema_version=1 合约**）。编辑 `config_client.json`：

```json
{
    "schema_version": 1,
    "log": {"format": "text"},

    "inbounds": [
        {"type": "socks5", "listen": "127.0.0.1:1080"}
    ],

    "outbounds": [
        {
            "tag": "node-jp-1", "type": "mirage",
            "server": "your.tokyo.server.ip", "server_port": 443,
            "password": "your-strong-password-here",
            "sni": "www.apple.com"
        },
        {
            "tag": "node-us-1", "type": "mirage",
            "server": "your.us.server.ip", "server_port": 443,
            "password": "your-strong-password-here",
            "sni": "www.apple.com"
        },

        {"tag": "auto",    "type": "urltest",  "outbounds": ["node-jp-1", "node-us-1"], "interval": "60s", "tolerance": 50},
        {"tag": "us-only", "type": "fallback", "outbounds": ["node-us-1"]},

        {"tag": "direct", "type": "direct"},
        {"tag": "block",  "type": "block"}
    ],

    "route": {
        "default": "auto",
        "rules": [
            {"rule_set": ["loyalsoldier:category-ads-all"], "outbound": "block"},

            {"rule_set": ["loyalsoldier:private"],          "outbound": "direct"},
            {"geoip":    ["loyalsoldier:private"],          "outbound": "direct"},

            {"rule_set": ["loyalsoldier:cn"],               "outbound": "direct"},
            {"geoip":    ["loyalsoldier:cn"],               "outbound": "direct"},

            {"ip_cidr": ["127.0.0.0/8", "10.0.0.0/8",
                         "172.16.0.0/12", "192.168.0.0/16"], "outbound": "direct"},

            {"rule_set": ["loyalsoldier:netflix",
                          "loyalsoldier:disney",
                          "loyalsoldier:openai"],            "outbound": "us-only"}
        ]
    },

    "dns": {
        "listen": "127.0.0.1:5353"
    },

    "api": {
        "listen": "127.0.0.1:9090",
        "secret": "your-strong-api-token",
        "cors": ["*"]
    },

    "tuning": {
        "access_log": false
    },

    "cn_dns": "119.29.29.29",
    "remote_dns": "1.1.1.1:53",
    "geosite_dir": ".geosite",
    "geosite_update_days": 7
}
```

> **DNS 上游配置位置（0.4.41+）**：拆分 DNS 的 `listen` / `cn` / `remote` 现在统一写在 `dns` 块里，启动时由 config 自动投射到运行时老顶层键，无告警：
> ```json
> "dns": {"listen": "127.0.0.1:5353", "cn": "119.29.29.29", "remote": "tls://1.1.1.1:853"}
> ```
> - `dns.listen` → `dns_listen_host` / `dns_listen_port`
> - `dns.cn` → `cn_dns`（命中 `direct` 出口时用，纯 UDP，支持 `host:port`）
> - `dns.remote` → `remote_dns`（经隧道查境外，支持 `host:port` / `tls://` / `https://`）
> - 0.4.40 及更早把这些写在**顶层**的老配置仍可工作，但会收到一条 deprecation 提示，建议迁移到 `dns` 块。
> - `dns.resolvers` + `dns.rules`（多解析器 schema）已在 0.4.16 落地但 dns_forwarder 尚未消费，当前用上面的简易拆分模型。

| 顶层字段 | 说明 |
|---|---|
| `schema_version` | 固定为 `1`。**合约：1 期间不再加第 9 个结构化 section**（扁平运维标量键可并存，详见下方"schema_version=1 合约"） |
| `log` | `{"format": "text" | "json"}`，默认 text。json 模式每行一个 JSON 行（见"结构化日志"） |
| `inbounds[*]` | 入站监听。支持 `socks5` / `http` / `mixed` 三种类型（见 **入站类型**） |
| `outbounds[*]` | 出口节点 + 组定义，见 **多节点与自适应选路** |
| `route.default` | 未命中任何 rule 时的兜底 outbound tag（替代老的 `route.final`） |
| `route.rules[*]` | 分流规则（sing-box 结构化对象数组），见 **分流规则** |
| `dns.listen` | DNS 转发器监听 `"host:port"`，未设则不启用 |
| `api.listen` | Clash 兼容 API 监听 `"host:port"`，未设则不启用。**`api.secret` 同时必填** |
| `api.secret` | Bearer token；Yacd / metacubexd 登录用 |
| `api.cors` | CORS allow-origin 列表，默认 `["*"]` |
| `tuning.*` | 高级调优，主表不出现；详见 **高级调优字段** |
| `cn_dns` | 命中 `direct` 出口的 DNS 查询走此 UDP 服务器，默认 `119.29.29.29` |
| `remote_dns` | 命中 `mirage` 出口的 DNS 查询经隧道走此服务器，支持 **UDP / DoT / DoH** 三种 scheme（见 **DNS 转发器**） |
| `geosite_dir` / `geosite_update_days` | GeoSite/GeoIP 缓存目录与默认刷新周期 |

**outbound 内字段（`mirage` 类型）：**

| 字段 | 说明 |
|---|---|
| `type` | `mirage` / `direct` / `block` / `urltest` / `fallback` |
| `tag` | 唯一标识，被 rules 的 `outbound` 字段引用 |
| `server` / `server_port` | 服务端地址 |
| `password` | 与服务端一致 |
| `sni` | 伪装 SNI，与服务端 `camouflage_host` 一致 |
| `brutal_rate_bps` | 可选，默认 0（客户端不启用 Brutal） |
| `brutal_pool_size` | 可选，预建连接池大小，默认 20。**按预期最大并发 conn 数定**：浏览器日常 8-16 足够；爬虫 / 下载器场景设 32+。池过小时，并发请求会触发冷建（每条 ~0.3s staircase 间隔），实测拖慢 16-并发上行近 50% |
| `stagger_step_sec` | 可选，相邻 build 之间的最小间隔（秒），默认 0.30。基于 pcap 实测的反 SYN-burst 指纹值。**仅在不在意 GFW 流量分析的内网/可信链路场景下**可降到 0.05 加速 refill |
| `stagger_jitter_sec` | 可选，上一参数的随机抖动（秒），默认 0.08 |

> **`direct` / `block` 自动补全**：即便未在 `outbounds` 显式声明，启动期也会自动补出 `{"type":"direct","tag":"direct"}` 与 `{"type":"block","tag":"block"}` 两个出口，分流规则可直接引用这两个 tag。

#### 向后兼容：老单节点配置仍工作

若 `config_client.json` 顶层用的是 `server_host` / `server_port` / `password` / `camouflage_host` 这套老字段、且没有 `outbounds` 数组，启动期会**自动合成**一个单节点 `mirage` 出口（tag 为 `proxy`），同时 CSV 风格的 `rules` 中老关键字 `PROXY` / `DIRECT` / `REJECT` 自动映射到 `proxy` / `direct` / `block`。**不需要修改旧配置就能升级**。新格式与老格式不能混用 outbounds 与 server_host 顶层字段，但 `outbounds` + CSV `rules` 这种半新半旧的写法也支持。

#### 高级调优字段（可选，默认值适合家用场景）

| 字段 | 默认 | 说明 |
|---|---|---|
| `access_log` | `false` | 是否打每连接的 dispatch INFO 日志（`PROXY/DIRECT/REJECT  host:port  [#N rule]`）。高并发下打开会显著拖慢吞吐，**建议生产关、调试时再开**。关闭后启动日志、错误日志不受影响 |
| `drain_threshold` | `65536` | 写缓冲达到该字节数才触发 `drain()`。家用百兆带宽 64KB 足够；**跨境长肥管道**（200ms RTT × 100Mbps = 2.5MB BDP）调到 `262144`（256KB）或 `1048576`（1MB）能让 pipeline 填满，吞吐更稳。代价：单连接内存占用上升、短包延迟可能略恶化 |
| `log_levels` | `{}` | 按模块控制日志级别（用于调试）。键是 logger 名（见下表），值是 `DEBUG` / `INFO` / `WARNING` / `ERROR`；大小写不敏感。无此字段或为空 = 全部沿用 INFO 默认。特殊键 `default` 设置 root logger 级别。详见 **按模块开调试日志** |

服务端 `config_server.json` 同样支持 `access_log`、`drain_threshold` 和 `log_levels`，含义一致。

#### 按模块开调试日志

排查问题时，可以在配置文件里把感兴趣的模块切到 `DEBUG`，无须改代码或环境变量。

```json
"log_levels": {
    "outbound": "DEBUG",
    "server":   "DEBUG"
}
```

可用 logger 名（按职责分组）：

| 模块 | 调高到 DEBUG 后能看到什么 |
|---|---|
| `outbound` | 客户端中继 leg 的对端 RST / Timeout / EOF 异常类型 + 消息；group 选路切换（`switch tokyo-1 (80ms) -> us-west (35ms)`）|
| `server` | 服务端 `tunnel→target` / `target→tunnel` leg 终止异常（含 `conn.id` + 目标）|
| `utils` | `DirectOutbound` 直连路径 leg 终止异常（含 outbound tag + 目标）|
| `router` | 每条规则的展开与命中详情（GEOSITE/GEOIP 内部子条目命中也会显示）|
| `dns` | 每条 DNS 查询的路由决策与出口选择 |
| `conn_pool` | 单条隧道 build 失败原因（**真实** TLS Alert / EOF / 超时具体描述）|
| `group` / `healthcheck` | urltest 候选评分、health probe 触发时刻、节点 unhealthy 标记 |
| `camouflage` / `handshake_cache` | 服务端伪装路径决策与 TLS 1.3 握手缓存刷新 |
| `geo_cache` | geosite / geoip 文件下载与刷新 |
| `socks5` / `tproxy` / `brutal` / `egress` | 协议层 / 内核交互细节 |

**典型场景：**

- "代理莫名其妙断了" → `outbound`（客户端）或 `server`（服务端）开 DEBUG，看到对端 RST/Timeout 的真实类型
- "某域名走错路由" → `router` 开 DEBUG，看具体命中了哪条规则的哪个子条目
- "Pool 一直 warmup 不上" → `conn_pool` 开 DEBUG，看握手在哪一步失败（TLS Alert / EOF / 超时）
- "urltest 老在两个节点之间反复切" → `group` 开 DEBUG，看每次决策的延迟对比

无效级别字符串会被 warning 跳过、不影响启动；无该字段时全部保持 `INFO` 默认。

启动：

```bash
python client.py                        # 默认读 config_client.json
python client.py /path/to/config.json   # 指定配置文件
```

将系统代理设为 `SOCKS5 <本机IP>:1080` 即可使用，局域网内其他设备同样可以指向该地址。

---

## 入站类型

`inbounds[*].type` 三选一，可同时启多个（不同端口）：

| type | 协议 | 典型用法 |
|---|---|---|
| `socks5` | RFC 1928 SOCKS5（含 UDP ASSOCIATE） | `curl --socks5 127.0.0.1:1080 ...` |
| `http` | HTTP/1.1 — CONNECT 隧道 + 绝对 URL forward | `curl -x http://127.0.0.1:8080 ...` |
| `mixed` | **一口同时支持** SOCKS5 + HTTP（peek 首字节自动分发） | Chrome 系统代理、curl 任意 scheme |

### Mixed 分发逻辑

```
peek 第一字节：
  0x05          → SOCKS5
  ASCII 方法首字符（C/G/P/H/D/O/T/A）→ HTTP/1.1
  0x16          → TLS ClientHello → 回 400 提示（代理不监听 TLS）
  其他          → 关连接
```

### HTTP/1.1 入站细节

- **CONNECT** `example.com:443` → 回 `200 Connection established` + 字节中继（HTTPS 网站走代理的标准方式）
- **GET / POST / PUT / DELETE / ...** 必须用**绝对 URL**（`GET http://example.com/path HTTP/1.1`）；自动改写为相对路径 + 剥 `Proxy-*` 头后转发
- 转发后是字节中继；同一连接上的后续请求若到不同 upstream **不再支持**（浏览器对 HTTP forward 自然新开 TCP，可接受）

### 不实现

- **代理本身用 TLS 监听**（"HTTPS proxy"）：需要 cert 配置，极少见
- **Proxy-Authorization 鉴权**：127.0.0.1 用例不需要；LAN 共享时建议配合防火墙限制源 IP

### 推荐配置

```json
"inbounds": [
  {"type": "mixed", "listen": "127.0.0.1:7890"}
]
```

一个 mixed 入口覆盖所有应用层使用方式。

---

## 多节点与自适应选路

`outbounds` 数组中可同时声明多个 `mirage` 节点 + 若干个**组**，组按策略类型决定如何在 child 中挑选实际承担流量的节点。

### 出口类型

| `type` | 角色 | 说明 |
|---|---|---|
| `mirage` | 叶子节点 | 一个具体的服务端，独占一个 BrutalPool |
| `direct` | 叶子节点 | 系统直连，不经过任何代理 |
| `block` | 叶子节点 | 立即关闭本地连接（用于广告 / 屏蔽场景）|
| `urltest` | 组 | 选 children 中 median 握手延迟最低的（自动）|
| `fallback` | 组 | 按声明顺序选第一个 `is_healthy=True` 的（自动）|
| `selector` | 组 | **手动选节点**：通过 Clash API `PUT /proxies/{tag}` 切换，保持到下次手动切换 |

组的 `outbounds` 字段也可以引用**另一个组**，启动期通过 fixpoint 求解依赖（循环引用会立即报错退出）。

### selector：手动选节点

```json
{
    "tag":       "pick",
    "type":      "selector",
    "outbounds": ["node-jp-1", "node-us-1", "auto"],
    "default":   "auto"
}
```

把它设为 `route.default`（或绑到规则），就能在 Yacd / metacubexd 上一键切换走哪个节点。`default` 指定初始选择（缺省取首个 child）。child 可以是叶子节点，也可以是 `urltest` / `fallback` 组（"手动在'自动选'和'指定节点'之间切换"）。

- `PUT /proxies/pick`，body `{"name": "node-us-1"}` → 切到 us 节点，成功 204；非成员返回 400。
- selector 的 `is_healthy` 跟随**当前选中** child（手动选了就认它，不因别的节点健康而假装可用）。
- **选择跨热加载存活**：`outbounds` 是 locked field，reload（SIGHUP / PUT /configs）不重建组对象，当前选择保持不变；只有整进程重启才回到 `default`。

### urltest：自动选最快

```json
{
    "type": "urltest", "tag": "auto",
    "outbounds": ["node-jp-1", "node-jp-2", "node-us-1"],
    "interval": "60s",
    "tolerance": 50
}
```

| 字段 | 说明 |
|---|---|
| `outbounds` | 候选 children（按 tag 引用）|
| `interval` | 主动 probe 兜底周期。**正常路径并不会真的每 60s 网络探测一次**——节点池在持续 warmup/refill，每次 build 的握手耗时直接作为新鲜样本回灌。该字段控制长时间无流量时 `HealthCheck` 多久扫一次、对样本陈旧 > 5min 的节点触发一次 probe |
| `tolerance` | 防抖阈值（毫秒）。`current.latency − best.latency < tolerance` 时**保持当前选择**不切换，避免相近延迟下频繁切换破坏 TCP 长连接复用。默认 50ms |

**延迟样本机制**：每条 `mirage` 节点维护一个 10 样本的滚动窗口，BrutalPool 每次成功 build 完整握手（TCP + ClientHello + 缓存握手回放 + 密钥派生）的耗时直接 push 进去。urltest 决策时取窗口的 median 作为代表值（median 抗瞬时抖动）。

### fallback：按顺序故障转移

```json
{ "type": "fallback", "tag": "us-only",
  "outbounds": ["node-us-1", "node-us-2"] }
```

- 沿 `outbounds` 顺序依次检查每个 child 的 `is_healthy`，**返回第一个健康者**
- 健康判定：节点连续 3 次 build 失败标记 unhealthy（任一次成功立即清零）；组节点的 health = `any(child.is_healthy)`
- 全部不健康时：返回首项让端到端层报真实错误（避免悄无声息地无路可走）

### 把节点 / 组绑到规则

`route.rules` 中每条规则的 `outbound` 字段可以填**任意已定义的 outbound tag**，包括叶子（节点 / direct / block）或组：

```json
"route": {
    "rules": [
        {"rule_set": ["loyalsoldier:netflix"], "outbound": "us-only"},
        {"rule_set": ["loyalsoldier:gfw"],     "outbound": "auto"},
        {"rule_set": ["loyalsoldier:cn"],      "outbound": "direct"}
    ],
    "final": "auto"
}
```

- Netflix → fallback 组 `us-only` → 美国节点优先
- GFW 域名 → urltest 组 `auto` → 当前 median 延迟最低的节点
- CN 域名 → `direct` → 系统直连
- 其余 → `final: auto` 走自动选路

dispatch 日志可看到组对叶子的解析：

```
auto -> node-jp-1 (via urltest)  github.com:443  [#2 GEOSITE loyalsoldier:gfw]
```

### 启动期 warmup 时序

启动时**所有** `mirage` 节点的池**并行** warmup，每个池内的 N 条隧道仍按"真阶梯"延迟 `i × 0.30s ± 0.08s` 错开发起（避免同 IP 同秒批量 TLS 1.3 出口形成代理指纹）。Geo 数据下载会自动复用第一个 warmup 成功的池作为隧道出口（弱网下走自家隧道比直连 GitHub 稳）。

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

TProxy 防火墙规则需要手动配置（`install.sh` 不再自动生成）。下面给出 iptables 模板，按需调整：

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
table ip mirage {
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
nft delete table ip mirage
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
table ip mirage_nat {
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

清除：`nft delete table ip mirage_nat`

如果要做 DNS 捕获（让 LAN 设备的 DNS 查询透明转发），把 `dns_listen_host` 改为 `0.0.0.0` 并在防火墙规则里 redirect 53 端口到 `dns_listen_port`。

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

**两种写法都受支持**——按配置顺序从上到下扫描，首条命中即返回（与 Clash / sing-box / Surge 行为一致）：

| 格式 | 写在哪 | 适合 |
|---|---|---|
| **结构化（sing-box）** | `route.rules` 数组 + `route.final` | 多节点 / 自适应选路；推荐 |
| **CSV（老格式）** | `rules` 字符串数组 + `FINAL,X` 行 | 老配置；单节点；简单上手 |

`action` / `outbound` 字段填 **任意已定义的 outbound tag**（节点 / 组 / `direct` / `block`），不再限定 `PROXY` / `DIRECT` / `REJECT` 三个关键字。

### 结构化 rules（sing-box 风格）

```json
"route": {
    "rules": [
        {"rule_set":       ["loyalsoldier:category-ads-all"], "outbound": "block"},
        {"domain_suffix":  ["openai.com", "anthropic.com"],   "outbound": "us-only"},
        {"domain":         ["example.com"],                   "outbound": "direct"},
        {"domain_keyword": ["bilibili"],                      "outbound": "direct"},
        {"domain_regex":   ["^(?:.+\\.)?google\\.com$"],      "outbound": "auto"},
        {"ip_cidr":        ["192.168.0.0/16", "10.0.0.0/8"],  "outbound": "direct"},
        {"geoip":          ["loyalsoldier:cn"],               "outbound": "direct"},
        {"geoip":          ["loyalsoldier:cn"], "invert": true, "outbound": "auto"}
    ],
    "final": "auto"
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `domain` | string[] | 精确匹配 |
| `domain_suffix` | string[] | 后缀匹配（含本身及所有子域名；首位 `.` 可选）|
| `domain_keyword` | string[] | 子串包含 |
| `domain_regex` | string[] | Python `re.search`，编译时 `IGNORECASE` |
| `ip_cidr` | string[] | IPv4/IPv6 CIDR |
| `rule_set` | string[] | geosite.dat 中的 tag，`source:` 前缀可选 |
| `geoip` | string[] | geoip.dat 中的国家代码，`source:` 前缀可选 |
| `invert` | bool | `true` 时整条规则的命中/未命中取反（典型场景：`{"geoip":["cn"],"invert":true}` = 非 CN IP）|
| `outbound` | string | 命中后路由到的 outbound tag（必填）|

**多 criterion 默认 = OR**：一条 rule 写多个 criterion 字段时，**任一字段命中即命中**（等价于把它们拆成多条指向同一 outbound 的独立规则）：

```jsonc
// 域名属 geosite:google 或 解析 IP 属 geoip:us，任一满足就走 us-node
{"rule_set": ["geosite:google"], "geoip": ["us"], "outbound": "us-node"}
```

**显式 `"mode": "and"` → AND 复合**：需要"全部字段都满足才命中"时，加 `"mode": "and"`（缺省 `"or"`）：

```jsonc
// 域名属 geosite:google 且 解析 IP 属 geoip:us 才走 us-node
{"rule_set": ["geosite:google"], "geoip": ["us"], "mode": "and", "outbound": "us-node"}
```

AND 模式下字段内仍是 OR、字段间是 AND；某字段在空 geo / 解析失败下展开为空时，整条 AND 跳过（不半命中）。默认 OR 模式下，空字段被跳过但其余字段仍生效。`mode` 非法值（非 `or`/`and`）会告警并退回 `or`。其余 sing-box 字段（`protocol` / `process_name` 等）当前不解析，写了也不报错、不生效。

> 注意：这与 sing-box 不同——sing-box 多字段默认 AND。Mirage 默认 OR 更贴近"把相关条件并到一条规则"的直觉，需要 AND 时显式 `"mode": "and"`。

**数组语义 = OR**：`{"domain_suffix":["a.com","b.com"]}` 等价于"a.com 或 b.com 后缀匹配"。

### CSV rules（老格式 / 向后兼容）

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

CSV 的 action 部分对老关键字 `PROXY` / `DIRECT` / `REJECT` 自动映射到 `proxy` / `direct` / `block` 三个 outbound tag（前提是这些 tag 存在；老单节点配置中 `proxy` 由系统合成）；其它任意名（如 `tokyo-1`、`auto`）则原样作为 outbound tag 查找。

> **TProxy 模式补充说明**：TProxy 只能从内核获得目标 IP，但客户端会在连接建立后立即嗅探初始字节提取 TLS SNI / HTTP Host，因此域名类规则（domain / domain_suffix / rule_set 等）在 TProxy 模式下同样有效。无法嗅探到域名时（非 TLS/HTTP 协议）自动退化为 IP 规则匹配。

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

## schema_version=1 合约

`schema_version` 字段是配置文件的**契约**：

- **8 个结构化 section 锁定**：`schema_version` / `log` / `inbounds` / `outbounds` / `route` / `dns` / `api` / `tuning`。`schema_version=1` 期间不新增第 9 个**结构化 section**——新功能进入已有 section 的 nested key，不另起顶层 section
- **少量扁平的运维 / 路径标量键**可在顶层并存（不算"section"）：`log_levels`、`geosite_dir` / `geosite_sources` / `geoip_sources` / `geosite_update_days`、`tproxy_port`、`geo_refresh_check_sec`，以及服务端的 `listen_*` / `camouflage_*` / `admin_*` / `egress*` / 运行时调优项。这些都在 `core/config._V1_TOP_KEYS` 白名单内，schema 校验认得、不报 unknown
- **类型特定字段塞进 outbound 的 `params` 子对象**，新增协议变种 = 新 `type`，schema 不动
- **复杂调优用命名预设**，不开放裸参数（如 `transport: "brutal-default"`）

### 合约表

| 改动类型 | 允许在哪个版本 |
|---|---|
| 新顶层 section | 只 MAJOR（schema_version 升号） |
| 新 nested key（带默认值） | MINOR |
| 改字段含义 / 移除 | 只 MAJOR |
| 重命名（保留老名为 alias） | MINOR，老名至少保留 3 个 MINOR |
| 默认值变化 | PATCH（CHANGELOG 必须标注） |
| 校验严格度提升 | MINOR，先 WARN 一轮再 ERROR |

### 向后兼容

- 无 `schema_version` + 命中 legacy 顶层 key（`server_host` / `password` / `socks5_host` 等）→ 启动期 INFO `Legacy schema detected`，行为保持现有
- 服务端**当前仍是 legacy 平铺格式**，没有 schema_version 字段（避免触发 unknown-top-keys WARN）
- 老 `config_client.json` / `config_server.json` 不需要改动

---

## Clash 兼容 API（client 端只读）

启用 `api.listen` + `api.secret` 后，客户端启动一个 HTTP/1.1 + WebSocket 服务器，与 Yacd / metacubexd 等 Clash UI 100% 兼容。零外部依赖（仅 stdlib + asyncio）。

### 端点清单（17 个）

| 路径 | 方法 | 用途 |
|---|---|---|
| `/version` | GET | `{"version":"...","meta":true}`；UI 检测心跳 |
| `/configs` | GET | 当前 cfg（脱敏）+ Clash 标准字段（socks-port / mode 等） |
| `/configs` | **PUT** | **触发配置热加载**（与 SIGHUP 等效） |
| `/proxies` | GET | 所有 outbound + 合成 `GLOBAL` Selector + `DIRECT`/`REJECT` 别名 |
| `/proxies/{name}` | GET | 单个 outbound（含合成 `GLOBAL`） |
| `/proxies/{name}` | **PUT** | 切节点（body `{"name":"<child>"}`）。目标是 `selector` 组 → 真正切换（非成员 400）；urltest / fallback / GLOBAL 等自动组 → no-op 204 |
| `/proxies/{name}/delay` | GET | 直读该节点 `latency_ms`（健康检查已采集，不触发额外 probe） |
| `/rules` | GET | 路由规则（Clash CamelCase 类型，末尾自动追加 `Match`） |
| `/connections` | GET | 活跃 + 5s linger 的关闭连接，含 up/down/chains/rule/memory |
| `/traffic` | **WS** | 1Hz 推 `{"up": B/s, "down": B/s}` |
| `/logs?level=info` | **WS** | 实时日志流（按 level 过滤） |
| `/memory` | **WS** | 1Hz 推 `{"inuse": RSS, "oslimit": 0}`（读 `/proc/self/statm`） |
| `/connections` | **WS** | 1Hz 推全量连接快照（schema 同 REST `/connections`） |
| `/mirage/pool` | GET | BrutalPool 实时（ready / building / cursor / latency / healthy） |
| `/mirage/timesync` | GET | offset / last_source / last_sync_epoch |
| `/mirage/geo` | GET | meta.json 视图（cache_dir / update_days / sources[]） |
| `/mirage/cache` | GET | 决策缓存命中率（见 **DNS 转发器**） |

### 鉴权

- `Authorization: Bearer <secret>` 或 `?token=<secret>`（兼容 Yacd 老用法）
- secret 常量时间比较；强制 127.0.0.1 绑定（0.0.0.0 启动期 WARN）

### Yacd 接入

```
host=127.0.0.1  port=9090  secret=<api.secret>
```

### 控制类端点说明

- `PUT /proxies/{name}` 切节点（body `{"name": "<child-tag>"}`）：
  - 目标是 **`selector` 组** → **真正切换**选中 child，保持到下次手动切（非成员
    返回 400）。在 Yacd / metacubexd 上点选即时生效。
  - 目标是 **urltest / fallback / 合成 GLOBAL** 等自动选路组 → 没有 Selector 语义，
    **no-op 204**（点选"成功"但实际不改变自动选路，仅防 UI 报错）。
- `GET /proxies/{name}/delay`：直接返回健康检查已采集的 `latency_ms`，**不触发
  独立 probe**（避免 UI 反复点击引发外部测速）。无延迟样本的节点返回 400。
- `DELETE /connections/{id}` 杀连接：**不实现**。Clash API 的连接控制类按设计不做
  （服务端 Web 管理面板提供断开 / 封 IP）。

---

## DNS 转发器（含 DoH / DoT + 决策缓存）

设了 `dns.listen` 后客户端起一个 UDP DNS 服务。每个查询：

1. **DnsCache 命中** → 直接返回（替换 tx_id）
2. miss → router.match(domain) → 决定走 `direct`（用 `cn_dns`）或 `mirage`（用 `remote_dns`，经隧道）
3. 上游响应后写两层缓存

### remote_dns 的三种 scheme

| 形式 | 实现 |
|---|---|
| `1.1.1.1:53` / `dns://1.1.1.1:53` | UDP：经隧道到 server，server 端 plain TCP forward 到 1.1.1.1:53 |
| `tls://1.1.1.1:853` | **DoT**：经隧道到 server pass-through TCP，**客户端进程内 TLS 1.2+ 端到端** + 长度前缀 DNS pipeline |
| `https://1.1.1.1/dns-query` | **DoH**：同上 + HTTP/1.1 POST `application/dns-message`，单连接 keep-alive 串行 |

**DoH URL 的 host 必须是 IP literal**（防 bootstrap 死循环）。三种 scheme **服务端代码完全透明**，TLS 由客户端做端到端，server 看不到明文 DNS。

### 决策缓存（无脑开，无需配置）

- `RoutingCache`：domain → outbound_tag，TTL 1h（路由规则静态）
- `DnsCache`：(domain, qtype) → 原始 DNS 响应字节，**TTL 从 answer 段直接抽 `min(TTL)`**（截断到 30s-3600s）
- LRU 上限各 10k entries，**总内存 ~1MB**
- 实测：同域名第二次查询从 **~400ms（DoH 全程） 降到 ~0.3ms（dict 查找 + tx_id 重写）**

### `tuning` 里相关参数

```json
"tuning": {
  "routing_cache": {"enabled": true, "max_entries": 10000, "ttl_sec": 3600},
  "dns_cache":     {"enabled": true, "max_entries": 10000}
}
```

`/mirage/cache` 端点查实时命中率。

---

## 配置热加载

改完 `config_client.json` 后两种触发方式：

```bash
# 方式 1：systemd / OpenRC / SysV 都支持的 reload 子命令
systemctl reload mirage-client
rc-service  mirage-client reload
service     mirage-client reload

# 方式 2：Clash API（需 api.listen）
curl -X PUT -H "Authorization: Bearer <secret>" http://127.0.0.1:9090/configs

# 方式 3：直接发 SIGHUP
kill -HUP $(pgrep -f 'python3.*client.py')
```

### 可热加载

| 字段 | 行为 |
|---|---|
| `route.rules` / `route.default` | rebuild Router + routing_cache 清空 + dns_cache 清空 |
| `cn_dns` / `remote_dns`（含 DoH / DoT scheme 切换） | DNSForwarder.reload：drop _tunnels，下次查询按新地址重建 |
| `log.format` / `log_levels` | 立即应用 |
| `tuning.access_log` 等运行时可调项 | 注册的 handler 回调 |

### 不动（locked field）

`schema_version` / `inbounds` / `outbounds` / `api.listen` / `api.secret` / legacy 顶层鉴权字段。检测到改动 → `warnings: ["locked: <field>"]`，新值不生效。改这些需 restart。

### 不打断现有连接

- outbounds + 池不动，正在跑的 TCP 隧道继续用
- 新 dispatch 命中新路由
- DNS upstream 跌掉时旧 in-flight 查询抛 OSError 回退 NXDOMAIN，下一次重建

---

## 结构化日志

`cfg.log.format` 决定输出格式：

| 值 | 输出 |
|---|---|
| `"text"`（默认） | `2026-06-11 14:24:07,896 [INFO] config: dns.default not set; using first resolver 'proxy-dns'` |
| `"json"` | 每行一个 JSON |

JSON schema：

| 字段 | 类型 | 含义 |
|---|---|---|
| `ts` | string | ISO-8601 UTC，毫秒精度 `2026-06-11T06:29:48.416Z` |
| `level` | string | `debug` / `info` / `warning` / `error` / `critical` |
| `logger` | string | logger 名（`client` / `conn_pool` / `time_sync` 等） |
| `msg` | string | 格式化后的消息 |
| `extra` | object | 可选；调用方传 `extra=` 的字段集合 |
| `exc` | string | 可选；异常时的 traceback 文本 |

### 解析示例

```bash
# 看 conn_pool 的所有 WARNING+
cat client.log | jq 'select(.level=="warning" or .level=="error") | select(.logger=="conn_pool")'

# 按 outbound 筛连接级日志
cat client.log | jq 'select(.extra.outbound=="proxy")'
```

WS `/logs` 端点的 `payload` 字段始终是紧凑文本（Clash 协议要求），**不**随 `cfg.log.format` 改变。

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

或运行 `sudo bash install.sh` 选择服务端，向导检测到 Brutal 模块可用时会询问速率并写入 cfg。

**速率设置建议：**

- 单连接速率建议设为实际上行带宽的 80%–100%（例如 VPS 带宽 100 Mbps → 设 80–100 Mbps）
- `config_server.json` 中的 `brutal_rate_bps` 控制服务端向客户端发送时的速率

---

## 系统服务

`install.sh` 自动检测 init 系统并生成对应 unit：**systemd**（多数主流发行版）/ **OpenRC**（Alpine、Gentoo）/ **SysV init.d**（CentOS 6、老 Debian 等）。三种都支持 `reload` 子命令（发 SIGHUP 触发 0.4.25 的热加载）。

### systemd

适用于：Debian、Ubuntu、CentOS 8+、Fedora、Arch 等主流发行版。

若非 root 运行向导，脚本会写到当前目录（`mirage-server.service`），手动安装：

```bash
sudo cp mirage-server.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable mirage-server   # 设置开机自启
sudo systemctl start  mirage-server   # 立即启动
```

日常管理：

```bash
systemctl status  mirage-server
systemctl restart mirage-server
journalctl -u mirage-server -f        # 实时日志
```

### SysV init

适用于：Debian 8 以下、CentOS 6、旧版 OpenWrt 等。

```bash
sudo cp mirage-server.init /etc/init.d/mirage-server
sudo chmod +x /etc/init.d/mirage-server
sudo update-rc.d mirage-server defaults   # Debian/Ubuntu
# 或
sudo chkconfig --add mirage-server        # RHEL/CentOS
sudo service mirage-server start
```

日常管理：

```bash
service mirage-server status
service mirage-server restart
tail -f /var/log/mirage-server.log        # 实时日志
```

### OpenRC（Alpine Linux）

Alpine 默认使用 OpenRC，向导会自动识别并生成 `mirage-server.openrc`：

```bash
sudo cp mirage-server.openrc /etc/init.d/mirage-server
sudo chmod +x /etc/init.d/mirage-server
sudo rc-update add mirage-server default  # 加入 default runlevel
sudo rc-service mirage-server start
```

日常管理：

```bash
rc-service mirage-server status
rc-service mirage-server restart
tail -f /var/log/mirage-server.log
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
mirage-proxy/
├── server.py              服务端入口
├── client.py              客户端入口（SOCKS5 + 分流 + 连接池）
├── install.sh             一键交互式部署向导（服务端 / 客户端 / 两端）
├── bench.py               吞吐 + 延迟基准（DNS / SOCKS5 / 各种 scenario）
├── config_server.json            服务端运行时配置（install.sh 生成；也可手编）
├── config_client.json            客户端运行时配置（install.sh 生成；也可手编）
├── config_server.example.jsonc   服务端带注释示例（JSONC，供参考）
├── config_client.example.jsonc   客户端带注释示例（JSONC，schema_v1 完整字段）
├── tests/                 测试脚本（throughput / gfw_probe / admin / traffic_analyzer / run_test*.sh）
└── core/
    ├── hello_auth.py      ClientHello token 生成与验证（Poly1305 + 时间戳掩码 + nonce 防重放）
    ├── camouflage.py      服务端 TLS 伪装决策（认证通过 → 代理模式；探测/重放 → 回放缓存）
    ├── handshake_cache.py 服务端握手缓存池（32 份 TLS 1.3 记录轮换 + 每小时刷新）
    ├── tls_raw.py         TLS ClientHello 构造（Chrome / Firefox / Safari 三档随机指纹）
    ├── tunnel.py          ChaCha20-Poly1305 加密信道（TLS 0x17 帧格式 + 双向独立密钥）
    ├── outbound.py        客户端出口抽象（叶子节点 + 工厂；含老 server_host 配置的自动合成）
    ├── group.py           组节点策略（UrlTestGroup tolerance 防抖 / FallbackGroup 顺序故障转移）
    ├── healthcheck.py     节点延迟主动 probe 兜底（被动样本陈旧 > 5min 时触发）
    ├── conn_pool.py       per-outbound 预建连接池（BrutalPool；阶梯 jitter + 握手延迟回调）
    ├── socks5.py          本地 SOCKS5 协议解析
    ├── router.py          分流路由（结构化 / CSV 双格式 + geosite.dat / geoip.dat 解析）
    ├── geosite_cache.py   GeoSite/GeoIP 多源缓存管理（下载 / 刷新 / meta.json）
    ├── bloom.py           Bloom Filter（域名后缀预过滤，Kirsch-Mitzenmacher 双哈希）
    ├── sniffer.py         流量嗅探（TLS SNI + HTTP Host 提取域名，PrefixedReader）
    ├── dns_forwarder.py   DNS 转发器（按 outbound 决策分流；每 mirage 节点独占 DoT pipeline）
    ├── stats.py           服务端连接统计与 IP 封锁表（StatsStore）
    ├── admin.py           Web 管理面板（内嵌 HTTP 服务，排序 / 着色，无外部依赖）
    ├── brutal.py          TCP Brutal socket 选项封装
    ├── tproxy.py          TProxy 透明代理监听器（IP_TRANSPARENT socket，仅 Linux）
    ├── egress.py          服务端出口抽象（默认路由 / SO_MARK 策略路由，用于 WARP 等）
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

### 单元测试套件

纯 stdlib `unittest`，零第三方依赖，秒级跑完：

```bash
bash tests/run_tests.sh           # 单元测试（无网络依赖，CI 友好）
bash tests/run_tests.sh --smoke   # 额外跑端到端冒烟（需联网抓 camouflage TLS）
# 或直接：
python3 -m unittest discover -s tests -p 'test_*.py'
```

覆盖九大模块的回归点 + schema-代码契约元测试：

| 测试文件 | 覆盖 |
|---|---|
| `test_router.py` | 路由解析、`route.default`/`final` 优先级、`rule_set`→GEOSITE 映射、多字段 OR/AND（`mode`）、空 geo 存活 |
| `test_config.py` | schema 白名单、dns 块投影、deprecation 提示、server 端键、**schema-代码契约元测试**（扫 server.py/client.py 真读的 cfg 键，未声明即报） |
| `test_selector.py` | SelectorGroup 选择 / 健康跟随 / 嵌套组 |
| `test_udp_relay.py` | UDP 帧 + SOCKS5 头编解码、畸形输入安全 |
| `test_dns_forwarder.py` | DNS 报文解析、`_nxdomain`、cn_dns 地址解析 |
| `test_hello_auth.py` | token 认证 / 防重放 / anti-fingerprint（可控时钟） |
| `test_tunnel.py` | ChaCha20-Poly1305 加密往返（socketpair）、方向密钥、close_notify |
| `test_api_endpoints.py` | Clash 端点、CORS、selector 切换、密码脱敏 |
| `test_ws_endpoints.py` | LogBroadcaster、`_rss_bytes` |
| `test_install_config.py` | **install.sh 生成的 cfg** 用 `load_config` 校验（字段名 / schema 回归） |

`smoke_e2e.py` 真起 server + client，经 mixed 入口走加密隧道访问本地 target，验证完整数据路径（需出网抓 `camouflage_host` 的 TLS 会话）。

### 多版本 Python 兼容性测试

Mirage 目标支持 **Python 3.9+**。仓库附带跨版本冒烟测试脚本，跑一次能快速发现"在某个 Python 上语法不兼容"或"模块导入崩"这类回归。

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
- 装 `cryptography` 后对全部 `core.*` 模块 + `client.py` / `server.py` 做 `import` 测试
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

Mirage 通过 `from __future__ import annotations` 让**类型注解**走字符串延迟求值，所以 `def f(x: int | None)` 在 3.9 也合法。但下面这些**运行时表达式**仍要求 3.10+：

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
