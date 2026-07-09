"use strict";

const $ = (id) => document.getElementById(id);

// ---------- 基础请求 ----------
async function api(path, opts = {}) {
  if (opts.json !== undefined) {
    opts.method = opts.method || "POST";
    opts.headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
    opts.body = JSON.stringify(opts.json);
    delete opts.json;
  }
  const res = await fetch(path, opts);
  if (res.status === 401 && path !== "/api/login") {
    showLogin();
    throw new Error("未登录");
  }
  let data = {};
  try { data = await res.json(); } catch (_) {}
  if (!res.ok) throw new Error(data.error || `请求失败 (${res.status})`);
  return data;
}

let toastTimer = null;
function toast(msg, isErr = false) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast" + (isErr ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 3200);
}

function esc(s) {
  return String(s).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fmtSize(n) {
  if (n < 1024) return n + " B";
  const units = ["KB", "MB", "GB", "TB"];
  let i = -1;
  do { n /= 1024; i++; } while (n >= 1024 && i < units.length - 1);
  return n.toFixed(n >= 100 ? 0 : 1) + " " + units[i];
}

function fmtTime(sec) {
  if (!sec) return "-";
  const d = new Date(sec * 1000);
  const p = (x) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}

// ---------- 视图切换 ----------
function showLogin() {
  $("login-view").classList.remove("hidden");
  $("main-view").classList.add("hidden");
}
function showMain() {
  $("login-view").classList.add("hidden");
  $("main-view").classList.remove("hidden");
  loadShares();
  loadUsers();
  refreshShareSelect();
}

let activeTab = "shares";
document.querySelectorAll(".nav-item").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".nav-item").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    activeTab = btn.dataset.tab;
    ["shares", "users", "files"].forEach((t) =>
      $("tab-" + t).classList.toggle("hidden", t !== activeTab));
    if (activeTab === "shares") loadShares();
    if (activeTab === "users") loadUsers();
    if (activeTab === "files") refreshShareSelect();
  });
});

// ---------- 登录 ----------
$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("login-error").textContent = "";
  try {
    await api("/api/login", { json: { password: $("login-password").value } });
    $("login-password").value = "";
    showMain();
  } catch (err) {
    $("login-error").textContent = err.message;
  }
});

$("btn-logout").addEventListener("click", async () => {
  try { await api("/api/logout", { method: "POST" }); } catch (_) {}
  showLogin();
});

// 修改管理密码
$("btn-change-pw").addEventListener("click", () => $("pw-dialog").showModal());
$("pw-cancel").addEventListener("click", () => $("pw-dialog").close());
$("pw-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/password", { json: { old_password: $("pw-old").value, new_password: $("pw-new").value } });
    $("pw-dialog").close();
    $("pw-old").value = $("pw-new").value = "";
    toast("管理密码已修改");
  } catch (err) { toast(err.message, true); }
});

// ---------- 共享管理 ----------
let editingShare = null; // null = 新建

async function loadShares() {
  try {
    const { shares } = await api("/api/shares");
    const tbody = $("shares-tbody");
    tbody.innerHTML = shares.map((s) => `
      <tr>
        <td><b>${esc(s.name)}</b></td>
        <td class="muted">${esc(s.path)}</td>
        <td>${esc(s.comment || "-")}</td>
        <td>${s.read_only ? '<span class="badge off">只读</span>' : '<span class="badge on">可写</span>'}</td>
        <td>${s.guest_ok ? '<span class="badge warn">允许</span>' : '<span class="badge off">禁止</span>'}</td>
        <td>${s.browseable ? '<span class="badge on">可见</span>' : '<span class="badge off">隐藏</span>'}</td>
        <td class="muted">${esc(s.valid_users || "所有人")}</td>
        <td>${s.managed ? '<span class="badge on">本工具</span>' : '<span class="badge off">主配置</span>'}</td>
        <td class="ops">${s.managed ? `
          <button class="btn mini" data-edit="${esc(s.name)}">编辑</button>
          <button class="btn mini danger" data-del="${esc(s.name)}">删除</button>` : '<span class="muted small">只读</span>'}
        </td>
      </tr>`).join("");
    tbody.querySelectorAll("[data-edit]").forEach((b) =>
      b.addEventListener("click", () => openShareDialog(shares.find((s) => s.name === b.dataset.edit))));
    tbody.querySelectorAll("[data-del]").forEach((b) =>
      b.addEventListener("click", async () => {
        if (!confirm(`确定删除共享「${b.dataset.del}」？（不会删除磁盘文件）`)) return;
        try {
          const r = await api("/api/shares/" + encodeURIComponent(b.dataset.del), { method: "DELETE" });
          toast(r.message || "已删除");
          loadShares(); refreshShareSelect();
        } catch (err) { toast(err.message, true); }
      }));
  } catch (err) { toast(err.message, true); }
}

