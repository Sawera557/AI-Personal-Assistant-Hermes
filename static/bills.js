/* ── Billing & Invoices — real API, no mock data ── */

/* ── State ─────────────────────────────────────────────────────────────── */
let businesses   = [];   // from GET /api/businesses
let bills        = [];   // from GET /api/bills
let insights     = {};   // from response.insights
let activeBizId  = "all";
let filterStatus = "all";
let searchQuery  = "";
let scanning     = false;
let billsInitialized = false;

/* ── Entry point ─────────────────────────────────────────────────────────── */
async function initBilling() {
  if (billsInitialized) return;
  billsInitialized = true;
  renderSkeleton();
  await Promise.all([loadBusinesses(), loadBills()]);
  renderAll();
  bindDropzone();
  bindConfirmOverlay();
  bindBillViewer();

  // Auto-scan logic: 45-day backfill on first open, 7-day refresh after 6h
  const firstDone = localStorage.getItem("billsFirstScanDone");
  const lastScan = parseInt(localStorage.getItem("lastBillScan") || "0", 10);
  const hoursSince = (Date.now() - lastScan) / 3600000;
  if (!firstDone) {
    triggerScan();               // first open: 45-day backfill with UI
  } else if (hoursSince > 6) {
    _autoScan();                 // periodic: silent 7-day refresh
  }
}

/* ── Data loading ────────────────────────────────────────────────────────── */
async function loadBusinesses() {
  try {
    const r = await fetch("/api/businesses");
    const j = await r.json();
    businesses = j.businesses || [];
  } catch (e) {
    console.error("loadBusinesses:", e);
  }
}

async function loadBills() {
  try {
    const r = await fetch("/api/bills");
    const j = await r.json();
    bills    = j.bills    || [];
    insights = j.insights || {};
  } catch (e) {
    console.error("loadBills:", e);
  }
}

async function reload() {
  await loadBills();
  renderStats();
  renderBizTabs();
  renderBillsList();
  renderInsights();
}

/* ── Full render pass ────────────────────────────────────────────────────── */
function renderAll() {
  renderBizTabs();
  renderStats();
  renderDropzone();
  renderToolbar();
  renderBillsList();
  renderInsights();
}

/* ── Skeleton while loading ──────────────────────────────────────────────── */
function renderSkeleton() {
  const list = document.getElementById("bills-list");
  if (list) list.innerHTML = `
    ${[1,2,3].map(() => `
      <div class="bill-item" style="pointer-events:none;opacity:.4">
        <div class="bill-avatar" style="background:var(--surface-2)"></div>
        <div class="bill-info">
          <div style="height:13px;width:140px;border-radius:6px;background:var(--surface-2);margin-bottom:8px"></div>
          <div style="height:10px;width:90px;border-radius:6px;background:var(--surface-2)"></div>
        </div>
        <div class="bill-right">
          <div style="height:14px;width:60px;border-radius:6px;background:var(--surface-2);margin-bottom:6px;margin-left:auto"></div>
          <div style="height:10px;width:50px;border-radius:6px;background:var(--surface-2);margin-left:auto"></div>
        </div>
      </div>`).join("")}`;
}

/* ── Business tabs ───────────────────────────────────────────────────────── */
function renderBizTabs() {
  const row = document.getElementById("biz-tabs-row");
  if (!row) return;

  const allCount = bills.filter(b => b.status !== "paid").length;
  const allBiz = { business_id: "all", name: "All Businesses", emoji: "🏢", color: "#9aa3b2" };
  const bizList = [allBiz, ...businesses];

  row.innerHTML = bizList.map(biz => {
    const isAll = biz.business_id === "all";
    const count = isAll ? allCount
      : bills.filter(b => b.business_id === biz.business_id && b.status !== "paid").length;
    const isActive = activeBizId === biz.business_id;
    const bg = isActive && !isAll
      ? `background:${biz.color}22; border-color:${biz.color}55;`
      : isActive ? "background:rgba(255,255,255,.08); border-color:rgba(255,255,255,.15);" : "";
    return `
      <button class="biz-tab${isActive ? " active" : ""}" style="${bg}"
              onclick="setBizFilter('${biz.business_id}')">
        ${!isAll ? `<span class="biz-tab-dot" style="background:${biz.color}"></span>` : ""}
        ${biz.emoji} ${biz.name}
        ${count > 0 ? `<span class="biz-tab-count">${count}</span>` : ""}
      </button>`;
  }).join("");

  // Append scan button
  row.insertAdjacentHTML("beforeend", `
    <button class="biz-tab" id="scan-btn" onclick="triggerScan()"
            style="margin-left:auto;flex-shrink:0;color:var(--accent);border-color:rgba(245,166,35,.3)">
      ${scanning ? `<span class="scan-dots"><span></span><span></span><span></span></span> Scanning…`
                 : "⚡ Scan Connected Apps"}
    </button>`);
}

