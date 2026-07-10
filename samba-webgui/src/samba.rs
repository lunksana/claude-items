//! Samba 配置与用户管理：共享写入独立 include 文件，用户走 pdbedit/smbpasswd。

use serde::{Deserialize, Serialize};
use std::path::Path;
use tokio::process::Command;

pub const SMB_CONF: &str = "/etc/samba/smb.conf";
pub const MANAGED_CONF: &str = "/etc/samba/webgui-shares.conf";
/// 每个共享一个片段文件的目录；MANAGED_CONF 退化为只含 include 行的聚合文件
pub const MANAGED_DIR: &str = "/etc/samba/webgui-shares.d";
/// 单级配置备份目录：每次改配置前快照，供"还原上次配置"使用
pub const BACKUP_DIR: &str = "/etc/samba/webgui-backup";

/// 串行化共享配置的读-改-写，防并发覆盖
pub static CONF_LOCK: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Share {
    pub name: String,
    pub path: String,
    #[serde(default)]
    pub comment: String,
    #[serde(default)]
    pub read_only: bool,
    #[serde(default)]
    pub guest_ok: bool,
    #[serde(default = "default_true")]
    pub browseable: bool,
    /// 允许访问的用户列表，空 = 不限制
    #[serde(default)]
    pub valid_users: String,
    /// 只读共享中仍可写的用户列表
    #[serde(default)]
    pub write_list: String,
    /// 强制只读的用户列表（可写共享中把特定用户限制为只读）
    #[serde(default)]
    pub read_list: String,
    /// 是否开启网络回收站 (.recycle)。前端字段名为 recycle
    #[serde(default, rename = "recycle")]
    pub recycle_bin: bool,
    /// 是否开启 macOS/Time Machine 兼容。前端字段名为 fruit
    #[serde(default, rename = "fruit")]
    pub fruit_time_machine: bool,
    /// 是否由本工具管理（false = 来自主配置，只读展示）
    #[serde(default)]
    pub managed: bool,
    /// 保存时是否自动修正目录属主/权限使写入生效（仅请求参数，不落盘）
    #[serde(default, skip_serializing)]
    pub fix_perms: bool,
}

fn default_true() -> bool {
    true
}

#[derive(Debug, Clone, Serialize)]
pub struct SmbUser {
    pub username: String,
    pub uid: String,
    pub disabled: bool,
    pub groups: Vec<String>,
}

pub fn valid_section_name(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 64
        && !s.eq_ignore_ascii_case("global")
        && !has_control_char(s)
        && !s.contains('[')
        && !s.contains(']')
        && !s.contains('=')
        && !s.contains('#')
        && !s.contains(';')
        && !s.starts_with(' ')
        && !s.ends_with(' ')
}

/// 新建用户的严格规则
pub fn valid_username(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 32
        && s.chars().next().map_or(false, |c| c.is_ascii_lowercase() || c == '_')
        && s.chars()
            .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || matches!(c, '-' | '_'))
}

/// 操作已存在用户的宽松规则：允许大写与 $ 结尾（机器账号），仍排除注入字符
pub fn valid_existing_username(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 32
        && !s.starts_with('-')
        && s.chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.' | '$'))
}

/// 解析 ini 风格配置为 (section, [(key, value)]) 列表
fn parse_ini(text: &str) -> Vec<(String, Vec<(String, String)>)> {
    let mut sections = Vec::new();
    let mut cur: Option<(String, Vec<(String, String)>)> = None;
    for line in text.lines() {
        let line = line.trim();
        if line.is_empty() || line.starts_with('#') || line.starts_with(';') {
            continue;
        }
        if line.starts_with('[') && line.ends_with(']') {
            if let Some(s) = cur.take() {
                sections.push(s);
            }
            cur = Some((line[1..line.len() - 1].trim().to_string(), Vec::new()));
        } else if let Some((k, v)) = line.split_once('=') {
            if let Some((_, kvs)) = cur.as_mut() {
                kvs.push((k.trim().to_lowercase(), v.trim().to_string()));
            }
        }
    }
    if let Some(s) = cur {
        sections.push(s);
    }
    sections
}

fn get<'a>(kvs: &'a [(String, String)], key: &str) -> Option<&'a str> {
    kvs.iter().find(|(k, _)| k == key).map(|(_, v)| v.as_str())
}

fn as_bool(v: Option<&str>, default: bool) -> bool {
    match v.map(|s| s.to_lowercase()) {
        Some(s) => matches!(s.as_str(), "yes" | "true" | "1"),
        None => default,
    }
}

fn section_to_share(name: &str, kvs: &[(String, String)], managed: bool) -> Share {
    // samba 里 "writable/writeable" 是 "read only" 的反义别名
    let read_only = if let Some(w) = get(kvs, "writable").or(get(kvs, "writeable")) {
        !matches!(w.to_lowercase().as_str(), "yes" | "true" | "1")
    } else {
        as_bool(get(kvs, "read only"), true)
    };
    let vfs = get(kvs, "vfs objects").unwrap_or("").to_lowercase();
    let recycle_bin = vfs.contains("recycle");
    let fruit_time_machine = vfs.contains("fruit") || as_bool(get(kvs, "fruit:time machine"), false);
    Share {
        name: name.to_string(),
        path: get(kvs, "path").unwrap_or("").to_string(),
        comment: get(kvs, "comment").unwrap_or("").to_string(),
        read_only,
        guest_ok: as_bool(get(kvs, "guest ok"), false),
        browseable: as_bool(get(kvs, "browseable").or(get(kvs, "browsable")), true),
        valid_users: get(kvs, "valid users").unwrap_or("").to_string(),
        write_list: get(kvs, "write list").unwrap_or("").to_string(),
        read_list: get(kvs, "read list").unwrap_or("").to_string(),
        recycle_bin,
        fruit_time_machine,
        managed,
        fix_perms: false,
    }
}

