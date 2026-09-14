/* ── Billing & Invoices — real API, no mock data ── */

/* ── HTML escaping ───────────────────────────────────────────────────────
   Every string this file interpolates into innerHTML originates outside our
   trust boundary: `vendor` is parsed from an email's From header or a Drive
   filename, `invoice_number` and `category` come out of a model reading an
   attacker-supplied document, and business names are free text. None of it
   was escaped, so a bill titled `<img src=x onerror=…>` executed as soon as
   the Billing tab rendered. app.js has escapeHtml() but throws on non-strings;
   esc() coerces first so a null vendor or a numeric amount can't break a
   render. Use esc() for text and escAttr() inside a quoted attribute. */
function esc(v) {
  if (v === null || v === undefined) return "";
  return String(v).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}
const escAttr = esc;

/* Colours land in `style="…"` where escaping alone wouldn't stop a value like
   `red;background:url(…)` from injecting extra declarations. Only allow the
   hex/rgb/hsl forms the API actually produces. */
const _COLOR_RE = /^(#[0-9a-f]{3,8}|rgba?\([\d\s.,%]+\)|hsla?\([\d\s.,%]+\))$/i;
function safeColor(v, fallback = "#9aa3b2") {
  const s = String(v ?? "").trim();
  return _COLOR_RE.test(s) ? s : fallback;
}

/* Only ever put http(s) URLs in an href/src — never a javascript: or data:
   URL that reached us through an API payload. */
function safeUrl(v) {
  try {
    const u = new URL(String(v), window.location.origin);
    return (u.protocol === "http:" || u.protocol === "https:") ? u.href : "";
  } catch { return ""; }
}

/* ── State ─────────────────────────────────────────────────────────────── */
// Business scope (which business's bills are shown, "all" or a slug) lives in
// the shared HermesBiz module (static/business.js) — bills.js just reads
// HermesBiz.current / HermesBiz.list and re-renders on HermesBiz.onChange.
let bills        = [];   // from GET /api/bills
let insights     = {};   // from response.insights
let filterStatus = "all";
let searchQuery  = "";
let scanning     = false;
let billsInitialized = false;

/* Real businesses only (excludes the synthetic "All Businesses" entry) —
   used anywhere we need an actual business row (assign dialog, bill cards). */
function realBusinesses() {
  return HermesBiz.list.filter((b) => b.slug !== "all");
}

/* The business_id (uuid) the current HermesBiz selection maps to, or null
   for "all" / an unmapped slug — used to filter the bills list client-side. */
function currentBusinessId() {
  if (HermesBiz.current === "all") return null;
  return HermesBiz.find(HermesBiz.current)?.business_id || null;
}

/* ── Entry point ─────────────────────────────────────────────────────────── */
async function initBilling() {
  if (billsInitialized) return;
  billsInitialized = true;
  renderSkeleton();
  await loadBills();
  renderAll();
  // The register (the Google Sheet) is the default view: it is the source of
  // truth for recurring bills. This tab's own bill list is the document store
  // behind it, not a second register. Guarded so a register failure can never
  // take the document view down with it.
  try {
    if (typeof initRegister === "function") await initRegister();
    if (typeof setBillingMode === "function") setBillingMode("register");
  } catch (e) {
    console.error("register init:", e);
  }
  bindDropzone();
  bindConfirmOverlay();
  bindBillViewer();
  HermesBiz.bind(
    document.getElementById("hermes-biz-select-bills"),
    document.getElementById("hermes-biz-badge-bills")
  );
  HermesBiz.onChange(() => {
    // The stats row is shared by both modes, so only the active one may paint
    // it — otherwise switching business while reading the register replaces its
    // figures with the document counts.
    renderActiveStats();
    renderToolbar();
    renderBillsList();
  });

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

/* Paint whichever mode's figures are actually on screen. */
function renderActiveStats() {
  if (typeof billingMode !== "undefined" && billingMode === "register"
      && typeof renderRegisterStats === "function") {
    renderRegisterStats();
  } else {
    renderStats();
  }
}

async function reload() {
  await loadBills();
  renderActiveStats();
  renderBillsList();
  renderInsights();
}

/* ── Full render pass ────────────────────────────────────────────────────── */
function renderAll() {
  renderActiveStats();
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

/* ── Stats row ───────────────────────────────────────────────────────────── */
/* Documents mode is Hermes' evidence vault, not a register. Money lives in the
   Google Sheet, which is the source of truth for what is owed — so this view
   showed "$0.00 Overdue / $0.00 Due This Week / $0.00 Total Outstanding"
   forever, because these rows carry documents, not amounts. Three zeroes and a
   count is not a dashboard; it is three wrong answers to questions this view
   cannot answer. Report coverage instead, which is what it does know. */
function renderStats() {
  const row = document.getElementById("bills-stats-row");
  if (!row) return;
  const bizId = currentBusinessId();
  const scoped = HermesBiz.current === "all" ? bills : bills.filter(b => b.business_id === bizId);

  const withDoc = scoped.filter(b => b.file_url);

  // Which captured documents the register already points at. Anything left over
  // is the interesting part: a bill we hold evidence for that no register line
  // claims — i.e. a candidate the fortnightly sweep may not have picked up.
  const linkedIds = new Set();
  const lines = (typeof registerLines !== "undefined" && registerLines) || [];
  for (const l of lines) for (const d of (l.documents || [])) linkedIds.add(d.bill_id);
  const linked = withDoc.filter(b => linkedIds.has(b.bill_id)).length;
  const unlinked = withDoc.length - linked;

  const tile = (value, label, color, note) => `
    <div class="bstat">
      <div class="bstat-val"${color ? ` style="color:${color}"` : ""}>${esc(String(value))}</div>
      <div class="bstat-label">${esc(label)}</div>
      ${note ? `<div class="bstat-note" style="font-size:10px;color:var(--muted)">${esc(note)}</div>` : ""}
    </div>`;

  row.innerHTML =
    tile(scoped.length, "Captured", "") +
    tile(withDoc.length, "With a document", "var(--ok)", "readable evidence") +
    tile(linked, "Linked to the register", "var(--accent)") +
    tile(unlinked, "Not in the register", unlinked ? "#f87171" : "", "possible missing lines");
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

  const biz = HermesBiz.find(HermesBiz.current);
  const notConnected = HermesBiz.current !== "all" && biz && !biz.connected;
  const scanLabel = scanning
    ? `<span class="scan-dots"><span></span><span></span><span></span></span> Scanning…`
    : notConnected ? "⚠ Not connected yet" : "⚡ Scan Connected Apps";

  tb.innerHTML = `
    <div class="bills-search">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
      </svg>
      <input type="text" placeholder="Search vendors, invoices…" id="bills-search-input" value="${escAttr(searchQuery)}"/>
    </div>
    <select class="bills-filter-sel" id="bills-status-filter">
      <option value="all">All Status</option>
      <option value="overdue">Overdue</option>
      <option value="due-soon">Due Soon</option>
      <option value="pending">Pending</option>
      <option value="paid">Paid</option>
      <option value="processing">Processing</option>
    </select>
    <button class="bv-action-btn" id="scan-btn" onclick="triggerScan()" ${notConnected ? "disabled" : ""}
            style="flex-shrink:0;white-space:nowrap;color:var(--accent);border-color:rgba(245,166,35,.3)">
      ${scanLabel}
    </button>
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
  if (HermesBiz.current !== "all") {
    const bizId = currentBusinessId();
    visible = visible.filter(b => b.business_id === bizId);
  }
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

/* Status is used as a CSS class and an onclick argument, so keep it to the
   known set rather than trusting whatever the API sent. */
const STATUS_LABELS = { overdue:"Overdue", "due-soon":"Due Soon", pending:"Pending",
  paid:"Paid", processing:"Scanning…" };
function safeStatus(s) {
  return Object.prototype.hasOwnProperty.call(STATUS_LABELS, s) ? s : "pending";
}

function billCardHTML(bill) {
  const biz = realBusinesses().find(b => b.business_id === bill.business_id);
  const bizColor  = safeColor(bill.business_color || biz?.color);
  const bizName   = bill.business_name   || biz?.name   || "Unassigned";

  const vendorEmoji = vendorIcon(bill.vendor);
  const dueDateStr = formatDueDate(bill.due_date, { month:"short", day:"numeric" });
  const status = safeStatus(bill.status);
  const statusLabel = STATUS_LABELS[status];

  const needsConfirm = (bill.confidence ?? 1) < 0.75 && status !== "paid" && status !== "processing";
  const confBadge = needsConfirm
    ? `<button class="bill-confidence" onclick="event.stopPropagation();showConfirmFor('${escAttr(bill.bill_id)}')">🤔 Confirm business</button>` : "";

  const metaRow = status === "processing"
    ? `<div class="bill-meta"><span style="color:var(--blue);font-size:12px">Scanning <span class="scan-dots"><span></span><span></span><span></span></span></span></div>`
    : `<div class="bill-meta">
         ${bill.invoice_number ? `<span class="bill-inv">${esc(bill.invoice_number)}</span>` : ""}
         <span class="bill-biz-tag" style="background:${bizColor}18;border-color:${bizColor}33;color:${bizColor}">
           <span class="bill-biz-dot" style="background:${bizColor}"></span>${esc(bizName)}
         </span>
         ${bill.category ? `<span class="bill-inv">· ${esc(bill.category)}</span>` : ""}
         ${bill.source && bill.source !== "upload"
           ? `<span class="bill-inv" title="from ${escAttr(bill.source)}">· ${bill.source === "gmail" ? "📧" : "📁"}</span>` : ""}
       </div>
       ${confBadge}`;

  return `
    <div class="bill-item ${status}" data-id="${escAttr(bill.bill_id)}"
         onclick="openBillViewer('${escAttr(bill.bill_id)}')">
      <div class="bill-avatar">${vendorEmoji}</div>
      <div class="bill-info">
        <div class="bill-vendor">${esc(bill.vendor)}</div>
        ${metaRow}
      </div>
      <div class="bill-right">
        ${status !== "processing" && bill.amount ? `<div class="bill-amount">${esc(fmt(bill.amount, bill.currency))}</div>` : ""}
        ${dueDateStr ? `<div class="bill-due">${status === "overdue" ? `<span style="color:var(--err)">` : ""}Due ${esc(dueDateStr)}${status === "overdue" ? "</span>" : ""}</div>` : ""}
        <div class="bill-status ${status}">${statusLabel}</div>
      </div>
    </div>`;
}

/* Turn a Drive share link into its embeddable /preview form.
   https://drive.google.com/file/d/<id>/view?usp=… -> …/file/d/<id>/preview
   Returns "" for anything that isn't a recognised Drive file link. */
function drivePreviewUrl(url) {
  try {
    const u = new URL(url);
    if (!/(^|\.)drive\.google\.com$/.test(u.hostname)) return "";
    const m = u.pathname.match(/^\/file\/d\/([^/]+)/);
    if (m) return `https://drive.google.com/file/d/${m[1]}/preview`;
    const id = u.searchParams.get("id");
    return id ? `https://drive.google.com/file/d/${id}/preview` : "";
  } catch { return ""; }
}

/* `new Date("2026-13-45T00:00:00")` is Invalid Date, and toLocaleDateString
   then renders the literal string "Invalid Date" in the due column. */
function formatDueDate(value, opts) {
  if (!value) return "";
  const d = new Date(String(value).slice(0, 10) + "T00:00:00");
  return isNaN(d.getTime()) ? "" : d.toLocaleDateString("en-US", opts);
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

  // Upload all files in one multipart POST — scope to the active business
  // (if one is selected) so uploads don't need AI-guessing.
  const form = new FormData();
  files.forEach(f => form.append("files", f));
  const bizId = currentBusinessId();
  if (bizId) form.append("business_id", bizId);

  let created = [];
  let requestFailed = false;
  try {
    const resp = await fetch("/api/bills/upload", { method: "POST", body: form });
    const json = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      requestFailed = true;
      showToast("Upload failed: " + (json.error || resp.status));
    }
    created = json.created || [];
  } catch (e) {
    console.error("Upload failed:", e);
    requestFailed = true;
    showToast("Upload failed — check your connection");
  }

  // Remove placeholders, reload from API
  bills = bills.filter(b => !String(b.bill_id || "").startsWith("proc_"));
  await loadBills();
  renderAll();

  // Per-file failures came back inside `created` and were never looked at, so
  // a rejected file just made its placeholder vanish with no explanation.
  const failed = created.filter(c => c.error);
  if (failed.length && !requestFailed) {
    const first = failed[0];
    const more = failed.length > 1 ? ` (+${failed.length - 1} more)` : "";
    showToast(
      first.bill_id
        ? `⚠ Couldn't read "${first.filename}" — saved for manual entry${more}`
        : `⚠ ${first.filename}: ${first.error}${more}`
    );
    console.warn("Bill upload problems:", failed);
  }

  // If any need business confirmation, show dialog for first one
  const needsConfirm = created.find(c => c.needs_confirmation && c.bill_id);
  if (needsConfirm) {
    const fullBill = bills.find(b => b.bill_id === needsConfirm.bill_id);
    if (fullBill) showConfirmOverlay(fullBill);
  }
}

/* ── Connected-app scan ──────────────────────────────────────────────────── */
// Scans exactly one business per request (the backend rejects a "loop
// everything" style call — attachment downloads across many businesses in one
// request risk a platform timeout). For "All Businesses", we fan out here,
// one request per connected business, sequentially.

async function _scanOne(slug, days) {
  const resp = await fetch("/api/bills/scan", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({ business: slug, sources: ["gmail", "google-drive"], days }),
  });
  const json = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(json.error || `scan failed (${resp.status})`);
  return json;
}

