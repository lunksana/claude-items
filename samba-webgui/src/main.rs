mod files;
mod samba;

use argon2::password_hash::{rand_core::OsRng, PasswordHash, PasswordHasher, PasswordVerifier, SaltString};
use argon2::Argon2;
use axum::extract::{ConnectInfo, DefaultBodyLimit, Path as AxPath, Request, State};
use axum::http::{header, StatusCode};
use axum::middleware::{self, Next};
use axum::response::{Html, IntoResponse, Response};
use axum::routing::{get, post, put};
use axum::{Json, Router};
use rand::RngCore;
use serde::Deserialize;
use serde_json::json;
use std::collections::HashMap;
use std::net::{IpAddr, SocketAddr};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

const CONFIG_PATH: &str = "data/config.json";
const DEFAULT_PASSWORD: &str = "admin123";

#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct AppConfig {
    pub password_hash: String,
    #[serde(default = "default_listen")]
    pub listen_addr: String,
    #[serde(default = "default_ttl")]
    pub session_ttl_hours: u64,
    #[serde(default)]
    pub guest_map_bad_user: bool,
    /// 登录后必须修改密码（首次运行生成默认密码时为 true，改密成功后清除）
    #[serde(default)]
    pub must_change_password: bool,
}

fn default_listen() -> String {
    std::env::var("SWG_LISTEN").unwrap_or_else(|_| "0.0.0.0:8686".into())
}
fn default_ttl() -> u64 {
    24
}

pub struct AppState {
    sessions: Mutex<HashMap<String, Instant>>,
    config: Mutex<AppConfig>,
    /// 每 IP 的 (窗口内失败次数, 窗口起点)：按来源限流，避免全局锁定被单一攻击者利用为 DoS
    login_fails: Mutex<HashMap<IpAddr, (u32, Instant)>>,
}

const LOGIN_FAIL_LIMIT: u32 = 5;
const LOGIN_LOCKOUT: Duration = Duration::from_secs(60);
const LOGIN_FAILS_CAP: usize = 4096;

pub type SharedState = Arc<AppState>;

pub fn err_json(code: StatusCode, msg: &str) -> Response {
    (code, Json(json!({ "error": msg }))).into_response()
}

fn hash_password(pw: &str) -> String {
    let salt = SaltString::generate(&mut OsRng);
    Argon2::default().hash_password(pw.as_bytes(), &salt).unwrap().to_string()
}

fn verify_password(pw: &str, hash: &str) -> bool {
    PasswordHash::new(hash)
        .map(|h| Argon2::default().verify_password(pw.as_bytes(), &h).is_ok())
        .unwrap_or(false)
}

fn load_or_init_config() -> AppConfig {
    match std::fs::read_to_string(CONFIG_PATH) {
        Ok(text) => {
            if let Ok(mut cfg) = serde_json::from_str::<AppConfig>(&text) {
                if !cfg.password_hash.is_empty() {
                    if cfg.session_ttl_hours == 0 {
                        cfg.session_ttl_hours = 24;
                    }
                    return cfg;
                }
            } else if let Ok(v) = serde_json::from_str::<serde_json::Value>(&text) {
                if let Some(h) = v.get("password_hash").and_then(|h| h.as_str()) {
                    if !h.is_empty() {
                        return AppConfig {
                            password_hash: h.to_string(),
                            listen_addr: default_listen(),
                            session_ttl_hours: default_ttl(),
                            guest_map_bad_user: false,
                            must_change_password: false,
                        };
                    }
                }
            }
            // 文件存在但无法解析/无有效密码：绝不静默重置为默认密码（否则掉电损坏后留后门）
            eprintln!("✗ {CONFIG_PATH} 已损坏或缺少有效 password_hash，拒绝启动以免密码被重置为默认。");
            eprintln!("  请修复该文件，或确认无价值后删除它再重启（删除后会重新生成默认密码）。");
            std::process::exit(1);
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            // 文件不存在=首次运行，生成默认（并标记必须改密）
            let cfg = AppConfig {
                password_hash: hash_password(DEFAULT_PASSWORD),
                listen_addr: default_listen(),
                session_ttl_hours: default_ttl(),
                guest_map_bad_user: false,
                must_change_password: true,
            };
            if let Err(e) = save_config(&cfg) {
                eprintln!("⚠ 默认配置写入失败（重启后默认密码将不生效）: {e}");
            }
            eprintln!("⚠ 已生成默认登录密码: {DEFAULT_PASSWORD}（登录后必须立即修改）");
            cfg
        }
        Err(e) => {
            eprintln!("✗ 无法读取 {CONFIG_PATH}: {e}，拒绝启动");
            std::process::exit(1);
        }
    }
}