/// 启动时确保 include 文件存在且被主配置在【文件末尾】引用（首次修改前备份 smb.conf）。
///
/// smb.conf 的 include 是线性文本包含：若 include 行位于 [global] 中间，
/// include 文件里的共享段会"吞掉"其后的全局参数。因此 include 必须放在
/// 主配置最后一行；旧版本插错位置的会在这里自动迁移。
pub fn ensure_include() -> std::io::Result<()> {
    if !Path::new(MANAGED_CONF).exists() {
        std::fs::write(MANAGED_CONF, "# Managed by samba-webgui. Do not edit manually.\n")?;
    }
    std::fs::create_dir_all(MANAGED_DIR)?;
    // 旧单文件格式（聚合文件里直接写 [section]）自动拆分为每共享一个片段
    migrate_single_file_to_fragments()?;
    let conf = std::fs::read_to_string(SMB_CONF)?;
    let is_include_line = |l: &str| {
        let l = l.trim();
        l.strip_prefix("include")
            .and_then(|r| r.trim_start().strip_prefix('='))
            // 容忍行尾注释：include = /path  # ...
            .map(|v| v.split('#').next().unwrap_or("").trim() == MANAGED_CONF)
            .unwrap_or(false)
    };
    let lines: Vec<&str> = conf.lines().collect();
    // 已在末尾（其后只有空行/注释）则无需处理
    let last_meaningful = lines
        .iter()
        .rposition(|l| !l.trim().is_empty() && !l.trim().starts_with('#') && !l.trim().starts_with(';'));
    if last_meaningful.map_or(false, |i| is_include_line(lines[i])) {
        return Ok(());
    }
    let bak = format!("{SMB_CONF}.webgui.bak");
    if !Path::new(&bak).exists() {
        std::fs::copy(SMB_CONF, &bak)?;
    }
    let mut out: String = lines
        .iter()
        .filter(|l| !is_include_line(l))
        .map(|l| format!("{l}\n"))
        .collect();
    if !out.ends_with("\n\n") {
        out.push('\n');
    }
    out.push_str(&format!("# samba-webgui 托管共享（include 必须位于文件末尾）\ninclude = {MANAGED_CONF}\n"));
    std::fs::write(SMB_CONF, out)
}

/// 读取所有片段文件，解析出托管共享（片段内的 [section] 为权威名字，与文件名无关）。
/// 兼容旧格式：若聚合文件里还残留 [section]（尚未迁移），也一并解析。
pub fn load_managed() -> Vec<Share> {
    let mut shares: Vec<Share> = Vec::new();
    let mut seen = std::collections::HashSet::new();
    if let Ok(rd) = std::fs::read_dir(MANAGED_DIR) {
        let mut files: Vec<_> = rd
            .filter_map(|e| e.ok().map(|e| e.path()))
            .filter(|p| p.extension().map_or(false, |x| x == "conf"))
            .collect();
        files.sort();
        for path in files {
            let text = std::fs::read_to_string(&path).unwrap_or_default();
            for (name, kvs) in parse_ini(&text) {
                if seen.insert(name.clone()) {
                    shares.push(section_to_share(&name, &kvs, true));
                }
            }
        }
    }
    // 旧格式兜底：聚合文件里若仍有 [section]
    let agg = std::fs::read_to_string(MANAGED_CONF).unwrap_or_default();
    for (name, kvs) in parse_ini(&agg) {
        if seen.insert(name.clone()) {
            shares.push(section_to_share(&name, &kvs, true));
        }
    }
    shares.sort_by(|a, b| a.name.to_lowercase().cmp(&b.name.to_lowercase()));
    shares
}

/// 渲染单个共享的片段文件内容（含 [section] 头）
fn render_fragment(s: &Share) -> String {
    let mut out = String::from("# Managed by samba-webgui. Do not edit manually.\n");
    out.push_str(&format!("[{}]\n", s.name));
    out.push_str(&format!("   path = {}\n", s.path));
    if !s.comment.is_empty() {
        out.push_str(&format!("   comment = {}\n", s.comment));
    }
    out.push_str(&format!("   read only = {}\n", if s.read_only { "yes" } else { "no" }));
    out.push_str(&format!("   guest ok = {}\n", if s.guest_ok { "yes" } else { "no" }));
    out.push_str(&format!("   browseable = {}\n", if s.browseable { "yes" } else { "no" }));
    if !s.valid_users.trim().is_empty() {
        out.push_str(&format!("   valid users = {}\n", s.valid_users.trim()));
    }
    if !s.write_list.trim().is_empty() {
        out.push_str(&format!("   write list = {}\n", s.write_list.trim()));
    }
    if !s.read_list.trim().is_empty() {
        out.push_str(&format!("   read list = {}\n", s.read_list.trim()));
    }
    // inherit acls: SMB 客户端新建的文件继承目录的默认 ACL
    out.push_str("   create mask = 0664\n   directory mask = 0775\n   inherit acls = yes\n");
    if s.recycle_bin || s.fruit_time_machine {
        let mut vfs_list = Vec::new();
        if s.recycle_bin {
            vfs_list.push("recycle");
        }
        if s.fruit_time_machine {
            vfs_list.push("fruit");
            vfs_list.push("streams_xattr");
        }
        out.push_str(&format!("   vfs objects = {}\n", vfs_list.join(" ")));
        if s.recycle_bin {
            out.push_str("   recycle:repository = .recycle\n   recycle:keeptree = yes\n   recycle:versions = yes\n");
        }
        if s.fruit_time_machine {
            out.push_str("   fruit:time machine = yes\n");
        }
    }
    out
}

/// 由共享名派生安全的片段文件名（含 .conf）。
/// valid_section_name 已禁止 [ ] = # ; 与控制字符；这里再挡掉路径分隔符/前导点/空格。
fn fragment_filename(name: &str) -> String {
    let mut f: String = name
        .chars()
        .map(|c| if c == '/' || c == '\\' || c == ' ' || c.is_control() { '_' } else { c })
        .collect();
    while f.starts_with('.') {
        f.replace_range(0..1, "_");
    }
    if f.is_empty() {
        f.push('_');
    }
    format!("{f}.conf")
}

