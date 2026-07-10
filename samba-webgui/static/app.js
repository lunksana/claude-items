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
function toast(msg, isErr = false, ms = 3500) {
  const t = $("toast");
  if (!t) return;
  t.textContent = msg;
  t.className = "toast" + (isErr ? " err" : "");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.classList.add("hidden"), ms);
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
  $("login-view")?.classList.remove("hidden");
  $("main-view")?.classList.add("hidden");
}

function showMain() {
  $("login-view")?.classList.add("hidden");
  $("main-view")?.classList.remove("hidden");
  loadStatus();
  loadShares();
  loadUsers();
  refreshShareSelect();
  loadSettings(); // 让"还原上次配置"按钮尽早知道是否有备份
}

let activeTab = "status";
document.querySelectorAll(".nav-item").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".nav-item").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    activeTab = btn.dataset.tab;
    const titles = {
      status: "监控大盘",
      shares: "共享目录管理",
      users: "Samba 账号与权限组",
      files: "系统文件资源管理器",
      settings: "全局配置与监听参数"
    };
    if ($("page-heading")) $("page-heading").textContent = titles[activeTab] || activeTab;

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
$("login-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  if ($("login-error")) $("login-error").textContent = "";
  try {
    await api("/api/login", { json: { password: $("login-password").value } });
    $("login-password").value = "";
    showMain();
  } catch (err) {
    if ($("login-error")) $("login-error").textContent = err.message;
  }
});

$("btn-logout")?.addEventListener("click", async () => {
  if (!confirm("确定要退出 Samba 管理后台？")) return;
  try { await api("/api/logout", { method: "POST" }); } catch (_) {}
  showLogin();
});

// 修改 WebGUI 管理密码
$("btn-change-pw")?.addEventListener("click", () => {
  $("pw-form").reset();
  $("pw-dialog").showModal();
});
$("pw-cancel")?.addEventListener("click", () => $("pw-dialog").close());
$("pw-close")?.addEventListener("click", () => $("pw-dialog").close());
$("pw-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  if ($("pw-new").value !== $("pw-confirm").value) {
    toast("两次输入的新密码不一致，请仔细核对！", true);
    return;
  }
  try {
    await api("/api/password", { json: { old_password: $("pw-old").value, new_password: $("pw-new").value } });
    $("pw-dialog").close();
    $("pw-form").reset();
    toast("WebGUI 管理后台密码已更新成功");
  } catch (err) { toast(err.message, true); }
});

// ---------- 监控大盘及实时状态 ----------
async function loadStatus() {
  try {
    const data = await api("/api/status");
    const smbdBadge = $("badge-smbd");
    if (smbdBadge) {
      smbdBadge.textContent = data.smbd_active ? "运行中 (Active)" : "未启动 (Stopped)";
      smbdBadge.className = "status-pill " + (data.smbd_active ? "active" : "stopped");
    }
    const nmbdBadge = $("badge-nmbd");
    if (nmbdBadge) {
      nmbdBadge.textContent = data.nmbd_active ? "运行中 (Active)" : "未启动 (Stopped)";
      nmbdBadge.className = "status-pill " + (data.nmbd_active ? "active" : "stopped");
    }

    const diskBox = $("status-disks-list");
    if (diskBox && data.disks) {
      diskBox.innerHTML = data.disks.length ? data.disks.map((d) => {
        const pct = Math.min(Math.max(d.pct || 0, 0), 100);
        const cls = pct > 88 ? "danger" : pct > 75 ? "warn" : "ok";
        const mountName = d.mounted_on || d.mount || "-";
        const fsName = d.fs || "-";
        return `
          <div class="disk-item">
            <div class="disk-header">
              <span><b>${esc(mountName)}</b> <small class="muted">(${esc(fsName)})</small></span>
              <span>${fmtSize(d.used)} / ${fmtSize(d.total)} (${pct}%)</span>
            </div>
            <div class="disk-bar-wrap"><div class="disk-bar ${cls}" style="width: ${pct}%"></div></div>
          </div>`;
      }).join("") : '<p class="muted text-center py-4">未检测到有效的数据存储分区</p>';
    }

    const connsTbody = $("status-conns-tbody");
    if (connsTbody && data.connections) {
      if ($("conn-count-badge")) $("conn-count-badge").textContent = `${data.connections.length} 在线`;
      connsTbody.innerHTML = data.connections.length ? data.connections.map((c) => {
        const groupName = c.groupname || c.group || "-";
        const ipAddr = c.ip_addr || c.ip || "-";
        return `
          <tr>
            <td><code style="color:var(--primary);font-weight:600">${c.pid}</code></td>
            <td><b>${esc(c.username || "-")}</b></td>
            <td><span class="badge info">${esc(groupName)}</span></td>
            <td>${esc(c.machine || "-")}</td>
            <td class="muted font-mono">${esc(ipAddr)}</td>
            <td class="ops">
              <button class="btn mini danger" data-kick="${c.pid}">断开连接</button>
            </td>
          </tr>`;
      }).join("") : '<tr><td colspan="6" class="muted text-center p-4">当前没有活跃的客户端访问连接</td></tr>';

      connsTbody.querySelectorAll("[data-kick]").forEach((b) => {
        b.addEventListener("click", async () => {
          const pid = +b.dataset.kick;
          if (!confirm(`确定要强制断开 PID 为 ${pid} 的客户端会话连接？`)) return;
          try {
            await api("/api/status/disconnect", { json: { pid } });
            toast("客户端会话已成功断开");
            loadStatus();
          } catch (err) { toast(err.message, true); }
        });
      });
    }
  } catch (err) { toast(err.message, true); }
}