function openShareDialog(share) {
  editingShare = share || null;
  $("share-dialog-title").textContent = share ? `编辑共享：${share.name}` : "新建共享";
  $("sh-name").value = share?.name || "";
  $("sh-name").disabled = !!share;
  $("sh-path").value = share?.path || "";
  $("sh-comment").value = share?.comment || "";
  $("sh-writable").checked = share ? !share.read_only : true;
  $("sh-guest").checked = share?.guest_ok || false;
  $("sh-browseable").checked = share ? share.browseable : true;
  $("sh-valid-users").value = share?.valid_users || "";
  $("sh-write-list").value = share?.write_list || "";
  $("sh-fix-perms").checked = !share; // 新建时默认修正权限
  $("share-dialog").showModal();
}

$("btn-new-share").addEventListener("click", () => openShareDialog(null));
$("share-cancel").addEventListener("click", () => $("share-dialog").close());
$("share-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {
    name: $("sh-name").value.trim(),
    path: $("sh-path").value.trim(),
    comment: $("sh-comment").value.trim(),
    read_only: !$("sh-writable").checked,
    guest_ok: $("sh-guest").checked,
    browseable: $("sh-browseable").checked,
    valid_users: $("sh-valid-users").value.trim(),
    write_list: $("sh-write-list").value.trim(),
    fix_perms: $("sh-fix-perms").checked,
  };
  try {
    const r = editingShare
      ? await api("/api/shares/" + encodeURIComponent(editingShare.name), { method: "PUT", json: body })
      : await api("/api/shares", { json: body });
    $("share-dialog").close();
    toast(r.message || "已保存");
    loadShares(); refreshShareSelect();
  } catch (err) { toast(err.message, true); }
});

// ---------- 用户管理 ----------
async function loadUsers() {
  try {
    const { users } = await api("/api/users");
    $("users-tbody").innerHTML = users.length ? users.map((u) => `
      <tr>
        <td><b>${esc(u.username)}</b></td>
        <td class="muted">${esc(u.uid)}</td>
        <td>${u.disabled ? '<span class="badge danger">已禁用</span>' : '<span class="badge on">正常</span>'}</td>
        <td class="ops">
          <button class="btn mini" data-pw="${esc(u.username)}">改密</button>
          <button class="btn mini" data-toggle="${esc(u.username)}" data-enabled="${u.disabled ? 1 : 0}">
            ${u.disabled ? "启用" : "禁用"}</button>
          <button class="btn mini danger" data-deluser="${esc(u.username)}">删除</button>
        </td>
      </tr>`).join("") : '<tr><td colspan="4" class="muted">暂无共享用户</td></tr>';
    const tbody = $("users-tbody");
    tbody.querySelectorAll("[data-pw]").forEach((b) =>
      b.addEventListener("click", async () => {
        const pw = prompt(`为用户「${b.dataset.pw}」设置新密码：`);
        if (!pw) return;
        try {
          await api(`/api/users/${encodeURIComponent(b.dataset.pw)}/password`, { method: "PUT", json: { password: pw } });
          toast("密码已更新");
        } catch (err) { toast(err.message, true); }
      }));
    tbody.querySelectorAll("[data-toggle]").forEach((b) =>
      b.addEventListener("click", async () => {
        try {
          await api(`/api/users/${encodeURIComponent(b.dataset.toggle)}/enable`,
            { method: "PUT", json: { enabled: b.dataset.enabled === "1" } });
          loadUsers();
        } catch (err) { toast(err.message, true); }
      }));
    tbody.querySelectorAll("[data-deluser]").forEach((b) =>
      b.addEventListener("click", async () => {
        if (!confirm(`确定删除共享用户「${b.dataset.deluser}」？`)) return;
        try {
          await api("/api/users/" + encodeURIComponent(b.dataset.deluser), { method: "DELETE" });
          toast("已删除"); loadUsers();
        } catch (err) { toast(err.message, true); }
      }));
  } catch (err) { toast(err.message, true); }
}