function setBizFilter(bizId) {
  activeBizId = bizId;
  renderBizTabs();
  renderStats();
  renderBillsList();
}

/* ── Stats row ───────────────────────────────────────────────────────────── */
function renderStats() {
  const row = document.getElementById("bills-stats-row");
  if (!row) return;
  const filtered = activeBizId === "all" ? bills : bills.filter(b => b.business_id === activeBizId);
  const overdue   = filtered.filter(b => b.status === "overdue").reduce((s,b) => s + (b.amount||0), 0);
  const dueSoon   = filtered.filter(b => b.status === "due-soon").reduce((s,b) => s + (b.amount||0), 0);
  const total     = filtered.reduce((s,b) => s + (b.amount||0), 0);
  const paidCount = filtered.filter(b => b.status === "paid").length;

  row.innerHTML = `
    <div class="bstat">
      <div class="bstat-val" style="color:#f87171">${fmt(overdue)}</div>
      <div class="bstat-label">Overdue</div>
    </div>
    <div class="bstat">
      <div class="bstat-val" style="color:var(--accent)">${fmt(dueSoon)}</div>
      <div class="bstat-label">Due This Week</div>
    </div>
    <div class="bstat">
      <div class="bstat-val">${fmt(total)}</div>
      <div class="bstat-label">Total This Month</div>
    </div>
    <div class="bstat">
      <div class="bstat-val" style="color:var(--ok)">${paidCount}</div>
      <div class="bstat-label">Paid</div>
    </div>`;
}

/* ── Dropzone ────────────────────────────────────────────────────────────── */
function renderDropzone() {
  const dz = document.getElementById("bills-dropzone");
  if (!dz) return;
  const compact = bills.length > 0;
  if (compact) dz.classList.add("dz-compact");
  dz.innerHTML = `
    <div class="dz-icon">
      <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
        <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
        <polyline points="17 8 12 3 7 8"/>
        <line x1="12" y1="3" x2="12" y2="15"/>
      </svg>
    </div>
    <div class="dz-title">Drop bills &amp; invoices here</div>
    <div class="dz-sub">AI extracts vendor, amount, due date &amp; line items automatically</div>
    <div class="dz-formats">
      <span class="dz-fmt">PDF</span><span class="dz-fmt">JPG</span>
      <span class="dz-fmt">PNG</span><span class="dz-fmt">HEIC</span>
    </div>
    <div class="dz-compact-row">
      <div class="dz-compact-icon">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
          <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
          <polyline points="17 8 12 3 7 8"/>
          <line x1="12" y1="3" x2="12" y2="15"/>
        </svg>
      </div>
      <div class="dz-compact-text">Drop more files <span>PDF · JPG · PNG · HEIC</span></div>
    </div>`;
}

function bindDropzone() {
  const dz        = document.getElementById("bills-dropzone");
  const fileInput = document.getElementById("bill-file-input");
  const fileMobile = document.getElementById("bill-file-input-m");
  if (!dz || !fileInput) return;

  const openPicker = () => fileInput.click();
  dz.addEventListener("click", openPicker);
  dz.addEventListener("dragover", e => { e.preventDefault(); dz.classList.add("dz-over"); });
  dz.addEventListener("dragleave", () => dz.classList.remove("dz-over"));
  dz.addEventListener("drop", e => {
    e.preventDefault();
    dz.classList.remove("dz-over");
    handleFilesSelected(Array.from(e.dataTransfer.files));
  });
  const onChange = (inp) => () => {
    if (inp.files.length) handleFilesSelected(Array.from(inp.files));
    inp.value = "";
  };
  fileInput.addEventListener("change", onChange(fileInput));
  if (fileMobile) fileMobile.addEventListener("change", onChange(fileMobile));
}