/// 原子写配置：临时文件 + fsync + rename。失败返回错误（由调用方决定如何处理），不再静默吞掉
fn save_config(cfg: &AppConfig) -> std::io::Result<()> {
    use std::io::Write;
    std::fs::create_dir_all("data")?;
    let data = serde_json::to_string_pretty(cfg).unwrap();
    let tmp = format!("{CONFIG_PATH}.tmp");
    {
        let mut f = std::fs::File::create(&tmp)?;
        f.write_all(data.as_bytes())?;
        f.sync_all()?;
    }
    std::fs::rename(&tmp, CONFIG_PATH)?;
    Ok(())
}

/// 审计日志：JSON Lines 追加写 data/audit.log（与配置同目录），尽力而为、不阻断业务操作
pub fn audit(event: &str, detail: &str) {
    static AUDIT_LOCK: Mutex<()> = Mutex::new(());
    let _g = AUDIT_LOCK.lock().unwrap_or_else(|e| e.into_inner());
    let ts = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let entry = json!({ "ts": ts, "event": event, "detail": detail });
    use std::io::Write;
    let _ = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open("data/audit.log")
        .and_then(|mut f| writeln!(f, "{entry}"));
}

fn new_token() -> String {
    let mut buf = [0u8; 32];
    rand::thread_rng().fill_bytes(&mut buf);
    buf.iter().map(|b| format!("{b:02x}")).collect()
}

fn get_cookie(req: &Request, name: &str) -> Option<String> {
    let cookies = req.headers().get(header::COOKIE)?.to_str().ok()?;
    cookies.split(';').find_map(|kv| {
        let (k, v) = kv.trim().split_once('=')?;
        (k == name).then(|| v.to_string())
    })
}

async fn require_auth(State(st): State<SharedState>, req: Request, next: Next) -> Response {
    let authed = get_cookie(&req, "sid").is_some_and(|tok| {
        let ttl = Duration::from_secs(st.config.lock().unwrap().session_ttl_hours * 3600);
        let mut sessions = st.sessions.lock().unwrap();
        match sessions.get_mut(&tok) {
            Some(exp) if exp.elapsed() < ttl => {
                *exp = Instant::now();
                true
            }
            Some(_) => {
                sessions.remove(&tok);
                false
            }
            None => false,
        }
    });
    if authed {
        next.run(req).await
    } else {
        err_json(StatusCode::UNAUTHORIZED, "未登录或会话已过期")
    }
}

#[derive(Deserialize)]
struct LoginReq {
    password: String,
}

async fn login(
    State(st): State<SharedState>,
    ConnectInfo(peer): ConnectInfo<SocketAddr>,
    Json(req): Json<LoginReq>,
) -> Response {
    let ip = peer.ip();
    // 按来源 IP 限流：窗口内失败超限直接拒绝（sleep 挡不住并发爆破，全局锁定会被利用为 DoS）
    {
        let mut fails = st.login_fails.lock().unwrap();
        fails.retain(|_, (_, t)| t.elapsed() <= LOGIN_LOCKOUT);
        if fails.len() > LOGIN_FAILS_CAP {
            fails.clear(); // 防 IP 伪造撑爆内存（TCP 完成握手的源 IP 无法伪造，属兜底）
        }
        if let Some((n, t)) = fails.get(&ip) {
            if *n >= LOGIN_FAIL_LIMIT && t.elapsed() <= LOGIN_LOCKOUT {
                audit("login_blocked", &format!("ip={ip}, fails={n}"));
                return err_json(StatusCode::TOO_MANY_REQUESTS, "失败次数过多，请稍后再试");
            }
        }
    }
    let hash = st.config.lock().unwrap().password_hash.clone();
    if !verify_password(&req.password, &hash) {
        {
            let mut fails = st.login_fails.lock().unwrap();
            let e = fails.entry(ip).or_insert((0, Instant::now()));
            if e.1.elapsed() > LOGIN_LOCKOUT {
                *e = (0, Instant::now());
            }
            e.0 += 1;
        }
        audit("login_failed", &format!("ip={ip}"));
        tokio::time::sleep(Duration::from_millis(800)).await;
        return err_json(StatusCode::UNAUTHORIZED, "密码错误");
    }
    st.login_fails.lock().unwrap().remove(&ip);
    audit("login_ok", &format!("ip={ip}"));
    let tok = new_token();
    // Cookie 有效期与服务器端会话 TTL 保持一致（此前硬编码 24h，TTL>24h 时浏览器仍 24h 掉线）
    let ttl_secs = st.config.lock().unwrap().session_ttl_hours * 3600;
    {
        let ttl = Duration::from_secs(ttl_secs);
        let mut sessions = st.sessions.lock().unwrap();
        sessions.retain(|_, exp| exp.elapsed() < ttl);
        sessions.insert(tok.clone(), Instant::now());
    }
    Response::builder()
        .header(
            header::SET_COOKIE,
            format!("sid={tok}; HttpOnly; SameSite=Strict; Path=/; Max-Age={ttl_secs}"),
        )
        .header(header::CONTENT_TYPE, "application/json")
        .body(json!({ "ok": true }).to_string().into())
        .unwrap()
}

