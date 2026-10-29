/* MacBook Price Watcher India — iPhone web app.
 * Reads data/{config,history,alerts}.json (written by the cloud checker) and lets you
 * edit product links, which are committed back to the GitHub repo via its API. */
"use strict";

const RETAILERS = [
  { id: "amazon",     name: "Amazon India",     domains: ["amazon.in", "amzn.in"] },
  { id: "flipkart",   name: "Flipkart",         domains: ["flipkart.com"] },
  { id: "croma",      name: "Croma",            domains: ["croma.com"] },
  { id: "vijaysales", name: "Vijay Sales",      domains: ["vijaysales.com"] },
  { id: "reliance",   name: "Reliance Digital", domains: ["reliancedigital.in"] },
];
const BY_ID = Object.fromEntries(RETAILERS.map(r => [r.id, r]));
const WORKFLOW = "check-prices.yml";

const STATUS = {
  verified:      { label: "Verified",       cls: "good",  icon: "✓" },
  blocked:       { label: "Blocked",        cls: "warn",  icon: "⛔" },
  no_price:      { label: "No price found", cls: "warn",  icon: "?" },
  spec_mismatch: { label: "Spec mismatch",  cls: "bad",   icon: "≠" },
  out_of_stock:  { label: "Out of stock",   cls: "muted", icon: "○" },
  suspicious:    { label: "Suspicious",     cls: "bad",   icon: "!" },
  error:         { label: "Error",          cls: "bad",   icon: "!" },
  pending:       { label: "Awaiting check", cls: "muted", icon: "…" },
  unset:         { label: "No link",        cls: "muted", icon: "+" },
};

const state = { config: null, history: { last_run: null, checks: [] }, alerts: [], range: 7, chart: null };
const $ = s => document.querySelector(s);

// ------------------------------------------------------------------ helpers
const store = {
  get(k, d = "") { try { return localStorage.getItem("pw." + k) ?? d; } catch { return d; } },
  set(k, v) { try { localStorage.setItem("pw." + k, v); } catch { /* private mode */ } },
};

function inr(x) {
  if (x == null || isNaN(x)) return "—";
  return "₹" + Math.round(x).toLocaleString("en-IN");
}
function ist(ts, opts = { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit", hour12: false }) {
  return new Date(ts).toLocaleString("en-IN", { timeZone: "Asia/Kolkata", ...opts });
}
function ago(ts) {
  if (!ts) return "never";
  const s = (Date.now() - new Date(ts)) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return Math.round(s / 60) + " min ago";
  if (s < 86400) return Math.round(s / 3600) + " h ago";
  return Math.round(s / 86400) + " d ago";
}
function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  clearTimeout(toast.t); toast.t = setTimeout(() => t.classList.remove("show"), 2600);
}

// ------------------------------------------------------------------ GitHub
function gh() {
  // Default owner/repo from a GitHub Pages URL: https://<owner>.github.io/<repo>/
  const host = location.hostname, seg = location.pathname.split("/").filter(Boolean)[0] || "";
  const pagesOwner = host.endsWith(".github.io") ? host.split(".")[0] : "";
  return {
    owner: store.get("owner", pagesOwner),
    repo: store.get("repo", pagesOwner ? seg : ""),
    branch: store.get("branch", "main") || "main",
    token: store.get("token"),
  };
}
async function ghApi(path, opts = {}) {
  const g = gh();
  const headers = { Accept: "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28", ...(opts.headers || {}) };
  if (g.token) headers.Authorization = "Bearer " + g.token;
  const res = await fetch(`https://api.github.com/repos/${g.owner}/${g.repo}${path}`, { ...opts, headers, cache: "no-store" });
  if (!res.ok) {
    let detail = ""; try { detail = (await res.json()).message; } catch {}
    throw new Error(`GitHub ${res.status}${detail ? ": " + detail : ""}`);
  }
  return res;
}
const b64 = str => btoa(String.fromCharCode(...new TextEncoder().encode(str)));

