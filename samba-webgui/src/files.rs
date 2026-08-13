//! 共享目录内的文件浏览、上传、下载（含预览用 inline 响应）。

use axum::body::Body;
use axum::extract::{Multipart, Query, State};
use axum::http::{header, StatusCode};
use axum::response::{IntoResponse, Response};
use axum::Json;
use percent_encoding::{utf8_percent_encode, NON_ALPHANUMERIC};
use serde::{Deserialize, Serialize};
use serde_json::json;
use std::path::{Path, PathBuf};
use tokio_util::io::ReaderStream;

use crate::{err_json, SharedState};

#[derive(Deserialize)]
pub struct FsQuery {
    pub share: String,
    #[serde(default)]
    pub path: String,
    #[serde(default)]
    pub inline: Option<u8>,
}

#[derive(Serialize)]
pub struct Entry {
    name: String,
    is_dir: bool,
    size: u64,
    mtime: u64,
}

/// 把共享名 + 相对路径解析成共享根内的绝对路径；拒绝 .. 与越界符号链接
async fn resolve(share: &str, rel: &str) -> Result<(PathBuf, PathBuf), String> {
    let shares = crate::samba::list_all_shares_cached().await?;
    let s = shares
        .iter()
        .find(|s| s.name.eq_ignore_ascii_case(share))
        .ok_or_else(|| format!("共享不存在: {share}"))?;
    let base = std::fs::canonicalize(&s.path).map_err(|e| format!("共享路径不可用: {e}"))?;
    let mut full = base.clone();
    for comp in rel.split('/') {
        if comp.is_empty() || comp == "." {
            continue;
        }
        if comp == ".." || comp.contains('\0') {
            return Err("非法路径".into());
        }
        full.push(comp);
    }
    // 已存在的路径直接改用规范化结果，缩小校验与使用间的 TOCTOU 窗口
    if let Ok(canon) = std::fs::canonicalize(&full) {
        if !canon.starts_with(&base) {
            return Err("路径越界".into());
        }
        return Ok((base, canon));
    }
    // 目标尚不存在（upload/mkdir 新建）：向上找到最近的已存在祖先并规范化校验，
    // 防止中间某段是指向共享外的符号链接导致写入逃逸
    let mut existing = full.as_path();
    while !existing.exists() {
        existing = existing.parent().ok_or("非法路径")?;
    }
    let canon_existing = std::fs::canonicalize(existing).map_err(|e| format!("路径解析失败: {e}"))?;
    if !canon_existing.starts_with(&base) {
        return Err("路径越界".into());
    }
    // 剩余的不存在部分（已排除 .. 与符号链接）拼回规范化祖先，物理父目录必在 base 内
    let rest = full.strip_prefix(existing).unwrap_or(&full);
    Ok((base, canon_existing.join(rest)))
}

fn rand_suffix() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let nanos = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0);
    format!("{nanos:x}")
}

fn sanitize_filename(name: &str) -> Option<String> {
    let name = Path::new(name).file_name()?.to_str()?.to_string();
    if name.is_empty() || name == "." || name == ".." || name.contains('\0') {
        None
    } else {
        Some(name)
    }
}

/// 单个文件/目录的详细属性（供文件管理器右侧属性面板）
pub async fn stat(Query(q): Query<FsQuery>) -> Response {
    let (_, path) = match resolve(&q.share, &q.path).await {
        Ok(v) => v,
        Err(e) => return err_json(StatusCode::BAD_REQUEST, &e),
    };
    // 一次 stat 拿到 属主/属组/权限串/八进制/大小/mtime/类型；
    // 用 NUL(\0) 分隔（--printf 才会解释转义），避免用户名/组名含分隔符导致错位
    let out = tokio::process::Command::new("stat")
        .args(["--printf", "%U\\0%G\\0%A\\0%a\\0%s\\0%Y\\0%F", "--"])
        .arg(&path)
        .output()
        .await;
    let out = match out {
        Ok(o) if o.status.success() => o,
        _ => return err_json(StatusCode::NOT_FOUND, "无法读取属性"),
    };
    let line = String::from_utf8_lossy(&out.stdout);
    let f: Vec<&str> = line.split('\0').collect();
    if f.len() < 7 {
        return err_json(StatusCode::INTERNAL_SERVER_ERROR, "属性解析失败");
    }
    let name = path.file_name().map(|s| s.to_string_lossy().to_string()).unwrap_or_default();
    let is_dir = f[6].contains("directory");
    let mime = if is_dir { "inode/directory".to_string() } else { mime_guess::from_path(&path).first_or_octet_stream().to_string() };
    Json(json!({
        "name": name,
        "owner": f[0], "group": f[1],
        "perms": f[2], "mode": f[3],
        "size": f[4].parse::<u64>().unwrap_or(0),
        "mtime": f[5].parse::<u64>().unwrap_or(0),
        "ftype": f[6], "is_dir": is_dir, "mime": mime,
    })).into_response()
}