async function triggerScan() {
  if (scanning) return;

  const firstDone = localStorage.getItem("billsFirstScanDone");
  const days = firstDone ? 7 : 45;

  const targets = HermesBiz.current === "all"
    ? realBusinesses().filter(b => b.connected)
    : [HermesBiz.find(HermesBiz.current)].filter(Boolean);

  if (!targets.length) {
    const biz = HermesBiz.find(HermesBiz.current);
    showToast(biz && !biz.connected
      ? `⚠ '${biz.name}' has no Maton connection configured yet`
      : "No connected businesses to scan");
    return;
  }

  scanning = true;
  renderToolbar();

  let total = 0;
  let failures = 0;
  try {
    for (const biz of targets) {
      if (targets.length > 1) showToast(`⚡ Scanning ${biz.name}…`);
      try {
        const json = await _scanOne(biz.slug, days);
        total += json.scanned || 0;
        // The endpoint now reports per-source failures instead of quietly
        // returning scanned:0, so a Gmail outage no longer reads as "all clear".
        if (json.errors && json.errors.length) {
          failures++;
          console.warn(`Scan problems for ${biz.slug}:`, json.errors);
        }
      } catch (e) {
        failures++;
        console.error(`Scan failed for ${biz.slug}:`, e);
      }
    }
    // Only retire the 45-day backfill once a scan has actually succeeded.
    // Marking it done unconditionally meant one failed first scan permanently
    // downgraded the user to a 7-day window and their history was never imported.
    if (failures < targets.length) localStorage.setItem("billsFirstScanDone", "1");
    localStorage.setItem("lastBillScan", Date.now().toString());

    if (total > 0) {
      await reload();
      showToast(`⚡ Found ${total} new bill${total !== 1 ? "s" : ""} from your connected apps`
                + (failures ? ` · ${failures} account${failures !== 1 ? "s" : ""} had errors` : ""));
    } else if (failures === targets.length) {
      showToast(`⚠ Scan failed for ${failures === 1 ? "that account" : "all accounts"} — see console`);
    } else if (failures) {
      showToast(`⚠ Scan partly failed — no new bills from the accounts that responded`);
    } else {
      showToast(`✓ Scan complete — no new bills (checked last ${days} days)`);
    }
  } finally {
    scanning = false;
    renderToolbar();
  }
}

