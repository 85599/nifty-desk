const COLS = [
  { k: "symbol",   l: "Symbol",       cls: "l", edit: 1 },
  { k: "expiry",   l: "Expiry",       cls: "l", edit: 1 },
  { k: "strike",   l: "Strike",       edit: 1, dp: 0 },
  { k: "otype",    l: "Type",         edit: 1 },
  { k: "qty",      l: "Qty",          edit: 1, dp: 0 },
  { k: "side",     l: "Side",         edit: 1 },
  { k: "buy_qty",  l: "Buy Qty",      edit: 1, dp: 0 },
  { k: "buy_avg",  l: "Buy Avg",      edit: 1, dp: 2 },
  { k: "sell_qty", l: "Sell Qty",     edit: 1, dp: 0 },
  { k: "sell_avg", l: "Sell Avg",     edit: 1, dp: 2 },
  { k: "mkt_price",l: "Mkt (screen)", edit: 1, dp: 2 },
  { k: "_netavg",  l: "Net Avg",      dp: 2 },
  { k: "_live",    l: "Live Prem",    dp: 2 },
  { k: "_livepl",  l: "Live P&L",     dp: 0 },
  { k: "mtm",      l: "Terminal MTM", edit: 1, dp: 0 },
];
const SUSPECT_KEYS = ["strike", "otype", "expiry"];

const state = { rows: [], strikes: {}, spot: null, expiry: "", path: "", cfg: {}, busy: false };
const $ = (id) => document.getElementById(id);

function log(msg, bad) {
  const el = document.createElement("div");
  if (bad) el.className = "err";
  el.innerHTML = `<b>${new Date().toLocaleTimeString()}</b> ${msg}`;
  $("log").prepend(el);
}
function toast(msg, kind = "") {
  const t = $("toast");
  t.textContent = msg;
  t.className = `toast show ${kind}`;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => (t.className = "toast"), 4200);
}
const num = (v) => (v === "" || v === null || v === undefined ? null : Number(v));
function fmt(v, dp) {
  if (v === null || v === undefined || v === "" || Number.isNaN(Number(v))) return "";
  return Number(v).toLocaleString("en-IN", { minimumFractionDigits: dp, maximumFractionDigits: dp });
}

function derive(r) {
  const qty = num(r.qty) || 0;
  const bq = num(r.buy_qty) || 0, ba = num(r.buy_avg) || 0;
  const sq = num(r.sell_qty) || 0, sa = num(r.sell_avg) || 0;
  r._netavg = qty ? (r.side === "Buy" ? (bq * ba - sq * sa) / qty : (sq * sa - bq * ba) / qty) : null;
  const key = r.strike === null || r.strike === undefined || r.strike === "" || !r.expiry
    ? null : `${r.expiry}|${Number(r.strike)}`;
  const leg = key !== null && state.strikes[key] ? state.strikes[key][r.otype] : null;
  r._live = leg ? leg.last : null;
  r._livepl = r._live !== null && r._netavg !== null && qty
    ? qty * (r.side === "Sell" ? 1 : -1) * (r._netavg - r._live) : null;
}

function isSuspect(r, k) {
  if (k === "otype") return !r.otype;
  if (k === "strike") return !r.strike;
  if (k === "expiry") return !r.expiry;
  return false;
}