pub async fn list(Query(q): Query<FsQuery>) -> Response {
    let (_, dir) = match resolve(&q.share, &q.path).await {
        Ok(v) => v,
        Err(e) => return err_json(StatusCode::BAD_REQUEST, &e),
    };
    let mut rd = match tokio::fs::read_dir(&dir).await {
        Ok(v) => v,
        Err(e) => return err_json(StatusCode::BAD_REQUEST, &format!("无法读取目录: {e}")),
    };
    let mut entries = Vec::new();
    while let Ok(Some(ent)) = rd.next_entry().await {
        let name = ent.file_name().to_string_lossy().to_string();
        if let Ok(meta) = ent.metadata().await {
            entries.push(Entry {
                name,
                is_dir: meta.is_dir(),
                size: meta.len(),
                mtime: meta
                    .modified()
                    .ok()
                    .and_then(|t| t.duration_since(std::time::UNIX_EPOCH).ok())
                    .map(|d| d.as_secs())
                    .unwrap_or(0),
            });
        }
    }
    entries.sort_by(|a, b| (b.is_dir, a.name.to_lowercase()).cmp(&(a.is_dir, b.name.to_lowercase())));
    Json(json!({ "entries": entries })).into_response()
}

pub async fn download(Query(q): Query<FsQuery>, headers: axum::http::HeaderMap) -> Response {
    let (_, path) = match resolve(&q.share, &q.path).await {
        Ok(v) => v,
        Err(e) => return err_json(StatusCode::BAD_REQUEST, &e),
    };
    let meta = match tokio::fs::metadata(&path).await {
        Ok(m) if m.is_file() => m,
        Ok(_) => return err_json(StatusCode::BAD_REQUEST, "不是文件"),
        Err(e) => return err_json(StatusCode::NOT_FOUND, &format!("文件不存在: {e}")),
    };
    let file = match tokio::fs::File::open(&path).await {
        Ok(f) => f,
        Err(e) => return err_json(StatusCode::INTERNAL_SERVER_ERROR, &e.to_string()),
    };
    let filename = path.file_name().map(|s| s.to_string_lossy().to_string()).unwrap_or_default();
    let mime = mime_guess::from_path(&path).first_or_octet_stream();
    let inline = q.inline == Some(1);
    let disp = format!(
        "{}; filename*=UTF-8''{}",
        if inline { "inline" } else { "attachment" },
        utf8_percent_encode(&filename, NON_ALPHANUMERIC)
    );
    let size = meta.len();

    // 解析 Range（bytes=start-end / bytes=start- / bytes=-suffix），支持断点续传
    use axum::http::header::{ACCEPT_RANGES, CONTENT_RANGE};
    let (status, start, content_len, range_hdr) =
        if let Some(spec) = headers.get(header::RANGE).and_then(|v| v.to_str().ok()).and_then(|r| r.trim().strip_prefix("bytes=")) {
            let (s, e) = spec.split_once('-').unwrap_or((spec, ""));
            let s0 = s.trim().parse::<u64>().ok();
            let e0 = e.trim().parse::<u64>().ok();
            let (st, en) = match (s0, e0) {
                (Some(a), Some(b)) => (a, b.min(size.saturating_sub(1))),
                (Some(a), None) => (a, size.saturating_sub(1)),
                (None, Some(n)) => (size.saturating_sub(n.min(size)), size.saturating_sub(1)),
                (None, None) => (0, size.saturating_sub(1)),
            };
            if size == 0 || st >= size || st > en {
                return Response::builder()
                    .status(StatusCode::RANGE_NOT_SATISFIABLE)
                    .header(CONTENT_RANGE, format!("bytes */{size}"))
                    .body(Body::empty())
                    .unwrap();
            }
            (StatusCode::PARTIAL_CONTENT, st, en - st + 1, Some(format!("bytes {st}-{en}/{size}")))
        } else {
            (StatusCode::OK, 0, size, None)
        };

    let mut resp = Response::builder()
        .status(status)
        .header(header::CONTENT_TYPE, mime.as_ref())
        .header(header::CONTENT_LENGTH, content_len)
        .header(header::CONTENT_DISPOSITION, disp)
        .header("X-Content-Type-Options", "nosniff")
        .header(ACCEPT_RANGES, "bytes");
    if let Some(cr) = range_hdr {
        resp = resp.header(CONTENT_RANGE, cr);
    }
    if inline {
        // 预览的 html/svg 等可能含脚本：sandbox 剥离同源与脚本执行
        resp = resp.header("Content-Security-Policy", "sandbox");
    }
    if start > 0 {
        use tokio::io::AsyncSeekExt;
        let mut f = file;
        if let Err(e) = f.seek(std::io::SeekFrom::Start(start)).await {
            return err_json(StatusCode::INTERNAL_SERVER_ERROR, &format!("seek 失败: {e}"));
        }
        resp.body(Body::from_stream(ReaderStream::new(f))).unwrap()
    } else {
        resp.body(Body::from_stream(ReaderStream::new(file))).unwrap()
    }
}

