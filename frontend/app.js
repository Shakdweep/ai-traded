/* AI Traded – dashboard frontend. Vanilla JS + TradingView lightweight-charts.
   Talks only to this app's own /api; no keys or secrets ever reach the browser. */
"use strict";

const $ = (s, el = document) => el.querySelector(s);
const app = $("#app");
const IST_OFFSET = 19800; // charts render UTC; shift so the axis reads IST
const state = { charts: [], timers: [], scanCache: {}, settings: null };

// ------------------------------------------------------------------ helpers
async function api(path, opts = {}) {
  const r = await fetch("/api" + path, {
    headers: { "content-type": "application/json" }, ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await r.json(); } catch { data = null; }
  if (r.status === 401 && data && data.login) { location.href = "/login"; throw new Error("Please log in."); }
  if (!r.ok) throw new Error((data && (data.detail || data.error)) || ("HTTP " + r.status));
  return data;
}
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (v, d = 2) => (v === null || v === undefined || Number.isNaN(v)) ? "–" : Number(v).toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d });
const inr = (v, d = 0) => (v === null || v === undefined) ? "–" : "₹" + fmt(v, d);
const pct = (v, d = 2) => (v === null || v === undefined) ? "–" : (v > 0 ? "+" : "") + fmt(v, d) + "%";
const cls = (v) => v > 0 ? "up" : v < 0 ? "down" : "flat";
const vol = (v) => { if (!v) return "–"; if (v >= 1e7) return fmt(v / 1e7, 2) + " Cr"; if (v >= 1e5) return fmt(v / 1e5, 2) + " L"; return fmt(v, 0); };
const ago = (ts) => { if (!ts) return "never"; const m = (Date.now() / 1000 - ts) / 60; return m < 1 ? "just now" : m < 60 ? Math.round(m) + " min ago" : m < 1440 ? Math.round(m / 60) + " h ago" : Math.round(m / 1440) + " d ago"; };
const istTime = (ts) => new Date(ts * 1000).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });
function ratingBadge(r, big) {
  const m = { "STRONG BULLISH": ["r-strong", "🟢"], "BULLISH": ["r-bull", "🟢"], "WAIT": ["r-wait", "🟡"], "HIGH RISK": ["r-risk", "🟠"], "BEARISH": ["r-bear", "🔴"], "AVOID": ["r-avoid", "🔴"] }[r] || ["r-na", "⚪"];
  return `<span class="rating ${m[0]} ${big ? "big" : ""}">${m[1]} ${esc(r || "N/A")}</span>`;
}
function freshBadge(f) {
  if (!f) return "";
  const t = f.as_of ? `As of ${new Date(f.as_of).toLocaleString("en-IN", { timeZone: "Asia/Kolkata" })} IST · ${f.source}` : f.source;
  return `<span class="fresh f-${esc(f.label)}" title="${esc(t)}">${esc(f.label)}${f.age_min != null && f.label !== "CLOSED" ? " · " + Math.round(f.age_min) + "m" : ""}</span>`;
}
function setupTags(labels, keys) {
  return (labels || []).map((l, i) => {
    const k = (keys || [])[i] || "";
    const bear = /breakdown|bear|death|loss|reversal_down|below/.test(k);
    const bull = /breakout|bull|golden|reclaim|momentum|bounce|reversal_up|near/.test(k);
    return `<span class="tag ${bear ? "s" : bull ? "b" : "w"}">${esc(l)}</span>`;
  }).join("");
}
function toast(msg, ms = 4000) {
  const t = document.createElement("div"); t.className = "toast"; t.innerHTML = msg; document.body.appendChild(t);
  setTimeout(() => t.remove(), ms);
}
function clearView() {
  state.charts.forEach((c) => { try { c.remove(); } catch { } });
  state.charts = []; state.timers.forEach(clearInterval); state.timers = [];
}
function every(ms, fn) { state.timers.push(setInterval(fn, ms)); }
function errorBox(e) { return `<div class="empty">⚠️ ${esc(e.message || e)}</div>`; }
function cssVar(n) { return getComputedStyle(document.documentElement).getPropertyValue(n).trim(); }
const disclaimer = `<div class="disclaimer">Analysis and paper trading only. Prices come from Yahoo Finance (unofficial, typically delayed), never labelled live. Ratings are transparent rules applied to that data: they are not predictions and not investment advice. Check anything that matters against your broker before acting.</div>`;

// ------------------------------------------------------------------ charts
function mkChart(el, height) {
  const c = LightweightCharts.createChart(el, {
    height: height || el.clientHeight || 400, autoSize: true,
    layout: { background: { type: "solid", color: "transparent" }, textColor: cssVar("--muted"), fontFamily: "Inter, sans-serif", attributionLogo: false },
    grid: { vertLines: { color: cssVar("--line") }, horzLines: { color: cssVar("--line") } },
    rightPriceScale: { borderColor: cssVar("--line2") }, timeScale: { borderColor: cssVar("--line2"), timeVisible: true },
    crosshair: { mode: 0 },
  });
  state.charts.push(c);
  return c;
}
const shift = (arr) => (arr || []).map((p) => ({ ...p, time: p.time + IST_OFFSET }));
function candleChart(el, data, opts = {}) {
  const c = mkChart(el);
  const s = c.addCandlestickSeries({ upColor: cssVar("--bull"), downColor: cssVar("--bear"), borderVisible: false, wickUpColor: cssVar("--bull"), wickDownColor: cssVar("--bear") });
  s.setData(shift(data.candles));
  const v = c.addHistogramSeries({ priceFormat: { type: "volume" }, priceScaleId: "vol" });
  c.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
  v.setData(shift(data.candles.map((b) => ({ time: b.time, value: b.volume, color: b.close >= b.open ? "rgba(34,197,94,.35)" : "rgba(239,68,68,.35)" }))));
  const lines = [["ema20", "#38bdf8", "EMA 20"], ["ema50", "#eab308", "EMA 50"], ["ema200", "#a78bfa", "EMA 200"], ["vwap", "#f97316", "VWAP"], ["vwap20", "#f97316", "VWAP 20d"]];
  for (const [k, col, title] of lines) {
    if (data[k] && data[k].length && (!opts.only || opts.only.includes(k))) {
      const l = c.addLineSeries({ color: col, lineWidth: 1.5, title, priceLineVisible: false, lastValueVisible: false });
      l.setData(shift(data[k]));
    }
  }
  (opts.levels || []).forEach((lv) => s.createPriceLine({ price: lv.price, color: lv.color, lineWidth: 1, lineStyle: 2, axisLabelVisible: true, title: lv.title }));
  if (opts.markers) s.setMarkers(opts.markers);
  if (opts.visible) c.timeScale().setVisibleLogicalRange({ from: data.candles.length - opts.visible, to: data.candles.length + 3 });
  else c.timeScale().fitContent();
  return c;
}
function lineChart(el, data, color, opts = {}) {
  const c = mkChart(el, opts.height);
  c.applyOptions({ timeScale: { visible: opts.axis !== false }, rightPriceScale: { visible: opts.axis !== false }, grid: { vertLines: { visible: false }, horzLines: { visible: opts.axis !== false } }, handleScroll: !opts.static, handleScale: !opts.static });
  const s = c.addAreaSeries({ lineColor: color, topColor: color + "55", bottomColor: color + "00", lineWidth: 2, priceLineVisible: false, lastValueVisible: opts.axis !== false });
  s.setData(shift(data));
  c.timeScale().fitContent();
  return { chart: c, series: s };
}

// ------------------------------------------------------------------ header
async function refreshHeader() {
  try {
    const s = await api("/status");
    const m = s.market;
    $("#mkt-pill").innerHTML = `<span class="dot ${m.open ? "on" : m.state === "preopen" || m.state === "pre" ? "warn" : ""}"></span><span>NSE: ${esc(m.label)}</span>`;
    const p = s.providers.prices.stats;
    const last = Math.max(p.last_success || 0, ...s.scans.map((x) => x.finished || 0));
    const okRecently = last && Date.now() / 1000 - last < (m.open ? 1800 : 6 * 3600);
    $("#data-pill").innerHTML = `<span class="dot ${okRecently ? (m.open ? "warn" : "") : "off"}"></span>Data: ${m.open ? "DELAYED (Yahoo, unofficial)" : "last close"} · updated ${ago(last || null)}`;
    $("#data-pill").title = "Prices are never shown as live: Yahoo Finance is unofficial and typically delayed.";
    const a = await api("/alerts?unseen=true");
    $("#alert-count").innerHTML = a.alerts.length ? `<span class="badge-count">${a.alerts.length}</span>` : "";
  } catch (e) {
    $("#mkt-pill").innerHTML = `<span class="dot off"></span><span>Backend unreachable</span>`;
  }
}
function clock() { $("#clock").textContent = new Date().toLocaleTimeString("en-IN", { timeZone: "Asia/Kolkata", hour12: false }) + " IST"; }
setInterval(clock, 1000); clock();
setInterval(refreshHeader, 60000);

let searchT;
$("#search").addEventListener("input", (e) => {
  clearTimeout(searchT);
  const q = e.target.value.trim();
  const box = $("#search-results");
  if (!q) { box.hidden = true; return; }
  searchT = setTimeout(async () => {
    try {
      const r = await api("/search?q=" + encodeURIComponent(q));
      box.innerHTML = r.length ? r.map((x) => `<div data-s="${esc(x.symbol)}"><b>${esc(x.symbol)}</b> <span class="muted">${esc(x.name)}</span><br><span class="dim small">${esc(x.sector || "")} ${x.cap ? "· " + esc(x.cap) : ""}</span></div>`).join("") : `<div class="muted">No match</div>`;
      box.hidden = false;
    } catch (err) { box.innerHTML = `<div>${esc(err.message)}</div>`; box.hidden = false; }
  }, 250);
});
$("#search-results").addEventListener("click", (e) => {
  const d = e.target.closest("[data-s]"); if (!d) return;
  $("#search-results").hidden = true; $("#search").value = "";
  location.hash = "#/stock/" + d.dataset.s;
});
document.addEventListener("click", (e) => { if (!e.target.closest(".search")) $("#search-results").hidden = true; });
$("#theme-btn").addEventListener("click", () => {
  const cur = document.documentElement.dataset.theme === "light" ? "dark" : "light";
  document.documentElement.dataset.theme = cur;
  try { localStorage.setItem("theme", cur); } catch { }
  route();
});
try { const t = localStorage.getItem("theme"); if (t) document.documentElement.dataset.theme = t; } catch { }

// signed-in user + log out (only shown when the invite-only login is on)
api("/auth/me").then((d) => {
  if (!d || !d.auth_enabled || !d.user) return;
  const name = d.name || d.user;
  $("#user-name").textContent = name;
  $("#user-name").title = d.user;
  $("#user-av").textContent = name.slice(0, 1).toUpperCase();
  $("#user-box").hidden = false;
  if (d.owner) { $("#tab-members").hidden = false; $("#tab-admin").hidden = false; }
}).catch(() => { });
$("#logout-btn").addEventListener("click", async () => {
  try { await api("/auth/logout", { method: "POST" }); } catch { }
  location.href = "/";
});

// ------------------------------------------------------------------ shared tables
function scanTable(rows, opts = {}) {
  if (!rows || !rows.length) return `<div class="empty">${esc(opts.empty || "Nothing matches right now.")}</div>`;
  const intraday = opts.mode === "intraday";
  const head = intraday
    ? ["Stock", "Price", "Chg %", "Volume", "OR high", "OR low", "VWAP", "Trend", "Setups", "Rating", "Conf.", "Plan"]
    : ["Stock", "Price", "Chg %", "Volume", "Vol ×", "RSI", "MACD", "VWAP 20d", "EMA 20", "EMA 50", "EMA 200", "Support", "Resistance", "Trend", "Setups", "Rating", "Conf.", "Plan"];
  const num = new Set(["Price", "Chg %", "Volume", "Vol ×", "RSI", "MACD", "VWAP 20d", "VWAP", "EMA 20", "EMA 50", "EMA 200", "Support", "Resistance", "Conf.", "OR high", "OR low"]);
  const body = rows.map((r) => {
    const p = r.plan || {};
    const planTxt = p.actionable ? `<span class="tag b">ACTIONABLE</span> ${fmt(p.entry)} / SL ${fmt(p.stop)} / T1 ${fmt(p.target1)}`
      : `<span class="tag">${esc(p.status || "–")}</span>`;
    const common = `<td><b>${esc(r.symbol)}</b><div class="dim small">${esc((r.name || "").slice(0, 28))}</div></td>
      <td class="n">${fmt(r.price)} ${opts.fresh ? freshBadge(r.freshness) : ""}</td><td class="n ${cls(r.change_pct)}">${pct(r.change_pct)}</td><td class="n">${vol(r.volume)}</td>`;
    const mid = intraday
      ? `<td class="n">${fmt(r.or_high)}</td><td class="n">${fmt(r.or_low)}</td><td class="n">${fmt(r.vwap)}</td>`
      : `<td class="n">${fmt(r.vol_ratio, 1)}</td><td class="n">${fmt(r.rsi, 1)}</td><td class="n ${cls((r.macd ?? 0) - (r.macd_signal ?? 0))}">${fmt(r.macd)}</td><td class="n">${fmt(r.vwap)}</td>
         <td class="n">${fmt(r.ema20)}</td><td class="n">${fmt(r.ema50)}</td><td class="n">${fmt(r.ema200)}</td><td class="n">${fmt(r.support)}</td><td class="n">${fmt(r.resistance)}</td>`;
    return `<tr data-s="${esc(r.symbol)}" title="${esc((r.why || []).join(" | "))}">${common}${mid}<td>${esc(r.trend || "")}</td>
      <td style="white-space:normal;min-width:160px">${setupTags(r.setup_labels, r.setups)}</td><td>${ratingBadge(r.rating)}</td>
      <td class="n">${r.confidence ?? "–"}</td><td class="small">${planTxt}</td></tr>`;
  }).join("");
  return `<div class="tablewrap" style="max-height:${opts.maxh || 640}px"><table class="${opts.mini ? "mini" : ""}"><thead><tr>${head.map((h) => `<th class="${num.has(h) ? "n" : ""}">${h}</th>`).join("")}</tr></thead><tbody>${body}</tbody></table></div>`;
}
// click a column header to sort that table (numbers numerically, text alphabetically)
document.addEventListener("click", (e) => {
  const th = e.target.closest("th"); if (!th) return;
  const table = th.closest("table"); const tb = table && table.tBodies[0]; if (!tb) return;
  const i = [...th.parentNode.children].indexOf(th);
  const dir = th.dataset.dir === "asc" ? "desc" : "asc"; th.dataset.dir = dir;
  const val = (tr) => { const t = (tr.children[i]?.innerText || "").replace(/[₹,%×+\s]|L$|Cr$/g, "").trim(); const n = parseFloat(t); return Number.isNaN(n) ? t.toLowerCase() : n; };
  const rows = [...tb.rows].sort((a, b) => { const x = val(a), y = val(b); const c = typeof x === "number" && typeof y === "number" ? x - y : String(x).localeCompare(String(y)); return dir === "asc" ? c : -c; });
  rows.forEach((r) => tb.appendChild(r));
});

