(function () {
  const summaryEl = document.getElementById("summary-stats");
  const coinsEl = document.getElementById("coins-container");
  const tbody = document.querySelector("#recent-trades tbody");
  const badge = document.getElementById("conn-badge");
  const configEditor = document.getElementById("config-editor");
  const configMsg = document.getElementById("config-message");
  const headerSubtitle = document.getElementById("header-subtitle");

  function labelFromIntervalSec(sec) {
    if (sec == null || Number.isNaN(sec)) return null;
    if (sec === 300) return "5m";
    if (sec === 900) return "15m";
    return `${sec}s`;
  }

  function updateHeaderSubtitle(data) {
    if (!headerSubtitle) return;
    let ml = data.market_label;
    if (!ml && data.market_interval_sec != null) {
      ml = labelFromIntervalSec(data.market_interval_sec);
    }
    const part = ml ? `${ml} ` : "";
    headerSubtitle.textContent = `Polymarket ${part}控制台 · 实时状态 · 设置 · 分析`;
  }

  function updateHeaderSubtitleFromConfig(cfg) {
    if (!headerSubtitle || !cfg || typeof cfg !== "object") return;
    const pm = cfg.data_sources && cfg.data_sources.polymarket;
    if (!pm) return;
    let ml = pm.market_window;
    if (!ml && pm.market_interval_sec != null) {
      ml = labelFromIntervalSec(pm.market_interval_sec);
    }
    if (ml) {
      headerSubtitle.textContent = `Polymarket ${ml} 控制台 · 实时状态 · 设置 · 分析`;
    }
  }

  function fmtTime(sec) {
    sec = Math.floor(sec || 0);
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    if (h > 0) return `${h}h ${m}m ${s}s`;
    return `${m}m ${s}s`;
  }

  function fmtUsd(n) {
    if (n == null || Number.isNaN(n)) return "—";
    const sign = n >= 0 ? "+" : "";
    return sign + "$" + Number(n).toFixed(2);
  }

  function renderSummary(data) {
    if (!summaryEl) return;
    const p = data.portfolio || {};
    const dry = data.dry_run;
    summaryEl.innerHTML = [
      card("运行时间", fmtTime(data.uptime_sec)),
      card("市场", data.market_label || "—"),
      card("模式", dry ? "模拟运行" : "实盘", dry ? "warn" : "ok"),
      card("钱包", data.wallet_balance != null ? "$" + data.wallet_balance.toFixed(2) : "—"),
      card("总盈亏", fmtUsd(p.total_pnl), (p.total_pnl || 0) >= 0 ? "pos" : "neg"),
      card("交易数", String(p.total_trades ?? "0")),
      card("收益率", (p.portfolio_roi != null ? p.portfolio_roi.toFixed(2) + "%" : "—")),
    ].join("");
  }

  function card(label, value, valClass) {
    const vc = valClass ? ` ${valClass}` : "";
    return `<div class="stat-card"><div class="label">${label}</div><div class="value${vc}">${escapeHtml(
      String(value)
    )}</div></div>`;
  }

  function escapeHtml(s) {
    const d = document.createElement("div");
    d.textContent = s;
    return d.innerHTML;
  }

  function renderCoins(data) {
    if (!coinsEl) return;
    const coins = data.coins || {};
    const names = ["btc", "eth", "sol"];
    coinsEl.innerHTML = names
      .map((c) => {
        const x = coins[c];
        if (!x) return "";
        const en = x.trading_enabled !== false;
        const fav = x.favorite || "—";
        const conf = x.confidence != null ? x.confidence.toFixed(3) : "—";
        const slugShort = (x.market_slug || "").split("-").pop() || "—";
        const st = x.stats || {};
        let posHtml = '<div class="row"><span>持仓</span><strong>无</strong></div>';
        if (x.position) {
          const p = x.position;
          posHtml = `
          <div class="pos-block">
            <div class="row"><span>未实现</span><strong class="${p.unrealized_pnl >= 0 ? "pnl-pos" : "pnl-neg"}">${fmtUsd(
            p.unrealized_pnl
          )}</strong></div>
            <div class="row"><span>已投入</span><strong>$${p.total_invested}</strong></div>
            <div class="row"><span>方向 / 次数</span><strong>${p.our_side} · ${p.entries_count}</strong></div>
            <div class="row"><span>UP 胜</span><strong>${fmtUsd(p.if_up_wins)}</strong></div>
            <div class="row"><span>DOWN 胜</span><strong>${fmtUsd(p.if_down_wins)}</strong></div>
          </div>`;
        }
        return `
        <div class="coin-card">
          <h3>${c.toUpperCase()}
            ${en ? "" : '<span class="disabled-tag">已禁用</span>'}
          </h3>
          <div class="row"><span>市场</span><strong>${escapeHtml(slugShort)}</strong></div>
          <div class="row"><span>剩余时间</span><strong>${fmtTime(x.seconds_till_end)}</strong></div>
          <div class="row"><span>UP / DN 卖价</span><strong>${x.up_ask?.toFixed(3) ?? "—"} / ${x.down_ask?.toFixed(
            3
          ) ?? "—"}</strong></div>
          <div class="row"><span>偏好 · 置信度</span><strong>${fav} · ${conf}</strong></div>
          <div class="row"><span>盈亏(币)</span><strong class="${(st.pnl || 0) >= 0 ? "pnl-pos" : "pnl-neg"}">${fmtUsd(
            st.pnl
          )}</strong></div>
          <div class="row"><span>胜/负 · 胜率</span><strong>${st.wins ?? 0}/${st.losses ?? 0} · ${st.win_rate ?? 0}%</strong></div>
          ${posHtml}
        </div>`;
      })
      .join("");
  }

  function renderRecent(data) {
    if (!tbody) return;
    const rows = data.recent_trades || [];
    tbody.innerHTML = rows
      .map((t) => {
        const pnl = t.pnl;
        const cls = pnl >= 0 ? "pnl-pos" : "pnl-neg";
        const m = (t.market_slug || "").split("-").pop() || t.market_slug;
        const side = t.side || "—";
        const exitType = t.exit_type || "—";
        const roi = t.roi_display != null ? (t.roi_display >= 0 ? "+" : "") + t.roi_display.toFixed(2) : "—";
        const closeTime = t.close_time ? String(t.close_time).slice(0, 19).replace("T", " ") : "—";
        const url = t.polymarket_url || "";
        const linkHtml = url ? `<a href="${url}" target="_blank" rel="noopener" class="poly-link">查看</a>` : "—";
        return `<tr>
        <td>${escapeHtml(closeTime)}</td>
        <td>${escapeHtml(m || "")}</td>
        <td>${escapeHtml(side)}</td>
        <td class="${cls}"><strong>${pnl >= 0 ? "+" : ""}${pnl.toFixed(2)}</strong></td>
        <td class="${cls}">${roi}</td>
        <td>${escapeHtml(exitType)}</td>
        <td>${escapeHtml(t.winner != null ? String(t.winner) : "—")}</td>
        <td>${linkHtml}</td>
      </tr>`;
      })
      .join("");
    if (!rows.length) {
      tbody.innerHTML = '<tr><td colspan="8">本会话暂无已平仓交易</td></tr>';
    }
  }

  async function fetchStatus() {
    const r = await fetch("/api/status", { cache: "no-store" });
    if (!r.ok) throw new Error("status " + r.status);
    return r.json();
  }

  async function tick() {
    try {
      const data = await fetchStatus();
      updateHeaderSubtitle(data);
      renderSummary(data);
      renderCoins(data);
      renderRecent(data);
      const health = await fetch("/api/health", { cache: "no-store" }).then((x) => x.json());
      if (health.bot_live) {
        badge.textContent = "运行中";
        badge.className = "badge badge-ok";
      } else {
        badge.textContent = "机器人未运行";
        badge.className = "badge badge-off";
      }
    } catch (e) {
      badge.textContent = "已断开";
      badge.className = "badge badge-warn";
    }
  }

  async function loadConfig() {
    if (!configEditor || !configMsg) return;
    configMsg.textContent = "";
    configMsg.className = "message";
    try {
      const r = await fetch("/api/config");
      const j = await r.json();
      if (j.error) throw new Error(j.error);
      configEditor.value = JSON.stringify(j, null, 2);
      updateHeaderSubtitleFromConfig(j);
      configMsg.textContent = "已加载。";
      configMsg.className = "message ok";
    } catch (e) {
      configMsg.textContent = String(e.message || e);
      configMsg.className = "message err";
    }
  }

  document.getElementById("btn-refresh")?.addEventListener("click", tick);
  document.getElementById("btn-load-config")?.addEventListener("click", loadConfig);
  document.getElementById("btn-save-config")?.addEventListener("click", async () => {
    if (!configEditor || !configMsg) return;
    configMsg.textContent = "";
    try {
      const parsed = JSON.parse(configEditor.value);
      const r = await fetch("/api/config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(parsed),
      });
      const j = await r.json();
      if (!r.ok) throw new Error(j.error || "save failed");
      configMsg.textContent = j.message || "已保存。";
      configMsg.className = "message ok";
    } catch (e) {
      configMsg.textContent = String(e.message || e);
      configMsg.className = "message err";
    }
  });

  document.getElementById("btn-stop")?.addEventListener("click", async () => {
    if (!confirm("请求正常停止？机器人将退出（等同于 Ctrl+C）。")) return;
    try {
      const r = await fetch("/api/bot/stop", { method: "POST" });
      const j = await r.json();
      alert(j.message || "OK");
    } catch (e) {
      alert(e);
    }
  });

  loadConfig();
  tick();
  setInterval(tick, 1200);
})();
