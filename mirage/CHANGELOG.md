# CHANGELOG

按 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/) 风格组织，版本号遵循
[SemVer](https://semver.org/lang/zh-CN/)。

`0.x` 表示协议 / 配置 schema 仍可能在 MINOR 升级时变动，**向后兼容由每条版本
条目单独说明**——影响升级路径的改动会在"迁移"小节里写清楚。

类别约定：

- **新增** —— 新功能 / 新可配置项
- **修改** —— 已有行为的变化（不是 bug，是设计调整）
- **修复** —— bug 修复
- **安全** —— 安全相关修复，无论是否同时是 bug
- **内部** —— 重构 / 性能 / 代码组织，对用户无可见影响
- **迁移** —— 老配置 / 老代码升级时需要做什么

---

## [0.4.40] - 2026-06-13

### 改动 / 客户端 install 允许用户填入 cfg 路径

0.4.39 的自动识别只看默认位置（system: `/etc/mirage/config_client.json`、
inplace: `${WORK_DIR}/config_client.json`）。现实场景里用户更常 scp 到 `~/` 或
`/root/`，导致检测不到。

本版本改为**两种情况都让用户填路径**：

**默认位置已存在**：

```
[*] 默认位置已有配置：/etc/mirage/config_client.json
    config_client.json 路径（回车用默认；输 '-' 跳过；或输绝对路径用其他）：
    [/etc/mirage/config_client.json]
```

- 回车 → 用默认
- 输绝对路径（如 `/root/from-server.json`） → 用那个
- 输 `-` → 跳过自动识别，全部字段从头问

**默认位置不存在**：

```
[*] 默认位置无现有配置：/etc/mirage/config_client.json
    已有 config_client.json 想导入？输绝对路径（留空跳过、自行填全部字段）：
```

- 空 → 跳过
- 路径 → 导入

### 错误处理

| 情况 | 行为 |
|---|---|
| 路径不存在 | WARN + 跳过自动识别（继续走完整问询） |
| 文件存在但 JSON 损坏 / 缺 mirage outbound | 同上 |
| 解析成功 | 显示连接信息 + 路由 / DNS / API 摘要，问是否复用 |

### 典型流程

```bash
# 服务端：bash install.sh → 控制台显示客户端 cfg
# 用户：scp client.json user@client:~/
# 客户端：bash install.sh → install_client
#   询问 cfg 路径 → 输入 /root/client.json
#   检测成功 → 显示摘要 → 复用连接信息
#   只问路由 / DNS / API / 日志
#   写出 cfg 到 /etc/mirage/ + 启动 systemd
```

### 验证

任意路径的 cfg 都能被 `_load_existing_client_cfg` 正确解析：

```
parsed:
  server: 203.0.113.45:443
  pwd:    pwd-from-scp
  sni:    www.bing.com
  port:   8888
```

---

## [0.4.39] - 2026-06-13

### 改动 / 客户端安装：检测现有 cfg + 服务端模板带分流

两件事联动解决"复制服务端打印的模板到客户端 → 缺分流"的痛点：

#### A. 服务端尾声打印的客户端模板默认带分流 + DNS

之前是最简版（`route.rules: []` 无分流，无 DNS）。改成 china_split 默认值：

```diff
- "route": {"default": "proxy", "rules": []}
+ "route": {
+     "default": "proxy",
+     "rules": [
+         {"ip_cidr": ["127.0.0.0/8", "10.0.0.0/8", ...], "outbound": "direct"},
+         {"geosite": ["loyalsoldier:cn"], "outbound": "direct"},
+         {"geoip":   ["loyalsoldier:cn"], "outbound": "direct"}
+     ]
+ },
+ "dns_listen_host": "127.0.0.1",
+ "dns_listen_port": 5353,
+ "cn_dns": "119.29.29.29",
+ "remote_dns": "1.1.1.1:53"
```

用户 scp 这个模板到客户端，开箱即用国内外分流。

#### B. 客户端 install.sh 检测现有 config_client.json

`install_client` 启动时先检查 `${EFFECTIVE_ETC}/config_client.json`，用
`python3` 解析提取：

| 字段 | 来源 |
|---|---|
| server_host / server_port | `outbounds[*].type=mirage` 节点 |
| password / camouflage_host | 同上 |
| socks5_port | `inbounds[0].listen` |
| rules_count | `len(route.rules)` |
| dns_listen / api_listen | 顶层 cfg 字段 |

检测到后打印摘要 + 询问：

```
[*] 检测到现有客户端配置：/etc/mirage/config_client.json
[*]   服务端：52.221.254.189:4433
[*]   伪装 SNI：speedtest.net
[*]   路由规则：0 条
[*]   DNS 转发器：未启用
[*]   Clash API：未启用
    复用上面的连接信息，只补齐分流 / DNS / API / 日志？ (Y/n)
```

选 Y → 跳过 server_host / port / password / camouflage / socks5_port 的询问，
直接进入路由模板 / DNS 方案 / API / 日志的问答。

#### 兼容 legacy outbound type

`type` 字段支持 `"mirage"`（新）和 `"pyrealiy"`（0.4.34 之前的命名）。

### 实测

```
detected
  server_host: 52.221.254.189
  server_port: 4433
  password:    test-pass-abc
  camouflage:  speedtest.net
  socks5_port: 7890
  rules_count: 0 (空 → 提示补齐)
  dns_listen:  '' (空 → 未启用)
  api_listen:  '' (空 → 未启用)
```

### 用户体感

服务端跑完安装 → 服务端控制台显示完整客户端 cfg →
`scp config_client.json` 到客户端 → 客户端跑 `bash install.sh` →
自动检测、跳过连接信息、专注问分流策略 → 写出完整 cfg + systemd unit。

---

## [0.4.38] - 2026-06-13

### 改动 / 日志轮转换成进程内管理（替代 logrotate）

0.4.37 用 logrotate + `/etc/logrotate.d/mirage-*` 做轮转。本版本改为 Mirage 进程
自己用 Python `RotatingFileHandler` + gzip 做。

**为什么换**：

| 维度 | logrotate | 进程内 |
|---|---|---|
| 配置位置 | `/etc/logrotate.d/mirage-*` 单独文件 | `cfg.log.*` 单一来源 |
| 跨 init | 依赖 logrotate cron | 与 init 无关 |
| copytruncate hack | 必须（systemd append fd） | 不需要——进程自己管 fd |
| 触发时机 | 每日 cron 检查 | 每次写日志精确检查 size |
| 卸载 | 要清 /etc/logrotate.d/ | 删 cfg 即停 |

### 新 cfg.log schema

```json
"log": {
  "format":       "text",                          // text | json
  "file":         "/var/log/mirage/server.log",    // 缺省走 stderr
  "max_bytes":    104857600,                       // 100 MB
  "backup_count": 7,                               // 历史份数
  "compress":     true                             // gzip 历史
}
```

- `file` 缺省 → 走 stderr / systemd journal
- `file` 设了 → Mirage 自己写文件 + 按 size 轮转 + gzip 历史

### 实现

**`core/utils.py`**：

- `apply_log_format(cfg)` 扩展：识别 `cfg.log.file`，挂 `RotatingFileHandler`，
  移除 basicConfig 的 stderr handler 避免双写
- 新类 `_CompressedRotatingFileHandler`：完全接管 `doRollover` —— 标准
  `RotatingFileHandler` 不认 `.gz` 扩展，多次轮转会丢历史；这里：
  1. 关 stream
  2. `.N.gz → .(N+1).gz` 倒序链式重命名，超 backup_count 的删
  3. 当前文件 → `.1`，立即 gzip 成 `.1.gz`
  4. 重新打开

### init unit 模板自适应

| init | `cfg.log.file` 设了 | 未设 |
|---|---|---|
| systemd | `StandardOutput=journal` `StandardError=journal`（兜底捕获 early/crash） | `append:LOG`（旧行为） |
| OpenRC | `output_log="/dev/null"` | `output_log=LOG` |
| SysV | `nohup ... > /dev/null 2>&1 &` | `>> $LOGFILE 2>&1` |

### install.sh 交互

`ask_log_config` 改：

```
日志格式：[1] text / [2] json
启用进程内文件日志 + 自动轮转？（推荐）(Y/n)
  单文件最大体积（K/M/G 后缀）：[100M]
  保留几份历史：[7]
  压缩历史日志（gzip）？(Y/n)
```

`_to_bytes` helper 把 "100M" 转成 104857600 等整数（cfg 用字节）。

### 不再生成的

- ✗ `/etc/logrotate.d/mirage-*` 不再写（旧版残余 install.sh 卸载时仍清）

### 实测

```
maxBytes=200, backup_count=3, 30 条 80B 消息：
  test.log       142  (current)
  test.log.1.gz   99  (msg 26-27, 最新)
  test.log.2.gz   99  (msg 24-25)
  test.log.3.gz   99  (msg 22-23, 最旧)
```

gzip 解压可还原内容；超过 backup_count 的最老一份会被丢掉。

```
server cfg log → {"format":"json","file":"/var/log/.../server.log",
                  "max_bytes":104857600,"backup_count":7,"compress":true}
client cfg log → 同上 (client.log)
```

JSON 全合法。

### 向后兼容

旧 cfg（仅 `"log": {"format": "text"}`）行为不变——走 stderr，无文件轮转。

---

## [0.4.37] - 2026-06-12

### 新增 / install.sh 日志配置交互 + 客户端默认 mixed

**日志配置在向导里集成**——之前服务端 / 客户端的 `cfg.log.format` 硬编码 `"text"`，
logrotate 完全没配，日志文件无限增长。本版本加 `ask_log_config()` 步骤：

| 询问项 | 默认 | 说明 |
|---|---|---|
| 日志格式 | `text` | `text`（人类可读）或 `json`（Loki/ELK/jq） |
| 启用 logrotate | `Yes`（system 模式） | inplace 模式不配（用户自管） |
| 单文件最大体积 | `100M` | 支持 K/M/G 后缀 |
| 保留历史份数 | `7`（一周） | 达到上限丢最老的 |
| 压缩历史日志 | `Yes` | gzip + delaycompress |

生成的 logrotate 配置 `/etc/logrotate.d/mirage-{server,client}`：

```
/var/log/mirage/server.log {
    daily
    rotate 7
    maxsize 100M
    missingok
    notifempty
    compress
    delaycompress
    copytruncate
}
```

**关键点 `copytruncate`**：systemd 的 `StandardOutput=append:LOG` 持续打开文件，
logrotate 默认 rename + 通知进程 reopen 那套不适用；`copytruncate` 拷贝后原地清空
，与 always-open fd 兼容。

### 客户端模板默认 inbound 改 `mixed`

服务端尾声打印的快速客户端配置模板之前是 `socks5`：

```diff
- "inbounds": [{"type": "socks5", "listen": "127.0.0.1:1080"}]
+ "inbounds": [{"type": "mixed",  "listen": "127.0.0.1:7890"}]
```

`mixed` 一口同时支持 SOCKS5 + HTTP/CONNECT + HTTP forward，Chrome 系统代理无脑
设这一个端口就能用。

### uninstall 同步

`/etc/logrotate.d/mirage-*` 在卸载流程里一并清理（含 legacy `pyrealiy-*` 残余）。

### 验证

| 检查 | 结果 |
|---|---|
| `bash -n install.sh` | ✓ |
| logrotate 模板渲染 | ✓ daily / rotate N / maxsize / compress 全正确 |
| cfg.log.format 用 `$LOG_FORMAT` 替换 | ✓ server + client 两处 |
| 服务端打印的客户端模板用 mixed | ✓ |

### 联动

- `ask_log_config` 在两端流程里插在 `install_system_files` 之前（先问完所有交互
  再开始动文件系统）
- inplace 模式跳过 logrotate（日志在 `$WORK_DIR`，开发用，无需轮转）

---

## [0.4.36] - 2026-06-12

### 修复 / install.sh 两个 bug

**B1：服务端尾声打印的客户端配置模板里 `camouflage_host` 字段被探测消息污染**

现象：
```
"camouflage_host": "[*] 探测 speedtest.net:443 TLS 1.3 支持...
[✓] speedtest.net 支持 TLS 1.3，握手成功
speedtest.net"
```

根因：`ask_camouflage_host` 内部调 `probe_camouflage`，后者用 `info` / `ok`
打的状态消息走 stdout，被外层 `camouflage=$(ask_camouflage_host ...)` 一并捕获。

修：`info` / `ok` / `warn` / `title` 全部改写 stderr。所有信息消息仍可见但不
污染 `$(funcname)` 捕获的数据。

**B2：Brutal 选"装"分支没真装**

现象：用户回答"需要为本机安装 Brutal" → 只打了文档链接 + 让用户手动装。

修：实际跑官方一键脚本 `curl -fsSL https://tcp.hy2.sh/ | bash`，跑完后
`brutal_loaded` 校验内核模块是否真就绪。失败 / 内核不匹配时给具体排查命令
（`modinfo brutal`、`dmesg | tail`、`sysctl net.ipv4.tcp_available_congestion_control`）。
没装 curl 自动按 PKG_MGR 装。

### 验证

| 场景 | 结果 |
|---|---|
| `result=$(ask_camouflage_host "speedtest.net")` | result = `speedtest.net`（纯净，无探测文本） |
| `bash -n install.sh` | ✓ |
| Brutal install 路径检查 | curl 命令存在 + 跑完后调 `brutal_loaded` 重新校验 |

---

## [0.4.35] - 2026-06-12

### 改动 / 项目改名 PyRealiy → Mirage

`PyRealiy` 原是 `PyReality` 的 typo（reality 漏一字母），错字延续了 30+ 版本。
本版本起改名为 **Mirage**（海市蜃楼，匹配"让 GFW 看到看似真实但其实不存在的
Apple 流量"的核心意境）。Rust 重写版叫 **Mirage-rs**。

### 改名范围

| 类别 | 旧 | 新 |
|---|---|---|
| 项目名 | PyRealiy / PyReality | Mirage |
| Python 类名 | `PyrealiyOutbound` | `MirageOutbound` |
| Outbound type | `"pyrealiy"` | `"mirage"` |
| API 路径 | `/pyrealiy/{pool,timesync,geo,cache}` | `/mirage/{pool,timesync,geo,cache}` |
| 环境变量 | `PYREALIY_NO_UVLOOP` | `MIRAGE_NO_UVLOOP` |
| install path | `/opt/pyrealiy/` | `/opt/mirage/` |
| etc | `/etc/pyrealiy/` | `/etc/mirage/` |
| logs | `/var/log/pyrealiy/` | `/var/log/mirage/` |
| state | `/var/lib/pyrealiy/` | `/var/lib/mirage/` |
| shim | `pyrealiy-{server,client}` | `mirage-{server,client}` |
| systemd unit | `pyrealiy-{server,client}.service` | `mirage-{server,client}.service` |
| 内部文件 | `core/api/pyrealiy_endpoints.py` | `core/api/mirage_endpoints.py` |

### Backward Compat 保留

不让现有用户的配置 / 集成立刻断：

| 兼容点 | 行为 |
|---|---|
| Outbound `type: "pyrealiy"` | 仍工作；启动期 INFO 提示请改为 `"mirage"` |
| API `/pyrealiy/*` 路径 | 仍可访问（与 `/mirage/*` 注册同一 handler） |
| 环境变量 `PYREALIY_NO_UVLOOP=1` | 与新的 `MIRAGE_NO_UVLOOP=1` 等价 |
| **install.sh uninstall** | 同时清理 `/opt/pyrealiy/` + `/etc/pyrealiy/` + `/var/log/pyrealiy/` + `/var/lib/pyrealiy/` + legacy `pyrealiy-*` shim + service unit |

### 不动的

- Git commit history 里旧 commit message 仍写"PyRealiy"（不重写历史）
- CHANGELOG 中已发布的 0.3.x / 0.4.x 旧条目里的"PyRealiy"已机械替换为"Mirage"（文档内称呼一致；不追求历史准确）
- 配置 schema 字段（如 `password` / `camouflage_host` / `brutal_rate_bps`）无任何改动

### 实施

- 322 处文本替换（4 种 case 形式：`pyrealiy` / `Pyrealiy` / `PyRealiy` / `PyReality` → `mirage` / `Mirage`）
- 文件改名：`pyrealiy_endpoints.py` → `mirage_endpoints.py`
- 4 个 memory 文件改名（`pyrealiy_*.md` → `mirage_*.md`）+ 1 个新 memory 记录改名历史

### 验证

| 测试 | 结果 |
|---|---|
| `python3 -m py_compile` 所有模块 | ✓ |
| `bash -n install.sh` | ✓ |
| 用 `type: "pyrealiy"` 的 cfg 跑 `build_outbounds` | ✓ 创建 `MirageOutbound` + INFO 提示 |
| API `/pyrealiy/*` + `/mirage/*` 双注册 | 代码已对，下次启动实测 |

### 迁移指引（用户）

**不需要立刻做**：现有 cfg / 脚本 / API 调用都仍能工作。

**建议在下次方便时**：

```bash
# 配置：outbound type 改新名
sed -i 's/"type": "pyrealiy"/"type": "mirage"/g' /etc/mirage/config_client.json
systemctl reload mirage-client

# 监控脚本里的 API 路径
sed -i 's|/pyrealiy/|/mirage/|g' your-monitoring-scripts.sh

# 环境变量
unset PYREALIY_NO_UVLOOP
export MIRAGE_NO_UVLOOP=1   # 如果之前用过
```

---

## [0.4.34] - 2026-06-12

### 新增 / 规范化（FHS layout + 卸载）

`install.sh` 重构，加 2 件大事：

1. **安装模式 2 选 1**：FHS 系统部署 vs 原地（dev）
2. **卸载**：菜单第 4 项

### FHS layout（system 模式）

| 用途 | 路径 |
|---|---|
| 程序文件 | `/opt/mirage/`（server.py / client.py / core/ / install.sh / example.jsonc） |
| 配置文件 | `/etc/mirage/config_{server,client}.json`（权限 600） |
| 日志文件 | `/var/log/mirage/{server,client}.log` |
| 运行状态 | `/var/lib/mirage/geosite/`（geo 缓存） |
| CLI shim | `/usr/local/bin/mirage-{server,client}` |

shim 脚本：

```bash
#!/bin/bash
# Mirage Client CLI shim
exec /usr/bin/python3 /opt/mirage/client.py "${@:-/etc/mirage/config_client.json}"
```

直接 `mirage-client` 就能用默认 cfg 启动。

### inplace 模式（dev / 测试）

全部文件留在仓库目录，与 0.4.33 之前行为完全一致。

### 模式选择

启动 install.sh → 选服务端/客户端/两端 → 接着问"安装位置"：

```
1) 系统标准位置（推荐）：/opt + /etc + /var/log
2) 原地安装（开发用）
```

已在 `/opt/mirage` 跑 install.sh → 自动 system 模式（开装的就是部署版）。

### service unit 路径自适应

systemd / OpenRC / SysV 三种 unit 模板的 `ExecStart` / `WorkingDirectory` / 日志路径都根据安装模式自动填正确值：

```
# system 模式 systemd 例子
WorkingDirectory=/opt/mirage
ExecStart=/usr/bin/python3 /opt/mirage/server.py /etc/mirage/config_server.json
StandardOutput=append:/var/log/mirage/server.log
```

### 卸载流程

`install.sh` 菜单第 4 项「卸载」：

```
1) 停 + disable + 删 service unit（systemd / OpenRC / SysV 自动识别）
2) 删 /usr/local/bin/mirage-{server,client} shim
3) 询问删 /opt/mirage           默认 Yes
4) 询问删 /etc/mirage           默认 No（含密码！）
5) 询问删 /var/log/mirage       默认 No
6) 询问删 /var/lib/mirage       默认 No（geosite 缓存）
```

pip 依赖（cryptography / uvloop）**不卸载**（可能其他工具在用）。

### 客户端默认 inbound 改为 `mixed`

install.sh 生成的 client cfg 默认用 mixed（SOCKS5 + HTTP 同口），与 0.4.33 引入的能力一致：

```json
"inbounds": [
  {"type": "mixed", "listen": "127.0.0.1:7890"}
]
```

末尾使用提示同步改：「Chrome 等：设置 HTTP 或 SOCKS5 代理 127.0.0.1:7890 都可」。

### 验证

`bash -n install.sh` 通过 + 沙箱测试（path 常量重定向到 /tmp）跑通：

| 检查项 | 结果 |
|---|---|
| 源文件复制到 $INSTALL_PREFIX | ✓ server.py / client.py / core/ |
| shim 脚本写入 $BIN_DIR | ✓ 内容含 `exec python3 /opt/mirage/client.py /etc/mirage/...` |
| server cfg 写到 $ETC_DIR 权限 600 | ✓ 6 个顶层 key（无 schema_version，走 legacy 路径） |
| client cfg 写到 $ETC_DIR 权限 600 | ✓ schema_v1，含 `geosite_dir: /var/lib/mirage/geosite` |
| client inbounds[0] 默认 mixed | ✓ |
| 卸载流程不报错 | ✓ 无 service unit 时静默跳过 |

### 文件大小

`install.sh`: 757 → 1033 行（+276）

---

## [0.4.33] - 2026-06-12

### 新增 / HTTP inbound + Mixed 入口

`inbounds[*].type` 从单一 `socks5` 扩展到 **`socks5` / `http` / `mixed`** 三选一。

| type | 协议 | 典型用法 |
|---|---|---|
| `socks5` | SOCKS5（含 UDP ASSOCIATE） | `curl --socks5 ...` |
| `http` | HTTP/1.1 — CONNECT 隧道 + 绝对 URL forward | `curl -x http://... ...` |
| **`mixed`** | **一口同时支持** SOCKS5 + HTTP | Chrome 系统代理、curl 任意 scheme |

### Mixed 分发逻辑

peek 第一字节决定走 SOCKS5 还是 HTTP/1.1：

```
0x05                                  → SOCKS5
ASCII (C/G/P/H/D/O/T/A)               → HTTP/1.1
0x16 (TLS ClientHello)                → 回 400 + 提示文本（代理不监听 TLS）
其他                                  → 关连接
```

### HTTP/1.1 inbound 实现

- **CONNECT** `host:port` → 回 `HTTP/1.1 200 Connection established\r\n\r\n` + 字节中继（与 SOCKS5 TCP 等价）
- **GET / POST / PUT / DELETE / ...** 必须用**绝对 URL**：
  - 解析 URL → 改 request line 为相对路径
  - 剥 hop-by-hop header（`Proxy-*`）
  - 走 `_dispatch` 转发，body 经 `PrefixedReader` 透明跟随
- IPv6 字面量 `[::1]:443` 支持

### 不实现（设计取舍）

- 代理本身 TLS termination（"HTTPS proxy"）—— 需要 cert，rare 场景
- `Proxy-Authorization` 鉴权 —— 127.0.0.1 用例不需要；LAN 共享走防火墙
- HTTP forward 的 keep-alive 多请求循环 —— 浏览器对不同 host 自然新开 TCP，可接受

### 新模块 / 改动

- **`core/http_inbound.py`**（~280 行）：`handle_mixed_connection` + `handle_http_connection`
- **`core/sniffer.py::PrefixedReader`** 扩展：之前只实现 `read()`；本版本加 `readexactly()` /
  `readuntil()` / `at_eof()`，正确处理 separator 跨"前缀-reader"分界（小心 `\r\n` 拆在
  prefix 和 reader 中间的边界情况）
- **`core/config.py::_validate_inbounds`** 放开 `http` / `mixed` 类型
- **`core/config.py::_project_v1_to_legacy_keys`**：`mixed` 入口的 listen 也投到
  `socks5_host` / `socks5_port`（mixed 包含 SOCKS5 路径，UDP relay 需要）
- **`client.py`**：遍历 `cfg.inbounds`，按 type 起多个 listener；用 `AsyncExitStack`
  统一管理 server lifecycle（含 tproxy）

### 验证（6 个 curl 场景全过）

| 场景 | 命令 | 结果 |
|---|---|---|
| SOCKS5 via mixed | `curl --socks5 127.0.0.1:7890 http://target` | ✓ |
| HTTP forward via mixed | `curl -x http://127.0.0.1:7890 http://target` | ✓ |
| HTTP CONNECT via mixed | `curl --proxytunnel -x http://127.0.0.1:7890 ...` | ✓ |
| HTTP forward via http-only | `curl -x http://127.0.0.1:8080 ...` | ✓ |
| SOCKS5 via socks5-only | `curl --socks5 127.0.0.1:1080 ...` | ✓ |
| TLS bytes via mixed → 400 | `echo TLS 字节 \| nc 127.0.0.1 7890` | ✓ |

### 配置示例

```json
"inbounds": [
  {"type": "mixed", "listen": "127.0.0.1:7890"}
]
```

一个 mixed 入口覆盖所有应用层使用方式。`config_client.example.jsonc` + README "入站类型" section 同步更新。

---

## [0.4.32] - 2026-06-11

### 修改 / 示例文件补全所有功能模块

自查 0.4.31 示例文件发现 11 个功能模块漏列。本版本补齐到 server.py / client.py
实际读的全部 cfg 字段。

### 服务端示例 — 新增 9 项

| 模块 | 字段 |
|---|---|
| 伪装非标端口 | `camouflage_port`（默认 443） |
| **反 DoS 三件套** | `idle_timeout_sec` / `max_conns_per_ip` / `tcp_keepalive` |
| 服务端 access log | `access_log`（与 client 同义） |
| 长肥管道调优 | `drain_threshold`（默认 64 KB，跨境 BDP 高时调大） |
| **时钟同步** | `time_sync` section（`enabled` / `interval` / `startup_timeout` / `max_offset_sec` + udp/tcp 服务器源） |
| **SO_MARK 多出口** | `egresses[]` + `egress_rules[]`（WARP / 多 WireGuard / 多 ISP 场景，示例注释保留） |

服务端示例顶层 key：**10 → 19**

### 客户端示例 — 新增 6 项

| 模块 | 字段 |
|---|---|
| 节点级 tuning | 每个 mirage 节点的 `brutal_rate_bps` / `brutal_pool_size` / `stagger_step_sec` / `stagger_jitter_sec`（注释保留） |
| 长肥管道 | `tuning.drain_threshold` |
| 时钟同步 | `time_sync` section（与服务端同 schema） |
| TProxy | `tproxy_port`（top-level，0 = 禁用） |
| UDP relay 调优 | `udp_relay_host` / `udp_idle_timeout` |
| Rules 类型扩展 | `domain` / `domain_suffix` / `domain_keyword` / `domain_regex` / `invert` 5 种形态在 route 末尾以注释展示 |
| 自定义 geo 源 | `geosite_sources` / `geoip_sources` / `force_geosite_update` |

客户端示例顶层 key：**12 → 18**

### 验证

```
config_server.example.jsonc: VALID  (19 top-level keys)
config_client.example.jsonc: VALID  (18 top-level keys)

行数：157 + 278 = 435 行
```

去除 `//` 和 `/* */` 后 `json.loads` 通过。

### 设计原则

- **常用字段**直接显示默认值（不需要解释也能用）
- **可选字段**注释掉但保留在示例里（让用户知道存在、可发现）
- **类型扩展**（如 5 种 domain rule 形态）以注释段集中展示，不污染主流程
- 每个 section 顶部一行 `═══` 分隔符 + 含义说明

### 与 README 联动

`config_*.example.jsonc` 是**字段层面**的参考；README 是**概念层面**的说明
（schema 合约、Clash API、热加载、DoH/DoT 等）。两者互补。

---

## [0.4.31] - 2026-06-11

### 新增 / 带注释的示例配置文件

为不愿用 `install.sh` 向导而想手编配置的用户，提供两份**完整带注释**的示例文件：

| 文件 | 行数 / 注释 | 内容 |
|---|---|---|
| `config_server.example.jsonc` | 5.4 KB / **57 行注释** | 服务端全字段说明：监听 / 鉴权 / TLS 伪装 / Brutal / Web 管理面板 / 日志。仍是 schema_v0 平铺格式 |
| `config_client.example.jsonc` | 10.6 KB / **90 行注释** | 客户端 **schema_v1** 完整字段：log / inbounds / outbounds（含 urltest + fallback 组）/ route / dns / api / tuning / 老顶层 DNS 字段 |

### 格式选择：JSONC

JSON 不支持注释。**JSONC = JSON with Comments**（`//` 和 `/* */`），VSCode / Sublime / JetBrains 等主流编辑器**原生**识别 `.jsonc` 扩展名并高亮。

### 使用流程

```bash
# 方式 1：用 install.sh 向导生成（推荐，无需手编）
sudo bash install.sh

# 方式 2：从示例复制，去注释后保存为 .json
python3 -c "
import re, json
src = open('config_client.example.jsonc').read()
src = re.sub(r'/\*.*?\*/', '', src, flags=re.S)
src = re.sub(r'//[^\n]*', '', src)
json.dump(json.loads(src), open('config_client.json','w'), indent=4)
"
```

### 验证

- 两个 `.jsonc` 文件去掉 `//` 和 `/* */` 后 `json.loads` 通过
- server 示例顶层 10 key，client 示例顶层 12 key（schema_v1 8 个 + 顶层过渡 DNS 字段）

### 联动改动

- **README "手动配置" 章节**：开头加示例文件说明 + 去注释命令
- **README 项目结构文件树**：加 `.example.jsonc` 两行
- **install.sh 完成提示**：尾部加一行指向示例文件 + README

---

## [0.4.30] - 2026-06-11

### 文档 / README 大同步

把 0.4.16 之后的所有用户可见特性补进 README。新增 5 个顶层 section + 客户端
config 示例改用 schema_v1 形态。

### 新增 section

| Section | 内容 |
|---|---|
| **schema_version=1 合约** | 顶层 8 key 锁定、合约表（什么改动在哪个版本允许）、向后兼容说明 |
| **Clash 兼容 API（client 端只读）** | 11 个端点清单（含 GET/PUT/WS） + Bearer 鉴权 + Yacd 接入信息 + 不实现的控制类列表 |
| **DNS 转发器（含 DoH / DoT + 决策缓存）** | UDP/DoT/DoH 三种 scheme 表格、决策缓存的两层 LRU 描述（实测 ~400ms → ~0.3ms） |
| **配置热加载** | 三种触发方式（systemctl reload / PUT /configs / SIGHUP）、可热加载 vs locked field 表、不打断现有连接的设计 |
| **结构化日志** | `cfg.log.format` 切换、JSON schema 字段表、jq 解析示例 |

### 客户端 config 示例改造

- 加 `schema_version: 1` 顶层字段
- inbounds 数组、route.default、dns.listen、api.listen+secret、tuning section 全部
  按 schema_v1 形态写
- `cn_dns` / `remote_dns` / `geosite_*` 保留在顶层（dns_forwarder 尚未消费
  `dns.resolvers` schema），文中说明这个过渡状态
- urltest / fallback 组示例保留
- direct / block outbound 显式写出（即使自动补全也写）

### 字段表更新

- 顶层字段表覆盖全部 schema_v1 + 仍生效的 legacy 顶层
- `remote_dns` 行注明支持 UDP / DoT / DoH 三种 scheme，指向新 DNS section

### 不动的（仍准确）

- 服务端 Web 管理面板（`admin_host` / `admin_port` / `admin_token`）—— server.py
  里这部分代码仍在，文档准确
- 多节点 / urltest / fallback section
- TProxy section
- TCP Brutal section
- 系统服务 section（OpenRC / SysV 在 0.4.28 已补齐）
- 协议设计 section（历史时不变）

### 修订

- Web 管理面板（server）和 Clash API（client）分开成两个独立 section，避免混淆
- 取消之前 README 里"方式一 / 方式二"双部署路径，改为单一 `install.sh` 入口
  （0.4.27 已做，此版本再清一遍）

### 验证

- 客户端 JSON 示例 `json.loads()` 通过；顶层 keys 包含 `schema_version` 等 12 项
- 100 个 code fence 平衡（even）
- 14 个顶层 `## ` section 顺序合理
- 无残余 `setup.py` 引用
- `admin_*` 字段仍只出现在服务端章节

---

## [0.4.29] - 2026-06-11

### 修复 / 自查清账（0.4.27 + 0.4.28 遗留）

自查发现 6 个问题（2 个真 bug + 4 个旧 docstring + 1 个死代码），全部修复：

- **C1：`pip install --break-system-packages` 在 pip < 23.0.1 系统上报错**
  - 现象：Debian 11 / Ubuntu 22 / 老 CentOS 的 pip 不识别该 flag，`install_pip_deps` 直接退出
  - 修：用 `compgen -G "/usr/lib/python3*/EXTERNALLY-MANAGED"` 检测 PEP 668 marker；只在
    确实是 PEP 668 environment 时才加该 flag
  - 检测到时额外打印 `info "  检测到 PEP 668 environment，加 --break-system-packages"`
- **C2：服务端配置写 `"schema_version": 1` 触发 unknown-top-keys WARN**
  - 现象：服务端用老平铺字段（`listen_host` / `listen_port` 等不在 schema_v1 的 7 个
    顶层 key 里），写 `schema_version: 1` 后 load_config 每次启动 WARN 一条
    `unknown top-level keys ignored: [...]`
  - 修：服务端 config 不写 `schema_version` 字段，让 load_config 走"Legacy schema
    detected" 路径直接返回（服务端的 schema_v1 化是未来工作）
  - 验证：load_config 现在只打一条 INFO `Legacy schema detected`，无 WARN
- **C3 / C4 / C5：3 处 docstring 引用已删除的 `setup.py`**
  - `client.py:17`：删 "由 setup.py 生成"，改为 "参考 README TProxy 防火墙规则一节手配"
  - `core/tproxy.py:6`：同上
  - `core/egress.py:56`：删 "setup.py 的 WARP 一键脚本会一并写好"，改为通用说明
- **C6：`SERVICE_CMD` 全局变量声明 + 赋值但全文未读取**
  - 死代码，移除（4 处赋值 + 1 处声明 + 1 处注释）
- **C7：`core/egress.py:86,89` 残余 `setup-warp.sh` 引用**（第一轮扫漏了）
  - 改为 "典型的 WireGuard policy routing" + "用户自行加 `ip -6 rule`"

### 全项目最终 setup* 引用

```
$ grep -rE 'setup\.py|setup-warp' --include='*.py' --include='*.sh' .
install.sh:15:# 单一安装入口；旧 setup.py 已淘汰。     ← 仅剩这一处说明性注释
```

CHANGELOG 历史条目里的引用保留（记录用）。

### 自查覆盖（这次过的，未发现问题）

- `bash -n install.sh` 语法 ✓
- 服务端 / 客户端 JSON 在各 preset 组合下都通过 `json.load`
- `detect_init_system` 当前环境识别为 `systemd` ✓
- 3 种 init unit 模板（systemd / OpenRC / SysV）heredoc 变量替换正确，
  `\$MAINPID` / `\$network` / `\${RC_SVCNAME}` 保留为 literal
- `${kind^}` 大小写转换是 bash 4+ 内建，shebang `#!/usr/bin/env bash` 保证
- `ask_choice` 1-based 索引 + 输入边界检查
- 服务端尾声打印的客户端 cfg 模板格式合法

---

## [0.4.28] - 2026-06-11

### 新增 / install.sh 支持 OpenRC + SysV init.d

`install.sh` 之前只生成 systemd unit。本版本加入 init 系统自动检测 + 三种 unit 模板：

- **systemd**（多数 Linux 发行版默认）— 已有
- **OpenRC**（Alpine、Gentoo）— 新增
- **SysV init.d**（CentOS 6、老 Debian、部分嵌入式 Linux）— 新增

### 检测顺序

```
[ -d /run/systemd/system ] || systemctl   → systemd
rc-service && rc-update                    → openrc
[ -d /etc/init.d ] && (update-rc.d|chkconfig)  → sysv
else                                       → unknown（手动启动）
```

启动时打印 `[*] 检测到 init 系统：<name>`。

### 三种 unit 都支持的子命令

| 子命令 | systemd | OpenRC | SysV |
|---|---|---|---|
| start / stop / restart | ✓ | ✓ | ✓ |
| status | ✓ | ✓ | ✓ |
| **reload（SIGHUP，触发热加载）** | `ExecReload=/bin/kill -HUP $MAINPID` | `start-stop-daemon --signal HUP --pidfile ...` | `kill -HUP $(cat $PIDFILE)` |

### OpenRC unit 关键字段

```
#!/sbin/openrc-run
command="/usr/bin/python3"
command_args="<work_dir>/<kind>.py <work_dir>/config_<kind>.json"
command_background=true
pidfile="/run/mirage-<kind>.pid"
rc_ulimit="-n 65536"
depend() { need net; after net; }
reload() { ... start-stop-daemon --signal HUP ... }
```

### SysV init.d unit 关键字段

```
### BEGIN INIT INFO
# Provides:          mirage-<kind>
# Required-Start:    $network $remote_fs
# Default-Start:     2 3 4 5
### END INIT INFO

nohup python3 ... >> $LOG 2>&1 &
echo $! > $PIDFILE
```

- enable：`update-rc.d` 优先（Debian 系），`chkconfig` 次之（RedHat 系）
- `ulimit -n 65536` 在脚本顶部

### 用户命令提示自适应

客户端安装末尾的"常用命令"段根据检测到的 init 自动切换：

| init | 状态 | 重载 |
|---|---|---|
| systemd | `systemctl status mirage-client` | `systemctl reload mirage-client` |
| openrc | `rc-service mirage-client status` | `rc-service mirage-client reload` |
| sysv | `service mirage-client status` | `service mirage-client reload` |
| unknown | `kill -HUP $(pgrep -f 'python3.*client.py')` |

### 验证

| init | 模板生成 | enable 命令 |
|---|---|---|
| systemd | `[Unit] Description=... ExecReload=/bin/kill -HUP $MAINPID ...` | `systemctl daemon-reload && systemctl enable` |
| OpenRC | `#!/sbin/openrc-run ... reload() { start-stop-daemon --signal HUP ... }` | `rc-update add mirage-* default` |
| SysV | `### BEGIN INIT INFO ... case "$1" in start|stop|reload|status)` | `update-rc.d mirage-* defaults` or `chkconfig --add` |

三种模板在当前环境（systemd）+ 手工 stub 后各自生成的 unit 文件**变量替换正确**：
- `${WORK_DIR}` 展开为绝对路径
- `\$MAINPID` / `\$network` / `\${RC_SVCNAME}` 保留为 literal（运行时由 init 解释）

### 修订

- 0.4.27 CHANGELOG / README 之前写"仅支持 systemd"是错的，本版本订正

---

## [0.4.27] - 2026-06-11

### 修改 / 工程结构整理

- **删除 `setup.py`**（1536 行）。它的全部功能并入 `install.sh`
- **新增 `tests/` 目录**，归集所有测试脚本（保持 `bench.py` 在根，作为主要性能基准）
  ```
  tests/
  ├── throughput_test.py
  ├── gfw_probe_test.py
  ├── test_admin.py
  ├── traffic_analyzer.py
  ├── run_test.sh        ← 自动 cd 到项目根
  └── run_test_v2.sh
  ```
- **`install.sh` 重写**（370 → 540 行 bash）成交互式向导

### `install.sh` 新流程

```
sudo bash install.sh
↓
[1] 服务端 / [2] 客户端 / [3] 两端
```

**服务端**：

| 步骤 | 行为 |
|---|---|
| 监听地址 + 端口 | 默认 `0.0.0.0:443` |
| 密码 | `openssl rand -base64 24` 自动生成 或手输 |
| 伪装 SNI | **`openssl s_client -tls1_3` 实测目标 TLS 1.3**，失败询问是否继续 |
| TCP Brutal | 检测内核模块（可选） |
| 写 `config_server.json` | schema_version=1 |
| systemd unit | `/etc/systemd/system/mirage-server.service`，含 `ExecReload=/bin/kill -HUP $MAINPID` |
| **末尾自动打印对应客户端 cfg** | 含 `curl ipify` 探出的公网 IP + 密码 + SNI + 完整 JSON 模板 + scp 提示 |

**客户端**：

| 步骤 | 行为 |
|---|---|
| 服务端地址 / 端口 / 密码 / SNI | 与服务端匹配 |
| SOCKS5 监听口 | 默认 1080 |
| **路由模板**（三选一） | `china_split`（默认）/ `transparent`（全代理）/ `custom`（用户编辑） |
| **DNS 方案**（三选一） | `china_split`（默认）/ `full_proxy` / `off` |
| Clash API | 可选，默认开；自动 `openssl rand` 生成 secret |
| systemd unit | `mirage-client.service` |

### DNS 默认方案（用户指定）

- **国内**：`cn_dns: "119.29.29.29"` （DNSPod 公共 DNS，国内最快）
- **国外**：`remote_dns: "1.1.1.1:53"` （Cloudflare via VPS 转发；DNS 查询从 VPS 出，避开本地 GFW DNS 污染）
- 配合 `china_split` 路由：geosite:cn / 内网 → 直连用 cn_dns；其余 → 经隧道用 remote_dns

### 路由 `china_split` 模板

```json
{
  "default": "proxy",
  "rules": [
    {"ip_cidr": ["127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12",
                 "192.168.0.0/16", "169.254.0.0/16"], "outbound": "direct"},
    {"geosite": ["loyalsoldier:cn"], "outbound": "direct"},
    {"geoip":   ["loyalsoldier:cn"], "outbound": "direct"}
  ]
}
```

geosite/geoip 数据由客户端启动时自动下载（geosite_cache）。

### systemd unit 模板

```
[Service]
Type=simple
WorkingDirectory=<project_dir>
ExecStart=/usr/bin/python3 <project_dir>/{server,client}.py <project_dir>/config_{server,client}.json
ExecReload=/bin/kill -HUP $MAINPID         ← 配合 0.4.25 的热加载
Restart=on-failure
LimitNOFILE=65536
```

### 文档同步

- README "快速部署" 章节重写：单一入口 `bash install.sh`，删 setup.py 方式二
- 文件树更新：去 setup.py，加 install.sh / bench.py / tests/
- TProxy 防火墙规则：从"setup.py 自动生成"改为"按下面 iptables 模板手配"
- 系统服务章节：从"setup.py 多 init 支持"改为"install.sh 自动检测 init（**0.4.28 起支持 systemd / OpenRC / SysV**）"

### 迁移

| 旧用法 | 新用法 |
|---|---|
| `python setup.py` | `sudo bash install.sh` |
| `python throughput_test.py` | `python tests/throughput_test.py` |
| `bash run_test.sh` | `bash tests/run_test.sh`（脚本自动 cd 到项目根） |

向后兼容：现有 `config_server.json` / `config_client.json` 不需要改动。

---

## [0.4.26] - 2026-06-11

### 修复 / 热加载状态一致性

自查 0.4.25 热加载找到两个状态一致性问题：

- **B1：`GET /configs` 在 reload 后返回旧 cfg 快照**
  - 根因：`APIContext.cfg` 在 API 启动时绑定到当时的 cfg dict；Reloader 只更新自己
    的 `self.cfg`，没动 api_ctx，所以 `/configs` 端点永远返回老值
  - 修：Reloader 持有 `api_ctx` 引用，reload 成功后同步 `api_ctx.cfg = new_cfg`
  - 实测：`remote_dns: https://1.1.1.1/dns-query → 1.1.1.1:53` reload 后 GET /configs
    立即反映新值
- **B2：reload 路由规则后，DNS 缓存里来自旧路由的 IP 仍能命中**
  - 根因：reload 时 invalidate 了 routing_cache，但 dns_cache 没动
  - 场景：原 `cn.bing.com → direct` 缓存到本地 DNS 返回的 IP；改成
    `cn.bing.com → proxy` 后，下一次 DNS 查询命中老 IP（来自老 outbound），用户
    感受"改了规则没生效"
  - 修：`DnsCache.invalidate()` 新增公共方法；Reloader rebuild router 时也清 dns_cache
  - 实测：reload 报告 `router (routing_cache cleared 1, dns_cache cleared 1)`

### 已知限制（已审计，本版本不修）

| ID | 项 | 现状 |
|---|---|---|
| E1 | DoH `Connection: close` 不响应 | 下次查询命中已关连接，自动重连，无功能影响 |
| E2 | DoH chunked 响应不支持 | 所有主流 DoH 服务返回固定长度，未观测到 |
| E3 | DoT/DoH SNI 无法独立配置 | 主流公共 DoT/DoH 证书 SAN 含 IP，可用；自建 DoT 服务可能撞 |
| E4 | `print("[*] uvloop ...")` 不走 logging | JSON 模式下 stdout 混入两行非 JSON，0.4.22 CHANGELOG 已注 |

### 自查矩阵（这次过的项）

- RouterRef 替换的原子性（Python 属性赋值原子 + asyncio 单线程）
- Reloader 并发 lock（`asyncio.Lock`）
- reload 失败回滚（`load_config` 抛错时不动任何状态）
- `_extract_min_ttl` 的 DNS 解析（question 段指针压缩、answer NAME pointer、RDLENGTH 跳过都正确）
- LogBroadcaster 多订阅（`list(self._queues)` 拷贝防 race；满队列 drop 老消息）
- WSConnection 双向 close ack 符合 RFC 6455
- DoH `Content-Length` 大写敏感（`re.IGNORECASE`）
- 上游 timeout 处理（`asyncio.TimeoutError` 不 drop tunnel）

---

## [0.4.25] - 2026-06-11

### 新增 / 配置热加载

- **`core/reload.py`**（~170 行）：`RouterRef` + `Reloader` 协调子系统更新
  - `RouterRef`：1-级间接，让 `_dispatch` / DNSForwarder 通过 ref 访问，热加载时
    `replace(new_inner)` 原子替换
  - `Reloader`：持有各子系统引用，`reload()` 按顺序更新；防并发 lock
- **两种触发方式**

| 触发 | 用法 |
|---|---|
| `SIGHUP` 信号 | `kill -HUP <pid>`（systemd `Reload=` 友好），客户端启动时日志会打出 PID |
| `PUT /configs` | Clash 兼容，需 Bearer 鉴权；响应含 `{ok, changed, warnings}` |

### 可热加载范围

| 字段 | 行为 |
|---|---|
| `route.rules` / `route.default` / CSV `rules` / `final` | rebuild Router + routing_cache invalidate |
| `cn_dns` / `remote_dns`（含 DoH/DoT scheme 切换） | DNSForwarder.reload()：drop _tunnels，下一次查询时按新地址重建 |
| `log.format` / `log_levels` | re-apply（之后日志立即按新格式输出） |
| `tuning.access_log` 等运行时可调项 | 注册的 `tuning_handlers` 被回调 |

### 不动（locked field，新值仅警告不生效）

`schema_version` / `inbounds` / `outbounds` / `api.listen` / `api.secret` /
legacy 顶层 `socks5_host` / `server_host` / `password` 等。检测到改动 →
`warnings: ["locked: <field>"]`。

### 实测 4 场景

| 场景 | 结果 |
|---|---|
| `PUT /configs` 无改动 | `{ok:true, changed:["log","tuning","router (cache cleared 0)","dns_forwarder"]}` ✓ |
| 改 `remote_dns: 1.1.1.1:53 → tls://1.1.1.1:853`，发 SIGHUP | log: `dns reload: ... remote https://1.1.1.1/dns-query -> tls://1.1.1.1:853` ✓ |
| reload 后查 example.org（新域名） | DoT 路径 61B 响应 ✓ |
| `PUT /configs` 改回 DoH | 缓存清 1 条，立即生效 ✓ |

### 设计要点

- **不打断现有连接**：outbounds + inbounds 不动；正在跑的 TCP 隧道继续用老 router
  的决策结果（cache 命中）或下一次 dispatch 时切到新 router
- **DNS upstream 跌掉时优雅**：旧 upstream `close()` 让 in-flight queries 抛
  OSError 回退到 NXDOMAIN；下一次查询自动按新 cfg 重建 upstream
- **geo 数据不重下**：`available_site` / `available_ip` 在启动期下载一次保留下来，
  reload 沿用，避免每次 SIGHUP 都打几 MB 流量
- **CHANGELOG 合约保持**：`schema_version=1` 期间顶层 8 个 key 不动，本次没新加

### 不做的（按之前对齐 + 性价比考量）

- `inbounds` 重 bind（socket 已绑；用户改了端口请 restart）
- `outbounds` 重建（会断现有所有连接）
- 自动监听 file watch（用户敲 SIGHUP 显式控制更可预测；inotify 会因 editor 临时
  文件触发误 reload）

---

## [0.4.24] - 2026-06-11

### 新增 / 路由决策缓存 + DNS 响应缓存

- **`core/decision_cache.py`**（~190 行）：两个 LRU+TTL 缓存
  - `RoutingCache`：domain → outbound_tag，TTL 默认 1h（路由规则静态）
  - `DnsCache`：(domain, qtype) → 原始 DNS 响应字节，**TTL 从响应 answer 段直接抽
    `min(TTL)`**（截断到 30s–3600s）
  - `OrderedDict` 实现 LRU；过期 entry 在 get 时延迟回收
  - asyncio 单线程，无锁
- **集成点**
  - `client.py::_dispatch`：命中 RoutingCache 即跳过 `router.match`
  - `core/dns_forwarder.py::_handle`：双层缓存
    - DnsCache 命中 → 直接回包（替换 tx_id），跳过路由 + 上游
    - DnsCache 未命中 → 查 RoutingCache → 上游查询 → 写两个 cache
- **新端点 `GET /mirage/cache`**：
  ```json
  {"routing":{"entries":2,"max":10000,"hits":0,"misses":2,"ttl_sec":3600,"hit_rate":0},
   "dns":    {"entries":2,"max":10000,"hits":3,"misses":2,"hit_rate":0.6}}
  ```

### 配置

```json
"tuning": {
  "routing_cache": {"enabled": true, "max_entries": 10000, "ttl_sec": 3600},
  "dns_cache":     {"enabled": true, "max_entries": 10000}
}
```

- 默认开启（max 10k entries 各 50-100 字节，约 1MB 内存）
- 可显式 `"enabled": false` 退化到无缓存（debug 用）

### 实测性能（DoH 路径，最坏情况）

| 查询 | 路径 | 耗时 |
|---|---|---|
| q1 cold: example.com | DoH 全程：TLS + HTTP/1.1 + 上游 | **417 ms** |
| q2 hot: example.com | 缓存命中（dict get）→ tx_id 重写 | **0.3 ms**（~1300×） |
| q3 cold: google.com | DoH 全程（TLS 已建可复用） | **142 ms** |
| q4 hot: google.com | 缓存命中 | **0.4 ms** |
| q5 hot: example.com（TTL 内） | 缓存命中 | **0.4 ms** |

5 次查询 DNS cache `hit_rate = 0.6`。

### 设计要点

- **tx_id 重写**：缓存里存的是 first response 的字节，要把响应里的 tx_id 替换成
  本次查询的（DNS 协议规定回包 ID 必须与请求一致）
- **TTL 跟 DNS answer 的实际 TTL**：不固定写死，给 ttl=86400 的 CDN 节省大量
  上游查询，给 ttl=60 的不滥缓存
- **negative cache**：NXDOMAIN / 空 answer 也缓存（默认 60s），防止反复打不存在的
  域名拖垮上游
- **路由 + DNS 各自独立 LRU**：DNS 命中后不再问路由（响应即答案），所以路由 cache
  hits 通常只在 TCP dispatch 路径上累加；两层独立是 by design

### 限制 / 后续

- 当前不持久化（重启丢）。落盘 cache 是 Option B（不做，按之前对齐）
- 不与 DNS 客户端的本地缓存协作（客户端按返回 TTL 自己缓存；我们缓存的 TTL 是
  原值，不动态衰减）

---

## [0.4.23] - 2026-06-11

### 新增 / DoH + DoT 上游 resolver

`cfg.remote_dns` 现在接受 3 种 scheme，统一由 `core/dns/upstream.py::make_upstream`
工厂派发：

| address 形式 | upstream | 说明 |
|---|---|---|
| `"1.1.1.1:53"` / `"dns://..."` / `"1.1.1.1"` | UdpUpstream | 旧行为：mirage 隧道 → server plain TCP → UDP 53 |
| `"tls://1.1.1.1:853"` | DotUpstream | 端到端 TLS 1.2+ + 长度前缀 DNS pipeline |
| `"https://1.1.1.1/dns-query"` | DohUpstream | 端到端 TLS + HTTP/1.1 POST `application/dns-message`，单连接 keep-alive 串行 |

### 新模块

- **`core/dns/tls_over_tunnel.py`**（~110 行）：`TlsOverTunnel` 用 `ssl.MemoryBIO`
  在 EncryptedTunnel 上跑标准 TLS。mirage 隧道是 message 模式（不是 raw socket），
  无法直接 `ssl.wrap_socket`；MemoryBIO 允许手工拉送 SSL 状态机的入/出字节
- **`core/dns/upstream.py`**（~370 行）：
  - `UdpUpstream`：迁移自原 `_DnsTunnel`，行为不变（向后兼容）
  - `DotUpstream`：TLS 握手 + 长度前缀 DNS 帧 pipeline + tx_id 重写
  - `DohUpstream`：TLS + HTTP/1.1 POST + `Content-Length` 响应解析
  - `make_upstream(outbound, address)` 工厂
- **`core/dns/__init__.py`**：导出 `make_upstream`

### 内部

- `core/dns_forwarder.py`：删除原 `_DnsTunnel` 类（移到 dns/upstream.py），
  `_tunnel_for` 改用 `make_upstream` 工厂；删除 `struct` / `pack_address` /
  `_TUNNEL_TIMEOUT` 等 orphan 导入
- DoH 的 `Host` header 与 SNI 用 URL 的 hostname；上游 server 用 IP literal 是
  **硬性要求**（域名 host 会导致 bootstrap 死循环 —— DNS 服务要先解析 DNS 服务器的
  域名），工厂在 parse 时即拒绝

### TLS 端到端的拓扑

```
[client app] → UDP DNS 5454 → [DNSForwarder]
                                    ↓
                          mirage 加密隧道（AEAD）
                                    ↓
                          [server.py 透明 TCP 转发到 host:443]
                                    ↓
                         [客户端进程内 TLS 握手到 host]  ← TLS 端到端，server 看不到明文
                                    ↓
                         HTTP/1.1 POST application/dns-message
                                    ↓
                          Cloudflare 1.1.1.1 DoH 回包
```

### 验证

| 场景 | 结果 |
|---|---|
| scheme 解析（8 种组合） | UDP/DoT/DoH 分发正确 ✓；DoH domain host 被拒（ValueError） |
| `1.1.1.1:53`（UDP） | 105 B 响应，qid 一致，2 条 A 记录 ✓ |
| `tls://1.1.1.1:853`（DoT） | 61 B 响应，qid 一致，2 条 A ✓ |
| `https://1.1.1.1/dns-query`（DoH） | 61 B 响应，qid 一致，2 条 A ✓ |
| DoH keep-alive 第二次查询 | 44 B 响应（google.com），同一 TLS 连接复用 ✓ |

### 限制 / 后续

- DoH URL 的 host 必须是 IP literal（如 `https://1.1.1.1/dns-query`）。要支持
  `https://cloudflare-dns.com/dns-query` 需要 bootstrap resolver（先用 IP DoH 解析
  域名 DoH 服务器的 IP），留待后续
- 当前所有 DNS 查询走同一 upstream（`cfg.remote_dns`）。按 `cfg.dns.resolvers` +
  `dns.rules` 做 per-domain 上游分流，留待后续（schema 已就位）
- DoT/DoH 都是端到端 TLS；服务器侧不能 MITM（这是设计目标）

---

## [0.4.22] - 2026-06-11

### 新增 / 结构化日志

- **`cfg.log.format: "text" | "json"`**：日志格式开关，默认 `text`（向后兼容）
- **`core/utils.py::JsonFormatter`**：每行一个 JSON
  ```json
  {"ts":"2026-06-11T06:29:48.416Z","level":"info","logger":"time_sync",
   "msg":"time_sync: offset 0.00s → -3.10s","extra":{...}}
  ```
- **`apply_log_format(cfg)`**：替换 root handler 的 formatter；在 `load_config`
  开头先 peek 一次，确保配置校验自身的 INFO/WARN 也走 JSON
- **`logger.info("...", extra={"k":"v"})`** 的 extras 自动收进 `extra` 字段
- **异常**：traceback 进 `exc` 字段

### Schema 定义（写进 README）

| 字段 | 类型 | 含义 |
|---|---|---|
| `ts` | string | ISO-8601 UTC，毫秒精度 `2026-06-11T06:29:48.416Z` |
| `level` | string | `debug` / `info` / `warning` / `error` / `critical` |
| `logger` | string | logger 名（`client` / `conn_pool` / `time_sync` 等） |
| `msg` | string | 格式化后的消息 |
| `extra` | object | 可选；调用方传 `extra=` 的字段集合 |
| `exc` | string | 可选；异常时的 traceback 文本 |

### 解析示例（Loki / ELK / jq）

```bash
# 看 conn_pool 的所有 WARNING+
cat client.log | jq 'select(.level=="warning" or .level=="error") | select(.logger=="conn_pool")'

# 流量异常时筛连接级日志
cat client.log | jq 'select(.extra.outbound=="proxy")'
```

### 验证

| 场景 | 结果 |
|---|---|
| `cfg.log.format` 缺省 | 仍是 `2026-06-11 14:24:07,896 [INFO] config: ...` 文本格式 ✓ |
| `cfg.log.format: "json"` 单次启动 | 15/15 日志行都是 JSON，含校验阶段的 2 条 INFO ✓ |
| 同一行带 extras | `extra` 字段出现并含 `{outbound, dst}` 等 ✓ |
| WS `/logs` 端点（P4） | 仍按 `[%(name)s] %(message)s` 推送（API 表层 schema 不变） |

### 已知边界

- WS `/logs` 的 `payload` 字段始终是 Clash UI 期望的紧凑文本格式，**不**随
  `cfg.log.format` 改变（Clash 协议层）。stderr 日志输出受控；WS 日志输出独立
- 现有代码里的 `logger.info("...")` 调用**完全不动**；想结构化某条只需要传
  `extra=` 字典，不传也兼容

---

## [0.4.21] - 2026-06-11

### 新增 / Clash API P5：mirage 私有诊断端点

- **`core/api/mirage_endpoints.py`**（~140 行）：3 个非 Clash 端点，
  mirage 私有命名空间（Yacd 不读，curl 排查用）。

| 端点 | 返回 |
|---|---|
| `GET /mirage/pool` | 每 mirage outbound 的 `BrutalPool` 实时快照：`ready` / `building` / `target` / `next_build_in_sec`（staircase 游标距离）/ `stagger_step_sec` / `latency_ms` / `latency_age_sec` / `healthy` / `consecutive_failures` |
| `GET /mirage/timesync` | `offset_sec`（当前时钟修正）/ `last_source`（"ntp" / "https"）/ `last_sync_epoch` / `last_sample_count` / `max_offset_sec` / `since_sync_sec` |
| `GET /mirage/geo` | `cache_dir`（绝对路径）/ `update_days` / `sources[*]`：`key` / `file_path` / `exists` / `file_size` / `downloaded_epoch` / `age_days` / `url` |

### 内部

- `core/time_sync.py`：类级新增 `_last_source` / `_last_sync_at_epoch` /
  `_last_sample_count`，`_sync_once` 成功路径写入。无业务行为变化
- `core/api/server.py::_register_endpoints`：注册 mirage_endpoints

### 实测

| 端点 | 数据 |
|---|---|
| `/mirage/pool` | `{"proxy": {"ready": 20, "building": 0, "target": 20, "latency_ms": 1506.8, "healthy": true}}` |
| `/mirage/timesync` | `{"offset_sec": -3.097, "last_source": "ntp", "last_sample_count": 2, "since_sync_sec": 14.4}` |
| `/mirage/geo` | `{"cache_dir": "/opt/.../.geosite", "update_days": 7.0, "sources": []}` |

### Clash API 完整能力（P0-P5 全部完成）

```
HTTP /version            ✓ P1
HTTP /configs            ✓ P1
HTTP /connections        ✓ P2
HTTP /proxies            ✓ P3
HTTP /proxies/{name}     ✓ P3
HTTP /rules              ✓ P3
WS   /traffic            ✓ P4
WS   /logs?level=...     ✓ P4
HTTP /mirage/pool      ✓ P5
HTTP /mirage/timesync  ✓ P5
HTTP /mirage/geo       ✓ P5
```

**总代码量**：`core/api/` 9 个文件 ~1200 行，**零外部依赖**（仅 stdlib + asyncio）。

### 不实现的（控制类，留待 P6 或不做）

- `POST /proxies/{group}` 切换 urltest 选择
- `GET /proxies/{name}/delay` 触发主动 probe
- `DELETE /connections/{id}` 杀连接
- `PUT /configs` 热更配置

按之前对齐：Clash API v1 范围是**纯查询**。控制类等真有需求再开 P6。

---

## [0.4.20] - 2026-06-11

### 新增 / Clash API P4：WebSocket 推流

- **`core/api/ws_proto.py`**（~170 行）：RFC 6455 服务端最小实现
  - 握手：GET + Upgrade + Sec-WebSocket-Key/Version 13 → 101 + base64(SHA1(key+GUID))
  - 帧编解码：服务端发不 mask；客户端发自动解 mask；单帧 ≤ 1MB
  - 自动响应 client → server 的 ping（回 pong）和 close（回 close ack）
  - `WSConnection` 类：构造时启 `_read_loop` 后台协程；`send_text(s)` 加发送锁防交错
  - 故意不支持：分片、扩展（permessage-deflate）、WSS（本身 127.0.0.1）
- **`core/api/ws_endpoints.py`**（~140 行）：两个端点 + LogBroadcaster
  - `WS /traffic`：1Hz 推 `{"up": bytes_per_sec, "down": bytes_per_sec}`（速率 =
    delta(meter.totals) / dt，每订阅独立采样）
  - `WS /logs?level=info|warning|error|debug`：实时日志流，每条 `{"type": level,
    "payload": "[logger] msg"}`
  - `LogBroadcaster(logging.Handler)`：挂到 root logger，emit 写每个订阅者的队列；
    队列满 → drop 老消息保留新的（订阅者消费慢不影响其他订阅者）

### 路由层

- `core/api/router.py::Router.add_ws(pattern, handler)`：注册 WS 路由
- `core/api/router.py::Router.match_ws(path) → (handler, params)`：单独匹配
- `core/api/server.py::_handle_conn`：检测 `Upgrade: websocket` 头优先走 WS 分支
- `core/api/server.py::_maybe_dispatch_ws`：鉴权 → handshake → 把 reader/writer
  交给 handler；handler 退出后协程结束

### client.py 集成

- 启动期：`LogBroadcaster()` + `attach()` 到 root logger，挂进 APIContext
- shutdown：`log_bc.detach()` 解除 logging handler
- `tuning.access_log: true` 也支持（之前仅顶层 cfg.access_log）

### 验证（端到端 `/tmp/ws_test.py`）

| 测试 | 结果 |
|---|---|
| 无 Authorization 头 → 101 应失败 | ✓ 返回 `HTTP/1.1 401 Unauthorized` |
| Sec-WebSocket-Accept 校验 | ✓ 客户端按 RFC 算的 expected 出现在响应里 |
| `/traffic` 连续 5 帧 | ✓ 17 → 61 → 61 → 61 → 4.9 MB/s（与 bench 时序一致） |
| `/logs?level=info` 触发 bench | ✓ 3 条 `[info] [client] proxy 127.0.0.1:19001 [FINAL]` |
| 客户端发 close → 服务端回 close ack | ✓ 干净退出 |

### Yacd 此时

| 功能 | 状态 |
|---|---|
| 节点列表 + 延迟 + alive（P3） | ✓ |
| 规则面板（P3） | ✓ |
| 连接列表（P2） | ✓ |
| **流量曲线（实时 up/down 速率）** | ✓ |
| **实时日志流** | ✓ |
| mirage 私有指标（handshake/pool/timesync/geo） | ✗ P5 |

P4 之后 Yacd / metacubexd 的核心功能（除控制类操作）全部可用。

---

## [0.4.19] - 2026-06-11

### 新增 / Clash API P3：proxies + rules 端点

- **`GET /proxies`**：返回所有 outbounds 的 Clash 格式 dict
  ```json
  {"proxies": {
    "proxy":  {"type":"Trojan", "name":"proxy", "server":"...", "port":443,
               "alive":true, "udp":true, "history":[{"time":"...","delay":150}]},
    "direct": {"type":"Direct", "name":"direct", "alive":true, "udp":true, "history":[]},
    "block":  {"type":"Reject", "name":"block",  "alive":true, "udp":true, "history":[]},
    "auto":   {"type":"URLTest", "name":"auto", "all":["a","b"], "now":"a", ...}
  }}
  ```
- **`GET /proxies/{name}`**：单个 outbound（404 if not found）
- **`GET /rules`**：路由规则列表
  ```json
  {"rules":[
    {"type":"DomainSuffix","payload":"google.com","proxy":"proxy","invert":false},
    {"type":"IPCIDR","payload":"192.168.0.0/16","proxy":"direct","invert":false},
    {"type":"Match","payload":"","proxy":"proxy","invert":false}
  ]}
  ```

### 类型映射（mirage → Clash）

| mirage 类型 | Clash 类型 | 备注 |
|---|---|---|
| `mirage` | `Trojan` | 行为最接近，UI 图标对应；带 `server` + `port` |
| `direct` | `Direct` | |
| `block` | `Reject` | |
| `urltest` | `URLTest` | 带 `all` + `now` |
| `fallback` | `Fallback` | 带 `all` + `now`（取 resolve_leaf 结果） |

### Rule 类型映射

router 内部 `DOMAIN-SUFFIX` / `IPCIDR` / `GEOSITE` 等映射到 Clash CamelCase：
`DomainSuffix` / `IPCIDR` / `GeoSite` / `Match` / 等。Yacd UI 直接读这些字符串
决定显示文案。

### 内部

- `core/router.py::Router.rules`：新增只读 property，从 `_rules` 提取
  `(type, payload, proxy, invert)`
- `core/router.py::_clash_rule_type()`：UPPER-HYPHEN → CamelCase 映射表
- `core/api/clash_endpoints.py::_outbound_to_clash()`：单 outbound → Clash dict；
  叶子带 server/port，组节点带 all/now
- `core/api/clash_endpoints.py::_history_for()`：把 `latency_ms` + `latency_age_sec`
  合成单条 Clash history 条目（Yacd 只读 `history[-1].delay`）
- `FINAL` 规则末尾追加为 `Match` 让 UI 看到默认动作

### 验证

| 场景 | 结果 |
|---|---|
| legacy config `/proxies` | 显示 proxy/direct/block 3 节点 ✓ |
| legacy config `/proxies/proxy` | 单 dict，含 server/port/history ✓ |
| `/proxies/nope` | 404 `{"code":404,"message":"proxy not found"}` ✓ |
| 4 条规则配置 `/rules` | DomainSuffix×2 / Domain / DomainKeyword / IPCIDR + FINAL Match 6 条 ✓ |
| Trojan history.delay | 1507ms（来自首次 handshake 实测，后续 urltest probe 会更新） |

### Yacd 此时能看到

✓ 节点列表 + 当前延迟 + alive 状态  
✓ 规则面板（按顺序显示，UI 可按 host 试匹配）  
✓ 实时连接列表（P2）  
✗ 流量曲线（P4 实现 `WS /traffic`）  
✗ 实时日志流（P4 实现 `WS /logs`）

### 不做的

P3 范围**纯查询**，不实现：
- `POST /proxies/{group}` 切换 urltest/fallback 选择（控制类）
- `GET /proxies/{name}/delay` 触发主动 probe（控制类）
- `DELETE /connections/{id}` 杀连接（控制类）

这些都是写动作，按之前对齐留待下一个版本（如有需要再开 P6）。

---

## [0.4.18] - 2026-06-11

### 新增 / Clash API P2：连接表 + 流量计

- **`core/api/stats.py`**：ConnectionRegistry + TrafficMeter
  - `ConnInfo` 字段贴 Clash 格式（`metadata.{network,type,sourceIP,sourcePort,
    destinationIP,destinationPort,host}` + `upload` / `download` / `start` /
    `chains` / `rule` / `rulePayload`）
  - asyncio 单线程模型下注册 / 注销 / 遍历都在 loop 内，无 Lock
  - 关闭连接保留 5 秒（linger）让 UI 看到"刚关闭"，避免快闪流量丢失
  - 全局 `TrafficMeter` 累计 `up_total` / `down_total`（monotonic，不重置）

- **`GET /connections`**：Clash 标准格式
  ```json
  {
    "downloadTotal": 12345678,
    "uploadTotal":   234567,
    "memory":        0,
    "connections": [
      { "id": "abc...", "metadata": {...}, "upload": 1234,
        "download": 5678, "start": "2026-06-11T12:00:00.000Z",
        "chains": ["proxy"], "rule": "DomainSuffix", "rulePayload": "google.com" }
    ]
  }
  ```

- **byte 计数器：在 relay 路径**注入 on_up / on_down 回调（最小侵入）
  - `core/outbound.py::_bidi_tunnel_relay` 加 `on_up` / `on_down` 形参
  - `core/utils.py::relay`（DirectOutbound 用）同上
  - `core/udp_relay.py::UDPRelay` 构造增加 `on_up` / `on_down` 形参，绑到
    `_handle_uplink` / `_send_to_socks_client`
  - `Outbound.handle` 基类签名扩展（向后兼容默认 None）

- **client.py 集成**
  - 启动期检测 `cfg.api.listen` → 创建 `ConnectionRegistry`
  - `_dispatch` / `_dispatch_udp` 在调 `outbound.handle` 前注册 ConnInfo、生成
    byte 回调，finally 注销
  - APIContext 增加 `registry` 字段

### 设计要点

- **关闭后仍可见 5 秒**：避免 Yacd 轮询 1Hz 时"刚关的连接没看见"
- **chains 顺序"叶在前"**：Clash UI 数组首项是实际跑流量的节点
- **rule split**：router 的 source 字符串拆成 `(rule_type, rule_payload)`，支持
  `DomainSuffix:google.com` / `GeoIP:cn` / `default` 等形式
- **零性能回归**：byte 回调是无锁原子加，bench 同条件 c=4 上行 483 Mbps，与
  0.4.17 同区间

### 验证

| 场景 | 实测 |
|---|---|
| 启动期空 registry | `{"uploadTotal":0,"downloadTotal":0,"connections":[]}` |
| 4 条 SOCKS5 上行 bench 期间 | 5 conn（含 sanity ping），每条 upload ~30MB |
| metadata 全字段 | `tcp/Socks5 127.0.0.1:48200 -> 127.0.0.1:19001` 正确 |
| chains | `['proxy']` |
| bench 结束 + 5s 内 | 5 conn 仍可见（linger） |
| bench 结束 + 5s 后 | `connections = 0`，但 `uploadTotal` monotonic 不重置 |
| bench 吞吐 | 483 Mbps（与无 API 时 ~520 Mbps 持平，无回归） |

### Yacd 此时能看到

- ✓ 实时活跃连接列表（source / destination / host / chains）
- ✓ 每条连接的 upload/download 字节数
- ✓ 全局上下行累计
- ✗ 节点列表（P3 实现 `/proxies`）
- ✗ 规则面板（P3 实现 `/rules`）
- ✗ 流量曲线（P4 实现 `WS /traffic`）

---

## [0.4.17] - 2026-06-11

### 新增 / Clash 兼容 API（P1）

- **`core/api/`：从零原生实现的 HTTP/1.1 + 路由 + 鉴权 + CORS**
  - 零外部依赖（不引 aiohttp / starlette），只用 stdlib + asyncio
  - `http_proto.py`（~160 行）：Request / Response / read_request / write_response，
    支持 keep-alive、CORS 头注入、4xx/5xx 错误路径
  - `router.py`（~30 行）：method × path 模式匹配，`{name}` 捕获路径参数
  - `server.py`（~170 行）：APIServer + Bearer Token 鉴权（常量时间比较）+
    `?token=` query 备用 + CORS 中间件 + keep-alive 串行处理
  - `clash_endpoints.py`（~80 行）：`/version` + `/configs`
- **支持的端点**（P1 范围）

  | 路径 | 行为 |
  |---|---|
  | `GET /version` | `{"version":"0.4.17","meta":true,"premium":false}` |
  | `GET /configs` | Clash 标准字段（`socks-port` / `mode` / `log-level` / ...）+ mirage cfg 摘要（脱敏） |
  | `OPTIONS *` | CORS preflight，返回 `Allow-Origin/Methods/Headers/Max-Age` |
  | 其他 | 404 / 405 / 401 / 400 / 500，统一 `{"code":N,"message":"..."}` |

- **脱敏**：`/configs` 响应里 `password` / `credential` / `api.secret` 都替换为 `"***"`
- **鉴权**：所有路径（含 `/version`）必须带 secret。不带 → 401。Yacd 历史用法
  `?token=<secret>` 也支持
- **keep-alive**：默认 HTTP/1.1 keep-alive，闲置 75s 关连接；同一 socket 上可
  跑无限多请求（实测 3 个串行 RTT < 5ms）

### 配置

```json
"api": {
  "listen": "127.0.0.1:9090",
  "secret": "your-strong-token",
  "cors": ["*"]
}
```

- `listen` 缺失 → 不启 API（向后兼容默认）
- `secret` 缺失 → schema 校验阶段就 ConfigError 拒绝启动（0.4.16 已合约）

### 验证

9 个 curl / raw socket 测试矩阵：
- 无鉴权 / 错 token → 401 ✓
- Bearer + 401 query token → 200 ✓
- `/configs` 含 Clash 字段 + mirage 摘要 + password 脱敏 ✓
- 未知路径 → 404，错方法 → 405 ✓
- CORS preflight → 204 + 完整头 ✓
- 同一 socket 3 个串行请求（keep-alive） → 全 200 ✓

E2E：API 启动后跑 bench c=4 上行 + 延迟，throughput 不受 API 监听影响，
P50 1.5ms 与无 API 时一致。

### 内部

- `client.py` 在 build_outbounds / build_router 之后启动 APIServer；shutdown
  时优雅 stop
- API server 与 SOCKS5 server 在同一 asyncio loop，无额外线程 / 进程

### 后续

P2-P5 接续：connections registry（活跃连接列表 + 流量计）→ proxies/rules 映射 →
WebSocket traffic/logs 推流 → mirage 私有端点。

---

## [0.4.16] - 2026-06-11

### 新增 / 配置 schema 合约

- **配置 schema_version=1 落地**（`core/config.py`）
  - 顶层 8 个 key 锁定：`schema_version` `log` `inbounds` `outbounds` `route`
    `dns` `api` `tuning`。**合约**：schema_version=1 期间不再加第 9 个；新功能
    进入已有 section 的 nested key
  - 配置文件加 `"schema_version": 1` 即识别为 v1，触发严格验证
  - 缺 `schema_version` + 命中 legacy 顶层 key（`server_host` / `password` / ...）
    → 视为 v0 legacy，启动期 INFO 日志，保持现有自动合成行为
  - 未知顶层 key → WARNING（不阻止启动，便于发现拼写错误）
- **`dns` 顶层 section**（DNS 转发 + 缓存 + 分流 + hosts + fakeip）
  - `dns.listen`（"host:port"，缺省不起 DNS 服务）
  - `dns.default`（resolver tag；**未填时自动回退到 `resolvers[0].tag`** 并
    INFO 提示）
  - `dns.resolvers[*]`：`tag` / `address` / **`via` 必填**（必须指到某 outbound tag）
  - `dns.rules[*]`：`match` 用 `kind:value` 前缀语法（`domain:` / `domain-suffix:`
    / `geosite:` / `ipcidr:` / `geoip:` 等），`use` 指 resolver tag
  - `dns.cache` / `dns.hosts` / `dns.fakeip` 占位（实际逻辑后续 MINOR 实现）
  - `dns.strategy`：`prefer_ipv4` / `prefer_ipv6` / `ipv4_only` / `ipv6_only`
- **`tuning` 顶层 section**（高级调优）
  - 所有 perf knob（pool_size、stagger_step_sec、idle_timeout 等）今后归入这里
  - 启动期检测到非空 tuning → INFO `tuning overrides active: <keys>`，便于事后
    排查"为什么这台机器表现不一样"
  - README 单独章节文档化为 "advanced"，主表 / 快速开始**不出现**
- **`api` 顶层 section**（schema 占位，端点逻辑在后续 P1-P5 实现）
  - `api.listen` / `api.secret` / `api.cors`
  - **强制鉴权**：`api.listen` 设了但 `api.secret` 缺失 → 启动期 ConfigError
  - 非 loopback 绑定 → 启动期 WARNING

### 内部

- `core/config.py::load_config()` 替代 `client.py` / `server.py` 里的
  `json.load`；统一配置入口
- `core/config.py::validate_cross_refs(cfg, outbound_tags)` 在 `build_outbounds`
  之后调用，校验 `dns.resolvers[*].via` 引用的 outbound 确实存在
- v1 → legacy projection：v1 的 `inbounds[0]`（socks5）/ `dns.listen` 投射到
  `cfg['socks5_host']` / `cfg['socks5_port']` / `cfg['dns_listen_*']`，让下游
  模块不必同时支持两套 schema。下游模块逐步迁移到读 v1 sub-section 后可删除

### 稳定性合约（写进 schema_version=1）

| 改动 | 允许在哪个版本 |
|---|---|
| 新顶层 section | 只 MAJOR（`schema_version` 升号） |
| 新 nested key（带默认值） | MINOR |
| 改字段含义 / 移除 | 只 MAJOR |
| 重命名（保留老名为 alias） | MINOR，老名至少保留 3 个 MINOR |
| 默认值变化 | PATCH（CHANGELOG 必须标注） |
| 校验严格度提升 | MINOR，先 WARN 一轮再 ERROR |

### 迁移

- **完全向后兼容**：现有 `config_client.json` / `config_server.json` 不需要
  改动，启动后看到 `Legacy schema detected ...` INFO 日志即正常
- 自愿迁移：在配置首行加 `"schema_version": 1`，按 README 的 v1 模板把
  legacy 顶层 key 重构到对应 section（`socks5_host` → `inbounds`、
  `server_host` → `outbounds[0]`）
- 自动迁移工具 `mirage migrate-config` 留待后续 MINOR

### 验证

- 7 个验证场景全通：legacy 识别 / 最小 v1 / 完整 dns / 缺 via 报错 / 跨引用
  报错 / 未知顶层 key WARN / api 鉴权强制
- E2E：legacy 配置 + v1 配置分别跑通，bench c=4 上行 ~520 Mbps，与 0.4.15
  无回归

---

## [0.4.15] - 2026-06-11

### 内部 / 性能

- **`brutal_pool_size` 默认 10 → 20**
  - 背景：2026-06-10 B1 性能基线（`bench.py`，c=16 并发上行）显示，原默认 10
    + 0.3s 阶梯（staircase）下，16 并发触发 6 条冷建，bench 前 ~2s 被啃掉，实测
    上行吞吐 243 Mbps；池设到 32 后同测试得 477 Mbps（+96%）。
  - 浏览器日常并发 5-10、HTTP/2 多路复用后更低；默认 20 即覆盖 95% 个人使用
    场景，避免冷建拖慢。内存代价 ~ 10 条 × 50KB tunnel state = +500KB，可忽略。
- **新增配置项 `stagger_step_sec` / `stagger_jitter_sec`**
  - 之前阶梯延迟硬编码在 `_STAGGER_STEP=0.30` / `_STAGGER_JITTER=0.08`。
    现可在 outbound 节点配置里覆盖。
  - 默认仍是 0.30 / 0.08（pcap 反 SYN-burst 指纹的实测值）；高并发且不在意 GFW
    流量分析的内网场景可降到 0.05 / 0.02 加速 refill。

### 新增

- **`bench.py`：吞吐 + 延迟基准脚本**
  - 场景：upload / download / bidir / latency（小包 ping-pong，P50/P95/P99）
  - 参数：`-c` 并发连接、`-d` 时长、`--scenarios`、`--no-uvloop`、`--warmup-wait`、
    `--json` 落盘
  - 测量目标进程的本地 sink/source/echo 由脚本自启自停
- **`PYREALIY_NO_UVLOOP=1` 环境变量**：禁用 uvloop（A/B 对照用）
  - 实测对比（c=16 × 10s）：uvloop 关→开 上行 +12.6%、bidir 聚合 +18.2%、
    P95 延迟从 54.5ms 降至 4.8ms（-91%）。结论：uvloop 该留默认开。

### 文档

- `README.md` 节点配置参数表新增 `stagger_step_sec` / `stagger_jitter_sec`
  说明；`brutal_pool_size` 文档加上 "按预期最大并发 conn 数定" 的指导

### 性能基线（POC 单进程 Python，对比仅供参考）

实测环境：单机 loopback，Python 3.11.2 + uvloop 0.17.0，c=16 并发 × 10s：

| 项目 | 结果 |
|---|---|
| Upload | 243 → 477 Mbps（池 10→32 时） |
| Download 16 conn | ~344 Mbps（uvloop 无差异 → CPU 瓶颈） |
| Latency P50 | ~1.5 ms |
| Latency P95 (uvloop) | ~4.8 ms |
| Latency P95 (默认 asyncio) | ~54 ms |
| 单核 CPU | server + client 双方均 80-100% pegged |

**单进程 Python 单核天花板 ~500 Mbps**（ChaCha20-Poly1305 AEAD × 双方加解密 × 4
次/包）。突破这条线需要多进程 / Rust 重写——POC 阶段不做。

### 迁移

- **无破坏性改动**。
- 原 `brutal_pool_size: 10` 配置仍工作；不写则默认升到 20。
- 内存敏感的部署可显式写回 `brutal_pool_size: 10`。

---

## [0.4.14] - 2026-06-08

### 修复 / 透明化

- **修：geosite/geoip 缓存"每次启动都下载"的隐性根因 + 给出可见诊断**
  - 现象：用户每次启动 client 都看到下载，**实际上代码里已有 `geosite_update_days`
    判定**，但因为几个原因看起来像没生效：
    1. **`geosite_dir` 默认 `.geosite` 是相对路径**，systemd 启动 / 不同 CWD →
       每次找到不同的物理 `.geosite` → 找不到上次的 meta.json → 视为首次下载
    2. **"up-to-date" 命中日志是 DEBUG 级别**，用户看不到"今天没下载"的事实，
       只看到"Downloading geo[...]"以为每次都重下
    3. 没有任何日志显示 meta.json 在哪、读到几条记录
  - 修法（全部在 `core/geosite_cache.py`）：
    1. **绝对路径化**：`ensure_all` 启动期 `os.path.abspath(cache_dir)`，无论
       从哪起 CWD，缓存目录位置固定（如果原是相对路径会同时打日志展示解析结果）
    2. **命中日志改 INFO**：`geo[<key>] up-to-date, skip download (age N.Nd < M.Md, file=...)`，
       用户能直接看到"今天复用缓存"
    3. **诊断行**：`meta.json: N source(s) tracked at <path>` 或 `meta.json empty
       / not found (<path>) — all sources will download`，把读到的状态摊开
    4. **下载理由清晰化**：`Downloading geo[<key>] from N mirror(s) — <reason>`，
       reason ∈ `{local .dat missing, no downloaded_at timestamp in meta.json,
       age N.Nd > update_days M.Md, force update}`

### 新增

- **`cfg["force_geosite_update"]` 可选 bool**：设为 true 时一律强制重下载（默认
  false）。**用于"GitHub 释放了新版规则我想立刻拉一次"或"调试 geo 下载链路"**
  的场景。一次性 force → 把它设回 false 即可恢复正常的 update_days 判定

### 验证

端到端 4 场景实测：

| 场景 | 期望 | 实际 |
|---|---|---|
| 空缓存目录（首次启动）| "meta.json empty" + 触发下载 | ✓ |
| 1 天前下载（< 7 天）| `skip download (age 1.0d < 7.0d)` | ✓ |
| 8 天前下载（> 7 天）| `Downloading — age 8.0d > update_days 7.0d` | ✓ |
| `force_geosite_update: true` | `Downloading — force update` | ✓ |

### 迁移

- 行为变化：缓存目录现以绝对路径解析，从不同 CWD 启动 client 不再各自维护一份
  `.geosite/`。**用户感知是"原本每次启动都下载，现在第一次后就不动了"**
- 老配置无需改动：`geosite_update_days` 默认 7 天，`force_geosite_update` 默认 false

---

## [0.4.13] - 2026-06-07

全模块自审，找到 1 个真 leak + 2 处微优化。

### 修复

- **🚨 D1 修：DNS pipeline `_ensure_ready` 失败时 tunnel leak**
  - 现象：`core/dns_forwarder.py:_DnsTunnel._ensure_ready` 从 outbound acquire 一条
    tunnel 后立即 `tunnel.send(pack_address(remote_dns, 53))` 标记 DoT 起手。
    **如果 send 失败**（对端 RST / 写入异常）：`ready` 已被取走但 `self._ready`
    没赋值、`ready.close()` 没调，**池里少一条 tunnel 长期不补，底层 server_writer
    长期泄漏**
  - 修法：用 try/except 包 send，失败时 `ready.close()` 再 raise，let `query()`
    的重试逻辑去补
  - 真实场景：用户在 DNS 转发场景下偶发遇到"池越来越空、新 DNS 查询越来越慢"
    可能就是这条路径在悄悄积累

### 微优化

- **D2 修：`tunnel.py:drain_recv` 函数内 `import time as _time` 改为模块级**
- **D3 优：`time_sync._ntp_query_https` 用 `buf.find(b"\\r\\n\\r\\n")` 替代
  `b"\\r\\n\\r\\n" not in bytes(buf)`** —— bytearray 原生支持 substring 搜索，
  不需要每次迭代 `bytes()` 复制整个 buffer

### 审计覆盖

逐文件审计了所有模块（含 0.4.10/11/12 新代码 + 之前没复审过的 admin / stats /
sniffer / router 等）。除上面 3 处，**未发现其它真问题**：

| 模块 | 备注 |
|---|---|
| `admin.py` | CSRF + hmac.compare_digest 安全 ✓ |
| `stats.py` | LRU OrderedDict 无 OOM 风险 ✓ |
| `sniffer.py` | TLS SNI / HTTP Host 解析全 bounds 检查 ✓ |
| `router.py` | structured + CSV 双路径、规则前缀提取等无问题 ✓ |
| `hello_auth.py` | TimeSync `_now()` 整数转换无溢出风险 ✓ |
| `outbound.py` | `_bidi_tunnel_relay` 关闭顺序 safe_close + drain_recv 正确 ✓ |
| `conn_pool.py` | acquire 流程 stale + alive 双过滤、fallback 走游标 ✓ |
| `handshake_cache.py` | ServerHello 字段补丁 定长替换 不破坏 record_length ✓ |
| `udp_relay.py` | C1-C4 + B1-B4 已在 0.4.11/12 修完 ✓ |

### 经验

- DNS pipeline 这种"取出资源 → 立即用 → 失败不归还"的代码片段，**很容易在
  压力测试 / 长跑场景才暴露问题**，单元测试根本测不出来。审计这种代码要专门
  问"if 这一行抛了，前面 acquire 的东西谁负责关？"

---

## [0.4.12] - 2026-06-07

### 修复（0.4.11 之后再审计发现的）

- **🚨 C1 修：UDP 目标是域名时阻塞事件循环**
  - 0.4.11 我把"域名 target 走 getaddrinfo"列为"文档化限制"，但**没在代码里 guard**
    —— Python `socket.sendto` 对 UDP socket 传域名时会调内核 `getaddrinfo` 解析，
    **同步阻塞 asyncio 事件循环**几秒，整个 client / server 卡死
  - 修法：新增 `_is_ip_literal(host)` 预检 + 在 `_handle_uplink`（客户端 direct）
    和 `_tunnel_to_target`（服务端）两处加 guard，**域名目标直接 drop**
    （+ debug 日志说明），事件循环不再被卡
  - 实测：域名 target 包处理耗时 < 1ms（vs 旧版可能数秒）

- **🚨 C2 修：服务端 `handle_udp_tunnel` 缺 idle timeout**
  - 客户端 tunnel TCP 半死时，`tunnel.recv` 挂死等 TCP keepalive（~90s），期间
    UDP socket + tunnel 资源都不回收
  - 修法：加 `idle_timeout` 参数（默认 600s）+ `_idle_watcher` 协程 + `_touch()`
    helper 在每次双向 IO 触达时刷新最后活动时间，超时 stop.set 触发清理

- **C3 修：`router._default` 私有访问**
  - `client.py:_dispatch_udp` 走 `router._default` 私有属性。新增 `Router.default`
    property，调用方用公开 API

- **C4 修：v4-mapped 还原大小写敏感**
  - `if host.startswith("::ffff:")` 只认小写。Python 多数情况返回小写但脆弱。
    新增 `_unmap_v4(host)` helper 走 `ipaddress.IPv6Address.ipv4_mapped`，
    标准库级别正确处理大小写 / 边界（如 `::ffff:0.0.0.0` 是合法但奇怪的值）

---

## [0.4.11] - 2026-06-07

### 性能 / 修复

代码审计 + UDP 热路径优化，闭合 0.4.10 新功能的几个真问题。

- **修：UDP 出口 IPv6 支持**
  - 0.4.10 服务端 `handle_udp_tunnel` 与客户端 direct 模式都用 `AF_INET / 0.0.0.0`
    socket，**给 IPv6 target sendto 会 EAFNOSUPPORT 失败**——代理 HTTP/3 /
    IPv6 服务端就崩
  - 修法：新增 `_create_dualstack_udp_endpoint()` helper，建 `("::", 0)` 的
    dual-stack v6 socket（Linux 默认 IPV6_V6ONLY=0），同时收发 v4/v6；
    `_normalize_addr_for_dualstack()` 把 IPv4 字面量映射到 `::ffff:1.2.3.4` 让
    v6 socket 也能发；回包时若 source 是 v4-mapped，还原为 v4 字面给 SOCKS5
    UDP header
  - 建 v6 socket 失败时自动回落到老的 v4 socket（Windows / 老 BSD 不支持
    dual-stack 时的兜底）

- **修：`_socks_client_addr` 只锁首包 → 改成跟最近一次**
  - 现象：部分 DNS 解析器每查询用一个新源端口；旧实现把回包统一发给首端口
    导致应用收不到回包
  - 修法：每个上行包都刷新 `_socks_client_addr` —— 回包跟最近一次源 addr

- **性能：UDP 改用队列消费替代每包一个 Task**
  - 旧实现 `datagram_received` 里 `asyncio.create_task(...)` 每个包一个 Task。
    高 pps 场景（游戏 / 视频）峰值上千 Task 创建/秒，GC + 调度开销不必要
  - 改用 `asyncio.Queue(maxsize=1024)`：`datagram_received` 同步 `put_nowait`，
    单消费协程 await get 串行处理。**满则丢**（UDP 本就 best-effort 背压策略）
  - 客户端 UDPRelay 和服务端 handle_udp_tunnel 两侧都改

- **修：parse 阶段异常被 asyncio 记成 unhandled task exception**
  - 旧 `_on_local_udp` / `_on_udp_reply` 只 try 了 `tunnel.send`，
    `parse_socks5_udp_header` / 算长度等地方抛非预期异常时，asyncio 会在
    Task 析构时 logger.error
  - 修法：消费协程内层加全包 try/except + debug 日志

### 内部清洁

- UDPRelay direct 模式从"raw socket + sock_recvfrom"统一为 `DatagramProtocol`，
  与 tunnel 模式保持一致 API、易维护
- `server.py` UDP 字节计数 `setattr` lambda 改成闭包函数，少一次属性 set 调用

### 限制（明确文档化）

- 当目标 host 是**域名**（SOCKS5 ATYP=domain）时，`transport.sendto` 会调
  内核 getaddrinfo，**阻塞当前事件循环**。多数 UDP 应用直接用 IP 作 target，
  不踩到这条路径。彻底异步 UDP DNS 解析留待 v2
- block outbound 拒绝 UDP（final 应配 mirage 或 direct）
- SOCKS5 UDP FRAG≠0 包丢弃

### 迁移

- 协议线上字节流无变化（修的是实现细节）
- 客户端字段 `udp_relay_host` / `udp_idle_timeout` 不变

---

## [0.4.10] - 2026-06-07

### 新增

**A1: UDP 转发（SOCKS5 UDP ASSOCIATE + UDP-over-TCP 隧道）**

闭合"优秀代理诊断" A 类的最大缺口。完成后 mirage 可代理任何 UDP 流量：
游戏 / 视频通话 / WireGuard / QUIC / 普通 DNS 等。

- **`core/udp_relay.py`** 新模块：
  - SOCKS5 UDP header 解析 / 构造（RFC 1928 §7，支持 IPv4 / IPv6 / domain）
  - 加密隧道内 UDP 帧封装：`[2B len][packed_addr][payload]`
  - `FrameReader` 类：从 `tunnel.recv()` 不定长 chunks 中缓冲式切出完整帧
    （帧可跨多条 TLS record）
  - `UDPRelay` 类（客户端侧）：绑本地 UDP socket、对接 SOCKS5 UDP 报文
    与加密隧道帧、看门狗（TCP 控制连接关 / idle 超时）
  - `handle_udp_tunnel(tunnel, ...)` （服务端侧）：单 UDP socket 多路复用
    NAT，桥接 tunnel ↔ raw UDP

- **`core/socks5.py`** 扩展：
  - 新增 `CMD_UDP_ASSOCIATE = 3` 支持
  - `parse_socks5_request` 返回值从 `(host, port)` 改为 `(cmd, host, port)`
    其中 `cmd ∈ {"tcp", "udp"}`
  - 新增 `reply_udp_associate(writer, bnd_host, bnd_port)` —— UDP ASSOCIATE
    的 SOCKS5 回复（必须在 bind 本地 UDP 后才能算出 BND 地址）

- **服务端**：`server.py:handle_client` 读到首包首字节为 0x00（host_len=0）
  时进 UDP 路径——调用 `handle_udp_tunnel`。**完全向后兼容**：合法 IP / 域名
  的 `pack_address` 首字节 host_len 永不为 0（最短 "0.0.0.0" host_len=7），
  TCP 路径不受任何影响

- **客户端**：`client.py:_dispatch_udp` 新分派路径
  - UDP 路由 = `route.final` 的 leaf
  - 解析到 `MirageOutbound` → acquire tunnel + 发哨兵 `b"\x00"` → 隧道路径
  - 解析到 `DirectOutbound` → UDPRelay 走本地 socket 直发
  - 其他（block 等）→ 拒绝 UDP ASSOCIATE
  - 新配置字段 `udp_relay_host`（默认 socks5_host）、`udp_idle_timeout`（默认 60s）

### 协议设计

- **UDP 模式哨兵**：客户端首包 `b"\x00"`（host_len=0），向后兼容 0.x 老协议
- **帧格式**：`[2B 总长度 BE][packed_addr][UDP payload]`
- **多路复用**：一条 tunnel 内多个 UDP 目标共用，每包带 dest header
- **服务端 NAT**：每条 UDP-mode tunnel 绑一个 ephemeral UDP socket，多 target
  共用（Linux UDP NAT 表项按 target 自动追踪）

### 限制（文档化，本版不做）

- TProxy UDP：v2
- 按 UDP 目标做 router 决策：当前所有 UDP 走 final outbound；按 target 分流
  v2
- block outbound 拒绝 UDP（用户应该让 final 落到 mirage 或 direct）
- SOCKS5 UDP FRAG≠0（分片）直接丢

### 迁移

- 协议向后兼容：老 TCP 客户端 / 老服务端继续工作（首字节非 0 走 TCP 路径）
- API 变化：`parse_socks5_request` 返回 3-tuple `(cmd, host, port)`，仅 `client.py`
  一处调用方，已同步更新

---

## [0.4.9] - 2026-06-07

### 安全 / 反指纹

闭合 "优秀代理特性诊断" 中 A 类的两个真实弱点（A1 UDP 待用户对齐设计后单独发版）。

- **A2: ServerHello session_id_echo + server_random 现场补丁**
  - 根因：旧实现直接回放 handshake_cache 里整条 ServerHello record，**两个
    TLS 1.3 spec 强制约束被破坏**：
    1. `session_id_echo` 是缓存抓取时那个伪 client 的随机值，**跟当前客户端
       的 session_id（token）不一样**——任何会解 TLS 的探测端 1 RTT 内就发现
       "server 没正确 echo session_id"
    2. `server_random` 在多次连接里**相同**（同一份 cached record 反复回放）
       → GFW 把 random 值聚类，统计上立刻发现"代理"
  - 修法：`core/handshake_cache.py` 新增 `_patch_server_hello(record, sid, rnd)`，
    回放前现场改写两个字段：
    - `server_random` 用 `os.urandom(32)` 每次新鲜
    - `session_id_echo` 用当前客户端 session_id（即我们的 token）
    - **不动 record_length / handshake_length**（定长替换），对 wire 上的解析器
      完全透明
  - 透明性：TLS 1.3 transcript_hash 因为改了 random/sid_echo 会不一致，但
    后续的 EncryptedExtensions/Certificate/CertVerify/Finished 都是不透明
    AEAD，**GFW 没密钥无法验**；客户端我们根本不跑真 TLS、也不解密这些
  - 调用方：`core/camouflage.py` 两处都改：
    1. 认证通过的 proxy 路径：传客户端 session_id 给 send_server_hello_done
    2. 探测路径：把探测端 ClientHello 里的 session_id 透传给 serve_probe
       （真实 TLS server 必须 echo，伪装路径也得跟上）

- **A3: 池僵尸 tunnel 探活（SO_KEEPALIVE + MSG_PEEK 双兜底）**
  - 根因：旧 `_ReadyTunnel.is_stale` 只看时间不看 TCP 真实状态。对端因服务端
    idle timeout / NAT 表项过期 / 网络抖动后 FIN 丢包等原因悄悄关了但我们
    没感知，**用户第一次从池里取这条 tunnel 才发现 send 失败**、感受到一次
    延迟翻倍
  - 修法（双兜底）：
    1. **建连时设 SO_KEEPALIVE + Linux TCP_KEEPIDLE=60s / INTVL=10s /
       CNT=3**（90s 内 OS 自己 RST 死连接），让池更早 refill
    2. **acquire 时实时 `is_alive` 检测**：从队列拿出 tunnel 后用
       `MSG_PEEK + MSG_DONTWAIT` 看一字节——FIN/RST/异常推数据都判为 dead、
       立即 discard + 触发 refill
  - 抓底层 socket 走 `writer.transport.get_extra_info("socket")`，无 socket 时
    保守认为活
  - 影响文件：`core/conn_pool.py` 新增 `_enable_tcp_keepalive()` helper +
    `_ReadyTunnel.is_alive` property；`acquire()` 加 is_alive 检查分支

### 内部

- `_ReadyTunnel.is_alive` 是 property（带状态副作用：read syscall）。命名按
  Python 惯例本应改成方法，但本项目内部使用、对调用方简洁性更重要

### 迁移

- 协议线上字节流变化：ServerHello 的 random 现在每连接独立、session_id_echo
  正确 echo 客户端 token —— 与真实 TLS 1.3 行为一致
- 池僵尸 tunnel 自动 discard：用户感知是"突发突发用代理时第一条更稳"，无 API
  变化
- `handshake_cache.send_server_hello_done(writer)` → 必须传 `client_session_id`
  参数；`serve_probe(reader, writer)` 加可选 `probe_session_id` 参数

---

## [0.4.8] - 2026-06-07

### 新增

- **客户端 / 服务端启动期自动时钟同步（`core/time_sync.py`）**：解决 VPS 时钟
  漂移 > `TIMESTAMP_TOLERANCE = 60s` 导致 `TokenReplayCache` 把所有合法 token
  误判为超时、连接全失败的硬伤
  - **分层策略**：UDP NTP（port 123）优先 → HTTPS Date 头（port 443）兜底 →
    系统时钟兜底。"TCP 路径"工程上等价于 HTTPS Date —— 几乎没有公开 NTP server
    真的支持 RFC 5905 §7.5 的 TCP/123；HTTPS Date 秒级精度对 60s 容差绰绰有余
  - **多源 median 抗劫持**：每次同步从 UDP 或 HTTPS 各取 ≤3 个源，median 决定
    offset；`max_offset_sec` 净化：>1 天的偏移直接拒绝
  - **不动系统时钟**：只在 hello_auth 中通过注入的 time provider 应用偏移；
    其他程序（chrony 等）不受影响
  - **HTTPS Date 直连不走代理隧道**：避免"隧道又依赖时钟"的鸡蛋问题
  - **启动期阻塞首次同步**（默认上限 5s），失败不挂掉业务、后台周期重试
- **`hello_auth` 新增 `set_time_provider(fn)` 注入点**：`make_session_token` /
  `verify_session_token` / `TokenReplayCache._bucket` 三处从 `time.time()`
  改走 `_time_provider()`；TimeSync 启动后注入 `TimeSync.corrected_time`
- **新配置字段 `cfg["time_sync"]`**（可选；不写按默认走）：

  ```json
  "time_sync": {
      "enabled": true,
      "udp_servers": ["pool.ntp.org", "time.cloudflare.com", "time.google.com"],
      "tcp_servers": ["www.apple.com", "www.cloudflare.com", "www.microsoft.com"],
      "interval": "1h",
      "startup_timeout": "5s",
      "max_offset_sec": 86400
  }
  ```

  - 时长字段支持 `30s` / `5m` / `1h` / `1d` 或裸数字（秒）
  - 不写 `time_sync` 字段 = 全部使用默认值 + 启用
  - `enabled: false` 关闭（用户已有 chrony 等场景）

### 内部

- `core/hello_auth.py` 调用 `time.time()` 的三处统一封装为 `_now()` helper

### 迁移

- 老配置完全无需改动：缺 `time_sync` 字段时按默认启用，使用公开 NTP/HTTPS 源
- 老调用方式无回归：`time_provider` 默认就是 `time.time`，TimeSync 未启用 /
  未同步成功时 offset=0、行为完全等价于老代码
- API 新增：`hello_auth.set_time_provider(fn)`，`TimeSync.corrected_time` 类方法

---

## [0.4.7] - 2026-06-06

### 新增

- **`setup.py` 加入 `log_levels` 交互式询问**：客户端 / 服务端配置流程
  最后会问"是否启用按模块调试日志"。选 yes 则展示带说明的模块清单，按
  编号多选，自动生成 `cfg["log_levels"]` 写入配置：
  - 客户端可选 9 个模块（outbound / conn_pool / router / dns / group /
    healthcheck / utils / client / asyncio）
  - 服务端可选 8 个模块（server / camouflage / handshake_cache /
    conn_pool / router / egress / admin / asyncio）
  - 进阶可同时把 root logger 提到 WARNING 屏蔽其他模块 INFO 噪音
  - 抽出 `configure_log_levels(side)` helper，两边共享
- **`install.sh` banner 显示版本号**：从 `core/version.py` 读 `__version__`，
  与 client/server 启动 banner 一致（单一来源）

### 修改

- **`install.sh` 客户端配置切换到 sing-box 风格新 schema**：从老的顶层
  `server_host` / CSV `rules` 改为 `outbounds` 数组 + `route.rules` 对象数
  组。单节点配置等价于老格式，但**便于以后扩展为多节点 + urltest 组**
  （详见 README"多节点与自适应选路"章节）。老配置文件仍由 `build_outbounds`
  的兼容路径支持，无需迁移
- **客户端默认 `brutal_pool_size` 从 10 调高到 20**（`install.sh` + `setup.py`）：
  0605.pcap 池突发分析显示 10 的池在 8-9 并发突发时会瞬间打空、触发 5s
  超时 fallback 风暴。20 能扛住典型家用场景的瞬时高并发，从源头降低
  "Pool exhausted" 频率与 SYN 同秒爆发的几率

### 内部

- `setup.py` 模块清单在两个常量里维护（`_LOG_MODULES_CLIENT` /
  `_LOG_MODULES_SERVER`），新增 / 删除模块时只需改这两个 list

---

## [0.4.6] - 2026-06-05

### 安全 / 反指纹

0.4.5 的修复消除了 87% 的客户端 RST 与一半的 SYN 同秒爆发，但 0605.pcap
对照分析揭示了**两个残余特征**，本版闭合。

- **修：服务端在 FIN 后 ~2s 发 RST（占服务端连接 23%）**
  - 根因：0.4.5 的 `safe_close` 加了 `write_eof()` 半关写端发 FIN，但
    **Linux close() 看到接收缓冲有未读数据仍 RST**——`write_eof` 解决不了
    接收侧。`wait_both_with_grace` 2s 超时 cancel `target_to_tunnel` 后，
    `client_reader` 的 OS 接收缓冲里**还有客户端继续推过来的加密字节**
    （前条 record 没被 tunnel.recv 拿走 / 客户端后续小帧），close → RST
  - 抓包证据：0605.pcap 52 个服务端 RST，距最后一条 payload **中位 1.98s**，
    精确命中 `_DRAIN_AFTER_HALF = 2.0` 的超时点
  - 修法（三处协同）：
    1. **`safe_close(writer, reader=None)`** 新增可选 `reader` 参数：
       在 `close` 之前 best-effort 读光接收缓冲（最多 0.5s 硬上限）。close
       看到接收缓冲为空 → 发 FIN 不发 RST
    2. **`EncryptedTunnel.drain_recv(max_seconds)`** 新增方法：绕过 TLS
       解码，直接从底层 `_reader` 读光 OS TCP 接收缓冲（grace 超时后对端
       可能还在推加密 record，我们不需要解密、只需消费）
    3. 三个中继的 finally：close 前先 `await tunnel.drain_recv(0.5)`
       （加密侧）+ `safe_close(writer, reader)` 传入 reader 让其 drain
       应用层接收缓冲
  - 影响文件：`core/utils.py:safe_close` 签名扩展 / `core/tunnel.py` 新增
    方法 / `core/outbound.py:_bidi_tunnel_relay` finally / `core/utils.py:relay`
    finally / `server.py:handle_client` finally

- **修：连接池 acquire() 5s 超时 fallback 绕过 staircase**
  - 根因：`BrutalPool.acquire()` 等不到队列 5s 后会落到"直接 build 一条"
    fallback。这条路径**不调 `_reserve_build_slot`**，多个并发 acquire 同时
    走到 fallback 时各自立刻发 SYN
  - 抓包证据：0605.pcap 残余 18 个 <50ms 同秒 SYN（8.5%），端口号连号
    集中在两片，命中 fallback 风暴模式
  - 修法：fallback 分支也走 `_reserve_build_slot`——延续池级游标累计。池
    空时游标多在过去 → 第 1 个 delay=0 立即发、第 2 个 ~300ms 后。**对单
    并发 acquire 无延迟代价**，多并发场景下错开
  - 影响文件：`core/conn_pool.py:BrutalPool.acquire` 的 `except TimeoutError`
    分支

### 迁移

- 协议线上字节流变化：服务端关连接时不再出现"FIN 后 ~2s 发 RST"模式，SYN
  时间序列彻底落到 staircase（含 acquire 超时分支也阶梯化）
- API 变化：`safe_close(writer)` → `safe_close(writer, reader=None)`，
  reader 可选；老调用方式仍兼容
- 新增 `EncryptedTunnel.drain_recv(max_seconds=0.5)` 公开方法，供调用方在
  关 tunnel 之前 drain 接收缓冲

---

## [0.4.5] - 2026-06-04

### 安全 / 反指纹

经 0604.pcap（实际抓包）对照分析发现**两个明确可被 GFW 利用的指纹**，本版闭合：

- **修：双向中继关闭时 FIN 后紧跟 RST（87% 客户端连接受影响）**
  - 根因：`core/utils.py:safe_close` 旧实现直接 `writer.close()`，Linux TCP 规约
    在接收缓冲有未消费数据时 close() 会发 RST 而非 FIN。双向中继场景里这是
    常态——一方向 EOF 时另一方向往往刚收到几条 record 还没读、buffer 里有
    数据 → close → RST
  - 抓包对照：真实 HTTPS 0 个 RST；我们的 92.7% 连接显示 FIN→RST 模式，
    间隔中位 78ms，是非常稳定的"代理客户端"指纹
  - 修法（三处协同）：
    1. **`core/utils.py:safe_close`** 改为四步优雅关：`write_eof()` 半关
       写端发 FIN → `drain()` 冲完发送缓冲 → `close()` → `wait_closed()`
    2. **新增 `wait_both_with_grace(task_a, task_b, grace=2.0)`** helper：
       等任一方向结束后给另一方向最多 2s 自然退出（让 server 收到 close_notify
       后自己也回 close_notify、tunnel.recv() 自然 EOF），超时才 cancel
    3. **中继协程内不再 `safe_close(writer)`**，只发 FIN（write_eof 或
       tunnel.send_close_notify），由外层 finally 在两方向都退出后统一 close
       —— 此时接收缓冲已空，OS 发 FIN 不发 RST
  - 影响文件：`core/utils.py` / `core/outbound.py:_bidi_tunnel_relay` /
    `server.py:handle_client` 三处中继的关闭路径

- **修：连接池 SYN 同秒爆发（19% 相邻间隔 <50ms）**
  - 根因：`core/conn_pool.py:_schedule_refills` 旧实现每次调用都从 `i=0`
    重新算 `_staggered_delay(i)`。N 个并发 acquire 各自调一次该方法、各看到
    deficit=1，每次都把唯一那条 build 排到 delay=0 立刻发 → N 条 SYN 同秒
    一齐出去。staircase **只在单次调用内**有效，跨调用就破了
  - 抓包验证：0604.pcap 中 43/227 个相邻 SYN 间隔 <50ms（19%），属代理
    批量建连指纹
  - 修法：把阶梯起点改为 **池级游标 `_next_build_at`**（monotonic time）。
    新增 `_reserve_build_slot()` 内部方法：每次预定 build 把游标推进
    `_STAGGER_STEP ± _STAGGER_JITTER`，跨 `_schedule_refills` 调用累计。
    这样全局任意两条相邻 build 真实启动时间间隔 ≥ ~0.22s
  - 影响文件：`core/conn_pool.py:BrutalPool` 加 `_next_build_at` 属性 +
    `_reserve_build_slot()` 方法；`warmup()` 与 `_schedule_refills()` 改用
    新机制；删除原 module 级 `_staggered_delay(i)` 函数

### 内部

- `core/utils.py` 新增模块级常量 `_DRAIN_AFTER_HALF = 2.0`（单向结束后给另一
  方向优雅退出的硬上限），与 `_CLOSE_TIMEOUT` 区分用途

### 迁移

- 协议线上字节流改变：关连接时不再出现 FIN→RST 序列（合规化），同时 SYN
  时间序列从"同秒爆发"恢复为真阶梯。**对端无感**，老客户端 / 老服务端继续
  互通
- 老 `_staggered_delay(i)` 函数已删除：本属内部辅助函数，无外部调用方

---

## [0.4.4] - 2026-06-04

### 新增

- **`cfg["log_levels"]` 配置项**：按模块控制日志级别，无须改代码或环境变量。
  `client.py` / `server.py` 启动期调用 `core.utils.apply_log_levels(cfg)` 读取。
  - 例：`{"outbound":"DEBUG","server":"DEBUG","router":"WARNING"}`
  - 大小写不敏感；`WARN` 视为 `WARNING`；特殊键 `"default"` 作用在 root logger
  - 类型守门：非字符串值、非字符串 key、空键、未知级别都会 warning + 跳过、
    不打断启动
  - 影响文件：`core/utils.py` 新增 `apply_log_levels` 与 `_logger`；
    `client.py` / `server.py` 启动 banner 之前各加一行调用；
    `README.md` 高级调优字段表追加 + 新增"按模块开调试日志"子节
- **三处中继 leg 的可选 DEBUG 日志**（默认静默，开 DEBUG 才可见）：
  - `core/utils.py:relay()` —— `DirectOutbound` 直连路径，方向 `local→remote` / `remote→local`
  - `core/outbound.py:_bidi_tunnel_relay()` —— `MirageOutbound` 隧道路径，
    方向 `local→tunnel` / `tunnel→local`
  - `server.py` `tunnel_to_target` / `target_to_tunnel` —— 服务端中继
  - 输出格式：`relay <label> <方向> ended: <异常类型>: <消息>`，label 由
    调用方传入（含 outbound tag + 目标 host:port，server 端含 `conn.id`）
  - **用途**：排查"代理莫名其妙就断了"类问题。对端 RST / FIN / Timeout 默认
    静默是网络代理常规操作，开 DEBUG 即可看到具体类型

### 内部

- `core/utils.py` 模块级 logger 从 `_RELAY_LOGGER` 改名为 `_logger`：原名只
  指代中继用途、加 `apply_log_levels` 后已不准确

---

## [0.4.3] - 2026-06-04

### 修复

- **`core/conn_pool.py:_read_server_handshake` 显式校验 TLS 1.3 握手 flight**：
  老实现 `except Exception: pass` 把所有失败信号吞掉，导致 `_build_one` 误判
  "握手成功"、派生密钥后把已经死掉的隧道返回给调用方，**首次 `send`/`recv`
  才以深栈 EOF 形式爆出来、用户根本看不出根因是握手没成**。
  - 新逻辑显式 raise `IOError`，被 `_build_one` 外层捕获并视作 build 失败：
    - `0x15` Alert：`server sent TLS alert (level=2, desc=51)` —— desc 通常
      就直接对应 RFC 5246 §7.2 的告警含义（51=`decrypt_error` ≈ 密码错；
      42=`bad_certificate` ≈ SNI 拒绝）
    - `IncompleteReadError`：`server closed during TLS handshake (read N of M bytes header)` / `...record body (ct=0x16, got 5/10)`
    - `TimeoutError` 且未见 CCS：`server TLS handshake stalled (no CCS within 12s)`
    - flight 收完但缺 SH/CCS/0x17：`incomplete TLS handshake flight (ServerHello=..., CCS=..., EncryptedRecords=...)`
  - 限制声明（写在 docstring）：服务端 token 验证失败时回放真实 apple.com 握手
    与"接受"模式字节流**完全相同**，客户端无法从握手 flight 区分两种模式 ——
    这是协议本身的隐蔽性目标，本修复不破坏
- 资源安全验证：失败路径 `_build_one_inner` 的 `except BaseException`
  仍正确释放 `server_writer.close()`，无 fd 泄漏

---

## [0.4.2] - 2026-06-04

### 修复

- **`core/router.py:_extract_literal` 改用 `sre_parse` 解析正则**：老实现用
  "去转义 + 替换非字面字符为空格 + split 取最长"的暴力法，对正则语义一无所知，
  两个**真 bug**：
  - **Alternation `|`**：`^(google\.com|youtube\.com)$` 老代码提取出
    `youtube.com`，prefilter `not in host` 把所有 google.com 请求错拦
  - **转义元字符 `\d \s \w`**：`google\.com\d+` 老代码 unescape 把 `\d` 变成
    字面 `d`、提取出 `google.comd`，把 google.com123 全错拦
  - 新实现走 `re._parser` (3.11+) / `sre_parse` (3.9-3.10)，只在顶层 OpSeq 走，
    把连续的 LITERAL op 聚成 run，**任何**非 LITERAL op（BRANCH / IN / 重复 /
    SUBPATTERN / 锚点）打断当前 run，最长 run 通过过滤后返回
  - 正确性不变量（写在注释里）：返回的 literal `L` 必须是 pat 能匹配的任意
    字符串 `s` 的子串，否则 prefilter 会拒掉合法匹配
  - 保守取舍：不递归子组。`(google\.com)` 顶层是 SUBPATTERN，run 立即被打断、
    literal 留空、prefilter 跳过、正则引擎全跑——损失加速、**绝不可能错拦**

---

## [0.4.1] - 2026-06-04

### 安全

- **`core/hello_auth.py:TokenReplayCache` 保留桶数 2 → 3**，闭合 2T 临界
  重放窗口。
  - 老实现只保留 `{current, current-1}` 两桶，桶宽 `W = T = 60s`。最坏情况
    （当前桶刚开始）下保证留存的 nonce 最长年龄 = `W = 60s`，远小于需要留存的
    `2T = 120s`（同一 token 合法接受窗口跨度）
  - 临界场景：客户端 ts=60，首次在 t=59 被接受存入 bucket 0；t=120 重放时
    bucket=2、容差校验通过（`|120-60|=60 > 60` 不成立），清理删了 bucket 0，
    查 (2,1) 找不到 → **重放放行**
  - 修复：留存 `⌈2T/W⌉ + 1 = 3` 桶。在 elapsed=0 最坏点，bucket-2 内项目的
    年龄区间是 `[W, 2W) = [60s, 120s)`，恰好覆盖到 2T 整点
  - 数学正确性 + 6 个临界测试都在类 docstring 与测试输出中详细记录
  - 内存代价：1000 req/s 假设下从 ~480KB 增至 ~1.4MB，仍可忽略

---

## [0.4.0] - 2026-06-04

### 新增

**多节点 + 自适应选路（sing-box 风格的 outbounds + route）。**
这一版是一次完整的客户端架构升级，主要面向"多 VPS + 流媒体走美 / 国内直连 /
其余自动"这类粒度的路由需求。

- **`core/outbound.py`**：Outbound 抽象基类 + MirageOutbound / DirectOutbound /
  BlockOutbound 三种叶子节点 + `build_outbounds(cfg)` 工厂
  - MirageOutbound 独占一个 BrutalPool；BrutalPool 每次 build 的握手耗时
    回调进延迟样本窗口（deque maxlen=10），urltest 决策取 median 抗抖
- **`core/group.py`**：UrlTestGroup（tolerance 防抖避免高频切换） +
  FallbackGroup（顺序选第一个 healthy）；嵌套组通过启动期 fixpoint 求解，
  循环引用立即报错退出
- **`core/healthcheck.py`**：每 60s 扫一次 mirage 节点，对 last_sample_time
  > 5min 的节点主动 probe；正常有流量时被动样本就够新鲜，本模块只兜底
- **结构化 rules**（sing-box 风格）：`route.rules` 数组每条对象支持 `domain` /
  `domain_suffix` / `domain_keyword` / `domain_regex` / `ip_cidr` / `rule_set` /
  `geoip` / `invert` 字段，`outbound` 填任意已定义 tag。数组值 = OR，单 rule
  对象只允许一个 criterion 字段（避免与 sing-box 的 AND 语义混淆）
- **`route.final`** / 顶层 `final` 字段：sing-box 风格的默认动作配置

### 修改

- **`core/conn_pool.py:BrutalPool`** 重构：构造函数接收 `node_cfg`（per-outbound
  参数子集）而不是全局 cfg；新加 `on_latency` / `on_failure` 回调通知调用方
  握手耗时与失败；新加 `probe_once()` 方法供 HealthCheck 主动探测
- **`core/dns_forwarder.py`** 改用 outbound 决策：按 router 命中的 leaf 类型
  分流——`direct` 走 UDP 直查 cn_dns、`block` 返回 NXDOMAIN、`mirage` 走该
  outbound 独占的 DoT pipeline（每个 mirage 叶子单独持有 `_DnsTunnel` 实例，
  懒建）
- **`core/router.py:build_router`** 同时支持结构化 rules 与 CSV rules，自动
  按 `cfg["route"]` 存在与否 + `cfg["rules"]` 首元素类型判别
- **`client.py:main()`** 重构：加载 outbounds 字典 → 全部 mirage 并行
  warmup → 选首个 ready 的池给 geo 下载用 → 构建 router → 启动 HealthCheck
  → 启动 SOCKS5 / DNS / TProxy 服务器
- **`config_client.json`** 示例改为 sing-box 风格（outbounds + route block）

### 迁移

- **老 `server_host` 顶层单节点配置仍工作**：`build_outbounds` 检测到没有
  `outbounds` 数组但有 `server_host` 时自动合成单 mirage outbound（tag=`proxy`）
- **老 CSV `rules` 仍工作**：`PROXY` / `DIRECT` / `REJECT` 关键字自动映射到
  `proxy` / `direct` / `block`；其他名（如自定义 tag）原样保留 case 查 outbound
- 自动补全 `direct` / `block` 叶子：即便用户没在 `outbounds` 显式声明，启动
  期也会补出，规则可直接引用

### 内部

- `core.router.build_router` 参数名从 `valid_actions` 保留（中性），适配
  client（outbound tag 集）和 server（egress 名集）两类调用方
- 修复：之前一轮重构把参数名改成 `valid_outbounds` 让 `server.py:315` 启动
  崩，**已回滚到原 `valid_actions`**

---

## [0.3.2] - 2026-06 (历史)

### 新增

- **服务端 WireGuard 出口（WARP 等）**：`core/egress.py` 新加
  DefaultEgress / MarkedEgress；后者用 SO_MARK 触发 Linux 策略路由把流量
  送进 WG 接口。启动期 `probe()` 检查 CAP_NET_ADMIN 可用性，不可用直接
  `sys.exit(1)`、不静默回落（避免用户以为 WARP 在用结果服务端 IP 直接被 Netflix 拒）
- `setup.py` 加入 `configure_warp_egress()` 向导：询问 fwmark / table、
  选预置 tag（Netflix / Disney+ / OpenAI / Spotify / YouTube / Google）、
  生成 `setup-warp.sh` 一键脚本（含 wgcf 注册、wg-quick 配置生成、
  `ip rule` + `ip route` 命令）
- 服务端 anti-DoS：`_IDLE_TIMEOUT_SEC=1800` 空闲超时、`_MAX_CONNS_PER_IP=100`
  per-IP 连接上限、`_TCP_KEEPALIVE=True`

### 修复

- `MarkedEgress` 强制 `getaddrinfo(family=AF_INET)`：v6 sockaddr 被 SO_MARK
  后内核找不到 v6 策略路由（setup-warp.sh 只配 v4 表），强制 v4 让 v6-only
  目标显式 gaierror 失败、避免静默走错路由
- `core/admin.py` 加 CSRF（Origin header 校验）+ `hmac.compare_digest` 做
  timing-safe token 比较

---

## [0.3.1] - 2026-06 (历史)

### 修复

- **DNS pipeline tx_id 防覆盖**：`_DnsTunnel._allocate_tx()` 改用 bounded loop
  分配 16-bit 不与 `_pending` 冲突的 id，跳过 0；理论上 MAX_INFLIGHT=64
  远不会塞满 65535 空间，但防御性写法可避免长时间高并发下覆盖旧 future
  导致响应错配 / 悬挂
- **连接池 warmup / refill 真阶梯抖动**：相邻条建连接的延迟改成
  `i × 0.30s ± 0.08s`（累计阶梯），不是各自独立的 `uniform(0.1, 0.5)`
  （实测旧实现 10 条仍全在 0.5s 内集中爆发，没真阶梯化）。同时改进 idle
  jitter 让每条 tunnel 单独过期、避免一批同时齐刷刷重建

---

## [0.3.0] - 2026-06 (历史)

### 修改

- **TLS 1.3 inner content type 完整模拟（Option A，协议破坏性更改）**：
  握手后所有加密记录外层一律 `0x17`，真实 content type 放进 plaintext 末尾，
  与 RFC 8446 §5.2 完全一致
  - `core/tunnel.py:send()` —— 明文末尾追加 `0x17`（application_data）后送 AEAD
  - `core/tunnel.py:recv()` —— 解密后剥末字节，按 inner type 分发：
    `0x17` 返回 body / `0x15` 抛 `EOFError("peer sent TLS alert")` /
    其他抛 `ValueError`
  - `core/tunnel.py:send_close_notify()` —— 发与真实 TLS 1.3 close_notify
    逐字节一致的 alert：外层 `0x17 0x03 0x03 0x00 0x13 [19B 密文]`，
    plaintext = `0x01 0x00 0x15`
- Android `ProxyTunnel.kt` 镜像 send / recv / sendCloseNotify 改动

### 迁移

- **协议破坏性**：新客户端与旧服务端、旧客户端与新服务端，握手能成但首条
  数据帧解密会失败（解出来的明文末字节不是 inner type 而是真实数据）。
  **必须同时升级两端**。"个人测试"项目用户已同意此破坏性

---

## [0.x.x] - 更早 (历史)

更早的历史在 git log 里；从 `0.3.0` 起按本规范打 tag。

[未发布]: https://github.com/<你的仓库>/compare/v0.4.40...HEAD
[0.4.40]: https://github.com/<你的仓库>/releases/tag/v0.4.40
[0.4.39]: https://github.com/<你的仓库>/releases/tag/v0.4.39
[0.4.38]: https://github.com/<你的仓库>/releases/tag/v0.4.38
[0.4.37]: https://github.com/<你的仓库>/releases/tag/v0.4.37
[0.4.36]: https://github.com/<你的仓库>/releases/tag/v0.4.36
[0.4.35]: https://github.com/<你的仓库>/releases/tag/v0.4.35
[0.4.34]: https://github.com/<你的仓库>/releases/tag/v0.4.34
[0.4.33]: https://github.com/<你的仓库>/releases/tag/v0.4.33
[0.4.32]: https://github.com/<你的仓库>/releases/tag/v0.4.32
[0.4.31]: https://github.com/<你的仓库>/releases/tag/v0.4.31
[0.4.30]: https://github.com/<你的仓库>/releases/tag/v0.4.30
[0.4.29]: https://github.com/<你的仓库>/releases/tag/v0.4.29
[0.4.28]: https://github.com/<你的仓库>/releases/tag/v0.4.28
[0.4.27]: https://github.com/<你的仓库>/releases/tag/v0.4.27
[0.4.26]: https://github.com/<你的仓库>/releases/tag/v0.4.26
[0.4.25]: https://github.com/<你的仓库>/releases/tag/v0.4.25
[0.4.24]: https://github.com/<你的仓库>/releases/tag/v0.4.24
[0.4.23]: https://github.com/<你的仓库>/releases/tag/v0.4.23
[0.4.22]: https://github.com/<你的仓库>/releases/tag/v0.4.22
[0.4.21]: https://github.com/<你的仓库>/releases/tag/v0.4.21
[0.4.20]: https://github.com/<你的仓库>/releases/tag/v0.4.20
[0.4.19]: https://github.com/<你的仓库>/releases/tag/v0.4.19
[0.4.18]: https://github.com/<你的仓库>/releases/tag/v0.4.18
[0.4.17]: https://github.com/<你的仓库>/releases/tag/v0.4.17
[0.4.16]: https://github.com/<你的仓库>/releases/tag/v0.4.16
[0.4.15]: https://github.com/<你的仓库>/releases/tag/v0.4.15
[0.4.14]: https://github.com/<你的仓库>/releases/tag/v0.4.14
[0.4.13]: https://github.com/<你的仓库>/releases/tag/v0.4.13
[0.4.12]: https://github.com/<你的仓库>/releases/tag/v0.4.12
[0.4.11]: https://github.com/<你的仓库>/releases/tag/v0.4.11
[0.4.10]: https://github.com/<你的仓库>/releases/tag/v0.4.10
[0.4.9]: https://github.com/<你的仓库>/releases/tag/v0.4.9
[0.4.8]: https://github.com/<你的仓库>/releases/tag/v0.4.8
[0.4.7]: https://github.com/<你的仓库>/releases/tag/v0.4.7
[0.4.6]: https://github.com/<你的仓库>/releases/tag/v0.4.6
[0.4.5]: https://github.com/<你的仓库>/releases/tag/v0.4.5
[0.4.4]: https://github.com/<你的仓库>/releases/tag/v0.4.4
[0.4.3]: https://github.com/<你的仓库>/releases/tag/v0.4.3
[0.4.2]: https://github.com/<你的仓库>/releases/tag/v0.4.2
[0.4.1]: https://github.com/<你的仓库>/releases/tag/v0.4.1
[0.4.0]: https://github.com/<你的仓库>/releases/tag/v0.4.0
[0.3.2]: https://github.com/<你的仓库>/releases/tag/v0.3.2
[0.3.1]: https://github.com/<你的仓库>/releases/tag/v0.3.1
[0.3.0]: https://github.com/<你的仓库>/releases/tag/v0.3.0