$("btn-new-user").addEventListener("click", () => { $("user-form").reset(); $("user-dialog").showModal(); });
$("user-cancel").addEventListener("click", () => $("user-dialog").close());
$("user-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    await api("/api/users", { json: { username: $("u-name").value.trim(), password: $("u-password").value } });
    $("user-dialog").close();
    toast("用户已创建"); loadUsers();
  } catch (err) { toast(err.message, true); }
});

// ---------- 文件浏览 ----------
let curShare = "", curPath = "", curEntries = [], selectedIdx = -1;
let filesReqSeq = 0; // 请求代号：快速切换目录时丢弃过期响应

async function refreshShareSelect() {
  try {
    const { shares } = await api("/api/shares");
    const sel = $("file-share");
    const prev = sel.value;
    sel.innerHTML = shares.map((s) => `<option value="${esc(s.name)}">${esc(s.name)}（${esc(s.path)}）</option>`).join("");
    if (shares.some((s) => s.name === prev)) sel.value = prev;
    if (sel.value && sel.value !== curShare) { curShare = sel.value; curPath = ""; }
    if (curShare && activeTab === "files") loadFiles();
  } catch (err) { toast(err.message, true); }
}

$("file-share").addEventListener("change", () => {
  curShare = $("file-share").value; curPath = ""; loadFiles();
});

function renderBreadcrumb() {
  const parts = curPath.split("/").filter(Boolean);
  let html = `<a data-goto="">📁 ${esc(curShare)}</a>`;
  let acc = "";
  for (const p of parts) {
    acc += "/" + p;
    html += ` / <a data-goto="${esc(acc)}">${esc(p)}</a>`;
  }
  $("breadcrumb").innerHTML = html;
  $("breadcrumb").querySelectorAll("a").forEach((a) =>
    a.addEventListener("click", () => { curPath = a.dataset.goto; loadFiles(); }));
}

const fileIcon = (e) => e.is_dir ? "📁" :
  /\.(png|jpe?g|gif|webp|bmp|svg|ico)$/i.test(e.name) ? "🖼" :
  /\.(mp4|mkv|webm|avi|mov)$/i.test(e.name) ? "🎬" :
  /\.(mp3|wav|flac|ogg|m4a)$/i.test(e.name) ? "🎵" :
  /\.pdf$/i.test(e.name) ? "📕" :
  /\.(zip|tar|gz|xz|7z|rar|bz2)$/i.test(e.name) ? "📦" : "📄";

async function loadFiles() {
  if (!curShare) return;
  const seq = ++filesReqSeq;
  const reqShare = curShare, reqPath = curPath;
  try {
    const { entries } = await api(`/api/files?share=${encodeURIComponent(curShare)}&path=${encodeURIComponent(curPath)}`);
    // 响应回来时若已切换到别的目录/共享，丢弃这份过期数据
    if (seq !== filesReqSeq || reqShare !== curShare || reqPath !== curPath) return;
    curEntries = entries; selectedIdx = -1;
    renderBreadcrumb();
    $("files-tbody").innerHTML = entries.length ? entries.map((e, i) => `
      <tr data-idx="${i}">
        <td class="file-name"><span class="file-icon">${fileIcon(e)}</span>${esc(e.name)}</td>
        <td class="muted">${e.is_dir ? "-" : fmtSize(e.size)}</td>
        <td class="muted">${fmtTime(e.mtime)}</td>
        <td class="ops">
          ${e.is_dir ? "" : `<button class="btn mini" data-dl="${i}">下载</button>`}
          <button class="btn mini" data-acl="${i}">权限</button>
          <button class="btn mini danger" data-rm="${i}">删除</button>
        </td>
      </tr>`).join("") : '<tr><td colspan="4" class="muted">空目录</td></tr>';

    const tbody = $("files-tbody");
    tbody.querySelectorAll("tr[data-idx]").forEach((tr) => {
      const i = +tr.dataset.idx;
      tr.addEventListener("click", () => selectRow(i));
      tr.addEventListener("dblclick", () => {
        const e = curEntries[i];
        if (e.is_dir) { curPath = joinPath(curPath, e.name); loadFiles(); }
        else openPreview(e);
      });
    });
    tbody.querySelectorAll("[data-dl]").forEach((b) =>
      b.addEventListener("click", (ev) => { ev.stopPropagation(); downloadEntry(curEntries[+b.dataset.dl]); }));
    tbody.querySelectorAll("[data-acl]").forEach((b) =>
      b.addEventListener("click", (ev) => { ev.stopPropagation(); openAclDialog(curEntries[+b.dataset.acl]); }));
    tbody.querySelectorAll("[data-rm]").forEach((b) =>
      b.addEventListener("click", async (ev) => {
        ev.stopPropagation();
        const e = curEntries[+b.dataset.rm];
        if (!confirm(`确定删除「${e.name}」？${e.is_dir ? "（包含其下所有内容）" : ""}`)) return;
        try {
          await api("/api/files/delete", { json: { share: curShare, path: joinPath(curPath, e.name) } });
          toast("已删除"); loadFiles();
        } catch (err) { toast(err.message, true); }
      }));
  } catch (err) { toast(err.message, true); }
}