async fn logout(State(st): State<SharedState>, req: Request) -> Response {
    if let Some(tok) = get_cookie(&req, "sid") {
        st.sessions.lock().unwrap().remove(&tok);
    }
    audit("logout", "");
    Response::builder()
        .header(header::SET_COOKIE, "sid=; HttpOnly; Path=/; Max-Age=0")
        .header(header::CONTENT_TYPE, "application/json")
        .body(json!({ "ok": true }).to_string().into())
        .unwrap()
}

async fn me(State(st): State<SharedState>) -> Response {
    let must = st.config.lock().unwrap().must_change_password;
    Json(json!({ "ok": true, "must_change_password": must })).into_response()
}

#[derive(Deserialize)]
struct ChangePwReq {
    old_password: String,
    new_password: String,
}

async fn change_password(State(st): State<SharedState>, Json(req): Json<ChangePwReq>) -> Response {
    if req.new_password.len() < 6 {
        return err_json(StatusCode::BAD_REQUEST, "新密码至少 6 位");
    }
    let mut cfg = st.config.lock().unwrap();
    if !verify_password(&req.old_password, &cfg.password_hash) {
        return err_json(StatusCode::UNAUTHORIZED, "原密码错误");
    }
    cfg.password_hash = hash_password(&req.new_password);
    cfg.must_change_password = false;
    match save_config(&cfg) {
        Ok(_) => {
            audit("password_changed", "WebGUI 管理密码");
            Json(json!({ "ok": true })).into_response()
        }
        Err(e) => err_json(
            StatusCode::INTERNAL_SERVER_ERROR,
            &format!("配置保存失败，密码可能未持久化（重启后恢复旧密码）: {e}"),
        ),
    }
}

// ---- 共享管理 ----

async fn shares_list() -> Response {
    match samba::list_all_shares().await {
        Ok(shares) => Json(json!({ "shares": shares })).into_response(),
        Err(e) => err_json(StatusCode::INTERNAL_SERVER_ERROR, &e),
    }
}

async fn share_create(Json(mut share): Json<samba::Share>) -> Response {
    let _guard = samba::CONF_LOCK.lock().await;
    share.managed = true;
    let all = match samba::list_all_shares().await {
        Ok(v) => v,
        Err(e) => return err_json(StatusCode::INTERNAL_SERVER_ERROR, &e),
    };
    if all.iter().any(|s| s.name.eq_ignore_ascii_case(&share.name)) {
        return err_json(StatusCode::CONFLICT, "同名共享已存在");
    }
    if let Err(e) = samba::check_share_path(&share.path) {
        return err_json(StatusCode::BAD_REQUEST, &e);
    }
    let mut managed = samba::load_managed();
    managed.push(share.clone());
    samba::backup_config(); // 改动前快照，供"还原上次配置"
    match samba::save_managed(&managed).await {
        Ok(msg) => {
            audit("share_create", &format!("name={} path={}", share.name, share.path));
            Json(json!({ "ok": true, "message": apply_perms(&share, msg).await })).into_response()
        }
        Err(e) => err_json(StatusCode::BAD_REQUEST, &e),
    }
}

