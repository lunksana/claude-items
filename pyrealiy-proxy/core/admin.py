"""Embedded HTTP admin server — zero extra dependencies."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import urlparse, parse_qs

from .stats import StatsStore

# ── embedded dashboard ─────────────────────────────────────────────────────────

_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PyReality 监控面板</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;overflow:hidden;background:#0f111a;color:#c9d1d9;font-family:'Courier New',monospace;font-size:13px}

/* ── 整体三区布局 ── */
.layout{display:flex;height:100vh}

/* ── 左导航 ── */
.nav{width:160px;flex-shrink:0;background:#0d1117;border-right:1px solid #21262d;display:flex;flex-direction:column;padding:0}
.nav-brand{padding:18px 16px 14px;color:#58a6ff;font-size:14px;font-weight:bold;letter-spacing:.5px;border-bottom:1px solid #21262d}
.nav-menu{flex:1;padding:10px 0}
.nav-item{padding:10px 18px;cursor:pointer;color:#8b949e;border-left:2px solid transparent;transition:all .15s}
.nav-item:hover{color:#c9d1d9;background:#161b22}
.nav-item.on{color:#58a6ff;border-left-color:#58a6ff;background:#161b22}
.nav-item .badge{float:right;background:#30363d;color:#8b949e;border-radius:10px;padding:1px 7px;font-size:10px;line-height:1.6}
.nav-item.on .badge{background:#1f3a5f;color:#79c0ff}
.nav-ts{padding:12px 16px;color:#484f58;font-size:10px;border-top:1px solid #21262d}

/* ── 右侧：上统计 + 下内容 ── */
.right{flex:1;display:flex;flex-direction:column;overflow:hidden}

/* 上栏：统计 */
.stats{display:flex;gap:0;border-bottom:1px solid #21262d;background:#161b22;flex-shrink:0}
.stat{flex:1;padding:14px 20px;border-right:1px solid #21262d}
.stat:last-child{border-right:none}
.stat-val{font-size:22px;font-weight:bold;color:#58a6ff;line-height:1}
.stat-val.red{color:#f85149}
.stat-lbl{font-size:10px;color:#8b949e;margin-top:5px;letter-spacing:.3px}

/* 下栏：内容面板 */
.panels{flex:1;overflow-y:auto;padding:20px 24px}
.panel{display:none}
.panel.on{display:block}

/* ── 表格 ── */
table{width:100%;border-collapse:collapse}
th{background:#161b22;color:#8b949e;padding:6px 10px;text-align:left;border-bottom:1px solid #30363d;font-weight:normal;font-size:10px;letter-spacing:.5px;text-transform:uppercase;position:sticky;top:0}
td{padding:6px 10px;border-bottom:1px solid #161b22;white-space:nowrap}
tr:hover td{background:#161b22}
.empty{color:#484f58;font-style:italic}

/* ── 按钮 ── */
.btn{border:none;padding:2px 9px;border-radius:3px;cursor:pointer;font-size:11px;font-family:inherit;line-height:1.6}
.btn-red{background:#da3633;color:#fff}.btn-red:hover{background:#f85149}
.btn-grn{background:#238636;color:#fff}.btn-grn:hover{background:#2ea043}
.btn-gray{background:#30363d;color:#c9d1d9}.btn-gray:hover{background:#3d444e}
.dim td{opacity:.5}.dim td:last-child{opacity:1}
.sh{cursor:pointer;user-select:none}.sh:hover{color:#c9d1d9}.sa{color:#58a6ff}.arr{font-size:9px;opacity:.8}
.cg{color:#3fb950}.cy{color:#d29922}.co{color:#e3b341}
</style>
</head>
<body>
<div class="layout">

<!-- ── 左导航 ── -->
<nav class="nav">
  <div class="nav-brand">PyReality</div>
  <div class="nav-menu">
    <div class="nav-item on" onclick="show('conns')"  id="nav-conns" >活跃连接  <span class="badge" id="b-active">0</span></div>
    <div class="nav-item"   onclick="show('recent')" id="nav-recent">连接记录  <span class="badge" id="b-recent">0</span></div>
    <div class="nav-item"   onclick="show('domains')" id="nav-domains">域名分布</div>
    <div class="nav-item"   onclick="show('blocked')" id="nav-blocked">黑名单    <span class="badge" id="b-blocked">0</span></div>
  </div>
  <div class="nav-ts" id="ts">—</div>
</nav>

<!-- ── 右侧 ── -->
<div class="right">

  <!-- 上栏：统计 -->
  <div class="stats">
    <div class="stat"><div class="stat-val" id="s-active">0</div><div class="stat-lbl">活跃连接</div></div>
    <div class="stat"><div class="stat-val" id="s-total">0</div><div class="stat-lbl">累计连接</div></div>
    <div class="stat"><div class="stat-val" id="s-up">—</div><div class="stat-lbl">总上行</div></div>
    <div class="stat"><div class="stat-val" id="s-dn">—</div><div class="stat-lbl">总下行</div></div>
    <div class="stat"><div class="stat-val red" id="s-blocked">0</div><div class="stat-lbl">封锁 IP</div></div>
  </div>

  <!-- 下栏：内容面板 -->
  <div class="panels">

    <div class="panel on" id="panel-conns">
      <table><thead id="th-conns"></thead><tbody id="conns"></tbody></table>
    </div>

    <div class="panel" id="panel-recent">
      <table><thead id="th-recent"></thead><tbody id="recent"></tbody></table>
    </div>

    <div class="panel" id="panel-domains">
      <table><thead id="th-domains"></thead><tbody id="domains"></tbody></table>
    </div>

    <div class="panel" id="panel-blocked">
      <table><thead id="th-blocked"></thead><tbody id="blocked"></tbody></table>
    </div>

  </div>
</div>
</div>

<script>
const token = new URLSearchParams(location.search).get('token') || '';
const q = s => token ? s + (s.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(token) : s;

let cur = 'conns';
function show(id) {
  cur = id;
  document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('on'));
  document.querySelectorAll('.panel').forEach(el => el.classList.remove('on'));
  document.getElementById('nav-' + id).classList.add('on');
  document.getElementById('panel-' + id).classList.add('on');
}

function fmtB(b) {
  if (!b) return '0 B';
  if (b < 1024) return b + ' B';
  if (b < 1048576) return (b/1024).toFixed(1) + ' KB';
  if (b < 1073741824) return (b/1048576).toFixed(1) + ' MB';
  return (b/1073741824).toFixed(2) + ' GB';
}
function fmtD(s) {
  s = Math.floor(s);
  if (s < 60) return s + 's';
  if (s < 3600) return Math.floor(s/60) + 'm' + (s%60) + 's';
  return Math.floor(s/3600) + 'h' + Math.floor((s%3600)/60) + 'm';
}
function fmtT(ts) { return new Date(ts * 1000).toLocaleTimeString(); }
function esc(s) { return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

// ── 排序 ──────────────────────────────────────────────────────────────────────
const sort = {
  conns:   { col:'id',        asc:true  },
  recent:  { col:'closed_at', asc:false },
  domains: { col:'conns',     asc:false },
};
function sortBy(tbl, col) {
  const s = sort[tbl];
  s.asc = (s.col === col) ? !s.asc : true;
  s.col = col;
  load();
}
function srt(arr, tbl) {
  const { col, asc } = sort[tbl];
  return [...arr].sort((a, b) => {
    const av = a[col] ?? '', bv = b[col] ?? '';
    const cmp = typeof av === 'number' ? av - bv : String(av).localeCompare(String(bv));
    return asc ? cmp : -cmp;
  });
}
function sth(tbl, col, lbl) {
  const s = sort[tbl], on = s.col === col;
  const arw = on ? `<span class="arr">${s.asc ? '▲' : '▼'}</span>` : '';
  return `<th class="sh${on?' sa':''}" onclick="sortBy('${tbl}','${col}')">${lbl} ${arw}</th>`;
}
function durCls(s) { return s > 300 ? 'cg' : s > 30 ? 'cy' : ''; }
function bCls(b)   { return b > 10485760 ? 'co' : b > 1048576 ? 'cg' : ''; }

async function api(url) { await fetch(q(url), {method:'POST'}); load(); }
function kill(id)    { api('/api/kill?id='    + id); }
function block(ip)   { api('/api/block?ip='   + encodeURIComponent(ip)); }
function unblock(ip) { api('/api/unblock?ip=' + encodeURIComponent(ip)); }

async function load() {
  let d;
  try {
    const r = await fetch(q('/api/stats'));
    if (!r.ok) { document.getElementById('ts').textContent = '认证失败'; return; }
    d = await r.json();
  } catch(e) { document.getElementById('ts').textContent = '连接失败'; return; }

  document.getElementById('ts').textContent = new Date().toLocaleTimeString();

  document.getElementById('s-active').textContent  = d.active_count;
  document.getElementById('s-total').textContent   = d.total_conns;
  document.getElementById('s-up').textContent      = fmtB(d.total_bytes_up);
  document.getElementById('s-dn').textContent      = fmtB(d.total_bytes_dn);
  document.getElementById('s-blocked').textContent = d.blocked.length;
  document.getElementById('b-active').textContent  = d.active_count;
  document.getElementById('b-recent').textContent  = (d.recent||[]).length;
  document.getElementById('b-blocked').textContent = d.blocked.length;

  // 活跃连接
  document.getElementById('th-conns').innerHTML =
    `<tr>${sth('conns','id','ID')}${sth('conns','client_ip','客户端 IP')}${sth('conns','target','目标')}` +
    `${sth('conns','duration','时长')}${sth('conns','bytes_up','上行')}${sth('conns','bytes_down','下行')}<th>操作</th></tr>`;
  const ac = srt(d.connections, 'conns');
  document.getElementById('conns').innerHTML = ac.length
    ? ac.map(c =>
        `<tr><td>${c.id}</td><td>${esc(c.client_ip)}</td><td>${esc(c.target)}</td>` +
        `<td class="${durCls(c.duration)}">${fmtD(c.duration)}</td>` +
        `<td class="${bCls(c.bytes_up)}">${fmtB(c.bytes_up)}</td>` +
        `<td class="${bCls(c.bytes_down)}">${fmtB(c.bytes_down)}</td>` +
        `<td><button class="btn btn-gray" onclick="kill(${c.id})">断开</button> ` +
        `<button class="btn btn-red" onclick="block(${JSON.stringify(c.client_ip)})">封锁</button></td></tr>`
      ).join('')
    : `<tr><td colspan="7" class="empty">暂无活跃连接</td></tr>`;

  // 连接记录
  document.getElementById('th-recent').innerHTML =
    `<tr>${sth('recent','id','ID')}${sth('recent','client_ip','客户端 IP')}${sth('recent','target','目标')}` +
    `${sth('recent','duration','时长')}${sth('recent','bytes_up','上行')}${sth('recent','bytes_down','下行')}` +
    `${sth('recent','closed_at','关闭时间')}<th>操作</th></tr>`;
  const rc = srt(d.recent||[], 'recent');
  document.getElementById('recent').innerHTML = rc.length
    ? rc.map(c =>
        `<tr class="dim"><td>${c.id}</td><td>${esc(c.client_ip)}</td><td>${esc(c.target)}</td>` +
        `<td class="${durCls(c.duration)}">${fmtD(c.duration)}</td>` +
        `<td class="${bCls(c.bytes_up)}">${fmtB(c.bytes_up)}</td>` +
        `<td class="${bCls(c.bytes_down)}">${fmtB(c.bytes_down)}</td>` +
        `<td>${fmtT(c.closed_at)}</td>` +
        `<td><button class="btn btn-red" onclick="block(${JSON.stringify(c.client_ip)})">封锁</button></td></tr>`
      ).join('')
    : `<tr><td colspan="8" class="empty">暂无记录</td></tr>`;

  // 域名分布
  document.getElementById('th-domains').innerHTML =
    `<tr>${sth('domains','domain','域名 / 目标')}${sth('domains','conns','连接数')}${sth('domains','bytes','总流量')}</tr>`;
  const dm = srt(d.top_domains||[], 'domains').slice(0,30);
  document.getElementById('domains').innerHTML = dm.length
    ? dm.map(x =>
        `<tr><td>${esc(x.domain)}</td><td>${x.conns}</td>` +
        `<td class="${bCls(x.bytes)}">${fmtB(x.bytes)}</td></tr>`
      ).join('')
    : `<tr><td colspan="3" class="empty">暂无数据</td></tr>`;

  // 黑名单（无排序，静态头）
  document.getElementById('th-blocked').innerHTML = '<tr><th>IP 地址</th><th>操作</th></tr>';
  document.getElementById('blocked').innerHTML = d.blocked.length
    ? d.blocked.map(ip =>
        `<tr><td>${esc(ip)}</td>` +
        `<td><button class="btn btn-grn" onclick="unblock(${JSON.stringify(ip)})">解除</button></td></tr>`
      ).join('')
    : `<tr><td colspan="2" class="empty">无封锁 IP</td></tr>`;
}

load();
setInterval(load, 1000);
</script>
</body>
</html>"""

