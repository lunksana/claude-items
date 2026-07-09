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
    if let Ok(text) = std::fs::read_to_string(CONFIG_PATH) {
        if let Ok(mut cfg) = serde_json::from_str::<AppConfig>(&text) {
            if !cfg.password_hash.is_empty() {
                if cfg.session_ttl_hours == 0 {
                    cfg.session_ttl_hours = 24;
                }
                return cfg;
            }
        } else if let Ok(v) = serde_json::from_str::<serde_json::Value>(&text) {
            if let Some(h) = v.get("password_hash").and_then(|h| h.as_str()) {
                return AppConfig {
                    password_hash: h.to_string(),
                    listen_addr: default_listen(),
                    session_ttl_hours: default_ttl(),
                    guest_map_bad_user: false,
                };
            }
        }
    }
    let hash = hash_password(DEFAULT_PASSWORD);
    let cfg = AppConfig {
        password_hash: hash,
        listen_addr: default_listen(),
        session_ttl_hours: default_ttl(),
        guest_map_bad_user: false,
    };
    let _ = std::fs::create_dir_all("data");
    let _ = std::fs::write(CONFIG_PATH, serde_json::to_string_pretty(&cfg).unwrap());
    eprintln!("⚠ 已生成默认登录密码: {DEFAULT_PASSWORD}（请登录后立即修改）");
    cfg
}

fn save_config(cfg: &AppConfig) {
    let _ = std::fs::create_dir_all("data");
    let _ = std::fs::write(CONFIG_PATH, serde_json::to_string_pretty(cfg).unwrap());
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
        tokio::time::sleep(Duration::from_millis(800)).await;
        return err_json(StatusCode::UNAUTHORIZED, "密码错误");
    }
    st.login_fails.lock().unwrap().remove(&ip);
    let tok = new_token();
    {
        let ttl = Duration::from_secs(st.config.lock().unwrap().session_ttl_hours * 3600);
        let mut sessions = st.sessions.lock().unwrap();
        sessions.retain(|_, exp| exp.elapsed() < ttl);
        sessions.insert(tok.clone(), Instant::now());
    }
    Response::builder()
        .header(
            header::SET_COOKIE,
            format!("sid={tok}; HttpOnly; SameSite=Strict; Path=/; Max-Age=86400"),
        )
        .header(header::CONTENT_TYPE, "application/json")
        .body(json!({ "ok": true }).to_string().into())
        .unwrap()
}

async fn logout(State(st): State<SharedState>, req: Request) -> Response {
    if let Some(tok) = get_cookie(&req, "sid") {
        st.sessions.lock().unwrap().remove(&tok);
    }
    Response::builder()
        .header(header::SET_COOKIE, "sid=; HttpOnly; Path=/; Max-Age=0")
        .header(header::CONTENT_TYPE, "application/json")
        .body(json!({ "ok": true }).to_string().into())
        .unwrap()
}

async fn me() -> Response {
    Json(json!({ "ok": true })).into_response()
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
    save_config(&cfg);
    Json(json!({ "ok": true })).into_response()
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
    match samba::save_managed(&managed).await {
        Ok(msg) => Json(json!({ "ok": true, "message": apply_fix_perms(&share, msg).await })).into_response(),
        Err(e) => err_json(StatusCode::BAD_REQUEST, &e),
    }
}