// ------------------------------------------------------------------ data
async function loadFile(name, fallback) {
  const g = gh();
  // Prefer the API (always fresh — Pages can lag a few minutes behind the bot's commits).
  if (g.owner && g.repo) {
    try {
      const r = await ghApi(`/contents/data/${name}?ref=${encodeURIComponent(g.branch)}`, { headers: { Accept: "application/vnd.github.raw+json" } });
      return await r.json();
    } catch (e) { console.warn("API load failed, using site copy", name, e.message); }
  }
  try {
    const r = await fetch(`data/${name}?t=${Date.now()}`, { cache: "no-store" });
    if (r.ok) return await r.json();
  } catch {}
  return fallback;
}

async function loadAll() {
  $("#refresh").classList.add("spin");
  const [config, history, alerts] = await Promise.all([
    loadFile("config.json", { target_price: 185000, urls: {} }),
    loadFile("history.json", { last_run: null, checks: [] }),
    loadFile("alerts.json", []),
  ]);
  Object.assign(state, { config, history, alerts });
  $("#refresh").classList.remove("spin");
  renderAll();
}

// Derived per-retailer view: latest check, last two verified checks.
function summary() {
  const out = {};
  for (const r of RETAILERS) out[r.id] = { latest: null, verified: [], url: (state.config.urls || {})[r.id] || "" };
  for (const c of state.history.checks) {
    const s = out[c.retailer]; if (!s) continue;
    s.latest = c;
    if (c.verified) s.verified.push(c);
  }
  for (const s of Object.values(out)) {
    s.current = s.verified.at(-1) || null;
    // last verified price that differs from today's, so the card shows a real move, not "same as 3h ago"
    s.previous = s.current ? s.verified.findLast(c => c.price !== s.current.price) || null : null;
    s.status = !s.url ? "unset" : !s.latest || s.latest.url !== s.url ? "pending" : s.latest.status;
  }
  return out;
}

// ------------------------------------------------------------------ render
function renderAll() {
  renderPrices(); renderHistory(); renderAlerts();
  if (!state.settingsFilled) { fillSettings(); state.settingsFilled = true; }   // don't clobber edits on refresh
}

function renderPrices() {
  const target = Number(state.config.target_price || 185000);
  const sum = summary();
  const priced = RETAILERS.filter(r => sum[r.id].current).sort((a, b) => sum[a.id].current.price - sum[b.id].current.price);
  const best = priced[0];

  // hero
  const hero = $("#hero");
  if (!best) {
    hero.className = "card hero";
    hero.innerHTML = `<div class="label">Best verified price</div><div class="big">—</div>
      <div class="where">${Object.values(sum).some(s => s.url) ? "Waiting for the first verified check." : "Add product links in Settings to start tracking."}</div>
      <div class="gap-line">Target ${inr(target)}</div>`;
  } else {
    const p = sum[best.id].current.price, hit = p <= target;
    const pct = Math.max(4, Math.min(100, (target / p) * 100));
    hero.className = "card hero" + (hit ? " hit" : "");
    hero.innerHTML = `<div class="label">Best verified price</div>
      <div class="big">${inr(p)}</div>
      <div class="where">at ${esc(best.name)} · ${ago(sum[best.id].current.ts)}</div>
      <div class="gap-line">${hit
        ? `<span class="pill good">✓ Target hit</span> ${inr(target - p)} below ${inr(target)}`
        : `<span class="pill muted">Target ${inr(target)}</span> ${inr(p - target)} to go`}</div>
      <div class="meter" aria-hidden="true"><i style="width:${pct}%"></i></div>`;
  }

  // cards: priced (cheapest first), then the rest in fixed order
  const order = [...priced, ...RETAILERS.filter(r => !sum[r.id].current)];
  $("#retailers").innerHTML = order.map(r => {
    const s = sum[r.id], st = STATUS[s.status] || STATUS.error;
    const cur = s.current, prev = s.previous;
    let delta = "";
    if (cur && prev) {
      const d = cur.price - prev.price;
      const since = ist(prev.ts, { day: "numeric", month: "short" });
      delta = d < 0 ? `<span class="r-delta down">▼ ${inr(-d)} from ${inr(prev.price)} (${since})</span>`
                    : `<span class="r-delta up">▲ ${inr(d)} from ${inr(prev.price)} (${since})</span>`;
    } else if (cur) {
      delta = `<span class="r-delta flat">Unchanged since tracking began</span>`;
    }
    const checked = s.latest && s.latest.url === s.url
      ? `Checked ${ago(s.latest.ts)} · ${ist(s.latest.ts)}` : s.url ? "Not checked yet" : "";
    const stale = cur && s.status !== "verified" && s.status !== "pending"
      ? `<div class="r-note">Latest check: ${esc(st.label.toLowerCase())} — ${esc(s.latest.note)}. Showing last verified price from ${ist(cur.ts)}.</div>`
      : !cur && s.latest && s.latest.url === s.url && s.status !== "verified"
      ? `<div class="r-note">${esc(s.latest.note)}</div>` : "";
    const cls = ["card", "r-card", r === best ? "best" : "", cur && cur.price <= target ? "hit" : ""].join(" ");
    return `<article class="${cls}">
      <div>
        <div class="r-name"><span class="dot" style="background:var(--s-${r.id})"></span>${esc(r.name)}</div>
        <div class="r-price ${cur ? "" : "none"}">${cur ? inr(cur.price) : s.url ? "No verified price yet" : "Not tracking"}</div>
        ${delta}
      </div>
      <div class="r-side">
        ${r === best ? `<span class="tag-best">Lowest</span>` : ""}
        <span class="pill ${st.cls}">${st.icon} ${st.label}</span>
        ${s.url ? `<a class="link-btn" href="${esc(s.url)}" target="_blank" rel="noopener">Open ↗</a>`
                : `<button class="link-btn" data-goto="settings">Add link</button>`}
      </div>
      ${checked ? `<div class="r-meta">${checked}</div>` : ""}
      ${stale}
    </article>`;
  }).join("");

  const lr = state.history.last_run;
  $("#last-run").textContent = lr ? `Last cloud check ${ago(lr)} (${ist(lr)} IST) · runs every 3 h` : "The cloud checker hasn't run yet.";
}

