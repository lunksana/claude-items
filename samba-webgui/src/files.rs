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

pub async fn download(Query(q): Query<FsQuery>) -> Response {
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
    let mut resp = Response::builder()
        .header(header::CONTENT_TYPE, mime.as_ref())
        .header(header::CONTENT_LENGTH, meta.len())
        .header(header::CONTENT_DISPOSITION, disp)
        .header("X-Content-Type-Options", "nosniff");
    if inline {
        // 预览的 html/svg 等可能含脚本：sandbox 剥离同源与脚本执行
        resp = resp.header("Content-Security-Policy", "sandbox");
    }
    resp.body(Body::from_stream(ReaderStream::new(file))).unwrap()
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
        _ if req.action == "clear" => "u",
        _ => return err_json(StatusCode::BAD_REQUEST, "tag 必须是 user/group/other"),
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
    let res = match tokio::fs::metadata(&path).await {
        Ok(m) if m.is_dir() => tokio::fs::remove_dir_all(&path).await,
        Ok(_) => tokio::fs::remove_file(&path).await,
        Err(e) => return err_json(StatusCode::NOT_FOUND, &format!("不存在: {e}")),
    };
    match res {
        Ok(_) => Json(json!({ "ok": true })).into_response(),
        Err(e) => err_json(StatusCode::INTERNAL_SERVER_ERROR, &format!("删除失败: {e}")),
    }
}