const joinPath = (base, name) => (base ? base.replace(/\/+$/, "") + "/" : "") + name;

function selectRow(i) {
  selectedIdx = i;
  document.querySelectorAll("#files-tbody tr").forEach((tr) =>
    tr.classList.toggle("selected", +tr.dataset.idx === i));
}

function fileUrl(e, inline) {
  return `/api/files/download?share=${encodeURIComponent(curShare)}&path=${encodeURIComponent(joinPath(curPath, e.name))}${inline ? "&inline=1" : ""}`;
}

function downloadEntry(e) {
  const a = document.createElement("a");
  a.href = fileUrl(e, false);
  a.download = e.name;
  a.click();
}

// 新建文件夹
$("btn-mkdir").addEventListener("click", async () => {
  if (!curShare) return toast("请先选择共享", true);
  const name = prompt("新文件夹名称：");
  if (!name) return;
  try {
    await api("/api/files/mkdir", { json: { share: curShare, path: curPath, name } });
    toast("已创建"); loadFiles();
  } catch (err) { toast(err.message, true); }
});

// 上传（XHR 以获得进度）
$("btn-upload").addEventListener("click", () => {
  if (!curShare) return toast("请先选择共享", true);
  $("upload-input").click();
});
$("upload-input").addEventListener("change", () => {
  const files = $("upload-input").files;
  if (!files.length) return;
  const fd = new FormData();
  for (const f of files) fd.append("file", f);
  const xhr = new XMLHttpRequest();
  xhr.open("POST", `/api/files/upload?share=${encodeURIComponent(curShare)}&path=${encodeURIComponent(curPath)}`);
  $("upload-progress").classList.remove("hidden");
  xhr.upload.onprogress = (ev) => {
    if (ev.lengthComputable) {
      const pct = Math.round((ev.loaded / ev.total) * 100);
      $("upload-bar-inner").style.width = pct + "%";
      $("upload-label").textContent = `上传中 ${pct}%`;
    }
  };
  xhr.onload = () => {
    $("upload-progress").classList.add("hidden");
    $("upload-bar-inner").style.width = "0";
    $("upload-input").value = "";
    if (xhr.status === 200) { toast("上传完成"); loadFiles(); }
    else {
      let msg = "上传失败";
      try { msg = JSON.parse(xhr.responseText).error || msg; } catch (_) {}
      toast(msg, true);
    }
  };
  xhr.onerror = () => { $("upload-progress").classList.add("hidden"); toast("上传失败", true); };
  xhr.send(fd);
});

// ---------- ACL 权限管理 ----------
let aclEntry = null; // 当前弹窗对应的文件项

const TAG_LABEL = { user: "用户", group: "组", mask: "掩码", other: "其他人" };

async function openAclDialog(e) {
  aclEntry = e;
  $("acl-title").textContent = `访问控制：${e.name}`;
  $("acl-dir-opts").style.display = e.is_dir ? "" : "none";
  $("acl-default").checked = false;
  $("acl-recursive").checked = false;
  // 用户名下拉提示复用共享用户列表
  try {
    const { users } = await api("/api/users");
    $("acl-userlist").innerHTML = users.map((u) => `<option value="${esc(u.username)}">`).join("");
  } catch (_) {}
  $("acl-dialog").showModal();
  await loadAcl();
}

