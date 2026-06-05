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
  - `core/outbound.py:_bidi_tunnel_relay()` —— `PyrealiyOutbound` 隧道路径，
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

- **`core/outbound.py`**：Outbound 抽象基类 + PyrealiyOutbound / DirectOutbound /
  BlockOutbound 三种叶子节点 + `build_outbounds(cfg)` 工厂
  - PyrealiyOutbound 独占一个 BrutalPool；BrutalPool 每次 build 的握手耗时
    回调进延迟样本窗口（deque maxlen=10），urltest 决策取 median 抗抖
- **`core/group.py`**：UrlTestGroup（tolerance 防抖避免高频切换） +
  FallbackGroup（顺序选第一个 healthy）；嵌套组通过启动期 fixpoint 求解，
  循环引用立即报错退出
- **`core/healthcheck.py`**：每 60s 扫一次 pyrealiy 节点，对 last_sample_time
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
  分流——`direct` 走 UDP 直查 cn_dns、`block` 返回 NXDOMAIN、`pyrealiy` 走该
  outbound 独占的 DoT pipeline（每个 pyrealiy 叶子单独持有 `_DnsTunnel` 实例，
  懒建）
- **`core/router.py:build_router`** 同时支持结构化 rules 与 CSV rules，自动
  按 `cfg["route"]` 存在与否 + `cfg["rules"]` 首元素类型判别
- **`client.py:main()`** 重构：加载 outbounds 字典 → 全部 pyrealiy 并行
  warmup → 选首个 ready 的池给 geo 下载用 → 构建 router → 启动 HealthCheck
  → 启动 SOCKS5 / DNS / TProxy 服务器
- **`config_client.json`** 示例改为 sing-box 风格（outbounds + route block）

### 迁移

- **老 `server_host` 顶层单节点配置仍工作**：`build_outbounds` 检测到没有
  `outbounds` 数组但有 `server_host` 时自动合成单 pyrealiy outbound（tag=`proxy`）
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

[未发布]: https://github.com/<你的仓库>/compare/v0.4.7...HEAD
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