async function _autoScan() {
  // Silent background scan — no spinner, just reloads if new bills found.
  if (scanning) return;   // don't race a scan the user started
  const targets = HermesBiz.current === "all"
    ? realBusinesses().filter(b => b.connected)
    : [HermesBiz.find(HermesBiz.current)].filter(b => b && b.connected);
  if (!targets.length) return;

  scanning = true;
  let total = 0;
  try {
    for (const biz of targets) {
      try {
        const json = await _scanOne(biz.slug, 7);
        total += json.scanned || 0;
      } catch (e) {
        console.debug(`Auto-scan error (${biz.slug}):`, e);
      }
    }
  } finally {
    scanning = false;
  }
  localStorage.setItem("lastBillScan", Date.now().toString());
  if (total > 0) {
    await reload();
    showToast(`⚡ Auto-found ${total} new bill${total !== 1 ? "s" : ""}`);
  }
}

/* ── Insights ────────────────────────────────────────────────────────────── */
/* The money insights below (overdue, upcoming, cash flow) are computed from the
   bills table, which holds documents rather than amounts — so they rendered as
   empty groups and a row of $0 bars sitting next to real figures. The register
   owns money now. What this column can say that nothing else does is which
   captured documents the register has NOT claimed: evidence for a bill that may
   be missing from it entirely. */
