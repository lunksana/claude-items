"use strict";

const $ = (id) => document.getElementById(id);

// ---------- 基础通用 API ----------
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
    throw new Error("未登录或管理会话已超时过期");
  }
  let data = {};
  try { data = await res.json(); } catch (_) {}
  if (!res.ok) throw new Error(data.error || `请求异常 (${res.status})`);
  return data;
}

let toastTimer = null;
function toast(msg, isErr = false) {
  const t = $("toast");
  if (!t) return;
  t.textContent = msg;
  t.className = "toast" + (isErr ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), 3500);
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function fmtSize(n) {
  if (n === undefined || n === null || isNaN(n)) return "-";
  if (n < 1024) return n + " B";
  const units = ["KB", "MB", "GB", "TB", "PB"];
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

// ---------- 密码显隐切换通用 ----------
document.querySelectorAll(".toggle-pw").forEach((btn) => {
  btn.addEventListener("click", () => {
    const targetId = btn.dataset.target;
    const input = $(targetId);
    if (!input) return;
    if (input.type === "password") {
      input.type = "text";
      btn.textContent = "🙈";
    } else {
      input.type = "password";
      btn.textContent = "👁️";
    }
  });
});

// ---------- 视图与导航切换 ----------
function showLogin() {
  $("login-view").classList.remove("hidden");
  $("main-view").classList.add("hidden");
}

function showMain() {
  $("login-view").classList.add("hidden");
  $("main-view").classList.remove("hidden");
  loadStatus();
  loadShares();
  loadUsers();
  refreshShareSelect();
}

let activeTab = "status";
document.querySelectorAll(".nav-item").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".nav-item").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    activeTab = btn.dataset.tab;
    ["status", "shares", "users", "files", "settings"].forEach((t) => {
      const sec = $("tab-" + t);
      if (sec) sec.classList.toggle("hidden", t !== activeTab);
    });
    if (activeTab === "status") loadStatus();
    if (activeTab === "shares") loadShares();
    if (activeTab === "users") loadUsers();
    if (activeTab === "files") { refreshShareSelect(); loadFiles(); }
    if (activeTab === "settings") loadSettings();
  });
});

// ---------- 登录与退出 ----------
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
  if (!confirm("确定要退出 Samba 管理平台？")) return;
  try { await api("/api/logout", { method: "POST" }); } catch (_) {}
  showLogin();
});

// 修改 WebGUI 管理密码
$("btn-change-pw").addEventListener("click", () => {
  $("pw-form").reset();
  $("pw-dialog").showModal();
});
$("pw-cancel").addEventListener("click", () => $("pw-dialog").close());
$("pw-close").addEventListener("click", () => $("pw-dialog").close());
$("pw-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  if ($("pw-new").value !== $("pw-confirm").value) {
    toast("两次输入的新密码不一致，请核对！", true);
    return;
  }
  try {
    await api("/api/password", { json: { old_password: $("pw-old").value, new_password: $("pw-new").value } });
    $("pw-dialog").close();
    $("pw-form").reset();
    toast("管理密码已成功更新");
  } catch (err) { toast(err.message, true); }
});

// ---------- 监控大盘及实时状态 ----------
async function loadStatus() {
  try {
    const data = await api("/api/status");
    const smbdBadge = $("badge-smbd");
    if (smbdBadge) {
      smbdBadge.textContent = data.smbd_active ? "正常运行 (Active)" : "未启动 (Stopped)";
      smbdBadge.className = "status-pill " + (data.smbd_active ? "active" : "stopped");
    }
    const nmbdBadge = $("badge-nmbd");
    if (nmbdBadge) {
      nmbdBadge.textContent = data.nmbd_active ? "正常运行 (Active)" : "未启动 (Stopped)";
      nmbdBadge.className = "status-pill " + (data.nmbd_active ? "active" : "stopped");
    }

    const diskBox = $("status-disks-list");
    if (diskBox && data.disks) {
      diskBox.innerHTML = data.disks.length ? data.disks.map((d) => {
        const pct = Math.min(Math.max(d.percent || 0, 0), 100);
        const cls = pct > 88 ? "danger" : pct > 75 ? "warn" : "ok";
        return `
          <div class="disk-item">
            <div class="disk-header">
              <span><b>${esc(d.mounted_on)}</b> <small class="muted">(${esc(d.fs)})</small></span>
              <span>${fmtSize(d.used)} / ${fmtSize(d.total)} (${pct}%)</span>
            </div>
            <div class="disk-bar-wrap"><div class="disk-bar ${cls}" style="width: ${pct}%"></div></div>
          </div>`;
      }).join("") : '<p class="muted">暂无存储分区信息</p>';
    }

    const connsTbody = $("status-conns-tbody");
    if (connsTbody && data.connections) {
      $("conn-count-badge").textContent = `${data.connections.length} 在线`;
      connsTbody.innerHTML = data.connections.length ? data.connections.map((c) => `
        <tr>
          <td><code class="primary">${c.pid}</code></td>
          <td><b>${esc(c.username)}</b></td>
          <td><span class="badge info">${esc(c.groupname)}</span></td>
          <td>${esc(c.machine)}</td>
          <td class="muted">${esc(c.ip_addr)}</td>
          <td class="ops">
            <button class="btn mini danger" data-kick="${c.pid}">踢出断开</button>
          </td>
        </tr>`).join("") : '<tr><td colspan="6" class="muted">当前无活跃客户端连接</td></tr>';

      connsTbody.querySelectorAll("[data-kick]").forEach((b) => {
        b.addEventListener("click", async () => {
          const pid = +b.dataset.kick;
          if (!confirm(`确定强制断开 PID 为 ${pid} 的客户端连接？`)) return;
          try {
            const r = await api("/api/status/disconnect", { json: { pid } });
            toast("连接已断开");
            loadStatus();
          } catch (err) { toast(err.message, true); }
        });
      });
    }
  } catch (err) { toast(err.message, true); }
}