async function loadAcl() {
  const e = aclEntry;
  try {
    const data = await api(`/api/files/acl?share=${encodeURIComponent(curShare)}&path=${encodeURIComponent(joinPath(curPath, e.name))}`);
    $("acl-ownerinfo").textContent = `属主: ${data.owner} · 属组: ${data.group}`;
    $("acl-tbody").innerHTML = data.entries.map((en, i) => {
      const label = TAG_LABEL[en.tag] || en.tag;
      const name = en.qualifier || (en.tag === "user" ? `(属主 ${data.owner})` : en.tag === "group" ? `(属组 ${data.group})` : "-");
      // effective 存在 = 该位被 mask/create mask 裁剪，实际未生效
      const p = (c) => !en.perms.includes(c) ? ""
        : (en.effective && !en.effective.includes(c))
          ? '<span class="acl-clip" title="被掩码裁剪，实际未生效">✔</span>' : "✔";
      const removable = en.qualifier && (en.tag === "user" || en.tag === "group");
      return `<tr>
        <td>${esc(label)}</td><td>${esc(name)}</td>
        <td class="acl-perm">${p("r")}</td><td class="acl-perm">${p("w")}</td><td class="acl-perm">${p("x")}</td>
        <td>${en.default ? '<span class="badge on">默认</span>' : ""}</td>
        <td class="ops">${removable ? `<button class="btn mini danger" data-aclrm="${i}">删除</button>` : ""}</td>
      </tr>`;
    }).join("") || '<tr><td colspan="7" class="muted">无 ACL 条目</td></tr>';
    $("acl-tbody").querySelectorAll("[data-aclrm]").forEach((b) =>
      b.addEventListener("click", async () => {
        const en = data.entries[+b.dataset.aclrm];
        try {
          await api("/api/files/acl", { json: {
            share: curShare, path: joinPath(curPath, e.name),
            action: "remove", tag: en.tag, qualifier: en.qualifier, default_acl: en.default,
          }});
          loadAcl();
        } catch (err) { toast(err.message, true); }
      }));
  } catch (err) { toast(err.message, true); }
}

$("acl-add-btn").addEventListener("click", async () => {
  const name = $("acl-name").value.trim();
  if (!name) return toast("请输入用户/组名", true);
  const perms = ($("acl-r").checked ? "r" : "-") + ($("acl-w").checked ? "w" : "-") + ($("acl-x").checked ? "x" : "-");
  const base = {
    share: curShare, path: joinPath(curPath, aclEntry.name),
    action: "set", tag: $("acl-tag").value, qualifier: name, perms,
    recursive: $("acl-recursive").checked,
  };
  try {
    await api("/api/files/acl", { json: base });
    if ($("acl-default").checked && aclEntry.is_dir) {
      await api("/api/files/acl", { json: { ...base, default_acl: true } });
    }
    toast("ACL 已更新");
    loadAcl();
  } catch (err) { toast(err.message, true); }
});

$("acl-clear").addEventListener("click", async () => {
  if (!confirm("清除该文件/目录的全部扩展 ACL？（恢复为基础 Unix 权限）")) return;
  try {
    await api("/api/files/acl", { json: {
      share: curShare, path: joinPath(curPath, aclEntry.name),
      action: "clear", recursive: $("acl-recursive").checked,
    }});
    toast("已清除");
    loadAcl();
  } catch (err) { toast(err.message, true); }
});

$("acl-close").addEventListener("click", () => $("acl-dialog").close());

// ---------- Quick Look：长按空格预览 ----------
const TEXT_EXT = /\.(txt|md|markdown|log|json|js|ts|jsx|tsx|rs|py|c|cpp|cc|h|hpp|java|go|rb|php|sh|bash|zsh|yml|yaml|toml|ini|cfg|conf|xml|html|htm|css|scss|csv|sql|dockerfile|makefile|gitignore|env|properties|lua|kt|swift|pl|vim)$/i;
const IMG_EXT = /\.(png|jpe?g|gif|webp|bmp|svg|ico|avif)$/i;
const VIDEO_EXT = /\.(mp4|webm|mov|m4v)$/i;
const AUDIO_EXT = /\.(mp3|wav|flac|ogg|m4a|aac)$/i;