function bindRows(root, mode) {
  root.querySelectorAll("tr[data-s]").forEach((tr) => tr.addEventListener("click", () => { location.hash = `#/stock/${tr.dataset.s}${mode ? "?mode=" + mode : ""}`; }));
}
function compactList(rows, mode, kind) {
  if (!rows || !rows.length) return `<div class="empty small">${kind === "act" ? "No setup passes every check right now." : "None right now."}</div>`;
  return `<div class="tablewrap"><table class="mini"><thead><tr><th>Stock</th><th class="n">Price</th><th class="n">Chg</th><th>Rating</th><th class="n">Conf.</th><th>${kind === "vol" ? "Volume ×" : "Setup / reason"}</th></tr></thead><tbody>${rows.map((r) => `
    <tr data-s="${esc(r.symbol)}"><td><b>${esc(r.symbol)}</b></td><td class="n">${fmt(r.price)}</td><td class="n ${cls(r.change_pct)}">${pct(r.change_pct, 1)}</td><td>${ratingBadge(r.rating)}</td><td class="n">${r.confidence ?? "–"}</td>
    <td class="small" style="white-space:normal">${kind === "vol" ? fmt(r.vol_ratio, 1) + "×" : kind === "act" && r.plan ? `Entry ${fmt(r.plan.entry)} · SL ${fmt(r.plan.stop)} · T1 ${fmt(r.plan.target1)} · R:R ${fmt(r.plan.reward_risk, 1)}${r.backtest ? `<div class="dim">${esc(r.backtest.why)}</div>` : ""}` : esc((r.setup_labels || []).slice(0, 2).join(", ") || (r.why || [])[0] || "")}</td></tr>`).join("")}</tbody></table></div>`;
}
function newsList(items, limit = 50) {
  if (!items || !items.length) return `<div class="empty small">No news in the time window (older items are never shown as current).</div>`;
  return `<div class="list-news">${items.slice(0, limit).map((n) => `
    <div class="item"><div class="row between"><a class="title" href="${esc(n.link)}" target="_blank" rel="noopener">${esc(n.title)}</a></div>
    <div class="row small" style="margin-top:3px">
      <span class="tag ${n.sentiment === "positive" ? "b" : n.sentiment === "negative" ? "s" : ""}" title="Keyword lexicon: +${esc((n.sentiment_words?.positive || []).join(", "))} / −${esc((n.sentiment_words?.negative || []).join(", "))}">${esc(n.sentiment)}</span>
      <span class="tag ${n.impact === "high" ? "w" : ""}">impact: ${esc(n.impact)}</span>
      ${(n.tags || []).map((t) => `<span class="tag">${esc(t)}</span>`).join("")}
      ${(n.symbols || []).map((s) => `<a class="tag" href="#/stock/${esc(s)}">${esc(s)}</a>`).join("")}
      <span class="dim">${esc(n.source)} · ${esc(n.published_ist)} (${n.age_hours}h ago)</span></div></div>`).join("")}</div>`;
}

// ------------------------------------------------------------------ TODAY (home)
async function viewToday() {
  const my = state.route;
  const live = () => my === state.route;
  app.innerHTML = `${disclaimer}<div class="grid g4" id="idx"></div><div style="height:14px"></div>
  <div class="grid g3"><div class="card hero span2" id="brief"><div class="loading"><span class="spinner"></span> Building today's briefing…</div></div>
  <div class="card" id="trend"></div></div><div style="height:14px"></div>
  <div class="grid g3">
    <div class="card"><h3>Best bullish setups <span class="sub" id="scan-asof"></span></h3><div id="t-bull"></div></div>
    <div class="card"><h3>Bearish / avoid</h3><div id="t-bear"></div></div>
    <div class="card"><h3>Best swing opportunities <span class="sub">rules + positive backtest</span></h3><div id="t-act"></div></div>
    <div class="card"><h3>Best intraday opportunities <span class="sub">NIFTY 50, 5-min</span></h3><div id="t-intra"></div></div>
    <div class="card"><h3>Approaching breakout</h3><div id="t-near"></div></div>
    <div class="card"><h3>High volume</h3><div id="t-vol"></div></div>
    <div class="card span2"><h3>Latest market news <a class="sub" href="#/news">all news →</a></h3><div id="t-news"></div></div>
    <div class="card"><h3>My watchlist <a class="sub" href="#/watchlist">manage →</a></h3><div id="t-watch"></div><hr><h3>Paper portfolio <a class="sub" href="#/paper">open →</a></h3><div id="t-paper"></div></div>
    <div class="card spanall"><h3>Sectors &amp; global cues</h3><div id="t-sect"></div></div>
  </div>`;
  const ov = api("/overview"), br = api("/briefing"), sc = api("/scan?universe=nifty100&mode=swing"),
    intra = api("/scan?universe=nifty50&mode=intraday"), news = api("/news?hours=24"), wl = api("/watchlist"), pp = api("/paper");
  ov.then((x) => live() && renderOverview(x)).catch((e) => { if (!live()) return; $("#idx").innerHTML = errorBox(e); });
  br.then((x) => live() && renderBriefing(x)).catch((e) => { if (!live()) return; $("#brief").innerHTML = errorBox(e); });
  sc.then((s) => { if (!live()) return;
    if (s.running && !s.rows.length) { ["#t-bull", "#t-bear", "#t-act", "#t-near", "#t-vol"].forEach((id) => $(id).innerHTML = `<div class="loading small"><span class="spinner"></span> First scan running (NIFTY 100)…</div>`); setTimeout(() => { if (location.hash.startsWith("#/today") || !location.hash) route(); }, 20000); return; }
    $("#scan-asof").textContent = s.finished_ist ? "scan " + s.finished_ist : "";
    $("#t-bull").innerHTML = compactList(s.top.bullish, "swing");
    $("#t-bear").innerHTML = compactList(s.top.bearish, "swing");
    $("#t-act").innerHTML = s.top.actionable.length ? compactList(s.top.actionable, "swing", "act") : `<div class="notrade small">NO TRADE – no swing setup passes every check (rating, reward:risk, size). Protect your capital.</div>`;
    $("#t-near").innerHTML = compactList(s.top.near_breakout, "swing");
    $("#t-vol").innerHTML = compactList(s.top.volume, "swing", "vol");
    ["#t-bull", "#t-bear", "#t-act", "#t-near", "#t-vol"].forEach((id) => bindRows($(id), "swing"));
  }).catch((e) => { if (!live()) return; $("#t-bull").innerHTML = errorBox(e); });
  intra.then((s) => { if (!live()) return;
    const rows = (s.top && s.top.bullish) || [];
    $("#t-intra").innerHTML = s.running && !s.rows.length ? `<div class="loading small"><span class="spinner"></span> Intraday scan running…</div>`
      : (rows.length ? compactList(rows, "intraday") : `<div class="empty small">No intraday long setups${s.market && !s.market.open ? " (market closed: showing the last session)" : ""}.</div>`) +
      (s.market && !s.market.open && rows.length ? `<div class="dim small" style="margin-top:6px">Market closed: these are from the last completed session, not live.</div>` : "");
    bindRows($("#t-intra"), "intraday");
  }).catch((e) => { if (!live()) return; $("#t-intra").innerHTML = errorBox(e); });
  news.then((n) => { if (!live()) return; $("#t-news").innerHTML = newsList(n.items, 8); }).catch((e) => { if (!live()) return; $("#t-news").innerHTML = errorBox(e); });
  wl.then((w) => { if (!live()) return;
    $("#t-watch").innerHTML = w.length ? `<div class="tablewrap"><table class="mini"><tbody>${w.map((x) => { const a = x.analysis || {}; return `<tr data-s="${esc(x.symbol)}"><td><b>${esc(x.symbol)}</b></td><td class="n">${fmt(a.price)}</td><td class="n ${cls(a.change_pct)}">${pct(a.change_pct, 1)}</td><td>${a.rating ? ratingBadge(a.rating) : '<span class="dim small">analysing…</span>'}</td></tr>`; }).join("")}</tbody></table></div>` : `<div class="empty small">Empty. Add stocks from any stock page or the Watchlist tab.</div>`;
    bindRows($("#t-watch"), "swing");
  });
  pp.then((p) => { if (!live()) return;
    const st = p.stats;
    $("#t-paper").innerHTML = `<div class="kv"><div><span>Cash</span><b>${inr(p.cash)}</b></div><div><span>Open</span><b>${p.open.length}</b></div><div><span>Realised P&amp;L</span><b class="${cls(st.realised_pnl)}">${inr(st.realised_pnl)}</b></div><div><span>Win rate</span><b>${st.win_rate == null ? "–" : st.win_rate + "%"}</b></div></div>`;
  });
}
function renderOverview(ov) {
  const cards = ov.indices.map((q) => `
    <div class="card idx"><div class="row between"><span class="name">${esc(q.label)}</span>${freshBadge(q.freshness)}</div>
    <div class="px">${fmt(q.price)}</div><div class="chg ${cls(q.change)}">${q.change == null ? "–" : (q.change > 0 ? "+" : "") + fmt(q.change)} (${pct(q.change_pct)})</div>
    <div class="spark" id="sp-${esc(q.label.replace(/\W/g, ""))}"></div></div>`).join("");
  $("#idx").innerHTML = cards;
  for (const [name, t] of Object.entries(ov.trends || {})) {
    const el = $("#sp-" + name.replace(/\W/g, ""));
    if (el && t.history) lineChart(el, t.history.map((h) => ({ time: h.time, value: h.close })), (t.history.at(-1).close >= t.history[0].close) ? cssVar("--bull") : cssVar("--bear"), { axis: false, static: true, height: 56 });
  }
  const mt = ov.market_trend;
  const col = mt.verdict === "Bullish" ? "r-bull" : mt.verdict === "Bearish" ? "r-bear" : "r-wait";
  const b = ov.breadth;
  $("#trend").innerHTML = `<h3>Market trend</h3><span class="rating big ${col}">${esc(mt.verdict.toUpperCase())}</span>
    <ul class="reasons small" style="margin-top:10px">${mt.why.map((w) => `<li>• ${esc(w)}</li>`).join("")}</ul>
    ${b ? `<hr><div class="small muted">Breadth (${esc(b.universe)}, ${esc(b.as_of)})</div><div class="row" style="margin-top:6px"><span class="up">▲ ${b.advancers}</span><span class="down">▼ ${b.decliners}</span><span class="muted">${b.pct_above_ema50}% above EMA 50</span></div>
    <div class="bar" style="margin-top:6px"><i style="width:${100 * b.advancers / Math.max(1, b.advancers + b.decliners)}%;background:var(--bull)"></i></div>` : `<div class="dim small">Breadth appears after the first scan.</div>`}
    ${Object.entries(ov.trends).map(([n, t]) => `<hr><div class="small"><b>${esc(n)}</b> ${ratingBadge(t.rating)} RSI ${fmt(t.rsi, 1)}<div class="muted">S: ${(t.supports || []).slice(0, 2).map((x) => fmt(x.price, 0)).join(", ") || "–"} · R: ${(t.resistances || []).slice(0, 2).map((x) => fmt(x.price, 0)).join(", ") || "–"}</div></div>`).join("")}`;
  const sect = ov.sectors.filter((q) => q.price != null).map((q) => { const c = q.change_pct || 0; const a = Math.min(1, Math.abs(c) / 2.5); return `<span class="s" style="background:${c >= 0 ? `rgba(34,197,94,${0.1 + a * 0.4})` : `rgba(239,68,68,${0.1 + a * 0.4})`}">${esc(q.label.replace("NIFTY ", ""))} <b class="mono">${pct(c, 2)}</b></span>`; }).join("");
  const glob = ov.global.map((q) => `<span class="tag" title="${esc(q.freshness?.as_of || "")}">${esc(q.label)} <b class="mono ${cls(q.change_pct)}">${q.price == null ? "n/a" : fmt(q.price) + " " + pct(q.change_pct, 1)}</b></span>`).join("");
  $("#t-sect").innerHTML = `<div class="sector-strip">${sect}</div><div style="margin-top:10px">${glob}</div><div class="dim small" style="margin-top:6px">Global quotes are each market's last session (time zones differ).</div>`;
}
function renderBriefing(b) {
  const dcls = b.decision === "TRADE SELECTIVELY" ? "d-TRADE" : b.decision === "NO TRADE" ? "d-NO" : "d-WAIT";
  $("#brief").innerHTML = `<h3>What should I do today? <span class="sub">${esc(b.generated_ist)}${b.scan_as_of ? " · scan " + esc(b.scan_as_of) : ""}</span></h3>
  <div class="row"><span class="decision ${dcls}">${esc(b.decision)}</span><span class="muted small">Market ${esc(b.market.label)} · next open ${new Date(b.next_open).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", weekday: "short", hour: "2-digit", minute: "2-digit" })} IST</span></div>
  <p style="font-size:15px">${esc(b.summary)}</p>
  <div class="grid g2">
    <div><div class="small muted">MARKET OUTLOOK</div><ul class="reasons small">${b.outlook.map((l) => `<li>• ${esc(l)}</li>`).join("")}</ul>
      <div class="small muted" style="margin-top:8px">KEY LEVELS</div>${b.levels.map((l) => `<div class="small"><b>${esc(l.index)}</b> support ${l.supports.map((x) => fmt(x, 0)).join(" / ") || "–"} · resistance ${l.resistances.map((x) => fmt(x, 0)).join(" / ") || "–"}</div>`).join("") || '<div class="dim small">–</div>'}
      <div class="small muted" style="margin-top:8px">RISKS</div>${b.risks.length ? `<ul class="reasons small">${b.risks.map((r) => `<li>⚠️ ${esc(r)}</li>`).join("")}</ul>` : '<div class="dim small">No elevated VIX or sharp global moves flagged.</div>'}
      ${b.upcoming_holidays.length ? `<div class="small dim" style="margin-top:6px">Upcoming NSE holidays (verify): ${b.upcoming_holidays.map((h) => esc(h.date + " " + h.name)).join(", ")}</div>` : ""}</div>
    <div><div class="small muted">${b.opportunities_are_actionable ? "OPPORTUNITIES THAT PASS EVERY RULE" : "BULLISH NAMES TO INVESTIGATE (not yet actionable)"}</div>
      ${b.opportunities.length ? `<table class="mini plain"><tbody>${b.opportunities.map((o) => `<tr><td><a href="#/stock/${esc(o.symbol)}"><b>${esc(o.symbol)}</b></a></td><td>${ratingBadge(o.rating)}</td><td class="small" style="white-space:normal">${esc(o.setup || "")}${o.entry ? ` · ${fmt(o.entry)} / SL ${fmt(o.stop)} / T1 ${fmt(o.target1)}` : ""}${o.backtest ? `<div class="dim">History: ${esc(o.backtest)}</div>` : ""}</td></tr>`).join("")}</tbody></table>` : '<div class="dim small">None.</div>'}
      <div class="small muted" style="margin-top:8px">STOCKS TO AVOID</div>${b.avoid.length ? b.avoid.map((a) => `<a class="tag s" href="#/stock/${esc(a.symbol)}" title="${esc((a.why || []).join(" "))}">${esc(a.symbol)}</a>`).join("") : '<div class="dim small">–</div>'}
      <div class="small muted" style="margin-top:8px">HIGH-IMPACT NEWS (24h)</div>${b.events.length ? b.events.slice(0, 5).map((n) => `<div class="small">• <a href="${esc(n.link)}" target="_blank" rel="noopener">${esc(n.title.slice(0, 110))}</a> <span class="dim">${esc(n.source)}</span></div>`).join("") : '<div class="dim small">None flagged.</div>'}
    </div></div>
  <div class="dim small" style="margin-top:8px">${esc(b.method)} ${esc(b.intraday_note)}</div>`;
}