$("btn-refresh-status")?.addEventListener("click", () => {
  toast("正在刷新监控状态...");
  loadStatus();
});

document.querySelectorAll(".service-actions button[data-action]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const act = btn.dataset.action;
    const labels = { restart: "重启服务", reload: "热加载配置", stop: "停止服务", start: "启动服务" };
    if (act === "stop" && !confirm("警告：停止服务将导致所有正在进行的 SMB 文件传输断开，确定停止？")) return;
    try {
      toast(`正在${labels[act] || act}...`);
      const r = await api("/api/status/service", { json: { action: act } });
      toast(r.msg || `${labels[act]}完成`);
      loadStatus();
    } catch (err) { toast(err.message, true); }
  });
});

// ---------- 共享管理 ----------
let editingShare = null;

async function loadShares() {
  try {
    const { shares } = await api("/api/shares");
    const tbody = $("shares-tbody");
    if (!tbody) return;
    tbody.innerHTML = shares.map((s) => `
      <tr>
        <td><b>${esc(s.name)}</b></td>
        <td class="muted"><code>${esc(s.path)}</code></td>
        <td>${esc(s.comment || "-")}</td>
        <td>
          <span class="badge ${s.read_only ? 'off' : 'on'}">${s.read_only ? '只读' : '可写'}</span>
          <span class="badge ${s.guest_ok ? 'warn' : 'off'}">${s.guest_ok ? '访客免密' : '需验证'}</span>
        </td>
        <td>
          ${s.recycle ? '<span class="badge info" title="网络回收站开启">♻️ 回收站</span> ' : ''}
          ${s.fruit ? '<span class="badge info" title="Apple/macOS 兼容开启">🍎 macOS/TimeMachine</span>' : ''}
          ${!s.recycle && !s.fruit ? '<span class="muted small">默认</span>' : ''}
        </td>
        <td class="muted small">${esc(s.valid_users || "所有人/任意有效用户")}</td>
        <td>${s.managed ? '<span class="badge on">本工具托管</span>' : '<span class="badge off">原 smb.conf 主配置</span>'}</td>
        <td class="ops">${s.managed ? `
          <button class="btn mini" data-edit="${esc(s.name)}">⚙️ 编辑</button>
          <button class="btn mini danger" data-del="${esc(s.name)}">🗑️ 删除</button>` : `
          <button class="btn mini success" data-migrate="${esc(s.name)}" title="接管到 WebGUI 中托管编辑">✨ 接管编辑</button>`}
        </td>
      </tr>`).join("");

    tbody.querySelectorAll("[data-edit]").forEach((b) =>
      b.addEventListener("click", () => openShareDialog(shares.find((s) => s.name === b.dataset.edit))));

    tbody.querySelectorAll("[data-del]").forEach((b) =>
      b.addEventListener("click", async () => {
        if (!confirm(`确定要从 Samba 中删除共享「${b.dataset.del}」？（物理磁盘上的文件和目录不会被删除）`)) return;
        try {
          const r = await api("/api/shares/" + encodeURIComponent(b.dataset.del), { method: "DELETE" });
          toast(r.message || "共享目录已删除");
          loadShares(); refreshShareSelect();
        } catch (err) { toast(err.message, true); }
      }));

    tbody.querySelectorAll("[data-migrate]").forEach((b) =>
      b.addEventListener("click", async () => {
        const name = b.dataset.migrate;
        if (!confirm(`将共享「${name}」接管至本管理工具托管？\n接管后原 smb.conf 中的对应段落将被注释保留，您可以随时在 WebGUI 中灵活修改所有参数及扩展。`)) return;
        try {
          const r = await api("/api/shares/migrate/" + encodeURIComponent(name), { method: "POST" });
          toast(r.msg || "接管成功");
          loadShares(); refreshShareSelect();
        } catch (err) { toast(err.message, true); }
      }));
  } catch (err) { toast(err.message, true); }
}

