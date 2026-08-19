# Vendored 同步记录

- 上游仓库: /opt/Mirage-rs
- 上游 commit: `af6af4c21b02b2c58b49eae2e9cfaf54cf7ff8b3`
- 同步时间: 2026-08-19T22:17:30+08:00

## 同步后必须做的事
1. 检查 $DEST 里是否有对**未 vendored 模块**的引用 (`crate::api` / `crate::ebpf` /
   `crate::config_watcher` / `crate::net_monitor` / `crate::monitor` / `crate::startup` /
   `crate::proxy::wg` / `crate::proxy::shadowsocks` / `crate::proxy::splice` 等) ——
   这些是移动端裁剪掉的模块, 出现引用说明裁剪边界需要调整。
2. 检查移动端补丁是否仍适用 (搜索 `MIRAGE_MOBILE` 标记)。
3. `cd native && cargo check` 过一遍。

## 移动端补丁清单 (在 vendored 副本中, 上游没有)
- `proxy/brutal.rs`: `#[cfg(target_os = "android")] const SO_COOKIE = 67` (bionic 内核支持,
  libc crate 未对 android target 暴露该常量)