// ------------------------------------------------------------------ SCANNER
const SETUPS = [["", "Any setup"], ["breakout_20d", "20-day breakout"], ["breakdown_20d", "20-day breakdown"], ["bull_flag", "Bull flag"], ["bear_flag", "Bear flag"], ["oversold", "RSI oversold"], ["overbought", "RSI overbought"], ["oversold_bounce", "Oversold bounce"], ["macd_bull_cross", "MACD bull cross"], ["macd_bear_cross", "MACD bear cross"], ["ema_golden_20_50", "EMA 20/50 golden"], ["ema_death_20_50", "EMA 20/50 death"], ["golden_cross_50_200", "Golden cross 50/200"], ["death_cross_50_200", "Death cross 50/200"], ["vwap_breakout", "VWAP breakout"], ["vwap_breakdown", "VWAP breakdown"], ["unusual_volume", "Unusual volume"], ["momentum", "Momentum"], ["resistance_breakout", "Resistance breakout"], ["support_breakdown", "Support breakdown"], ["trend_reversal_up", "Trend reversal up"], ["trend_reversal_down", "Trend reversal down"], ["near_breakout", "Approaching breakout"], ["orb_breakout", "ORB breakout (intraday)"], ["orb_breakdown", "ORB breakdown (intraday)"], ["vwap_reclaim", "VWAP reclaim (intraday)"], ["volume_spike", "Volume spike (intraday)"], ["intraday_momentum", "Intraday momentum"]];
const RATINGS = ["", "STRONG BULLISH", "BULLISH", "WAIT", "HIGH RISK", "BEARISH", "AVOID"];
async function viewScanner(preset = {}) {
  const my = state.route;
  const unis = await api("/universes").catch(() => []);
  if (my !== state.route) return;
  const p = new URLSearchParams(location.hash.split("?")[1] || "");
  const f = { universe: p.get("universe") || preset.universe || "nifty100", mode: p.get("mode") || preset.mode || "swing", sector: p.get("sector") || "", cap: p.get("cap") || "", setup: p.get("setup") || preset.setup || "", rating: p.get("rating") || preset.rating || "", min_price: p.get("min_price") || "", max_price: p.get("max_price") || "", min_volume: p.get("min_volume") || "", q: p.get("q") || "" };
  app.innerHTML = `${disclaimer}<div class="card"><h3>${esc(preset.title || "Live market scanner")} <span class="sub" id="sc-meta"></span></h3>
    ${preset.note ? `<div class="small muted" style="margin-bottom:10px">${preset.note}</div>` : ""}
    <div class="filters">
      <label class="f">Universe<select id="f-universe">${unis.map((u) => `<option value="${u.key}" ${u.key === f.universe ? "selected" : ""}>${esc(u.name)}</option>`).join("")}<option value="watchlist" ${f.universe === "watchlist" ? "selected" : ""}>My watchlist</option></select></label>
      <label class="f">Mode<select id="f-mode" ${preset.lockMode ? "disabled" : ""}><option value="swing" ${f.mode === "swing" ? "selected" : ""}>Swing (daily)</option><option value="intraday" ${f.mode === "intraday" ? "selected" : ""}>Intraday (5-min)</option></select></label>
      <label class="f">Sector<select id="f-sector"><option value="">All sectors</option></select></label>
      <label class="f">Market cap<select id="f-cap"><option value="">All</option>${["Largecap", "Midcap", "Smallcap"].map((c) => `<option ${c === f.cap ? "selected" : ""}>${c}</option>`).join("")}</select></label>
      <label class="f">Setup<select id="f-setup">${SETUPS.map(([k, l]) => `<option value="${k}" ${k === f.setup ? "selected" : ""}>${l}</option>`).join("")}</select></label>
      <label class="f">Rating<select id="f-rating">${RATINGS.map((r) => `<option value="${r}" ${r === f.rating ? "selected" : ""}>${r || "Any rating"}</option>`).join("")}</select></label>
      <label class="f">Min price ₹<input id="f-min_price" type="number" value="${esc(f.min_price)}"></label>
      <label class="f">Max price ₹<input id="f-max_price" type="number" value="${esc(f.max_price)}"></label>
      <label class="f">Min volume<input id="f-min_volume" type="number" value="${esc(f.min_volume)}"></label>
      <label class="f">Search<input id="f-q" value="${esc(f.q)}" placeholder="symbol / name"></label>
      <div class="row"><button class="btn primary" id="sc-apply">Apply</button><button class="btn" id="sc-refresh" title="Re-fetch data and re-run the scan">↻ Rescan</button></div>
    </div></div><div style="height:12px"></div><div id="sc-body"><div class="loading"><span class="spinner"></span> Loading scan…</div></div>`;
  if (preset.lockMode) $("#f-mode").value = preset.mode;
  const qs = (refresh) => {
    const o = {}; ["universe", "mode", "sector", "cap", "setup", "rating", "min_price", "max_price", "min_volume", "q"].forEach((k) => { const v = $("#f-" + k).value; if (v) o[k] = v; });
    if (refresh) o.refresh = "true"; return new URLSearchParams(o).toString();
  };
  async function load(refresh) {
    try {
      const r = await api("/scan?" + qs(refresh));
      const sel = $("#f-sector"); const cur = sel.value || f.sector;
      sel.innerHTML = `<option value="">All sectors</option>` + (r.sectors || []).map((s) => `<option ${s === cur ? "selected" : ""}>${esc(s)}</option>`).join("");
      $("#sc-meta").innerHTML = r.running ? `<span class="spinner"></span> scanning ${esc(r.title || r.universe)}…` : r.finished_ist ? `${r.rows.length} of ${r.total} · scanned ${esc(r.finished_ist)} · ${Object.keys(r.errors || {}).length} data errors` : "";
      const mode = $("#f-mode").value;
      $("#sc-body").innerHTML = (r.message && !r.rows.length) ? `<div class="empty">${esc(r.message)}</div>` :
        `${mode === "intraday" && r.market && !r.market.open ? `<div class="disclaimer">Market is closed: intraday rows describe the last completed session, not a live one.</div>` : ""}${scanTable(r.rows, { mode, fresh: false, maxh: 700 })}
         <div class="dim small" style="margin-top:6px">${esc(r.source || "")} Hover a row for the reasons; click for the full analysis.</div>`;
      bindRows($("#sc-body"), mode);
      if (r.running) setTimeout(() => { if ($("#sc-body")) load(false); }, 8000);
    } catch (e) { $("#sc-body").innerHTML = errorBox(e); }
  }
  $("#sc-apply").onclick = () => load(false);
  $("#sc-refresh").onclick = () => load(true);
  ["#f-universe", "#f-mode"].forEach((id) => $(id).addEventListener("change", () => load(false)));
  load(false);
}