function renderUnclaimedDocuments(el, header) {
  const lines = (typeof registerLines !== "undefined" && registerLines) || [];
  const linked = new Set();
  for (const l of lines) for (const d of (l.documents || [])) linked.add(d.bill_id);

  const unclaimed = bills.filter(b => b.file_url && !linked.has(b.bill_id));
  if (!lines.length) {
    el.innerHTML = header + `
      <div class="insight-group" style="padding:16px 14px;text-align:center;color:var(--muted);font-size:12px">
        Load the register to see which documents it is missing.
      </div>`;
    return;
  }
  if (!unclaimed.length) {
    el.innerHTML = header + `
      <div class="insight-group" style="padding:16px 14px;text-align:center;color:var(--muted);font-size:12px">
        ✓ Every stored document is accounted for in the register.
      </div>`;
    return;
  }

  el.innerHTML = header + `
    <div class="insight-group">
      <div class="insight-group-head">
        <span class="ig-emoji">🔎</span>
        <div>
          <div class="ig-title">Not in the register</div>
          <div class="ig-sub">Documents no register line claims</div>
        </div>
        <span class="ig-count" style="color:#f87171">${unclaimed.length}</span>
      </div>
      ${unclaimed.slice(0, 12).map(b => `
        <div class="insight-row">
          <div class="ir-dot" style="background:#f87171"></div>
          <div class="ir-body">
            <div class="ir-text">${esc(b.vendor || "Unknown vendor")}</div>
            <button class="ir-action" onclick="openBillViewer('${escAttr(b.bill_id)}')">Open the document →</button>
          </div>
        </div>`).join("")}
      ${unclaimed.length > 12
        ? `<div style="padding:6px 12px 10px;font-size:11px;color:var(--muted)">+ ${unclaimed.length - 12} more</div>`
        : ""}
    </div>`;
}