async fn apply_perms(share: &samba::Share, mut msg: String) -> String {
    if share.fix_perms {
        match samba::fix_share_perms(share).await {
            Ok(m) => msg = format!("{msg}；{m}"),
            Err(e) => msg = format!("{msg}；但权限修正失败: {e}"),
        }
        // SELinux 强制模式下，目录需 samba_share_t 上下文 Samba 才能访问；未启用则无操作
        match samba::apply_selinux_context(&share.path, &share.selinux_type).await {
            Ok(Some(m)) => msg = format!("{msg}；{m}"),
            Ok(None) => {}
            Err(e) => msg = format!("{msg}；但 {e}"),
        }
    }
    // 粘滞位始终按配置同步（+t/-t）：即使未勾选"自动修正权限"，关闭限制删除也能真正清除 +t
    if let Err(e) = samba::apply_sticky(&share.path, share.sticky).await {
        msg = format!("{msg}；但粘滞位设置失败: {e}");
    }
    msg
}

async fn share_update(AxPath(name): AxPath<String>, Json(mut share): Json<samba::Share>) -> Response {
    let _guard = samba::CONF_LOCK.lock().await;
    share.managed = true;
    let mut managed = samba::load_managed();
    let Some(idx) = managed.iter().position(|s| s.name.eq_ignore_ascii_case(&name)) else {
        return err_json(StatusCode::NOT_FOUND, "共享不存在或不由本工具管理");
    };
    if !share.name.eq_ignore_ascii_case(&name)
        && managed.iter().any(|s| s.name.eq_ignore_ascii_case(&share.name))
    {
        return err_json(StatusCode::CONFLICT, "同名共享已存在");
    }
    if let Err(e) = samba::check_share_path(&share.path) {
        return err_json(StatusCode::BAD_REQUEST, &e);
    }
    managed[idx] = share.clone();
    samba::backup_config(); // 改动前快照，供"还原上次配置"
    match samba::save_managed(&managed).await {
        Ok(msg) => {
            audit("share_update", &format!("name={} path={}", share.name, share.path));
            Json(json!({ "ok": true, "message": apply_perms(&share, msg).await })).into_response()
        }
        Err(e) => err_json(StatusCode::BAD_REQUEST, &e),
    }
}

async fn share_delete(AxPath(name): AxPath<String>) -> Response {
    let _guard = samba::CONF_LOCK.lock().await;
    let mut managed = samba::load_managed();
    let before = managed.len();
    managed.retain(|s| !s.name.eq_ignore_ascii_case(&name));
    if managed.len() == before {
        return err_json(StatusCode::NOT_FOUND, "共享不存在或不由本工具管理");
    }
    samba::backup_config(); // 改动前快照，供"还原上次配置"
    match samba::save_managed(&managed).await {
        Ok(msg) => {
            audit("share_delete", &format!("name={name}"));
            Json(json!({ "ok": true, "message": msg })).into_response()
        }
        Err(e) => err_json(StatusCode::BAD_REQUEST, &e),
    }
}

// ---- 用户管理 ----

async fn users_list() -> Response {
    match samba::list_users().await {
        Ok(users) => Json(json!({ "users": users })).into_response(),
        Err(e) => err_json(StatusCode::INTERNAL_SERVER_ERROR, &e),
    }
}

#[derive(Deserialize)]
struct UserCreateReq {
    username: String,
    password: String,
    #[serde(default)]
    groups: Vec<String>,
}

async fn user_create(Json(req): Json<UserCreateReq>) -> Response {
    if let Err(e) = samba::create_user(&req.username, &req.password).await {
        return err_json(StatusCode::BAD_REQUEST, &e);
    }
    // 用户已建成；再补充组成员资格（缺失的组自动创建）。
    // 组这步失败不回滚用户，只在消息里提示。
    if !req.groups.is_empty() {
        for g in &req.groups {
            if let Err(e) = samba::create_group(g).await {
                audit("user_create_warn", &format!("username={} group={g} err={e}", req.username));
                return Json(json!({ "ok": true, "warn": true, "message": format!("用户已创建，但用户组 {g} 处理失败: {e}") })).into_response();
            }
        }
        if let Err(e) = samba::set_user_groups(&req.username, &req.groups).await {
            audit("user_create_warn", &format!("username={} groups err={e}", req.username));
            return Json(json!({ "ok": true, "warn": true, "message": format!("用户已创建，但设置用户组失败: {e}") })).into_response();
        }
    }
    audit("user_create", &format!("username={} groups={:?}", req.username, req.groups));
    Json(json!({ "ok": true })).into_response()
}