/// 在 used 集合内取一个唯一的片段文件名（冲突追加 -2 -3 …）
fn unique_fragment_filename(name: &str, used: &mut std::collections::HashSet<String>) -> String {
    let mut fname = fragment_filename(name);
    if !used.insert(fname.clone()) {
        let stem = fname.trim_end_matches(".conf").to_string();
        let mut i = 2;
        loop {
            fname = format!("{stem}-{i}.conf");
            if used.insert(fname.clone()) {
                break;
            }
            i += 1;
        }
    }
    fname
}

/// 迁移：把旧的单文件 webgui-shares.conf 里的 [section] 拆成每共享一个片段，
/// 并把聚合文件改写成只含 include 行。幂等（聚合文件无 section 时直接返回）。
fn migrate_single_file_to_fragments() -> std::io::Result<()> {
    let agg = std::fs::read_to_string(MANAGED_CONF).unwrap_or_default();
    let sections = parse_ini(&agg);
    if sections.is_empty() {
        return Ok(());
    }
    std::fs::create_dir_all(MANAGED_DIR)?;
    let mut used = std::collections::HashSet::new();
    let mut agg_out = String::from(
        "# Managed by samba-webgui. Do not edit manually.\n# 每个共享一个片段文件，见 webgui-shares.d/\n",
    );
    for (name, kvs) in &sections {
        let share = section_to_share(name, kvs, true);
        let fname = unique_fragment_filename(name, &mut used);
        let path = std::path::Path::new(MANAGED_DIR).join(&fname);
        std::fs::write(&path, render_fragment(&share))?;
        agg_out.push_str(&format!("include = {}\n", path.display()));
    }
    std::fs::write(MANAGED_CONF, agg_out)
}

/// 读取目录下所有片段文件为 路径→内容 快照（用于失败回滚）
fn snapshot_fragments() -> Vec<(std::path::PathBuf, String)> {
    let mut snap = Vec::new();
    if let Ok(rd) = std::fs::read_dir(MANAGED_DIR) {
        for p in rd.filter_map(|e| e.ok().map(|e| e.path())) {
            if p.extension().map_or(false, |x| x == "conf") {
                if let Ok(c) = std::fs::read_to_string(&p) {
                    snap.push((p, c));
                }
            }
        }
    }
    snap
}

/// 控制字符（含 \r \n \t \0 等）会被 smb.conf 解析器当作换行/分隔，
/// 借此可注入任意配置指令，必须一律拒绝
fn has_control_char(s: &str) -> bool {
    s.chars().any(|c| c.is_control())
}

/// 写入托管共享文件：testparm 校验失败则回滚，成功后热加载 smbd
pub async fn save_managed(shares: &[Share]) -> Result<String, String> {
    for s in shares {
        if !valid_section_name(&s.name) {
            return Err(format!("非法共享名: {}", s.name));
        }
        if s.path.trim().is_empty() || !s.path.starts_with('/') || has_control_char(&s.path) {
            return Err(format!("共享 {} 的路径必须是绝对路径", s.name));
        }
        if has_control_char(&s.comment)
            || has_control_char(&s.valid_users)
            || has_control_char(&s.write_list)
            || has_control_char(&s.read_list)
        {
            return Err("字段中不允许控制字符".into());
        }
    }
    std::fs::create_dir_all(MANAGED_DIR).map_err(|e| e.to_string())?;
    // 快照当前片段 + 聚合文件，供 testparm 失败时回滚
    let snap = snapshot_fragments();
    let agg_old = std::fs::read_to_string(MANAGED_CONF).unwrap_or_default();

    // 计算期望的片段文件集（文件名去重）与聚合 include 列表
    let mut used = std::collections::HashSet::new();
    let mut desired: Vec<(std::path::PathBuf, String)> = Vec::new();
    let mut agg = String::from(
        "# Managed by samba-webgui. Do not edit manually.\n# 每个共享一个片段文件，见 webgui-shares.d/\n",
    );
    for s in shares {
        let fname = unique_fragment_filename(&s.name, &mut used);
        let path = std::path::Path::new(MANAGED_DIR).join(&fname);
        agg.push_str(&format!("include = {}\n", path.display()));
        desired.push((path, render_fragment(s)));
    }

    // 应用：写期望片段 → 删除多余旧片段 → 写聚合文件
    let apply = || -> std::io::Result<()> {
        for (path, content) in &desired {
            std::fs::write(path, content)?;
        }
        let keep: std::collections::HashSet<_> = desired.iter().map(|(p, _)| p.clone()).collect();
        for (path, _) in &snap {
            if !keep.contains(path) {
                let _ = std::fs::remove_file(path);
            }
        }
        std::fs::write(MANAGED_CONF, &agg)
    };
    apply().map_err(|e| e.to_string())?;

    let check = Command::new("testparm")
        .args(["-s", "--suppress-prompt", SMB_CONF])
        .output()
        .await
        .map_err(|e| e.to_string())?;
    if !check.status.success() {
        // 回滚：清空目录内片段 → 恢复快照 → 恢复聚合文件
        if let Ok(rd) = std::fs::read_dir(MANAGED_DIR) {
            for p in rd.filter_map(|e| e.ok().map(|e| e.path())) {
                if p.extension().map_or(false, |x| x == "conf") {
                    let _ = std::fs::remove_file(p);
                }
            }
        }
        for (path, content) in &snap {
            let _ = std::fs::write(path, content);
        }
        let _ = std::fs::write(MANAGED_CONF, agg_old);
        return Err(format!(
            "配置校验失败，已回滚: {}",
            String::from_utf8_lossy(&check.stderr).trim()
        ));
    }
    invalidate_share_cache();
    Ok(reload_smbd().await)
}

/// 支持的 SMB 协议版本 token（server min/max protocol 的取值）
pub const VALID_PROTOCOLS: &[&str] =
    &["NT1", "SMB2", "SMB2_02", "SMB2_10", "SMB3", "SMB3_00", "SMB3_02", "SMB3_11"];