function render() {
  const head = $("headRow");
  if (!head.children.length) {
    head.innerHTML = COLS.map((c) => `<th class="${c.cls || ""}">${c.l}</th>`).join("") + "<th></th>";
  }
  const body = $("body");
  body.innerHTML = "";
  state.rows.forEach((r, i) => {
    derive(r);
    const tr = document.createElement("tr");
    COLS.forEach((c) => {
      const td = document.createElement("td");
      const v = r[c.k];
      td.textContent = c.dp !== undefined ? fmt(v, c.dp) : (v ?? "");
      if (c.k === "side") td.className = "side-" + (r.side || "");
      if (c.k === "_livepl" || c.k === "mtm") td.className = num(v) > 0 ? "pos" : num(v) < 0 ? "neg" : "";
      if (c.edit) {
        td.tabIndex = 0;
        if (SUSPECT_KEYS.includes(c.k) && isSuspect(r, c.k)) td.classList.add("suspect");
        td.dataset.row = i;
        td.dataset.col = c.k;
      }
      tr.appendChild(td);
    });
    const del = document.createElement("td");
    del.innerHTML = `<button class="tiny" data-del="${i}" title="Remove row">x</button>`;
    tr.appendChild(del);
    body.appendChild(tr);
  });

  const foot = $("foot");
  const sum = (k) => state.rows.reduce((a, r) => a + (num(r[k]) || 0), 0);
  const cells = COLS.map((c) => {
    if (c.k === "symbol") return `<td class="l">TOTAL</td>`;
    if (c.k === "qty") return `<td>${fmt(sum("qty"), 0)}</td>`;
    if (c.k === "_livepl") return `<td class="${sum("_livepl") < 0 ? "neg" : "pos"}">${fmt(sum("_livepl"), 0)}</td>`;
    if (c.k === "mtm") return `<td class="${sum("mtm") < 0 ? "neg" : "pos"}">${fmt(sum("mtm"), 0)}</td>`;
    return `<td></td>`;
  });
  foot.innerHTML = cells.join("") + "<td></td>";

  $("empty").style.display = state.rows.length ? "none" : "block";
  $("posCount").textContent = state.rows.length ? `(${state.rows.length} open)` : "";
  $("btnBuild").disabled = !state.rows.length;
}

/* ---------------------------------------------------------------- editing */
$("body").addEventListener("blur", (e) => {
  const td = e.target.closest("td[data-col]");
  if (!td) return;
  const r = state.rows[+td.dataset.row], k = td.dataset.col;
  let text = td.textContent.trim();
  if (k === "side") {
    const up = text.toLowerCase();
    r.side = up.startsWith("s") ? "Sell" : up.startsWith("b") ? "Buy" : text;
  } else if (k === "otype") {
    const up = text.toUpperCase().replace(/[^CP]/g, "");
    if (up) r.otype = up[0] === "C" ? "CE" : "PE";
    else { r.otype = ""; log(`Row ${+td.dataset.row + 1}: Type left blank - fill it in.`, true); }
  } else if (k === "symbol") {
    r.symbol = text.toUpperCase();
  } else if (k === "expiry") {
    r.expiry = text;
  } else {
    const n = num(text.replace(/,/g, ""));
    r[k] = n;
    if (k === "qty" && n) r.side = r.side || "Sell";
  }
  render();
}, true);

$("body").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); e.target.blur(); }
});

$("body").addEventListener("click", (e) => {
  const td = e.target.closest("td[data-col]");
  if (td && td.dataset.col === "side") {
    const r = state.rows[+td.dataset.row];
    r.side = r.side === "Sell" ? "Buy" : "Sell";
    render();
  }
  const btn = e.target.closest("button[data-del]");
  if (btn) { state.rows.splice(+btn.dataset.del, 1); render(); }
});

$("btnAdd").onclick = () => {
  state.rows.push({ symbol: state.cfg.symbol || "NIFTY", expiry: state.expiry || "", strike: null,
    otype: "CE", qty: 0, side: "Sell", buy_qty: 0, buy_avg: null, sell_qty: 0, sell_avg: null,
    mkt_price: null, mtm: null, net_qty_raw: -0 });
  render();
};

/* ---------------------------------------------------------------- upload */
async function upload(file) {
  if (!file) return;
  if (state.busy) return;
  state.busy = true;
  $("dropInner").classList.add("hidden");
  $("preview").classList.remove("hidden");
  $("previewImg").src = URL.createObjectURL(file);
  $("previewName").textContent = file.name;
  const fd = new FormData();
  fd.append("image", file);
  log(`Reading <b>${file.name}</b> with Windows OCR...`);
  try {
    const res = await fetch("/api/ocr", { method: "POST", body: fd });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "OCR failed");
    state.rows = data.records;
    state.strikes = {};
    $("liveChip").innerHTML = "";
    showWarnings(data.warnings);
    if (data.detected_lot) {
      log(`Lot size read from the quantities: <b>${data.detected_lot}</b> ` +
          `(override it in Settings if that is wrong).`);
    }
    const expiries = [...new Set(data.records.map((r) => r.expiry).filter(Boolean))];
    if (expiries.length) log(`Expiries seen: <b>${expiries.join(", ")}</b>`);
    log(`Parsed <b>${data.records.length}</b> position row(s).`);
    if (!data.records.length) {
      explainEmpty(data.diagnostic);
      render();
      return;
    }
    render();
    toast(`${data.records.length} rows read. Check the amber cells, then Build Excel.`, "ok");
  } catch (err) {
    log(err.message, true);
    toast(err.message, "bad");
    $("dropInner").classList.remove("hidden");
    $("preview").classList.add("hidden");
  } finally {
    state.busy = false;
  }
}