pub async fn upload(
    State(_st): State<SharedState>,
    Query(q): Query<FsQuery>,
    mut multipart: Multipart,
) -> Response {
    let (_, dir) = match resolve(&q.share, &q.path).await {
        Ok(v) => v,
        Err(e) => return err_json(StatusCode::BAD_REQUEST, &e),
    };
    if !dir.is_dir() {
        return err_json(StatusCode::BAD_REQUEST, "目标不是目录");
    }
    let mut saved = Vec::new();
    loop {
        let field = match multipart.next_field().await {
            Ok(Some(f)) => f,
            Ok(None) => break,
            Err(e) => return err_json(StatusCode::BAD_REQUEST, &format!("上传解析失败: {e}")),
        };
        let Some(name) = field.file_name().and_then(sanitize_filename) else {
            continue;
        };
        let target = dir.join(&name);
        // 先写唯一临时文件，成功后原子 rename 覆盖：
        // 中途失败不会截断/清空已存在的同名文件，并发同名上传也不会写花
        let tmp = dir.join(format!(".swg-upload-{}-{}.part", std::process::id(), rand_suffix()));
        let mut file = match tokio::fs::File::create(&tmp).await {
            Ok(f) => f,
            Err(e) => return err_json(StatusCode::INTERNAL_SERVER_ERROR, &format!("创建文件失败: {e}")),
        };
        let mut field = field;
        use tokio::io::AsyncWriteExt;
        loop {
            match field.chunk().await {
                Ok(Some(chunk)) => {
                    if let Err(e) = file.write_all(&chunk).await {
                        drop(file);
                        let _ = tokio::fs::remove_file(&tmp).await;
                        return err_json(StatusCode::INTERNAL_SERVER_ERROR, &format!("写入失败: {e}"));
                    }
                }
                Ok(None) => break,
                Err(e) => {
                    drop(file);
                    let _ = tokio::fs::remove_file(&tmp).await;
                    return err_json(StatusCode::BAD_REQUEST, &format!("上传中断: {e}"));
                }
            }
        }
        if let Err(e) = file.sync_all().await {
            let _ = tokio::fs::remove_file(&tmp).await;
            return err_json(StatusCode::INTERNAL_SERVER_ERROR, &format!("落盘失败: {e}"));
        }
        drop(file);
        if let Err(e) = tokio::fs::rename(&tmp, &target).await {
            let _ = tokio::fs::remove_file(&tmp).await;
            return err_json(StatusCode::INTERNAL_SERVER_ERROR, &format!("保存失败: {e}"));
        }
        saved.push(name);
    }
    Json(json!({ "ok": true, "saved": saved })).into_response()
}

// ---- POSIX ACL ----

