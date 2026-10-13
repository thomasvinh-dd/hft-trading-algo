// Darvas Box dashboard — vanilla JS, no build step.

const VND = n => n.toLocaleString("en-US", { maximumFractionDigits: 0 });
const PCT = (n, d = 2) => (n >= 0 ? "+" : "") + n.toFixed(d) + "%";

const $ = sel => document.querySelector(sel);
const $$ = sel => Array.from(document.querySelectorAll(sel));

function setText(sel, text) { const el = $(sel); if (el) el.textContent = text; }
function classIf(sel, cls, cond) { const el = $(sel); if (el) el.classList.toggle(cls, cond); }
function fmtTime(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleString();
}

async function fetchJSON(url, opts = {}) {
  const r = await fetch(url, opts);
  if (!r.ok) throw new Error(`${url} → ${r.status}`);
  return r.json();
}

function renderKPIs(b) {
  setText("#kpi-return", PCT(b.total_return_pct));
  classIf("#kpi-return", "pos", b.total_return_pct >= 0);
  classIf("#kpi-return", "neg", b.total_return_pct < 0);

  setText("#kpi-cagr", PCT(b.cagr_pct));
  classIf("#kpi-cagr", "pos", b.cagr_pct >= 0);
  classIf("#kpi-cagr", "neg", b.cagr_pct < 0);

  setText("#kpi-mdd", PCT(b.max_dd_pct));
  classIf("#kpi-mdd", "neg", b.max_dd_pct < 0);

  setText("#kpi-winrate", b.win_rate_pct.toFixed(1) + "%");
  setText("#kpi-pf", b.profit_factor.toFixed(2) + "x");
  setText("#kpi-trades", `${b.closed_trades} closed / ${b.open_trades} open`);

  setText("#curve-period",
    `${b.period.start} → ${b.period.end} (${b.period.years} yr)`);
  setText("#data-period", `${b.period.start} → ${b.period.end}`);
}

function renderEquityCurve(curve) {
  const svg = $("#equity-chart");
  if (!svg || !curve.length) return;
  const W = 1000, H = 300, PAD = 30;
  const xs = curve.map(p => new Date(p.date).getTime());
  const ys = curve.map(p => p.nav);
  const xMin = Math.min(...xs), xMax = Math.max(...xs);
  const yMin = Math.min(...ys), yMax = Math.max(...ys);
  const xR = xMax - xMin || 1;
  const yR = yMax - yMin || 1;
  const X = t => PAD + (t - xMin) / xR * (W - 2 * PAD);
  const Y = v => H - PAD - (v - yMin) / yR * (H - 2 * PAD);

  let pathD = "";
  let areaD = `M ${X(xs[0])} ${H - PAD}`;
  curve.forEach((p, i) => {
    const x = X(xs[i]);
    const y = Y(p.nav);
    pathD += (i === 0 ? "M" : "L") + x.toFixed(1) + " " + y.toFixed(1) + " ";
    areaD += "L " + x.toFixed(1) + " " + y.toFixed(1) + " ";
  });
  areaD += `L ${X(xs[xs.length - 1])} ${H - PAD} Z`;

  const color = (ys[ys.length - 1] >= ys[0]) ? "var(--green)" : "var(--red)";

  svg.innerHTML = `
    <defs>
      <linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="${color}" stop-opacity="0.35"/>
        <stop offset="100%" stop-color="${color}" stop-opacity="0"/>
      </linearGradient>
    </defs>
    <path d="${areaD}" fill="url(#g)"/>
    <path d="${pathD}" fill="none" stroke="${color}" stroke-width="2"/>
    <text x="${PAD}" y="${PAD - 8}" fill="var(--muted)" font-size="11">
      ${VND(ys[0])} → ${VND(ys[ys.length - 1])}
    </text>
  `;
}