/* ── Toolbar ─────────────────────────────────────────────────────────────── */
function renderToolbar() {
  const tb = document.getElementById("bills-toolbar");
  if (!tb) return;
  tb.removeAttribute("hidden");
  tb.innerHTML = `
    <div class="bills-search">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
      </svg>
      <input type="text" placeholder="Search vendors, invoices…" id="bills-search-input" value="${searchQuery}"/>
    </div>
    <select class="bills-filter-sel" id="bills-status-filter">
      <option value="all">All Status</option>
      <option value="overdue">Overdue</option>
      <option value="due-soon">Due Soon</option>
      <option value="pending">Pending</option>
      <option value="paid">Paid</option>
      <option value="processing">Processing</option>
    </select>
    <button class="bv-action-btn" id="check-payments-btn" onclick="triggerCheckPayments()"
            style="flex-shrink:0;white-space:nowrap">
      🔍 Check Payments
    </button>`;
  document.getElementById("bills-status-filter").value = filterStatus;
  document.getElementById("bills-search-input")?.addEventListener("input", e => {
    searchQuery = e.target.value.toLowerCase();
    renderBillsList();
  });
  document.getElementById("bills-status-filter")?.addEventListener("change", e => {
    filterStatus = e.target.value;
    renderBillsList();
  });
}

/* ── Bills list ──────────────────────────────────────────────────────────── */
function renderBillsList() {
  const list = document.getElementById("bills-list");
  if (!list) return;

  let visible = bills.slice();
  if (activeBizId !== "all") visible = visible.filter(b => b.business_id === activeBizId);
  if (filterStatus !== "all") visible = visible.filter(b => b.status === filterStatus);
  if (searchQuery) visible = visible.filter(b =>
    (b.vendor||"").toLowerCase().includes(searchQuery) ||
    (b.invoice_number||"").toLowerCase().includes(searchQuery) ||
    (b.category||"").toLowerCase().includes(searchQuery)
  );

  if (!bills.length) {
    list.innerHTML = `
      <div style="text-align:center;padding:40px 16px;color:var(--muted)">
        <div style="font-size:32px;margin-bottom:12px">🧾</div>
        <div style="font-size:14px;font-weight:600;margin-bottom:6px">No bills yet</div>
        <div style="font-size:12px">Upload a bill or scan your connected apps to get started</div>
      </div>`;
    return;
  }

  if (!visible.length) {
    list.innerHTML = `<div style="text-align:center;color:var(--muted);padding:24px;font-size:13px">No bills match</div>`;
    return;
  }

  list.innerHTML = visible.map(b => billCardHTML(b)).join("");

  // Compact dropzone once we have bills
  const dz = document.getElementById("bills-dropzone");
  if (dz) dz.classList.add("dz-compact");
}

function billCardHTML(bill) {
  const biz = businesses.find(b => b.business_id === bill.business_id);
  const bizColor  = bill.business_color  || biz?.color  || "#9aa3b2";
  const bizEmoji  = bill.business_emoji  || biz?.emoji  || "🏢";
  const bizName   = bill.business_name   || biz?.name   || "Unassigned";

  const vendorEmoji = vendorIcon(bill.vendor);
  const dueDateStr = bill.due_date
    ? new Date(bill.due_date + "T00:00:00").toLocaleDateString("en-US", { month:"short", day:"numeric" })
    : "";
  const statusLabel = { overdue:"Overdue", "due-soon":"Due Soon", pending:"Pending",
    paid:"Paid", processing:"Scanning…" }[bill.status] || bill.status;

  const needsConfirm = (bill.confidence || 1) < 0.75 && bill.status !== "paid" && bill.status !== "processing";
  const confBadge = needsConfirm
    ? `<button class="bill-confidence" onclick="event.stopPropagation();showConfirmFor('${bill.bill_id}')">🤔 Confirm business</button>` : "";

  const metaRow = bill.status === "processing"
    ? `<div class="bill-meta"><span style="color:var(--blue);font-size:12px">Scanning <span class="scan-dots"><span></span><span></span><span></span></span></span></div>`
    : `<div class="bill-meta">
         ${bill.invoice_number ? `<span class="bill-inv">${bill.invoice_number}</span>` : ""}
         <span class="bill-biz-tag" style="background:${bizColor}18;border-color:${bizColor}33;color:${bizColor}">
           <span class="bill-biz-dot" style="background:${bizColor}"></span>${bizName}
         </span>
         ${bill.category ? `<span class="bill-inv">· ${bill.category}</span>` : ""}
         ${bill.source && bill.source !== "upload"
           ? `<span class="bill-inv" title="from ${bill.source}">· ${bill.source === "gmail" ? "📧" : "📁"}</span>` : ""}
       </div>
       ${confBadge}`;

  return `
    <div class="bill-item ${bill.status}" data-id="${bill.bill_id}"
         onclick="openBillViewer('${bill.bill_id}')">
      <div class="bill-avatar">${vendorEmoji}</div>
      <div class="bill-info">
        <div class="bill-vendor">${bill.vendor}</div>
        ${metaRow}
      </div>
      <div class="bill-right">
        ${bill.status !== "processing" && bill.amount ? `<div class="bill-amount">${fmt(bill.amount)}</div>` : ""}
        ${dueDateStr ? `<div class="bill-due">${bill.status === "overdue" ? `<span style="color:var(--err)">` : ""}Due ${dueDateStr}${bill.status === "overdue" ? "</span>" : ""}</div>` : ""}
        <div class="bill-status ${bill.status}">${statusLabel}</div>
      </div>
    </div>`;
}