function renderInsights() {
  const el = document.getElementById("bills-insights");
  if (!el) return;

  const header = `
    <div class="insights-col-header">
      🔎 Coverage <span class="insights-live-badge">Live</span>
    </div>`;

  if (!bills.length) {
    el.innerHTML = header + `
      <div class="insight-group" style="padding:20px 14px;text-align:center;color:var(--muted);font-size:12px">
        Upload bills or scan connected apps to capture documents
      </div>`;
    return;
  }

  renderUnclaimedDocuments(el, header);
}

/* ── Mark paid ───────────────────────────────────────────────────────────── */
async function markPaid(billId) {
  try {
    const resp = await fetch(`/api/bills/${billId}`, {
      method: "PATCH",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({ status: "paid" }),
    });
    // A failed PATCH used to be indistinguishable from a successful one: the
    // response was never inspected, so the row silently reverted on reload and
    // the user assumed the click hadn't registered.
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      showToast("Couldn't mark as paid: " + (err.error || resp.status));
      return false;
    }
    await reload();
    return true;
  } catch (e) {
    console.error("markPaid:", e);
    showToast("Couldn't mark as paid — check your connection");
    return false;
  }
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

  const bizList = realBusinesses();
  if (grid && bizList.length) {
    grid.innerHTML = bizList.map(biz => {
      const color = safeColor(biz.color);
      const isGuess = bill.business_id === biz.business_id;
      return `
      <button class="ai-biz-btn" onclick="assignBill('${escAttr(biz.business_id)}')"
              style="${isGuess ? `border-color:${color};box-shadow:0 0 0 1px ${color}44` : ""}">
        <span class="ai-biz-icon">${esc(biz.emoji)}</span>
        <span class="ai-biz-name">${esc(biz.name)}</span>
        <span class="ai-biz-industry">${esc(biz.industry || "")}</span>
        ${isGuess ? `<span class="ai-suggested-badge">AI Guess</span>` : ""}
      </button>`;
    }).join("");
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
    const resp = await fetch(`/api/bills/${pendingBillId}`, {
      method: "PATCH",
      headers: {"Content-Type":"application/json"},
      body: JSON.stringify({ business_id: bizId }),
    });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      showToast("Couldn't file that bill: " + (err.error || resp.status));
      return;   // keep the overlay open so the choice isn't silently lost
    }
  } catch (e) {
    console.error("assignBill:", e);
    showToast("Couldn't file that bill — check your connection");
    return;
  }
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
/* Format in the bill's own currency. Hard-coding "$" mislabelled every
   non-USD bill — and the extractor reads the currency off the document, so
   EUR and GBP invoices really do come through. */
function fmt(n, currency) {
  if (n === null || n === undefined || n === "" || isNaN(Number(n))) return "";
  const cur = String(currency || "USD").toUpperCase();
  try {
    return Number(n).toLocaleString("en-US", {
      style: "currency", currency: cur,
      minimumFractionDigits: 2, maximumFractionDigits: 2,
    });
  } catch {
    // Not a valid ISO 4217 code (the extractor can return junk) — show the
    // number with the code beside it rather than throwing mid-render.
    return Number(n).toLocaleString("en-US", { minimumFractionDigits:2, maximumFractionDigits:2 }) + " " + cur;
  }
}


/* ── Bill document viewer ────────────────────────────────────────────────── */
let viewerBill = null;

