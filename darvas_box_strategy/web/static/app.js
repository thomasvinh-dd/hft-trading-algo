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
    tr.innerHTML = `
      <td><a class="ticker-cell" data-ticker="${p.ticker}">${p.ticker}</a></td>
      <td>${VND(p.entry_price)}</td>
      <td>${VND(p.last_close)}</td>
      <td class="${pnlClass}">${PCT(p.pnl_pct)}</td>
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
    tr.innerHTML = `
      <td><a class="ticker-cell" data-ticker="${t.ticker}">${t.ticker}</a></td>
      <td class="muted small">${t.entry_date}</td>
      <td class="muted small">${t.exit_date}</td>
      <td class="${pnlClass}">${PCT(t.pnl_pct)}</td>
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
    renderPositions(posR.positions);
    renderKPIs(btR.backtest);
    renderEquityCurve(btR.backtest.equity_curve);
    renderTradeList("#winners-table", btR.backtest.top_winners);
    renderTradeList("#losers-table", btR.backtest.top_losers);
    renderExitReasons(btR.backtest.exit_reasons);
    setText("#ticker-breakdown", btR.backtest.ticker_breakdown || "(none)");
    setStatus(btR.last_updated);
  } catch (e) {
    setText("#cache-status", `Error: ${e.message}`);
  }
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
  const status = d.is_open ? "OPEN" : `Closed ${d.exit_reason}`;
  setText("#deal-modal-subtitle",
    `${d.ticker} • ${d.shares.toLocaleString()} shares • ${status} • held ${d.bars_held} bars`);

  // Summary tiles
  const fmtVnd = v => (v == null ? "—" : VND(v));
  const pnlClass = (d.pnl_pct || 0) >= 0 ? "pos" : "neg";
  const pnlVnd = d.is_open ? null : d.pnl_vnd;
  $("#deal-modal-summary").innerHTML = `
    <div class="kpi"><div class="kpi-label">Entry</div>
      <div class="kpi-value">${VND(d.entry_price)}</div></div>
    <div class="kpi"><div class="kpi-label">${d.is_open ? "Last close" : "Exit"}</div>
      <div class="kpi-value">${d.is_open ? VND(d.last_close) : VND(d.exit_price)}</div></div>
    <div class="kpi"><div class="kpi-label">P&L</div>
      <div class="kpi-value ${pnlClass}">${d.is_open ? "—" : PCT(d.pnl_pct)}</div></div>
    <div class="kpi"><div class="kpi-label">P&L VND</div>
      <div class="kpi-value ${pnlClass}">${fmtVnd(pnlVnd)}</div></div>
    <div class="kpi"><div class="kpi-label">High</div>
      <div class="kpi-value">${VND(d.highest_close)}</div></div>
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
    const isBuy = (f.shares || 0) > 0;
    const isSell = (f.shares || 0) < 0;
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
      <td><span class="fill-kind fill-kind-${f.kind}">${f.kind}</span></td>
      <td>${f.price ? VND(f.price) : "—"}</td>
      <td class="${isBuy ? "fill-positive" : isSell ? "fill-negative" : ""}">${f.shares > 0 ? "+" : ""}${(f.shares || 0).toLocaleString()}</td>
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
