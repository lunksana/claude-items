"""Embedded HTTP admin server — zero extra dependencies."""

from __future__ import annotations

import asyncio
import json
from urllib.parse import urlparse, parse_qs

from .stats import StatsStore
from .utils import safe_close

# ── embedded dashboard ─────────────────────────────────────────────────────────

_HTML = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>PyReality · NOC</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
html,body{height:100%;overflow:hidden;font-family:'Courier New',monospace;font-size:13px}
:root{
  --bg:#030712;--bg2:#060d1a;--card:rgba(8,18,38,.85);
  --bdr:rgba(0,180,255,.12);--bdr2:rgba(0,180,255,.28);
  --neon:#00ccff;--neon2:#0066ff;--np:#9944ee;
  --grn:#00ff88;--amb:#ffaa00;--red:#ff2244;
  --tx:#a8c8e8;--tx2:#4a6a8a;--tx3:#1e3050
}
body{
  background:var(--bg);color:var(--tx);
  background-image:
    linear-gradient(rgba(0,200,255,.022) 1px,transparent 1px),
    linear-gradient(90deg,rgba(0,200,255,.022) 1px,transparent 1px);
  background-size:44px 44px
}
.layout{display:flex;height:100vh;overflow:hidden}

/* ── sidebar ── */
.sidebar{
  width:188px;flex-shrink:0;
  background:linear-gradient(180deg,var(--bg2),var(--bg));
  border-right:1px solid var(--bdr);display:flex;flex-direction:column
}
.brand{padding:20px 16px 14px;border-bottom:1px solid var(--bdr)}
.brand-name{
  font-size:17px;font-weight:bold;letter-spacing:2px;color:var(--neon);
  text-shadow:0 0 20px rgba(0,204,255,.6)
}
.brand-sub{font-size:9px;color:var(--tx2);letter-spacing:3px;margin-top:3px;text-transform:uppercase}
.dot{
  display:inline-block;width:7px;height:7px;border-radius:50%;
  background:var(--grn);box-shadow:0 0 8px var(--grn);
  animation:pulse 2s ease-in-out infinite;margin-right:5px;vertical-align:middle
}
@keyframes pulse{
  0%,100%{opacity:1;box-shadow:0 0 8px var(--grn),0 0 14px rgba(0,255,136,.35)}
  50%{opacity:.3;box-shadow:0 0 3px var(--grn)}
}
.nav-menu{flex:1;padding:6px 0;overflow-y:auto}
.nav-item{
  display:flex;align-items:center;justify-content:space-between;
  padding:9px 16px;cursor:pointer;color:var(--tx2);font-size:12px;
  border-left:2px solid transparent;transition:all .15s;letter-spacing:.3px
}
.nav-item:hover{color:var(--tx);background:rgba(0,204,255,.04);border-left-color:rgba(0,204,255,.2)}
.nav-item.on{
  color:var(--neon);border-left-color:var(--neon);
  background:rgba(0,204,255,.07);text-shadow:0 0 8px rgba(0,204,255,.35)
}
.badge{
  background:rgba(0,204,255,.1);color:var(--neon);
  border:1px solid rgba(0,204,255,.2);border-radius:10px;
  padding:1px 7px;font-size:10px;line-height:1.5
}
.badge.r{background:rgba(255,34,68,.12);color:var(--red);border-color:rgba(255,34,68,.25)}
.sidebar-ft{padding:10px 14px;color:var(--tx3);font-size:10px;border-top:1px solid var(--bdr)}
.sidebar-ts{color:var(--tx2);font-size:10px;margin-top:3px}