pub fn valid_protocol(s: &str) -> bool {
    VALID_PROTOCOLS.iter().any(|p| p.eq_ignore_ascii_case(s))
}

/// 读取主配置 [global] 段中某参数的值（大小写不敏感），不存在返回 None
pub fn read_global_param(key: &str) -> Option<String> {
    let conf = std::fs::read_to_string(SMB_CONF).ok()?;
    let mut in_global = false;
    for line in conf.lines() {
        let t = line.trim();
        if t.starts_with('[') && t.ends_with(']') {
            in_global = t[1..t.len() - 1].trim().eq_ignore_ascii_case("global");
            continue;
        }
        if in_global {
            if let Some((k, v)) = t.split_once('=') {
                if k.trim().eq_ignore_ascii_case(key) {
                    return Some(v.trim().to_string());
                }
            }
        }
    }
    None
}

/// 在 [global] 段内更新/插入/删除若干参数并写回主配置。
/// value = Some → 设置；value = None → 删除该行。返回改写后的文本。
fn apply_global_params(conf: &str, pairs: &[(&str, Option<String>)]) -> String {
    let matches = |lhs: &str| pairs.iter().find(|(k, _)| lhs.trim().eq_ignore_ascii_case(k));
    let mut out: Vec<String> = Vec::new();
    let mut in_global = false;
    let mut handled: std::collections::HashSet<&str> = std::collections::HashSet::new();
    // 离开 [global] 前，把还没出现过的 Some 参数补插进去
    let flush_missing = |out: &mut Vec<String>, handled: &std::collections::HashSet<&str>| {
        for (k, v) in pairs {
            if let Some(val) = v {
                if !handled.contains(k) {
                    out.push(format!("   {k} = {val}"));
                }
            }
        }
    };
    for line in conf.lines() {
        let t = line.trim();
        if t.starts_with('[') && t.ends_with(']') {
            if in_global {
                flush_missing(&mut out, &handled);
            }
            in_global = t[1..t.len() - 1].trim().eq_ignore_ascii_case("global");
            out.push(line.to_string());
            continue;
        }
        if in_global {
            if let Some((lhs, _)) = t.split_once('=') {
                if let Some((k, v)) = matches(lhs) {
                    // 仅首次出现写入替换值；后续重复同名行直接剔除（顺带清洗历史冗余）
                    if handled.insert(k) {
                        if let Some(val) = v {
                            out.push(format!("   {k} = {val}"));
                        }
                    }
                    // None：删除该行（不 push）
                    continue;
                }
            }
        }
        out.push(line.to_string());
    }
    if in_global {
        flush_missing(&mut out, &handled);
    }
    let mut s = out.join("\n");
    s.push('\n');
    s
}

/// 写入 [global] 参数：testparm 校验失败则回滚，成功后热加载
pub async fn set_global_params(pairs: &[(&str, Option<String>)]) -> Result<String, String> {
    for (_, v) in pairs {
        if let Some(val) = v {
            if has_control_char(val) {
                return Err("参数值中不允许控制字符".into());
            }
        }
    }
    let conf = std::fs::read_to_string(SMB_CONF).map_err(|e| e.to_string())?;
    let new_conf = apply_global_params(&conf, pairs);
    std::fs::write(SMB_CONF, &new_conf).map_err(|e| e.to_string())?;
    let check = Command::new("testparm")
        .args(["-s", "--suppress-prompt", SMB_CONF])
        .output()
        .await
        .map_err(|e| e.to_string())?;
    if !check.status.success() {
        let _ = std::fs::write(SMB_CONF, conf);
        return Err(format!(
            "配置校验失败，已回滚: {}",
            String::from_utf8_lossy(&check.stderr).trim()
        ));
    }
    Ok(reload_smbd().await)
}

/// 改配置前调用：把当前 smb.conf + 聚合文件 + 全部片段快照到备份目录（单级，覆盖旧备份）。
/// 尽力而为——失败不阻断编辑，只是届时无备份可还原。
pub fn backup_config() {
    let bdir = Path::new(BACKUP_DIR);
    let bshares = bdir.join("shares.d");
    // 清空旧片段备份
    if let Ok(rd) = std::fs::read_dir(&bshares) {
        for e in rd.flatten() {
            let _ = std::fs::remove_file(e.path());
        }
    }
    if std::fs::create_dir_all(&bshares).is_err() {
        return;
    }
    let _ = std::fs::copy(SMB_CONF, bdir.join("smb.conf"));
    let _ = std::fs::copy(MANAGED_CONF, bdir.join("webgui-shares.conf"));
    if let Ok(rd) = std::fs::read_dir(MANAGED_DIR) {
        for e in rd.flatten() {
            let p = e.path();
            if p.extension().map_or(false, |x| x == "conf") {
                if let Some(name) = p.file_name() {
                    let _ = std::fs::copy(&p, bshares.join(name));
                }
            }
        }
    }
    let ts = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let _ = std::fs::write(bdir.join("timestamp"), ts.to_string());
}

/// 备份时间戳（unix 秒），无备份返回 None
pub fn backup_timestamp() -> Option<u64> {
    std::fs::read_to_string(Path::new(BACKUP_DIR).join("timestamp"))
        .ok()
        .and_then(|s| s.trim().parse().ok())
}