function vendorIcon(vendor) {
  const v = (vendor || "").toLowerCase();
  if (v.includes("aws") || v.includes("amazon")) return "☁️";
  if (v.includes("stripe")) return "💳";
  if (v.includes("google")) return "🔵";
  if (v.includes("slack")) return "💬";
  if (v.includes("figma")) return "🎨";
  if (v.includes("notion")) return "📓";
  if (v.includes("github")) return "🐙";
  if (v.includes("quickbooks") || v.includes("intuit")) return "📊";
  if (v.includes("adp") || v.includes("payroll")) return "👔";
  if (v.includes("sysco") || v.includes("food") || v.includes("supply")) return "🥩";
  if (v.includes("cushman") || v.includes("property") || v.includes("llc")) return "🏢";
  if (v.includes("meta") || v.includes("facebook")) return "📢";
  if (v.includes("deere") || v.includes("farm")) return "🚜";
  if (v.includes("cloudflare")) return "🔥";
  if (v.includes("centerpoint") || v.includes("energy") || v.includes("utility")) return "⚡";
  if (v.includes("xcel") || v.includes("electric")) return "⚡";
  if (v.includes("city") || v.includes("mpls") || v.includes("municipal")) return "🏛️";
  if (v.includes("bitwarden") || v.includes("password") || v.includes("security")) return "🔐";
  if (v.includes("anthropic") || v.includes("openai") || v.includes("ai")) return "🤖";
  if (v.includes("vercel") || v.includes("netlify") || v.includes("deploy")) return "🚀";
  if (v.includes("rent") || v.includes("lease")) return "🏠";
  if (v.includes("distribution") || v.includes("logistics")) return "📦";
  if (v.includes("youth") || v.includes("nonprofit") || v.includes("org")) return "👥";
  if (v.includes("dtb") || v.includes("bank") || v.includes("statement")) return "🏦";
  if (v.includes("hansen") || v.includes("lawn") || v.includes("landscape")) return "🌿";
  if (v.includes("tax")) return "📋";
  if (v.includes("intercom")) return "💬";
  if (v.includes("office")) return "📦";
  return "🧾";
}

/* ── File upload → NuExtract ─────────────────────────────────────────────── */
async function handleFilesSelected(files) {
  if (!files.length) return;

  // Add processing placeholders
  const placeholders = files.map(f => ({
    bill_id: "proc_" + Math.random().toString(36).slice(2,8),
    vendor: f.name.replace(/\.[^.]+$/, "").replace(/[-_]/g," "),
    status: "processing", amount: 0, invoice_number: "Extracting…",
    due_date: null, business_id: null, category: null,
  }));
  bills = [...placeholders, ...bills];
  renderBillsList();

  const dz = document.getElementById("bills-dropzone");
  if (dz) dz.classList.add("dz-compact");

  // Upload all files in one multipart POST
  const form = new FormData();
  files.forEach(f => form.append("files", f));

  let created = [];
  try {
    const resp = await fetch("/api/bills/upload", { method: "POST", body: form });
    const json = await resp.json();
    created = json.created || [];
  } catch (e) {
    console.error("Upload failed:", e);
  }

  // Remove placeholders, reload from API
  bills = bills.filter(b => !b.bill_id.startsWith("proc_"));
  await loadBills();
  renderAll();

  // If any need business confirmation, show dialog for first one
  const needsConfirm = created.find(c => c.needs_confirmation && c.bill_id);
  if (needsConfirm) {
    const fullBill = bills.find(b => b.bill_id === needsConfirm.bill_id);
    if (fullBill) showConfirmOverlay(fullBill);
  }
}