_HTML_BYTES = _HTML.encode()


# ── HTTP server ────────────────────────────────────────────────────────────────

async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                  store: StatsStore, token: str) -> None:
    try:
        raw = await asyncio.wait_for(reader.read(8192), timeout=5.0)
    except Exception:
        writer.close()
        return

    try:
        first_line = raw.split(b"\r\n", 1)[0].decode()
        parts = first_line.split(" ")
        method, raw_path = parts[0], parts[1]
    except Exception:
        writer.close()
        return

    parsed = urlparse(raw_path)
    path   = parsed.path
    params = parse_qs(parsed.query)

    def p(key: str) -> str:
        vals = params.get(key, [])
        return vals[0] if vals else ""

    if token and p("token") != token:
        _respond(writer, 401, b"application/json", b'{"error":"unauthorized"}')
        return

    if path in ("/", ""):
        _respond(writer, 200, b"text/html; charset=utf-8", _HTML_BYTES)

    elif path == "/api/stats":
        _respond(writer, 200, b"application/json",
                 json.dumps(store.snapshot()).encode())

    elif path == "/api/block" and method == "POST":
        ip = p("ip")
        if ip:
            store.block(ip)
        _respond(writer, 200, b"application/json", b'{"ok":true}')

    elif path == "/api/unblock" and method == "POST":
        ip = p("ip")
        if ip:
            store.unblock(ip)
        _respond(writer, 200, b"application/json", b'{"ok":true}')

    elif path == "/api/kill" and method == "POST":
        try:
            conn_id = int(p("id"))
            ok = store.kill(conn_id)
        except (ValueError, TypeError):
            ok = False
        _respond(writer, 200, b"application/json",
                 b'{"ok":true}' if ok else b'{"ok":false}')

    else:
        _respond(writer, 404, b"text/plain", b"Not Found")


def _respond(writer: asyncio.StreamWriter, status: int,
             content_type: bytes, body: bytes) -> None:
    phrases = {200: b"OK", 401: b"Unauthorized", 404: b"Not Found"}
    phrase  = phrases.get(status, b"Error")
    header  = (
        b"HTTP/1.1 " + str(status).encode() + b" " + phrase + b"\r\n"
        b"Content-Type: " + content_type + b"\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"Connection: close\r\n"
        b"\r\n"
    )
    writer.write(header + body)
    writer.close()


async def start_admin(store: StatsStore, host: str, port: int,
                      token: str) -> asyncio.Server:
    server = await asyncio.start_server(
        lambda r, w: _handle(r, w, store, token),
        host, port,
    )
    return server