/// 还原到上次备份的配置：恢复 smb.conf + 聚合文件 + 片段，然后热加载。
pub async fn restore_config() -> Result<String, String> {
    let bdir = Path::new(BACKUP_DIR);
    if backup_timestamp().is_none() {
        return Err("没有可还原的配置备份（尚未进行过配置修改）".into());
    }
    let bsmb = bdir.join("smb.conf");
    if bsmb.exists() {
        std::fs::copy(&bsmb, SMB_CONF).map_err(|e| e.to_string())?;
    }
    let bagg = bdir.join("webgui-shares.conf");
    if bagg.exists() {
        std::fs::copy(&bagg, MANAGED_CONF).map_err(|e| e.to_string())?;
    }
    std::fs::create_dir_all(MANAGED_DIR).map_err(|e| e.to_string())?;
    // 清空当前片段后从备份恢复
    if let Ok(rd) = std::fs::read_dir(MANAGED_DIR) {
        for e in rd.flatten() {
            let p = e.path();
            if p.extension().map_or(false, |x| x == "conf") {
                let _ = std::fs::remove_file(p);
            }
        }
    }
    if let Ok(rd) = std::fs::read_dir(bdir.join("shares.d")) {
        for e in rd.flatten() {
            let p = e.path();
            if let Some(name) = p.file_name() {
                let _ = std::fs::copy(&p, Path::new(MANAGED_DIR).join(name));
            }
        }
    }
    invalidate_share_cache();
    Ok(format!("已还原到上次配置；{}", reload_smbd().await))
}

async fn reload_smbd() -> String {
    let r = Command::new("smbcontrol").args(["all", "reload-config"]).output().await;
    if matches!(&r, Ok(o) if o.status.success()) {
        return "配置已生效（热加载）".into();
    }
    for act in ["reload", "restart"] {
        let r = Command::new("systemctl").args([act, "smbd"]).output().await;
        if matches!(&r, Ok(o) if o.status.success()) {
            return format!("配置已生效（{act} smbd）");
        }
    }
    "配置已写入，但 smbd 重载失败，请手动重启服务".into()
}

/// 短 TTL 共享缓存：文件浏览的每个请求都要解析共享路径，
/// 避免每次都 fork testparm
static SHARE_CACHE: std::sync::Mutex<Option<(std::time::Instant, Vec<Share>)>> =
    std::sync::Mutex::new(None);

pub async fn list_all_shares_cached() -> Result<Vec<Share>, String> {
    if let Some((t, shares)) = SHARE_CACHE.lock().unwrap().as_ref() {
        if t.elapsed() < std::time::Duration::from_secs(3) {
            return Ok(shares.clone());
        }
    }
    let shares = list_all_shares().await?;
    *SHARE_CACHE.lock().unwrap() = Some((std::time::Instant::now(), shares.clone()));
    Ok(shares)
}

fn invalidate_share_cache() {
    *SHARE_CACHE.lock().unwrap() = None;
}

/// 所有生效共享（含主配置里的），managed 标记区分能否编辑
pub async fn list_all_shares() -> Result<Vec<Share>, String> {
    let managed: Vec<String> = load_managed().into_iter().map(|s| s.name).collect();
    let out = Command::new("testparm")
        .args(["-s", "--suppress-prompt", SMB_CONF])
        .output()
        .await
        .map_err(|e| e.to_string())?;
    // testparm 把展开后的完整配置打到 stdout
    let text = String::from_utf8_lossy(&out.stdout);
    let mut shares: Vec<Share> = parse_ini(&text)
        .iter()
        .filter(|(name, _)| !name.eq_ignore_ascii_case("global"))
        .map(|(name, kvs)| {
            let managed = managed.iter().any(|m| m.eq_ignore_ascii_case(name));
            section_to_share(name, kvs, managed)
        })
        .collect();
    shares.sort_by(|a, b| a.name.to_lowercase().cmp(&b.name.to_lowercase()));
    Ok(shares)
}

/// 系统关键目录：不允许作为共享路径，更不允许被 chmod/chown 改动
const CRITICAL_PATHS: &[&str] = &[
    "/", "/etc", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/libx32", "/usr", "/boot",
    "/proc", "/sys", "/dev", "/run", "/root", "/var", "/opt/claude",
];

/// 校验共享路径是否安全：必须是已存在目录，且不是（也不在）系统关键目录内
pub fn check_share_path(path: &str) -> Result<std::path::PathBuf, String> {
    let canon = std::fs::canonicalize(path).map_err(|_| "路径不存在或不是目录".to_string())?;
    if !canon.is_dir() {
        return Err("路径不是目录".into());
    }
    for c in CRITICAL_PATHS {
        let cp = Path::new(c);
        // 命中关键目录本身，或将关键目录当作共享根（如 /etc）
        if canon == cp {
            return Err(format!("禁止将系统关键目录 {c} 设为共享/修改权限"));
        }
    }
    // 额外：不允许共享根直接是这些顶层目录的直接父级混淆
    Ok(canon)
}

/// 修正共享目录属主/权限：Samba 层放行后 Unix 层也要可写
pub async fn fix_share_perms(share: &Share) -> Result<String, String> {
    let canon = check_share_path(&share.path)?;
    let path = canon.to_string_lossy().to_string();
    let split = |s: &str| -> Vec<String> {
        s.split(&[',', ' '][..]).map(|x| x.trim().to_string()).filter(|x| !x.is_empty()).collect()
    };
    let entries: Vec<String> = split(&share.write_list)
        .into_iter()
        .chain(split(&share.valid_users))
        .chain(split(&share.read_list))
        .collect();
    // 第一个普通用户作属主
    let owner = entries
        .iter()
        .find(|s| !s.starts_with('@') && !s.starts_with('+'))
        .cloned()
        .or_else(|| share.guest_ok.then(|| "nobody".to_string()));
    // 第一个 @组 作为多用户协作组：设 setgid + 组属主，组内成员协作互通
    let group = entries
        .iter()
        .find(|s| s.starts_with('@'))
        .map(|s| s.trim_start_matches('@').to_string());

    // 有协作组时用 setgid 位（前导 2），使新建文件继承目录组
    let mode = match (&group, share.guest_ok && !share.read_only) {
        (Some(_), true) => "2777",
        (Some(_), false) => "2775",
        (None, true) => "0777",
        (None, false) => "0775",
    };
    let out = Command::new("chmod").args([mode, "--", &path]).output().await.map_err(|e| e.to_string())?;
    if !out.status.success() {
        return Err(format!("chmod 失败: {}", String::from_utf8_lossy(&out.stderr).trim()));
    }

    // chown 目标：属主[:组]
    if let Some(g) = &group {
        if !valid_existing_username(g) {
            return Err(format!("无法确定合法用户组: {g}"));
        }
    }
    if let Some(owner) = &owner {
        if !valid_username(owner) && owner != "nobody" {
            return Err(format!("无法确定合法属主: {owner}"));
        }
    }
    let spec = match (&owner, &group) {
        (Some(o), Some(g)) => format!("{o}:{g}"),
        (Some(o), None) => o.clone(),
        (None, Some(g)) => format!(":{g}"),
        (None, None) => String::new(),
    };
    if !spec.is_empty() {
        let out = Command::new("chown").args([spec.as_str(), "--", &path]).output().await.map_err(|e| e.to_string())?;
        if !out.status.success() {
            return Err(format!("chown 失败: {}", String::from_utf8_lossy(&out.stderr).trim()));
        }
    }
    Ok(match &group {
        Some(g) => format!("目录权限已修正（多用户协作：组 {g} + setgid，权限 {mode}）"),
        None => match &owner {
            Some(o) => format!("目录权限已修正（属主 {o}，权限 {mode}）"),
            None => format!("目录权限已修正（{mode}）"),
        },
    })
}