/* ── Connected-app scan ──────────────────────────────────────────────────── */
async function triggerScan() {
  if (scanning) return;
  scanning = true;
  renderBizTabs();

  const firstDone = localStorage.getItem("billsFirstScanDone");
  const days = firstDone ? 7 : 45;

  try {
    const resp = await fetch("/api/bills/scan", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ sources: ["gmail", "google-drive"], days }),
    });
    const json = await resp.json();
    const n = json.scanned || 0;
    localStorage.setItem("billsFirstScanDone", "1");
    localStorage.setItem("lastBillScan", Date.now().toString());
    if (n > 0) {
      await reload();
      showToast(`⚡ Found ${n} new bill${n !== 1 ? "s" : ""} from your connected apps`);
    } else {
      showToast(`✓ Scan complete — no new bills (checked last ${days} days)`);
    }
  } catch (e) {
    console.error("Scan failed:", e);
    showToast("Scan failed — check console");
  } finally {
    scanning = false;
    renderBizTabs();
  }
}

async function _autoScan() {
  // Silent background scan — no spinner, just reloads if new bills found
  try {
    const resp = await fetch("/api/bills/scan", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ sources: ["gmail", "google-drive"], days: 7 }),
    });
    const json = await resp.json();
    localStorage.setItem("lastBillScan", Date.now().toString());
    if ((json.scanned || 0) > 0) {
      await reload();
      showToast(`⚡ Auto-found ${json.scanned} new bill${json.scanned !== 1 ? "s" : ""}`);
    }
  } catch (e) {
    console.debug("Auto-scan error:", e);
  }
}

/* ── AI Insights ─────────────────────────────────────────────────────────── */
function renderInsights() {
  const el = document.getElementById("bills-insights");
  if (!el) return;

  const header = `
    <div class="insights-col-header">
      🧠 AI Insights <span class="insights-live-badge">Live</span>
    </div>`;

  if (!bills.length) {
    el.innerHTML = header + `
      <div class="insight-group" style="padding:20px 14px;text-align:center;color:var(--muted);font-size:12px">
        Upload bills or scan connected apps to see insights
      </div>`;
    return;
  }

  const overdueItems = (insights.overdue || []);
  const overdueGroup = overdueItems.length ? `
    <div class="insight-group">
      <div class="insight-group-head">
        <span class="ig-emoji">🚨</span>
        <div><div class="ig-title">Overdue Alerts</div><div class="ig-sub">Needs action</div></div>
        <span class="ig-count" style="color:#f87171">${overdueItems.length}</span>
      </div>
      ${overdueItems.map(r => `
        <div class="insight-row">
          <div class="ir-dot" style="background:${r.dot}"></div>
          <div class="ir-body"><div class="ir-text">${r.text}</div>
            <button class="ir-action" onclick="markPaid('${r.bill_id}')">Mark Paid →</button>
          </div>
        </div>`).join("")}
    </div>` : "";

  const cfItems = (insights.cashflow || []);
  const cashFlowGroup = cfItems.length ? `
    <div class="insight-group">
      <div class="insight-group-head">
        <span class="ig-emoji">📊</span>
        <div><div class="ig-title">Cash Flow by Business</div><div class="ig-sub">Pending outflows</div></div>
      </div>
      <div class="cf-bars">
        ${cfItems.map(c => `
          <div class="cf-row">
            <span class="cf-label" title="${c.label}">${c.label}</span>
            <div class="cf-track"><div class="cf-bar" style="width:${c.pct}%;background:${c.color}"></div></div>
            <span class="cf-val" style="color:${c.color}">${fmtShort(c.amount)}</span>
          </div>`).join("")}
      </div>
    </div>` : "";

  const upItems = (insights.upcoming || []);
  const upcomingGroup = upItems.length ? `
    <div class="insight-group">
      <div class="insight-group-head">
        <span class="ig-emoji">📅</span>
        <div><div class="ig-title">Upcoming (14 days)</div><div class="ig-sub">Plan your payments</div></div>
        <span class="ig-count">${upItems.length}</span>
      </div>
      ${upItems.map(r => `
        <div class="insight-row">
          <div class="ir-dot" style="background:${r.dot}"></div>
          <div class="ir-body">
            <div class="ir-text">${r.text}</div>
            ${r.action ? `<button class="ir-action">${r.action} →</button>` : ""}
          </div>
        </div>`).join("")}
    </div>` : "";

  const emptyInsight = !overdueGroup && !cashFlowGroup && !upcomingGroup
    ? `<div class="insight-group" style="padding:16px 14px;text-align:center;color:var(--muted);font-size:12px">
         ✓ No urgent items right now
       </div>` : "";

  el.innerHTML = header + overdueGroup + cashFlowGroup + upcomingGroup + emptyInsight;
}

/* ── Mark paid ───────────────────────────────────────────────────────────── */
async function markPaid(billId) {
  try {
    await fetch(`/api/bills/${billId}`, {
      method: "PATCH",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ status: "paid" }),
    });
    await reload();
  } catch (e) { console.error("markPaid:", e); }
}