function showWarnings(list) {
  $("warn").innerHTML = (list || []).map((w) => `<div>${w}</div>`).join("");
}

function explainEmpty(d) {
  const esc = (s) => String(s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
  let reason;
  if (!d) {
    reason = "No rows came back. Try a fresh screenshot of just the position grid.";
  } else if (!d.ocr_lines) {
    reason = `Windows OCR returned <b>no text at all</b> from this image ` +
             `(${esc(d.image)}, language ${esc(d.language)}). Either the English OCR ` +
             `recogniser is not installed, or the image is not a plain screenshot. ` +
             `Add English under Settings &gt; Time &amp; Language &gt; Language, then retry.`;
  } else if (!d.grid_rows) {
    reason = `Windows OCR read <b>${d.ocr_lines} line(s)</b> (${esc(d.image)}, language ` +
             `${esc(d.language)}), but <b>none of them formed the ODIN position grid</b>, so ` +
             `nothing could be parsed. This usually means the grid has different or extra ` +
             `columns from another broker's ODIN build, or the screenshot is cropped so the ` +
             `header row (Symbol / Expiry / Strike / Type / Net Qty / Market Price) is missing. ` +
             `Capture the whole grid including its header and try again.`;
  } else {
    reason = `The grid was read (<b>${d.grid_rows} row(s)</b>) but every row was filtered out. ` +
             `Untick <i>NIFTY rows only</i> or <i>Hide fully closed</i> in Settings and drop the ` +
             `screenshot again.`;
  }
  log(reason, true);
  if (d && d.sample && d.sample.length) {
    log(`What OCR actually read: <span class="ocrsample">${d.sample.map(esc).join(" · ")}</span>`, true);
  }
  toast("0 rows read - see the Activity log for the exact reason.", "bad");
}

const drop = $("drop");
drop.onclick = (e) => { if (!e.target.closest("button")) $("fileInput").click(); };
$("fileInput").onchange = (e) => upload(e.target.files[0]);
$("btnAgain").onclick = (e) => { e.stopPropagation(); $("fileInput").click(); };
["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => {
  e.preventDefault(); drop.classList.add("hot");
}));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => {
  e.preventDefault(); drop.classList.remove("hot");
  if (ev === "drop") upload(e.dataTransfer.files[0]);
}));
document.addEventListener("paste", (e) => {
  const item = [...(e.clipboardData?.items || [])].find((i) => i.type.startsWith("image"));
  if (item) upload(item.getAsFile());
});

/* ---------------------------------------------------------------- live + build */
async function fetchLive() {
  $("btnLive").disabled = true;
  $("btnLive").textContent = "Fetching...";
  try {
    const res = await fetch("/api/live", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ expiry: $("expiry").value, symbol: state.cfg.symbol,
        expiries: [...new Set(state.rows.map((r) => r.expiry).filter(Boolean))] }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "live fetch failed");
    state.strikes = data.strikes || {};
    state.spot = data.spot;
    state.expiry = data.expiry;
    fillExpiries(data.expiry_dates, data.expiry);
    $("liveChip").innerHTML =
      `<span class="chip">${data.symbol} <b>${data.expiry}</b></span>` +
      `<span class="chip">Spot <b>${Number(data.spot).toFixed(2)}</b></span>` +
      `<span class="chip">ATM <b>${data.atm}</b></span>` +
      `<span class="chip">NSE ${data.timestamp}</span>`;
    log(`Live: spot <b>${Number(data.spot).toFixed(2)}</b>, ${data.n_strikes} strikes, ATM ${data.atm}.`);
    if (data.quoted_expiries?.length > 1) log(`Quoted every expiry in the book: <b>${data.quoted_expiries.join(", ")}</b>.`);
    (data.errors || []).forEach((e) => log(`Expiry ${e} could not be quoted.`, true));
    const missing = state.rows.filter((r) => r.expiry && r.strike && !data.strikes[`${r.expiry}|${Number(r.strike)}`]);
    if (missing.length) log(`${missing.length} row(s) have no NSE quote for their strike - check the expiry/strike spelling in the table.`, true);
    render();
    toast(`Live rates in. ${data.n_strikes} strikes loaded.`, "ok");
  } catch (err) {
    log(err.message, true);
    toast(err.message, "bad");
  } finally {
    $("btnLive").disabled = false;
    $("btnLive").textContent = "Fetch live rates";
  }
}
$("btnLive").onclick = fetchLive;