function renderHistory() {
  const target = Number(state.config.target_price || 185000);
  const since = state.range ? Date.now() - state.range * 86400e3 : 0;
  const checks = state.history.checks.filter(c => new Date(c.ts) >= since);
  const verified = checks.filter(c => c.verified);

  // table: newest first, all results (the accessible view of the chart + diagnostics)
  $("#checks-table tbody").innerHTML = checks.slice(-60).reverse().map(c => {
    const st = STATUS[c.status] || STATUS.error;
    return `<tr><td>${ist(c.ts)}</td><td>${esc(BY_ID[c.retailer]?.name || c.retailer)}</td>
      <td class="num">${c.price ? inr(c.price) : "—"}</td><td><span class="pill ${st.cls}">${st.icon} ${st.label}</span></td></tr>`;
  }).join("") || `<tr><td colspan="4" class="empty">No checks in this range.</td></tr>`;

  $("#chart-empty").classList.toggle("hidden", verified.length > 0);
  $("#chart").parentElement.classList.toggle("hidden", verified.length === 0);
  if (!window.Chart || !verified.length) return;

  const ink2 = cssVar("--ink-2"), ink3 = cssVar("--ink-3"), line = cssVar("--line");
  const xs = verified.map(c => +new Date(c.ts));
  const xMin = Math.min(...xs), xMax = Math.max(...xs);
  const datasets = RETAILERS.map(r => {
    const pts = verified.filter(c => c.retailer === r.id).map(c => ({ x: +new Date(c.ts), y: c.price }));
    const col = cssVar("--s-" + r.id);
    return { label: r.name, data: pts, borderColor: col, backgroundColor: col, borderWidth: 2,
             pointRadius: pts.length > 40 ? 0 : 3, pointHoverRadius: 6, pointBorderColor: cssVar("--surface"),
             pointBorderWidth: 2, tension: 0, stepped: "after" };
  }).filter(d => d.data.length);
  datasets.push({ label: "Target", data: [{ x: xMin, y: target }, { x: xMax, y: target }],
                  borderColor: ink3, borderDash: [6, 5], borderWidth: 1.5, pointRadius: 0, pointHoverRadius: 0 });

  const opts = {
    responsive: true, maintainAspectRatio: false, animation: false,
    interaction: { mode: "nearest", axis: "x", intersect: false },
    scales: {
      x: { type: "linear", min: xMin, max: xMax === xMin ? xMin + 3600e3 : xMax,
           grid: { display: false }, border: { color: line },
           ticks: { color: ink3, maxTicksLimit: 5, maxRotation: 0,
                    callback: v => ist(v, { day: "numeric", month: "short" }) } },
      y: { grid: { color: line }, border: { display: false },
           ticks: { color: ink3, maxTicksLimit: 6, callback: v => "₹" + (v / 1000).toLocaleString("en-IN") + "k" } },
    },
    plugins: {
      legend: { position: "bottom", labels: { color: ink2, usePointStyle: true, pointStyle: "circle", boxWidth: 8, boxHeight: 8, padding: 14 } },
      tooltip: { filter: i => i.dataset.label !== "Target",
                 callbacks: { title: items => items.length ? ist(items[0].parsed.x) + " IST" : "",
                              label: i => ` ${i.dataset.label}: ${inr(i.parsed.y)}` } },
    },
  };
  if (state.chart) state.chart.destroy();
  state.chart = new Chart($("#chart"), { type: "line", data: { datasets }, options: opts });
}