/* ── main ── */
.main{flex:1;display:flex;flex-direction:column;overflow:hidden;min-width:0}
.topbar{
  display:flex;align-items:center;gap:14px;padding:8px 20px;flex-shrink:0;
  background:rgba(6,13,26,.92);border-bottom:1px solid var(--bdr);
  backdrop-filter:blur(20px)
}
.tb-title{font-size:10px;color:var(--tx2);letter-spacing:1.5px;text-transform:uppercase}
.tb-online{display:flex;align-items:center;gap:5px;font-size:10px;color:var(--grn);text-shadow:0 0 6px rgba(0,255,136,.4)}
.tb-sep{flex:1}
.tb-m{text-align:right}
.tb-m .v{font-size:13px;font-weight:bold}
.tb-m .l{font-size:9px;color:var(--tx2);letter-spacing:.4px}

/* ── panel system ── */
.panel{display:none;flex:1;min-height:0;flex-direction:column;overflow:hidden}
.panel.on{display:flex}

/* ── overview ── */
.ov-wrap{flex:1;overflow-y:auto;padding:14px 18px;display:flex;flex-direction:column;gap:12px}
.ov-wrap::-webkit-scrollbar{width:3px}
.ov-wrap::-webkit-scrollbar-thumb{background:var(--bdr2);border-radius:2px}

/* ── stat cards ── */
.stat-row{display:grid;grid-template-columns:repeat(5,1fr);gap:10px;flex-shrink:0}
.sc{
  background:var(--card);border:1px solid var(--bdr);border-radius:7px;
  padding:13px 14px;backdrop-filter:blur(20px);
  transition:border-color .25s,box-shadow .25s;position:relative;overflow:hidden
}
.sc::after{
  content:'';position:absolute;top:0;left:0;right:0;height:1px;
  background:linear-gradient(90deg,transparent,var(--neon),transparent);opacity:.35
}
.sc:hover{border-color:var(--bdr2);box-shadow:0 0 18px rgba(0,204,255,.09)}
.sv{font-size:22px;font-weight:bold;line-height:1;margin-bottom:5px}
.sv.b{color:var(--neon);text-shadow:0 0 10px rgba(0,204,255,.35)}
.sv.g{color:var(--grn);text-shadow:0 0 10px rgba(0,255,136,.35)}
.sv.a{color:var(--amb);text-shadow:0 0 10px rgba(255,170,0,.35)}
.sv.r{color:var(--red);text-shadow:0 0 10px rgba(255,34,68,.35)}
.sl{font-size:9px;color:var(--tx2);letter-spacing:.5px;text-transform:uppercase}

/* ── chart ── */
.chart-wrap{
  background:var(--card);border:1px solid var(--bdr);border-radius:7px;
  padding:12px 14px;flex-shrink:0;backdrop-filter:blur(20px)
}
.sec-head{display:flex;align-items:center;justify-content:space-between;margin-bottom:8px}
.sec-title{font-size:10px;color:var(--neon);letter-spacing:1.5px;text-transform:uppercase;font-weight:bold}
.legend{display:flex;gap:12px}
.leg{display:flex;align-items:center;gap:5px;font-size:10px;color:var(--tx2)}
.leg-line{width:14px;height:2px;border-radius:1px}
canvas{display:block;width:100%;height:80px}

/* ── glass card ── */
.glass{
  background:var(--card);border:1px solid var(--bdr);border-radius:7px;
  backdrop-filter:blur(20px);display:flex;flex-direction:column;overflow:hidden;
  transition:border-color .2s
}
.glass:hover{border-color:rgba(0,180,255,.2)}
.g-head{
  display:flex;align-items:center;justify-content:space-between;
  padding:9px 12px;border-bottom:1px solid var(--bdr);flex-shrink:0
}
.g-body{flex:1;overflow-y:auto;padding:8px}
.g-body::-webkit-scrollbar{width:3px}
.g-body::-webkit-scrollbar-thumb{background:var(--bdr2);border-radius:2px}
.ov-grid{display:grid;grid-template-columns:3fr 2fr;gap:12px;flex:1;min-height:220px}