function openShareDialog(share) {
  editingShare = share || null;
  $("share-dialog-title").textContent = share ? `编辑共享：${share.name}` : "新建共享文件夹";
  $("sh-name").value = share?.name || "";
  $("sh-name").disabled = !!share;
  $("sh-path").value = share?.path || "";
  $("sh-comment").value = share?.comment || "";
  $("sh-writable").checked = share ? !share.read_only : true;
  $("sh-guest").checked = share?.guest_ok || false;
  $("sh-browseable").checked = share ? share.browseable : true;
  $("sh-recycle").checked = share?.recycle || false;
  $("sh-fruit").checked = share?.fruit || false;
  $("sh-valid-users").value = share?.valid_users || "";
  $("sh-write-list").value = share?.write_list || "";
  $("sh-fix-perms").checked = !share;
  $("share-dialog").showModal();
}

$("btn-new-share")?.addEventListener("click", () => openShareDialog(null));
$("share-cancel")?.addEventListener("click", () => $("share-dialog").close());
$("share-close")?.addEventListener("click", () => $("share-dialog").close());

$("share-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {
    name: $("sh-name").value.trim(),
    path: $("sh-path").value.trim(),
    comment: $("sh-comment").value.trim(),
    read_only: !$("sh-writable").checked,
    guest_ok: $("sh-guest").checked,
    browseable: $("sh-browseable").checked,
    recycle: $("sh-recycle").checked,
    fruit: $("sh-fruit").checked,
    valid_users: $("sh-valid-users").value.trim(),
    write_list: $("sh-write-list").value.trim(),
    fix_perms: $("sh-fix-perms").checked,
  };
  try {
    const r = editingShare
      ? await api("/api/shares/" + encodeURIComponent(editingShare.name), { method: "PUT", json: body })
      : await api("/api/shares", { json: body });
    $("share-dialog").close();
    toast(r.message || "共享设置已成功生效");
    loadShares(); refreshShareSelect();
  } catch (err) { toast(err.message, true); }
});

// ---------- 树形目录浏览/新建对话框 ----------
let treeCurrentPath = "/";
let treeTargetInputId = "sh-path";

async function openTreeDialog(targetInputId, initPath = "/") {
  treeTargetInputId = targetInputId;
  treeCurrentPath = $(targetInputId)?.value || initPath || "/";
  if (!treeCurrentPath.startsWith("/")) treeCurrentPath = "/";
  $("tree-dialog").showModal();
  await loadTreeDir(treeCurrentPath);
}

async function loadTreeDir(path) {
  treeCurrentPath = path;
  $("tree-current-path").textContent = path;
  $("tree-new-dirname").value = "";
  try {
    const data = await api(`/api/files/tree?path=${encodeURIComponent(path)}`);
    const box = $("tree-list-box");
    box.innerHTML = data.entries.length ? data.entries.map((d) => `
      <div class="tree-dir-item" data-dirname="${esc(d.name)}">
        <span>📁</span>
        <b>${esc(d.name)}</b>
      </div>`).join("") : '<div class="p-4 muted text-center">当前目录下暂无子文件夹</div>';

    box.querySelectorAll(".tree-dir-item").forEach((item) => {
      item.addEventListener("click", () => {
        const next = joinPath(treeCurrentPath, item.dataset.dirname);
        loadTreeDir(next);
      });
    });
  } catch (err) {
    toast("无法读取路径：" + err.message, true);
    if (path !== "/") loadTreeDir("/");
  }
}

$("sh-browse-btn")?.addEventListener("click", () => openTreeDialog("sh-path"));
$("tree-close")?.addEventListener("click", () => $("tree-dialog").close());
$("tree-cancel-btn")?.addEventListener("click", () => $("tree-dialog").close());

$("tree-up-btn")?.addEventListener("click", () => {
  if (treeCurrentPath === "/") return;
  const parts = treeCurrentPath.split("/").filter(Boolean);
  parts.pop();
  const up = parts.length ? "/" + parts.join("/") : "/";
  loadTreeDir(up);
});

$("tree-mkdir-do-btn")?.addEventListener("click", async () => {
  const name = $("tree-new-dirname").value.trim();
  if (!name) return toast("请输入新文件夹名称", true);
  try {
    await api("/api/files/tree/mkdir", { json: { path: treeCurrentPath, name } });
    toast("文件夹建立成功");
    loadTreeDir(treeCurrentPath);
  } catch (err) { toast(err.message, true); }
});

$("tree-confirm-btn")?.addEventListener("click", () => {
  const target = $(treeTargetInputId);
  if (target) target.value = treeCurrentPath;
  $("tree-dialog").close();
});

// ---------- 多选弹窗 (用户/组选择) ----------
let multiSelectTargetInput = "sh-valid-users";
async function openMultiSelectDialog(targetInputId, title) {
  multiSelectTargetInput = targetInputId;
  $("multi-select-title").textContent = title;
  try {
    const [usersRes, groupsRes] = await Promise.all([api("/api/users"), api("/api/groups")]);
    const currentList = ($(targetInputId)?.value || "").split(",").map((s) => s.trim()).filter(Boolean);
    const box = $("multi-select-list");
    let html = `<div class="muted small mb-2">可勾选单个用户或以 @ 开头的 POSIX 用户组：</div>`;

    usersRes.users.forEach((u) => {
      const checked = currentList.includes(u.username) ? "checked" : "";
      html += `<label class="check"><input type="checkbox" value="${esc(u.username)}" ${checked}> 👤 ${esc(u.username)} <small class="muted">(${esc(u.uid)})</small></label>`;
    });

    groupsRes.groups.forEach((g) => {
      const val = "@" + g.groupname;
      const checked = currentList.includes(val) ? "checked" : "";
      html += `<label class="check"><input type="checkbox" value="${esc(val)}" ${checked}> 👥 @${esc(g.groupname)} <small class="muted">(用户组 GID: ${g.gid})</small></label>`;
    });

    box.innerHTML = html;
    $("multi-select-dialog").showModal();
  } catch (err) { toast(err.message, true); }
}