pub async fn list_users() -> Result<Vec<SmbUser>, String> {
    let out = Command::new("pdbedit")
        .args(["-L", "-v"])
        .output()
        .await
        .map_err(|e| e.to_string())?;
    if !out.status.success() {
        return Err(String::from_utf8_lossy(&out.stderr).trim().to_string());
    }
    let text = String::from_utf8_lossy(&out.stdout);
    let mut users = Vec::new();
    let mut cur: Option<SmbUser> = None;
    for line in text.lines() {
        if let Some(v) = line.strip_prefix("Unix username:") {
            if let Some(u) = cur.take() {
                users.push(u);
            }
            cur = Some(SmbUser {
                username: v.trim().to_string(),
                uid: String::new(),
                disabled: false,
                groups: Vec::new(),
            });
        } else if let Some(v) = line.strip_prefix("User SID:") {
            if let Some(u) = cur.as_mut() {
                u.uid = v.trim().rsplit('-').next().unwrap_or("").to_string();
            }
        } else if let Some(v) = line.strip_prefix("Account Flags:") {
            if let Some(u) = cur.as_mut() {
                u.disabled = v.contains('D');
            }
        }
    }
    if let Some(u) = cur {
        users.push(u);
    }
    for u in &mut users {
        u.groups = get_user_groups(&u.username).await;
    }
    Ok(users)
}

async fn get_user_groups(name: &str) -> Vec<String> {
    if let Ok(out) = Command::new("id").args(["-Gn", "--", name]).output().await {
        if out.status.success() {
            return String::from_utf8_lossy(&out.stdout)
                .split_whitespace()
                .map(|s| s.to_string())
                .collect();
        }
    }
    Vec::new()
}

/// 从 getent passwd/group 构造 "数字id → 名字" 映射（用于把 smbstatus 的数字解析成名字）。
/// passwd/group 每行格式：name:x:id:...，第 3 列是 uid/gid。
async fn getent_id_map(db: &str) -> std::collections::HashMap<String, String> {
    let file = if db == "group" { "/etc/group" } else { "/etc/passwd" };
    let text = match Command::new("getent").arg(db).output().await {
        Ok(o) if o.status.success() => String::from_utf8_lossy(&o.stdout).to_string(),
        _ => std::fs::read_to_string(file).unwrap_or_default(),
    };
    let mut map = std::collections::HashMap::new();
    for line in text.lines() {
        let cols: Vec<&str> = line.split(':').collect();
        // 第 3 列必须是数字 uid/gid（跳过异常行，避免非法 id 污染映射）
        if cols.len() >= 3 && !cols[0].is_empty() && cols[2].parse::<u32>().is_ok() {
            // 同一个 id 可能有多个名字，保留第一个即可
            map.entry(cols[2].to_string()).or_insert_with(|| cols[0].to_string());
        }
    }
    map
}

pub async fn list_groups() -> Result<Vec<String>, String> {
    let out = match Command::new("getent").arg("group").output().await {
        Ok(o) if o.status.success() => String::from_utf8_lossy(&o.stdout).to_string(),
        _ => std::fs::read_to_string("/etc/group").unwrap_or_default(),
    };
    let mut groups: Vec<String> = out
        .lines()
        .filter_map(|line| line.split(':').next())
        .filter(|g| !g.is_empty() && !g.starts_with('+') && !g.starts_with('-'))
        .map(|s| s.to_string())
        .collect();
    groups.sort();
    groups.dedup();
    Ok(groups)
}

/// 创建系统用户组（幂等：已存在则视为成功）
pub async fn create_group(name: &str) -> Result<(), String> {
    let name = name.trim();
    if !valid_existing_username(name) {
        return Err(format!("非法用户组名: {name}"));
    }
    // 已存在则直接成功
    let exists = Command::new("getent").args(["group", name]).output().await;
    if matches!(&exists, Ok(o) if o.status.success()) {
        return Ok(());
    }
    let out = Command::new("groupadd")
        .arg("--")
        .arg(name)
        .output()
        .await
        .map_err(|e| e.to_string())?;
    if out.status.success() {
        Ok(())
    } else {
        Err(format!("创建用户组失败: {}", String::from_utf8_lossy(&out.stderr).trim()))
    }
}

pub async fn set_user_groups(name: &str, groups: &[String]) -> Result<(), String> {
    if !valid_existing_username(name) {
        return Err("非法用户名".into());
    }
    for g in groups {
        if !valid_existing_username(g) {
            return Err(format!("非法用户组名: {g}"));
        }
    }
    let groups_arg = groups.join(",");
    let mut cmd = Command::new("usermod");
    if groups_arg.is_empty() {
        cmd.args(["-G", "", "--", name]);
    } else {
        cmd.args(["-G", &groups_arg, "--", name]);
    }
    let out = cmd.output().await.map_err(|e| e.to_string())?;
    if out.status.success() {
        Ok(())
    } else {
        Err(String::from_utf8_lossy(&out.stderr).trim().to_string())
    }
}

