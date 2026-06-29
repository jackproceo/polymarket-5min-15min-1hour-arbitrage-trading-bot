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
      --bg: #f5f6f8; --panel: #ffffff; --border: #e0e4e8;
      --text: #1a1d23; --muted: #6b7280; --green: #16a34a; --red: #dc2626;
      --blue: #2563eb;
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
      box-shadow: 0 4px 16px rgba(0,0,0,0.08);
    }
    h1 { font-size: 1.1rem; font-weight: 600; margin-bottom: 0.5rem; color: #111827; }
    p.sub { color: var(--muted); font-size: 0.85rem; margin-bottom: 1.25rem; }
    input {
      width: 100%; padding: 0.65rem 0.85rem;
      background: #f9fafb; border: 1px solid var(--border); border-radius: 6px;
      color: var(--text); font-size: 0.95rem; outline: none;
    }
    input:focus { border-color: var(--blue); box-shadow: 0 0 0 3px rgba(37,99,235,0.1); }
    button {
      width: 100%; margin-top: 0.75rem; padding: 0.65rem;
      background: var(--blue); color: #fff;
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
  <h1>BTC 涨跌 — 实时 文件夹:btc-binary-VWAP-Momentum-bot docker: bot-vwap 24008</h1>
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
  <title>🚀 BTC 实时交易机器人 - 高级仪表盘</title>
  <!-- Font Awesome 图标 -->
  <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css">
  <!-- Google Fonts -->
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg-primary: linear-gradient(135deg, #f8fafc 0%, #f1f5f9 50%, #e2e8f0 100%);
      --bg-secondary: linear-gradient(135deg, #ffffff 0%, #f8fafc 100%);
      --panel: rgba(255, 255, 255, 0.95);
      --panel-glass: rgba(255, 255, 255, 0.85);
      --panel-solid: #ffffff;
      --border: rgba(203, 213, 225, 0.6);
      --border-glow: rgba(59, 130, 246, 0.2);
      --text-primary: #1e293b;
      --text-secondary: #475569;
      --text-muted: #64748b;
      --green: #16a34a;
      --green-glow: rgba(22, 163, 74, 0.1);
      --red: #dc2626;
      --red-glow: rgba(220, 38, 38, 0.1);
      --yellow: #ca8a04;
      --yellow-glow: rgba(202, 138, 4, 0.1);
      --blue: #2563eb;
      --blue-glow: rgba(37, 99, 235, 0.1);
      --violet: #7c3aed;
      --cyan: #0891b2;
      --primary-gradient: linear-gradient(135deg, #3b82f6 0%, #8b5cf6 100%);
      --success-gradient: linear-gradient(135deg, #22c55e 0%, #16a34a 100%);
      --danger-gradient: linear-gradient(135deg, #ef4444 0%, #dc2626 100%);
      --warning-gradient: linear-gradient(135deg, #eab308 0%, #ca8a04 100%);
      --btc-gradient: linear-gradient(135deg, #f59e0b 0%, #fbbf24 100%);
      --shadow-lg: 0 20px 40px rgba(0, 0, 0, 0.12);
      --shadow-md: 0 10px 20px rgba(0, 0, 0, 0.1);
      --shadow-sm: 0 5px 15px rgba(0, 0, 0, 0.08);
      --radius-lg: 16px;
      --radius-md: 12px;
      --radius-sm: 8px;
      --transition-normal: all 0.3s cubic-bezier(0.4, 0, 0.2, 1);
      --transition-fast: all 0.2s ease;
    }
    
    * {
      box-sizing: border-box;
      margin: 0;
      padding: 0;
    }
    
    body {
      font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
      background: var(--bg-primary);
      color: var(--text-primary);
      padding: 1.5rem 2rem 2.5rem;
      line-height: 1.6;
      min-height: 100vh;
      -webkit-font-smoothing: antialiased;
      -moz-osx-font-smoothing: grayscale;
      position: relative;
      overflow-x: hidden;
    }

    /* 背景装饰 */
    body::before {
      content: '';
      position: fixed;
      top: 0;
      left: 0;
      width: 100%;
      height: 100%;
      background: 
        radial-gradient(circle at 20% 80%, rgba(59, 130, 246, 0.05) 0%, transparent 50%),
        radial-gradient(circle at 80% 20%, rgba(139, 92, 246, 0.05) 0%, transparent 50%),
        radial-gradient(circle at 40% 40%, rgba(6, 182, 212, 0.02) 0%, transparent 30%);
      z-index: -1;
      pointer-events: none;
    }
    
    /* 顶部标题栏 */
    .header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 2rem;
      padding-bottom: 1.5rem;
      border-bottom: 1px solid var(--border);
      position: relative;
    }

    .header::after {
      content: '';
      position: absolute;
      bottom: -1px;
      left: 0;
      width: 100%;
      height: 1px;
      background: linear-gradient(90deg, transparent, var(--blue), transparent);
    }
    
    .header h1 {
      font-size: 1.8rem;
      font-weight: 800;
      margin: 0;
      background: linear-gradient(135deg, #60a5fa 0%, #a78bfa 50%, #38bdf8 100%);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      background-clip: text;
      display: flex;
      align-items: center;
      gap: 1rem;
      letter-spacing: -0.5px;
    }
    
    .header h1 i {
      font-size: 1.8rem;
      background: var(--primary-gradient);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      background-clip: text;
      animation: pulse 2s ease-in-out infinite;
    }

    @keyframes pulse {
      0%, 100% { opacity: 1; }
      50% { opacity: 0.8; }
    }
    
    .header-controls {
      display: flex;
      align-items: center;
      gap: 1.5rem;
    }
    
    .status-badge {
      background: rgba(22, 163, 74, 0.1);
      color: #16a34a;
      padding: 0.5rem 1rem;
      border-radius: 50px;
      font-size: 0.85rem;
      font-weight: 600;
      border: 1px solid rgba(22, 163, 74, 0.2);
      display: flex;
      align-items: center;
      gap: 0.5rem;
      position: relative;
      overflow: hidden;
      transition: var(--transition-fast);
    }

    .status-badge::before {
      content: '';
      position: absolute;
      top: 0;
      left: -100%;
      width: 100%;
      height: 100%;
      background: linear-gradient(90deg, transparent, rgba(34, 197, 94, 0.2), transparent);
      animation: shine 3s infinite;
    }

    @keyframes shine {
      0% { left: -100%; }
      100% { left: 100%; }
    }
    
    .status-badge.offline {
      background: rgba(239, 68, 68, 0.1);
      color: #dc2626;
      border-color: rgba(239, 68, 68, 0.2);
    }

    .status-badge.offline::before {
      background: linear-gradient(90deg, transparent, rgba(239, 68, 68, 0.2), transparent);
    }
    
    .btn-primary {
      background: var(--primary-gradient);
      color: white;
      border: none;
      padding: 0.75rem 1.5rem;
      border-radius: var(--radius-md);
      font-size: 0.9rem;
      font-weight: 600;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 0.6rem;
      transition: var(--transition-fast);
      box-shadow: var(--shadow-md);
      position: relative;
      overflow: hidden;
    }

    .btn-primary::before {
      content: '';
      position: absolute;
      top: 0;
      left: -100%;
      width: 100%;
      height: 100%;
      background: linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.2), transparent);
      transition: left 0.5s ease;
    }

    .btn-primary:hover {
      transform: translateY(-3px);
      box-shadow: var(--shadow-lg);
    }

    .btn-primary:hover::before {
      left: 100%;
    }
    
    .btn-primary:active {
      transform: translateY(-1px);
    }

    .btn-primary i {
      font-size: 0.9rem;
    }
    
    /* 标签页样式 */
    .tabs {
      display: flex;
      gap: 0.5rem;
      margin-bottom: 2rem;
      background: rgba(248, 250, 252, 0.9);
      padding: 0.5rem;
      border-radius: var(--radius-lg);
      border: 1px solid var(--border);
      box-shadow: var(--shadow-sm);
    }
    
    .tab {
      padding: 0.8rem 1.5rem;
      cursor: pointer;
      border: none;
      background: transparent;
      color: var(--text-muted);
      font-size: 0.9rem;
      font-weight: 600;
      border-radius: var(--radius-md);
      transition: var(--transition-fast);
      display: flex;
      align-items: center;
      gap: 0.6rem;
      position: relative;
      overflow: hidden;
    }

    .tab::before {
      content: '';
      position: absolute;
      bottom: 0;
      left: 50%;
      width: 0;
      height: 2px;
      background: var(--primary-gradient);
      transition: var(--transition-fast);
      transform: translateX(-50%);
    }
    
    .tab:hover {
      background: rgba(71, 85, 105, 0.3);
      color: var(--text-primary);
    }

    .tab:hover::before {
      width: 80%;
    }
    
    .tab.active {
      background: var(--panel-glass);
      box-shadow: var(--shadow-sm);
    }

    .tab.active::before {
      width: 100%;
      height: 3px;
    }
    
    /* 选项卡内容样式 */
    .tab-content {
      display: none;
      opacity: 0;
      transform: translateY(10px);
      transition: opacity 0.3s ease, transform 0.3s ease;
    }
    
    .tab-content.active {
      display: block;
      opacity: 1;
      transform: translateY(0);
      animation: fadeIn 0.3s ease;
    }
    
    @keyframes fadeIn {
      from {
        opacity: 0;
        transform: translateY(10px);
      }
      to {
        opacity: 1;
        transform: translateY(0);
      }
    }
    
    /* 卡片样式 */
    .grid {
      display: grid;
      gap: 1.5rem;
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
    }
    
    .card {
      background: var(--panel-solid);
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      padding: 1.5rem;
      box-shadow: var(--shadow-md);
      transition: var(--transition-normal);
      position: relative;
      overflow: hidden;
    }

    .card::before {
      content: '';
      position: absolute;
      top: 0;
      left: 0;
      right: 0;
      height: 3px;
      background: var(--primary-gradient);
    }

    .card:hover {
      transform: translateY(-5px);
      box-shadow: var(--shadow-lg);
      border-color: var(--border-glow);
    }
    
    .card.btc::before {
      background: var(--btc-gradient);
    }

    .card.btc:hover {
      border-color: rgba(245, 158, 11, 0.3);
      box-shadow: 0 12px 28px rgba(245, 158, 11, 0.15);
    }
    
    .card h2 {
      font-size: 0.85rem;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      color: var(--cyan);
      margin: 0 0 1rem;
      font-weight: 700;
      display: flex;
      align-items: center;
      gap: 0.6rem;
      padding-bottom: 0.5rem;
      border-bottom: 1px solid rgba(71, 85, 105, 0.3);
    }
    
    .card h2 i {
      font-size: 1rem;
      width: 1.2rem;
      text-align: center;
    }
    
    /* 信号指示器 */
    .sig {
      font-size: 1.2rem;
      font-weight: 800;
      padding: 0.6rem 1rem;
      border-radius: var(--radius-md);
      display: inline-block;
      margin-bottom: 1rem;
      text-align: center;
      transition: var(--transition-fast);
      box-shadow: var(--shadow-sm);
      letter-spacing: 0.5px;
    }
    
    .sig.wait {
      background: linear-gradient(135deg, rgba(234, 179, 8, 0.2) 0%, rgba(202, 138, 4, 0.1) 100%);
      color: #eab308;
      border: 1px solid rgba(234, 179, 8, 0.3);
    }

    .sig.wait:hover {
      background: linear-gradient(135deg, rgba(234, 179, 8, 0.3) 0%, rgba(202, 138, 4, 0.2) 100%);
      transform: scale(1.02);
    }
    
    .sig.buy {
      background: linear-gradient(135deg, rgba(34, 197, 94, 0.2) 0%, rgba(22, 163, 74, 0.1) 100%);
      color: #22c55e;
      border: 1px solid rgba(34, 197, 94, 0.3);
    }

    .sig.buy:hover {
      background: linear-gradient(135deg, rgba(34, 197, 94, 0.3) 0%, rgba(22, 163, 74, 0.2) 100%);
      transform: scale(1.02);
    }
    
    .sig.block {
      background: linear-gradient(135deg, rgba(239, 68, 68, 0.2) 0%, rgba(220, 38, 38, 0.1) 100%);
      color: #ef4444;
      border: 1px solid rgba(239, 68, 68, 0.3);
    }

    .sig.block:hover {
      background: linear-gradient(135deg, rgba(239, 68, 68, 0.3) 0%, rgba(220, 38, 38, 0.2) 100%);
      transform: scale(1.02);
    }
    
    /* 单行显示 */
    .mono {
      font-family: 'JetBrains Mono', 'Fira Code', 'Cascadia Code', monospace;
      font-size: 0.9rem;
      line-height: 1.6;
      color: var(--text-secondary);
    }
    
    /* 元信息样式 */
    .meta {
      background: rgba(248, 250, 252, 0.9);
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      padding: 1rem 1.5rem;
      margin-bottom: 2rem;
      font-size: 0.9rem;
      color: var(--text-secondary);
      display: flex;
      justify-content: space-between;
      align-items: center;
      box-shadow: var(--shadow-sm);
      transition: var(--transition-fast);
    }

    .meta:hover {
      border-color: var(--border-glow);
      box-shadow: var(--shadow-md);
    }
    
    .meta a {
      color: var(--cyan);
      text-decoration: none;
      font-weight: 600;
      display: inline-flex;
      align-items: center;
      gap: 0.4rem;
      padding: 0.4rem 0.8rem;
      border-radius: var(--radius-sm);
      background: rgba(6, 182, 212, 0.1);
      border: 1px solid rgba(6, 182, 212, 0.2);
      transition: var(--transition-fast);
    }
    
    .meta a:hover {
      text-decoration: none;
      color: #22d3ee;
      background: rgba(6, 182, 212, 0.2);
      border-color: rgba(6, 182, 212, 0.3);
      transform: translateY(-2px);
    }

    .meta a i {
      font-size: 0.8rem;
    }
    
    /* 表格样式 */
    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 0.85rem;
      background: var(--panel-solid);
      border-radius: var(--radius-lg);
      overflow: hidden;
      box-shadow: var(--shadow-md);
      margin-top: 1rem;
      border: 1px solid var(--border);
    }
    
    th, td {
      padding: 0.9rem 1.2rem;
      text-align: left;
      border-bottom: 1px solid var(--border);
    }
    
    th {
      color: var(--text-primary);
      font-weight: 700;
      font-size: 0.8rem;
      text-transform: uppercase;
      background: rgba(248, 250, 252, 0.9);
      position: sticky;
      top: 0;
      z-index: 1;
      border-bottom: 2px solid var(--border);
    }

    th i {
      margin-right: 0.4rem;
      color: var(--blue);
    }
    
    tr {
      transition: var(--transition-fast);
    }

    tr:hover {
      background: rgba(59, 130, 246, 0.05);
    }
    
    tr:last-child td {
      border-bottom: none;
    }
    
    .pnl-pos {
      color: var(--green);
      font-weight: 700;
      text-shadow: 0 0 10px var(--green-glow);
    }
    
    .pnl-neg {
      color: var(--red);
      font-weight: 700;
      text-shadow: 0 0 10px var(--red-glow);
    }
    
    .won {
      color: var(--green);
      font-weight: 700;
      display: inline-flex;
      align-items: center;
      gap: 0.3rem;
    }

    .won::before {
      content: '✓';
    }
    
    .lost {
      color: var(--red);
      font-weight: 700;
      display: inline-flex;
      align-items: center;
      gap: 0.3rem;
    }

    .lost::before {
      content: '✗';
    }
    
    /* 统计卡片 */
    .stat-grid {
      display: grid;
      gap: 1rem;
      grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
      margin-bottom: 2rem;
    }
    
    .stat-card {
      background: var(--panel-glass);
      border: 1px solid var(--border);
      border-radius: var(--radius-lg);
      padding: 1.5rem 1rem;
      text-align: center;
      box-shadow: var(--shadow-md);
      transition: var(--transition-normal);
      backdrop-filter: blur(10px);
      position: relative;
      overflow: hidden;
    }

    .stat-card::before {
      content: '';
      position: absolute;
      top: 0;
      left: 0;
      right: 0;
      height: 4px;
      background: var(--primary-gradient);
    }
    
    .stat-card:hover {
      transform: translateY(-5px);
      box-shadow: var(--shadow-lg);
      border-color: var(--border-glow);
    }
    
    .stat-card .v {
      font-size: 1.8rem;
      font-weight: 900;
      margin-bottom: 0.5rem;
      background: var(--primary-gradient);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
      background-clip: text;
      letter-spacing: -0.5px;
    }
    
    .stat-card .l {
      font-size: 0.8rem;
      color: var(--text-muted);
      text-transform: uppercase;
      letter-spacing: 0.06em;
      font-weight: 600;
    }
    
    /* 页脚 */
    footer {
      margin-top: 3rem;
      color: var(--text-secondary);
      font-size: 0.85rem;
      text-align: center;
      padding-top: 1.5rem;
      border-top: 1px solid var(--border);
      position: relative;
    }

    footer::before {
      content: '';
      position: absolute;
      top: -1px;
      left: 50%;
      transform: translateX(-50%);
      width: 200px;
      height: 1px;
      background: linear-gradient(90deg, transparent, var(--blue), transparent);
    }
    
    /* Toast 通知 */
    .toast {
      position: fixed;
      top: 2rem;
      right: 2rem;
      padding: 1rem 1.8rem;
      border-radius: var(--radius-lg);
      font-size: 0.9rem;
      font-weight: 600;
      z-index: 9999;
      display: none;
      box-shadow: var(--shadow-lg);
      backdrop-filter: blur(20px);
      animation: slideIn 0.4s cubic-bezier(0.68, -0.55, 0.27, 1.55);
      border: 1px solid rgba(255, 255, 255, 0.1);
    }

    .toast::before {
      content: '';
      position: absolute;
      top: 0;
      left: 0;
      right: 0;
      height: 3px;
      border-radius: var(--radius-lg) var(--radius-lg) 0 0;
    }
    
    .toast.err {
      background: linear-gradient(135deg, rgba(239, 68, 68, 0.9) 0%, rgba(220, 38, 38, 0.8) 100%);
      color: white;
    }

    .toast.err::before {
      background: linear-gradient(90deg, #ef4444, #dc2626);
    }
    
    .toast.success {
      background: linear-gradient(135deg, rgba(34, 197, 94, 0.9) 0%, rgba(22, 163, 74, 0.8) 100%);
      color: white;
    }

    .toast.success::before {
      background: linear-gradient(90deg, #22c55e, #16a34a);
    }
    
    @keyframes slideIn {
      from {
        transform: translateX(100%) translateY(-20px);
        opacity: 0;
      }
      to {
        transform: translateX(0) translateY(0);
        opacity: 1;
      }
    }
    
    /* 加载动画 */
    .loading {
      text-align: center;
      color: var(--text-muted);
      padding: 3rem;
      font-size: 0.9rem;
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 1rem;
    }

    .loading-text {
      font-size: 1rem;
      font-weight: 600;
      color: var(--text-secondary);
    }
    
    .loading-spinner {
      width: 40px;
      height: 40px;
      border: 3px solid var(--border);
      border-top-color: var(--blue);
      border-radius: 50%;
      animation: spin 1s linear infinite;
    }
    
    @keyframes spin {
      to { transform: rotate(360deg); }
    }
    
    /* 响应式设计 */
    @media (max-width: 1024px) {
      body {
        padding: 1rem 1.5rem 2rem;
      }
      
      .grid {
        grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
        gap: 1.2rem;
      }
      
      .header h1 {
        font-size: 1.5rem;
      }
    }
    
    @media (max-width: 768px) {
      body {
        padding: 1rem 1rem 1.5rem;
      }
      
      .header {
        flex-direction: column;
        align-items: flex-start;
        gap: 1.2rem;
        margin-bottom: 1.5rem;
      }
      
      .header-controls {
        width: 100%;
        justify-content: space-between;
      }
      
      .grid {
        grid-template-columns: 1fr;
        gap: 1rem;
      }
      
      .stat-grid {
        grid-template-columns: repeat(2, 1fr);
        gap: 0.8rem;
      }
      
      .tabs {
        flex-wrap: wrap;
        padding: 0.4rem;
      }
      
      .tab {
        flex: 1;
        min-width: 140px;
        justify-content: center;
        padding: 0.7rem 1rem;
        font-size: 0.85rem;
      }

      .meta {
        flex-direction: column;
        gap: 1rem;
        text-align: center;
        padding: 1rem;
      }

      footer {
        text-align: center;
      }

      footer > div {
        flex-direction: column;
        gap: 1rem;
      }
    }
    
    @media (max-width: 480px) {
      .stat-grid {
        grid-template-columns: 1fr;
      }
      
      .tabs {
        flex-direction: column;
      }
      
      .tab {
        width: 100%;
        justify-content: center;
      }

      .btn-primary {
        padding: 0.6rem 1rem;
        font-size: 0.85rem;
      }

      .status-badge {
        padding: 0.4rem 0.8rem;
        font-size: 0.8rem;
      }

      table {
        font-size: 0.8rem;
      }

      th, td {
        padding: 0.6rem 0.8rem;
      }
    }

    /* 滚动条样式 */
    ::-webkit-scrollbar {
      width: 8px;
      height: 8px;
    }

    ::-webkit-scrollbar-track {
      background: rgba(203, 213, 225, 0.3);
      border-radius: 4px;
    }

    ::-webkit-scrollbar-thumb {
      background: rgba(100, 116, 139, 0.6);
      border-radius: 4px;
      transition: var(--transition-fast);
    }

    ::-webkit-scrollbar-thumb:hover {
      background: rgba(100, 116, 139, 0.8);
    }

    /* 数字动画 */
    .count-up {
      display: inline-block;
      animation: countUp 1s ease-out forwards;
    }

    @keyframes countUp {
      from { transform: translateY(10px); opacity: 0; }
      to { transform: translateY(0); opacity: 1; }
    }

    /* 高亮效果 */
    .highlight {
      position: relative;
      display: inline-block;
    }

    .highlight::after {
      content: '';
      position: absolute;
      bottom: 0;
      left: 0;
      width: 100%;
      height: 2px;
      background: var(--primary-gradient);
      transform: scaleX(0);
      transform-origin: right;
      transition: transform 0.3s ease;
    }

    .highlight:hover::after {
      transform: scaleX(1);
      transform-origin: left;
    }

    /* 分隔线 */
    .divider {
      height: 1px;
      background: linear-gradient(90deg, transparent, var(--border), transparent);
      margin: 1.5rem 0;
    }

    /* 工具提示 */
    [data-tooltip] {
      position: relative;
      cursor: help;
    }

    [data-tooltip]::after {
      content: attr(data-tooltip);
      position: absolute;
      bottom: 100%;
      left: 50%;
      transform: translateX(-50%);
      padding: 0.5rem 0.8rem;
      background: var(--panel-solid);
      color: var(--text-primary);
      font-size: 0.75rem;
      border-radius: var(--radius-sm);
      white-space: nowrap;
      opacity: 0;
      visibility: hidden;
      transition: var(--transition-fast);
      z-index: 1000;
      border: 1px solid var(--border);
      box-shadow: var(--shadow-md);
      pointer-events: none;
    }

    [data-tooltip]:hover::after {
      opacity: 1;
      visibility: visible;
      bottom: calc(100% + 5px);
    }
  </style>
</head>
<body>
  <!-- 头部区域 -->
  <div class="header">
    <div>
      <h1><i class="fas fa-chart-line"></i> BTC 涨跌 — 实时交易机器人</h1>
      <div style="font-size: 0.85rem; color: var(--text-muted); margin-top: 0.25rem; display: flex; align-items: center; gap: 1rem;">
        <span><i class="fas fa-folder" style="margin-right: 0.3rem;"></i>btc-binary-VWAP-Momentum-bot</span>
        <span><i class="fas fa-cube" style="margin-right: 0.3rem;"></i>docker: bot-vwap</span>
        <span><i class="fas fa-network-wired" style="margin-right: 0.3rem;"></i>24008</span>
      </div>
    </div>
    <div class="header-controls">
      <div class="status-badge" id="status-badge">
        <i class="fas fa-circle" style="font-size: 0.6rem;"></i>
        <span>正在连接...</span>
      </div>
      <button class="btn-primary" onclick="reloadAll()">
        <i class="fas fa-sync-alt"></i>
        刷新数据
      </button>
    </div>
  </div>
  
  <!-- 标签页导航 -->
  <div class="tabs">
    <button class="tab active" data-tab="dashboard">
      <i class="fas fa-tachometer-alt"></i>
      仪表盘
    </button>
    <button class="tab" data-tab="trades">
      <i class="fas fa-exchange-alt"></i>
      交易记录
    </button>
    <button class="tab" data-tab="balance">
      <i class="fas fa-wallet"></i>
      余额变动
    </button>
    <button class="tab" data-tab="stats">
      <i class="fas fa-chart-bar"></i>
      交易统计
    </button>
  </div>
  
  <!-- 快速统计栏 -->
  <div class="stat-grid" id="quick-stats" style="margin-bottom: 1.5rem;">
    <div class="stat-card">
      <div class="v" id="stats-trades">--</div>
      <div class="l">总交易数</div>
    </div>
    <div class="stat-card">
      <div class="v" id="stats-wins">--</div>
      <div class="l">胜率</div>
    </div>
    <div class="stat-card">
      <div class="v" id="stats-pnl">--</div>
      <div class="l">累计盈亏</div>
    </div>
    <div class="stat-card">
      <div class="v" id="stats-uptime">--</div>
      <div class="l">运行时间</div>
    </div>
  </div>
  
  <!-- 元信息 -->
  <div class="meta" id="meta">
    <div style="display: flex; align-items: center; gap: 0.75rem;">
      <i class="fas fa-info-circle" style="color: var(--cyan);"></i>
      <span id="market-info">正在加载机器人数据...</span>
    </div>
    <div style="display: flex; align-items: center; gap: 0.75rem;">
      <span id="last-update" style="font-size: 0.8rem; color: var(--text-muted);">
        <i class="fas fa-clock"></i> 最后更新: 刚刚
      </span>
      <a href="#" id="market-link" target="_blank" rel="noopener">
        <i class="fas fa-external-link-alt"></i>
        查看市场
      </a>
    </div>
  </div>
  
  <!-- Tab 1: 仪表盘 -->
  <div class="tab-content active" id="tab-dashboard">
    <div class="grid">
      <div class="card">
        <h2><i class="fas fa-desktop"></i> 会话状态</h2>
        <div id="session" class="mono"></div>
        <div class="divider"></div>
        <div style="display: flex; align-items: center; gap: 0.5rem; margin-top: 0.75rem; color: var(--text-muted); font-size: 0.8rem;">
          <i class="fas fa-clock"></i>
          <span id="timer-display">计时器: --</span>
        </div>
      </div>
      
      <div class="card">
        <h2><i class="fas fa-robot"></i> 交易策略</h2>
        <div id="strategy"></div>
        <div class="divider"></div>
        <div style="margin-top: 0.75rem; display: flex; flex-wrap: wrap; gap: 0.5rem;" id="strategy-checks">
          <div style="display: flex; align-items: center; gap: 0.3rem; font-size: 0.75rem;">
            <i class="fas fa-circle" style="color: var(--text-muted); font-size: 0.5rem;"></i>
            <span>等待策略数据...</span>
          </div>
        </div>
      </div>
      
      <div class="card">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem;">
          <h2><i class="fas fa-arrow-up" style="color: var(--green);"></i> UP 市场</h2>
          <div style="display: flex; align-items: center; gap: 0.4rem;">
            <i class="fas fa-chart-line" style="font-size: 0.7rem; color: var(--text-muted);"></i>
            <span id="up-status" style="font-size: 0.7rem; color: var(--text-muted);">--</span>
          </div>
        </div>
        <div id="up" class="mono"></div>
        <div class="divider"></div>
        <div id="up-indicators" style="margin-top: 0.75rem; display: grid; grid-template-columns: 1fr 1fr; gap: 0.5rem; font-size: 0.75rem;">
          <div style="color: var(--text-muted);">偏差: <span id="up-deviation">--</span></div>
          <div style="color: var(--text-muted);">动量: <span id="up-momentum">--</span></div>
        </div>
      </div>
      
      <div class="card">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem;">
          <h2><i class="fas fa-arrow-down" style="color: var(--red);"></i> DOWN 市场</h2>
          <div style="display: flex; align-items: center; gap: 0.4rem;">
            <i class="fas fa-chart-line" style="font-size: 0.7rem; color: var(--text-muted);"></i>
            <span id="down-status" style="font-size: 0.7rem; color: var(--text-muted);">--</span>
          </div>
        </div>
        <div id="down" class="mono"></div>
        <div class="divider"></div>
        <div id="down-indicators" style="margin-top: 0.75rem; display: grid; grid-template-columns: 1fr 1fr; gap: 0.5rem; font-size: 0.75rem;">
          <div style="color: var(--text-muted);">偏差: <span id="down-deviation">--</span></div>
          <div style="color: var(--text-muted);">动量: <span id="down-momentum">--</span></div>
        </div>
      </div>
      
      <div class="card btc">
        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 0.5rem;">
          <h2><i class="fab fa-bitcoin"></i> BTC / USD (Chainlink)</h2>
          <div style="display: flex; align-items: center; gap: 0.4rem;">
            <i class="fas fa-wifi" style="font-size: 0.7rem; color: var(--text-muted);"></i>
            <span id="btc-status" style="font-size: 0.7rem; color: var(--text-muted);">--</span>
          </div>
        </div>
        <div id="btc" class="mono"></div>
        <div class="divider"></div>
        <div style="margin-top: 0.75rem; display: flex; align-items: center; gap: 0.5rem;">
          <div style="font-size: 0.75rem; color: var(--text-muted);">
            <i class="fas fa-history" style="margin-right: 0.3rem;"></i>
            数据延迟: <span id="btc-fresh">--</span>
          </div>
        </div>
      </div>
      
      <div class="card">
        <h2><i class="fas fa-chart-line"></i> 交易状态</h2>
        <div id="trading" class="mono"></div>
        <div class="divider"></div>
        <div style="margin-top: 0.75rem; display: flex; align-items: center; gap: 0.75rem;">
          <div style="display: flex; align-items: center; gap: 0.3rem; font-size: 0.8rem;">
            <i class="fas fa-check-circle" style="color: var(--green);"></i>
            <span>胜: <span id="trading-wins">--</span></span>
          </div>
          <div style="display: flex; align-items: center; gap: 0.3rem; font-size: 0.8rem;">
            <i class="fas fa-times-circle" style="color: var(--red);"></i>
            <span>负: <span id="trading-losses">--</span></span>
          </div>
          <div style="display: flex; align-items: center; gap: 0.3rem; font-size: 0.8rem;">
            <i class="fas fa-coins" style="color: var(--yellow);"></i>
            <span>盈亏: <span id="trading-pnl">--</span></span>
          </div>
        </div>
      </div>
    </div>
  </div>

  <!-- Tab 2: 交易记录 -->
  <div class="tab-content" id="tab-trades">
    <div class="stat-grid" id="trades-summary"></div>
    <div style="overflow-x: auto; border-radius: 10px;">
      <table id="trades-table">
        <thead>
          <tr>
            <th><i class="fas fa-hashtag"></i> #</th>
            <th><i class="fas fa-clock"></i> 时间</th>
            <th><i class="fas fa-store"></i> 市场</th>
            <th><i class="fas fa-directions"></i> 方向</th>
            <th><i class="fas fa-sign-in-alt"></i> 入场价</th>
            <th><i class="fas fa-sign-out-alt"></i> 出场价</th>
            <th><i class="fas fa-layer-group"></i> 张数</th>
            <th><i class="fas fa-money-bill-wave"></i> 盈亏</th>
            <th><i class="fas fa-trophy"></i> 结果</th>
            <th><i class="fas fa-shield-alt"></i> 对冲</th>
            <th><i class="fas fa-receipt"></i> 订单号</th>
          </tr>
        </thead>
        <tbody>
          <tr><td colspan="11" class="loading">加载交易记录...</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- Tab 3: 余额变动 -->
  <div class="tab-content" id="tab-balance">
    <div class="stat-grid" id="balance-summary"></div>
    <div style="overflow-x: auto; border-radius: 10px;">
      <table id="balance-table">
        <thead>
          <tr>
            <th><i class="fas fa-clock"></i> 时间</th>
            <th><i class="fas fa-wallet"></i> 资金</th>
            <th><i class="fas fa-chart-line"></i> 已实现盈亏</th>
            <th><i class="fas fa-exchange-alt"></i> 交易数</th>
            <th><i class="fas fa-balance-scale"></i> 胜/负</th>
            <th><i class="fas fa-percentage"></i> 胜率</th>
            <th><i class="fas fa-cogs"></i> 模式</th>
          </tr>
        </thead>
        <tbody>
          <tr><td colspan="7" class="loading">加载余额变动记录...</td></tr>
        </tbody>
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

  <footer>
    <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 1rem;">
      <div style="display: flex; align-items: center; gap: 1rem; flex-wrap: wrap;">
        <div style="display: flex; align-items: center; gap: 0.5rem; font-size: 0.85rem;">
          <div style="display: flex; align-items: center; gap: 0.3rem;">
            <div id="connection-status" style="width: 8px; height: 8px; border-radius: 50%; background-color: var(--green);"></div>
            <span>连接状态: <span id="connection-text">在线</span></span>
          </div>
          <span style="color: var(--text-muted);">|</span>
          <div style="display: flex; align-items: center; gap: 0.3rem;">
            <i class="fas fa-sync-alt fa-spin" style="color: var(--blue);"></i>
            <span>自动刷新: 5秒</span>
          </div>
          <span style="color: var(--text-muted);">|</span>
          <div style="display: flex; align-items: center; gap: 0.3rem;">
            <i class="fas fa-clock"></i>
            <span id="last-update">最后更新: 刚刚</span>
          </div>
        </div>
      </div>
      <div style="display: flex; align-items: center; gap: 0.75rem;">
        <div id="system-info" style="font-size: 0.8rem; color: var(--text-muted);">
          <span>BTC实时交易机器人 · v1.0</span>
        </div>
        <div>
          <span id="err" style="color: var(--text-muted); font-size: 0.8rem;"></span>
        </div>
      </div>
    </div>
    <div style="margin-top: 0.75rem; text-align: center; color: var(--text-muted); font-size: 0.75rem; opacity: 0.7;">
      <span>© 2024 BTC交易机器人 · 高性能交易系统</span>
    </div>
  </footer>
  
  <div class="toast" id="toast"></div>

  <script>
    /* === 工具函数 ============================= */
    function esc(s){if(s===null||s===undefined)return"";var el=document.createElement("div");el.textContent=String(s);return el.innerHTML;}
    function numFmt(n,dec){if(n===null||n===undefined||typeof n!=="number"||isNaN(n))return"\u2014";return n.toFixed(dec);}
    function sigClass(t){if(!t)return"wait";if(t.indexOf("BUY")>=0)return"buy";if(t.indexOf("NO ENTRY")>=0)return"block";return"wait";}
    function chk(x){return x===true?"\u2713":x===false?"\u2717":"\u2014";}
    /* 中国时区 (UTC+8) 格式化 */
    function tzFmt(ts){if(!ts)return"\u2014";var d=new Date(ts*1000);return d.toLocaleString("zh-CN",{timeZone:"Asia/Shanghai",year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false});}
    function tzShort(ts){if(!ts)return"\u2014";var d=new Date(ts*1000);return d.toLocaleString("zh-CN",{timeZone:"Asia/Shanghai",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false});}
    /* 兼容旧版（用于可能不支持 toLocaleString 的环境） */
    function tsFmt(ts){if(!ts)return"\u2014";return tzFmt(ts);}
    function tsShort(ts){if(!ts)return"\u2014";return tzShort(ts);}
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

    function reloadAll(){
      tick();
      loadTrades();
      loadBalance();
      loadStats();
      var btn=document.querySelector("button[onclick='reloadAll()']");
      if(btn){btn.textContent="✅ 已刷新";btn.style.background="var(--green)";setTimeout(function(){btn.textContent="🔄 重新开始";btn.style.background="var(--blue)";},1500);}
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
          
          // 更新状态指示器
          var statusBadge=document.getElementById("status-badge");
          var hdr=d.header||{};
          if(statusBadge){
            if(hdr.ws_connected){
              statusBadge.innerHTML='<i class="fas fa-circle" style="color:#22c55e;font-size:0.6rem;"></i><span>已连接</span>';
              statusBadge.classList.remove("offline");
            }else{
              statusBadge.innerHTML='<i class="fas fa-circle" style="color:#ef4444;font-size:0.6rem;"></i><span>已断开</span>';
              statusBadge.classList.add("offline");
            }
          }
          
          // 更新页脚连接状态
          var connectionStatus=document.getElementById("connection-status");
          var connectionText=document.getElementById("connection-text");
          if(connectionStatus&&connectionText){
            if(hdr.ws_connected){
              connectionStatus.style.backgroundColor="var(--green)";
              connectionText.textContent="在线";
              connectionText.style.color="var(--green)";
            }else{
              connectionStatus.style.backgroundColor="var(--red)";
              connectionText.textContent="离线";
              connectionText.style.color="var(--red)";
            }
          }
          
          var slug=hdr.slug!=null?String(hdr.slug):"\u2014";
          var ts="";
          if(d.ts)ts=tzFmt(d.ts);
          
          // 更新元信息
          var metaEl=document.getElementById("meta");
          if(metaEl){
            metaEl.querySelector("#market-info").textContent=esc(slug)+(hdr.simulation?" · 模拟模式":" · 实盘模式");
          }
          
          // 更新市场链接
          var marketLink=document.getElementById("market-link");
          if(marketLink&&hdr.slug){
            marketLink.href="https://polymarket.com/event/"+esc(slug);
          }
          
          // 更新最后刷新时间
          var lastUpdateEl=document.getElementById("last-update");
          if(lastUpdateEl){
            lastUpdateEl.innerHTML='<i class="fas fa-clock"></i> 最后更新: '+new Date().toLocaleTimeString("zh-CN",{hour12:false});
          }
          
          // 更新会话状态
          var sessionHtml=[
            "计时器: "+(hdr.time_left_sec!=null?esc(Math.floor(hdr.time_left_sec)+"秒剩余"):"\u2014"),
            "WS: "+(hdr.ws_connected?"已连接":"已断开"),
            "模式: "+(hdr.simulation?"模拟":"实盘"),
          ].join("<br/>");
          document.getElementById("session").innerHTML=sessionHtml;
          
          // 更新计时器显示
          var timerDisplay=document.getElementById("timer-display");
          if(timerDisplay&&hdr.time_left_sec!=null){
            var mins=Math.floor(hdr.time_left_sec/60);
            var secs=Math.floor(hdr.time_left_sec%60);
            timerDisplay.textContent="计时器: "+(mins>0?mins+"分":"")+secs+"秒剩余";
          }
          
          var st=d.strategy||{};
          var sig=st.signal_text||"\u2014";
          var ck=st.checks||{};
          
          // 更新策略显示
          document.getElementById("strategy").innerHTML=
            '<div class="sig '+sigClass(sig)+'">'+esc(sig)+"</div>"+
            '<div class="mono" style="margin-top:0.4rem">'+
            "偏好: "+esc(st.favorite)+" · 胜率: "+esc(st.win_rate_str)+"<br/>"+
            "检查: P="+chk(ck.price)+" T="+chk(ck.time)+" D="+chk(ck.dev)+
            " M="+chk(ck.mom)+" 截止="+chk(ck.time_cutoff)+
            "</div>";
          
          // 更新策略检查指示器
          var checksEl=document.getElementById("strategy-checks");
          if(checksEl){
            var checksHtml='';
            var checkItems=[
              {key:'price',label:'价格检查',icon:'fa-dollar-sign'},
              {key:'time',label:'时间检查',icon:'fa-clock'},
              {key:'dev',label:'偏差检查',icon:'fa-chart-bar'},
              {key:'mom',label:'动量检查',icon:'fa-chart-line'},
              {key:'time_cutoff',label:'截止检查',icon:'fa-hourglass-end'}
            ];
            checkItems.forEach(function(item){
              var checkValue=ck[item.key];
              var color=checkValue===true?'var(--green)':checkValue===false?'var(--red)':'var(--text-muted)';
              checksHtml+='<div style="display:flex;align-items:center;gap:0.3rem;font-size:0.75rem;">'+
                '<i class="fas '+item.icon+'" style="color:'+color+';font-size:0.7rem;"></i>'+
                '<span style="color:var(--text-muted);">'+item.label+':</span>'+
                '<span style="color:'+color+';font-weight:bold;">'+chk(checkValue)+'</span>'+
                '</div>';
            });
            checksEl.innerHTML=checksHtml;
          }
          
          function book(x,id){
            var el=document.getElementById(id);
            if(!x){el.textContent="无数据";return;}
            var bk=x.book||{};
            var ind=x.indicators||{};
            el.innerHTML=[
              "最新 "+esc(bk.last_price),
              "买价 "+esc(bk.best_bid)+" / 卖价 "+esc(bk.best_ask),
              "VWAP "+numFmt(ind.vwap,4)+" · 偏差 "+(ind.deviation_pct!=null?numFmt(ind.deviation_pct,2)+"%":"\u2014"),
              "Z "+numFmt(ind.zscore,2)+" · 动量 "+(ind.momentum_pct!=null?numFmt(ind.momentum_pct,2)+"%":"\u2014"),
              "成交量 "+(bk.volume_total!=null?esc(Math.round(bk.volume_total)):"\u2014"),
            ].join("<br/>");
            
            // 更新状态指示器
            var statusEl=document.getElementById(id+"-status");
            if(statusEl){
              if(bk.last_price&&bk.best_bid&&bk.best_ask){
                statusEl.textContent="在线";
                statusEl.style.color="var(--green)";
              }else{
                statusEl.textContent="离线";
                statusEl.style.color="var(--red)";
              }
            }
            
            // 更新指标显示
            var deviationEl=document.getElementById(id+"-deviation");
            var momentumEl=document.getElementById(id+"-momentum");
            if(deviationEl){
              deviationEl.textContent=ind.deviation_pct!=null?numFmt(ind.deviation_pct,2)+"%":"--";
              deviationEl.style.color=ind.deviation_pct!=null&&Math.abs(ind.deviation_pct)>2?(ind.deviation_pct>0?"var(--green)":"var(--red)"):"var(--text-muted)";
            }
            if(momentumEl){
              momentumEl.textContent=ind.momentum_pct!=null?numFmt(ind.momentum_pct,2)+"%":"--";
              momentumEl.style.color=ind.momentum_pct!=null&&Math.abs(ind.momentum_pct)>1?(ind.momentum_pct>0?"var(--green)":"var(--red)"):"var(--text-muted)";
            }
          }
          
          book(d.up,"up");
          book(d.down,"down");
          
          var b=d.btc||{};
          var btcEl=document.getElementById("btc");
          if(b.btc_current_price>0){
            btcEl.innerHTML=[
              "$"+esc(numFmt(b.btc_current_price,2)),
              "锚定 $"+(b.btc_anchor_price>0?esc(numFmt(b.btc_anchor_price,2)):"\u2014"),
              esc(b.deviation_line||""),
              "数据源: "+(b.btc_connected?"正常":"离线")+(b.fresh_sec!=null?" · "+Math.floor(b.fresh_sec)+"s":""),
            ].join("<br/>");
            
            // 更新BTC状态
            var btcStatus=document.getElementById("btc-status");
            if(btcStatus){
              btcStatus.textContent=b.btc_connected?"正常":"离线";
              btcStatus.style.color=b.btc_connected?"var(--green)":"var(--red)";
            }
            
            // 更新新鲜度显示
            var btcFresh=document.getElementById("btc-fresh");
            if(btcFresh){
              btcFresh.textContent=b.fresh_sec!=null?Math.floor(b.fresh_sec)+"s":"--";
              btcFresh.style.color=b.fresh_sec!=null&&b.fresh_sec<10?"var(--green)":b.fresh_sec!=null&&b.fresh_sec<30?"var(--yellow)":"var(--red)";
            }
          }else{
            btcEl.textContent="等待 Chainlink 数据…";
            var btcStatus=document.getElementById("btc-status");
            if(btcStatus){
              btcStatus.textContent="离线";
              btcStatus.style.color="var(--red)";
            }
          }
          
          var tr=d.trading||{};
          var tHtml="市场数 "+esc(tr.markets_seen)+
            " · 交易数 "+esc(tr.total_trades!=null?tr.total_trades:tr.trade_count)+
            " · 胜 "+esc(tr.wins)+" / 负 "+esc(tr.losses)+
            " · 盈亏 $"+(tr.total_pnl!=null?numFmt(tr.total_pnl,2):"\u2014")+"<br/>";
          if(tr.account){
            tHtml+="资金: $"+numFmt(tr.account.current_capital,2)+
              " (初始 $"+numFmt(tr.account.initial_capital,0)+")"+
              " · 已实现 $"+(tr.account.realized_pnl!=null?numFmt(tr.account.realized_pnl,2):"0")+"<br/>";
          }
          if(tr.position){
            var p=tr.position;
            tHtml+="做多 "+esc(p.token_name)+" @ "+esc(p.entry_price)+
              " ×"+esc(p.contracts)+(p.hedged?" 已对冲":"")+"<br/>";
            tHtml+="未实现 $"+(p.unrealized_pnl!=null?numFmt(p.unrealized_pnl,2):"\u2014")+"<br/>";
          }else{tHtml+="无持仓<br/>";}
          if(tr.recent_trades&&tr.recent_trades.length){
            var lines=[];
            for(var i=0;i<tr.recent_trades.length;i++){lines.push(esc(tr.recent_trades[i].line));}
            tHtml+="<br/>最近:<br/>"+lines.join("<br/>");
          }
          document.getElementById("trading").innerHTML=tHtml;
          
          // 更新交易统计
          var winsEl=document.getElementById("trading-wins");
          var lossesEl=document.getElementById("trading-losses");
          var pnlEl=document.getElementById("trading-pnl");
          if(winsEl) winsEl.textContent=esc(tr.wins||0);
          if(lossesEl) lossesEl.textContent=esc(tr.losses||0);
          if(pnlEl){
            var pnl=tr.total_pnl;
            if(pnl!=null){
              pnlEl.textContent="$"+numFmt(pnl,2);
              pnlEl.style.color=pnl>=0?"var(--green)":"var(--red)";
            }else{
              pnlEl.textContent="--";
            }
          }
          
          // 更新快速统计
          updateQuickStats(d);
        }catch(e){errEl.textContent="轮询错误: "+(e&&e.message?e.message:e);}
      };
      r.onerror=function(){errEl.textContent="网络错误（机器人是否在运行？）";};
      r.send();
    }
    
    function updateQuickStats(data){
      var tr=data.trading||{};
      var statsTrades=document.getElementById("stats-trades");
      var statsWins=document.getElementById("stats-wins");
      var statsPnl=document.getElementById("stats-pnl");
      var statsUptime=document.getElementById("stats-uptime");
      
      if(statsTrades){
        var totalTrades=tr.total_trades||tr.trade_count||0;
        statsTrades.textContent=totalTrades;
        statsTrades.classList.add("count-up");
      }
      
      if(statsWins){
        var wins=tr.wins||0;
        var losses=tr.losses||0;
        var total=wins+losses;
        var winRate=total>0?Math.round((wins/total)*100):0;
        statsWins.textContent=winRate+"%";
        statsWins.style.color=winRate>=50?"var(--green)":winRate>=30?"var(--yellow)":"var(--red)";
        statsWins.classList.add("count-up");
      }
      
      if(statsPnl){
        var pnl=tr.total_pnl;
        if(pnl!=null){
          statsPnl.textContent=(pnl>=0?"+":"")+"$"+numFmt(pnl,2);
          statsPnl.style.color=pnl>=0?"var(--green)":"var(--red)";
          statsPnl.classList.add("count-up");
        }else{
          statsPnl.textContent="--";
        }
      }
      
      if(statsUptime){
        var hdr=data.header||{};
        if(hdr.start_time){
          var uptime=Math.floor((Date.now()/1000)-(hdr.start_time||0));
          var hours=Math.floor(uptime/3600);
          var minutes=Math.floor((uptime%3600)/60);
          var seconds=uptime%60;
          statsUptime.textContent=(hours>0?hours+"h ":"")+(minutes>0?minutes+"m ":"")+seconds+"s";
          statsUptime.classList.add("count-up");
        }else{
          statsUptime.textContent="--";
        }
      }
      
      // 移除动画类以便下次重新添加
      setTimeout(function(){
        var elements=document.querySelectorAll(".count-up");
        elements.forEach(function(el){
          el.classList.remove("count-up");
        });
      },1000);
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