#[derive(Serialize)]
struct AclEntry {
    tag: String,       // user / group / mask / other
    qualifier: String, // 空 = 基础条目（属主/属组/other）
    perms: String,     // rwx 形式
    default: bool,     // 是否为默认 ACL（目录继承）
    /// mask 裁剪后的实际生效权限（与 perms 不同才有值）
    #[serde(skip_serializing_if = "Option::is_none")]
    effective: Option<String>,
}

/// 解析 getfacl 输出
fn parse_getfacl(text: &str) -> (String, String, Vec<AclEntry>) {
    let mut owner = String::new();
    let mut group = String::new();
    let mut entries = Vec::new();
    for line in text.lines() {
        let line = line.trim();
        if let Some(v) = line.strip_prefix("# owner:") {
            owner = v.trim().to_string();
        } else if let Some(v) = line.strip_prefix("# group:") {
            group = v.trim().to_string();
        } else if line.is_empty() || line.starts_with('#') {
            continue;
        } else {
            // [default:]tag:qualifier:perms[ \t#effective:r--]
            let effective = line
                .split_once("#effective:")
                .map(|(_, e)| e.trim().to_string());
            let entry = line.split_whitespace().next().unwrap_or("");
            let (entry, default) = match entry.strip_prefix("default:") {
                Some(rest) => (rest, true),
                None => (entry, false),
            };
            let parts: Vec<&str> = entry.splitn(3, ':').collect();
            if parts.len() == 3 {
                entries.push(AclEntry {
                    tag: parts[0].to_string(),
                    qualifier: parts[1].to_string(),
                    perms: parts[2].to_string(),
                    default,
                    effective: effective.filter(|e| e != parts[2]),
                });
            }
        }
    }
    (owner, group, entries)
}

pub async fn acl_get(Query(q): Query<FsQuery>) -> Response {
    let (_, path) = match resolve(&q.share, &q.path).await {
        Ok(v) => v,
        Err(e) => return err_json(StatusCode::BAD_REQUEST, &e),
    };
    let out = match tokio::process::Command::new("getfacl")
        .args(["--absolute-names", "-p", "--"])
        .arg(&path)
        .output()
        .await
    {
        Ok(o) => o,
        Err(e) => return err_json(StatusCode::INTERNAL_SERVER_ERROR, &format!("getfacl 不可用: {e}")),
    };
    if !out.status.success() {
        return err_json(StatusCode::BAD_REQUEST, &String::from_utf8_lossy(&out.stderr));
    }
    let (owner, group, entries) = parse_getfacl(&String::from_utf8_lossy(&out.stdout));
    let is_dir = path.is_dir();
    Json(json!({ "owner": owner, "group": group, "is_dir": is_dir, "entries": entries })).into_response()
}

#[derive(Deserialize)]
pub struct AclSetReq {
    share: String,
    path: String,
    /// set / remove / clear
    action: String,
    #[serde(default)]
    tag: String, // user / group / other
    #[serde(default)]
    qualifier: String,
    #[serde(default)]
    perms: String,
    #[serde(default)]
    recursive: bool,
    /// 默认 ACL（仅目录，控制新建文件继承）
    #[serde(default)]
    default_acl: bool,
}

fn valid_perms(p: &str) -> bool {
    !p.is_empty() && p.len() <= 3 && p.chars().all(|c| matches!(c, 'r' | 'w' | 'x' | '-'))
}

