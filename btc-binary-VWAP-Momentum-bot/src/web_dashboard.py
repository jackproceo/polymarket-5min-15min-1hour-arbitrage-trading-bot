"""
Local web dashboard: FastAPI + single-page UI, JSON at /api/state.
Runs in a daemon thread; state is updated from the bot's main loop.
Password protection: set DASHBOARD_PASSWORD in .env (default "okok").
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import math
import secrets
import socket
import threading
import time
from typing import Any, Dict, Optional

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse, Response, RedirectResponse
import uvicorn

logger = logging.getLogger("btc_live")

# ── Auth helpers ────────────────────────────────────────────────────────────
_AUTH_COOKIE = "btc_live_auth"

def _make_auth_token(password: str) -> str:
    """Generate a signed auth token: random_hex::hmac_hex"""
    nonce = secrets.token_hex(16)
    sig = hmac.new(password.encode(), nonce.encode(), hashlib.sha256).hexdigest()
    return f"{nonce}::{sig}"

def _verify_auth_token(token: str, password: str) -> bool:
    """Verify the HMAC signature of an auth token."""
    try:
        nonce, sig = token.rsplit("::", 1)
        expected = hmac.new(password.encode(), nonce.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig)
    except (ValueError, AttributeError):
        return False

_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>BTC · 登录</title>
  <style>
    :root {
      --bg: #0d1117; --panel: #161b22; --border: #30363d;
      --text: #e6edf3; --muted: #8b949e; --green: #3fb950; --red: #f85149;
      --blue: #58a6ff;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: ui-sans-serif, system-ui, sans-serif;
      background: var(--bg); color: var(--text);
      display: flex; align-items: center; justify-content: center;
      min-height: 100vh;
    }
    .box {
      background: var(--panel); border: 1px solid var(--border);
      border-radius: 12px; padding: 2rem; width: 100%; max-width: 360px;
    }
    h1 { font-size: 1.1rem; font-weight: 600; margin-bottom: 0.5rem; }
    p.sub { color: var(--muted); font-size: 0.85rem; margin-bottom: 1.25rem; }
    input {
      width: 100%; padding: 0.65rem 0.85rem;
      background: var(--bg); border: 1px solid var(--border); border-radius: 6px;
      color: var(--text); font-size: 0.95rem; outline: none;
    }
    input:focus { border-color: var(--blue); }
    button {
      width: 100%; margin-top: 0.75rem; padding: 0.65rem;
      background: var(--green); color: #000;
      border: none; border-radius: 6px; font-size: 0.95rem;
      font-weight: 600; cursor: pointer;
    }
    button:hover { opacity: 0.9; }
    .err { color: var(--red); font-size: 0.82rem; margin-top: 0.6rem; display: none; }
  </style>
</head>
<body>
  <div class="box">
    <h1>BTC 实时交易机器人</h1>
    <p class="sub">请输入密码访问控制面板</p>
    <form method="post" action="/login">
      <input type="password" name="password" placeholder="密码" autofocus required/>
      <button type="submit">登 录</button>
    </form>
    <div class="err" id="err">__ERROR__</div>
  </div>
  <script>
    (function() {
      var e = document.getElementById("err");
      if (e.textContent.indexOf("__ERROR__") === -1) {
        e.style.display = "none";
      } else {
        e.textContent = e.textContent.replace("__ERROR__", "密码错误，请重试");
        e.style.display = "block";
      }
    })();
  </script>
</body>
</html>
"""

_THREE_HOURS = 3 * 60 * 60  # cookie max-age in seconds