/* ── Business confirmation overlay ──────────────────────────────────────── */
let pendingBillId = null;

function showConfirmFor(billId) {
  const bill = bills.find(b => b.bill_id === billId);
  if (bill) showConfirmOverlay(bill);
}

function showConfirmOverlay(bill) {
  pendingBillId = bill.bill_id;
  const overlay  = document.getElementById("ai-confirm-overlay");
  const vendorEl = document.getElementById("ai-confirm-vendor");
  const hintEl   = document.getElementById("ai-confirm-hint");
  const grid     = document.getElementById("ai-confirm-grid");
  if (!overlay) return;

  if (vendorEl) vendorEl.textContent = `${vendorIcon(bill.vendor)} ${bill.vendor}${bill.amount ? " · " + fmt(bill.amount) : ""}`;
  if (hintEl) {
    const pct = bill.confidence ? Math.round(bill.confidence * 100) : 0;
    hintEl.textContent = pct
      ? `I'm ${pct}% confident about this. Which business should I file it under?`
      : "Which business does this bill belong to?";
  }

  if (grid && businesses.length) {
    grid.innerHTML = businesses.map(biz => `
      <button class="ai-biz-btn" onclick="assignBill('${biz.business_id}')"
              style="${bill.business_id === biz.business_id ? `border-color:${biz.color};box-shadow:0 0 0 1px ${biz.color}44` : ""}">
        <span class="ai-biz-icon">${biz.emoji}</span>
        <span class="ai-biz-name">${biz.name}</span>
        <span class="ai-biz-industry">${biz.industry || ""}</span>
        ${bill.business_id === biz.business_id ? `<span class="ai-suggested-badge">AI Guess</span>` : ""}
      </button>`).join("");
  } else if (grid) {
    grid.innerHTML = `<div style="color:var(--muted);font-size:12px;padding:8px;grid-column:1/-1">
      No businesses set up yet. <a href="#" onclick="showBusinessSetup()" style="color:var(--accent)">Add businesses →</a>
    </div>`;
  }

  overlay.removeAttribute("hidden");
}

async function assignBill(bizId) {
  if (!pendingBillId) return;
  try {
    await fetch(`/api/bills/${pendingBillId}`, {
      method: "PATCH",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ business_id: bizId }),
    });
  } catch (e) { console.error("assignBill:", e); }
  hideConfirmOverlay();
  await reload();
}

function hideConfirmOverlay() {
  document.getElementById("ai-confirm-overlay")?.setAttribute("hidden","");
  pendingBillId = null;
}

function bindConfirmOverlay() {
  document.getElementById("ai-confirm-skip")?.addEventListener("click", hideConfirmOverlay);
  document.getElementById("ai-confirm-overlay")?.addEventListener("click", e => {
    if (e.target === e.currentTarget) hideConfirmOverlay();
  });
}

/* ── Business setup (no businesses yet) ─────────────────────────────────── */
function showBusinessSetup() {
  // Placeholder: a future modal to add businesses
  alert("Add your businesses via Settings or the API at POST /api/businesses");
}

/* ── Toast notification ──────────────────────────────────────────────────── */
function showToast(msg) {
  let t = document.getElementById("bills-toast");
  if (!t) {
    t = document.createElement("div");
    t.id = "bills-toast";
    t.style.cssText = `
      position:fixed;bottom:24px;left:50%;transform:translateX(-50%) translateY(20px);
      background:#1e2430;border:1px solid var(--border-strong);border-radius:12px;
      padding:11px 18px;font-size:13px;font-weight:600;color:var(--text);
      box-shadow:0 8px 32px rgba(0,0,0,.4);z-index:9999;
      transition:transform .22s cubic-bezier(.4,0,.2,1),opacity .22s;opacity:0;pointer-events:none;`;
    document.body.appendChild(t);
  }
  t.textContent = msg;
  t.style.transform = "translateX(-50%) translateY(0)";
  t.style.opacity = "1";
  clearTimeout(t._timer);
  t._timer = setTimeout(() => {
    t.style.opacity = "0";
    t.style.transform = "translateX(-50%) translateY(10px)";
  }, 3500);
}

/* ── Formatters ──────────────────────────────────────────────────────────── */
function fmt(n) {
  if (!n && n !== 0) return "";
  return "$" + Number(n).toLocaleString("en-US", { minimumFractionDigits:2, maximumFractionDigits:2 });
}
function fmtShort(n) {
  if (n >= 1000000) return "$" + (n/1000000).toFixed(1) + "M";
  if (n >= 1000)    return "$" + (n/1000).toFixed(0) + "k";
  return "$" + Math.round(n);
}