#[derive(Deserialize)]
struct GroupCreateReq {
    name: String,
}

async fn group_create(Json(req): Json<GroupCreateReq>) -> Response {
    match samba::create_group(&req.name).await {
        Ok(_) => {
            audit("group_create", &format!("name={}", req.name));
            Json(json!({ "ok": true })).into_response()
        }
        Err(e) => err_json(StatusCode::BAD_REQUEST, &e),
    }
}

#[derive(Deserialize)]
struct UserPwReq {
    password: String,
}

async fn user_password(AxPath(name): AxPath<String>, Json(req): Json<UserPwReq>) -> Response {
    match samba::set_user_password(&name, &req.password).await {
        Ok(_) => {
            audit("user_password", &format!("username={name}"));
            Json(json!({ "ok": true })).into_response()
        }
        Err(e) => err_json(StatusCode::BAD_REQUEST, &e),
    }
}

#[derive(Deserialize)]
struct UserEnableReq {
    enabled: bool,
}

async fn user_enable(AxPath(name): AxPath<String>, Json(req): Json<UserEnableReq>) -> Response {
    match samba::set_user_enabled(&name, req.enabled).await {
        Ok(_) => {
            audit("user_enable", &format!("username={name} enabled={}", req.enabled));
            Json(json!({ "ok": true })).into_response()
        }
        Err(e) => err_json(StatusCode::BAD_REQUEST, &e),
    }
}

async fn user_delete(AxPath(name): AxPath<String>) -> Response {
    match samba::delete_user(&name).await {
        Ok(_) => {
            audit("user_delete", &format!("username={name}"));
            Json(json!({ "ok": true })).into_response()
        }
        Err(e) => err_json(StatusCode::BAD_REQUEST, &e),
    }
}

#[derive(Deserialize)]
struct ConfigUpdateReq {
    listen_addr: String,
    session_ttl_hours: u64,
    guest_map_bad_user: bool,
    #[serde(default)]
    smb_min_protocol: String,
    #[serde(default)]
    smb_max_protocol: String,
}

async fn config_get(State(st): State<SharedState>) -> Response {
    let cfg = st.config.lock().unwrap().clone();
    Json(json!({
        "listen_addr": cfg.listen_addr,
        "session_ttl_hours": cfg.session_ttl_hours,
        "guest_map_bad_user": cfg.guest_map_bad_user,
        // SMB 协议版本直接读自 smb.conf [global]（缺省=空=用 Samba 默认）
        "smb_min_protocol": samba::read_global_param("server min protocol").unwrap_or_default(),
        "smb_max_protocol": samba::read_global_param("server max protocol").unwrap_or_default(),
        "backup_ts": samba::backup_timestamp(),
    })).into_response()
}