$("sh-pick-valid-users")?.addEventListener("click", () => openMultiSelectDialog("sh-valid-users", "选择允许访问的用户/组"));
$("sh-pick-write-list")?.addEventListener("click", () => openMultiSelectDialog("sh-write-list", "选择额外有写入权限的用户/组"));
$("multi-select-close")?.addEventListener("click", () => $("multi-select-dialog").close());
$("multi-select-cancel")?.addEventListener("click", () => $("multi-select-dialog").close());
$("multi-select-confirm")?.addEventListener("click", () => {
  const selected = [];
  $("multi-select-list").querySelectorAll("input[type=checkbox]:checked").forEach((cb) => selected.push(cb.value));
  const target = $(multiSelectTargetInput);
  if (target) target.value = selected.join(", ");
  $("multi-select-dialog").close();
});

// ---------- 用户与组管理 ----------
async function loadUsers() {
  try {
    const { users } = await api("/api/users");
    const tbody = $("users-tbody");
    if (!tbody) return;
    tbody.innerHTML = users.length ? users.map((u) => {
      const groupsStr = (u.groups || []).map((g) => `<span class="badge info">${esc(g)}</span>`).join(" ");
      return `
        <tr>
          <td><b>${esc(u.username)}</b></td>
          <td class="muted"><code>${esc(u.uid)}</code></td>
          <td>${groupsStr || '<span class="muted">-</span>'}</td>
          <td>${u.disabled ? '<span class="badge danger">已禁用</span>' : '<span class="badge on">正常启用</span>'}</td>
          <td class="ops">
            <button class="btn mini" data-pw="${esc(u.username)}">🔑 改密</button>
            <button class="btn mini" data-groups="${esc(u.username)}" data-curgroups="${esc((u.groups||[]).join(','))}">👥 调整组</button>
            <button class="btn mini" data-toggle="${esc(u.username)}" data-enabled="${u.disabled ? 1 : 0}">${u.disabled ? "🟢 启用" : "🔴 禁用"}</button>
            <button class="btn mini danger" data-deluser="${esc(u.username)}">🗑️ 删除</button>
          </td>
        </tr>`;
    }).join("") : '<tr><td colspan="5" class="muted">暂无共享用户</td></tr>';

    tbody.querySelectorAll("[data-pw]").forEach((b) =>
      b.addEventListener("click", () => {
        $("user-pw-form").reset();
        $("user-pw-title").textContent = `重置密码：${b.dataset.pw}`;
        $("user-pw-form").dataset.username = b.dataset.pw;
        $("user-pw-dialog").showModal();
      }));

    tbody.querySelectorAll("[data-groups]").forEach((b) =>
      b.addEventListener("click", async () => {
        const username = b.dataset.groups;
        const curGroups = b.dataset.curgroups.split(",").filter(Boolean);
        $("user-group-title").textContent = `调整所属组：${username}`;
        $("user-group-form").dataset.username = username;
        try {
          const { groups } = await api("/api/groups");
          const box = $("user-group-list");
          box.innerHTML = groups.map((g) => {
            const checked = curGroups.includes(g.groupname) ? "checked" : "";
            return `<label class="check"><input type="checkbox" value="${esc(g.groupname)}" ${checked}> 👥 ${esc(g.groupname)} <small class="muted">(GID: ${g.gid})</small></label>`;
          }).join("");
          $("user-group-dialog").showModal();
        } catch (err) { toast(err.message, true); }
      }));

    tbody.querySelectorAll("[data-toggle]").forEach((b) =>
      b.addEventListener("click", async () => {
        try {
          await api(`/api/users/${encodeURIComponent(b.dataset.toggle)}/enable`,
            { method: "PUT", json: { enabled: b.dataset.enabled === "1" } });
          toast(b.dataset.enabled === "1" ? "用户已启用" : "用户已禁用");
          loadUsers();
        } catch (err) { toast(err.message, true); }
      }));

    tbody.querySelectorAll("[data-deluser]").forEach((b) =>
      b.addEventListener("click", async () => {
        if (!confirm(`确定要彻底删除共享用户「${b.dataset.deluser}」？`)) return;
        try {
          await api("/api/users/" + encodeURIComponent(b.dataset.deluser), { method: "DELETE" });
          toast("用户已删除"); loadUsers();
        } catch (err) { toast(err.message, true); }
      }));
  } catch (err) { toast(err.message, true); }
}