/* ── connection cards ── */
.cc{
  background:rgba(0,204,255,.025);border:1px solid var(--bdr);border-radius:6px;
  padding:9px 11px;margin-bottom:5px;transition:all .18s;position:relative;overflow:hidden
}
.cc::before{
  content:'';position:absolute;left:0;top:0;bottom:0;width:2px;
  background:linear-gradient(180deg,var(--neon),var(--neon2))
}
.cc:hover{border-color:rgba(0,204,255,.24);box-shadow:0 0 12px rgba(0,204,255,.07);background:rgba(0,204,255,.045)}
.cr1{display:flex;align-items:center;gap:7px;margin-bottom:5px}
.ctarget{color:var(--tx);font-size:12px;flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.cdur{font-size:10px;padding:1px 5px;border-radius:3px;flex-shrink:0}
.cdur.n{color:var(--tx2)}.cdur.a{color:var(--amb)}.cdur.g{color:var(--grn)}
.cr2{display:flex;align-items:center;gap:10px;font-size:10px;color:var(--tx2)}
.cbytes{margin-left:auto;display:flex;gap:8px}
.cup{color:var(--neon)}.cdn{color:#aa66ff}
.cact{display:flex;gap:4px;margin-top:6px}

/* ── proto badges ── */
.pb{font-size:9px;padding:1px 6px;border-radius:3px;font-weight:bold;letter-spacing:.3px;flex-shrink:0}
.pb-tls {background:rgba(0,204,255,.13);color:var(--neon);border:1px solid rgba(0,204,255,.28)}
.pb-http{background:rgba(0,255,136,.1); color:var(--grn); border:1px solid rgba(0,255,136,.22)}
.pb-dns {background:rgba(136,51,255,.13);color:#aa88ff;  border:1px solid rgba(136,51,255,.28)}
.pb-tcp {background:rgba(255,170,0,.1); color:var(--amb); border:1px solid rgba(255,170,0,.22)}

/* ── domain bars ── */
.di{padding:7px 10px;border-bottom:1px solid rgba(0,180,255,.06)}
.di:last-child{border-bottom:none}
.di-row{display:flex;align-items:center;gap:8px}
.di-name{flex:1;color:var(--tx);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.di-cnt{color:var(--tx2);font-size:10px;min-width:28px;text-align:right}
.di-bytes{color:var(--neon);font-size:10px;min-width:56px;text-align:right}
.di-bw{height:2px;background:rgba(0,204,255,.07);border-radius:1px;overflow:hidden;margin-top:4px}
.di-bar{height:100%;border-radius:1px;background:linear-gradient(90deg,var(--neon2),var(--neon));transition:width .5s ease}

/* ── blocked list ── */
.al{display:flex;align-items:center;gap:10px;padding:9px 12px;border-bottom:1px solid rgba(0,180,255,.06)}
.al:last-child{border-bottom:none}
.al-dot{
  width:6px;height:6px;border-radius:50%;background:var(--red);box-shadow:0 0 6px var(--red);
  flex-shrink:0;animation:pulse 2s ease-in-out infinite
}
.al-ip{color:var(--red);flex:1}

/* ── table ── */
table{width:100%;border-collapse:collapse;font-size:12px}
th{
  background:rgba(0,204,255,.04);color:var(--tx2);padding:7px 10px;text-align:left;
  border-bottom:1px solid var(--bdr);font-weight:normal;
  font-size:10px;letter-spacing:.7px;text-transform:uppercase;
  position:sticky;top:0;backdrop-filter:blur(10px)
}
.sh{cursor:pointer}.sh:hover{color:var(--tx)}.sa{color:var(--neon)}
.arr{font-size:8px;opacity:.7}
td{padding:6px 10px;border-bottom:1px solid rgba(0,204,255,.035);white-space:nowrap;color:var(--tx)}
tr:hover td{background:rgba(0,204,255,.035)}
.empty{color:var(--tx3);font-style:italic;text-align:center;padding:20px;display:block}

/* ── buttons ── */
.btn{
  border:none;padding:3px 9px;border-radius:4px;cursor:pointer;
  font-size:10px;font-family:inherit;line-height:1.6;transition:all .18s
}
.bk{background:rgba(255,170,0,.1);color:var(--amb);border:1px solid rgba(255,170,0,.22)}
.bk:hover{background:rgba(255,170,0,.22);box-shadow:0 0 7px rgba(255,170,0,.18)}
.bb{background:rgba(255,34,68,.1);color:var(--red);border:1px solid rgba(255,34,68,.22)}
.bb:hover{background:rgba(255,34,68,.22);box-shadow:0 0 7px rgba(255,34,68,.18)}
.bu{background:rgba(0,255,136,.08);color:var(--grn);border:1px solid rgba(0,255,136,.2)}
.bu:hover{background:rgba(0,255,136,.18);box-shadow:0 0 7px rgba(0,255,136,.18)}

/* ── color helpers ── */
.cg{color:var(--grn)}.cy{color:var(--amb)}.co{color:#ff8800}
::-webkit-scrollbar{width:4px;height:4px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--bdr2);border-radius:2px}
</style>
</head>
<body>
<div class="layout">

<nav class="sidebar">
  <div class="brand">
    <div class="brand-name"><span class="dot"></span>PyReality</div>
    <div class="brand-sub">NOC · Console</div>
  </div>
  <div class="nav-menu">
    <div class="nav-item on" onclick="show('overview')" id="nav-overview"><span>◈ 概览</span></div>
    <div class="nav-item" onclick="show('conns')"   id="nav-conns"  ><span>◎ 活跃连接</span><span class="badge" id="b-active">0</span></div>
    <div class="nav-item" onclick="show('recent')"  id="nav-recent" ><span>◷ 连接记录</span><span class="badge" id="b-recent">0</span></div>
    <div class="nav-item" onclick="show('domains')" id="nav-domains"><span>◇ 域名分布</span></div>
    <div class="nav-item" onclick="show('blocked')" id="nav-blocked"><span>⊘ 封锁名单</span><span class="badge r" id="b-blocked">0</span></div>
  </div>
  <div class="sidebar-ft">
    <div>PyReality Proxy</div>
    <div class="sidebar-ts" id="ts">—</div>
  </div>
</nav>

<div class="main">
  <div class="topbar">
    <div class="tb-title">Network Operations Center</div>
    <div class="tb-online"><span class="dot"></span>ONLINE</div>
    <div class="tb-sep"></div>
    <div class="tb-m"><div class="v" id="t-up" style="color:var(--neon)">—</div><div class="l">上行总量</div></div>
    <div class="tb-m"><div class="v" id="t-dn" style="color:#aa66ff">—</div><div class="l">下行总量</div></div>
    <div class="tb-m"><div class="v" id="t-conns" style="color:var(--grn)">0</div><div class="l">活跃连接</div></div>
  </div>

  <!-- OVERVIEW -->
  <div class="panel on" id="panel-overview">
    <div class="ov-wrap">
      <div class="stat-row">
        <div class="sc"><div class="sv b" id="s-active">0</div><div class="sl">活跃连接</div></div>
        <div class="sc"><div class="sv b" id="s-total">0</div><div class="sl">累计连接</div></div>
        <div class="sc"><div class="sv g" id="s-up">—</div><div class="sl">总上行</div></div>
        <div class="sc"><div class="sv a" id="s-dn">—</div><div class="sl">总下行</div></div>
        <div class="sc"><div class="sv r" id="s-blocked">0</div><div class="sl">封锁 IP</div></div>
      </div>
      <div class="chart-wrap">
        <div class="sec-head">
          <div class="sec-title">实时流量监控</div>
          <div class="legend">
            <div class="leg"><div class="leg-line" style="background:var(--neon)"></div>上行</div>
            <div class="leg"><div class="leg-line" style="background:#aa66ff"></div>下行</div>
          </div>
        </div>
        <canvas id="chart" height="80"></canvas>
      </div>
      <div class="ov-grid">
        <div class="glass">
          <div class="g-head">
            <div class="sec-title">活跃连接</div>
            <div style="font-size:10px;color:var(--tx2)">最近 5 条</div>
          </div>
          <div class="g-body" id="ov-conns"></div>
        </div>
        <div class="glass">
          <div class="g-head">
            <div class="sec-title">访问排行</div>
            <div style="font-size:10px;color:var(--tx2)">TOP 10</div>
          </div>
          <div class="g-body" style="padding:0" id="ov-domains"></div>
        </div>
      </div>
    </div>
  </div>

  <!-- ACTIVE CONNS -->
  <div class="panel" id="panel-conns">
    <div style="padding:12px 18px 8px;flex-shrink:0;border-bottom:1px solid var(--bdr)">
      <div class="sec-title">活跃连接</div>
    </div>
    <div style="flex:1;min-height:0;overflow-y:auto;padding:10px 18px" id="conns-list"></div>
  </div>

  <!-- RECENT -->
  <div class="panel" id="panel-recent">
    <div style="flex:1;min-height:0;padding:14px 18px;display:flex;flex-direction:column">
      <div class="glass" style="flex:1;min-height:0">
        <div class="g-head"><div class="sec-title">连接记录</div></div>
        <div class="g-body" style="padding:0;overflow-x:auto">
          <table><thead id="th-recent"></thead><tbody id="recent"></tbody></table>
        </div>
      </div>
    </div>
  </div>

  <!-- DOMAINS -->
  <div class="panel" id="panel-domains">
    <div style="flex:1;min-height:0;padding:14px 18px;display:flex;flex-direction:column">
      <div class="glass" style="flex:1;min-height:0">
        <div class="g-head">
          <div class="sec-title">域名 / 目标分布</div>
          <div style="font-size:10px;color:var(--tx2)">TOP 30</div>
        </div>
        <div class="g-body" style="padding:0" id="domains"></div>
      </div>
    </div>
  </div>

  <!-- BLOCKED -->
  <div class="panel" id="panel-blocked">
    <div style="flex:1;min-height:0;padding:14px 18px;display:flex;flex-direction:column">
      <div class="glass" style="flex:1;min-height:0">
        <div class="g-head">
          <div class="sec-title">⊘ 封锁名单</div>
          <div style="font-size:10px;color:var(--red);text-shadow:0 0 6px rgba(255,34,68,.35)" id="b-blocked2">0 个 IP</div>
        </div>
        <div class="g-body" style="padding:0" id="blocked"></div>
      </div>
    </div>
  </div>

</div>
</div>
<script>
const token = new URLSearchParams(location.search).get('token') || '';
const q = s => token ? s + (s.includes('?') ? '&' : '?') + 'token=' + encodeURIComponent(token) : s;

const HLEN = 60;
let histUp = [], histDn = [], prevUp = 0, prevDn = 0;

const sort = {
  recent:  {col:'closed_at', asc:false},
  domains: {col:'bytes',     asc:false},
};

function show(id) {
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
function durCls(s) { return s > 300 ? 'cg' : s > 30 ? 'cy' : ''; }
function bCls(b)   { return b > 10485760 ? 'co' : b > 1048576 ? 'cg' : ''; }

function proto(target) {
  const m = String(target).match(/:(\d+)$/);
  const p = m ? +m[1] : 0;
  if (p === 443) return '<span class="pb pb-tls">TLS</span>';
  if (p === 80)  return '<span class="pb pb-http">HTTP</span>';
  if (p === 53)  return '<span class="pb pb-dns">DNS</span>';
  return '<span class="pb pb-tcp">TCP</span>';
}

function sortBy(tbl, col) {
  const s = sort[tbl];
  s.asc = s.col === col ? !s.asc : true;
  s.col = col;
  load();
}
function srt(arr, tbl) {
  const {col, asc} = sort[tbl];
  return [...arr].sort((a, b) => {
    const av = a[col] ?? '', bv = b[col] ?? '';
    const c = typeof av === 'number' ? av - bv : String(av).localeCompare(String(bv));
    return asc ? c : -c;
  });
}
function sth(tbl, col, lbl) {
  const s = sort[tbl], on = s.col === col;
  const arw = on ? `<span class="arr">${s.asc ? '▲' : '▼'}</span>` : '';
  return `<th class="sh${on?' sa':''}" onclick="sortBy('${tbl}','${col}')">${lbl} ${arw}</th>`;
}

async function api(url) { await fetch(q(url), {method:'POST'}); load(); }
function kill(id)    { api('/api/kill?id='    + id); }
function block(ip)   { api('/api/block?ip='   + encodeURIComponent(ip)); }
function unblock(ip) { api('/api/unblock?ip=' + encodeURIComponent(ip)); }

function drawChart(up, dn) {
  const canvas = document.getElementById('chart');
  const W = canvas.offsetWidth;
  if (!W) return;
  canvas.width = W;
  const H = canvas.height;
  const ctx = canvas.getContext('2d');
  ctx.clearRect(0, 0, W, H);

  ctx.strokeStyle = 'rgba(0,200,255,.05)';
  ctx.lineWidth = 1;
  for (let i = 1; i < 4; i++) {
    const y = Math.round(H * i / 4) + .5;
    ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
  }
  for (let i = 1; i < 7; i++) {
    const x = Math.round(W * i / 7) + .5;
    ctx.beginPath(); ctx.moveTo(x, 0); ctx.lineTo(x, H); ctx.stroke();
  }

  const mx = Math.max(...up, ...dn, 1);

  function line(data, color) {
    const pts = data.map((v, i) => [i / (HLEN - 1) * W, H - (v / mx) * (H - 6) - 3]);
    const g = ctx.createLinearGradient(0, 0, 0, H);
    g.addColorStop(0, color + '44');
    g.addColorStop(1, color + '00');
    ctx.beginPath();
    ctx.moveTo(pts[0][0], H);
    pts.forEach(([x, y]) => ctx.lineTo(x, y));
    ctx.lineTo(pts[pts.length - 1][0], H);
    ctx.closePath();
    ctx.fillStyle = g;
    ctx.fill();
    ctx.beginPath();
    pts.forEach(([x, y], i) => i ? ctx.lineTo(x, y) : ctx.moveTo(x, y));
    ctx.strokeStyle = color;
    ctx.lineWidth = 1.5;
    ctx.shadowColor = color;
    ctx.shadowBlur = 5;
    ctx.stroke();
    ctx.shadowBlur = 0;
  }

  line(up, '#00ccff');
  line(dn, '#9944ee');
}

function connCard(c, actions) {
  const dc = c.duration > 300 ? 'g' : c.duration > 30 ? 'a' : 'n';
  const act = actions ? `<div class="cact">
    <button class="btn bk" onclick="kill(${c.id})">断开</button>
    <button class="btn bb" onclick="block(${JSON.stringify(c.client_ip)})">封锁</button>
  </div>` : '';
  return `<div class="cc">
    <div class="cr1">${proto(c.target)}<span class="ctarget">${esc(c.target)}</span><span class="cdur ${dc}">${fmtD(c.duration)}</span></div>
    <div class="cr2"><span>${esc(c.client_ip)}</span><span class="cbytes"><span class="cup">↑ ${fmtB(c.bytes_up)}</span><span class="cdn">↓ ${fmtB(c.bytes_down)}</span></span></div>
    ${act}
  </div>`;
}

function domainItem(x, maxB) {
  const pct = maxB > 0 ? Math.max(2, x.bytes / maxB * 100) : 0;
  return `<div class="di">
    <div class="di-row"><span class="di-name">${esc(x.domain)}</span><span class="di-cnt">${x.conns}</span><span class="di-bytes">${fmtB(x.bytes)}</span></div>
    <div class="di-bw"><div class="di-bar" style="width:${pct.toFixed(1)}%"></div></div>
  </div>`;
}

async function load() {
  let d;
  try {
    const r = await fetch(q('/api/stats'));
    if (!r.ok) { document.getElementById('ts').textContent = '认证失败'; return; }
    d = await r.json();
  } catch(e) { document.getElementById('ts').textContent = '连接中断'; return; }

  document.getElementById('ts').textContent = new Date().toLocaleTimeString();

  const du = Math.max(0, d.total_bytes_up - prevUp);
  const dd = Math.max(0, d.total_bytes_dn - prevDn);
  prevUp = d.total_bytes_up; prevDn = d.total_bytes_dn;
  histUp.push(du); if (histUp.length > HLEN) histUp.shift();
  histDn.push(dd); if (histDn.length > HLEN) histDn.shift();
  const padU = Array(HLEN - histUp.length).fill(0).concat(histUp);
  const padD = Array(HLEN - histDn.length).fill(0).concat(histDn);
  drawChart(padU, padD);

  document.getElementById('s-active').textContent  = d.active_count;
  document.getElementById('s-total').textContent   = d.total_conns;
  document.getElementById('s-up').textContent      = fmtB(d.total_bytes_up);
  document.getElementById('s-dn').textContent      = fmtB(d.total_bytes_dn);
  document.getElementById('s-blocked').textContent = d.blocked.length;
  document.getElementById('t-up').textContent      = fmtB(d.total_bytes_up);
  document.getElementById('t-dn').textContent      = fmtB(d.total_bytes_dn);
  document.getElementById('t-conns').textContent   = d.active_count;
  document.getElementById('b-active').textContent  = d.active_count;
  document.getElementById('b-recent').textContent  = (d.recent||[]).length;
  document.getElementById('b-blocked').textContent = d.blocked.length;
  document.getElementById('b-blocked2').textContent= d.blocked.length + ' 个 IP';

  const top5 = (d.connections||[]).slice(0, 5);
  document.getElementById('ov-conns').innerHTML = top5.length
    ? top5.map(c => connCard(c, false)).join('')
    : '<span class="empty">暂无活跃连接</span>';

  const dm10 = srt(d.top_domains||[], 'domains').slice(0, 10);
  const mb10 = dm10.reduce((m, x) => Math.max(m, x.bytes), 0);
  document.getElementById('ov-domains').innerHTML = dm10.length
    ? dm10.map(x => domainItem(x, mb10)).join('')
    : '<span class="empty">暂无数据</span>';

  const conns = d.connections || [];
  document.getElementById('conns-list').innerHTML = conns.length
    ? conns.map(c => connCard(c, true)).join('')
    : '<span class="empty">暂无活跃连接</span>';

  document.getElementById('th-recent').innerHTML =
    `<tr>${sth('recent','id','ID')}${sth('recent','client_ip','客户端')}${sth('recent','target','目标')}` +
    `${sth('recent','duration','时长')}${sth('recent','bytes_up','上行')}${sth('recent','bytes_down','下行')}` +
    `${sth('recent','closed_at','关闭时间')}<th>操作</th></tr>`;
  const rc = srt(d.recent||[], 'recent');
  document.getElementById('recent').innerHTML = rc.length
    ? rc.map(c =>
        `<tr><td>${c.id}</td><td>${esc(c.client_ip)}</td><td>${esc(c.target)}</td>` +
        `<td class="${durCls(c.duration)}">${fmtD(c.duration)}</td>` +
        `<td class="${bCls(c.bytes_up)}">${fmtB(c.bytes_up)}</td>` +
        `<td class="${bCls(c.bytes_down)}">${fmtB(c.bytes_down)}</td>` +
        `<td>${fmtT(c.closed_at)}</td>` +
        `<td><button class="btn bb" onclick="block(${JSON.stringify(c.client_ip)})">封锁</button></td></tr>`
      ).join('')
    : '<tr><td colspan="8" class="empty">暂无记录</td></tr>';

  const allDm = srt(d.top_domains||[], 'domains').slice(0, 30);
  const mbAll = allDm.reduce((m, x) => Math.max(m, x.bytes), 0);
  document.getElementById('domains').innerHTML = allDm.length
    ? allDm.map(x => domainItem(x, mbAll)).join('')
    : '<span class="empty">暂无数据</span>';

  document.getElementById('blocked').innerHTML = d.blocked.length
    ? d.blocked.map(ip =>
        `<div class="al"><div class="al-dot"></div><div class="al-ip">${esc(ip)}</div>` +
        `<button class="btn bu" onclick="unblock(${JSON.stringify(ip)})">解除封锁</button></div>`
      ).join('')
    : '<span class="empty">无封锁 IP</span>';
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
        await safe_close(writer)
        return

    try:
        first_line = raw.split(b"\r\n", 1)[0].decode()
        parts = first_line.split(" ")
        method, raw_path = parts[0], parts[1]
    except Exception:
        await safe_close(writer)
        return

    parsed = urlparse(raw_path)
    path   = parsed.path
    params = parse_qs(parsed.query)

    def p(key: str) -> str:
        vals = params.get(key, [])
        return vals[0] if vals else ""

    if token and p("token") != token:
        await _respond(writer, 401, b"application/json", b'{"error":"unauthorized"}')
        return

    if path in ("/", ""):
        await _respond(writer, 200, b"text/html; charset=utf-8", _HTML_BYTES)

    elif path == "/api/stats":
        await _respond(writer, 200, b"application/json",
                       json.dumps(store.snapshot()).encode())

    elif path == "/api/block" and method == "POST":
        ip = p("ip")
        if ip:
            store.block(ip)
        await _respond(writer, 200, b"application/json", b'{"ok":true}')

    elif path == "/api/unblock" and method == "POST":
        ip = p("ip")
        if ip:
            store.unblock(ip)
        await _respond(writer, 200, b"application/json", b'{"ok":true}')

    elif path == "/api/kill" and method == "POST":
        try:
            conn_id = int(p("id"))
            ok = store.kill(conn_id)
        except (ValueError, TypeError):
            ok = False
        await _respond(writer, 200, b"application/json",
                       b'{"ok":true}' if ok else b'{"ok":false}')

    else:
        await _respond(writer, 404, b"text/plain", b"Not Found")


async def _respond(writer: asyncio.StreamWriter, status: int,
                   content_type: bytes, body: bytes) -> None:
    """
    发送 HTTP 响应：write → drain → safe_close。

    drain() 等写缓冲下到 low water mark 再返回，确保 24KB HTML 这类大响应
    在 close() 触发 transport 关闭前已被实际推到 OS socket buffer，
    避免边写边断带来的截断风险。
    safe_close 接着做 close + wait_closed，让 fd 立刻释放。
    """
    phrases = {200: b"OK", 401: b"Unauthorized", 404: b"Not Found"}
    phrase  = phrases.get(status, b"Error")
    header  = (
        b"HTTP/1.1 " + str(status).encode() + b" " + phrase + b"\r\n"
        b"Content-Type: " + content_type + b"\r\n"
        b"Content-Length: " + str(len(body)).encode() + b"\r\n"
        b"Connection: close\r\n"
        b"\r\n"
    )
    try:
        writer.write(header + body)
        await writer.drain()
    except Exception:
        pass
    await safe_close(writer)


async def start_admin(store: StatsStore, host: str, port: int,
                      token: str) -> asyncio.Server:
    server = await asyncio.start_server(
        lambda r, w: _handle(r, w, store, token),
        host, port,
    )
    return server