async fn smbpasswd_stdin(args: &[&str], password: &str) -> Result<(), String> {
    use std::process::Stdio;
    use tokio::io::AsyncWriteExt;
    // 密码含换行会截断喂给 smbpasswd 的两行输入，操纵其行为
    if password.contains('\n') || password.contains('\r') {
        return Err("密码不能包含换行符".into());
    }
    let mut child = Command::new("smbpasswd")
        .arg("-s")
        .args(args)
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|e| e.to_string())?;
    let input = format!("{password}\n{password}\n");
    child
        .stdin
        .take()
        .ok_or("no stdin")?
        .write_all(input.as_bytes())
        .await
        .map_err(|e| e.to_string())?;
    let out = child.wait_with_output().await.map_err(|e| e.to_string())?;
    if out.status.success() {
        Ok(())
    } else {
        Err(String::from_utf8_lossy(&out.stderr).trim().to_string())
    }
}

pub async fn create_user(name: &str, password: &str) -> Result<(), String> {
    if !valid_username(name) {
        return Err("非法用户名（小写字母开头，可含数字、-、_，≤32 字符）".into());
    }
    if password.len() < 4 {
        return Err("密码至少 4 位".into());
    }
    // 密码合法性提前校验：避免 useradd 成功后 smbpasswd 才失败，残留孤儿系统账号
    if password.contains('\n') || password.contains('\r') {
        return Err("密码不能包含换行符".into());
    }
    // 系统/特权账号不允许开通 SMB（避免给 root 等开出网络口令）
    let exists = Command::new("id").arg("-u").arg(name).output().await;
    if let Ok(o) = &exists {
        if o.status.success() {
            let uid: u32 = String::from_utf8_lossy(&o.stdout).trim().parse().unwrap_or(0);
            if uid < 1000 {
                return Err(format!("{name} 是系统账号（uid {uid} < 1000），不允许开通 SMB 访问"));
            }
        }
    }
    // 对应 unix 用户不存在则创建（禁止登录 shell）
    let created_unix = !matches!(&exists, Ok(o) if o.status.success());
    if created_unix {
        let out = Command::new("useradd")
            .args(["-M", "-s", "/usr/sbin/nologin", name])
            .output()
            .await
            .map_err(|e| e.to_string())?;
        if !out.status.success() {
            return Err(format!("创建系统用户失败: {}", String::from_utf8_lossy(&out.stderr).trim()));
        }
    }
    // smbpasswd 失败时回滚刚创建的 unix 用户，不留孤儿账号
    if let Err(e) = smbpasswd_stdin(&["-a", name], password).await {
        if created_unix {
            let _ = Command::new("userdel").arg("--").arg(name).output().await;
        }
        return Err(e);
    }
    Ok(())
}

pub async fn set_user_password(name: &str, password: &str) -> Result<(), String> {
    if !valid_existing_username(name) {
        return Err("非法用户名".into());
    }
    if password.len() < 4 {
        return Err("密码至少 4 位".into());
    }
    smbpasswd_stdin(&[name], password).await
}

pub async fn set_user_enabled(name: &str, enabled: bool) -> Result<(), String> {
    if !valid_existing_username(name) {
        return Err("非法用户名".into());
    }
    let flag = if enabled { "-e" } else { "-d" };
    let out = Command::new("smbpasswd").args([flag, name]).output().await.map_err(|e| e.to_string())?;
    if out.status.success() {
        Ok(())
    } else {
        Err(String::from_utf8_lossy(&out.stderr).trim().to_string())
    }
}

pub async fn delete_user(name: &str) -> Result<(), String> {
    if !valid_existing_username(name) {
        return Err("非法用户名".into());
    }
    let out = Command::new("smbpasswd").args(["-x", name]).output().await.map_err(|e| e.to_string())?;
    if out.status.success() {
        Ok(())
    } else {
        Err(String::from_utf8_lossy(&out.stderr).trim().to_string())
    }
}

#[derive(Debug, Serialize, Clone)]
pub struct SmbConnection {
    pub pid: u32,
    pub username: String,
    #[serde(rename = "groupname")]
    pub group: String,
    pub machine: String,
    #[serde(rename = "ip_addr")]
    pub ip: String,
}

#[derive(Debug, Serialize, Clone)]
pub struct DiskInfo {
    pub fs: String,
    #[serde(rename = "mounted_on")]
    pub mount: String,
    pub total: u64,
    pub used: u64,
    pub avail: u64,
    pub pct: u32,
}

#[derive(Debug, Serialize, Clone)]
pub struct StatusDashboard {
    pub smbd_active: bool,
    pub nmbd_active: bool,
    pub connections: Vec<SmbConnection>,
    pub disks: Vec<DiskInfo>,
}

pub async fn migrate_share_from_main(name: &str) -> Result<String, String> {
    let _guard = CONF_LOCK.lock().await;
    let shares = list_all_shares().await?;
    let target = shares
        .iter()
        .find(|s| s.name.eq_ignore_ascii_case(name) && !s.managed)
        .ok_or_else(|| "无法在主配置中找到该非托管共享".to_string())?;

    let mut new_share = target.clone();
    new_share.managed = true;

    // 1. 在 /etc/samba/smb.conf 中注释掉该共享段
    let conf = std::fs::read_to_string(SMB_CONF).map_err(|e| e.to_string())?;
    let bak = format!("{SMB_CONF}.migrate.bak");
    if !Path::new(&bak).exists() {
        let _ = std::fs::copy(SMB_CONF, &bak);
    }
    let mut out_lines = Vec::new();
    let mut in_target_sec = false;
    for line in conf.lines() {
        let trimmed = line.trim();
        if trimmed.starts_with('[') && trimmed.ends_with(']') {
            let sec_name = trimmed[1..trimmed.len() - 1].trim();
            in_target_sec = sec_name.eq_ignore_ascii_case(name);
            if in_target_sec {
                out_lines.push(format!("# [migrated by webgui] {line}"));
                continue;
            }
        }
        if in_target_sec {
            out_lines.push(format!("# {line}"));
        } else {
            out_lines.push(line.to_string());
        }
    }
    let mut new_conf = out_lines.join("\n");
    if !new_conf.ends_with('\n') {
        new_conf.push('\n');
    }
    std::fs::write(SMB_CONF, new_conf).map_err(|e| e.to_string())?;

    // 2. 添加到托管列表并保存
    let mut managed = load_managed();
    managed.push(new_share);
    match save_managed(&managed).await {
        Ok(msg) => {
            invalidate_share_cache();
            Ok(format!("接管迁移成功: {msg}"))
        }
        Err(e) => {
            let _ = std::fs::write(SMB_CONF, conf);
            Err(format!("接管保存失败已自动回滚: {e}"))
        }
    }
}