// 新建用户
$("btn-new-user")?.addEventListener("click", () => { $("user-form").reset(); $("user-dialog").showModal(); });
$("user-cancel")?.addEventListener("click", () => $("user-dialog").close());
$("user-close")?.addEventListener("click", () => $("user-dialog").close());
$("user-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  if ($("u-password").value !== $("u-password-confirm").value) {
    toast("两次输入的密码不一致，请核对！", true);
    return;
  }
  try {
    await api("/api/users", { json: { username: $("u-name").value.trim(), password: $("u-password").value } });
    $("user-dialog").close();
    toast("Samba 用户创建成功"); loadUsers();
  } catch (err) { toast(err.message, true); }
});

// 重置用户密码
$("upw-cancel")?.addEventListener("click", () => $("user-pw-dialog").close());
$("user-pw-close")?.addEventListener("click", () => $("user-pw-dialog").close());
$("user-pw-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  if ($("upw-new").value !== $("upw-confirm").value) {
    toast("两次输入的新密码不一致！", true);
    return;
  }
  const username = $("user-pw-form").dataset.username;
  try {
    await api(`/api/users/${encodeURIComponent(username)}/password`, { method: "PUT", json: { password: $("upw-new").value } });
    $("user-pw-dialog").close();
    toast(`用户 ${username} 密码已重置`);
  } catch (err) { toast(err.message, true); }
});

// 提交组所属变更
$("user-group-cancel")?.addEventListener("click", () => $("user-group-dialog").close());
$("user-group-close")?.addEventListener("click", () => $("user-group-dialog").close());
$("user-group-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  const username = $("user-group-form").dataset.username;
  const selected = [];
  $("user-group-list").querySelectorAll("input[type=checkbox]:checked").forEach((cb) => selected.push(cb.value));
  try {
    await api(`/api/users/${encodeURIComponent(username)}/groups`, { method: "PUT", json: { groups: selected } });
    $("user-group-dialog").close();
    toast(`用户 ${username} 的组成员资格已生效`);
    loadUsers();
  } catch (err) { toast(err.message, true); }
});

// ---------- 全局设置 ----------
async function loadSettings() {
  try {
    const cfg = await api("/api/config");
    if ($("set-listen-addr")) $("set-listen-addr").value = cfg.listen_addr || "0.0.0.0:8686";
    if ($("set-session-ttl")) $("set-session-ttl").value = cfg.session_ttl_hours || 24;
    if ($("set-guest-map")) $("set-guest-map").checked = !!cfg.guest_map_bad_user;
  } catch (err) { toast(err.message, true); }
}

$("settings-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const body = {
      listen_addr: $("set-listen-addr").value.trim(),
      session_ttl_hours: Number($("set-session-ttl").value) || 24,
      guest_map_bad_user: $("set-guest-map").checked,
    };
    await api("/api/config", { json: body });
    toast("全局参数与监听设置已生效保存！");
  } catch (err) { toast(err.message, true); }
});

// ---------- 文件资源管理器及多选批量操作 ----------
let curShare = "", curPath = "", curEntries = [], selectedIdx = -1;
let filesReqSeq = 0;

async function refreshShareSelect() {
  try {
    const { shares } = await api("/api/shares");
    const sel = $("file-share");
    if (!sel) return;
    const prev = sel.value;
    sel.innerHTML = shares.map((s) => `<option value="${esc(s.name)}">${esc(s.name)} （${esc(s.path)}）</option>`).join("");
    if (shares.some((s) => s.name === prev)) sel.value = prev;
    if (sel.value && sel.value !== curShare) { curShare = sel.value; curPath = ""; }
    if (curShare && activeTab === "files") loadFiles();
  } catch (err) { toast(err.message, true); }
}

$("file-share")?.addEventListener("change", () => {
  curShare = $("file-share").value; curPath = ""; loadFiles();
});

const joinPath = (base, name) => (base ? base.replace(/\/+$/, "") + "/" : "") + name;