async fn config_update(State(st): State<SharedState>, Json(req): Json<ConfigUpdateReq>) -> Response {
    if req.session_ttl_hours < 1 || req.session_ttl_hours > 720 {
        return err_json(StatusCode::BAD_REQUEST, "会话超时时长必须在 1~720 小时之间");
    }
    if req.listen_addr.trim().is_empty() {
        return err_json(StatusCode::BAD_REQUEST, "监听地址不能为空");
    }
    // SMB 协议版本校验（空 = 用 Samba 默认，即删除该行）
    let min_p = req.smb_min_protocol.trim().to_uppercase();
    let max_p = req.smb_max_protocol.trim().to_uppercase();
    if !min_p.is_empty() && !samba::valid_protocol(&min_p) {
        return err_json(StatusCode::BAD_REQUEST, "非法的最小 SMB 协议版本");
    }
    if !max_p.is_empty() && !samba::valid_protocol(&max_p) {
        return err_json(StatusCode::BAD_REQUEST, "非法的最大 SMB 协议版本");
    }

    {
        let mut cfg = st.config.lock().unwrap();
        cfg.listen_addr = req.listen_addr.trim().to_string();
        cfg.session_ttl_hours = req.session_ttl_hours;
        cfg.guest_map_bad_user = req.guest_map_bad_user;
        if let Err(e) = save_config(&cfg) {
            return err_json(StatusCode::INTERNAL_SERVER_ERROR, &format!("WebGUI 配置保存失败: {e}"));
        }
    }

    // 期望的 [global] 参数（这些才真正落到 smb.conf）
    let guard = samba::CONF_LOCK.lock().await;
    let pairs: Vec<(&str, Option<String>)> = vec![
        ("map to guest", Some(if req.guest_map_bad_user { "Bad User".into() } else { "Never".into() })),
        ("server min protocol", (!min_p.is_empty()).then(|| min_p.clone())),
        ("server max protocol", (!max_p.is_empty()).then(|| max_p.clone())),
    ];
    // 仅当 smb.conf 实际会变化时才快照并写入（避免无谓的备份/重载消耗还原点）
    let norm = |s: Option<String>| s.map(|v| v.trim().to_uppercase()).unwrap_or_default();
    let changed = pairs.iter().any(|(k, want)| {
        norm(samba::read_global_param(k)) != norm(want.clone())
    });
    if changed {
        samba::backup_config();
        if let Err(e) = samba::set_global_params(&pairs).await {
            return err_json(StatusCode::BAD_REQUEST, &e);
        }
    }
    drop(guard);
    audit("config_update", &format!("listen={} ttl={}h guest_map={} smb_min={min_p} smb_max={max_p}", req.listen_addr.trim(), req.session_ttl_hours, req.guest_map_bad_user));

    Json(json!({ "ok": true })).into_response()
}

async fn status_get() -> Response {
    Json(samba::get_dashboard_status().await).into_response()
}

#[derive(Deserialize)]
struct DisconnectReq {
    pid: u32,
}

async fn status_disconnect(Json(req): Json<DisconnectReq>) -> Response {
    match samba::disconnect_client(req.pid).await {
        Ok(_) => {
            audit("client_disconnect", &format!("pid={}", req.pid));
            Json(json!({ "ok": true })).into_response()
        }
        Err(e) => err_json(StatusCode::BAD_REQUEST, &e),
    }
}

#[derive(Deserialize)]
struct ServiceReq {
    action: String,
}

async fn status_service(Json(req): Json<ServiceReq>) -> Response {
    match samba::service_action(&req.action).await {
        Ok(msg) => {
            audit("service_action", &format!("action={}", req.action));
            Json(json!({ "ok": true, "msg": msg })).into_response()
        }
        Err(e) => err_json(StatusCode::BAD_REQUEST, &e),
    }
}

async fn share_migrate(AxPath(name): AxPath<String>) -> Response {
    // 锁与改动前快照都在 migrate_share_from_main 内部完成（避免对 CONF_LOCK 重复加锁造成死锁）
    match samba::migrate_share_from_main(&name).await {
        Ok(msg) => {
            audit("share_migrate", &format!("name={name}"));
            Json(json!({ "ok": true, "msg": msg })).into_response()
        }
        Err(e) => err_json(StatusCode::BAD_REQUEST, &e),
    }
}

async fn config_restore() -> Response {
    let _guard = samba::CONF_LOCK.lock().await;
    match samba::restore_config().await {
        Ok(msg) => {
            audit("config_restore", "还原到上次配置备份");
            Json(json!({ "ok": true, "message": msg })).into_response()
        }
        Err(e) => err_json(StatusCode::BAD_REQUEST, &e),
    }
}

async fn groups_list() -> Response {
    match samba::list_groups().await {
        Ok(g) => Json(json!({ "groups": g })).into_response(),
        Err(e) => err_json(StatusCode::INTERNAL_SERVER_ERROR, &e),
    }
}

#[derive(Deserialize)]
struct UserGroupsReq {
    groups: Vec<String>,
}

async fn user_groups_update(AxPath(name): AxPath<String>, Json(req): Json<UserGroupsReq>) -> Response {
    match samba::set_user_groups(&name, &req.groups).await {
        Ok(_) => {
            audit("user_groups", &format!("username={name} groups={:?}", req.groups));
            Json(json!({ "ok": true })).into_response()
        }
        Err(e) => err_json(StatusCode::BAD_REQUEST, &e),
    }
}

// ---- 静态页面 ----

async fn index() -> Html<String> {
    // 把版本号注入到页面占位符，前端无需额外请求
    Html(include_str!("../static/index.html").replace("{{VERSION}}", env!("CARGO_PKG_VERSION")))
}