pub async fn get_dashboard_status() -> StatusDashboard {
    let smbd_active = match Command::new("systemctl").args(["is-active", "smbd"]).output().await {
        Ok(o) => String::from_utf8_lossy(&o.stdout).trim() == "active",
        Err(_) => false,
    };
    let nmbd_active = match Command::new("systemctl").args(["is-active", "nmbd"]).output().await {
        Ok(o) => String::from_utf8_lossy(&o.stdout).trim() == "active",
        Err(_) => false,
    };

    // smbstatus -n 输出的是数字 uid/gid（避免其反向解析主机名可能卡住）；
    // 用 getent 一次性拉全量映射，自行把 uid→用户名、gid→组名 解析出来
    let uid_map = getent_id_map("passwd").await;
    let gid_map = getent_id_map("group").await;

    let mut connections = Vec::new();
    if let Ok(out) = Command::new("smbstatus").args(["-b", "-n"]).output().await {
        if out.status.success() {
            let text = String::from_utf8_lossy(&out.stdout);
            let mut past_header = false;
            for line in text.lines() {
                if line.starts_with("------") {
                    past_header = true;
                    continue;
                }
                if past_header {
                    let parts: Vec<&str> = line.split_whitespace().collect();
                    if parts.len() >= 4 {
                        if let Ok(pid) = parts[0].parse::<u32>() {
                            // parts[1]/parts[2] 是数字 uid/gid，映射回名字；映射不到则原样显示
                            let username = uid_map.get(parts[1]).cloned().unwrap_or_else(|| parts[1].to_string());
                            let group = gid_map.get(parts[2]).cloned().unwrap_or_else(|| parts[2].to_string());
                            let machine = parts[3].to_string();
                            let mut ip = String::new();
                            for p in &parts[4..] {
                                if p.contains("ipv4:") || p.contains("ipv6:") || (p.starts_with('(') && p.ends_with(')')) {
                                    ip = p.trim_matches(|c| c == '(' || c == ')').to_string();
                                    break;
                                }
                            }
                            if ip.is_empty() && parts.len() > 4 {
                                ip = parts[4].trim_matches(|c| c == '(' || c == ')').to_string();
                            }
                            // 去掉 ipv4:/ipv6: 前缀，展示干净的地址（保留端口）
                            ip = ip
                                .strip_prefix("ipv4:")
                                .or_else(|| ip.strip_prefix("ipv6:"))
                                .unwrap_or(&ip)
                                .to_string();
                            connections.push(SmbConnection {
                                pid,
                                username,
                                group,
                                machine,
                                ip,
                            });
                        }
                    }
                }
            }
        }
    }

    let mut disks = Vec::new();
    if let Ok(out) = Command::new("df").args(["-P", "-B1"]).output().await {
        if out.status.success() {
            let text = String::from_utf8_lossy(&out.stdout);
            for (i, line) in text.lines().enumerate() {
                if i == 0 {
                    continue;
                }
                let parts: Vec<&str> = line.split_whitespace().collect();
                if parts.len() >= 6 {
                    let fs = parts[0].to_string();
                    let mount = parts[5..].join(" ");
                    if !fs.starts_with("tmpfs")
                        && !fs.starts_with("devtmpfs")
                        && !fs.starts_with("sysfs")
                        && !fs.starts_with("proc")
                        && !fs.starts_with("cgroup")
                        && !fs.starts_with("overlay")
                        && !fs.starts_with("shm")
                        && !fs.starts_with("nsfs")
                        && !mount.starts_with("/dev")
                        && !mount.starts_with("/sys")
                        && !mount.starts_with("/proc")
                    {
                        let total = parts[1].parse::<u64>().unwrap_or(0);
                        let used = parts[2].parse::<u64>().unwrap_or(0);
                        let avail = parts[3].parse::<u64>().unwrap_or(0);
                        let pct = parts[4].trim_end_matches('%').parse::<u32>().unwrap_or(0);
                        disks.push(DiskInfo {
                            fs,
                            mount,
                            total,
                            used,
                            avail,
                            pct,
                        });
                    }
                }
            }
        }
    }

    StatusDashboard {
        smbd_active,
        nmbd_active,
        connections,
        disks,
    }
}

pub async fn disconnect_client(pid: u32) -> Result<(), String> {
    if pid <= 1 {
        return Err("非法 PID".into());
    }
    let out = Command::new("kill").args(["-9", &pid.to_string()]).output().await.map_err(|e| e.to_string())?;
    if out.status.success() {
        Ok(())
    } else {
        Err(String::from_utf8_lossy(&out.stderr).trim().to_string())
    }
}

pub async fn service_action(action: &str) -> Result<String, String> {
    if !matches!(action, "restart" | "reload" | "start" | "stop") {
        return Err("非法操作".into());
    }
    let out = Command::new("systemctl").args([action, "smbd"]).output().await.map_err(|e| e.to_string())?;
    let _ = Command::new("systemctl").args([action, "nmbd"]).output().await;
    if out.status.success() {
        Ok(format!("smbd 和 nmbd 服务已执行 {action}"))
    } else {
        Err(String::from_utf8_lossy(&out.stderr).trim().to_string())
    }
}