// ------------------------------------------------------------------ STOCK DETAIL
async function viewStock(sym) {
  const p = new URLSearchParams(location.hash.split("?")[1] || "");
  const mode = p.get("mode") || "swing";
  app.innerHTML = `<div class="loading"><span class="spinner"></span> Analysing ${esc(sym)} (${esc(mode)}): fetching prices, running the engine${mode === "intraday" ? ", 10,000 Time Machine replays" : ", 5-year backtest"}${mode !== "intraday" ? " and fundamentals" : ""}…</div>`;
  let d;
  const my = state.route;
  try { d = await api(`/stock/${encodeURIComponent(sym)}?mode=${mode}`); } catch (e) { if (my === state.route) app.innerHTML = errorBox(e); return; }
  if (my !== state.route) return;
  const a = mode === "intraday" ? d.intraday : d.daily;
  const q = d.daily.quote;
  const conf = a.confidence;
  const plan = d.plan;
  const f = d.daily.features || {};
  const fi = (d.intraday && d.intraday.features) || {};
  app.innerHTML = `${disclaimer}
  <div class="card hero"><div class="row between">
    <div><div class="row"><h2 style="margin:0">${esc(d.symbol)}</h2><span class="muted">${esc(q.name || d.meta.name || "")}</span>${d.meta.sector ? `<span class="tag">${esc(d.meta.sector)}</span>` : ""}${d.meta.cap ? `<span class="tag">${esc(d.meta.cap)}</span>` : ""}</div>
      <div class="row" style="margin-top:6px"><span class="mono" style="font-size:26px;font-weight:600">₹${fmt(q.price)}</span><span class="mono ${cls(q.change)}" style="font-size:15px">${q.change == null ? "" : (q.change > 0 ? "+" : "") + fmt(q.change)} (${pct(q.change_pct)})</span>${freshBadge(q.freshness)}<span class="dim small">Vol ${vol(q.volume)} · 52w ${fmt(q.low52)}–${fmt(q.high52)}</span></div></div>
    <div class="row"><div class="seg">${["intraday", "swing", "longterm"].map((m) => `<button class="${m === mode ? "on" : ""}" data-m="${m}">${{ intraday: "Intraday", swing: "Swing", longterm: "Long-term" }[m]}</button>`).join("")}</div>
      <button class="btn" id="wl-add">☆ Watchlist</button></div></div></div>
  <div style="height:14px"></div>
  <div class="grid g3">
    <div class="card span2"><h3>${mode === "intraday" ? "Intraday chart" : "Price chart"} <span class="sub" id="ch-sub"></span></h3>
      <div class="row" style="margin-bottom:8px"><div class="seg" id="tf">${(mode === "intraday" ? ["1m", "5m", "15m"] : ["1d", "1wk"]).map((t, i) => `<button class="${i === (mode === "intraday" ? 1 : 0) ? "on" : ""}" data-t="${t}">${t}</button>`).join("")}</div>
      <span class="small muted">${mode === "intraday" ? "Session VWAP (orange), opening range (dashed)" : "EMA 20 (blue) · 50 (yellow) · 200 (purple) · VWAP 20d (orange) · S/R dashed"}</span></div>
      <div class="chart" id="ch-main"></div>${mode !== "intraday" ? `<div class="small muted" style="margin-top:6px">RSI 14</div><div class="chart sm" id="ch-rsi"></div><div class="small muted">MACD 12/26/9</div><div class="chart sm" id="ch-macd"></div>` : ""}</div>
    <div class="card"><h3>Assessment <span class="sub">${esc(a.timeframe || "")}</span></h3>
      <div class="row">${ratingBadge(a.rating, true)}<span class="mono" style="font-size:18px">score ${a.score == null ? "–" : (a.score > 0 ? "+" : "") + a.score}</span></div>
      ${conf ? `<div style="margin-top:10px"><div class="row between small"><span>Confidence <b class="mono">${conf.value}/100</b></span><span class="dim">evidence ${conf.strength} · agreement ${conf.agreement} · data ${conf.completeness}</span></div>
      <div class="bar" style="margin-top:4px"><i style="width:${conf.value}%;background:var(--accent)"></i></div>
      <div class="dim small" style="margin-top:4px">${esc(conf.formula)}. ${esc(conf.agreement_detail)}. ${esc(conf.note)}</div></div>` : ""}
      <div class="small muted" style="margin-top:10px">WHY THIS RATING</div><div class="small">${(a.why || []).map((w) => "• " + esc(w)).join("<br>")}</div>
      <ul class="reasons small" style="margin-top:8px">${(a.reasons || []).map((r) => `<li><span class="pts ${cls(r.points)}">${r.points > 0 ? "+" : ""}${r.points}</span>${esc(r.text)}</li>`).join("")}</ul>
      ${(a.flags || []).length ? `<div class="small muted" style="margin-top:8px">RISK FLAGS</div>${a.flags.map((x) => `<div class="small" style="color:var(--orange)">⚠️ ${esc(x)}</div>`).join("")}` : ""}
      <div class="small muted" style="margin-top:8px">SETUPS</div><div>${(a.setups || []).length ? a.setups.map((s) => `<span class="tag ${s.bias > 0 ? "b" : s.bias < 0 ? "s" : "w"}">${esc(s.label)}</span>`).join("") : '<span class="dim small">none</span>'}</div>
    </div>
  </div><div style="height:14px"></div>
  <div class="grid g3">
    <div class="card"><h3>Indicators <span class="sub">${mode === "intraday" ? "5-min session" : "daily, completed bars to " + esc(d.daily.last_completed || "")}</span></h3>
      <div class="kv">${mode === "intraday" ? `
        <div><span>VWAP</span><b>${fmt(fi.vwap)}</b></div><div><span>OR high (15m)</span><b>${fmt(fi.or_high)}</b></div><div><span>OR low</span><b>${fmt(fi.or_low)}</b></div><div><span>OR mid</span><b>${fmt(fi.or_mid)}</b></div>
        <div><span>EMA 9 (5m)</span><b>${fmt(fi.ema9)}</b></div><div><span>EMA 21 (5m)</span><b>${fmt(fi.ema21)}</b></div><div><span>Trigger</span><b>${fi.trigger_bar ? esc(fi.trigger_bar) + " @ " + fmt(fi.trigger_close) : "–"}</b></div><div><span>Vol spikes</span><b>${fi.volume_spikes ?? "–"}</b></div>` : ""}
        <div><span>RSI 14</span><b>${fmt(f.rsi, 1)}</b></div><div><span>MACD</span><b class="${cls((f.macd ?? 0) - (f.macd_signal ?? 0))}">${fmt(f.macd)}</b></div><div><span>Signal</span><b>${fmt(f.macd_signal)}</b></div><div><span>VWAP 20d</span><b>${fmt(f.vwap20)}</b></div>
        <div><span>EMA 20</span><b>${fmt(f.ema20)}</b></div><div><span>EMA 50</span><b>${fmt(f.ema50)}</b></div><div><span>EMA 200</span><b>${fmt(f.ema200)}</b></div><div><span>ATR 14</span><b>${fmt(f.atr)} <small class="dim">${fmt(f.atr_pct, 1)}%</small></b></div>
        <div><span>Vol vs 20d</span><b>${fmt(f.vol_ratio, 2)}×</b></div><div><span>Traded value</span><b>₹${fmt(f.traded_value_cr, 0)} cr</b></div><div><span>20d vs NIFTY</span><b class="${cls(f.rs20)}">${pct(f.rs20, 1)}</b></div><div><span>Trend</span><b style="font-size:13px">${esc(d.daily.trend || "–")}</b></div>
      </div>
      <div class="small muted" style="margin-top:10px">SUPPORT</div><div class="mono small">${(f.supports || []).map((s) => `${fmt(s.price)} <span class="dim">(${s.touches}×)</span>`).join(" · ") || "–"}</div>
      <div class="small muted" style="margin-top:4px">RESISTANCE</div><div class="mono small">${(f.resistances || []).map((s) => `${fmt(s.price)} <span class="dim">(${s.touches}×)</span>`).join(" · ") || "–"}</div></div>
    <div class="card" id="plan-card"></div>
    <div class="card" id="bt-card"></div>
  </div><div style="height:14px"></div>
  <div class="grid g2">
    <div class="card" id="fund-card"></div>
    <div class="card"><h3>Latest news <span class="sub">last 72h only</span></h3>${newsList(d.news, 12)}${d.news_error ? `<div class="dim small">${esc(d.news_error)}</div>` : ""}</div>
    <div class="card spanall"><h3>Signal history <span class="sub">from past scans stored in the database</span></h3>${d.history.length ? `<div class="tablewrap"><table class="mini plain"><thead><tr><th>When</th><th>Mode</th><th>Rating</th><th class="n">Score</th><th class="n">Conf.</th><th class="n">Price</th></tr></thead><tbody>${d.history.map((h) => `<tr><td>${istTime(h.ts)}</td><td>${esc(h.mode)}</td><td>${ratingBadge(h.rating)}</td><td class="n">${h.score}</td><td class="n">${h.confidence}</td><td class="n">${fmt(h.price)}</td></tr>`).join("")}</tbody></table></div>` : '<div class="empty small">No stored signals yet.</div>'}</div>
  </div>`;
  app.querySelectorAll(".seg [data-m]").forEach((b) => b.onclick = () => { location.hash = `#/stock/${d.symbol}?mode=${b.dataset.m}`; });
  $("#wl-add").onclick = async () => { try { await api("/watchlist", { method: "POST", body: { symbol: d.symbol, mode } }); toast(`${d.symbol} added to your watchlist`); } catch (e) { toast(esc(e.message)); } };
  renderPlan(d, plan, mode);
  renderBacktest(d.backtest, mode);
  renderFund(d.fundamentals, mode);
  drawStockCharts(d, mode, plan);
}
function drawStockCharts(d, mode, plan) {
  const f = d.daily.features || {};
  const lv = [];
  (f.supports || []).slice(0, 2).forEach((s) => lv.push({ price: s.price, color: cssVar("--bull"), title: "S" }));
  (f.resistances || []).slice(0, 2).forEach((s) => lv.push({ price: s.price, color: cssVar("--bear"), title: "R" }));
  if (plan && plan.entry) { lv.push({ price: plan.entry, color: cssVar("--info"), title: "Entry" }); lv.push({ price: plan.stop, color: cssVar("--bear"), title: "SL" }); if (plan.target1) lv.push({ price: plan.target1, color: cssVar("--bull"), title: "T1" }); }
  const draw = async (tf) => {
    state.charts.forEach((c) => { try { c.remove(); } catch { } }); state.charts = [];
    const el = $("#ch-main"); el.innerHTML = "";
    if (mode === "intraday") {
      let data = d.chart_intraday;
      if (tf !== "5m" || !data) { try { data = await api(`/chart/${d.symbol}?interval=${tf}`); } catch (e) { el.innerHTML = errorBox(e); return; } }
      const fi = d.intraday.features || {};
      const ilv = [{ price: fi.or_high, color: cssVar("--muted"), title: "OR H" }, { price: fi.or_low, color: cssVar("--muted"), title: "OR L" }].filter((x) => x.price).concat(plan && plan.entry ? [{ price: plan.entry, color: cssVar("--info"), title: "Entry" }, { price: plan.stop, color: cssVar("--bear"), title: "SL" }, { price: plan.target1, color: cssVar("--bull"), title: "T1" }] : []);
      if (!data.candles.length) { el.innerHTML = `<div class="empty">No intraday bars for this session.</div>`; return; }
      candleChart(el, data, { levels: ilv });
      $("#ch-sub").textContent = `session ${d.intraday.session || data.session || ""}${d.intraday.session_is_today ? "" : " (last completed session, not live)"}`;
    } else {
      if (tf === "1wk") { candleChart(el, { candles: d.chart_weekly.map((b) => ({ ...b, volume: 0 })) }, { levels: lv }); $("#ch-sub").textContent = "weekly, 5 years"; }
      else { candleChart(el, d.chart_daily, { levels: lv, visible: 180 }); $("#ch-sub").textContent = "daily, 2 years (scroll to zoom)"; }
      const r = lineChart($("#ch-rsi"), d.chart_daily.rsi, cssVar("--accent"), { height: 150 });
      [70, 30].forEach((x) => r.series.createPriceLine({ price: x, color: cssVar("--dim"), lineStyle: 2, lineWidth: 1, axisLabelVisible: false }));
      r.chart.timeScale().setVisibleLogicalRange({ from: d.chart_daily.rsi.length - 180, to: d.chart_daily.rsi.length });
      const m = mkChart($("#ch-macd"), 150);
      const h = m.addHistogramSeries({ priceLineVisible: false }); h.setData(shift(d.chart_daily.hist.map((p) => ({ ...p, color: p.value >= 0 ? "rgba(34,197,94,.6)" : "rgba(239,68,68,.6)" }))));
      m.addLineSeries({ color: "#38bdf8", lineWidth: 1.5, priceLineVisible: false }).setData(shift(d.chart_daily.macd));
      m.addLineSeries({ color: "#eab308", lineWidth: 1.5, priceLineVisible: false }).setData(shift(d.chart_daily.signal));
      m.timeScale().setVisibleLogicalRange({ from: d.chart_daily.hist.length - 180, to: d.chart_daily.hist.length });
    }
  };
  $("#tf").querySelectorAll("button").forEach((b) => b.onclick = () => { $("#tf").querySelectorAll("button").forEach((x) => x.classList.remove("on")); b.classList.add("on"); draw(b.dataset.t); });
  draw(mode === "intraday" ? "5m" : "1d");
}
function renderPlan(d, plan, mode) {
  const el = $("#plan-card");
  if (mode === "longterm") {
    el.innerHTML = `<h3>Long-term view</h3><div class="small">Long-term investing uses the fundamentals panel below, not trading signals. Technical context: <b>${esc(d.daily.trend || "–")}</b>, rated ${ratingBadge(d.daily.rating)} on the daily chart (for timing only).</div><div class="dim small" style="margin-top:8px">No trade plan is generated in long-term mode: an intraday or swing signal is never used as an investment recommendation.</div>`;
    return;
  }
  if (!plan.entry) {
    el.innerHTML = `<h3>Trade plan <span class="sub">${esc(mode)}</span></h3><div class="notrade">${plan.status === "NO TRADE" ? "NO TRADE – " : ""}${esc(plan.message || "No trade.")}</div>
    <div class="small muted" style="margin-top:8px">NO TRADE – Market conditions are not favourable for this stock. Protect your capital.</div>`;
    return;
  }
  const pos = plan.position || {};
  el.innerHTML = `<h3>Trade plan <span class="sub">${esc(mode)} · long only</span></h3>
  <div class="row">${plan.actionable ? `<span class="rating r-strong">ACTIONABLE after confirmation</span>` : `<span class="rating r-wait">NOT ACTIONABLE</span>`}<span class="small muted">${esc(plan.setup || "")}</span></div>
  ${plan.actionable ? "" : `<div class="small" style="color:var(--warn);margin-top:6px">${esc(plan.message || "")}</div>`}
  <div class="kv" style="margin-top:10px">
    <div><span>Entry range</span><b>${fmt(plan.entry_low)}–${fmt(plan.entry_high)}</b></div><div><span>Stop-loss</span><b class="down">${fmt(plan.stop)}</b></div>
    <div><span>Target 1 (${esc(plan.target1_basis || "")})</span><b class="up">${fmt(plan.target1)}</b></div><div><span>Target 2</span><b class="up">${fmt(plan.target2)}</b></div>
    <div><span>Reward : risk</span><b>${fmt(plan.reward_risk, 2)}</b></div><div><span>Holding</span><b style="font-size:12px">${esc(plan.holding_period || "")}</b></div>
    <div><span>Quantity</span><b>${pos.qty ?? 0}</b></div><div><span>Capital needed</span><b>${inr(pos.capital_required)}</b></div>
    <div><span>Max loss (at stop)</span><b class="down">${inr(pos.max_loss)}</b></div><div><span>Potential reward (T1)</span><b class="up">${inr(pos.potential_reward)}</b></div>
  </div>
  ${pos.refused ? `<div class="small" style="color:var(--bear);margin-top:6px">${esc(pos.refused)}</div>` : `<div class="dim small" style="margin-top:6px">Size decided by: ${esc(pos.decided_by || "")}. Charges ≈ ${inr(pos.est_costs?.total, 2)} round trip. ${esc(pos.note || "")}</div>`}
  <div class="small muted" style="margin-top:10px">CONFIRMATION REQUIRED BEFORE ENTRY</div>${(plan.confirmation || []).map((c) => `<div class="small">☐ ${esc(c)}</div>`).join("")}
  <div class="small muted" style="margin-top:8px">SETUP IS INVALID IF</div>${(plan.invalidation || []).map((c) => `<div class="small">✕ ${esc(c)}</div>`).join("")}
  <div class="small muted" style="margin-top:8px">TECHNICAL REASONING</div>${(plan.reasoning || []).map((c) => `<div class="small">• ${esc(c)}</div>`).join("") || '<div class="dim small">–</div>'}
  <hr><button class="btn ${plan.actionable ? "good" : ""}" id="propose">Propose paper trade</button> <span class="dim small">Paper only. You will be asked to type "yes"; the risk check runs again.</span>
  <div id="ticket"></div>`;
  $("#propose").onclick = () => proposeFlow(d.symbol, mode, $("#ticket"));
}
async function proposeFlow(symbol, mode, box) {
  box.innerHTML = `<div class="loading small"><span class="spinner"></span> Re-analysing and running the risk check…</div>`;
  try {
    const r = await api("/paper/propose", { method: "POST", body: { symbol, mode } });
    if (r.error) { box.innerHTML = `<div class="notrade small" style="margin-top:8px">${esc(r.error)}</div>`; return; }
    const checks = r.risk.checks.map((c) => `<div class="small">${c.ok ? "✅" : "⛔"} ${esc(c.text)}</div>`).join("");
    if (!r.ok) { box.innerHTML = `<div style="margin-top:10px"><span class="rating r-avoid">RISK CHECK: BLOCK</span>${checks}</div>`; return; }
    box.innerHTML = `<div style="margin-top:10px"><span class="rating r-strong">RISK CHECK: PASS</span>${checks}
      ${r.warning ? `<div class="small" style="color:var(--warn)">⚠️ ${esc(r.warning)}</div>` : ""}
      <div class="card" style="margin-top:8px"><div class="small">${esc(r.ticket)}</div>
      <div class="row" style="margin-top:8px"><input id="yes-in" placeholder='Type "yes" to confirm this one order'><button class="btn good" id="yes-btn">Confirm</button><button class="btn bad" id="no-btn">Decline</button></div><div id="yes-msg" class="small"></div></div></div>`;
    $("#yes-btn").onclick = async () => {
      const c = await api("/paper/confirm", { method: "POST", body: { order_id: r.order.id, reply: $("#yes-in").value } });
      $("#yes-msg").innerHTML = c.ok ? (c.queued ? `<span style="color:var(--warn)">${esc(c.note)}</span>` : `<span class="up">Paper position opened at ₹${fmt(c.fill_price)}. ${esc(c.note)}</span>`) : `<span class="down">${esc(c.error)}</span>`;
    };
    $("#no-btn").onclick = async () => { await api("/paper/decline", { method: "POST", body: { order_id: r.order.id } }); $("#yes-msg").innerHTML = "Declined. Nothing placed."; };
  } catch (e) { box.innerHTML = errorBox(e); }
}
function renderBacktest(bt, mode) {
  const el = $("#bt-card");
  if (mode === "longterm") { el.innerHTML = `<h3>Backtest</h3><div class="dim small">Backtests apply to trading setups (intraday / swing modes).</div>`; return; }
  if (!bt) { el.innerHTML = `<h3>Backtest &amp; probability</h3><div class="empty small">No plan with an entry and stop, so nothing to test.</div>`; return; }
  if (bt.error) { el.innerHTML = `<h3>Backtest &amp; probability</h3><div class="empty small">${esc(bt.error)}</div>`; return; }
  const h = mode === "intraday" ? bt.history : bt;
  const mc = bt.monte_carlo;
  const stat = (h) => `<div class="kv">
    <div><span>Occurrences</span><b>${h.occurrences}</b></div><div><span>Target hit</span><b class="up">${h.target_hit_pct ?? "–"}%</b></div><div><span>Stop hit</span><b class="down">${h.stop_hit_pct ?? "–"}%</b></div><div><span>Neither / time exit</span><b>${h.neither_pct ?? "–"}%</b></div>
    <div><span>Win rate (net)</span><b>${h.win_rate_pct ?? "–"}%</b></div><div><span>Avg R (net)</span><b class="${cls(h.avg_R)}">${fmt(h.avg_R, 2)}</b></div><div><span>Avg win / loss</span><b>${fmt(h.avg_win_R, 2)} / ${fmt(h.avg_loss_R, 2)}</b></div><div><span>Max drawdown</span><b class="down">${fmt(h.max_drawdown_R, 1)}R</b></div></div>`;
  el.innerHTML = `<h3>Backtest &amp; probability <span class="sub">${esc(bt.method)}</span></h3>
    <div class="small"><b>Rule:</b> ${esc(h.rule || bt.rule || "")}</div><div class="small muted">Period ${esc(bt.period || "")} · ${mode === "intraday" ? `${bt.sessions_used} sessions` : `${bt.bars} daily bars, max hold ${bt.max_hold_days}d, stop ${bt.stop_atr_multiple}×ATR, target ${bt.target_R}R`}</div>
    <div style="margin-top:8px">${h.occurrences ? stat(h) : '<div class="empty small">The rule never fired in this period.</div>'}</div>
    ${h.sample_warning ? `<div class="small" style="color:var(--warn);margin-top:6px">⚠️ ${esc(h.sample_warning)}</div>` : ""}
    <div class="dim small" style="margin-top:6px">Costs: ${esc(h.costs || bt.costs || "")}. ${bt.data_note ? esc(bt.data_note) : ""}</div>
    ${mc && !mc.error ? `<hr><div class="small muted">TIME MACHINE · ${mc.replays.toLocaleString()} replays from ${esc(mc.entry_bar)} (seed ${mc.seed})</div>
      <div class="kv" style="margin-top:6px"><div><span>Target first</span><b class="up">${fmt(mc.p_target * 100, 1)}%</b></div><div><span>Stop first</span><b class="down">${fmt(mc.p_stop * 100, 1)}%</b></div><div><span>Neither by ${esc(mc.until || "")}</span><b>${fmt(mc.p_neither * 100, 1)}%</b></div><div><span>Avg R</span><b class="${cls(mc.avg_R)}">${fmt(mc.avg_R, 2)}</b></div>
      <div><span>Median close</span><b>${fmt(mc.median_close)}</b></div><div><span>5–95% close</span><b style="font-size:12px">${fmt(mc.p5_close)}–${fmt(mc.p95_close)}</b></div></div>
      <div class="small" style="margin-top:6px"><b>${esc(mc.verdict)}</b> ${(mc.notes || []).map(esc).join(" ")}</div>` : ""}
    <div class="small" style="color:var(--warn);margin-top:8px">${esc(bt.disclaimer)}</div>
    ${(h.trades || []).length ? `<details style="margin-top:8px"><summary class="small muted">Last ${h.trades.length} historical trades</summary><div class="tablewrap" style="max-height:240px"><table class="mini plain"><thead><tr><th>Date</th><th class="n">Entry</th><th class="n">Stop</th><th class="n">Target</th><th>Outcome</th><th class="n">Net R</th></tr></thead><tbody>${h.trades.slice().reverse().map((t) => `<tr><td>${esc(t.date)}</td><td class="n">${fmt(t.entry)}</td><td class="n">${fmt(t.stop)}</td><td class="n">${fmt(t.target)}</td><td>${esc(t.outcome)}</td><td class="n ${cls(t.net_R)}">${fmt(t.net_R, 2)}</td></tr>`).join("")}</tbody></table></div></details>` : ""}`;
}
function renderFund(fd, mode) {
  const el = $("#fund-card");
  if (mode === "intraday") { el.innerHTML = `<h3>Fundamentals</h3><div class="dim small">Not used for intraday decisions. Switch to Swing or Long-term to load them.</div>`; return; }
  if (!fd || fd.error) { el.innerHTML = `<h3>Fundamentals</h3><div class="empty small">Unavailable: ${esc(fd ? fd.error : "not loaded")}</div>`; return; }
  const c = fd.classification;
  const ccls = c === "Strong fundamentals" ? "r-bull" : c === "Weak fundamentals" ? "r-bear" : c === "Average fundamentals" ? "r-wait" : "r-na";
  const vcls = /Under/.test(fd.valuation) ? "r-bull" : /Over/.test(fd.valuation) ? "r-bear" : "r-wait";
  const sh = fd.shareholding || {};
  const peers = (fd.peers && fd.peers.peers) || [];
  el.innerHTML = `<h3>Fundamentals <span class="sub">investment analysis, separate from trading signals</span></h3>
  <div class="row"><span class="rating ${ccls}">${esc(c)}</span><span class="rating ${vcls}">${esc(fd.valuation)}</span><span class="dim small">${esc(fd.valuation_note || "")}</span></div>
  <div class="kv" style="margin-top:10px">
    <div><span>Market cap</span><b>₹${fmt(fd.market_cap_cr, 0)} cr</b></div><div><span>P/E</span><b>${fmt(fd.pe, 1)}</b></div><div><span>P/B</span><b>${fmt(fd.pb, 2)}</b></div><div><span>EPS (TTM)</span><b>${fmt(fd.eps_ttm)}</b></div>
    <div><span>ROE</span><b>${fmt(fd.roe, 1)}%</b></div><div><span>ROCE</span><b>${fmt(fd.roce, 1)}%</b></div><div><span>Debt / equity</span><b>${fd.is_financial ? "n/a (lender)" : fmt(fd.debt_to_equity, 2)}</b></div><div><span>Operating margin</span><b>${fd.is_financial ? "n/a" : fmt(fd.opm, 0) + "%"}</b></div>
    <div><span>Sales growth 3y / 5y</span><b>${fmt(fd.sales_growth_3y, 0)}% / ${fmt(fd.sales_growth_5y, 0)}%</b></div><div><span>Profit growth 3y / 5y</span><b>${fmt(fd.profit_growth_3y, 0)}% / ${fmt(fd.profit_growth_5y, 0)}%</b></div>
    <div><span>Qtr sales / profit YoY</span><b>${pct(fd.qtr_sales_yoy, 0)} / ${pct(fd.qtr_profit_yoy, 0)}</b></div><div><span>CFO / profit (3y)</span><b>${fmt(fd.cfo_to_profit, 2)}</b></div>
    <div><span>Promoters</span><b>${sh.promoters == null ? "–" : fmt(sh.promoters, 1) + "%"}</b></div><div><span>FII / DII</span><b>${fmt(sh.fii, 1)}% / ${fmt(sh.dii, 1)}%</b></div><div><span>Pledge</span><b style="font-size:12px">${fd.promoter_pledge_pct == null ? "not disclosed" : fmt(fd.promoter_pledge_pct, 2) + "%"}</b></div><div><span>Holding as of</span><b style="font-size:12px">${esc(sh.as_of || "–")}</b></div>
  </div>
  <div class="small muted" style="margin-top:10px">CHECKS</div>${fd.checks.map((x) => `<div class="small">${x.ok === true ? "✅" : x.ok === false ? "⛔" : "➖"} ${esc(x.text)}</div>`).join("")}
  ${fd.missing.length ? `<div class="dim small">Not available: ${esc(fd.missing.join(", "))}</div>` : ""}${fd.financial_note ? `<div class="dim small">${esc(fd.financial_note)}</div>` : ""}
  <div class="small muted" style="margin-top:10px">QUARTERLY RESULTS (₹ cr)</div><div class="tablewrap"><table class="mini plain"><thead><tr><th>Quarter</th><th class="n">Sales</th><th class="n">Op. profit</th><th class="n">OPM %</th><th class="n">Net profit</th><th class="n">EPS</th></tr></thead><tbody>${fd.quarters.map((q) => `<tr><td>${esc(q.quarter)}</td><td class="n">${fmt(q.sales, 0)}</td><td class="n">${fmt(q.operating_profit, 0)}</td><td class="n">${fmt(q.opm, 0)}</td><td class="n">${fmt(q.net_profit, 0)}</td><td class="n">${fmt(q.eps)}</td></tr>`).join("")}</tbody></table></div>
  ${peers.length ? `<div class="small muted" style="margin-top:10px">PEERS</div><div class="tablewrap"><table class="mini plain"><thead><tr><th>Company</th><th class="n">P/E</th><th class="n">M.cap cr</th><th class="n">ROCE</th><th class="n">Qtr profit var</th></tr></thead><tbody>${peers.map((p) => `<tr><td>${esc(p.company)}</td><td class="n">${fmt(p.pe, 1)}</td><td class="n">${fmt(p.mcap_cr, 0)}</td><td class="n">${fmt(p.roce, 1)}</td><td class="n ${cls(p.qtr_profit_var)}">${pct(p.qtr_profit_var, 0)}</td></tr>`).join("")}${fd.peers.median ? `<tr><td><b>Median</b></td><td class="n"><b>${fmt(fd.peers.median.pe, 1)}</b></td><td class="n">${fmt(fd.peers.median.mcap_cr, 0)}</td><td class="n">${fmt(fd.peers.median.roce, 1)}</td><td class="n">${pct(fd.peers.median.qtr_profit_var, 0)}</td></tr>` : ""}</tbody></table></div>` : ""}
  ${(fd.source_cons || []).length || (fd.source_pros || []).length ? `<div class="small muted" style="margin-top:10px">SOURCE NOTES (machine-generated by Screener)</div>${fd.source_pros.map((x) => `<div class="small up">+ ${esc(x)}</div>`).join("")}${fd.source_cons.map((x) => `<div class="small down">− ${esc(x)}</div>`).join("")}` : ""}
  <div class="dim small" style="margin-top:8px">${esc(fd.source)} · <a href="${esc(fd.source_url)}" target="_blank" rel="noopener">source page</a> · fetched ${ago(fd.fetched)}. ${esc(fd.disclaimer)}</div>`;
}