_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>BTC 实时机器人</title>
  <style>
    :root {
      --bg: #0d1117; --panel: #161b22; --border: #30363d;
      --text: #e6edf3; --muted: #8b949e; --green: #3fb950; --red: #f85149;
      --yellow: #d29922; --blue: #58a6ff; --violet: #a371f7;
    }
    * { box-sizing: border-box; }
    body { font-family: ui-sans-serif, system-ui, sans-serif; background: var(--bg); color: var(--text);
      margin: 0; padding: 1rem; line-height: 1.45; }
    h1 { font-size: 1.1rem; font-weight: 600; margin: 0 0 0.75rem; }
    .meta { color: var(--muted); font-size: 0.85rem; margin-bottom: 1rem; }
    .grid { display: grid; gap: 0.75rem; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); }
    .card { background: var(--panel); border: 1px solid var(--border); border-radius: 8px; padding: 0.85rem; }
    .card h2 { font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.04em; color: var(--muted);
      margin: 0 0 0.5rem; }
    .row { display: flex; justify-content: space-between; gap: 0.5rem; font-size: 0.9rem; }
    .sig { font-size: 1rem; font-weight: 600; }
    .sig.wait { color: var(--yellow); }
    .sig.buy { color: var(--green); }
    .sig.block { color: var(--red); }
    .mono { font-family: ui-monospace, monospace; font-size: 0.82rem; }
    .btc { border-color: #d29922; }
    footer { margin-top: 1rem; color: var(--muted); font-size: 0.75rem; }
  </style>
</head>
<body>
  <h1>BTC 涨跌 — 实时</h1>
  <div class="meta" id="meta">加载中…</div>
  <div class="grid">
    <div class="card"><h2>会话</h2><div id="session" class="mono"></div></div>
    <div class="card"><h2>策略</h2><div id="strategy"></div></div>
    <div class="card"><h2>UP</h2><div id="up" class="mono"></div></div>
    <div class="card"><h2>DOWN</h2><div id="down" class="mono"></div></div>
    <div class="card btc"><h2>BTC / USD (Chainlink)</h2><div id="btc" class="mono"></div></div>
    <div class="card"><h2>交易</h2><div id="trading" class="mono"></div></div>
  </div>
  <footer>每秒刷新 · <span id="err"></span></footer>
  <script>
    /* No optional chaining (?.) — must run in older browsers / Edge legacy. */
    function esc(s) {
      if (s === null || s === undefined) return "";
      var el = document.createElement("div");
      el.textContent = String(s);
      return el.innerHTML;
    }
    function sigClass(t) {
      if (!t) return "wait";
      if (t.indexOf("BUY") >= 0) return "buy";
      /* Do not use \\uD83D\\uDEAB here: Python treats \\u.... in the template as escapes and emits invalid UTF-8 surrogates. */
      if (t.indexOf("NO ENTRY") >= 0) return "block";
      return "wait";
    }
    function numFmt(n, dec) {
      if (n === null || n === undefined || typeof n !== "number" || isNaN(n)) return "\u2014";
      return n.toFixed(dec);
    }
    function tick() {
      var errEl = document.getElementById("err");
      var r = new XMLHttpRequest();
      r.open("GET", "/api/state", true);
      r.onreadystatechange = function () {
        if (r.readyState !== 4) return;
        try {
          if (r.status !== 200) throw new Error("HTTP " + r.status);
          var d = JSON.parse(r.responseText);
          errEl.textContent = "";
          var hdr = d.header || {};
          var slug = hdr.slug != null ? String(hdr.slug) : "\u2014";
          var ts = "";
          if (d.ts) ts = new Date(d.ts * 1000).toISOString();
          document.getElementById("meta").innerHTML = esc(slug) + " \u00b7 " + esc(ts);
          document.getElementById("session").innerHTML = [
            "计时器: " + (hdr.time_left_sec != null ? esc(Math.floor(hdr.time_left_sec) + "秒剩余") : "\u2014"),
            "WS: " + (hdr.ws_connected ? "已连接" : "已断开"),
            "模式: " + (hdr.simulation ? "模拟" : "实盘"),
          ].join("<br/>");
          var st = d.strategy || {};
          var sig = st.signal_text || "\u2014";
          function chk(x) { return x === true ? "\u2713" : x === false ? "\u2717" : "\u2014"; }
          var ck = st.checks || {};
          document.getElementById("strategy").innerHTML =
            '<div class="sig ' + sigClass(sig) + '">' + esc(sig) + "</div>" +
            '<div class="mono" style="margin-top:0.4rem">' +
            "偏好: " + esc(st.favorite) + " \u00b7 胜率: " + esc(st.win_rate_str) + "<br/>" +
            "检查: P=" + chk(ck.price) + " T=" + chk(ck.time) + " D=" + chk(ck.dev) +
            " M=" + chk(ck.mom) + " 截止=" + chk(ck.time_cutoff) +
            "</div>";
          function book(x, id) {
            var el = document.getElementById(id);
            if (!x) { el.textContent = "无数据"; return; }
            var bk = x.book || {};
            var ind = x.indicators || {};
            el.innerHTML = [
              "最新 " + esc(bk.last_price),
              "买价 " + esc(bk.best_bid) + " / 卖价 " + esc(bk.best_ask),
              "VWAP " + numFmt(ind.vwap, 4) +
                " \u00b7 偏差 " + (ind.deviation_pct != null ? numFmt(ind.deviation_pct, 2) + "%" : "\u2014"),
              "Z " + numFmt(ind.zscore, 2) +
                " \u00b7 动量 " + (ind.momentum_pct != null ? numFmt(ind.momentum_pct, 2) + "%" : "\u2014"),
              "成交量 " + (bk.volume_total != null ? esc(Math.round(bk.volume_total)) : "\u2014"),
            ].join("<br/>");
          }
          book(d.up, "up");
          book(d.down, "down");
          var b = d.btc || {};
          var btcEl = document.getElementById("btc");
          if (b.btc_current_price > 0) {
            btcEl.innerHTML = [
              "$" + esc(numFmt(b.btc_current_price, 2)),
              "锚定 $" + (b.btc_anchor_price > 0 ? esc(numFmt(b.btc_anchor_price, 2)) : "\u2014"),
              esc(b.deviation_line || ""),
              "数据源: " + (b.btc_connected ? "正常" : "离线") +
                (b.fresh_sec != null ? " \u00b7 " + Math.floor(b.fresh_sec) + "s" : ""),
            ].join("<br/>");
          } else {
            btcEl.textContent = "等待 Chainlink 数据…";
          }
          var tr = d.trading || {};
          var tHtml = "市场数 " + esc(tr.markets_seen) +
            " \u00b7 交易数 " + esc(tr.trade_count) +
            " \u00b7 胜 " + esc(tr.wins) + " / 负 " + esc(tr.losses) +
            " \u00b7 盈亏 $" + (tr.total_pnl != null ? numFmt(tr.total_pnl, 2) : "\u2014") + "<br/>";
          if (tr.account) {
            tHtml += "资金: $" + numFmt(tr.account.current_capital, 2) +
              " (初始 $" + numFmt(tr.account.initial_capital, 0) + ")" +
              " \u00b7 已实现 $" + (tr.account.realized_pnl != null ? numFmt(tr.account.realized_pnl, 2) : "0") + "<br/>";
          }
          if (tr.position) {
            var p = tr.position;
            tHtml += "做多 " + esc(p.token_name) + " @ " + esc(p.entry_price) +
              " \u00d7" + esc(p.contracts) + (p.hedged ? " 已对冲" : "") + "<br/>";
            tHtml += "未实现 $" + (p.unrealized_pnl != null ? numFmt(p.unrealized_pnl, 2) : "\u2014") + "<br/>";
          } else {
            tHtml += "无持仓<br/>";
          }
          if (tr.recent_trades && tr.recent_trades.length) {
            var lines = [];
            for (var i = 0; i < tr.recent_trades.length; i++) {
              lines.push(esc(tr.recent_trades[i].line));
            }
            tHtml += "<br/>最近:<br/>" + lines.join("<br/>");
          }
          document.getElementById("trading").innerHTML = tHtml;
        } catch (e) {
          errEl.textContent = "轮询错误: " + (e && e.message ? e.message : e);
        }
      };
      r.onerror = function () {
        errEl.textContent = "网络错误（机器人是否在运行？）";
      };
      r.send();
    }
    tick();
    setInterval(tick, 1000);
  </script>
</body>
</html>
"""

_HTML_TABBED = r"""<!DOCTYPE html>
<html lang="zh">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width, initial-scale=1"/>
  <title>BTC 实时机器人</title>
  <style>
    :root {--bg:#0d1117;--panel:#161b22;--border:#30363d;--text:#e6edf3;--muted:#8b949e;--green:#3fb950;--red:#f85149;--yellow:#d29922;--blue:#58a6ff;--violet:#a371f7;}
    *{box-sizing:border-box;margin:0;padding:0;}
    body{font-family:ui-sans-serif,system-ui,sans-serif;background:var(--bg);color:var(--text);padding:0.5rem 1rem 1rem;line-height:1.45;}
    h1{font-size:1.1rem;font-weight:600;margin:0 0 0.5rem;}
    .tabs{display:flex;gap:0;margin-bottom:0.75rem;border-bottom:2px solid var(--border);}
    .tab{padding:0.5rem 1rem;cursor:pointer;border:none;background:none;color:var(--muted);font-size:0.85rem;font-weight:500;border-bottom:2px solid transparent;margin-bottom:-2px;transition:.15s;}
    .tab:hover{color:var(--text);}
    .tab.active{color:var(--blue);border-bottom-color:var(--blue);}
    .tab-content{display:none;}
    .tab-content.active{display:block;}
    .meta{color:var(--muted);font-size:0.85rem;margin-bottom:1rem;}
    .grid{display:grid;gap:0.75rem;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));}
    .card{background:var(--panel);border:1px solid var(--border);border-radius:8px;padding:0.85rem;}
    .card h2{font-size:0.75rem;text-transform:uppercase;letter-spacing:.04em;color:var(--muted);margin:0 0 0.5rem;}
    .sig{font-size:1rem;font-weight:600;}
    .sig.wait{color:var(--yellow);}.sig.buy{color:var(--green);}.sig.block{color:var(--red);}
    .mono{font-family:ui-monospace,monospace;font-size:0.82rem;}
    .btc{border-color:#d29922;}
    footer{margin-top:1rem;color:var(--muted);font-size:0.75rem;}

    /* 表格样式 */
    table{width:100%;border-collapse:collapse;font-size:0.8rem;}
    th,td{padding:0.45rem 0.5rem;text-align:left;border-bottom:1px solid var(--border);}
    th{color:var(--muted);font-weight:600;font-size:0.72rem;text-transform:uppercase;position:sticky;top:0;background:var(--panel);z-index:1;}
    tr:hover{background:rgba(88,166,255,0.05);}
    .pnl-pos{color:var(--green);}.pnl-neg{color:var(--red);}
    .won{color:var(--green);}.lost{color:var(--red);}
    a{color:var(--blue);text-decoration:none;}
    a:hover{text-decoration:underline;}

    /* 统计卡片 */
    .stat-grid{display:grid;gap:0.5rem;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));margin-bottom:1rem;}
    .stat-card{background:var(--panel);border:1px solid var(--border);border-radius:6px;padding:0.65rem 0.75rem;text-align:center;}
    .stat-card .v{font-size:1.25rem;font-weight:700;}
    .stat-card .l{font-size:0.7rem;color:var(--muted);margin-top:0.15rem;text-transform:uppercase;}

    .toast{position:fixed;top:1rem;right:1rem;padding:0.5rem 1rem;border-radius:6px;font-size:0.8rem;z-index:99;display:none;}
    .toast.err{background:var(--red);color:#fff;}

    .loading{text-align:center;color:var(--muted);padding:2rem;}
  </style>
</head>
<body>
  <h1>BTC 涨跌 — 实时</h1>
  <div class="tabs">
    <button class="tab active" data-tab="dashboard">仪表盘</button>
    <button class="tab" data-tab="trades">交易记录</button>
    <button class="tab" data-tab="balance">余额变动</button>
    <button class="tab" data-tab="stats">交易统计</button>
  </div>
  <div class="meta" id="meta">加载中…</div>

  <!-- Tab 1: 仪表盘 -->
  <div class="tab-content active" id="tab-dashboard">
    <div class="grid">
      <div class="card"><h2>会话</h2><div id="session" class="mono"></div></div>
      <div class="card"><h2>策略</h2><div id="strategy"></div></div>
      <div class="card"><h2>UP</h2><div id="up" class="mono"></div></div>
      <div class="card"><h2>DOWN</h2><div id="down" class="mono"></div></div>
      <div class="card btc"><h2>BTC / USD (Chainlink)</h2><div id="btc" class="mono"></div></div>
      <div class="card"><h2>交易</h2><div id="trading" class="mono"></div></div>
    </div>
  </div>

  <!-- Tab 2: 交易记录 -->
  <div class="tab-content" id="tab-trades">
    <div class="stat-grid" id="trades-summary"></div>
    <div style="overflow-x:auto;">
      <table id="trades-table">
        <thead><tr>
          <th>#</th><th>时间</th><th>市场</th><th>方向</th><th>入场价</th><th>出场价</th><th>张数</th><th>盈亏</th><th>结果</th><th>对冲</th><th>订单号</th>
        </tr></thead>
        <tbody><tr><td colspan="11" class="loading">加载中…</td></tr></tbody>
      </table>
    </div>
  </div>

  <!-- Tab 3: 余额变动 -->
  <div class="tab-content" id="tab-balance">
    <div class="stat-grid" id="balance-summary"></div>
    <div style="overflow-x:auto;">
      <table id="balance-table">
        <thead><tr>
          <th>时间</th><th>资金</th><th>已实现盈亏</th><th>交易数</th><th>胜/负</th><th>胜率</th><th>模式</th>
        </tr></thead>
        <tbody><tr><td colspan="7" class="loading">加载中…</td></tr></tbody>
      </table>
    </div>
  </div>

  <!-- Tab 4: 交易统计 -->
  <div class="tab-content" id="tab-stats">
    <div class="stat-grid" id="stats-grid"></div>
    <div class="card" style="margin-top:1rem;">
      <h2>账户摘要</h2>
      <div id="account-stats" class="mono"></div>
    </div>
  </div>

  <footer>每秒刷新 · <span id="err"></span></footer>
  <div class="toast" id="toast"></div>

  <script>
    /* === 工具函数 ============================= */
    function esc(s){if(s===null||s===undefined)return"";var el=document.createElement("div");el.textContent=String(s);return el.innerHTML;}
    function numFmt(n,dec){if(n===null||n===undefined||typeof n!=="number"||isNaN(n))return"\u2014";return n.toFixed(dec);}
    function sigClass(t){if(!t)return"wait";if(t.indexOf("BUY")>=0)return"buy";if(t.indexOf("NO ENTRY")>=0)return"block";return"wait";}
    function chk(x){return x===true?"\u2713":x===false?"\u2717":"\u2014";}
    function tsFmt(ts){if(!ts)return"\u2014";var d=new Date(ts*1000);return d.toISOString().replace("T"," ").slice(0,19);}
    function tsShort(ts){if(!ts)return"\u2014";var d=new Date(ts*1000);return d.toISOString().slice(11,19);}
    var currentTab = "dashboard";
    var pollingActive = true;

    /* === Tab 切换 ============================= */
    (function(){
      document.querySelectorAll(".tab").forEach(function(btn){
        btn.addEventListener("click",function(){
          document.querySelectorAll(".tab").forEach(function(b){b.classList.remove("active");});
          document.querySelectorAll(".tab-content").forEach(function(c){c.classList.remove("active");});
          btn.classList.add("active");
          currentTab = btn.getAttribute("data-tab");
          document.getElementById("tab-"+currentTab).classList.add("active");
          loadTab(currentTab);
        });
      });
    })();

    function loadTab(name){
      if(name==="dashboard"){tick();return;}
      if(name==="trades"){loadTrades();return;}
      if(name==="balance"){loadBalance();return;}
      if(name==="stats"){loadStats();return;}
    }

    /* === 仪表盘（每秒刷新）===================== */
    var dashboardTimer = null;
    function tick(){
      var errEl=document.getElementById("err");
      var r=new XMLHttpRequest();
      r.open("GET","/api/state",true);
      r.onreadystatechange=function(){
        if(r.readyState!==4)return;
        try{
          if(r.status!==200)throw new Error("HTTP "+r.status);
          var d=JSON.parse(r.responseText);
          errEl.textContent="";
          var hdr=d.header||{};
          var slug=hdr.slug!=null?String(hdr.slug):"\u2014";
          var ts="";
          if(d.ts)ts=new Date(d.ts*1000).toISOString();
          document.getElementById("meta").innerHTML=esc(slug)+" \u00b7 "+esc(ts);
          document.getElementById("session").innerHTML=[
            "计时器: "+(hdr.time_left_sec!=null?esc(Math.floor(hdr.time_left_sec)+"秒剩余"):"\u2014"),
            "WS: "+(hdr.ws_connected?"已连接":"已断开"),
            "模式: "+(hdr.simulation?"模拟":"实盘"),
          ].join("<br/>");
          var st=d.strategy||{};
          var sig=st.signal_text||"\u2014";
          var ck=st.checks||{};
          document.getElementById("strategy").innerHTML=
            '<div class="sig '+sigClass(sig)+'">'+esc(sig)+"</div>"+
            '<div class="mono" style="margin-top:0.4rem">'+
            "偏好: "+esc(st.favorite)+" \u00b7 胜率: "+esc(st.win_rate_str)+"<br/>"+
            "检查: P="+chk(ck.price)+" T="+chk(ck.time)+" D="+chk(ck.dev)+
            " M="+chk(ck.mom)+" 截止="+chk(ck.time_cutoff)+
            "</div>";
          function book(x,id){
            var el=document.getElementById(id);
            if(!x){el.textContent="无数据";return;}
            var bk=x.book||{};
            var ind=x.indicators||{};
            el.innerHTML=[
              "最新 "+esc(bk.last_price),
              "买价 "+esc(bk.best_bid)+" / 卖价 "+esc(bk.best_ask),
              "VWAP "+numFmt(ind.vwap,4)+" \u00b7 偏差 "+(ind.deviation_pct!=null?numFmt(ind.deviation_pct,2)+"%":"\u2014"),
              "Z "+numFmt(ind.zscore,2)+" \u00b7 动量 "+(ind.momentum_pct!=null?numFmt(ind.momentum_pct,2)+"%":"\u2014"),
              "成交量 "+(bk.volume_total!=null?esc(Math.round(bk.volume_total)):"\u2014"),
            ].join("<br/>");
          }
          book(d.up,"up");book(d.down,"down");
          var b=d.btc||{};
          var btcEl=document.getElementById("btc");
          if(b.btc_current_price>0){
            btcEl.innerHTML=[
              "$"+esc(numFmt(b.btc_current_price,2)),
              "锚定 $"+(b.btc_anchor_price>0?esc(numFmt(b.btc_anchor_price,2)):"\u2014"),
              esc(b.deviation_line||""),
              "数据源: "+(b.btc_connected?"正常":"离线")+(b.fresh_sec!=null?" \u00b7 "+Math.floor(b.fresh_sec)+"s":""),
            ].join("<br/>");
          }else{btcEl.textContent="等待 Chainlink 数据…";}
          var tr=d.trading||{};
          var tHtml="市场数 "+esc(tr.markets_seen)+
            " \u00b7 交易数 "+esc(tr.trade_count)+
            " \u00b7 胜 "+esc(tr.wins)+" / 负 "+esc(tr.losses)+
            " \u00b7 盈亏 $"+(tr.total_pnl!=null?numFmt(tr.total_pnl,2):"\u2014")+"<br/>";
          if(tr.account){
            tHtml+="资金: $"+numFmt(tr.account.current_capital,2)+
              " (初始 $"+numFmt(tr.account.initial_capital,0)+")"+
              " \u00b7 已实现 $"+(tr.account.realized_pnl!=null?numFmt(tr.account.realized_pnl,2):"0")+"<br/>";
          }
          if(tr.position){
            var p=tr.position;
            tHtml+="做多 "+esc(p.token_name)+" @ "+esc(p.entry_price)+
              " \u00d7"+esc(p.contracts)+(p.hedged?" 已对冲":"")+"<br/>";
            tHtml+="未实现 $"+(p.unrealized_pnl!=null?numFmt(p.unrealized_pnl,2):"\u2014")+"<br/>";
          }else{tHtml+="无持仓<br/>";}
          if(tr.recent_trades&&tr.recent_trades.length){
            var lines=[];
            for(var i=0;i<tr.recent_trades.length;i++){lines.push(esc(tr.recent_trades[i].line));}
            tHtml+="<br/>最近:<br/>"+lines.join("<br/>");
          }
          document.getElementById("trading").innerHTML=tHtml;
        }catch(e){errEl.textContent="轮询错误: "+(e&&e.message?e.message:e);}
      };
      r.onerror=function(){errEl.textContent="网络错误（机器人是否在运行？）";};
      r.send();
    }

    /* === Tab: 交易记录 ======================= */
    function loadTrades(){
      var r=new XMLHttpRequest();
      r.open("GET","/api/trades?limit=200",true);
      r.onload=function(){
        try{
          var trades=JSON.parse(r.responseText);
          if(!Array.isArray(trades)){trades=[];}
          var totalPnl=0,wins=0,losses=0;
          trades.forEach(function(t){totalPnl+=t.pnl;if(t.won){wins++;}else{losses++;}});
          document.getElementById("trades-summary").innerHTML=[
            '<div class="stat-card"><div class="v">'+trades.length+'</div><div class="l">总交易数</div></div>',
            '<div class="stat-card"><div class="v '+(totalPnl>=0?"pnl-pos":"pnl-neg")+'">'+(totalPnl>=0?"+":"")+'$'+numFmt(totalPnl,2)+'</div><div class="l">累计盈亏</div></div>',
            '<div class="stat-card"><div class="v" style="color:var(--green)">'+wins+'</div><div class="l">胜</div></div>',
            '<div class="stat-card"><div class="v" style="color:var(--red)">'+losses+'</div><div class="l">负</div></div>',
            '<div class="stat-card"><div class="v">'+(trades.length>0?numFmt(wins/trades.length*100,1)+"%":"\u2014")+'</div><div class="l">胜率</div></div>',
          ].join("");
          var tbody="";
          if(trades.length===0){
            tbody='<tr><td colspan="11" style="text-align:center;color:var(--muted);padding:2rem;">暂无交易记录</td></tr>';
          }else{
            for(var i=0;i<Math.min(trades.length,200);i++){
              var t=trades[i];
              var orderLink="\u2014";
              if(t.order_id&&t.order_id.length>3&&t.order_id!=="recovered"){
                orderLink='<a href="https://clob.polymarket.com/orders/'+esc(t.order_id)+'" target="_blank" title="'+esc(t.order_id)+'">'+esc(t.order_id.slice(0,12))+'…</a>';
              }else if(t.order_id==="recovered"){
                orderLink='<span style="color:var(--yellow)" title="超时恢复，无原始订单号">已恢复</span>';
              }
              tbody+='<tr>'+
                '<td>'+esc(t.id)+'</td>'+
                '<td style="white-space:nowrap">'+esc(tsFmt(t.timestamp))+'</td>'+
                '<td style="max-width:140px;overflow:hidden;text-overflow:ellipsis" title="'+esc(t.market_slug)+'">'+esc(t.market_slug.slice(-30))+'</td>'+
                '<td>'+esc(t.token_name)+'</td>'+
                '<td>'+numFmt(t.entry_price,4)+'</td>'+
                '<td>'+numFmt(t.exit_price,4)+'</td>'+
                '<td>'+esc(t.contracts)+'</td>'+
                '<td class="'+(t.pnl>=0?"pnl-pos":"pnl-neg")+'">'+(t.pnl>=0?"+":"")+'$'+numFmt(t.pnl,2)+'</td>'+
                '<td class="'+(t.won?"won":"lost")+'">'+(t.won?"WIN":"LOSS")+'</td>'+
                '<td>'+(t.hedged?"是":"否")+'</td>'+
                '<td>'+orderLink+'</td>'+
                '</tr>';
            }
          }
          document.querySelector("#trades-table tbody").innerHTML=tbody;
        }catch(e){showToast("交易记录加载失败: "+e.message);}
      };
      r.onerror=function(){showToast("网络错误");};
      r.send();
    }

    /* === Tab: 余额变动 ======================= */
    function loadBalance(){
      var r=new XMLHttpRequest();
      r.open("GET","/api/snapshots?limit=200",true);
      r.onload=function(){
        try{
          var snaps=JSON.parse(r.responseText);
          if(!Array.isArray(snaps)){snaps=[];}
          snaps.reverse();
          if(snaps.length>0){
            var last=snaps[snaps.length-1];
            document.getElementById("balance-summary").innerHTML=[
              '<div class="stat-card"><div class="v">$'+numFmt(last.capital,2)+'</div><div class="l">当前资金</div></div>',
              '<div class="stat-card"><div class="v '+(last.realized_pnl>=0?"pnl-pos":"pnl-neg")+'">'+(last.realized_pnl>=0?"+":"")+'$'+numFmt(last.realized_pnl,2)+'</div><div class="l">已实现盈亏</div></div>',
              '<div class="stat-card"><div class="v">'+esc(last.trade_count)+'</div><div class="l">已平仓交易</div></div>',
              '<div class="stat-card"><div class="v">'+numFmt(last.win_rate_pct,1)+'%</div><div class="l">胜率</div></div>',
            ].join("");
          }
          var tbody="";
          if(snaps.length===0){
            tbody='<tr><td colspan="7" style="text-align:center;color:var(--muted);padding:2rem;">暂无余额变动记录（交易平仓后自动记录）</td></tr>';
          }else{
            for(var i=0;i<snaps.length;i++){
              var s=snaps[i];
              tbody+='<tr>'+
                '<td style="white-space:nowrap">'+esc(tsFmt(s.timestamp))+'</td>'+
                '<td>$'+numFmt(s.capital,2)+'</td>'+
                '<td class="'+(s.realized_pnl>=0?"pnl-pos":"pnl-neg")+'">'+(s.realized_pnl>=0?"+":"")+'$'+numFmt(s.realized_pnl,2)+'</td>'+
                '<td>'+esc(s.trade_count)+'</td>'+
                '<td>'+esc(s.win_count)+'W/'+esc(s.loss_count)+'L</td>'+
                '<td>'+numFmt(s.win_rate_pct,1)+'%</td>'+
                '<td>'+esc(s.mode==="simulation"?"模拟":"实盘")+'</td>'+
                '</tr>';
            }
          }
          document.querySelector("#balance-table tbody").innerHTML=tbody;
        }catch(e){showToast("余额变动加载失败: "+e.message);}
      };
      r.onerror=function(){showToast("网络错误");};
      r.send();
    }

    /* === Tab: 交易统计 ======================= */
    function loadStats(){
      var r=new XMLHttpRequest();
      r.open("GET","/api/stats",true);
      r.onload=function(){
        try{
          var data=JSON.parse(r.responseText);
          var s=data.summary||{};
          var a=data.account||{};
          document.getElementById("stats-grid").innerHTML=[
            '<div class="stat-card"><div class="v">'+esc(s.trade_count)+'</div><div class="l">总交易数</div></div>',
            '<div class="stat-card"><div class="v" style="color:var(--green)">'+esc(s.wins)+'</div><div class="l">胜</div></div>',
            '<div class="stat-card"><div class="v" style="color:var(--red)">'+esc(s.losses)+'</div><div class="l">负</div></div>',
            '<div class="stat-card"><div class="v">'+numFmt(s.win_rate_pct,1)+'%</div><div class="l">胜率</div></div>',
            '<div class="stat-card"><div class="v '+(s.total_pnl_usd>=0?"pnl-pos":"pnl-neg")+'">'+(s.total_pnl_usd>=0?"+":"")+'$'+numFmt(s.total_pnl_usd,2)+'</div><div class="l">累计盈亏</div></div>',
            '<div class="stat-card"><div class="v '+(s.avg_trade_pnl_usd>=0?"pnl-pos":"pnl-neg")+'">'+(s.avg_trade_pnl_usd>=0?"+":"")+'$'+numFmt(s.avg_trade_pnl_usd,2)+'</div><div class="l">平均每笔</div></div>',
            '<div class="stat-card"><div class="v" style="color:var(--green)">'+(s.best_trade_pnl_usd!=null?"+$"+numFmt(s.best_trade_pnl_usd,2):"\u2014")+'</div><div class="l">最佳交易</div></div>',
            '<div class="stat-card"><div class="v" style="color:var(--red)">'+(s.worst_trade_pnl_usd!=null?"$"+numFmt(s.worst_trade_pnl_usd,2):"\u2014")+'</div><div class="l">最差交易</div></div>',
          ].join("");
          if(a){
            document.getElementById("account-stats").innerHTML=[
              "初始资金: $"+numFmt(a.initial_capital,0),
              "当前资金: $"+numFmt(a.current_capital,2),
              "已实现盈亏: "+(a.realized_pnl>=0?"+":"")+"$"+numFmt(a.realized_pnl,2),
              "收益率: "+(a.initial_capital>0?numFmt((a.current_capital-a.initial_capital)/a.initial_capital*100,2)+"%":"\u2014"),
            ].join("<br/>");
          }
        }catch(e){showToast("统计加载失败: "+e.message);}
      };
      r.onerror=function(){showToast("网络错误");};
      r.send();
    }

    function showToast(msg){
      var t=document.getElementById("toast");
      t.textContent=msg;t.className="toast err";t.style.display="block";
      setTimeout(function(){t.style.display="none";},3000);
    }

    /* === 初始化 ============================== */
    tick();
    setInterval(function(){
      tick();
      if(currentTab==="trades")loadTrades();
      else if(currentTab==="balance")loadBalance();
      else if(currentTab==="stats")loadStats();
    },5000);

    /* 每30秒刷新非仪表盘 tab */
    setInterval(function(){
      if(currentTab!=="dashboard")loadTab(currentTab);
    },30000);
  </script>
</body>
</html>
"""


def _sanitize_for_json(obj: Any) -> Any:
    """
    Starlette JSONResponse serializes with allow_nan=False; NaN/Inf break the ASGI handler.
    """
    if obj is None:
        return None
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, int) and not isinstance(obj, bool):
        return obj
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
        return obj
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_sanitize_for_json(v) for v in obj]
    return obj


class WebSnapshotHolder:
    """Thread-safe snapshot for /api/state."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = {"status": "starting"}

    def set(self, data: Dict[str, Any]) -> None:
        with self._lock:
            self._data = dict(data)

    def get(self) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data)