/* ── Bill document viewer ────────────────────────────────────────────────── */
let viewerBill = null;

async function openBillViewer(billId) {
  viewerBill = bills.find(b => b.bill_id === billId) || null;
  if (!viewerBill) return;

  const overlay = document.getElementById("bv-overlay");
  if (!overlay) return;

  // Header
  const biz = businesses.find(b => b.business_id === viewerBill.business_id);
  const bizName  = viewerBill.business_name  || biz?.name  || "Unassigned";
  const bizEmoji = viewerBill.business_emoji || biz?.emoji || "🏢";
  document.getElementById("bv-vendor").textContent =
    `${vendorIcon(viewerBill.vendor)} ${viewerBill.vendor}`;
  document.getElementById("bv-meta").textContent = [
    viewerBill.amount ? fmt(viewerBill.amount) : null,
    viewerBill.due_date
      ? "Due " + new Date(viewerBill.due_date + "T00:00:00")
          .toLocaleDateString("en-US", { month:"short", day:"numeric", year:"numeric" })
      : null,
    `${bizEmoji} ${bizName}`,
  ].filter(Boolean).join("  ·  ");

  // Footer
  const srcMap = { gmail:"📧 Gmail", "google-drive":"📁 Google Drive", upload:"📤 Uploaded" };
  document.getElementById("bv-source").textContent = srcMap[viewerBill.source] || (viewerBill.source || "");
  document.getElementById("bv-invoice").textContent =
    viewerBill.invoice_number ? `# ${viewerBill.invoice_number}` : "";
  document.getElementById("bv-dates").textContent = [
    viewerBill.issue_date ? `Issued ${viewerBill.issue_date}` : null,
    viewerBill.due_date  ? `Due ${viewerBill.due_date}`   : null,
  ].filter(Boolean).join("  ·  ");

  const statusEl = document.getElementById("bv-status");
  const statusLabel = { overdue:"Overdue", "due-soon":"Due Soon", pending:"Pending",
    paid:"Paid", processing:"Processing" }[viewerBill.status] || viewerBill.status;
  statusEl.textContent = statusLabel;
  statusEl.className = `bv-status-badge ${viewerBill.status}`;

  const paidBtn = document.getElementById("bv-paid-btn");
  if (paidBtn) paidBtn.disabled = viewerBill.status === "paid";

  // Show loading
  document.getElementById("bv-doc").innerHTML = `
    <div class="bv-doc-loading">
      <div class="scan-dots"><span></span><span></span><span></span></div>
      <div style="margin-top:10px;font-size:12px;color:var(--muted)">Loading document…</div>
    </div>`;
  overlay.removeAttribute("hidden");

  // Fetch presigned / Drive URL
  try {
    const resp = await fetch(`/api/bills/${billId}/view`, {cache: "no-store"});
    const docEl = document.getElementById("bv-doc");
    if (!docEl) return;

    if (resp.ok) {
      const json = await resp.json();
      if (json.type === "pdf") {
        docEl.innerHTML = `<iframe src="${json.url}" title="Bill document"></iframe>`;
      } else if (json.type === "web") {
        docEl.innerHTML = `
          <div class="bv-no-doc">
            <div class="bv-no-doc-icon">📁</div>
            <div class="bv-no-doc-text">Stored in Google Drive</div>
            <div class="bv-no-doc-sub">Tap to open in Drive — embedded preview requires authentication.</div>
            <a class="bv-open-link" href="${json.url}" target="_blank" rel="noopener">
              Open in Google Drive →
            </a>
          </div>`;
      } else if (json.type === "gmail") {
        await _loadGmailBody(billId, json.url, docEl);
      } else {
        docEl.innerHTML = `<img src="${json.url}" alt="Bill document" />`;
      }
    } else {
      const src = viewerBill.source;
      const hint = src === "google-drive"
        ? "Drop this file into the upload zone to extract amount &amp; dates."
        : src === "gmail"
        ? "No PDF attachment was captured from this email."
        : "No document file stored for this bill.";
      docEl.innerHTML = `
        <div class="bv-no-doc">
          <div class="bv-no-doc-icon">🧾</div>
          <div class="bv-no-doc-text">No document available</div>
          <div class="bv-no-doc-sub">${hint}</div>
        </div>`;
    }
  } catch (e) {
    console.error("openBillViewer:", e);
    const docEl = document.getElementById("bv-doc");
    if (docEl) docEl.innerHTML = `
      <div class="bv-no-doc">
        <div class="bv-no-doc-icon">⚠️</div>
        <div class="bv-no-doc-text">Could not load document</div>
        <div class="bv-no-doc-sub">Check your connection and try again.</div>
      </div>`;
  }
}