// ------------------------------------------------------------------ LONG-TERM
async function viewLongterm() {
  app.innerHTML = `${disclaimer}<div class="card"><h3>Long-term investing watchlist <span class="sub">fundamentals only: kept separate from intraday and swing signals</span></h3><div id="lt"><div class="loading"><span class="spinner"></span> Loading fundamentals for your watchlist (one source request per stock, cached 12h)…</div></div></div>`;
  try {
    const my = state.route;
    const r = await api("/longterm");
    if (my !== state.route) return;
    $("#lt").innerHTML = r.rows.length ? `<div class="tablewrap"><table><thead><tr><th>Stock</th><th>Fundamentals</th><th>Valuation</th><th class="n">P/E</th><th class="n">P/B</th><th class="n">ROE</th><th class="n">ROCE</th><th class="n">D/E</th><th class="n">Sales 3y</th><th class="n">Profit 3y</th><th class="n">Qtr profit YoY</th><th class="n">Promoters</th><th class="n">M.cap cr</th></tr></thead><tbody>${r.rows.map((x) => x.error ? `<tr data-s="${esc(x.symbol)}"><td><b>${esc(x.symbol)}</b></td><td colspan="12" class="dim">${esc(x.error)}</td></tr>` : `
      <tr data-s="${esc(x.symbol)}"><td><b>${esc(x.symbol)}</b><div class="dim small">${esc((x.name || "").slice(0, 30))}</div></td><td><span class="rating ${x.classification === "Strong fundamentals" ? "r-bull" : x.classification === "Weak fundamentals" ? "r-bear" : "r-wait"}">${esc(x.classification)}</span></td>
      <td title="${esc(x.valuation_note || "")}"><span class="rating ${/Under/.test(x.valuation) ? "r-bull" : /Over/.test(x.valuation) ? "r-bear" : "r-wait"}">${esc(x.valuation)}</span></td><td class="n">${fmt(x.pe, 1)}</td><td class="n">${fmt(x.pb, 2)}</td><td class="n">${fmt(x.roe, 1)}</td><td class="n">${x.is_financial ? "n/a" : fmt(x.roce, 1)}</td><td class="n">${x.is_financial ? "n/a" : fmt(x.debt_to_equity, 2)}</td>
      <td class="n">${pct(x.sales_growth_3y, 0)}</td><td class="n">${pct(x.profit_growth_3y, 0)}</td><td class="n ${cls(x.qtr_profit_yoy)}">${pct(x.qtr_profit_yoy, 0)}</td><td class="n">${fmt(x.promoters, 1)}</td><td class="n">${fmt(x.market_cap_cr, 0)}</td></tr>`).join("")}</tbody></table></div><div class="dim small" style="margin-top:6px">${esc(r.note)} Click a row for the full long-term view.</div>`
      : `<div class="empty">Your watchlist is empty. Add stocks from the Watchlist tab or any stock page.</div>`;
    bindRows($("#lt"), "longterm");
  } catch (e) { $("#lt").innerHTML = errorBox(e); }
}

// ------------------------------------------------------------------ NEWS
async function viewNews() {
  app.innerHTML = `${disclaimer}<div class="card"><h3>News &amp; market sentiment <span class="sub" id="nw-meta"></span></h3>
    <div class="row" style="margin-bottom:10px"><div class="seg" id="nw-kind">${[["", "All"], ["exchange", "NSE announcements"], ["results", "Results"], ["regulator", "RBI / SEBI"], ["media", "Market news"]].map(([k, l], i) => `<button class="${i ? "" : "on"}" data-k="${k}">${l}</button>`).join("")}</div>
    <select id="nw-h"><option value="24">Last 24h</option><option value="72" selected>Last 72h</option><option value="168">Last 7 days</option></select></div><div id="nw"></div></div>`;
  let kind = "";
  const load = async () => {
    try {
      const r = await api(`/news?hours=${$("#nw-h").value}${kind ? "&kind=" + kind : ""}`);
      const st = r.status;
      $("#nw-meta").textContent = `${r.items.length} items · ${st.feeds.filter((f) => !f.error).length}/${st.feeds.length} feeds OK`;
      $("#nw").innerHTML = newsList(r.items, 200) + `<div class="dim small" style="margin-top:8px">Sentiment is a keyword lexicon (hover the label for the words), not AI understanding. ${st.unavailable.map((u) => esc(u.name + ": " + u.why)).join(" ")}</div>`;
    } catch (e) { $("#nw").innerHTML = errorBox(e); }
  };
  $("#nw-kind").querySelectorAll("button").forEach((b) => b.onclick = () => { $("#nw-kind").querySelectorAll("button").forEach((x) => x.classList.remove("on")); b.classList.add("on"); kind = b.dataset.k; load(); });
  $("#nw-h").onchange = load;
  load(); every(120000, load);
}