fn valid_qualifier(q: &str) -> bool {
    q.is_empty()
        || (q.len() <= 32
            && q.chars()
                .all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.')))
}

pub async fn acl_set(Json(req): Json<AclSetReq>) -> Response {
    let (_, path) = match resolve(&req.share, &req.path).await {
        Ok(v) => v,
        Err(e) => return err_json(StatusCode::BAD_REQUEST, &e),
    };
    if !valid_qualifier(&req.qualifier) {
        return err_json(StatusCode::BAD_REQUEST, "非法的用户/组名");
    }
    let tag_char = match req.tag.as_str() {
        "user" => "u",
        "group" => "g",
        "other" => "o",
        "mask" => "m",
        _ if req.action == "clear" => "u",
        _ => return err_json(StatusCode::BAD_REQUEST, "tag 必须是 user/group/other/mask"),
    };
    let dflt = if req.default_acl { "d:" } else { "" };
    let mut cmd = tokio::process::Command::new("setfacl");
    if req.recursive {
        cmd.arg("-R");
    }
    match req.action.as_str() {
        "set" => {
            if !valid_perms(&req.perms) {
                return err_json(StatusCode::BAD_REQUEST, "非法权限（rwx- 组合）");
            }
            cmd.arg("-m").arg(format!("{dflt}{tag_char}:{}:{}", req.qualifier, req.perms));
        }
        "remove" => {
            if req.qualifier.is_empty() {
                return err_json(StatusCode::BAD_REQUEST, "基础条目不可删除");
            }
            cmd.arg("-x").arg(format!("{dflt}{tag_char}:{}", req.qualifier));
        }
        "clear" => {
            cmd.arg("-b");
        }
        _ => return err_json(StatusCode::BAD_REQUEST, "action 必须是 set/remove/clear"),
    }
    cmd.arg("--").arg(&path);
    let out = match cmd.output().await {
        Ok(o) => o,
        Err(e) => return err_json(StatusCode::INTERNAL_SERVER_ERROR, &format!("setfacl 不可用: {e}")),
    };
    if !out.status.success() {
        return err_json(StatusCode::BAD_REQUEST, &String::from_utf8_lossy(&out.stderr));
    }
    Json(json!({ "ok": true })).into_response()
}

#[derive(Deserialize)]
pub struct MkdirReq {
    share: String,
    #[serde(default)]
    path: String,
    name: String,
}

pub async fn mkdir(Json(req): Json<MkdirReq>) -> Response {
    let Some(name) = sanitize_filename(&req.name) else {
        return err_json(StatusCode::BAD_REQUEST, "非法目录名");
    };
    let (_, dir) = match resolve(&req.share, &req.path).await {
        Ok(v) => v,
        Err(e) => return err_json(StatusCode::BAD_REQUEST, &e),
    };
    match tokio::fs::create_dir(dir.join(&name)).await {
        Ok(_) => Json(json!({ "ok": true })).into_response(),
        Err(e) => err_json(StatusCode::BAD_REQUEST, &format!("创建失败: {e}")),
    }
}

#[derive(Deserialize)]
pub struct DeleteReq {
    share: String,
    path: String,
}

pub async fn delete(Json(req): Json<DeleteReq>) -> Response {
    if req.path.trim_matches('/').is_empty() {
        return err_json(StatusCode::BAD_REQUEST, "不能删除共享根目录");
    }
    let (base, path) = match resolve(&req.share, &req.path).await {
        Ok(v) => v,
        Err(e) => return err_json(StatusCode::BAD_REQUEST, &e),
    };
    if path == base {
        return err_json(StatusCode::BAD_REQUEST, "不能删除共享根目录");
    }
    // 删除 = 移入共享根下 .recycle/<时间戳>/<相对路径>，可在文件管理器中手动移回恢复；
    // rename 移动符号链接时只移动链接本身（不追进链接目标），目录整棵移动而非递归删除
    let rel = match path.strip_prefix(&base) {
        Ok(r) if !r.as_os_str().is_empty() => r.to_path_buf(),
        _ => return err_json(StatusCode::BAD_REQUEST, "路径解析失败"),
    };
    let ts = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0);
    let recycle_dir = base.join(".recycle").join(ts.to_string());
    let mut dest = recycle_dir.join(&rel);
    if dest.exists() {
        // 同秒重复删除同名项目：加随机后缀避免覆盖
        dest = recycle_dir.join(format!("{ts}-{}", rand_suffix())).join(&rel);
    }
    if let Some(p) = dest.parent() {
        if let Err(e) = tokio::fs::create_dir_all(p).await {
            return err_json(StatusCode::INTERNAL_SERVER_ERROR, &format!("创建回收站目录失败: {e}"));
        }
    }
    match tokio::fs::rename(&path, &dest).await {
        Ok(_) => {
            crate::audit("files_delete", &format!("share={} path={} → .recycle", req.share, req.path));
            Json(json!({ "ok": true, "message": "已移入共享回收站 .recycle（可在文件管理器中找到并移回）" })).into_response()
        }
        Err(e) => err_json(StatusCode::INTERNAL_SERVER_ERROR, &format!("移入回收站失败: {e}")),
    }
}

#[derive(Deserialize)]
pub struct TreeReq {
    #[serde(default)]
    pub path: String,
}