function collect() {
  return {
    symbol: state.cfg.symbol || "NIFTY",
    expiry: state.expiry || $("expiry").value,
    step: Number($("step").value), span: Number($("span").value), lot: Number($("lot").value),
    only_symbol: $("onlyNifty").checked, drop_closed: $("dropClosed").checked,
    output_dir: $("outdir").value,
  };
}

async function build() {
  $("btnBuild").disabled = true;
  $("btnBuild").textContent = "Writing...";
  try {
    const res = await fetch("/api/build", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ positions: state.rows, config: collect() }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "build failed");
    state.path = data.path;
    $("btnOpen").disabled = false;
    const s = data.summary;
    log(`Wrote <b>${data.path}</b> - ${s.positions} positions, ` +
        `${s.blocks} expiry page(s), ${s.strike_rows} strike rows, ` +
        `${s.scenarios} CP IF columns each, ` +
        `rates ${s.live_expiries?.length ? s.live_expiries.join(", ") : "none"}` +
        `${s.skipped_closed ? `, ${s.skipped_closed} closed skipped` : ""}.`);
    toast(`Excel ready: ${s.blocks} expiry page(s), one per print page.`, "ok");
  } catch (err) {
    log(err.message, true);
    toast(err.message, "bad");
  } finally {
    $("btnBuild").disabled = false;
    $("btnBuild").textContent = "Build Excel";
  }
}
$("btnBuild").onclick = build;
$("btnOpen").onclick = () => fetch("/api/open", {
  method: "POST", headers: { "Content-Type": "application/json" },
  body: JSON.stringify({ path: state.path }),
});

async function fillExpiries(dates, selected) {
  const sel = $("expiry");
  const keep = [...(dates || [])];
  state.rows.forEach((r) => { if (r.expiry && !keep.includes(r.expiry)) keep.push(r.expiry); });
  sel.innerHTML = `<option value="">auto (nearest)</option>` +
    keep.map((d) => `<option ${d === selected ? "selected" : ""}>${d}</option>`).join("");
  if (selected) sel.value = selected;
}
$("expiry").onchange = () => { state.expiry = $("expiry").value; };

/* ---------------------------------------------------------------- boot */
(async function boot() {
  render();
  try {
    const st = await (await fetch("/api/state")).json();
    state.cfg = st.config || {};
    state.rows = st.records || [];
    state.strikes = st.strikes || {};
    $("step").value = state.cfg.step || 50;
    $("span").value = state.cfg.span || 1500;
    $("lot").value = state.cfg.lot ?? 0;
    $("outdir").value = state.cfg.output_dir || "";
    $("onlyNifty").checked = state.cfg.only_symbol !== false;
    $("dropClosed").checked = state.cfg.drop_closed !== false;
    if (st.live) {
      $("liveChip").innerHTML = `<span class="chip">${st.live.symbol} <b>${st.live.expiry}</b></span>` +
        `<span class="chip">Spot <b>${Number(st.live.spot).toFixed(2)}</b></span><span class="chip">NSE ${st.live.timestamp}</span>`;
      state.spot = st.live.spot; state.expiry = st.live.expiry;
    }
    showWarnings(st.warnings);
    const ex = await (await fetch("/api/expiries")).json();
    await fillExpiries(ex.expiry_dates, state.cfg.expiry);
    log("Desk ready. Drop a screenshot or press Ctrl+V.");
    render();
  } catch (err) {
    log("Could not load settings: " + err.message, true);
  }
})();
document.addEventListener("keydown", (e) => {
  if (e.ctrlKey && e.key === "Enter") build();
  if (e.ctrlKey && e.key.toLowerCase() === "l") { e.preventDefault(); fetchLive(); }
});