// ------------------------------------------------------------------ WATCHLIST & ALERTS
async function viewWatchlist() {
  app.innerHTML = `${disclaimer}<div class="grid g3"><div class="card span2"><h3>My watchlist</h3>
    <div class="row" style="margin-bottom:10px"><input id="wl-sym" placeholder="NSE symbol, e.g. INFY"><button class="btn primary" id="wl-btn">Add</button><button class="btn" id="wl-refresh">↻ Re-analyse</button><span class="dim small">Watchlist stocks are re-analysed by every background scan and drive your alerts.</span></div><div id="wl"></div></div>
    <div class="card"><h3>Alerts <button class="btn sm" id="al-seen">Mark all read</button></h3><div id="al-tg" class="small"></div><div id="al"></div></div></div>`;
  const load = async (refresh) => {
    try {
      if (refresh) await api("/scan?universe=watchlist&mode=swing&refresh=true");
      const w = await api("/watchlist");
      const rows = w.map((x) => x.analysis).filter(Boolean);
      const missing = w.filter((x) => !x.analysis).map((x) => x.symbol);
      $("#wl").innerHTML = (rows.length ? scanTable(rows, { mode: "swing", fresh: true }) : "") + (missing.length ? `<div class="dim small" style="margin-top:6px">Analysing: ${missing.map(esc).join(", ")}… (refreshes automatically)</div>` : "") + (!w.length ? `<div class="empty">Empty. Add a symbol above.</div>` : "") +
        (w.length ? `<div class="row" style="margin-top:8px">${w.map((x) => `<span class="tag">${esc(x.symbol)} <a href="#" data-del="${esc(x.symbol)}" title="Remove">✕</a></span>`).join("")}</div>` : "");
      bindRows($("#wl"), "swing");
      $("#wl").querySelectorAll("[data-del]").forEach((a) => a.onclick = async (e) => { e.preventDefault(); await api("/watchlist/" + a.dataset.del, { method: "DELETE" }); load(); });
      if (missing.length) setTimeout(() => { if ($("#wl")) load(); }, 8000);
    } catch (e) { $("#wl").innerHTML = errorBox(e); }
  };
  const loadAlerts = async () => {
    const r = await api("/alerts");
    $("#al-tg").innerHTML = r.telegram ? `Telegram: <span class="up">configured</span> <button class="btn sm" id="tg-test">Send test</button>` : `Telegram: not configured. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in <code>.env</code> (see README).`;
    if ($("#tg-test")) $("#tg-test").onclick = async () => { const t = await api("/alerts/test-telegram", { method: "POST" }); toast(esc(t.message)); };
    $("#al").innerHTML = r.alerts.length ? r.alerts.map((a) => `<div class="alert-row" style="${a.seen ? "opacity:.6" : ""}"><span class="lv lv-${esc(a.level)}"></span><div><div class="small"><b>${esc(a.kind.replace(/_/g, " "))}</b> ${a.symbol ? `<a href="#/stock/${esc(a.symbol)}">${esc(a.symbol)}</a>` : ""} <span class="dim">${istTime(a.ts)}${a.telegram ? " · sent to Telegram" : ""}</span></div><div class="small">${esc(a.message)}</div></div></div>`).join("") : `<div class="empty small">No alerts yet. They ring once each: buy zone, breakout, stop approaching, target, volume spike, news, trend reversal.</div>`;
  };
  $("#wl-btn").onclick = async () => { const s = $("#wl-sym").value.trim(); if (!s) return; try { await api("/watchlist", { method: "POST", body: { symbol: s } }); $("#wl-sym").value = ""; toast(esc(s.toUpperCase()) + " added"); load(); } catch (e) { toast(esc(e.message)); } };
  $("#wl-refresh").onclick = () => { $("#wl").innerHTML = `<div class="loading"><span class="spinner"></span> Re-analysing…</div>`; load(true); };
  $("#al-seen").onclick = async () => { await api("/alerts/seen", { method: "POST", body: {} }); loadAlerts(); refreshHeader(); };
  load(); loadAlerts(); every(60000, loadAlerts);
}

// ------------------------------------------------------------------ PAPER
// exit check (HOLD / TIGHTEN STOP / BOOK PARTIAL / EXIT) for an open paper position
const EXIT_CLS = { HOLD: "d-TRADE", "TIGHTEN STOP": "d-WAIT", "BOOK PARTIAL": "d-INFO", EXIT: "d-NO" };
function exitBadge(c) {
  if (!c || !c.verdict) return '<span class="dim small">–</span>';
  return `<span class="exit-pill ${EXIT_CLS[c.verdict]}">${esc(c.verdict)}</span>`;
}
function exitCard(x) {
  const c = x.exit_check || {};
  const why = (c.reasons || []).map(([tone, t]) => `<li class="x-${esc(tone)}">${esc(t)}</li>`).join("");
  const stats = [
    c.r_multiple != null ? `<span><b>${c.r_multiple > 0 ? "+" : ""}${fmt(c.r_multiple, 1)}R</b> so far</span>` : "",
    c.pnl_pct != null ? `<span class="${cls(c.pnl_pct)}">${pct(c.pnl_pct)}</span>` : "",
    c.sessions_held != null ? `<span>${c.sessions_held} session${c.sessions_held === 1 ? "" : "s"} held</span>` : "",
    c.basis ? `<span class="dim">${esc(c.basis)}</span>` : ""].filter(Boolean).join("");
  return `<div class="exit-card">
    <div class="row between"><div class="row"><b>${esc(x.symbol)}</b> ${exitBadge(c)}</div><div class="exit-stats">${stats}</div></div>
    <ul class="exit-why">${why}</ul>
    ${c.suggested_stop ? `<div class="row"><button class="btn sm good" data-stop="${x.id}" data-to="${c.suggested_stop}">Raise stop to ₹${fmt(c.suggested_stop)}</button>
      <span class="dim small">Stops only ever move up.</span></div>` : ""}
  </div>`;
}