pub async fn tree_list(Query(req): Query<TreeReq>) -> Response {
    let path = if req.path.trim().is_empty() {
        // 返回常用起始目录
        let mut roots = Vec::new();
        for candidate in ["/", "/srv", "/mnt", "/data", "/home", "/var", "/opt", "/root"] {
            if Path::new(candidate).is_dir() {
                roots.push(candidate.to_string());
            }
        }
        return Json(json!({ "path": "", "dirs": roots })).into_response();
    } else {
        req.path.trim().to_string()
    };

    if !path.starts_with('/') || path.contains("..") || path.contains('\0') {
        return err_json(StatusCode::BAD_REQUEST, "非法路径");
    }

    let p = Path::new(&path);
    let mut dirs = Vec::new();
    if let Ok(mut entries) = tokio::fs::read_dir(p).await {
        while let Ok(Some(entry)) = entries.next_entry().await {
            if let Ok(ft) = entry.file_type().await {
                if ft.is_dir() {
                    let name = entry.file_name().to_string_lossy().to_string();
                    if !name.starts_with('.') {
                        dirs.push(name);
                    }
                }
            }
        }
    }
    dirs.sort_by(|a, b| a.to_lowercase().cmp(&b.to_lowercase()));
    Json(json!({ "path": path, "dirs": dirs })).into_response()
}

#[derive(Deserialize)]
pub struct TreeMkdirReq {
    pub path: String,
    pub name: String,
}

pub async fn tree_mkdir(Json(req): Json<TreeMkdirReq>) -> Response {
    if !req.path.starts_with('/') || req.path.contains("..") || req.path.contains('\0') {
        return err_json(StatusCode::BAD_REQUEST, "非法路径");
    }
    let Some(name) = sanitize_filename(&req.name) else {
        return err_json(StatusCode::BAD_REQUEST, "非法目录名");
    };
    let full = Path::new(&req.path).join(&name);
    match tokio::fs::create_dir_all(&full).await {
        Ok(_) => Json(json!({ "ok": true, "path": full.to_string_lossy() })).into_response(),
        Err(e) => err_json(StatusCode::BAD_REQUEST, &format!("创建目录失败: {e}")),
    }
}

#[derive(Deserialize)]
pub struct CopyMoveReq {
    pub src_share: String,
    pub src_path: String,
    pub dst_share: String,
    pub dst_path: String,
}

pub async fn copy_file(Json(req): Json<CopyMoveReq>) -> Response {
    let (_, src) = match resolve(&req.src_share, &req.src_path).await {
        Ok(v) => v,
        Err(e) => return err_json(StatusCode::BAD_REQUEST, &e),
    };
    let (_, dst_parent) = match resolve(&req.dst_share, &req.dst_path).await {
        Ok(v) => v,
        Err(e) => return err_json(StatusCode::BAD_REQUEST, &e),
    };
    let file_name = match src.file_name() {
        Some(n) => n,
        None => return err_json(StatusCode::BAD_REQUEST, "无效源文件名"),
    };
    let dst = dst_parent.join(file_name);
    if src == dst {
        return err_json(StatusCode::BAD_REQUEST, "源和目标路径相同");
    }
    // 目标若已是符号链接，cp 会穿透写入链接指向的文件（可越出共享），必须拒绝
    if tokio::fs::symlink_metadata(&dst).await.map(|m| m.file_type().is_symlink()).unwrap_or(false) {
        return err_json(StatusCode::BAD_REQUEST, "目标位置已存在同名符号链接，已拒绝复制");
    }
    let out = tokio::process::Command::new("cp")
        .args(["-a", "--", src.to_str().unwrap_or(""), dst.to_str().unwrap_or("")])
        .output()
        .await;
    match out {
        Ok(o) if o.status.success() => Json(json!({ "ok": true })).into_response(),
        Ok(o) => err_json(StatusCode::BAD_REQUEST, &format!("复制失败: {}", String::from_utf8_lossy(&o.stderr))),
        Err(e) => err_json(StatusCode::INTERNAL_SERVER_ERROR, &format!("cp 命令执行失败: {e}")),
    }
}

