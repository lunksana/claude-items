//! Samba 配置与用户管理：共享写入独立 include 文件，用户走 pdbedit/smbpasswd。

use serde::{Deserialize, Serialize};
use std::path::Path;
use tokio::process::Command;

pub const SMB_CONF: &str = "/etc/samba/smb.conf";
pub const MANAGED_CONF: &str = "/etc/samba/webgui-shares.conf";

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
}

pub fn valid_section_name(s: &str) -> bool {
    !s.is_empty()
        && s.len() <= 64
        && !s.eq_ignore_ascii_case("global")
        && s.chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.' | ' '))
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
    Share {
        name: name.to_string(),
        path: get(kvs, "path").unwrap_or("").to_string(),
        comment: get(kvs, "comment").unwrap_or("").to_string(),
        read_only,
        guest_ok: as_bool(get(kvs, "guest ok"), false),
        browseable: as_bool(get(kvs, "browseable").or(get(kvs, "browsable")), true),
        valid_users: get(kvs, "valid users").unwrap_or("").to_string(),
        write_list: get(kvs, "write list").unwrap_or("").to_string(),
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

pub fn load_managed() -> Vec<Share> {
    let text = std::fs::read_to_string(MANAGED_CONF).unwrap_or_default();
    parse_ini(&text)
        .iter()
        .map(|(name, kvs)| section_to_share(name, kvs, true))
        .collect()
}

fn render_managed(shares: &[Share]) -> String {
    let mut out = String::from("# Managed by samba-webgui. Do not edit manually.\n");
    for s in shares {
        out.push_str(&format!("\n[{}]\n", s.name));
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
        // inherit acls: SMB 客户端新建的文件继承目录的默认 ACL
        out.push_str("   create mask = 0664\n   directory mask = 0775\n   inherit acls = yes\n");
    }
    out
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
        {
            return Err("字段中不允许控制字符".into());
        }
    }
    let old = std::fs::read_to_string(MANAGED_CONF).unwrap_or_default();
    std::fs::write(MANAGED_CONF, render_managed(shares)).map_err(|e| e.to_string())?;

    let check = Command::new("testparm")
        .args(["-s", "--suppress-prompt", SMB_CONF])
        .output()
        .await
        .map_err(|e| e.to_string())?;
    if !check.status.success() {
        let _ = std::fs::write(MANAGED_CONF, old);
        return Err(format!(
            "配置校验失败，已回滚: {}",
            String::from_utf8_lossy(&check.stderr).trim()
        ));
    }
    invalidate_share_cache();
    Ok(reload_smbd().await)
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
    // 取 write_list / valid_users 中第一个普通用户作为属主
    let owner = share
        .write_list
        .split(&[',', ' '][..])
        .chain(share.valid_users.split(&[',', ' '][..]))
        .map(|s| s.trim())
        .find(|s| !s.is_empty() && !s.starts_with('@') && !s.starts_with('+'))
        .map(|s| s.to_string())
        .or_else(|| share.guest_ok.then(|| "nobody".to_string()));

    let mode = if share.guest_ok && !share.read_only { "0777" } else { "0775" };
    let out = Command::new("chmod")
        .args([mode, "--", &path])
        .output()
        .await
        .map_err(|e| e.to_string())?;
    if !out.status.success() {
        return Err(format!("chmod 失败: {}", String::from_utf8_lossy(&out.stderr).trim()));
    }
    if let Some(owner) = &owner {
        if !valid_username(owner) && owner != "nobody" {
            return Err(format!("无法确定合法属主: {owner}"));
        }
        let out = Command::new("chown")
            .args([owner.as_str(), "--", &path])
            .output()
            .await
            .map_err(|e| e.to_string())?;
        if !out.status.success() {
            return Err(format!("chown 失败: {}", String::from_utf8_lossy(&out.stderr).trim()));
        }
        Ok(format!("目录权限已修正（属主 {owner}，权限 {mode}）"))
    } else {
        Ok(format!("目录权限已修正（{mode}）"))
    }
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
            cur = Some(SmbUser { username: v.trim().to_string(), uid: String::new(), disabled: false });
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
    Ok(users)
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
    if !matches!(&exists, Ok(o) if o.status.success()) {
        let out = Command::new("useradd")
            .args(["-M", "-s", "/usr/sbin/nologin", name])
            .output()
            .await
            .map_err(|e| e.to_string())?;
        if !out.status.success() {
            return Err(format!("创建系统用户失败: {}", String::from_utf8_lossy(&out.stderr).trim()));
        }
    }
    smbpasswd_stdin(&["-a", name], password).await
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