async function viewPaper() {
  app.innerHTML = `${disclaimer}<div id="pp"><div class="loading"><span class="spinner"></span> Loading paper portfolio…</div></div>`;
  const load = async () => {
    const my = state.route;
    const p = await api("/paper");
    if (my !== state.route || !$("#pp")) return;
    const st = p.stats;
    const pending = p.orders.filter((o) => o.status === "proposed");
    const queued = p.orders.filter((o) => o.status === "queued");
    $("#pp").innerHTML = `<div class="grid g4">
      <div class="card idx"><span class="name">Cash (paper)</span><div class="px">${inr(p.cash)}</div><div class="dim small">of ${inr(p.capital)} capital</div></div>
      <div class="card idx"><span class="name">Realised P&amp;L</span><div class="px ${cls(st.realised_pnl)}">${inr(st.realised_pnl)}</div><div class="dim small">after ₹${fmt(st.charges, 0)} charges</div></div>
      <div class="card idx"><span class="name">Unrealised P&amp;L</span><div class="px ${cls(st.unrealised_pnl)}">${inr(st.unrealised_pnl)}</div><div class="dim small">${p.open.length} open position(s), delayed prices</div></div>
      <div class="card idx"><span class="name">Win rate</span><div class="px">${st.win_rate == null ? "–" : st.win_rate + "%"}</div><div class="dim small">${st.trades} closed trade(s)</div></div></div>
      <div style="height:14px"></div><div class="grid g2">
      <div class="card spanall"><h3>Open positions</h3>${p.open.length ? `<div class="tablewrap"><table class="mini plain"><thead><tr><th>Stock</th><th>Mode</th><th class="n">Qty</th><th class="n">Entry</th><th class="n">Now</th><th class="n">Stop</th><th class="n">T1</th><th class="n">T2</th><th class="n">P&amp;L</th><th>Exit check</th><th></th></tr></thead><tbody>${p.open.map((x) => `<tr><td><a href="#/stock/${esc(x.symbol)}"><b>${esc(x.symbol)}</b></a></td><td>${esc(x.mode)}</td><td class="n">${x.qty}</td><td class="n">${fmt(x.entry_price)}</td><td class="n">${fmt(x.price)} ${freshBadge(x.freshness)}</td><td class="n down">${fmt(x.stop)}</td><td class="n">${fmt(x.target1)}</td><td class="n">${fmt(x.target2)}</td><td class="n ${cls(x.unrealised)}">${inr(x.unrealised)}</td><td>${exitBadge(x.exit_check)}</td><td><button class="btn sm bad" data-close="${x.id}">Close</button></td></tr>`).join("")}</tbody></table></div>
        <div class="exit-list">${p.open.map(exitCard).join("")}</div>` : `<div class="empty small">No open paper positions. Propose one from a stock's trade plan.</div>`}
        ${pending.length ? `<hr><h3>Awaiting your yes</h3>${pending.map((o) => `<div class="small" style="margin-bottom:8px">#${o.id} BUY ${o.qty} ${esc(o.symbol)} entry ${fmt(o.entry)} SL ${fmt(o.stop)} T1 ${fmt(o.target1)} <div class="row"><input data-yes="${o.id}" placeholder='type "yes"'><button class="btn sm good" data-ok="${o.id}">Confirm</button><button class="btn sm bad" data-no="${o.id}">Decline</button></div></div>`).join("")}` : ""}
        ${queued.length ? `<hr><h3>Approved, queued for the next open</h3>${queued.map((o) => `<div class="small">#${o.id} BUY ${o.qty} ${esc(o.symbol)} in ${fmt(o.entry)} range, SL ${fmt(o.stop)} <button class="btn sm bad" data-no="${o.id}">Cancel</button></div>`).join("")}` : ""}</div>
      <div class="card"><h3>Equity curve <span class="sub">realised, paper</span></h3>${p.equity_curve.length > 1 ? `<div class="chart sm" id="eq" style="height:220px"></div>` : `<div class="empty small">Appears after two closed trades.</div>`}</div>
      <div class="card"><h3>Closed trades</h3>${p.closed.length ? `<div class="tablewrap" style="max-height:320px"><table class="mini plain"><thead><tr><th>Closed</th><th>Stock</th><th class="n">Qty</th><th class="n">Entry</th><th class="n">Exit</th><th>Reason</th><th class="n">P&amp;L</th></tr></thead><tbody>${p.closed.map((x) => `<tr><td>${istTime(x.closed_at)}</td><td>${esc(x.symbol)}</td><td class="n">${x.qty}</td><td class="n">${fmt(x.entry_price)}</td><td class="n">${fmt(x.exit_price)}</td><td>${esc(x.exit_reason)}</td><td class="n ${cls(x.pnl)}">${inr(x.pnl)}</td></tr>`).join("")}</tbody></table></div>` : `<div class="empty small">None yet.</div>`}</div>
      <div class="card"><h3>Trade journal</h3><div class="row" style="margin-bottom:8px"><input id="jr" placeholder="Write a note (why you took / skipped a trade)…" style="flex:1"><button class="btn" id="jr-btn">Add</button></div>
        <div style="max-height:300px;overflow:auto">${p.journal.map((j) => `<div class="small" style="padding:5px 0;border-bottom:1px solid var(--line)"><span class="dim">${istTime(j.ts)}</span> ${j.symbol ? `<b>${esc(j.symbol)}</b>` : ""} ${esc(j.text)}</div>`).join("") || '<div class="empty small">Empty.</div>'}</div></div>
      <div class="card spanall"><h3>Order log</h3>${p.orders.length ? `<div class="tablewrap" style="max-height:300px"><table class="mini plain"><thead><tr><th>When</th><th>#</th><th>Stock</th><th class="n">Qty</th><th class="n">Entry</th><th class="n">Stop</th><th>Status</th><th>Risk check</th><th>Your reply</th></tr></thead><tbody>${p.orders.map((o) => { const rv = JSON.parse(o.risk_verdict || "{}"); return `<tr><td>${istTime(o.ts)}</td><td>${o.id}</td><td>${esc(o.symbol)}</td><td class="n">${o.qty}</td><td class="n">${fmt(o.entry)}</td><td class="n">${fmt(o.stop)}</td><td>${esc(o.status)}</td><td title="${esc((rv.failed || []).join("; "))}">${esc(rv.verdict || "")}</td><td>${esc(o.approval || "")}</td></tr>`; }).join("")}</tbody></table></div>` : `<div class="empty small">No orders yet.</div>`}</div></div>`;
    if ($("#eq")) lineChart($("#eq"), p.equity_curve, cssVar("--accent"), { height: 220 });
    $("#pp").querySelectorAll("[data-close]").forEach((b) => b.onclick = async () => { if (!confirm("Close this paper position at the latest (delayed) price?")) return; const r = await api("/paper/close", { method: "POST", body: { position_id: +b.dataset.close } }); toast(r.ok ? `Closed, P&L ₹${fmt(r.pnl)}` : esc(r.error)); load(); });
    $("#pp").querySelectorAll("[data-ok]").forEach((b) => b.onclick = async () => { const reply = $(`[data-yes="${b.dataset.ok}"]`).value; const r = await api("/paper/confirm", { method: "POST", body: { order_id: +b.dataset.ok, reply } }); toast(r.ok ? (r.queued ? esc(r.note) : `Filled at ₹${fmt(r.fill_price)}`) : esc(r.error), 7000); load(); });
    $("#pp").querySelectorAll("[data-no]").forEach((b) => b.onclick = async () => { await api("/paper/decline", { method: "POST", body: { order_id: +b.dataset.no } }); load(); });
    $("#pp").querySelectorAll("[data-stop]").forEach((b) => b.onclick = async () => {
      const r = await api("/paper/stop", { method: "POST", body: { position_id: +b.dataset.stop, stop: +b.dataset.to } });
      toast(r.ok ? `Stop raised to ₹${fmt(r.stop)}` : esc(r.error)); load();
    });
    $("#jr-btn").onclick = async () => { const t = $("#jr").value.trim(); if (!t) return; await api("/journal", { method: "POST", body: { text: t } }); load(); };
  };
  try { await load(); } catch (e) { $("#pp").innerHTML = errorBox(e); }
}

// ------------------------------------------------------------------ SETTINGS
async function viewSettings() {
  const my = state.route;
  const s = await api("/settings");
  if (my !== state.route) return;
  const field = (k, label, step = "any", hint = "") => `<label class="f">${label}<input type="number" step="${step}" id="s-${k}" value="${esc(s[k])}">${hint ? `<span class="dim">${hint}</span>` : ""}</label>`;
  app.innerHTML = `${disclaimer}<div class="grid g2"><div class="card"><h3>My capital &amp; risk limits</h3>
    <div class="filters" style="grid-template-columns:repeat(auto-fill,minmax(200px,1fr))">
      ${field("capital", "Total trading capital (₹)", "1000")}${field("cash", "Available cash (₹, paper)", "100")}
      ${field("risk_per_trade_pct", "Max risk per trade (% of capital)", "0.1", "₹" + fmt(s.capital * s.risk_per_trade_pct / 100, 0) + " now")}
      ${field("max_daily_loss_pct", "Max daily loss (% of capital)", "0.1", "₹" + fmt(s.capital * s.max_daily_loss_pct / 100, 0) + " now")}
      ${field("max_trades_per_day", "Max trades per day", "1")}${field("max_position_pct", "Max position size (% of capital)", "1")}
      ${field("min_reward_risk", "Minimum reward : risk", "0.1")}${field("slippage_pct", "Slippage assumption (% per fill)", "0.01")}
      ${field("brokerage_per_order", "Brokerage per order (₹ cap)", "1")}
      <label class="f">Preferred trading style<select id="s-style">${["intraday", "swing", "longterm"].map((x) => `<option ${x === s.style ? "selected" : ""}>${x}</option>`).join("")}</select></label>
      <label class="f">Intraday: no entries before<input id="s-intraday_no_entry_before" value="${esc(s.intraday_no_entry_before)}"></label>
      <label class="f">Intraday: no entries after<input id="s-intraday_no_entry_after" value="${esc(s.intraday_no_entry_after)}"></label>
      <label class="f">Intraday: square off by<input id="s-intraday_flat_by" value="${esc(s.intraday_flat_by)}"></label>
    </div><div class="row" style="margin-top:12px"><button class="btn primary" id="s-save">Save limits</button><span id="s-msg" class="small"></span></div>
    <hr><div class="small muted">LOCKED SAFETY SETTINGS</div><div class="small">Paper trading: <b class="up">ON (cannot be turned off here)</b> · Short selling: <b>off</b> · Margin / leverage: <b>off</b> · Options: <b>off</b>. This app never connects to a live broker order API.</div></div>
    <div class="card"><h3>Position size calculator</h3><div class="filters"><label class="f">Entry ₹<input id="c-e" type="number" step="any"></label><label class="f">Stop ₹<input id="c-s" type="number" step="any"></label><label class="f">Target ₹ (optional)<input id="c-t" type="number" step="any"></label><label class="f">Product<select id="c-p"><option value="delivery">Delivery (swing)</option><option value="intraday">Intraday</option></select></label><button class="btn primary" id="c-go">Calculate</button></div><div id="c-out" style="margin-top:12px"></div></div></div>`;
  $("#s-save").onclick = async () => {
    const body = {}; ["capital", "cash", "risk_per_trade_pct", "max_daily_loss_pct", "max_trades_per_day", "max_position_pct", "min_reward_risk", "slippage_pct", "brokerage_per_order"].forEach((k) => body[k] = $("#s-" + k).value);
    ["style", "intraday_no_entry_before", "intraday_no_entry_after", "intraday_flat_by"].forEach((k) => body[k] = $("#s-" + k).value);
    try { await api("/settings", { method: "PUT", body }); $("#s-msg").innerHTML = `<span class="up">Saved. New plans and checks use these limits.</span>`; } catch (e) { $("#s-msg").innerHTML = `<span class="down">${esc(e.message)}</span>`; }
  };
  $("#c-go").onclick = async () => {
    try {
      const r = await api("/size", { method: "POST", body: { entry: $("#c-e").value, stop: $("#c-s").value, target: $("#c-t").value || null, product: $("#c-p").value } });
      $("#c-out").innerHTML = r.refused ? `<div class="notrade small">${esc(r.refused)}</div>` : `<div class="kv"><div><span>Quantity</span><b>${r.qty}</b></div><div><span>Capital required</span><b>${inr(r.capital_required)}</b></div><div><span>Stop-loss per share</span><b>${inr(r.risk_per_share, 2)}</b></div><div><span>Max loss</span><b class="down">${inr(r.max_loss)}</b></div><div><span>% of capital</span><b>${fmt(r.max_loss_pct_capital, 2)}%</b></div><div><span>Potential reward</span><b class="up">${inr(r.potential_reward)}</b></div><div><span>Net reward : risk</span><b>${fmt(r.net_reward_risk, 2)}</b></div><div><span>Charges (est.)</span><b>${inr(r.est_costs?.total, 2)}</b></div></div><div class="dim small" style="margin-top:6px">Decided by: ${esc(r.decided_by)}. ${esc(r.note)}</div>`;
    } catch (e) { $("#c-out").innerHTML = errorBox(e); }
  };
}

// ------------------------------------------------------------------ STATUS
async function viewStatus() {
  const my = state.route;
  const s = await api("/status");
  if (my !== state.route) return;
  const pr = s.providers;
  app.innerHTML = `<div class="grid g2"><div class="card"><h3>Data providers</h3>
    <table class="mini plain"><tbody>
    <tr><td><b>Prices</b></td><td>${esc(pr.prices.name)}<div class="dim small">${esc(pr.prices.type)}. ${esc(pr.prices.label_rule)}</div><div class="small">Requests ${pr.prices.stats.requests}, failures ${pr.prices.stats.failures}, last success ${ago(pr.prices.stats.last_success)}</div></td></tr>
    <tr><td><b>Index lists</b></td><td>${esc(pr.index_lists.name)} <span class="dim small">${esc(pr.index_lists.type)}</span></td></tr>
    <tr><td><b>Fundamentals</b></td><td>${esc(pr.fundamentals.name)} <span class="dim small">${esc(pr.fundamentals.type)} · ${pr.fundamentals.enabled ? "enabled" : "disabled"}</span></td></tr>
    <tr><td><b>Broker APIs</b></td><td>${Object.entries(pr.brokers).map(([k, v]) => `<div class="small">${esc(k)}: ${esc(v)}</div>`).join("")}<div class="dim small">A licensed broker feed (Kite Connect / Upstox / SmartAPI) is needed for true real-time data. See README.</div></td></tr>
    </tbody></table></div>
    <div class="card"><h3>News feeds</h3>${pr.news.feeds.map((f) => `<div class="small"><span class="dot ${f.error ? "off" : f.last_fetch ? "" : "warn"}"></span> ${esc(f.name)} <span class="dim">${f.error ? esc(f.error) : "fetched " + ago(f.last_fetch)}</span></div>`).join("")}${pr.news.unavailable.map((u) => `<div class="small"><span class="dot off"></span> ${esc(u.name)} <span class="dim">${esc(u.why)}</span></div>`).join("")}</div>
    <div class="card"><h3>Scans</h3>${s.scans.length ? s.scans.map((x) => `<div class="small">${esc(x.universe)} · ${esc(x.mode)} · ${x.analysed} analysed · ${x.errors} errors · ${esc(x.finished_ist || "")}</div>`).join("") : '<div class="dim small">No scan finished yet.</div>'}${s.running.length ? `<div class="small"><span class="spinner"></span> running: ${s.running.map((r) => esc(r.join("/"))).join(", ")}</div>` : ""}
      <div class="small muted" style="margin-top:8px">Background: ${s.background.cycles} cycles · last scan ${ago(s.background.last_scan)} · last news ${ago(s.background.last_news)} · last intraday ${ago(s.background.last_intraday)}</div></div>
    <div class="card"><h3>Recent errors</h3>${Object.entries(pr.prices.recent_errors).map(([k, v]) => `<div class="small"><b>${esc(k)}</b> ${esc(v.error)} <span class="dim">${ago(v.ts)}</span></div>`).join("") + s.background.errors.map((e) => `<div class="small"><b>${esc(e.where)}</b> ${esc(e.error)} <span class="dim">${ago(e.ts)}</span></div>`).join("") || '<div class="dim small">None.</div>'}</div></div>`;
}

// ------------------------------------------------------------------ admin panel (owner only)
function verdictOf(seg, minSample) {
  const d = seg.direction || {};
  if (!d.calls || d.calls < minSample) return { cls: "d-WAIT", text: "Too few calls to judge", sub: `${d.calls || 0} of ${minSample} needed` };
  const edge = seg.bullish_edge_pct;
  if (edge == null) return { cls: "d-WAIT", text: "No bullish calls yet", sub: "" };
  if (edge >= 3) return { cls: "d-TRADE", text: "Better than chance", sub: `bullish calls right ${fmt(seg.bullish_hit_pct, 1)}% vs ${fmt(seg.base_up_pct, 1)}% base rate` };
  if (edge <= -3) return { cls: "d-NO", text: "Worse than chance", sub: `bullish calls right ${fmt(seg.bullish_hit_pct, 1)}% vs ${fmt(seg.base_up_pct, 1)}% base rate` };
  return { cls: "d-WAIT", text: "About a coin flip", sub: `bullish calls right ${fmt(seg.bullish_hit_pct, 1)}% vs ${fmt(seg.base_up_pct, 1)}% base rate` };
}

function accuracyBlock(title, rep, minSample, intro) {
  if (!rep || !rep.segments) return "";
  const cards = rep.segments.map((s) => {
    const v = verdictOf(s, minSample), d = s.direction || {};
    return `<div class="acc-card"><div class="dim small">${esc(s.name)}</div>
      <div class="acc-big">${d.hit_pct == null ? "–" : fmt(d.hit_pct, 1) + "%"}</div>
      <div class="small muted">of ${d.calls || 0} up/down calls right</div>
      <div style="margin-top:10px"><span class="exit-pill ${v.cls}">${esc(v.text)}</span></div>
      <div class="dim small" style="margin-top:6px">${esc(v.sub)}</div></div>`;
  }).join("");
  const tables = rep.segments.filter((s) => s.rows.length).map((s) => `
    <h4 class="acc-h">${esc(s.name)} <span class="dim small">· ${esc(s.note)} · base rate: ${s.base_up_pct == null ? "–" : fmt(s.base_up_pct, 1) + "% of stocks went up"}</span></h4>
    <div class="tablewrap"><table class="plain"><thead><tr><th>Rating</th><th class="n">Times given</th><th class="n">Right</th>
      <th class="n">Avg move after</th><th class="n">Vs NIFTY</th><th class="n">Beat NIFTY</th></tr></thead><tbody>
      ${s.rows.map((r) => `<tr><td>${ratingBadge(r.rating)}</td><td class="n">${r.n}</td>
        <td class="n">${r.hit_pct == null ? '<span class="dim">no direction</span>' : `<b>${fmt(r.hit_pct, 1)}%</b>${r.enough ? "" : ' <span class="dim small">(few)</span>'}`}</td>
        <td class="n ${cls(r.avg_ret_pct)}">${pct(r.avg_ret_pct)}</td><td class="n ${cls(r.avg_vs_nifty_pct)}">${pct(r.avg_vs_nifty_pct)}</td>
        <td class="n">${r.beat_nifty_pct == null ? "–" : fmt(r.beat_nifty_pct, 1) + "%"}</td></tr>`).join("")}
    </tbody></table></div>`).join("");
  return `<div class="card spanall"><h3>${title}</h3><p class="muted" style="margin-top:0">${intro}</p>
    <div class="acc-grid">${cards}</div>${tables || '<div class="empty" style="margin-top:18px">No results yet.</div>'}</div>`;
}

function ratingBadge(r) {
  const c = { "STRONG BULLISH": "r-strong", BULLISH: "r-bull", WAIT: "r-wait", "HIGH RISK": "r-risk", BEARISH: "r-bear", AVOID: "r-avoid" }[r] || "r-na";
  return `<span class="rating ${c}">${esc(r)}</span>`;
}

async function viewAdmin() {
  const my = state.route;
  app.innerHTML = `<div class="loading"><span class="spinner"></span> Loading admin panel…</div>`;
  const [o, acc] = await Promise.all([api("/admin/overview"), api("/admin/accuracy")]);
  if (my !== state.route) return;
  const k = o.kpis, h = o.health;
  const when = (ts) => ts ? new Date(ts * 1000).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" }) : "–";
  const ago2 = (ts) => ts ? ago(ts) : "never";
  const kpi = (label, value, sub = "") => `<div class="card idx"><span class="name">${label}</span><div class="px">${value}</div><div class="dim small">${sub}</div></div>`;
  const maxSign = Math.max(1, ...o.signups_by_day.map(([, n]) => n));
  const seg = o.paper_by_segment;
  app.innerHTML = `<div class="grid">
    <div class="grid g4">
      ${kpi("Members", k.members, `${k.pending} waiting for approval`)}
      ${kpi("Active", k.active_24h + " today", `${k.active_7d} in the last 7 days`)}
      ${kpi("Joined (7 days)", k.joined_7d, `invites: ${k.invites_used} used, ${k.invites_unused} unused`)}
      ${kpi("Paper trades", k.closed_trades + " closed", `${k.open_positions} open · ${k.paper_orders} orders`)}
    </div>

    <div id="acc-area"></div>

    <div class="card spanall"><h3>Paper-trade results by segment <span class="sub">everyone's closed trades</span></h3>
      ${Object.keys(seg).length ? `<div class="tablewrap"><table class="plain"><thead><tr><th>Segment</th><th class="n">Trades</th><th class="n">Win rate</th>
        <th class="n">Total P&amp;L</th><th class="n">Avg per trade</th><th class="n">Avg R</th><th>How they ended</th></tr></thead><tbody>
        ${Object.entries(seg).map(([m, s]) => `<tr><td><b>${esc(m)}</b></td><td class="n">${s.trades}</td><td class="n">${s.win_rate == null ? "–" : fmt(s.win_rate, 1) + "%"}</td>
          <td class="n ${cls(s.pnl)}">${inr(s.pnl)}</td><td class="n ${cls(s.avg_pnl)}">${inr(s.avg_pnl)}</td><td class="n ${cls(s.avg_r)}">${s.avg_r == null ? "–" : fmt(s.avg_r, 2) + "R"}</td>
          <td class="small">${Object.entries(s.exit_reasons).map(([r, n]) => `${esc(r)} ×${n}`).join(", ")}</td></tr>`).join("")}
        </tbody></table></div>` : '<div class="empty">No closed paper trades yet.</div>'}
    </div>

    <div class="card spanall"><h3>People <span class="sub">${o.people.length} including you</span></h3>
      <div class="tablewrap"><table class="plain"><thead><tr><th>Name</th><th>Mobile / email</th><th>Status</th><th>Invite</th><th>Joined</th>
        <th>Last active</th><th class="n">Watchlist</th><th class="n">Open</th><th class="n">Closed</th><th class="n">Win rate</th><th class="n">Realised P&amp;L</th></tr></thead><tbody>
        ${o.people.map((p) => `<tr><td><b>${esc(p.name)}</b></td><td class="mono small">${esc(p.id)}${p.contact_email ? `<div class="dim">${esc(p.contact_email)}</div>` : ""}</td>
          <td>${p.status === "owner" ? '<span class="tag b">admin</span>' : p.status === "pending" ? '<span class="tag w">pending</span>' : '<span class="tag">member</span>'}</td>
          <td class="small">${esc(p.invite_note || "–")}</td><td class="small">${when(p.created_at)}</td>
          <td class="small">${p.status === "owner" ? "–" : ago2(Math.max(p.last_seen || 0, p.last_login || 0) || null)}</td>
          <td class="n">${p.watchlist}</td><td class="n">${p.open_positions}</td><td class="n">${p.closed_trades}</td>
          <td class="n">${p.win_rate == null ? "–" : fmt(p.win_rate, 1) + "%"}</td><td class="n ${cls(p.realised_pnl)}">${inr(p.realised_pnl)}</td></tr>`).join("")}
      </tbody></table></div>
      <p class="dim small" style="margin-bottom:0">Manage invites, approvals and passwords on the <a href="#/members">Members</a> tab.</p>
    </div>

    <div class="grid g2">
      <div class="card"><h3>Sign-ups by day <span class="sub">last 30 days with sign-ups</span></h3>
        ${o.signups_by_day.length ? `<div class="bars">${o.signups_by_day.map(([d, n]) => `<div class="bar-row"><span class="dim small mono">${esc(d.slice(5))}</span>
          <div class="bar-track"><i style="width:${Math.round(100 * n / maxSign)}%"></i></div><b class="small">${n}</b></div>`).join("")}</div>`
          : '<div class="empty">No sign-ups yet.</div>'}</div>
      <div class="card"><h3>System health</h3><div class="kv">
        <div><span>Database</span><b>${esc(h.storage)}</b></div>
        <div><span>Email</span><b>${esc(h.email.provider)}</b></div>
        <div><span>Market</span><b>${esc(h.market.state || (h.market.open ? "open" : "closed"))}</b></div>
        <div><span>Ratings stored</span><b>${h.signals_stored ?? "–"}</b></div>
        <div><span>Background cycles</span><b>${h.background.cycles ?? "–"}</b></div>
        <div><span>Last scan</span><b class="small">${h.background.last_scan ? ago(h.background.last_scan) : "not yet"}</b></div></div>
        ${h.scans.length ? `<div class="small muted" style="margin-top:14px">${h.scans.map((s) => `${esc(s.universe)} (${esc(s.mode)}): ${s.analysed ?? 0} stocks, ${esc(s.finished_ist || "running")}${s.errors ? `, ${s.errors} errors` : ""}`).join("<br>")}</div>` : ""}
        ${h.background_errors.length ? `<div class="small down" style="margin-top:12px">${h.background_errors.map((e) => `${esc(e.where)}: ${esc(e.error)} (${ago(e.ts)})`).join("<br>")}</div>` : '<div class="small up" style="margin-top:12px">No background errors.</div>'}
      </div>
    </div></div>`;
  renderAccuracy(acc);
}

function renderAccuracy(acc) {
  const area = $("#acc-area");
  if (!area) return;
  const my = state.route;
  const r = acc.result;
  const head = `<div class="card spanall acc-head"><div class="row between"><div><h3 style="margin:0">How accurate is the site?</h3>
      <div class="muted small" style="margin-top:6px">A call is <b>right</b> when the price moved its way: bullish ratings if the price went up, bearish ratings if it went down.
      <b>Base rate</b> is how often any stock went up over the same windows; a call only adds value when it beats that.
      Past accuracy does not promise future results.</div></div>
      <div class="row">${acc.status === "running" ? '<span class="small muted"><span class="spinner"></span> Calculating (about a minute)…</span>' :
        `<span class="dim small">${r ? "Updated " + esc(r.computed_ist) : ""}</span><button class="btn sm" id="acc-refresh">Recalculate</button>`}</div></div>
      ${acc.error ? `<div class="small down" style="margin-top:10px">Last calculation failed: ${esc(acc.error)}</div>` : ""}</div>`;
  if (!r) {
    area.innerHTML = head + (acc.status === "running" ? "" : '<div class="empty">No report yet.</div>');
  } else {
    const live = r.live, rep = r.replay;
    area.innerHTML = head +
      accuracyBlock("Live: what the site actually said", live, r.min_sample,
        `Every rating the site gave since ${esc(live.since || "launch")} (first call per stock, segment and day): <b>${live.calls}</b> calls, <b>${live.pending}</b> still waiting for their result.`) +
      accuracyBlock("Historical replay: last year on NIFTY 50", rep, r.min_sample,
        `The same swing rating rules re-run every day from ${esc(rep.period || "–")} on ${rep.stocks} NIFTY 50 stocks, using only data available on that day. Intraday can't be replayed (free data keeps only 60 days of 5-minute bars), so intraday appears in Live only.`);
  }
  const btn = $("#acc-refresh");
  if (btn) btn.onclick = async () => renderAccuracy(await api("/admin/accuracy?refresh=1"));
  if (acc.status === "running") setTimeout(async () => { if (my === state.route && $("#acc-area")) renderAccuracy(await api("/admin/accuracy")); }, 5000);
}

// ------------------------------------------------------------------ members (owner only)
async function viewMembers() {
  const [d, inv] = await Promise.all([api("/admin/accounts"), api("/admin/invites")]);
  const when = (ts) => ts ? new Date(ts * 1000).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" }) : "–";
  const pending = d.accounts.filter((a) => a.status === "pending");
  const active = d.accounts.filter((a) => a.status !== "pending");
  const who = (a) => `<b>${esc(a.name)}</b>${a.invite_note ? ` <span class="dim small">(invite: ${esc(a.invite_note)})</span>` : ""}`;
  const contact = (a) => `<span class="mono">${esc(a.id)}</span>${a.contact_email ? `<div class="dim small">${esc(a.contact_email)}</div>` : ""}`;
  const site = location.origin;
  app.innerHTML = `<div class="grid">
    ${pending.length ? `<div class="card hero"><h3>Waiting for your approval <span class="sub">${pending.length}</span></h3>
      <div class="tablewrap"><table class="plain"><thead><tr><th>Name</th><th>Mobile / email</th><th>Signed up</th>
        <th class="n">Actions</th></tr></thead><tbody>${pending.map((a) => `<tr><td>${who(a)}</td><td>${contact(a)}</td>
        <td>${when(a.created_at)}</td><td class="n"><button class="btn sm good" data-approve="${esc(a.id)}">Approve</button>
        <button class="btn sm bad" data-remove="${esc(a.id)}">Reject</button></td></tr>`).join("")}</tbody></table></div></div>` : ""}

    <div class="card"><h3>Invite a friend</h3>
      <p class="muted" style="margin-top:0">Create a personal code for each friend. Each code works <b>once</b>, and you'll see who used it.</p>
      <form id="inv-form" class="row" style="align-items:flex-end">
        <label class="f" style="flex:1;min-width:200px">Friend's name (just for you)<input id="inv-note" maxlength="60" placeholder="e.g. Rahul from college"></label>
        <button class="btn primary" type="submit">Create invite code</button>
      </form>
      <div id="inv-result"></div>
      ${inv.invites.length ? `<div class="tablewrap" style="margin-top:18px"><table class="plain"><thead><tr><th>For</th><th>Code ends</th>
        <th>Created</th><th>Status</th><th class="n"></th></tr></thead><tbody>${inv.invites.map((i) => `<tr>
        <td>${esc(i.note || "–")}</td><td class="mono">…${esc(i.hint)}</td><td>${when(i.created_at)}</td>
        <td>${i.used_by ? `<span class="tag b">used by ${esc(i.used_by)}</span>` : i.revoked ? '<span class="tag">cancelled</span>' : '<span class="tag w">not used yet</span>'}</td>
        <td class="n">${!i.used_by && !i.revoked ? `<button class="btn sm" data-revoke="${i.id}">Cancel code</button>` : ""}</td></tr>`).join("")}
        </tbody></table></div>` : ""}
    </div>

    <div class="card"><h3>Members <span class="sub">${active.length} account${active.length === 1 ? "" : "s"}</span></h3>
      <p class="muted" style="margin-top:0">If someone forgets their password, click <b>Reset password</b> and send them the
        temporary password privately. They can sign in with it straight away.</p>
      <div id="m-result"></div>
      ${active.length ? `<div class="tablewrap"><table class="plain"><thead><tr><th>Name</th><th>Mobile / email</th>
        <th>Joined</th><th>Last sign-in</th><th class="n">Actions</th></tr></thead><tbody>${active.map((a) => `<tr>
        <td>${who(a)}</td><td>${contact(a)}</td><td>${when(a.created_at)}</td><td>${when(a.last_login)}</td>
        <td class="n"><button class="btn sm" data-reset="${esc(a.id)}">Reset password</button>
        <button class="btn sm bad" data-remove="${esc(a.id)}">Remove</button></td></tr>`).join("")}</tbody></table></div>`
        : '<div class="empty">No members yet. Create an invite code above and send it with the site link.</div>'}
    </div></div>`;

  $("#inv-form").onsubmit = async (e) => {
    e.preventDefault();
    const note = $("#inv-note").value.trim();
    try {
      const r = await api("/admin/invites", { method: "POST", body: { note } });
      const msg = `Hey${note ? " " + note.split(" ")[0] : ""}! Join my private AI Traded beta (paper trading only, not investment advice): ${site}\n` +
        `Tap "Create account" and use your personal invite code: ${r.code}`;
      $("#inv-result").innerHTML = `<div class="card hero" style="margin-top:18px">
        <b>Invite code${note ? " for " + esc(note) : ""}:</b>
        <div class="mono" style="font-size:24px;margin:10px 0;user-select:all">${esc(r.code)}</div>
        <div class="muted small" style="margin-bottom:10px">Shown only once. Message to send:</div>
        <textarea id="inv-msg" rows="3" style="width:100%" readonly>${esc(msg)}</textarea>
        <div class="row" style="margin-top:10px"><button class="btn sm" id="inv-copy">Copy message</button>
          <a class="btn sm" target="_blank" rel="noopener" href="https://wa.me/?text=${encodeURIComponent(msg)}">Share on WhatsApp</a></div></div>`;
      $("#inv-copy").onclick = async () => {
        try { await navigator.clipboard.writeText(msg); $("#inv-copy").textContent = "Copied ✓"; }
        catch { $("#inv-msg").select(); }
      };
      const keep = $("#inv-result").innerHTML;
      await viewMembers();  // refresh the list, keep the new code on screen
      $("#inv-result").innerHTML = keep;
      $("#inv-copy").onclick = async () => {
        try { await navigator.clipboard.writeText(msg); $("#inv-copy").textContent = "Copied ✓"; }
        catch { $("#inv-msg").select(); }
      };
    } catch (err) { $("#inv-result").innerHTML = errorBox(err); }
  };
  app.querySelectorAll("[data-revoke]").forEach((b) => b.onclick = async () => {
    if (!confirm("Cancel this invite code? It will stop working.")) return;
    try { await api("/admin/invites/revoke", { method: "POST", body: { id: Number(b.dataset.revoke) } }); viewMembers(); }
    catch (e) { $("#inv-result").innerHTML = errorBox(e); }
  });
  app.querySelectorAll("[data-approve]").forEach((b) => b.onclick = async () => {
    try { await api("/admin/approve", { method: "POST", body: { id: b.dataset.approve } }); viewMembers(); }
    catch (e) { $("#m-result").innerHTML = errorBox(e); }
  });
  app.querySelectorAll("[data-reset]").forEach((b) => b.onclick = async () => {
    const id = b.dataset.reset;
    if (!confirm("Give " + id + " a new temporary password? Their current password stops working.")) return;
    try {
      const r = await api("/admin/reset", { method: "POST", body: { id } });
      $("#m-result").innerHTML = `<div class="card hero" style="margin-bottom:18px">
        <b>New temporary password for ${esc(id)}:</b>
        <div class="mono" style="font-size:22px;margin:10px 0;user-select:all">${esc(r.temp_password)}</div>
        <div class="muted small">Send it to them privately. It is shown only once.</div></div>`;
    } catch (e) { $("#m-result").innerHTML = errorBox(e); }
  });
  app.querySelectorAll("[data-remove]").forEach((b) => b.onclick = async () => {
    const id = b.dataset.remove;
    if (!confirm("Remove the account " + id + "? They can't sign in again (their invite code stays used).")) return;
    try { await api("/admin/remove", { method: "POST", body: { id } }); viewMembers(); }
    catch (e) { $("#m-result").innerHTML = errorBox(e); }
  });
}

// ------------------------------------------------------------------ router
async function route() {
  clearView();
  state.route = (state.route || 0) + 1;
  const h = location.hash || "#/today";
  const [path] = h.slice(2).split("?");
  const parts = path.split("/");
  document.querySelectorAll("#tabs a").forEach((a) => a.classList.toggle("active", a.dataset.r === parts[0]));
  window.scrollTo(0, 0);
  try {
    switch (parts[0]) {
      case "scanner": return await viewScanner();
      case "intraday": return await viewScanner({ mode: "intraday", universe: "nifty50", lockMode: true, title: "Intraday trading (5-minute)", note: "Opening-range breakouts (15 min), session VWAP, 5-min volume spikes and intraday momentum. Open a stock for 1m / 5m / 15m charts, the trade plan with a square-off time, and the Time Machine. Intraday signals are never used as long-term recommendations." });
      case "swing": return await viewScanner({ mode: "swing", universe: "nifty200", lockMode: true, title: "Swing trading (daily / weekly)", note: "EMA 20/50/200 structure, breakout confirmation, support and resistance, trend analysis and holding-period estimates. Plans include a 5-year event-study backtest with Indian delivery charges." });
      case "longterm": return await viewLongterm();
      case "news": return await viewNews();
      case "watchlist": return await viewWatchlist();
      case "paper": return await viewPaper();
      case "settings": return await viewSettings();
      case "status": return await viewStatus();
      case "members": return await viewMembers();
      case "admin": return await viewAdmin();
      case "stock": return await viewStock(decodeURIComponent(parts[1] || ""));
      default: return await viewToday();
    }
  } catch (e) { app.innerHTML = errorBox(e); }
}
window.addEventListener("hashchange", route);
refreshHeader();
route();