function renderSignals(signals) {
  const tbody = $("#signals-table tbody");
  const empty = $("#signals-empty");
  tbody.innerHTML = "";
  if (!signals.length) {
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");
  setText("#signals-meta", `${signals.length} signal(s)`);
  for (const s of signals) {
    const tr = document.createElement("tr");
    const conv = s.high_conviction
      ? '<span class="pos">HIGH</span>'
      : '<span class="muted">regular</span>';
    tr.innerHTML = `
      <td><a class="ticker-cell" data-ticker="${s.ticker}">${s.ticker}</a></td>
      <td>${VND(s.entry_price)}</td>
      <td>${(s.size_pct * 100).toFixed(0)}%</td>
      <td>${VND(s.sl_T)}</td>
      <td>${VND(s.sl_F)}</td>
      <td>${conv}</td>
    `;
    tbody.appendChild(tr);
  }
  attachTickerClicks(tbody);
}

function renderRecentSignals(recent) {
  const tbody = $("#recent-signals-table tbody");
  const empty = $("#recent-empty");
  tbody.innerHTML = "";
  if (!recent || !recent.length) {
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");
  setText("#recent-meta", `${recent.length} signal(s) in the last 7 trading days`);
  for (const s of recent) {
    const tr = document.createElement("tr");
    const conv = s.high_conviction
      ? '<span class="pos">HIGH</span>'
      : '<span class="muted">regular</span>';
    tr.innerHTML = `
      <td class="muted small">${s.signal_date}</td>
      <td><a class="ticker-cell" data-ticker="${s.ticker}">${s.ticker}</a></td>
      <td>${VND(s.entry_price)}</td>
      <td>${(s.size_pct * 100).toFixed(0)}%</td>
      <td>${VND(s.sl_T)}</td>
      <td>${VND(s.sl_F)}</td>
      <td>${conv}</td>
    `;
    tbody.appendChild(tr);
  }
  attachTickerClicks(tbody);
}

function renderPositions(positions) {
  const tbody = $("#positions-table tbody");
  const empty = $("#positions-empty");
  tbody.innerHTML = "";
  if (!positions.length) {
    empty.classList.remove("hidden");
    return;
  }
  empty.classList.add("hidden");
  setText("#positions-meta", `${positions.length} open`);
  for (const p of positions) {
    const tr = document.createElement("tr");
    const pnlClass = p.pnl_pct >= 0 ? "pos" : "neg";
    const pnlVnd = p.pnl_vnd != null ? VND(p.pnl_vnd) : "—";
    tr.innerHTML = `
      <td><a class="ticker-cell" data-ticker="${p.ticker}">${p.ticker}</a></td>
      <td>${VND(p.entry_price)}</td>
      <td>${VND(p.last_close)}</td>
      <td class="${pnlClass}">${PCT(p.pnl_pct)}</td>
      <td class="${pnlClass}">${pnlVnd}</td>
      <td>${VND(p.shares)}</td>
      <td>${p.bars_held}</td>
    `;
    tbody.appendChild(tr);
  }
  attachTickerClicks(tbody);
}

function renderTradeList(sel, trades) {
  const tbody = $(`${sel} tbody`);
  if (!tbody) return;
  tbody.innerHTML = "";
  for (const t of trades) {
    const tr = document.createElement("tr");
    const pnlClass = t.pnl_pct >= 0 ? "pos" : "neg";
    const pnlVnd = t.pnl_vnd != null ? VND(t.pnl_vnd) : "—";
    tr.innerHTML = `
      <td><a class="ticker-cell" data-ticker="${t.ticker}">${t.ticker}</a></td>
      <td class="muted small">${t.entry_date}</td>
      <td class="muted small">${t.exit_date}</td>
      <td class="${pnlClass}">${PCT(t.pnl_pct)}</td>
      <td class="${pnlClass}">${pnlVnd}</td>
      <td>${t.exit_reason}</td>
    `;
    tbody.appendChild(tr);
  }
  attachTickerClicks(tbody);
}

function renderExitReasons(reasons) {
  const wrap = $("#exit-reasons");
  wrap.innerHTML = "";
  const total = Object.values(reasons).reduce((a, b) => a + b, 0) || 1;
  for (const [k, v] of Object.entries(reasons).sort((a, b) => b[1] - a[1])) {
    const pct = (v / total * 100).toFixed(0);
    const chip = document.createElement("span");
    chip.className = "exit-chip";
    chip.innerHTML = `<strong>${v}</strong> ${k} <span class="muted">(${pct}%)</span>`;
    wrap.appendChild(chip);
  }
}

function setStatus(iso) {
  setText("#cache-status", `Updated ${fmtTime(iso)}`);
  setText("#last-updated", fmtTime(iso));
}

async function load() {
  try {
    const [sigR, posR, btR] = await Promise.all([
      fetchJSON("/api/signals"),
      fetchJSON("/api/positions"),
      fetchJSON("/api/backtest"),
    ]);
    renderSignals(sigR.signals);
    renderRecentSignals(sigR.recent);
    renderPositions(posR.positions);
    renderKPIs(btR.backtest);
    renderEquityCurve(btR.backtest.equity_curve);
    renderTradeList("#winners-table", btR.backtest.top_winners);
    renderTradeList("#losers-table", btR.backtest.top_losers);
    renderExitReasons(btR.backtest.exit_reasons);
    setText("#ticker-breakdown", btR.backtest.ticker_breakdown || "(none)");
    setStatus(btR.last_updated);
    // Populate the universe overview on the Signals tab
    populateUniverseOverview(btR.backtest);
  } catch (e) {
    setText("#cache-status", `Error: ${e.message}`);
  }
}

// ----- Universe overview (Signals tab) -----
// Shows one row per ticker that has at least one trade. Helps the
// user pick tickers to inspect from the search box.
//
// The ticker_breakdown field is a markdown table with this format:
//   Tkr      N Wins      PnL (VND)
//   -------------------------------
//   GEE      7    6    242,472,572
//   ...
// We parse the table rows and expose them as a structured list.
function populateUniverseOverview(bt) {
  const tbody = $("#universe-table tbody");
  if (!tbody) return;
  const lines = (bt.ticker_breakdown || "").split("\n");
  // Find lines that look like a ticker row: starts with a 3-4 char ticker
  // followed by 2+ columns of numbers.
  const rows = [];
  for (const line of lines) {
    // Skip headers, dividers, blanks, markdown fences
    const t = line.trim();
    if (!t || t.startsWith("```") || t.startsWith("Tkr") || t.startsWith("-")
        || t.startsWith("*Top") || t.startsWith("*Bottom")) continue;
    const m = t.match(/^(\S+)\s+(\d+)\s+(\d+)\s+([+\-]?[\d,]+)/);
    if (m) {
      const n = parseInt(m[2]);
      const wins = parseInt(m[3]);
      const pnlRaw = m[4];
      const winPct = n > 0 ? (wins / n * 100).toFixed(0) : "0";
      rows.push({ ticker: m[1], trades: n, wins, winPct, pnl: pnlRaw });
    }
  }
  if (rows.length === 0) {
    tbody.innerHTML = `<tr><td colspan="5" class="empty">No trade data.</td></tr>`;
    return;
  }
  tbody.innerHTML = rows.map(r =>
    `<tr><td><a class="ticker-link" data-ticker="${r.ticker}">${r.ticker}</a></td>` +
    `<td>${r.trades} (${r.wins}W)</td><td>${r.winPct}%</td>` +
    `<td class="${r.pnl.startsWith('-') ? 'neg' : 'pos'}">${r.pnl}</td>` +
    `<td>—</td></tr>`
  ).join("");
  setText("#universe-meta", `${rows.length} tickers with trades`);
  attachTickerClicks(tbody);
}

// ----- Quick-load chips (recent signals + open positions) -----
function populateQuickTickers() {
  const wrap = $("#ticker-quick-buttons");
  if (!wrap) return;
  const seen = new Set();
  const tickers = [];
  // From signals table
  document.querySelectorAll("#signals-table .ticker-link").forEach(a => {
    const t = a.dataset.ticker;
    if (t && !seen.has(t)) { seen.add(t); tickers.push(t); }
  });
  // From positions table
  document.querySelectorAll("#positions-table .ticker-link").forEach(a => {
    const t = a.dataset.ticker;
    if (t && !seen.has(t)) { seen.add(t); tickers.push(t); }
  });
  // From universe overview
  document.querySelectorAll("#universe-table .ticker-link").forEach(a => {
    const t = a.dataset.ticker;
    if (t && !seen.has(t)) { seen.add(t); tickers.push(t); }
  });
  if (tickers.length === 0) {
    wrap.innerHTML = `<span class="muted small">No tickers loaded yet.</span>`;
    return;
  }
  wrap.innerHTML = tickers.slice(0, 24).map(t =>
    `<button class="ticker-chip" data-ticker="${t}">${t}</button>`
  ).join("");
  wrap.querySelectorAll(".ticker-chip").forEach(b => {
    b.addEventListener("click", () => openTickerModal(b.dataset.ticker));
  });
}

async function refresh() {
  const btn = $("#refresh-btn");
  btn.disabled = true;
  btn.textContent = "Refreshing…";
  setText("#cache-status", "Refreshing (this takes ~3 min)…");
  try {
    const r = await fetchJSON("/api/refresh", { method: "POST" });
    setText("#cache-status",
      `Refreshed in ${r.elapsed_seconds}s — ${r.signal_count} signals, ${r.position_count} open`);
    await load();
  } catch (e) {
    setText("#cache-status", `Refresh failed: ${e.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = "Refresh data";
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("#refresh-btn").addEventListener("click", refresh);
  $("#modal-close").addEventListener("click", closeTickerModal);
  $("#ticker-modal").addEventListener("click", (e) => {
    if (e.target.id === "ticker-modal") closeTickerModal();
  });
  $("#deal-modal-close").addEventListener("click", closeDealModal);
  $("#deal-modal").addEventListener("click", (e) => {
    if (e.target.id === "deal-modal") closeDealModal();
  });
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      // Close whichever modal is open
      const dealOpen = !$("#deal-modal").classList.contains("hidden");
      if (dealOpen) closeDealModal();
      else closeTickerModal();
    }
  });

  // ----- Tab switching -----
  $$(".tab-btn").forEach(btn => {
    btn.addEventListener("click", () => {
      const tab = btn.dataset.tab;
      $$(".tab-btn").forEach(b => {
        b.classList.toggle("active", b === btn);
        b.setAttribute("aria-selected", b === btn ? "true" : "false");
      });
      $$(".tab-panel").forEach(p => {
        p.classList.toggle("active", p.id === `tab-${tab}`);
      });
      // Populate the quick-ticker chips on the Signals tab
      if (tab === "signals") populateQuickTickers();
    });
  });

  // ----- Ticker search on the Signals tab -----
  const searchInput = $("#ticker-search-input");
  const searchBtn = $("#ticker-search-btn");
  if (searchInput && searchBtn) {
    const doSearch = () => {
      const v = searchInput.value.trim().toUpperCase();
      if (v) openTickerModal(v);
    };
    searchBtn.addEventListener("click", doSearch);
    searchInput.addEventListener("keydown", (e) => {
      if (e.key === "Enter") doSearch();
    });
  }

  load();
});

function attachTickerClicks(container) {
  container.querySelectorAll(".ticker-cell").forEach((el) => {
    if (el.dataset.bound) return;
    el.dataset.bound = "1";
    el.addEventListener("click", (e) => {
      e.preventDefault();
      openTickerModal(el.dataset.ticker);
    });
  });
}

async function openTickerModal(ticker) {
  const modal = $("#ticker-modal");
  modal.classList.remove("hidden");
  setText("#modal-ticker-name", ticker);
  setText("#modal-ticker-subtitle", "Loading…");
  $("#modal-summary").innerHTML = "";
  $("#modal-deals-table tbody").innerHTML = "";

  try {
    const r = await fetchJSON(`/api/ticker/${encodeURIComponent(ticker)}`);
    renderTickerHistory(r);
  } catch (e) {
    setText("#modal-ticker-subtitle", `Error: ${e.message}`);
  }
}

function closeTickerModal() {
  $("#ticker-modal").classList.add("hidden");
}

// ---- Deal detail modal ----

function attachDealClicks(container) {
  container.querySelectorAll(".deal-id-link").forEach((el) => {
    if (el.dataset.bound) return;
    el.dataset.bound = "1";
    el.addEventListener("click", (e) => {
      e.preventDefault();
      openDealModal(el.dataset.dealId);
    });
  });
}

async function openDealModal(dealId) {
  const modal = $("#deal-modal");
  modal.classList.remove("hidden");
  setText("#deal-modal-title", dealId);
  setText("#deal-modal-subtitle", "Loading…");
  $("#deal-modal-summary").innerHTML = "";
  $("#deal-fills-table tbody").innerHTML = "";

  try {
    const d = await fetchJSON(`/api/deal/${encodeURIComponent(dealId)}`);
    renderDealDetail(d);
  } catch (e) {
    setText("#deal-modal-subtitle", `Error: ${e.message}`);
  }
}

function closeDealModal() {
  $("#deal-modal").classList.add("hidden");
}

function renderDealDetail(d) {
  if (!d) {
    setText("#deal-modal-subtitle", "No data");
    return;
  }
  // Subtitle
  const status = d.is_open ? "OPEN" : `Closed ${d.exit_reason || "?"}`;
  const sharesTxt = d.shares != null ? d.shares.toLocaleString() : "?";
  const barsTxt = d.bars_held != null ? d.bars_held : "?";
  setText("#deal-modal-subtitle",
    `${d.ticker || "?"} • ${sharesTxt} shares • ${status} • held ${barsTxt} bars`);

  // Summary tiles — every value goes through fmtVnd to handle nulls
  const fmtVnd = v => (v == null ? "—" : VND(v));
  const pnlClass = (d.pnl_pct || 0) >= 0 ? "pos" : "neg";
  const pnlVnd = d.is_open ? null : d.pnl_vnd;
  $("#deal-modal-summary").innerHTML = `
    <div class="kpi"><div class="kpi-label">Entry</div>
      <div class="kpi-value">${fmtVnd(d.entry_price)}</div></div>
    <div class="kpi"><div class="kpi-label">${d.is_open ? "Last close" : "Exit"}</div>
      <div class="kpi-value">${d.is_open ? fmtVnd(d.last_close) : fmtVnd(d.exit_price)}</div></div>
    <div class="kpi"><div class="kpi-label">P&L</div>
      <div class="kpi-value ${pnlClass}">${d.is_open ? "—" : PCT(d.pnl_pct)}</div></div>
    <div class="kpi"><div class="kpi-label">P&L VND</div>
      <div class="kpi-value ${pnlClass}">${fmtVnd(pnlVnd)}</div></div>
    <div class="kpi"><div class="kpi-label">High</div>
      <div class="kpi-value">${fmtVnd(d.highest_close)}</div></div>
    <div class="kpi"><div class="kpi-label">Days held</div>
      <div class="kpi-value">${d.bars_held != null ? d.bars_held : "—"}</div>
      <div class="kpi-sub muted small">trading bars</div></div>
  `;

  // Fills table — the bar-by-bar timeline
  const tbody = $("#deal-fills-table tbody");
  tbody.innerHTML = "";
  const fills = d.fills || [];
  if (fills.length === 0) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="9" class="empty">No fill events recorded for this deal.</td>`;
    tbody.appendChild(tr);
    return;
  }
  // Parse dates once for delta calculation
  const MS = 86400000;
  const dateMs = fills.map(f => f.date ? Date.parse(f.date) : null);
  let prevDateMs = null;
  for (let i = 0; i < fills.length; i++) {
    const f = fills[i];
    const tr = document.createElement("tr");
    const shares = f.shares || 0;
    const isBuy = shares > 0;
    const isSell = shares < 0;
    const pnlClass = (f.pnl_delta || 0) > 0 ? "fill-positive" :
                      (f.pnl_delta || 0) < 0 ? "fill-negative" : "";
    // Δ days since the previous event
    let deltaDays = "";
    if (i === 0) {
      deltaDays = "—";
    } else if (dateMs[i] != null && prevDateMs != null) {
      const days = Math.round((dateMs[i] - prevDateMs) / MS);
      deltaDays = days === 0 ? "same day" : `+${days}d`;
    }
    prevDateMs = dateMs[i];
    tr.innerHTML = `
      <td class="muted small">${f.date || ""}</td>
      <td><span class="fill-kind fill-kind-${f.kind || "?"}">${f.kind || "?"}</span></td>
      <td>${f.price ? VND(f.price) : "—"}</td>
      <td class="${isBuy ? "fill-positive" : isSell ? "fill-negative" : ""}">${shares > 0 ? "+" : ""}${shares.toLocaleString()}</td>
      <td class="${pnlClass}">${f.pnl_delta ? (f.pnl_delta > 0 ? "+" : "") + VND(f.pnl_delta) : "—"}</td>
      <td class="${(f.cumulative_pnl || 0) >= 0 ? "fill-positive" : "fill-negative"}">${f.cumulative_pnl != null ? (f.cumulative_pnl > 0 ? "+" : "") + VND(f.cumulative_pnl) : "—"}</td>
      <td>${(f.remaining || 0).toLocaleString()}</td>
      <td class="muted small">${deltaDays}</td>
      <td class="muted small">${f.note || ""}</td>
    `;
    tbody.appendChild(tr);
  }
}

function renderTickerHistory(h) {
  if (!h || !h.deals) {
    setText("#modal-ticker-subtitle", "No data");
    return;
  }
  const s = h.summary || {};
  setText("#modal-ticker-subtitle",
    `${s.total_deals || 0} deal(s) — ${s.closed_deals || 0} closed, ${s.open_deals || 0} open`);

  // Build summary tiles
  const fmtVnd = v => (v == null ? "—" : VND(v));
  const summary = $("#modal-summary");
  summary.innerHTML = `
    <div class="kpi"><div class="kpi-label">Win rate</div>
      <div class="kpi-value">${(s.win_rate_pct || 0).toFixed(1)}%</div></div>
    <div class="kpi"><div class="kpi-label">Total PnL</div>
      <div class="kpi-value ${(s.total_pnl_vnd || 0) >= 0 ? "pos" : "neg"}">
        ${fmtVnd(s.total_pnl_vnd)}</div></div>
    <div class="kpi"><div class="kpi-label">Avg return</div>
      <div class="kpi-value">${(s.avg_pnl_pct || 0).toFixed(2)}%</div></div>
    <div class="kpi"><div class="kpi-label">Wins / losses</div>
      <div class="kpi-value">${s.wins || 0} / ${s.losses || 0}</div></div>
  `;

  // Build deals table
  const tbody = $("#modal-deals-table tbody");
  tbody.innerHTML = "";
  if (!h.deals.length) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td colspan="8" class="empty">No deals for this ticker.</td>`;
    tbody.appendChild(tr);
    return;
  }
  for (const d of h.deals) {
    const tr = document.createElement("tr");
    const pnlClass = d.pnl_pct >= 0 ? "pos" : "neg";
    const reasonCell = d.is_open
      ? `<span class="reason-open">OPEN</span>`
      : `<span>${d.exit_reason}</span>`;
    const exitPx = d.is_open ? "—" : VND(d.exit_price);
    tr.innerHTML = `
      <td><a class="deal-id-link" data-deal-id="${d.deal_id}">${d.deal_id}</a></td>
      <td class="muted small">${d.entry_date}</td>
      <td>${VND(d.entry_price)}</td>
      <td class="muted small">${d.is_open ? "—" : d.exit_date}</td>
      <td>${exitPx}</td>
      <td class="${pnlClass}">${PCT(d.pnl_pct)}</td>
      <td>${reasonCell}</td>
      <td>${d.bars_held}</td>
    `;
    tbody.appendChild(tr);
  }
  attachDealClicks(tbody);
}