function renderBreadcrumb() {
  const parts = curPath.split("/").filter(Boolean);
  let html = `<a data-goto="">📁 ${esc(curShare)}</a>`;
  let acc = "";
  for (const p of parts) {
    acc += "/" + p;
    html += ` / <a data-goto="${esc(acc)}">${esc(p)}</a>`;
  }
  const box = $("breadcrumb");
  if (box) {
    box.innerHTML = html;
    box.querySelectorAll("a").forEach((a) =>
      a.addEventListener("click", () => { curPath = a.dataset.goto; loadFiles(); }));
  }
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
    if (seq !== filesReqSeq || reqShare !== curShare || reqPath !== curPath) return;
    curEntries = entries; selectedIdx = -1;
    if ($("files-select-all")) $("files-select-all").checked = false;
    renderBreadcrumb();

    const tbody = $("files-tbody");
    if (!tbody) return;
    tbody.innerHTML = entries.length ? entries.map((e, i) => {
      const isArchive = !e.is_dir && /\.(tar|gz|bz2|xz|zip)$/i.test(e.name);
      return `
        <tr data-idx="${i}">
          <td class="check-col"><input type="checkbox" class="file-chk" data-name="${esc(e.name)}"></td>
          <td class="file-name"><span class="file-icon">${fileIcon(e)}</span>${esc(e.name)}</td>
          <td class="muted">${e.is_dir ? "-" : fmtSize(e.size)}</td>
          <td class="muted">${fmtTime(e.mtime)}</td>
          <td class="ops">
            ${isArchive ? `<button class="btn mini success" data-extract="${i}" title="在线解压缩到当前文件夹">📦 解压</button>` : ""}
            ${e.is_dir ? "" : `<button class="btn mini" data-dl="${i}">下载</button>`}
            <button class="btn mini" data-acl="${i}">权限</button>
            <button class="btn mini danger" data-rm="${i}">删除</button>
          </td>
        </tr>`;
    }).join("") : '<tr><td colspan="5" class="muted p-6 text-center">当前文件夹下无任何项目</td></tr>';

    tbody.querySelectorAll("tr[data-idx]").forEach((tr) => {
      const i = +tr.dataset.idx;
      tr.addEventListener("click", (ev) => {
        if (ev.target.tagName !== "INPUT" && ev.target.tagName !== "BUTTON") selectRow(i);
      });
      tr.addEventListener("dblclick", (ev) => {
        if (ev.target.tagName === "INPUT" || ev.target.tagName === "BUTTON") return;
        const e = curEntries[i];
        if (e.is_dir) { curPath = joinPath(curPath, e.name); loadFiles(); }
        else openPreview(e);
      });
    });

    tbody.querySelectorAll("[data-dl]").forEach((b) =>
      b.addEventListener("click", (ev) => { ev.stopPropagation(); downloadEntry(curEntries[+b.dataset.dl]); }));

    tbody.querySelectorAll("[data-extract]").forEach((b) =>
      b.addEventListener("click", async (ev) => {
        ev.stopPropagation();
        const e = curEntries[+b.dataset.extract];
        if (!confirm(`正在解压「${e.name}」到当前路径下，是否继续？`)) return;
        try {
          toast("正在执行解压包...");
          await api("/api/files/extract", { json: { share: curShare, path: joinPath(curPath, e.name) } });
          toast("解压缩完成"); loadFiles();
        } catch (err) { toast(err.message, true); }
      }));

    tbody.querySelectorAll("[data-acl]").forEach((b) =>
      b.addEventListener("click", (ev) => { ev.stopPropagation(); openAclDialog(curEntries[+b.dataset.acl]); }));

    tbody.querySelectorAll("[data-rm]").forEach((b) =>
      b.addEventListener("click", async (ev) => {
        ev.stopPropagation();
        const e = curEntries[+b.dataset.rm];
        if (!confirm(`确定要删除「${e.name}」？${e.is_dir ? "（⚠️ 将递归删除该目录下的所有子文件夹及内容）" : ""}`)) return;
        try {
          await api("/api/files/delete", { json: { share: curShare, path: joinPath(curPath, e.name) } });
          toast("项目已成功删除"); loadFiles();
        } catch (err) { toast(err.message, true); }
      }));
  } catch (err) { toast(err.message, true); }
}

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

$("files-select-all")?.addEventListener("change", (ev) => {
  const chk = ev.target.checked;
  document.querySelectorAll("#files-tbody .file-chk").forEach((c) => c.checked = chk);
});

function getCheckedFileNames() {
  const list = [];
  document.querySelectorAll("#files-tbody .file-chk:checked").forEach((c) => {
    if (c.dataset.name) list.push(c.dataset.name);
  });
  return list;
}

// 批量复制/剪切移动
$("btn-batch-copy")?.addEventListener("click", async () => {
  const items = getCheckedFileNames();
  if (!items.length) return toast("请至少勾选一个要操作的文件或文件夹！", true);
  try {
    const { shares } = await api("/api/shares");
    $("cm-target-share").innerHTML = shares.map((s) => `<option value="${esc(s.name)}">${esc(s.name)}</option>`).join("");
    $("cm-target-share").value = curShare;
    $("copy-move-hint").textContent = `您已勾选 ${items.length} 个项目，请选择它们的目标存储位置：`;
    $("copy-move-dialog").showModal();
  } catch (err) { toast(err.message, true); }
});

$("copy-move-close")?.addEventListener("click", () => $("copy-move-dialog").close());
$("cm-cancel")?.addEventListener("click", () => $("copy-move-dialog").close());

$("cm-do-copy")?.addEventListener("click", async () => {
  const items = getCheckedFileNames();
  const dstShare = $("cm-target-share").value;
  const dstPath = $("cm-target-path").value.trim();
  $("copy-move-dialog").close();
  toast(`正在进行 ${items.length} 个项目的复制...`);
  try {
    for (const name of items) {
      await api("/api/files/copy", { json: {
        src_share: curShare, src_path: joinPath(curPath, name),
        dst_share: dstShare, dst_path: joinPath(dstPath, name)
      }});
    }
    toast("复制全部完成！"); loadFiles();
  } catch (err) { toast(err.message, true); }
});