$("btn-refresh-status")?.addEventListener("click", () => {
  toast("正在更新系统状态大盘...");
  loadStatus();
});

document.querySelectorAll(".service-actions button[data-action]").forEach((btn) => {
  btn.addEventListener("click", async () => {
    const act = btn.dataset.action;
    const labels = { restart: "重启服务", reload: "热加载配置", stop: "停止服务", start: "启动服务" };
    if (act === "stop" && !confirm("警告：停止 Samba 服务将断开所有在线连接和传输，确定执行吗？")) return;
    try {
      toast(`正在${labels[act] || act}...`);
      const r = await api("/api/status/service", { json: { action: act } });
      toast(r.msg || `${labels[act]}执行完成`);
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
        <td class="muted"><code style="color:#334155">${esc(s.path)}</code></td>
        <td>${esc(s.comment || "-")}</td>
        <td>
          <span class="badge ${s.read_only ? 'off' : 'on'}">${s.read_only ? '只读' : '可写'}</span>
          <span class="badge ${s.guest_ok ? 'warn' : 'off'}">${s.guest_ok ? '访客免密' : '需验证'}</span>
        </td>
        <td>
          ${s.recycle ? '<span class="badge info" title="网络回收站开启">♻️ 回收站</span> ' : ''}
          ${s.fruit ? '<span class="badge info" title="macOS 原生兼容开启">🍎 macOS</span>' : ''}
          ${!s.recycle && !s.fruit ? '<span class="muted small">默认</span>' : ''}
        </td>
        <td class="muted small">${esc(s.valid_users || "所有人/任意有效用户")}</td>
        <td>${s.managed ? '<span class="badge on">管理后台建立</span>' : '<span class="badge off">原主配置导入</span>'}</td>
        <td class="ops">${s.managed ? `
          <button class="btn mini" data-edit="${esc(s.name)}">编辑</button>
          <button class="btn mini danger" data-del="${esc(s.name)}">删除</button>` : `
          <button class="btn mini success" data-migrate="${esc(s.name)}" title="迁移至 WebGUI 管理后台中进行全面管理">✨ 接管编辑</button>`}
        </td>
      </tr>`).join("");

    tbody.querySelectorAll("[data-edit]").forEach((b) =>
      b.addEventListener("click", () => openShareDialog(shares.find((s) => s.name === b.dataset.edit))));

    tbody.querySelectorAll("[data-del]").forEach((b) =>
      b.addEventListener("click", async () => {
        if (!confirm(`确定要从 Samba 配置中删除共享项目「${b.dataset.del}」吗？（提示：宿主物理磁盘上的文件或文件夹不会被任何删除）`)) return;
        try {
          const r = await api("/api/shares/" + encodeURIComponent(b.dataset.del), { method: "DELETE" });
          toast(r.message || "共享目录已删除");
          loadShares(); refreshShareSelect();
        } catch (err) { toast(err.message, true); }
      }));

    tbody.querySelectorAll("[data-migrate]").forEach((b) =>
      b.addEventListener("click", async () => {
        const name = b.dataset.migrate;
        if (!confirm(`确定将原主配置中的共享「${name}」接管至 WebGUI 管理平台中托管吗？\n接管后原 smb.conf 中的配置项将被注释保留，您随后即可直接对该目录配置所有读写及高级兼容参数！`)) return;
        try {
          const r = await api("/api/shares/migrate/" + encodeURIComponent(name), { method: "POST" });
          toast(r.msg || "接管成功");
          loadShares(); refreshShareSelect();
        } catch (err) { toast(err.message, true); }
      }));
  } catch (err) { toast(err.message, true); }
}

async function openShareDialog(share) {
  editingShare = share || null;
  $("share-dialog-title").textContent = share ? `编辑共享文件夹：${share.name}` : "新建共享文件夹";
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
  $("sh-read-list").value = share?.read_list || "";
  $("sh-fix-perms").checked = !share;
  // 启用访问控制 = 原本 valid users 非空
  $("sh-access-control").checked = !!(share?.valid_users || "").trim();
  $("sh-matrix-filter").value = "";
  $("share-dialog").showModal();
  await buildPermMatrix(share?.valid_users || "", share?.write_list || "", share?.read_list || "");
  updateMatrixEnabled();
}

$("btn-new-share")?.addEventListener("click", () => openShareDialog(null));
$("share-cancel")?.addEventListener("click", () => $("share-dialog").close());
$("share-close")?.addEventListener("click", () => $("share-dialog").close());

$("share-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  syncMatrixToHidden(); // 权限矩阵 → valid_users/write_list/read_list 隐藏字段
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
    read_list: $("sh-read-list").value.trim(),
    fix_perms: $("sh-fix-perms").checked,
  };
  try {
    const r = editingShare
      ? await api("/api/shares/" + encodeURIComponent(editingShare.name), { method: "PUT", json: body })
      : await api("/api/shares", { json: body });
    $("share-dialog").close();
    toast(r.message || "共享设置保存成功");
    loadShares(); refreshShareSelect();
  } catch (err) { toast(err.message, true); }
});

// ---------- 树形目录浏览/新建路径 ----------
let treeCurrentPath = "";
let treeTargetInputId = "sh-path";

async function openTreeDialog(targetInputId) {
  treeTargetInputId = targetInputId;
  const inputVal = $(targetInputId)?.value || "";
  treeCurrentPath = inputVal.trim() || "";
  $("tree-dialog").showModal();
  await loadTreeDir(treeCurrentPath);
}

async function loadTreeDir(path) {
  treeCurrentPath = path;
  $("tree-current-path").textContent = path ? path : "（快捷根目录候选 / 存储卷选项）";
  $("tree-new-dirname").value = "";
  try {
    const data = await api(`/api/files/tree?path=${encodeURIComponent(path)}`);
    const actualPath = data.path || "";
    const list = Array.isArray(data.dirs) ? data.dirs : [];
    treeCurrentPath = actualPath;
    if ($("tree-current-path")) $("tree-current-path").textContent = actualPath ? actualPath : "（快捷根挂载点选择）";

    const box = $("tree-list-box");
    box.innerHTML = list.length ? list.map((d) => `
      <div class="tree-dir-item" data-dirname="${esc(d)}">
        <span style="font-size:16px">📁</span>
        <b>${esc(d)}</b>
      </div>`).join("") : '<div class="p-4 muted text-center">当前文件夹下无子目录</div>';

    box.querySelectorAll(".tree-dir-item").forEach((item) => {
      item.addEventListener("click", () => {
        const dirname = item.dataset.dirname;
        let next = dirname;
        if (actualPath !== "" && actualPath !== "/") {
          next = joinPath(actualPath, dirname);
        } else if (actualPath === "/") {
          next = "/" + dirname;
        }
        loadTreeDir(next);
      });
    });
  } catch (err) {
    toast("载入该目录失败：" + err.message, true);
    if (path !== "" && path !== "/") loadTreeDir("");
  }
}

$("sh-browse-btn")?.addEventListener("click", () => openTreeDialog("sh-path"));
$("tree-close")?.addEventListener("click", () => $("tree-dialog").close());
$("tree-cancel-btn")?.addEventListener("click", () => $("tree-dialog").close());

$("tree-up-btn")?.addEventListener("click", () => {
  if (!treeCurrentPath || treeCurrentPath === "/") {
    loadTreeDir("");
    return;
  }
  const parts = treeCurrentPath.split("/").filter(Boolean);
  parts.pop();
  const up = parts.length ? "/" + parts.join("/") : "/";
  loadTreeDir(up);
});

$("tree-mkdir-do-btn")?.addEventListener("click", async () => {
  if (!treeCurrentPath || treeCurrentPath === "") {
    return toast("请先点击选择一个具体物理父级路径（如 /srv 或 /mnt），然后再建立子文件夹", true);
  }
  const name = $("tree-new-dirname").value.trim();
  if (!name) return toast("请填写要新建的子文件夹名称", true);
  try {
    const r = await api("/api/files/tree/mkdir", { json: { path: treeCurrentPath, name } });
    toast("目录建立成功");
    if (r.path) treeCurrentPath = r.path;
    loadTreeDir(treeCurrentPath);
  } catch (err) { toast(err.message, true); }
});

$("tree-confirm-btn")?.addEventListener("click", () => {
  if (!treeCurrentPath || treeCurrentPath === "") {
    return toast("请点击具体候选目录（例如 /srv /mnt /data）或具体子路径确认", true);
  }
  const target = $(treeTargetInputId);
  if (target) target.value = treeCurrentPath;
  $("tree-dialog").close();
});

// ---------- 系统用户/组选择多选器弹窗 ----------
// ---------- 权限矩阵（群晖式：用户/组 × 无权限/只读/读写）----------
function parseList(s) {
  return (s || "").split(/[,\s]+/).map((x) => x.trim()).filter(Boolean);
}

async function buildPermMatrix(validUsers, writeList, readList) {
  const box = $("sh-perm-matrix");
  if (!box) return;
  const validSet = new Set(parseList(validUsers));
  const writeSet = new Set(parseList(writeList));
  const readSet = new Set(parseList(readList));
  let entities = [];
  try {
    const [ur, gr] = await Promise.all([api("/api/users"), api("/api/groups")]);
    (ur.users || []).forEach((u) => entities.push({ token: u.username, label: u.username, tag: "用户" }));
    const groups = Array.isArray(gr.groups) ? gr.groups : [];
    groups.forEach((g) => {
      const name = typeof g === "string" ? g : g.groupname;
      if (name) entities.push({ token: "@" + name, label: "@" + name, tag: "组" });
    });
  } catch (err) { toast(err.message, true); }
  // 配置里出现但系统列表没有的条目（自定义/历史），补进来不丢失
  const known = new Set(entities.map((e) => e.token));
  [...validSet, ...writeSet, ...readSet].forEach((t) => {
    if (!known.has(t)) { entities.push({ token: t, label: t, tag: "自定义" }); known.add(t); }
  });

  const stateOf = (token) => writeSet.has(token) ? "rw"
    : (validSet.has(token) || readSet.has(token)) ? "ro" : "none";

  box.innerHTML =
    `<div class="pm-head"><span class="pm-name">用户 / 用户组</span><div class="pm-opts"><span>无</span><span>只读</span><span>读写</span></div></div>` +
    (entities.length ? entities.map((e, i) => {
      const st = stateOf(e.token);
      const radio = (val) => `<label><input type="radio" name="pm-${i}" value="${val}" ${st === val ? "checked" : ""}></label>`;
      return `<div class="pm-row" data-token="${esc(e.token)}" data-label="${esc(e.label.toLowerCase())}">
        <span class="pm-name">${esc(e.label)}<span class="tag">${e.tag}</span></span>
        <div class="pm-opts">${radio("none")}${radio("ro")}${radio("rw")}</div>
      </div>`;
    }).join("") : `<div class="pm-empty">系统暂无用户 / 用户组</div>`);
}

function syncMatrixToHidden() {
  // 未启用访问控制：清空三者，所有有效用户按共享默认访问
  if (!$("sh-access-control").checked) {
    $("sh-valid-users").value = "";
    $("sh-write-list").value = "";
    $("sh-read-list").value = "";
    return;
  }
  const valid = [], write = [], read = [];
  $("sh-perm-matrix").querySelectorAll(".pm-row").forEach((row) => {
    const token = row.dataset.token;
    const sel = row.querySelector("input[type=radio]:checked");
    const st = sel ? sel.value : "none";
    if (st === "rw") { valid.push(token); write.push(token); }
    else if (st === "ro") { valid.push(token); read.push(token); }
  });
  $("sh-valid-users").value = valid.join(", ");
  $("sh-write-list").value = write.join(", ");
  $("sh-read-list").value = read.join(", ");
}

function updateMatrixEnabled() {
  const on = $("sh-access-control").checked;
  $("sh-perm-matrix")?.classList.toggle("disabled", !on);
  $("sh-matrix-group").style.opacity = on ? "1" : ".55";
}
$("sh-access-control")?.addEventListener("change", updateMatrixEnabled);
$("sh-matrix-filter")?.addEventListener("input", (e) => {
  const q = e.target.value.trim().toLowerCase();
  $("sh-perm-matrix").querySelectorAll(".pm-row").forEach((row) => {
    row.style.display = !q || row.dataset.label.includes(q) ? "" : "none";
  });
});

let multiSelectTargetInput = "sh-valid-users";
async function openMultiSelectDialog(targetInputId, title) {
  multiSelectTargetInput = targetInputId;
  $("multi-select-title").textContent = title;
  try {
    const [usersRes, groupsRes] = await Promise.all([api("/api/users"), api("/api/groups")]);
    const currentList = ($(targetInputId)?.value || "").split(",").map((s) => s.trim()).filter(Boolean);
    const box = $("multi-select-list");
    let html = `<div class="muted small mb-2">您可以自由勾选指定访问用户，或勾选以 @ 开头的整个用户组：</div>`;

    const users = Array.isArray(usersRes.users) ? usersRes.users : [];
    users.forEach((u) => {
      const checked = currentList.includes(u.username) ? "checked" : "";
      html += `<label class="check"><input type="checkbox" value="${esc(u.username)}" ${checked}> 👤 ${esc(u.username)} <small class="muted">(${esc(u.uid || "")})</small></label>`;
    });

    const groups = Array.isArray(groupsRes.groups) ? groupsRes.groups : (Array.isArray(groupsRes) ? groupsRes : []);
    groups.forEach((g) => {
      const gName = typeof g === "string" ? g : (g.groupname || "");
      if (!gName) return;
      const val = "@" + gName;
      const checked = currentList.includes(val) ? "checked" : "";
      html += `<label class="check"><input type="checkbox" value="${esc(val)}" ${checked}> 👥 @${esc(gName)} <small class="muted">(整个系统用户组)</small></label>`;
    });

    box.innerHTML = html;
    $("multi-select-dialog").showModal();
  } catch (err) { toast(err.message, true); }
}

$("sh-pick-valid-users")?.addEventListener("click", () => openMultiSelectDialog("sh-valid-users", "勾选允许访问的用户/组"));
$("sh-pick-write-list")?.addEventListener("click", () => openMultiSelectDialog("sh-write-list", "勾选拥有可写权限的用户/组"));
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
      const groupsList = Array.isArray(u.groups) ? u.groups : [];
      const groupsStr = groupsList.map((g) => `<span class="badge info">${esc(g)}</span>`).join(" ");
      return `
        <tr>
          <td><b>${esc(u.username)}</b></td>
          <td class="muted"><code>${esc(u.uid)}</code></td>
          <td>${groupsStr || '<span class="muted">-</span>'}</td>
          <td>${u.disabled ? '<span class="badge danger">已禁用</span>' : '<span class="badge on">正常启用</span>'}</td>
          <td class="ops">
            <button class="btn mini" data-pw="${esc(u.username)}">🔑 改密</button>
            <button class="btn mini outline" data-groups="${esc(u.username)}" data-curgroups="${esc(groupsList.join(','))}">👥 调整组</button>
            <button class="btn mini" data-toggle="${esc(u.username)}" data-enabled="${u.disabled ? 1 : 0}">${u.disabled ? "🟢 启用" : "🔴 禁用"}</button>
            <button class="btn mini danger" data-deluser="${esc(u.username)}">🗑️ 删除</button>
          </td>
        </tr>`;
    }).join("") : '<tr><td colspan="5" class="muted text-center p-4">暂无共享用户</td></tr>';

    tbody.querySelectorAll("[data-pw]").forEach((b) =>
      b.addEventListener("click", () => {
        $("user-pw-form").reset();
        $("user-pw-title").textContent = `重置口令：${b.dataset.pw}`;
        $("user-pw-form").dataset.username = b.dataset.pw;
        $("user-pw-dialog").showModal();
      }));

    tbody.querySelectorAll("[data-groups]").forEach((b) =>
      b.addEventListener("click", async () => {
        const username = b.dataset.groups;
        const curGroups = b.dataset.curgroups.split(",").filter(Boolean);
        $("user-group-title").textContent = `调整用户组归属：${username}`;
        $("user-group-form").dataset.username = username;
        try {
          const res = await api("/api/groups");
          const groups = Array.isArray(res.groups) ? res.groups : (Array.isArray(res) ? res : []);
          const box = $("user-group-list");
          box.innerHTML = groups.map((g) => {
            const gName = typeof g === "string" ? g : (g.groupname || "");
            if (!gName) return "";
            const checked = curGroups.includes(gName) ? "checked" : "";
            return `<label class="check"><input type="checkbox" value="${esc(gName)}" ${checked}> 👥 ${esc(gName)}</label>`;
          }).join("");
          $("user-group-dialog").showModal();
        } catch (err) { toast(err.message, true); }
      }));

    tbody.querySelectorAll("[data-toggle]").forEach((b) =>
      b.addEventListener("click", async () => {
        try {
          await api(`/api/users/${encodeURIComponent(b.dataset.toggle)}/enable`,
            { method: "PUT", json: { enabled: b.dataset.enabled === "1" } });
          toast(b.dataset.enabled === "1" ? "账号已启用" : "账号已禁用");
          loadUsers();
        } catch (err) { toast(err.message, true); }
      }));

    tbody.querySelectorAll("[data-deluser]").forEach((b) =>
      b.addEventListener("click", async () => {
        if (!confirm(`确定要从 Samba 数据库中删除用户「${b.dataset.deluser}」吗？`)) return;
        try {
          await api("/api/users/" + encodeURIComponent(b.dataset.deluser), { method: "DELETE" });
          toast("用户已删除"); loadUsers();
        } catch (err) { toast(err.message, true); }
      }));
  } catch (err) { toast(err.message, true); }
}

// 新建账号（含用户组选择 / 新建）
async function renderNewUserGroups(checkedSet) {
  const box = $("u-groups-list");
  if (!box) return;
  try {
    const res = await api("/api/groups");
    const groups = Array.isArray(res.groups) ? res.groups : (Array.isArray(res) ? res : []);
    box.innerHTML = groups.length ? groups.map((g) => {
      const gName = typeof g === "string" ? g : (g.groupname || "");
      const checked = checkedSet.has(gName) ? "checked" : "";
      return `<label class="check"><input type="checkbox" value="${esc(gName)}" ${checked}> 👥 ${esc(gName)}</label>`;
    }).join("") : '<span class="muted small">系统暂无用户组</span>';
  } catch (err) { toast(err.message, true); }
}
function currentCheckedGroups() {
  const s = new Set();
  $("u-groups-list")?.querySelectorAll("input[type=checkbox]:checked").forEach((c) => s.add(c.value));
  return s;
}

$("btn-new-user")?.addEventListener("click", async () => {
  $("user-form").reset();
  $("u-groups-list").innerHTML = "";
  await renderNewUserGroups(new Set());
  $("user-dialog").showModal();
});
$("user-cancel")?.addEventListener("click", () => $("user-dialog").close());
$("user-close")?.addEventListener("click", () => $("user-dialog").close());

// 新建组：创建后刷新清单并自动勾选，保留已勾选项
$("u-add-group-btn")?.addEventListener("click", async () => {
  const name = $("u-new-group").value.trim();
  if (!name) return toast("请输入要新建的用户组名称", true);
  try {
    await api("/api/groups", { json: { name } });
    const checked = currentCheckedGroups();
    checked.add(name);
    await renderNewUserGroups(checked);
    $("u-new-group").value = "";
    toast(`用户组 ${name} 已就绪`);
  } catch (err) { toast(err.message, true); }
});

$("user-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  if ($("u-password").value !== $("u-password-confirm").value) {
    toast("两次输入的密码不一致，请仔细检查！", true);
    return;
  }
  const groups = [...currentCheckedGroups()];
  try {
    const r = await api("/api/users", { json: { username: $("u-name").value.trim(), password: $("u-password").value, groups } });
    $("user-dialog").close();
    // 半成功（用户建了但组没配好）用醒目红色 + 长停留，便于排查是哪一步
    if (r.warn) toast(r.message, true, 8000);
    else toast(r.message || "Samba 用户账号创建成功");
    loadUsers();
  } catch (err) { toast(err.message, true); }
});

// 重置用户密码
$("upw-cancel")?.addEventListener("click", () => $("user-pw-dialog").close());
$("user-pw-close")?.addEventListener("click", () => $("user-pw-dialog").close());
$("user-pw-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  if ($("upw-new").value !== $("upw-confirm").value) {
    toast("两次确认的新口令不一致！", true);
    return;
  }
  const username = $("user-pw-form").dataset.username;
  try {
    await api(`/api/users/${encodeURIComponent(username)}/password`, { method: "PUT", json: { password: $("upw-new").value } });
    $("user-pw-dialog").close();
    toast(`用户 ${username} 访问口令重置成功`);
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
    toast(`账号 ${username} 组成员资格已更新并立刻生效`);
    loadUsers();
  } catch (err) { toast(err.message, true); }
});

// ---------- 配置还原（单级撤销）----------
let lastBackupTs = null;
async function restoreConfig() {
  const when = lastBackupTs ? `（备份于 ${fmtTime(lastBackupTs)}）` : "";
  if (!confirm(`确定还原到最近一次修改前的配置吗？${when}\n这会撤销你上一次对共享/全局配置的改动，并重新加载 Samba。`)) return;
  try {
    const r = await api("/api/config/restore", { method: "POST" });
    toast(r.message || "已还原上次配置");
    loadShares(); refreshShareSelect(); loadSettings(); loadStatus();
  } catch (err) { toast(err.message, true); }
}
$("btn-restore-config")?.addEventListener("click", restoreConfig);
$("btn-restore-config-2")?.addEventListener("click", restoreConfig);

// ---------- 全局系统配置 ----------
async function loadSettings() {
  try {
    const cfg = await api("/api/config");
    if ($("set-listen-addr")) $("set-listen-addr").value = cfg.listen_addr || "0.0.0.0:8686";
    if ($("set-session-ttl")) $("set-session-ttl").value = cfg.session_ttl_hours || 24;
    if ($("set-guest-map")) $("set-guest-map").checked = !!cfg.guest_map_bad_user;
    if ($("set-smb-min")) $("set-smb-min").value = (cfg.smb_min_protocol || "").toUpperCase();
    if ($("set-smb-max")) $("set-smb-max").value = (cfg.smb_max_protocol || "").toUpperCase();
    lastBackupTs = cfg.backup_ts || null;
    updateRestoreButtons();
  } catch (err) { toast(err.message, true); }
}

function updateRestoreButtons() {
  const has = !!lastBackupTs;
  const tip = has ? `还原到 ${fmtTime(lastBackupTs)} 的配置` : "暂无可还原的配置（尚未修改过）";
  ["btn-restore-config", "btn-restore-config-2"].forEach((id) => {
    const b = $(id);
    if (b) { b.disabled = !has; b.title = tip; }
  });
}

$("settings-form")?.addEventListener("submit", async (e) => {
  e.preventDefault();
  try {
    const body = {
      listen_addr: $("set-listen-addr").value.trim(),
      session_ttl_hours: Number($("set-session-ttl").value) || 24,
      guest_map_bad_user: $("set-guest-map").checked,
      smb_min_protocol: $("set-smb-min")?.value || "",
      smb_max_protocol: $("set-smb-max")?.value || "",
    };
    await api("/api/config", { json: body });
    toast("全局参数已保存成功（网络监听修改将在下一次重启后生效）");
    loadSettings();
  } catch (err) { toast(err.message, true); }
});

// ---------- 文件资源管理器及批量操作 ----------
let curShare = "", curPath = "", curEntries = [], selectedIdx = -1;
let filesReqSeq = 0;

async function refreshShareSelect() {
  try {
    const { shares } = await api("/api/shares");
    const sel = $("file-share");
    if (!sel) return;
    const prev = sel.value;
    sel.innerHTML = shares.map((s) => `<option value="${esc(s.name)}">${esc(s.name)} (${esc(s.path)})</option>`).join("");
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
    resetFileProps();
    renderBreadcrumb();

    const tbody = $("files-tbody");
    if (!tbody) return;
    tbody.innerHTML = entries.length ? entries.map((e, i) => {
      const isArchive = !e.is_dir && /\.(tar|gz|bz2|xz|zip)$/i.test(e.name);
      return `
        <tr data-idx="${i}">
          <td style="width:40px"><input type="checkbox" class="file-chk" data-name="${esc(e.name)}"></td>
          <td class="file-name"><span style="margin-right:8px">${fileIcon(e)}</span>${esc(e.name)}</td>
          <td class="muted">${e.is_dir ? "-" : fmtSize(e.size)}</td>
          <td class="muted">${fmtTime(e.mtime)}</td>
          <td class="ops">
            ${isArchive ? `<button class="btn mini success" data-extract="${i}" title="直接在当前文件夹下解压包内容">解压</button>` : ""}
            ${e.is_dir ? "" : `<button class="btn mini" data-dl="${i}">下载</button>`}
            <button class="btn mini outline" data-acl="${i}">权限</button>
            <button class="btn mini danger" data-rm="${i}">删除</button>
          </td>
        </tr>`;
    }).join("") : '<tr><td colspan="5" class="muted text-center p-6">当前目录为空</td></tr>';

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
        if (!confirm(`确定要直接在当前位置解压文件包「${e.name}」吗？`)) return;
        try {
          toast("正在解压，请耐心等待...");
          await api("/api/files/extract", { json: { share: curShare, path: joinPath(curPath, e.name) } });
          toast("解压缩全部完成！"); loadFiles();
        } catch (err) { toast(err.message, true); }
      }));

    tbody.querySelectorAll("[data-acl]").forEach((b) =>
      b.addEventListener("click", (ev) => { ev.stopPropagation(); openAclDialog(curEntries[+b.dataset.acl]); }));

    tbody.querySelectorAll("[data-rm]").forEach((b) =>
      b.addEventListener("click", async (ev) => {
        ev.stopPropagation();
        const e = curEntries[+b.dataset.rm];
        if (!confirm(`确定删除项目「${e.name}」吗？${e.is_dir ? "（⚠️ 警告：目录及其下全部子文件均将被递归清除）" : ""}`)) return;
        try {
          await api("/api/files/delete", { json: { share: curShare, path: joinPath(curPath, e.name) } });
          toast("项目已成功彻底删除"); loadFiles();
        } catch (err) { toast(err.message, true); }
      }));
  } catch (err) { toast(err.message, true); }
}

function selectRow(i) {
  selectedIdx = i;
  document.querySelectorAll("#files-tbody tr").forEach((tr) =>
    tr.classList.toggle("selected", +tr.dataset.idx === i));
  loadFileProps(i);
}

let propsSeq = 0;
function resetFileProps() {
  const box = $("file-props");
  if (box) box.innerHTML = '<div class="fp-empty muted text-center">单击左侧文件或文件夹<br>查看详细属性</div>';
}
async function loadFileProps(i) {
  const e = curEntries[i];
  const box = $("file-props");
  if (!e || !box) return;
  const seq = ++propsSeq;
  try {
    const d = await api(`/api/files/stat?share=${encodeURIComponent(curShare)}&path=${encodeURIComponent(joinPath(curPath, e.name))}`);
    if (seq !== propsSeq) return; // 已选别的，丢弃过期结果
    const kind = d.is_dir ? "文件夹" : (d.mime || "文件");
    box.innerHTML = `
      <div class="fp-head">
        <div class="fp-icon">${fileIcon(e)}</div>
        <div class="fp-name">${esc(d.name)}</div>
      </div>
      <div class="fp-list">
        <div class="fp-row"><span class="k">类型</span><span class="v">${esc(kind)}</span></div>
        <div class="fp-row"><span class="k">大小</span><span class="v">${d.is_dir ? "-" : fmtSize(d.size)}</span></div>
        <div class="fp-row"><span class="k">修改时间</span><span class="v">${fmtTime(d.mtime)}</span></div>
        <div class="fp-row"><span class="k">属主</span><span class="v">${esc(d.owner)}</span></div>
        <div class="fp-row"><span class="k">属组</span><span class="v">${esc(d.group)}</span></div>
        <div class="fp-row"><span class="k">权限</span><span class="v mono">${esc(d.perms)} (${esc(d.mode)})</span></div>
      </div>
      <div class="fp-actions">
        ${d.is_dir ? "" : '<button class="btn mini" id="fp-preview">👁 预览</button>'}
        ${d.is_dir ? "" : '<button class="btn mini" id="fp-dl">⬇ 下载</button>'}
        <button class="btn mini outline" id="fp-acl">🔑 权限 (ACL)</button>
      </div>`;
    $("fp-preview")?.addEventListener("click", () => openPreview(e));
    $("fp-dl")?.addEventListener("click", () => downloadEntry(e));
    $("fp-acl")?.addEventListener("click", () => openAclDialog(e));
  } catch (err) {
    if (seq === propsSeq) box.innerHTML = `<div class="fp-empty muted text-center">${esc(err.message)}</div>`;
  }
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
  if (!items.length) return toast("请先在表格左侧勾选要转移或复制的项目！", true);
  try {
    const { shares } = await api("/api/shares");
    $("cm-target-share").innerHTML = shares.map((s) => `<option value="${esc(s.name)}">${esc(s.name)}</option>`).join("");
    $("cm-target-share").value = curShare;
    $("copy-move-hint").textContent = `已选定 ${items.length} 个项目，选择它们的目标位置：`;
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
  toast(`正在批量复制 ${items.length} 个项目，请稍候...`);
  try {
    for (const name of items) {
      // dst_path 是目标「目录」，后端会自动拼接源文件名
      await api("/api/files/copy", { json: {
        src_share: curShare, src_path: joinPath(curPath, name),
        dst_share: dstShare, dst_path: dstPath
      }});
    }
    toast("全量复制操作已完成！"); loadFiles();
  } catch (err) { toast(err.message, true); }
});

$("cm-do-move")?.addEventListener("click", async () => {
  const items = getCheckedFileNames();
  const dstShare = $("cm-target-share").value;
  const dstPath = $("cm-target-path").value.trim();
  if (!confirm(`确定将勾选的 ${items.length} 个项目剪切移动到 ${dstShare}/${dstPath} 吗？`)) return;
  $("copy-move-dialog").close();
  toast(`正在批量移动 ${items.length} 个项目...`);
  try {
    for (const name of items) {
      // dst_path 是目标「目录」，后端会自动拼接源文件名
      await api("/api/files/move", { json: {
        src_share: curShare, src_path: joinPath(curPath, name),
        dst_share: dstShare, dst_path: dstPath
      }});
    }
    toast("剪切移动已全部完成！"); loadFiles();
  } catch (err) { toast(err.message, true); }
});

// 选定项打包归档为 .tar.gz
$("btn-batch-archive")?.addEventListener("click", async () => {
  const items = getCheckedFileNames();
  if (!items.length) return toast("请先勾选需要打包下载的项目", true);
  const name = prompt("设定生成的压缩文件名称 (须以 .tar.gz 结尾)：", "archive_files.tar.gz");
  if (!name) return;
  const archiveName = name.endsWith(".tar.gz") ? name : name + ".tar.gz";
  toast("服务端正在打包压缩中，请耐心等候...");
  try {
    const r = await api("/api/files/archive", { json: { share: curShare, path: curPath, items, archive_name: archiveName } });
    toast(r.message || "打包完成，可在当前目录下下载！"); loadFiles();
  } catch (err) { toast(err.message, true); }
});

// 新建文件夹
$("btn-mkdir")?.addEventListener("click", async () => {
  if (!curShare) return toast("请先在上方下拉菜单选择目标共享目录", true);
  const name = prompt("新文件夹名称：");
  if (!name) return;
  try {
    await api("/api/files/mkdir", { json: { share: curShare, path: curPath, name } });
    toast("文件夹创建成功"); loadFiles();
  } catch (err) { toast(err.message, true); }
});

// ---------- 右下角浮动上传任务栏 Drawer ----------
let uploadTasks = [];

$("upload-drawer-close")?.addEventListener("click", () => {
  $("upload-modal").classList.add("hidden");
});

function renderUploadTasks() {
  const box = $("upload-tasks-list");
  if (!box) return;
  if (!uploadTasks.length) {
    box.innerHTML = '<p class="muted text-center py-4">无上传任务</p>';
    if ($("upload-summary")) $("upload-summary").textContent = "0 任务";
    return;
  }
  const activeCount = uploadTasks.filter((t) => t.status === "uploading").length;
  if ($("upload-summary")) {
    $("upload-summary").textContent = activeCount > 0 ? `${activeCount} 正在传输` : `全部就绪 (${uploadTasks.length})`;
  }

  box.innerHTML = uploadTasks.map((t) => {
    const color = t.status === "done" ? "var(--ok)" : t.status === "err" ? "var(--danger)" : "var(--primary)";
    const label = t.status === "done" ? "100% 完成" : t.status === "err" ? "错误或已取消" : `${t.pct || 0}%`;
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
          ${t.status === "uploading" ? `<button class="btn mini danger" onclick="cancelUploadTask('${t.id}')">中止</button>` : ''}
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
  if (!curShare) return toast("请先选定具体的共享文件夹", true);
  $("upload-input").click();
});

$("upload-input")?.addEventListener("change", () => {
  const files = $("upload-input").files;
  if (!files.length) return;
  $("upload-modal")?.classList.remove("hidden");

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
        toast("全部文件上传处理完毕！");
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

// ---------- 访问控制权限 (ACL) ----------
let aclEntry = null;
const TAG_LABEL = { user: "用户", group: "组", mask: "掩码", other: "其他人" };

async function openAclDialog(e) {
  aclEntry = e;
  $("acl-title").textContent = `访问控制权限 (ACL)：${e.name}`;
  if ($("acl-dir-opts")) $("acl-dir-opts").style.display = e.is_dir ? "" : "none";
  if ($("acl-default")) $("acl-default").checked = false;
  if ($("acl-recursive")) $("acl-recursive").checked = false;
  try {
    const { users } = await api("/api/users");
    if ($("acl-userlist")) $("acl-userlist").innerHTML = users.map((u) => `<option value="${esc(u.username)}">`).join("");
  } catch (_) {}
  $("acl-dialog").showModal();
  await loadAcl();
}

async function loadAcl() {
  const e = aclEntry;
  try {
    const data = await api(`/api/files/acl?share=${encodeURIComponent(curShare)}&path=${encodeURIComponent(joinPath(curPath, e.name))}`);
    if ($("acl-ownerinfo")) $("acl-ownerinfo").textContent = `文件/目录属主: ${data.owner} · 所属组: ${data.group}`;
    const tbody = $("acl-tbody");
    if (tbody) {
      tbody.innerHTML = data.entries.map((en, i) => {
        const label = TAG_LABEL[en.tag] || en.tag;
        const name = en.qualifier || (en.tag === "user" ? `(属主 ${data.owner})` : en.tag === "group" ? `(属组 ${data.group})` : "-");
        const p = (c) => !en.perms.includes(c) ? ""
          : (en.effective && !en.effective.includes(c))
            ? '<span class="acl-clip" title="被最高掩码 Mask 裁剪">✔</span>' : "✔";
        const removable = en.qualifier && (en.tag === "user" || en.tag === "group");
        return `<tr>
          <td>${esc(label)}</td><td>${esc(name)}</td>
          <td class="acl-perm">${p("r")}</td><td class="acl-perm">${p("w")}</td><td class="acl-perm">${p("x")}</td>
          <td>${en.default ? '<span class="badge on">继承默认</span>' : ""}</td>
          <td class="ops">${removable ? `<button class="btn mini danger" data-aclrm="${i}">删除</button>` : ""}</td>
        </tr>`;
      }).join("") || '<tr><td colspan="7" class="muted text-center p-4">无扩展 ACL 条目</td></tr>';

      tbody.querySelectorAll("[data-aclrm]").forEach((b) =>
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
    }
  } catch (err) { toast(err.message, true); }
}

$("acl-add-btn")?.addEventListener("click", async () => {
  const name = $("acl-name").value.trim();
  if (!name) return toast("请指定目标用户或组名", true);
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
    toast("访问控制权限设置已更新！");
    loadAcl();
  } catch (err) { toast(err.message, true); }
});

$("acl-clear")?.addEventListener("click", async () => {
  if (!confirm("确定清除该项目全部扩展 ACL 规则，恢复为 Linux 基础 Unix 权限吗？")) return;
  try {
    await api("/api/files/acl", { json: {
      share: curShare, path: joinPath(curPath, aclEntry.name),
      action: "clear", recursive: $("acl-recursive").checked,
    }});
    toast("扩展 ACL 规则已清空");
    loadAcl();
  } catch (err) { toast(err.message, true); }
});

$("acl-close")?.addEventListener("click", () => $("acl-dialog").close());
$("acl-close-top")?.addEventListener("click", () => $("acl-dialog").close());

// ---------- 快速预览 Quick Look (长按空格超大超清晰体验) ----------
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
  if (!body) return;
  $("preview-title").textContent = `${e.name} (${fmtSize(e.size)})`;
  body.innerHTML = "";
  $("preview-overlay")?.classList.remove("hidden");
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
  } else if (TEXT_EXT.test(e.name) && e.size <= 3 * 1024 * 1024) {
    const pre = document.createElement("pre");
    pre.textContent = "正在快速读取并加载文件内容…";
    body.appendChild(pre);
    try {
      const res = await fetch(url);
      pre.textContent = await res.text();
    } catch (_) { pre.textContent = "预览失败：无法读取文件内容"; }
  } else {
    body.innerHTML = `<div class="p-8 text-center muted" style="padding:40px">
      <p style="font-size:15px;margin-bottom:20px">当前文件格式或超大体积不受在线快速预览支持</p>
      <button class="btn primary" id="preview-dl" style="padding:10px 24px">立即直接下载</button>
    </div>`;
    $("preview-dl")?.addEventListener("click", () => downloadEntry(e));
  }
}

window.closePreview = function() {
  $("preview-overlay")?.classList.add("hidden");
  if ($("preview-body")) $("preview-body").innerHTML = "";
  previewOpen = false;
};

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
      spaceHoldTimer = setTimeout(() => openPreview(e), 280);
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

// ---------- 系统启动 ----------
(async () => {
  try {
    await api("/api/me");
    showMain();
  } catch (_) {
    showLogin();
  }
})();