let previewOpen = false;
let spaceHoldTimer = null;
let spaceDown = false;

async function openPreview(e) {
  if (!e || e.is_dir) return;
  const body = $("preview-body");
  $("preview-title").textContent = `${e.name}（${fmtSize(e.size)}）`;
  body.innerHTML = "";
  $("preview-overlay").classList.remove("hidden");
  previewOpen = true;

  const url = fileUrl(e, true);
  if (IMG_EXT.test(e.name)) {
    const img = document.createElement("img");
    img.src = url;
    body.appendChild(img);
  } else if (/\.pdf$/i.test(e.name)) {
    const f = document.createElement("iframe");
    f.src = url;
    body.appendChild(f);
  } else if (VIDEO_EXT.test(e.name)) {
    const v = document.createElement("video");
    v.src = url; v.controls = true; v.autoplay = true;
    body.appendChild(v);
  } else if (AUDIO_EXT.test(e.name)) {
    const a = document.createElement("audio");
    a.src = url; a.controls = true; a.autoplay = true;
    body.appendChild(a);
  } else if (TEXT_EXT.test(e.name) && e.size <= 2 * 1024 * 1024) {
    const pre = document.createElement("pre");
    pre.textContent = "加载中…";
    body.appendChild(pre);
    try {
      const res = await fetch(url);
      pre.textContent = await res.text();
    } catch (_) { pre.textContent = "读取失败"; }
  } else {
    body.innerHTML = `<div class="no-preview">该类型暂不支持预览<br>
      <button class="btn primary" id="preview-dl">下载文件</button></div>`;
    $("preview-dl").addEventListener("click", () => downloadEntry(e));
  }
}

function closePreview() {
  $("preview-overlay").classList.add("hidden");
  $("preview-body").innerHTML = ""; // 停掉音视频播放
  previewOpen = false;
}

$("preview-overlay").addEventListener("click", (ev) => {
  if (ev.target === $("preview-overlay")) closePreview();
});

const inInput = (el) => el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT" || el.isContentEditable);

document.addEventListener("keydown", (ev) => {
  if (ev.code === "Escape" && previewOpen) { closePreview(); return; }
  if (activeTab !== "files" || inInput(document.activeElement)) return;
  if (document.querySelector("dialog[open]")) return;

  if (ev.code === "Space") {
    ev.preventDefault(); // 阻止页面滚动
    if (ev.repeat || spaceDown) return;
    spaceDown = true;
    const e = curEntries[selectedIdx];
    if (e && !e.is_dir && !previewOpen) {
      // 长按 350ms 触发 Quick Look
      spaceHoldTimer = setTimeout(() => openPreview(e), 350);
    }
  } else if (ev.code === "ArrowDown" || ev.code === "ArrowUp") {
    ev.preventDefault();
    if (previewOpen || !curEntries.length) return;
    const next = ev.code === "ArrowDown"
      ? Math.min(selectedIdx + 1, curEntries.length - 1)
      : Math.max(selectedIdx - 1, 0);
    selectRow(next === -1 ? 0 : next);
    document.querySelector(`#files-tbody tr[data-idx="${selectedIdx}"]`)?.scrollIntoView({ block: "nearest" });
  } else if (ev.code === "Enter") {
    if (previewOpen) return;
    const e = curEntries[selectedIdx];
    if (e?.is_dir) { curPath = joinPath(curPath, e.name); loadFiles(); }
  } else if (ev.code === "Backspace") {
    ev.preventDefault(); // 避免触发浏览器"后退"
    if (previewOpen) return;
    if (curPath) { curPath = curPath.split("/").slice(0, -1).join("/"); loadFiles(); }
  }
});

document.addEventListener("keyup", (ev) => {
  if (ev.code !== "Space") return;
  spaceDown = false;
  clearTimeout(spaceHoldTimer);
  // 长按式预览：松开空格即关闭
  if (previewOpen) closePreview();
});

// ---------- 启动 ----------
(async () => {
  try {
    await api("/api/me");
    showMain();
  } catch (_) {
    showLogin();
  }
})();