pub async fn move_file(Json(req): Json<CopyMoveReq>) -> Response {
    let (src_base, src) = match resolve(&req.src_share, &req.src_path).await {
        Ok(v) => v,
        Err(e) => return err_json(StatusCode::BAD_REQUEST, &e),
    };
    if src == src_base {
        return err_json(StatusCode::BAD_REQUEST, "不能移动/剪切共享根目录");
    }
    let (_, dst_parent) = match resolve(&req.dst_share, &req.dst_path).await {
        Ok(v) => v,
        Err(e) => return err_json(StatusCode::BAD_REQUEST, &e),
    };
    let file_name = match src.file_name() {
        Some(n) => n,
        None => return err_json(StatusCode::BAD_REQUEST, "无效源文件名"),
    };
    let dst = dst_parent.join(file_name);
    if src == dst {
        return err_json(StatusCode::BAD_REQUEST, "源和目标路径相同");
    }
    if tokio::fs::rename(&src, &dst).await.is_ok() {
        return Json(json!({ "ok": true })).into_response();
    }
    // rename 失败走 cp 回退：目标若已是符号链接，cp 会穿透写入链接指向的文件，必须拒绝
    if tokio::fs::symlink_metadata(&dst).await.map(|m| m.file_type().is_symlink()).unwrap_or(false) {
        return err_json(StatusCode::BAD_REQUEST, "目标位置已存在同名符号链接，已拒绝移动");
    }
    let out = tokio::process::Command::new("cp")
        .args(["-a", "--", src.to_str().unwrap_or(""), dst.to_str().unwrap_or("")])
        .output()
        .await;
    match out {
        Ok(o) if o.status.success() => {
            let _ = if src.is_dir() {
                tokio::fs::remove_dir_all(&src).await
            } else {
                tokio::fs::remove_file(&src).await
            };
            Json(json!({ "ok": true })).into_response()
        }
        Ok(o) => err_json(StatusCode::BAD_REQUEST, &format!("移动失败: {}", String::from_utf8_lossy(&o.stderr))),
        Err(e) => err_json(StatusCode::INTERNAL_SERVER_ERROR, &format!("移动执行失败: {e}")),
    }
}

#[derive(Deserialize)]
pub struct ExtractReq {
    pub share: String,
    pub path: String,
}

/// 检查链接目标是否越界：绝对路径或含 .. 组件的目标一律拒绝
/// （软链接/硬链接目标越界会让解压产物指向共享外，之后可经下载读到任意文件）
fn link_target_unsafe(target: &str) -> bool {
    let t = target.trim();
    t.starts_with('/') || t.split('/').any(|c| c == "..")
}