def build_app(holder: WebSnapshotHolder, password: str = "", db=None) -> FastAPI:
    """
    FastAPI app with optional password protection and multi-tab dashboard.
    """
    app = FastAPI(title="BTC Live Bot", docs_url=None, redoc_url=None)
    auth_required = bool(password and password.strip())

    def _check_auth(request: Request) -> bool:
        if not auth_required:
            return True
        token = request.cookies.get(_AUTH_COOKIE)
        if not token:
            return False
        return _verify_auth_token(token, password)

    # ── 页面路由 ──────────────────────────────────────────────
    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> Response:
        return Response(status_code=204)

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request):
        if _check_auth(request):
            return _HTML_TABBED
        return _LOGIN_HTML.replace("__ERROR__", "")

    @app.post("/login", response_class=HTMLResponse)
    async def login(request: Request, password_input: str = Form("", alias="password")):
        if not auth_required:
            return RedirectResponse(url="/", status_code=303)
        if password_input == password:
            token = _make_auth_token(password)
            resp = RedirectResponse(url="/", status_code=303)
            resp.set_cookie(
                key=_AUTH_COOKIE,
                value=token,
                max_age=_THREE_HOURS,
                httponly=True,
                samesite="lax",
                secure=False,
            )
            return resp
        return HTMLResponse(_LOGIN_HTML.replace("__ERROR__", "密码错误，请重试"), status_code=401)

    # ── API ───────────────────────────────────────────────────
    @app.get("/api/state")
    async def api_state(request: Request):
        if not _check_auth(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        return JSONResponse(_sanitize_for_json(holder.get()))

    @app.get("/api/trades")
    async def api_trades(request: Request, mode: str = "", limit: int = 200):
        if not _check_auth(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not db:
            return JSONResponse([])
        try:
            m = mode or None
            rows = db.get_trades(mode=m, limit=limit)
            return JSONResponse(_sanitize_for_json(rows))
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/api/snapshots")
    async def api_snapshots(request: Request, mode: str = "", limit: int = 200):
        if not _check_auth(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not db:
            return JSONResponse([])
        try:
            m = mode or None
            rows = db.get_snapshots(mode=m, limit=limit)
            return JSONResponse(_sanitize_for_json(rows))
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/api/stats")
    async def api_stats(request: Request, mode: str = ""):
        if not _check_auth(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not db:
            return JSONResponse({})
        try:
            m = mode or None
            summary = db.get_trade_summary(mode=m)
            account = db.get_account(mode=m or "live")
            return JSONResponse(_sanitize_for_json({
                "summary": summary,
                "account": dict(account) if account else None,
            }))
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    @app.get("/api/account")
    async def api_account(request: Request):
        if not _check_auth(request):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        if not db:
            return JSONResponse({})
        try:
            live_acc = db.get_account("live")
            sim_acc = db.get_account("simulation")
            return JSONResponse(_sanitize_for_json({
                "live": dict(live_acc) if live_acc else None,
                "simulation": dict(sim_acc) if sim_acc else None,
            }))
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)

    return app


def _client_probe_address(bind_host: str) -> str:
    """Address to test with socket.connect(); 0.0.0.0 / :: are not valid client targets."""
    if bind_host in ("0.0.0.0", ""):
        return "127.0.0.1"
    if bind_host in ("::", "[::]"):
        return "::1"
    return bind_host


def start_web_dashboard(host: str, port: int, holder: WebSnapshotHolder, password: str = "", db=None) -> bool:
    """
    Start uvicorn in a daemon thread. Returns True if the port accepts connections
    shortly after start (False if bind failed or port is in use).
    
    Args:
        password: If non-empty, require this password for dashboard access (login page shown).
        db: Database instance for trade/account/stats API endpoints.
    """
    app = build_app(holder, password, db)

    def run() -> None:
        try:
            uvicorn.run(
                app,
                host=host,
                port=port,
                log_level="warning",
                access_log=False,
            )
        except Exception:
            logger.exception("Web dashboard: uvicorn exited with an error")

    t = threading.Thread(target=run, name="web-dashboard", daemon=True)
    t.start()

    probe = _client_probe_address(host)
    for _ in range(60):
        time.sleep(0.1)
        try:
            with socket.create_connection((probe, port), timeout=0.4):
                return True
        except OSError:
            continue
    return False