async function openBillViewer(billId) {
  viewerBill = bills.find(b => b.bill_id === billId) || null;
  if (!viewerBill) return;

  const overlay = document.getElementById("bv-overlay");
  if (!overlay) return;

  // Header
  const biz = realBusinesses().find(b => b.business_id === viewerBill.business_id);
  const bizName  = viewerBill.business_name  || biz?.name  || "Unassigned";
  const bizEmoji = viewerBill.business_emoji || biz?.emoji || "🏢";
  document.getElementById("bv-vendor").textContent =
    `${vendorIcon(viewerBill.vendor)} ${viewerBill.vendor}`;
  const dueLong = formatDueDate(viewerBill.due_date, { month:"short", day:"numeric", year:"numeric" });
  document.getElementById("bv-meta").textContent = [
    viewerBill.amount ? fmt(viewerBill.amount, viewerBill.currency) : null,
    dueLong ? "Due " + dueLong : null,
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
  const vStatus = safeStatus(viewerBill.status);
  statusEl.textContent = STATUS_LABELS[vStatus];
  statusEl.className = `bv-status-badge ${vStatus}`;

  const paidBtn = document.getElementById("bv-paid-btn");
  if (paidBtn) paidBtn.disabled = viewerBill.status === "paid";

  fillBillEditor(viewerBill);

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
      // file_url can be a Drive webViewLink stored from an API response, so
      // pin it to http(s) before it becomes an href or an iframe src.
      const url = safeUrl(json.url);
      if (!url) {
        docEl.innerHTML = `
          <div class="bv-no-doc">
            <div class="bv-no-doc-icon">⚠️</div>
            <div class="bv-no-doc-text">Document link looks invalid</div>
            <div class="bv-no-doc-sub">This bill's stored link isn't a usable web address.</div>
          </div>`;
      } else if (json.type === "pdf") {
        docEl.innerHTML = `<iframe src="${escAttr(url)}" title="Bill document"></iframe>`;
      } else if (json.type === "image") {
        docEl.innerHTML = `<img src="${escAttr(url)}" alt="Bill document" />`;
      } else if (json.type === "download") {
        // HEIC and friends: no browser renders them inline, so offer the file
        // rather than an <img> that stays permanently blank.
        docEl.innerHTML = `
          <div class="bv-no-doc">
            <div class="bv-no-doc-icon">📎</div>
            <div class="bv-no-doc-text">Preview not supported</div>
            <div class="bv-no-doc-sub">This file type can't be shown in the browser.</div>
            <a class="bv-open-link" href="${escAttr(url)}" target="_blank" rel="noopener noreferrer">
              Open the file →
            </a>
          </div>`;
      } else if (json.type === "web") {
        // Drive's /view page refuses to be framed, but its /preview endpoint is
        // built for embedding. Try that first so Drive bills actually show
        // their document instead of only ever offering a link; the link stays
        // underneath for when the browser isn't signed in to that account.
        const embed = drivePreviewUrl(url);
        docEl.innerHTML = embed
          ? `<div class="bv-drive-wrap">
               <iframe class="bv-drive-iframe" src="${escAttr(embed)}" title="Bill document"
                       referrerpolicy="no-referrer-when-downgrade"></iframe>
               <a class="bv-drive-link" href="${escAttr(url)}" target="_blank" rel="noopener noreferrer">
                 Not showing? Open in Google Drive →
               </a>
             </div>`
          : `<div class="bv-no-doc">
               <div class="bv-no-doc-icon">📁</div>
               <div class="bv-no-doc-text">Stored in Google Drive</div>
               <div class="bv-no-doc-sub">Tap to open in Drive.</div>
               <a class="bv-open-link" href="${escAttr(url)}" target="_blank" rel="noopener noreferrer">
                 Open in Google Drive →
               </a>
             </div>`;
      } else if (json.type === "gmail") {
        await _loadGmailBody(billId, url, docEl);
      } else {
        docEl.innerHTML = `<img src="${escAttr(url)}" alt="Bill document" />`;
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

/* ── Editable bill details ───────────────────────────────────────────────
   Most bills reach us as stubs: a Drive scan only ever knows a filename, and a
   Gmail scan only what the subject line happens to say. Without a way to fill
   in the amount and due date by hand, those bills sat at "pending" forever,
   contributed nothing to any total, and could never surface as due or overdue.
   Values are written via the same PATCH the rest of the UI uses. */
function fillBillEditor(bill) {
  const amount   = document.getElementById("bv-edit-amount");
  const currency = document.getElementById("bv-edit-currency");
  const due      = document.getElementById("bv-edit-due");
  const invoice  = document.getElementById("bv-edit-invoice");
  const status   = document.getElementById("bv-edit-status");
  if (!amount) return;
  amount.value   = bill.amount ?? "";
  currency.value = bill.currency || "USD";
  // A date input only accepts yyyy-mm-dd; anything else must clear it rather
  // than silently keeping the previous bill's value.
  due.value      = /^\d{4}-\d{2}-\d{2}$/.test(String(bill.due_date || "")) ? bill.due_date : "";
  invoice.value  = bill.invoice_number || "";
  if (status) status.textContent = "";
}

async function saveBillDetails(event) {
  event.preventDefault();
  if (!viewerBill) return false;

  const statusEl = document.getElementById("bv-edit-status");
  const saveBtn  = document.getElementById("bv-save-btn");
  const rawAmount = document.getElementById("bv-edit-amount").value.trim();
  const rawCur    = document.getElementById("bv-edit-currency").value.trim().toUpperCase();
  const rawDue    = document.getElementById("bv-edit-due").value;
  const rawInv    = document.getElementById("bv-edit-invoice").value.trim();

  if (rawAmount !== "" && !isFinite(Number(rawAmount))) {
    if (statusEl) statusEl.textContent = "Amount must be a number";
    return false;
  }
  if (rawCur && !/^[A-Z]{3}$/.test(rawCur)) {
    if (statusEl) statusEl.textContent = "Currency must be a 3-letter code";
    return false;
  }

  // Send nulls explicitly — that is how a wrong value gets cleared.
  const payload = {
    amount:         rawAmount === "" ? null : Number(rawAmount),
    currency:       rawCur || "USD",
    due_date:       rawDue || null,
    invoice_number: rawInv || null,
  };

  const billId = viewerBill.bill_id;
  if (saveBtn) { saveBtn.disabled = true; saveBtn.textContent = "Saving…"; }
  if (statusEl) statusEl.textContent = "";
  try {
    const resp = await fetch(`/api/bills/${billId}`, {
      method: "PATCH",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
    });
    const json = await resp.json().catch(() => ({}));
    if (!resp.ok) {
      if (statusEl) statusEl.textContent = json.error || `Save failed (${resp.status})`;
      return false;
    }
  } catch (e) {
    console.error("saveBillDetails:", e);
    if (statusEl) statusEl.textContent = "Save failed — check your connection";
    return false;
  } finally {
    if (saveBtn) { saveBtn.disabled = false; saveBtn.textContent = "Save"; }
  }

  await reload();
  // Re-read the saved bill so the header, status badge and editor all reflect
  // the derived status the server just recomputed from the new due date.
  const fresh = bills.find(b => b.bill_id === billId);
  if (fresh) {
    viewerBill = fresh;
    const dueLong = formatDueDate(fresh.due_date, { month:"short", day:"numeric", year:"numeric" });
    const bizName = fresh.business_name || "Unassigned";
    const bizEmoji = fresh.business_emoji || "🏢";
    document.getElementById("bv-meta").textContent = [
      fresh.amount ? fmt(fresh.amount, fresh.currency) : null,
      dueLong ? "Due " + dueLong : null,
      `${bizEmoji} ${bizName}`,
    ].filter(Boolean).join("  ·  ");
    const badge = document.getElementById("bv-status");
    if (badge) {
      const s = safeStatus(fresh.status);
      badge.textContent = STATUS_LABELS[s];
      badge.className = `bv-status-badge ${s}`;
    }
    fillBillEditor(fresh);
  }
  if (statusEl) statusEl.textContent = "Saved ✓";
  return false;
}

function closeBillViewer() {
  document.getElementById("bv-overlay")?.setAttribute("hidden", "");
  // Remove transient Gmail link from footer
  document.getElementById("bv-gmail-link")?.remove();
  // Leave the delete button disarmed, or reopening the viewer on a different
  // bill shows a primed "Confirm delete?" that deletes on a single click.
  _disarmDelete(document.getElementById("bv-delete-btn"));
  // Stop an in-flight document/email load from painting into the next bill.
  const docEl = document.getElementById("bv-doc");
  if (docEl) docEl.innerHTML = "";
  viewerBill = null;
}

async function _loadGmailBody(billId, gmailUrl, docEl) {
  gmailUrl = safeUrl(gmailUrl);
  // Show Gmail link in footer immediately
  const footerEl = document.getElementById("bv-source");
  if (footerEl && gmailUrl && !document.getElementById("bv-gmail-link")) {
    const link = document.createElement("a");
    link.id = "bv-gmail-link";
    link.className = "bv-gmail-link";
    link.href = gmailUrl;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = "Open in Gmail →";
    footerEl.insertAdjacentElement("afterend", link);
  }

  // Show spinner while fetching body
  docEl.innerHTML = `
    <div class="bv-doc-loading">
      <div class="scan-dots"><span></span><span></span><span></span></div>
      <div style="margin-top:10px;font-size:12px;color:var(--muted)">Loading email…</div>
    </div>`;

  let reason = "";
  try {
    const bodyResp = await fetch(`/api/bills/${billId}/email-body`);
    if (!bodyResp.ok) {
      const err = await bodyResp.json().catch(() => ({}));
      reason = err.error || "";
      throw new Error("no body");
    }
    const body = await bodyResp.json();

    if (body.type === "html") {
      // `allow-same-origin` put untrusted vendor email HTML into OUR origin —
      // only the absence of `allow-scripts` was keeping it inert, one careless
      // edit away from full same-origin script execution. A fully sandboxed
      // frame renders the email just as well. The CSP additionally blocks the
      // remote image loads that make marketing invoices into tracking pixels.
      docEl.innerHTML = `<div class="bv-email-wrap"><iframe class="bv-email-iframe" id="bv-email-iframe" sandbox="" referrerpolicy="no-referrer" title="Email body"></iframe></div>`;
      // Deny-list the dangerous capabilities rather than starting from
      // `default-src 'none'`: inside sandbox="" the document has an opaque
      // origin, and a blanket default-src blocks the srcdoc's own styles and
      // images too — the email rendered as a blank white page. This keeps the
      // protection that matters (no script execution, no plugins, no nested
      // frames, no form posts to a phishing endpoint) while the email still
      // displays. sandbox="" independently blocks scripts, forms and popups.
      // Remote images do load, so a tracking pixel can fire; referrerpolicy
      // keeps our URL out of that request.
      const csp = `<meta http-equiv="Content-Security-Policy" content="script-src 'none'; object-src 'none'; frame-src 'none'; form-action 'none'; base-uri 'none'">`;
      // Set srcdoc via property to avoid attribute-encoding issues
      document.getElementById("bv-email-iframe").srcdoc = csp + body.content;
    } else if (body.type === "text") {
      docEl.innerHTML = `<div class="bv-email-text-wrap"><pre class="bv-email-text">${esc(body.content)}</pre></div>`;
    } else {
      throw new Error("empty");
    }
  } catch (_) {
    // Body unavailable — say why. "No attachment in this email" was wrong and
    // unhelpful: the usual cause is that the message has since been deleted
    // from the mailbox, or that mailbox's connection has dropped.
    docEl.innerHTML = `
      <div class="bv-no-doc">
        <div class="bv-no-doc-icon">📧</div>
        <div class="bv-no-doc-text">Email preview unavailable</div>
        <div class="bv-no-doc-sub">${esc(reason || "Couldn't load this email's contents.")}</div>
        ${gmailUrl ? `<a class="bv-open-link" href="${escAttr(gmailUrl)}" target="_blank" rel="noopener noreferrer">Open in Gmail →</a>` : ""}
      </div>`;
  }
}

async function markPaidFromViewer() {
  if (!viewerBill) return;
  const billId = viewerBill.bill_id;
  const ok = await markPaid(billId);
  // Only claim "Paid" in the viewer if the server agreed. reload() replaces
  // the `bills` array, so re-resolve rather than mutating a detached object.
  if (!ok) return;
  viewerBill = bills.find(b => b.bill_id === billId) || viewerBill;
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

function _disarmDelete(btn) {
  if (!btn) return;
  clearTimeout(btn._disarm);
  delete btn.dataset.armed;
  btn.textContent = "🗑 Delete";
  btn.classList.remove("bv-delete-armed");
}

async function deleteFromViewer() {
  if (!viewerBill) return;
  const btn = document.getElementById("bv-delete-btn");

  // First click: arm the button (ask for confirmation)
  if (btn && !btn.dataset.armed) {
    btn.dataset.armed = "1";
    btn.textContent = "Confirm delete?";
    btn.classList.add("bv-delete-armed");
    // Auto-disarm after 4 seconds if not confirmed. Clearing cssText wiped
    // every inline style the button had, not just the three set here.
    clearTimeout(btn._disarm);
    btn._disarm = setTimeout(() => _disarmDelete(btn), 4000);
    return;
  }

  // Second click: confirmed — delete
  const billId = viewerBill.bill_id;
  if (btn) { clearTimeout(btn._disarm); btn.disabled = true; btn.textContent = "Deleting…"; }
  try {
    const resp = await fetch(`/api/bills/${billId}`, { method: "DELETE" });
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      showToast("Delete failed: " + (err.error || resp.status));
      if (btn) { btn.disabled = false; _disarmDelete(btn); }
      return;
    }
  } catch (e) {
    console.error("deleteFromViewer:", e);
    showToast("Delete failed — check console");
    if (btn) { btn.disabled = false; _disarmDelete(btn); }
    return;
  }

  closeBillViewer();
  await reload();
  showToast("🗑 Bill deleted");
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