pub async fn extract_archive(Json(req): Json<ExtractReq>) -> Response {
    let (_, full) = match resolve(&req.share, &req.path).await {
        Ok(v) => v,
        Err(e) => return err_json(StatusCode::BAD_REQUEST, &e),
    };
    let parent = full.parent().unwrap_or(Path::new("/"));
    let path_str = full.to_string_lossy().to_string();
    let lower = path_str.to_lowercase();
    let is_zip = lower.ends_with(".zip");
    let is_tar = lower.ends_with(".tar.gz") || lower.ends_with(".tgz")
        || lower.ends_with(".tar.bz2") || lower.ends_with(".tbz2")
        || lower.ends_with(".tar.xz") || lower.ends_with(".txz")
        || lower.ends_with(".tar");
    if !is_zip && !is_tar {
        return err_json(StatusCode::BAD_REQUEST, "暂不支持此格式的解压缩包");
    }
    // 先列出包内成员，拒绝绝对路径或含 .. 的成员（Zip Slip 目录穿越）
    let list = if is_zip {
        tokio::process::Command::new("unzip").args(["-Z1", "--", &path_str]).output().await
    } else {
        tokio::process::Command::new("tar").args(["-tf", &path_str]).output().await
    };
    match list {
        Ok(o) if o.status.success() => {
            for name in String::from_utf8_lossy(&o.stdout).lines() {
                let n = name.trim();
                if n.starts_with('/') || n.split('/').any(|c| c == "..") {
                    return err_json(StatusCode::BAD_REQUEST, &format!("压缩包含越界路径，已拒绝解压: {n}"));
                }
            }
        }
        Ok(o) => return err_json(StatusCode::BAD_REQUEST, &format!("无法读取压缩包清单: {}", String::from_utf8_lossy(&o.stderr).trim())),
        Err(e) => return err_json(StatusCode::INTERNAL_SERVER_ERROR, &format!("解压工具不存在: {e}")),
    }

    // 再查符号链接/硬链接目标（tar 详细清单的 " -> target" / " link to target"，
    // unzip 详细清单对 Unix symlink 条目同样显示 " -> target"）
    let linklist = if is_zip {
        tokio::process::Command::new("unzip").args(["-Z", "-v", "--", &path_str]).output().await
    } else {
        tokio::process::Command::new("tar").args(["-tvvf", "--", &path_str]).output().await
    };
    match linklist {
        Ok(o) if o.status.success() => {
            for line in String::from_utf8_lossy(&o.stdout).lines() {
                let target = line
                    .rfind(" -> ")
                    .map(|i| &line[i + 4..])
                    .or_else(|| line.rfind(" link to ").map(|i| &line[i + 9..]));
                if let Some(t) = target {
                    if link_target_unsafe(t) {
                        return err_json(StatusCode::BAD_REQUEST, &format!("压缩包含越界链接目标，已拒绝解压: {}", t.trim()));
                    }
                }
            }
        }
        Ok(o) => return err_json(StatusCode::BAD_REQUEST, &format!("无法读取压缩包链接清单: {}", String::from_utf8_lossy(&o.stderr).trim())),
        Err(e) => return err_json(StatusCode::INTERNAL_SERVER_ERROR, &format!("解压工具不存在: {e}")),
    }

    let mut cmd = if is_zip {
        let mut c = tokio::process::Command::new("unzip");
        c.args(["-q", "-o", "--", &path_str]);
        c
    } else {
        // --no-same-owner/permissions：root 解压时不保留包内 UID/权限，避免属主劫持
        let flag = if lower.ends_with(".bz2") || lower.ends_with(".tbz2") { "-xjf" }
            else if lower.ends_with(".xz") || lower.ends_with(".txz") { "-xJf" }
            else if lower.ends_with(".gz") || lower.ends_with(".tgz") { "-xzf" }
            else { "-xf" };
        let mut c = tokio::process::Command::new("tar");
        c.args(["--no-same-owner", "--no-same-permissions", flag, &path_str]);
        c
    };
    cmd.current_dir(parent);
    match cmd.output().await {
        Ok(o) if o.status.success() => {
            crate::audit("files_extract", &format!("share={} path={}", req.share, req.path));
            Json(json!({ "ok": true })).into_response()
        }
        Ok(o) => err_json(StatusCode::BAD_REQUEST, &format!("解压错误: {}", String::from_utf8_lossy(&o.stderr).trim())),
        Err(e) => err_json(StatusCode::INTERNAL_SERVER_ERROR, &format!("解压工具不存在或执行失败: {e}")),
    }
}

#[derive(Deserialize)]
pub struct ArchiveReq {
    pub share: String,
    pub path: String,
    pub items: Vec<String>,
    pub archive_name: String,
}

pub async fn create_archive(Json(req): Json<ArchiveReq>) -> Response {
    if req.items.is_empty() {
        return err_json(StatusCode::BAD_REQUEST, "未选择要打包的文件或目录");
    }
    let (_, dir) = match resolve(&req.share, &req.path).await {
        Ok(v) => v,
        Err(e) => return err_json(StatusCode::BAD_REQUEST, &e),
    };
    let Some(arch_name) = sanitize_filename(&req.archive_name) else {
        return err_json(StatusCode::BAD_REQUEST, "非法打包文件名");
    };
    let mut args = vec!["-czf", &arch_name, "--"];
    for item in &req.items {
        if item.is_empty() || item.contains('/') || item == "." || item == ".." || item.contains('\0') {
            return err_json(StatusCode::BAD_REQUEST, "包含非法选择项目");
        }
        args.push(item);
    }
    let mut cmd = tokio::process::Command::new("tar");
    cmd.args(&args).current_dir(&dir);
    match cmd.output().await {
        Ok(o) if o.status.success() => {
            crate::audit("files_archive", &format!("share={} path={} name={}", req.share, req.path, arch_name));
            Json(json!({ "ok": true })).into_response()
        }
        Ok(o) => err_json(StatusCode::BAD_REQUEST, &format!("打包错误: {}", String::from_utf8_lossy(&o.stderr).trim())),
        Err(e) => err_json(StatusCode::INTERNAL_SERVER_ERROR, &format!("执行 tar 失败: {e}")),
    }
}