async fn app_js() -> Response {
    ([(header::CONTENT_TYPE, "application/javascript; charset=utf-8")], include_str!("../static/app.js"))
        .into_response()
}

async fn style_css() -> Response {
    ([(header::CONTENT_TYPE, "text/css; charset=utf-8")], include_str!("../static/style.css")).into_response()
}

#[tokio::main]
async fn main() {
    if !nix_is_root() {
        eprintln!("⚠ 未以 root 运行，Samba 配置与用户管理可能失败");
    }
    if let Err(e) = samba::ensure_include() {
        eprintln!("✗ 无法接管 smb.conf include: {e}，拒绝启动（共享管理功能将不可用）");
        std::process::exit(1);
    }

    let cfg = load_or_init_config();
    // 数据目录基于当前工作目录；明确打印，避免换目录启动后"静默重置"默认密码
    let data_dir = std::fs::canonicalize("data")
        .map(|p| p.display().to_string())
        .unwrap_or_else(|_| "data/".to_string());
    println!("数据与配置目录: {data_dir}（{CONFIG_PATH}）");
    let listen_str = if let Ok(env_addr) = std::env::var("SWG_LISTEN") {
        env_addr
    } else {
        cfg.listen_addr.clone()
    };

    let state: SharedState = Arc::new(AppState {
        sessions: Mutex::new(HashMap::new()),
        config: Mutex::new(cfg),
        login_fails: Mutex::new(HashMap::new()),
    });

    let protected = Router::new()
        .route("/api/logout", post(logout))
        .route("/api/me", get(me))
        .route("/api/password", post(change_password))
        .route("/api/config", get(config_get).post(config_update))
        .route("/api/config/restore", post(config_restore))
        .route("/api/status", get(status_get))
        .route("/api/status/disconnect", post(status_disconnect))
        .route("/api/status/service", post(status_service))
        .route("/api/shares", get(shares_list).post(share_create))
        .route("/api/shares/{name}", put(share_update).delete(share_delete))
        .route("/api/shares/migrate/{name}", post(share_migrate))
        .route("/api/users", get(users_list).post(user_create))
        .route("/api/users/{name}", axum::routing::delete(user_delete))
        .route("/api/users/{name}/password", put(user_password))
        .route("/api/users/{name}/enable", put(user_enable))
        .route("/api/groups", get(groups_list).post(group_create))
        .route("/api/users/{name}/groups", put(user_groups_update))
        .route("/api/files", get(files::list))
        .route("/api/files/stat", get(files::stat))
        .route("/api/files/download", get(files::download))
        .route(
            "/api/files/upload",
            post(files::upload).layer(DefaultBodyLimit::max(8 * 1024 * 1024 * 1024)),
        )
        .route("/api/files/mkdir", post(files::mkdir))
        .route("/api/files/delete", post(files::delete))
        .route("/api/files/acl", get(files::acl_get).post(files::acl_set))
        .route("/api/files/tree", get(files::tree_list))
        .route("/api/files/tree/mkdir", post(files::tree_mkdir))
        .route("/api/files/copy", post(files::copy_file))
        .route("/api/files/move", post(files::move_file))
        .route("/api/files/extract", post(files::extract_archive))
        .route("/api/files/archive", post(files::create_archive))
        .layer(middleware::from_fn_with_state(state.clone(), require_auth));

    let app = Router::new()
        .route("/", get(index))
        // index.html 引用的是根路径；同时保留 /static/ 别名向后兼容
        .route("/app.js", get(app_js))
        .route("/style.css", get(style_css))
        .route("/static/app.js", get(app_js))
        .route("/static/style.css", get(style_css))
        .route("/api/login", post(login))
        .merge(protected)
        .with_state(state);

    let listener = tokio::net::TcpListener::bind(&listen_str).await.expect("端口绑定失败");
    println!("Samba WebGUI 已启动: http://{listen_str}");
    axum::serve(listener, app.into_make_service_with_connect_info::<SocketAddr>())
        .await
        .unwrap();
}

fn nix_is_root() -> bool {
    std::fs::read_to_string("/proc/self/status")
        .ok()
        .and_then(|s| {
            s.lines()
                .find(|l| l.starts_with("Uid:"))
                .and_then(|l| l.split_whitespace().nth(1).map(|u| u == "0"))
        })
        .unwrap_or(false)
}