function renderAlerts() {
  const list = [...state.alerts].reverse();
  const icons = { target: "🎯", drop: "📉", health: "⚠️" };
  $("#alerts").innerHTML = list.length ? list.map(a => `
    <article class="card alert ${esc(a.type)}">
      <div class="ico" aria-hidden="true">${icons[a.type] || "🔔"}</div>
      <div>
        <h3>${esc(a.title.replace(/^\S+\s/, ""))}</h3>
        <p>${esc(a.message)}</p>
        <time>${ist(a.ts)} IST · ${ago(a.ts)}</time>
        ${a.url ? ` · <a href="${esc(a.url)}" target="_blank" rel="noopener">Open listing</a>` : ""}
      </div>
    </article>`).join("")
    : `<div class="card empty">No alerts yet.<br>You'll be notified when a verified price drops, and when it reaches ${inr(state.config.target_price || 185000)} or less.</div>`;

  const seen = store.get("alertsSeen", "");
  const unseen = list.filter(a => a.ts > seen).length;
  const badge = $("#alert-badge");
  badge.textContent = unseen > 9 ? "9+" : unseen;
  badge.classList.toggle("hidden", !unseen);
}

// ------------------------------------------------------------------ settings
function fillSettings() {
  const urls = state.config.urls || {};
  $("#url-fields").innerHTML = RETAILERS.map(r => `
    <label class="field" data-retailer="${r.id}">
      <span><span><span class="dot" style="display:inline-block;background:var(--s-${r.id});margin-right:6px"></span>${r.name}</span></span>
      <input type="url" inputmode="url" name="${r.id}" value="${esc(urls[r.id] || "")}"
             placeholder="https://www.${r.domains[0]}/…" autocapitalize="off" autocorrect="off" spellcheck="false">
    </label>`).join("");
  $("#target").value = state.config.target_price || 185000;
  const g = gh();
  $("#gh-owner").value = g.owner; $("#gh-repo").value = g.repo; $("#gh-branch").value = g.branch; $("#gh-token").value = g.token;
}

function readSettings() {
  let ok = true;
  const urls = {};
  for (const r of RETAILERS) {
    const field = document.querySelector(`[data-retailer="${r.id}"]`);
    const v = field.querySelector("input").value.trim();
    field.classList.remove("invalid"); field.querySelector(".err")?.remove();
    let err = "";
    if (v) {
      try {
        const u = new URL(v);
        if (!/^https?:$/.test(u.protocol)) err = "Must start with https://";
        else if (!r.domains.some(d => u.hostname === d || u.hostname.endsWith("." + d))) err = `This doesn't look like a ${r.name} link`;
      } catch { err = "Not a valid URL"; }
    }
    if (err) { ok = false; field.classList.add("invalid"); field.insertAdjacentHTML("beforeend", `<div class="err">${err}</div>`); }
    urls[r.id] = v;
  }
  const target = Math.round(Number($("#target").value));
  if (!(target >= 50000)) { ok = false; toast("Enter a valid target price"); }
  store.set("owner", $("#gh-owner").value.trim());
  store.set("repo", $("#gh-repo").value.trim());
  store.set("branch", $("#gh-branch").value.trim() || "main");
  store.set("token", $("#gh-token").value.trim());
  return ok ? { ...state.config, target_price: target, urls } : null;
}