$("cm-do-move")?.addEventListener("click", async () => {
  const items = getCheckedFileNames();
  const dstShare = $("cm-target-share").value;
  const dstPath = $("cm-target-path").value.trim();
  if (!confirm(`确定将选中的 ${items.length} 个项目剪切移动到 ${dstShare}/${dstPath} 吗？`)) return;
  $("copy-move-dialog").close();
  toast(`正在移动 ${items.length} 个项目...`);
  try {
    for (const name of items) {
      await api("/api/files/move", { json: {
        src_share: curShare, src_path: joinPath(curPath, name),
        dst_share: dstShare, dst_path: joinPath(dstPath, name)
      }});
    }
    toast("移动（剪切）全部完成！"); loadFiles();
  } catch (err) { toast(err.message, true); }
});

// 选定项打包压缩包
$("btn-batch-archive")?.addEventListener("click", async () => {
  const items = getCheckedFileNames();
  if (!items.length) return toast("请勾选需要打包压缩的文件或文件夹", true);
  const name = prompt("为您生成的新压缩包名称（以 .tar.gz 结尾）：", "archive_data.tar.gz");
  if (!name) return;
  const archiveName = name.endsWith(".tar.gz") ? name : name + ".tar.gz";
  toast("正在打包压缩中，请稍候...");
  try {
    const r = await api("/api/files/archive", { json: { share: curShare, path: curPath, items, archive_name: archiveName } });
    toast(r.message || "打包压缩完成！"); loadFiles();
  } catch (err) { toast(err.message, true); }
});

// 新建文件夹
$("btn-mkdir")?.addEventListener("click", async () => {
  if (!curShare) return toast("请先选择共享目录", true);
  const name = prompt("请输入新文件夹名称：");
  if (!name) return;
  try {
    await api("/api/files/mkdir", { json: { share: curShare, path: curPath, name } });
    toast("文件夹建立成功"); loadFiles();
  } catch (err) { toast(err.message, true); }
});

// ---------- 浮动上传任务管理器 Drawer ----------
let uploadTasks = [];

$("upload-drawer-close")?.addEventListener("click", () => {
  $("upload-modal").classList.add("hidden");
});

function renderUploadTasks() {
  const box = $("upload-tasks-list");
  if (!box) return;
  if (!uploadTasks.length) {
    box.innerHTML = '<p class="muted text-center py-4">当前无上传任务</p>';
    $("upload-summary").textContent = "0 个任务";
    return;
  }
  const activeCount = uploadTasks.filter((t) => t.status === "uploading").length;
  $("upload-summary").textContent = activeCount > 0 ? `${activeCount} 个正在上传` : `全部完成 (${uploadTasks.length})`;

  box.innerHTML = uploadTasks.map((t) => {
    const color = t.status === "done" ? "var(--ok)" : t.status === "err" ? "var(--danger)" : "var(--primary)";
    const label = t.status === "done" ? "100% (完成)" : t.status === "err" ? "上传出错或取消" : `${t.pct || 0}%`;
    return `
      <div class="upload-task-item">
        <div class="upload-task-header">
          <span><b>${esc(t.name)}</b> <small class="muted">(${fmtSize(t.size)})</small></span>
          <span style="color:${color};font-weight:600">${label}</span>
        </div>
        <div class="upload-task-bar-wrap">
          <div class="upload-task-bar" style="width:${t.pct || 0}%;background:${color}"></div>
        </div>
        <div class="flex justify-end mt-1">
          ${t.status === "uploading" ? `<button class="btn mini danger" onclick="cancelUploadTask('${t.id}')">取消任务</button>` : ''}
        </div>
      </div>`;
  }).join("");
}

window.cancelUploadTask = function(id) {
  const t = uploadTasks.find((item) => String(item.id) === String(id));
  if (t && t.xhr) {
    t.xhr.abort();
    t.status = "err";
    renderUploadTasks();
  }
};

$("btn-upload")?.addEventListener("click", () => {
  if (!curShare) return toast("请先选择要上传到的共享文件夹", true);
  $("upload-input").click();
});

$("upload-input")?.addEventListener("change", () => {
  const files = $("upload-input").files;
  if (!files.length) return;
  $("upload-modal").classList.remove("hidden");

  for (const f of files) {
    const task = {
      id: Math.random().toString(36).substring(2, 9),
      file: f,
      name: f.name,
      size: f.size,
      pct: 0,
      status: "uploading",
      xhr: new XMLHttpRequest(),
    };
    uploadTasks.unshift(task);
    renderUploadTasks();

    const fd = new FormData();
    fd.append("file", f);
    const xhr = task.xhr;
    xhr.open("POST", `/api/files/upload?share=${encodeURIComponent(curShare)}&path=${encodeURIComponent(curPath)}`);
    xhr.upload.onprogress = (ev) => {
      if (ev.lengthComputable) {
        task.pct = Math.round((ev.loaded / ev.total) * 100);
        renderUploadTasks();
      }
    };
    xhr.onload = () => {
      if (xhr.status === 200) {
        task.pct = 100;
        task.status = "done";
      } else {
        task.status = "err";
      }
      renderUploadTasks();
      if (uploadTasks.every((item) => item.status !== "uploading") && activeTab === "files") {
        toast("本批次文件上传操作处理完毕！");
        loadFiles();
      }
    };
    xhr.onerror = () => {
      task.status = "err";
      renderUploadTasks();
    };
    xhr.send(fd);
  }
  $("upload-input").value = "";
});

