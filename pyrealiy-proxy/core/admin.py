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
body{font-family:'Courier New',monospace;background:#0f111a;color:#c9d1d9;padding:20px;font-size:13px}
h1{color:#58a6ff;border-bottom:1px solid #21262d;padding-bottom:10px;margin-bottom:16px;font-size:18px}
h2{color:#79c0ff;margin:28px 0 8px;font-size:14px;letter-spacing:.5px}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:4px}
.card{background:#161b22;border:1px solid #30363d;border-radius:6px;padding:12px 20px;min-width:130px}
.card-val{font-size:26px;color:#58a6ff;font-weight:bold}
.card-lbl{font-size:11px;color:#8b949e;margin-top:2px}
table{width:100%;border-collapse:collapse}
th{background:#161b22;color:#8b949e;padding:7px 10px;text-align:left;border-bottom:1px solid #30363d;font-weight:normal;font-size:11px;letter-spacing:.5px;text-transform:uppercase}
td{padding:6px 10px;border-bottom:1px solid #21262d;color:#c9d1d9}
tr:hover td{background:#161b22}
.btn{border:none;padding:3px 10px;border-radius:4px;cursor:pointer;font-size:11px;font-family:inherit}
.btn-red{background:#da3633;color:#fff}
.btn-red:hover{background:#f85149}
.btn-grn{background:#238636;color:#fff}
.btn-grn:hover{background:#2ea043}
.btn-gray{background:#30363d;color:#c9d1d9}
.btn-gray:hover{background:#3d444e}
.empty{color:#484f58;font-style:italic;padding:10px}
#ts{color:#484f58;font-size:11px;float:right;margin-top:4px}
</style>
</head>
<body>
<h1>PyReality 监控面板 <span id="ts"></span></h1>
<div class="cards" id="cards"></div>

<h2>活跃连接</h2>
<table><thead><tr>
  <th>ID</th><th>客户端 IP</th><th>目标</th>
  <th>时长</th><th>上行</th><th>下行</th><th>操作</th>
</tr></thead><tbody id="conns"></tbody></table>

<h2>域名分布 Top 30</h2>
<table><thead><tr>
  <th>域名 / 目标</th><th>连接数</th><th>总流量</th>
</tr></thead><tbody id="domains"></tbody></table>

<h2>封锁 IP</h2>
<table><thead><tr>
  <th>IP 地址</th><th>操作</th>
</tr></thead><tbody id="blocked"></tbody></table>

<script>
const token = new URLSearchParams(location.search).get('token') || '';
const q = s => token ? s + (s.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(token) : s;

function fmtB(b) {
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
async function post(url) {
  await fetch(q(url), {method:'POST'});
  load();
}
function kill(id) { post('/api/kill?id=' + id); }
function block(ip) { post('/api/block?ip=' + encodeURIComponent(ip)); }
function unblock(ip) { post('/api/unblock?ip=' + encodeURIComponent(ip)); }

async function load() {
  let d;
  try {
    const r = await fetch(q('/api/stats'));
    if (!r.ok) { document.getElementById('ts').textContent = '认证失败'; return; }
    d = await r.json();
  } catch(e) { document.getElementById('ts').textContent = '连接失败'; return; }

  document.getElementById('ts').textContent = '已更新 ' + new Date().toLocaleTimeString();
  document.getElementById('cards').innerHTML =
    card(d.active_count, '活跃连接') +
    card(d.total_conns,  '累计连接') +
    card(d.blocked.length, '封锁 IP');

  const cb = document.getElementById('conns');
  cb.innerHTML = d.connections.length ? d.connections.map(c =>
    `<tr><td>${c.id}</td><td>${esc(c.client_ip)}</td><td>${esc(c.target)}</td>` +
    `<td>${fmtD(c.duration)}</td><td>${fmtB(c.bytes_up)}</td><td>${fmtB(c.bytes_down)}</td>` +
    `<td><button class="btn btn-gray" onclick="kill(${c.id})">断开</button> ` +
    `<button class="btn btn-red" onclick="block(${JSON.stringify(c.client_ip)})">封锁</button></td></tr>`
  ).join('') : `<tr><td colspan="7" class="empty">暂无活跃连接</td></tr>`;

  const db = document.getElementById('domains');
  const top = d.top_domains.slice(0,30);
  db.innerHTML = top.length ? top.map(x =>
    `<tr><td>${esc(x.domain)}</td><td>${x.conns}</td><td>${fmtB(x.bytes)}</td></tr>`
  ).join('') : `<tr><td colspan="3" class="empty">暂无数据</td></tr>`;

  const bb = document.getElementById('blocked');
  bb.innerHTML = d.blocked.length ? d.blocked.map(ip =>
    `<tr><td>${esc(ip)}</td><td>` +
    `<button class="btn btn-grn" onclick="unblock(${JSON.stringify(ip)})">解除</button></td></tr>`
  ).join('') : `<tr><td colspan="2" class="empty">无封锁 IP</td></tr>`;
}

function card(v, l) {
  return `<div class="card"><div class="card-val">${v}</div><div class="card-lbl">${l}</div></div>`;
}
function esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
}

load();
setInterval(load, 3000);
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