function msg(text, kind = "") { const m = $("#settings-msg"); m.textContent = text; m.className = "msg " + kind; }

function requireGh() {
  const g = gh();
  if (!g.owner || !g.repo || !g.token) { msg("Fill in GitHub owner, repository and token first.", "err"); return false; }
  return true;
}

async function saveConfig(e) {
  e.preventDefault();
  const cfg = readSettings();
  if (!cfg) { msg("Fix the highlighted fields.", "err"); return; }
  if (!requireGh()) return;
  const btn = $("#save"); btn.disabled = true; msg("Saving…");
  try {
    const g = gh();
    const cur = await (await ghApi(`/contents/data/config.json?ref=${encodeURIComponent(g.branch)}`)).json();
    await ghApi("/contents/data/config.json", {
      method: "PUT",
      body: JSON.stringify({ message: "Update watched product links", branch: g.branch, sha: cur.sha,
                             content: b64(JSON.stringify(cfg, null, 2) + "\n") }),
    });
    state.config = cfg; renderPrices(); renderHistory();
    msg("Saved ✓ A check starts automatically — results in ~1–2 minutes.", "ok");
  } catch (err) { msg(err.message, "err"); }
  btn.disabled = false;
}

async function dispatch(testPush) {
  readSettings();
  if (!requireGh()) return;
  msg(testPush ? "Sending test push…" : "Starting check…");
  try {
    await ghApi(`/actions/workflows/${WORKFLOW}/dispatches`, {
      method: "POST", body: JSON.stringify({ ref: gh().branch, inputs: { test_push: testPush } }),
    });
    msg(testPush ? "Requested ✓ The notification should arrive within a minute." : "Check started ✓ Pull to refresh in ~2 minutes.", "ok");
  } catch (err) { msg(err.message, "err"); }
}

async function copyConfig() {
  const cfg = readSettings(); if (!cfg) return;
  try { await navigator.clipboard.writeText(JSON.stringify(cfg, null, 2)); toast("Config copied"); }
  catch { toast("Couldn't access the clipboard"); }
}

// ------------------------------------------------------------------ nav
function showTab(id) {
  document.querySelectorAll(".tab").forEach(t => t.classList.toggle("active", t.id === "tab-" + id));
  document.querySelectorAll(".tabbar button").forEach(b => b.classList.toggle("on", b.dataset.tab === id));
  store.set("tab", id);
  window.scrollTo(0, 0);
  if (id === "history") renderHistory();   // canvas needs a visible box to size
  if (id === "alerts" && state.alerts.length) { store.set("alertsSeen", state.alerts.at(-1).ts); renderAlerts(); }
}

document.querySelector(".tabbar").addEventListener("click", e => { const b = e.target.closest("button"); if (b) showTab(b.dataset.tab); });
document.addEventListener("click", e => { const g = e.target.closest("[data-goto]"); if (g) showTab(g.dataset.goto); });
document.querySelector(".seg").addEventListener("click", e => {
  const b = e.target.closest("button"); if (!b) return;
  document.querySelectorAll(".seg button").forEach(x => x.classList.toggle("on", x === b));
  state.range = Number(b.dataset.range); renderHistory();
});
$("#refresh").addEventListener("click", loadAll);
$("#settings-form").addEventListener("submit", saveConfig);
$("#run-now").addEventListener("click", () => dispatch(false));
$("#test-push").addEventListener("click", () => dispatch(true));
$("#copy-config").addEventListener("click", copyConfig);
document.addEventListener("visibilitychange", () => { if (!document.hidden) loadAll(); });
matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", renderHistory);

showTab(store.get("tab", "prices"));
loadAll();
setInterval(() => { if (!document.hidden) renderPrices(); }, 60e3);   // keep "x min ago" fresh