// ---------- ACL 权限管理 ----------
let aclEntry = null;
const TAG_LABEL = { user: "用户", group: "组", mask: "掩码", other: "其他人" };

async function openAclDialog(e) {
  aclEntry = e;
  $("acl-title").textContent = `访问控制列表 (ACL)：${e.name}`;
  $("acl-dir-opts").style.display = e.is_dir ? "" : "none";
  $("acl-default").checked = false;
  $("acl-recursive").checked = false;
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
    $("acl-ownerinfo").textContent = `物理文件属主 (Owner): ${data.owner} · 属组 (Group): ${data.group}`;
    $("acl-tbody").innerHTML = data.entries.map((en, i) => {
      const label = TAG_LABEL[en.tag] || en.tag;
      const name = en.qualifier || (en.tag === "user" ? `(属主 ${data.owner})` : en.tag === "group" ? `(属组 ${data.group})` : "-");
      const p = (c) => !en.perms.includes(c) ? ""
        : (en.effective && !en.effective.includes(c))
          ? '<span class="acl-clip" title="被网络掩码裁剪">✔</span>' : "✔";
      const removable = en.qualifier && (en.tag === "user" || en.tag === "group");
      return `<tr>
        <td>${esc(label)}</td><td>${esc(name)}</td>
        <td class="acl-perm">${p("r")}</td><td class="acl-perm">${p("w")}</td><td class="acl-perm">${p("x")}</td>
        <td>${en.default ? '<span class="badge on">继承默认</span>' : ""}</td>
        <td class="ops">${removable ? `<button class="btn mini danger" data-aclrm="${i}">删除</button>` : ""}</td>
      </tr>`;
    }).join("") || '<tr><td colspan="7" class="muted text-center">当前无额外 ACL 条目</td></tr>';

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

$("acl-add-btn")?.addEventListener("click", async () => {
  const name = $("acl-name").value.trim();
  if (!name) return toast("请指定或选择目标实体名称", true);
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
    toast("访问控制 ACL 权限已更新成功！");
    loadAcl();
  } catch (err) { toast(err.message, true); }
});

$("acl-clear")?.addEventListener("click", async () => {
  if (!confirm("确定彻底清除该项目及其下所有扩展 ACL，恢复为基础 Unix 权限？")) return;
  try {
    await api("/api/files/acl", { json: {
      share: curShare, path: joinPath(curPath, aclEntry.name),
      action: "clear", recursive: $("acl-recursive").checked,
    }});
    toast("扩展 ACL 权限已清除");
    loadAcl();
  } catch (err) { toast(err.message, true); }
});

$("acl-close")?.addEventListener("click", () => $("acl-dialog").close());
$("acl-close-top")?.addEventListener("click", () => $("acl-dialog").close());

// ---------- Quick Look 长按空格预览 ----------
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
  $("preview-title").textContent = `${e.name} （${fmtSize(e.size)}）`;
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
    pre.textContent = "正在快速读取文件内容…";
    body.appendChild(pre);
    try {
      const res = await fetch(url);
      pre.textContent = await res.text();
    } catch (_) { pre.textContent = "在线预览读取失败"; }
  } else {
    body.innerHTML = `<div class="p-8 text-center muted">此类型文件体积较大或格式需专用工具查看<br><br>
      <button class="btn primary glow-btn" id="preview-dl">立即下载源文件</button></div>`;
    $("preview-dl").addEventListener("click", () => downloadEntry(e));
  }
}

function closePreview() {
  $("preview-overlay").classList.add("hidden");
  $("preview-body").innerHTML = "";
  previewOpen = false;
}

$("preview-overlay")?.addEventListener("click", (ev) => {
  if (ev.target === $("preview-overlay")) closePreview();
});

const inInput = (el) => el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT" || el.isContentEditable);

document.addEventListener("keydown", (ev) => {
  if (ev.code === "Escape" && previewOpen) { closePreview(); return; }
  if (activeTab !== "files" || inInput(document.activeElement)) return;
  if (document.querySelector("dialog[open]")) return;

  if (ev.code === "Space") {
    ev.preventDefault();
    if (ev.repeat || spaceDown) return;
    spaceDown = true;
    const e = curEntries[selectedIdx];
    if (e && !e.is_dir && !previewOpen) {
      spaceHoldTimer = setTimeout(() => openPreview(e), 300);
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
    ev.preventDefault();
    if (previewOpen) return;
    if (curPath) { curPath = curPath.split("/").slice(0, -1).join("/"); loadFiles(); }
  }
});

document.addEventListener("keyup", (ev) => {
  if (ev.code !== "Space") return;
  spaceDown = false;
  clearTimeout(spaceHoldTimer);
  if (previewOpen) closePreview();
});

// ---------- 系统初始化 ----------
(async () => {
  try {
    await api("/api/me");
    showMain();
  } catch (_) {
    showLogin();
  }
})();