function closeBillViewer() {
  document.getElementById("bv-overlay")?.setAttribute("hidden", "");
  // Remove transient Gmail link from footer
  document.getElementById("bv-gmail-link")?.remove();
  viewerBill = null;
}

async function _loadGmailBody(billId, gmailUrl, docEl) {
  // Show Gmail link in footer immediately
  const footerEl = document.getElementById("bv-source");
  if (footerEl && !document.getElementById("bv-gmail-link")) {
    const link = document.createElement("a");
    link.id = "bv-gmail-link";
    link.className = "bv-gmail-link";
    link.href = gmailUrl;
    link.target = "_blank";
    link.rel = "noopener";
    link.textContent = "Open in Gmail →";
    footerEl.insertAdjacentElement("afterend", link);
  }

  // Show spinner while fetching body
  docEl.innerHTML = `
    <div class="bv-doc-loading">
      <div class="scan-dots"><span></span><span></span><span></span></div>
      <div style="margin-top:10px;font-size:12px;color:var(--muted)">Loading email…</div>
    </div>`;

  try {
    const bodyResp = await fetch(`/api/bills/${billId}/email-body`);
    if (!bodyResp.ok) throw new Error("no body");
    const body = await bodyResp.json();

    if (body.type === "html") {
      docEl.innerHTML = `<div class="bv-email-wrap"><iframe class="bv-email-iframe" id="bv-email-iframe" sandbox="allow-same-origin allow-popups" title="Email body"></iframe></div>`;
      // Set srcdoc via property to avoid attribute-encoding issues
      document.getElementById("bv-email-iframe").srcdoc = body.content;
    } else if (body.type === "text") {
      const escaped = body.content.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
      docEl.innerHTML = `<div class="bv-email-text-wrap"><pre class="bv-email-text">${escaped}</pre></div>`;
    } else {
      throw new Error("empty");
    }
  } catch (_) {
    // Body unavailable — fall back to the Gmail link only
    docEl.innerHTML = `
      <div class="bv-no-doc">
        <div class="bv-no-doc-icon">📧</div>
        <div class="bv-no-doc-text">No attachment in this email</div>
        <div class="bv-no-doc-sub">Open the original email in Gmail to view its contents.</div>
        <a class="bv-open-link" href="${gmailUrl}" target="_blank" rel="noopener">Open in Gmail →</a>
      </div>`;
  }
}

async function markPaidFromViewer() {
  if (!viewerBill) return;
  await markPaid(viewerBill.bill_id);
  // Update viewer state without closing
  viewerBill.status = "paid";
  const statusEl = document.getElementById("bv-status");
  if (statusEl) { statusEl.textContent = "Paid"; statusEl.className = "bv-status-badge paid"; }
  const paidBtn = document.getElementById("bv-paid-btn");
  if (paidBtn) paidBtn.disabled = true;
}

function assignFromViewer() {
  if (!viewerBill) return;
  const bill = viewerBill;
  closeBillViewer();
  showConfirmOverlay(bill);
}

function bindBillViewer() {
  document.getElementById("bv-overlay")?.addEventListener("click", e => {
    if (e.target === e.currentTarget) closeBillViewer();
  });
  document.addEventListener("keydown", e => {
    if (e.key === "Escape" && !document.getElementById("bv-overlay")?.hasAttribute("hidden")) {
      closeBillViewer();
    }
  });
}

/* ── Check Payments (Gmail auto-pay detection) ───────────────────────────── */
async function triggerCheckPayments() {
  const btn = document.getElementById("check-payments-btn");
  if (btn) { btn.disabled = true; btn.textContent = "🔍 Checking…"; }
  try {
    const resp = await fetch("/api/bills/check-payments", { method: "POST" });
    const json = await resp.json();
    const n = json.count || 0;
    if (n > 0) {
      await reload();
      showToast(`✓ Marked ${n} bill${n !== 1 ? "s" : ""} as paid via Gmail confirmation`);
    } else {
      showToast("✓ No new payment confirmations found in Gmail");
    }
  } catch (e) {
    console.error("checkPayments:", e);
    showToast("Check failed — see console");
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = "🔍 Check Payments"; }
  }
}

/* ── Hook into app.js view switch ───────────────────────────────────────── */
window._billsInit = initBilling;