async fn apply_fix_perms(share: &samba::Share, msg: String) -> String {
    if !share.fix_perms {
        return msg;
    }
    match samba::fix_share_perms(share).await {
        Ok(m) => format!("{msg}；{m}"),
        Err(e) => format!("{msg}；但权限修正失败: {e}"),
    }
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
    match samba::save_managed(&managed).await {
        Ok(msg) => Json(json!({ "ok": true, "message": apply_fix_perms(&share, msg).await })).into_response(),
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
    match samba::save_managed(&managed).await {
        Ok(msg) => Json(json!({ "ok": true, "message": msg })).into_response(),
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
}

async fn user_create(Json(req): Json<UserCreateReq>) -> Response {
    match samba::create_user(&req.username, &req.password).await {
        Ok(_) => Json(json!({ "ok": true })).into_response(),
        Err(e) => err_json(StatusCode::BAD_REQUEST, &e),
    }
}

#[derive(Deserialize)]
struct UserPwReq {
    password: String,
}

async fn user_password(AxPath(name): AxPath<String>, Json(req): Json<UserPwReq>) -> Response {
    match samba::set_user_password(&name, &req.password).await {
        Ok(_) => Json(json!({ "ok": true })).into_response(),
        Err(e) => err_json(StatusCode::BAD_REQUEST, &e),
    }
}

#[derive(Deserialize)]
struct UserEnableReq {
    enabled: bool,
}

async fn user_enable(AxPath(name): AxPath<String>, Json(req): Json<UserEnableReq>) -> Response {
    match samba::set_user_enabled(&name, req.enabled).await {
        Ok(_) => Json(json!({ "ok": true })).into_response(),
        Err(e) => err_json(StatusCode::BAD_REQUEST, &e),
    }
}

async fn user_delete(AxPath(name): AxPath<String>) -> Response {
    match samba::delete_user(&name).await {
        Ok(_) => Json(json!({ "ok": true })).into_response(),
        Err(e) => err_json(StatusCode::BAD_REQUEST, &e),
    }
}

#[derive(Deserialize)]
struct ConfigUpdateReq {
    listen_addr: String,
    session_ttl_hours: u64,
    guest_map_bad_user: bool,
}

async fn config_get(State(st): State<SharedState>) -> Response {
    let cfg = st.config.lock().unwrap().clone();
    Json(json!({
        "listen_addr": cfg.listen_addr,
        "session_ttl_hours": cfg.session_ttl_hours,
        "guest_map_bad_user": cfg.guest_map_bad_user,
    })).into_response()
}

async fn config_update(State(st): State<SharedState>, Json(req): Json<ConfigUpdateReq>) -> Response {
    if req.session_ttl_hours < 1 || req.session_ttl_hours > 720 {
        return err_json(StatusCode::BAD_REQUEST, "会话超时时长必须在 1~720 小时之间");
    }
    if req.listen_addr.trim().is_empty() {
        return err_json(StatusCode::BAD_REQUEST, "监听地址不能为空");
    }
    let old_guest;
    {
        let mut cfg = st.config.lock().unwrap();
        old_guest = cfg.guest_map_bad_user;
        cfg.listen_addr = req.listen_addr.trim().to_string();
        cfg.session_ttl_hours = req.session_ttl_hours;
        cfg.guest_map_bad_user = req.guest_map_bad_user;
        save_config(&cfg);
    }

    if old_guest != req.guest_map_bad_user {
        if let Ok(conf) = std::fs::read_to_string(samba::SMB_CONF) {
            let mut out = Vec::new();
            let mut in_global = false;
            let mut found = false;
            for line in conf.lines() {
                let trimmed = line.trim();
                if trimmed.starts_with('[') && trimmed.ends_with(']') {
                    in_global = trimmed[1..trimmed.len() - 1].trim().eq_ignore_ascii_case("global");
                }
                if in_global && trimmed.to_lowercase().starts_with("map to guest") {
                    let val = if req.guest_map_bad_user { "Bad User" } else { "Never" };
                    out.push(format!("   map to guest = {val}"));
                    found = true;
                } else {
                    out.push(line.to_string());
                }
            }
            if !found {
                let mut out2 = Vec::new();
                for line in out {
                    out2.push(line.clone());
                    let trimmed = line.trim();
                    if trimmed.starts_with('[') && trimmed.ends_with(']') && trimmed[1..trimmed.len()-1].trim().eq_ignore_ascii_case("global") {
                        let val = if req.guest_map_bad_user { "Bad User" } else { "Never" };
                        out2.push(format!("   map to guest = {val}"));
                    }
                }
                out = out2;
            }
            let _ = std::fs::write(samba::SMB_CONF, out.join("\n"));
            let _ = samba::service_action("reload").await;
        }
    }

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
        Ok(_) => Json(json!({ "ok": true })).into_response(),
        Err(e) => err_json(StatusCode::BAD_REQUEST, &e),
    }
}

#[derive(Deserialize)]
struct ServiceReq {
    action: String,
}

async fn status_service(Json(req): Json<ServiceReq>) -> Response {
    match samba::service_action(&req.action).await {
        Ok(msg) => Json(json!({ "ok": true, "msg": msg })).into_response(),
        Err(e) => err_json(StatusCode::BAD_REQUEST, &e),
    }
}

async fn share_migrate(AxPath(name): AxPath<String>) -> Response {
    match samba::migrate_share_from_main(&name).await {
        Ok(msg) => Json(json!({ "ok": true, "msg": msg })).into_response(),
        Err(e) => err_json(StatusCode::BAD_REQUEST, &e),
    }
}

async fn groups_list() -> Response {
    match samba::list_groups().await {
        Ok(g) => Json(g).into_response(),
        Err(e) => err_json(StatusCode::INTERNAL_SERVER_ERROR, &e),
    }
}

#[derive(Deserialize)]
struct UserGroupsReq {
    groups: Vec<String>,
}

async fn user_groups_update(AxPath(name): AxPath<String>, Json(req): Json<UserGroupsReq>) -> Response {
    match samba::set_user_groups(&name, &req.groups).await {
        Ok(_) => Json(json!({ "ok": true })).into_response(),
        Err(e) => err_json(StatusCode::BAD_REQUEST, &e),
    }
}

// ---- 静态页面 ----

async fn index() -> Html<&'static str> {
    Html(include_str!("../static/index.html"))
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
        eprintln!("⚠ 无法接管 smb.conf include: {e}");
    }

    let cfg = load_or_init_config();
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
        .route("/api/groups", get(groups_list))
        .route("/api/users/{name}/groups", put(user_groups_update))
        .route("/api/files", get(files::list))
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
