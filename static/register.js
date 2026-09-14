/* ── Subscriptions & bills register ──────────────────────────────────────────
   The Google Sheet owns this data. This view renders it, attaches the documents
   Hermes holds, and offers proposed amounts for the lines the Sheet records as
   "AMOUNT IN PDF - UNVERIFIED".

   Nothing here writes a figure on its own. A proposal is shown with the evidence
   it came from and the reasons it was believed, and only an explicit approval
   updates the Sheet — which then also writes a CHANGE LOG row. That is the
   register's own no-estimation rule, kept intact through the UI.

   Escaping note: every value below originates in a spreadsheet a human types
   into, so it is untrusted for rendering purposes and goes through esc() from
   bills.js.                                                                   */

let registerLines = [];
let registerSummary = {};
let registerDeadlines = [];
let regFilter = "all";
let regSearch = "";
let regBusy = false;
let regLoading = false;
let billingMode = "register";

/* ── Mode switch ─────────────────────────────────────────────────────────── */
function setBillingMode(mode) {
  billingMode = mode;
  document.querySelectorAll(".reg-tab").forEach((b) =>
    b.classList.toggle("is-active", b.dataset.mode === mode));
  const reg = document.getElementById("register-pane");
  const docs = document.getElementById("documents-pane");
  if (reg) reg.hidden = mode !== "register";
  if (docs) docs.hidden = mode !== "documents";
  if (mode === "register") {
    renderRegisterStats();
    renderRegister();
    // The register is fetched from the Sheets API through Maton, so the first
    // attempt can lose a race with server start-up or hit a transient error.
    // Entering the view with nothing loaded should retry, not sit empty.
    if (!registerLines.length && !regLoading) refreshRegister();
  } else {
    renderStats(); renderBillsList(); renderInsights();
  }
}

/* ── Load ────────────────────────────────────────────────────────────────── */
async function loadRegister(refresh = false) {
  regLoading = true;
  try {
    const r = await fetch(`/api/register${refresh ? "?refresh=true" : ""}`);
    const j = await r.json().catch(() => ({}));
    if (!r.ok) {
      showRegisterError(j.error || `Could not load the register (${r.status})`);
      return false;
    }
    registerLines = j.lines || [];
    registerSummary = j.summary || {};
    registerDeadlines = j.deadlines || [];
    const link = document.getElementById("reg-sheet-link");
    if (link && j.sheet_url) link.href = j.sheet_url;
    const lc = document.getElementById("reg-count-lines");
    if (lc) lc.textContent = registerLines.length;
    return true;
  } catch (e) {
    console.error("loadRegister:", e);
    showRegisterError("Couldn't reach the register — check your connection.");
    return false;
  } finally {
    regLoading = false;
  }
}

function showRegisterError(msg) {
  const list = document.getElementById("reg-list");
  if (list) list.innerHTML = `
    <div class="reg-empty">
      <div class="reg-empty-icon">📋</div>
      <div class="reg-empty-title">Register unavailable</div>
      <div class="reg-empty-sub">${esc(msg)}</div>
      <div style="margin-top:12px"><button class="bv-action-btn" onclick="refreshRegister()">Try again</button></div>
    </div>`;
}

/* ── Stats ───────────────────────────────────────────────────────────────── */
function renderRegisterStats() {
  const row = document.getElementById("bills-stats-row");
  if (!row) return;
  const s = registerSummary;
  const atRisk = registerLines.filter((l) => RISK_STATUSES.has((l.status || "").toUpperCase())).length;

  row.innerHTML = `
    <div class="bstat">
      <div class="bstat-val">${esc(fmt(s.verified_monthly_run_rate ?? 0, "USD"))}</div>
      <div class="bstat-label">Verified monthly run-rate</div>
      <div class="bstat-note" style="font-size:10px;color:var(--muted)">verified fixed amounts only</div>
    </div>
    <div class="bstat">
      <div class="bstat-val" style="color:#f87171">${atRisk}</div>
      <div class="bstat-label">Failing / past due</div>
    </div>
    <div class="bstat">
      <div class="bstat-val" style="color:var(--accent)">${s.unverified_amounts ?? 0}</div>
      <div class="bstat-label">Amounts unverified</div>
    </div>
    <div class="bstat">
      <div class="bstat-val" style="color:var(--ok)">${s.pending_proposals ?? 0}</div>
      <div class="bstat-label">Proposals to review</div>
    </div>`;
}

/* Statuses that mean money is actually at risk right now. */
const RISK_STATUSES = new Set([
  "FAILING", "PAST DUE", "DECLINED", "BILLING PROBLEM", "OUTSTANDING", "BALANCE DUE", "DUE",
]);
const CLOSED_STATUSES = new Set([
  "CANCELLED", "ENDED", "CLOSED", "REFUNDED / WINDING DOWN",
]);

function statusClass(status) {
  const s = (status || "").toUpperCase();
  if (RISK_STATUSES.has(s)) return "risk";
  if (CLOSED_STATUSES.has(s)) return "closed";
  if (s.startsWith("ACTIVE") || s === "PAID" || s === "AUTO-PAY SCHEDULED") return "ok";
  return "watch";
}

/* ── Toolbar ─────────────────────────────────────────────────────────────── */
function renderRegisterToolbar() {
  const tb = document.getElementById("reg-toolbar");
  if (!tb) return;
  tb.innerHTML = `
    <div class="bills-search">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
        <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
      </svg>
      <input type="text" id="reg-search" placeholder="Search vendor, entity, account…" value="${escAttr(regSearch)}"/>
    </div>
    <select class="bills-filter-sel" id="reg-filter">
      <option value="all">All lines</option>
      <option value="unverified">Amount unverified</option>
      <option value="proposed">Has a proposal</option>
      <option value="risk">Failing / past due</option>
      <option value="active">Active only</option>
    </select>
    <button class="bv-action-btn" id="reg-verify-btn" onclick="runVerification()"
            style="flex-shrink:0;white-space:nowrap;color:var(--accent);border-color:rgba(245,166,35,.3)">
      ${regBusy ? `<span class="scan-dots"><span></span><span></span><span></span></span> Reading documents…`
                : "🔍 Verify amounts from documents"}
    </button>
    <button class="bv-action-btn" onclick="refreshRegister()" style="flex-shrink:0">↻ Refresh</button>`;

  const sel = document.getElementById("reg-filter");
  if (sel) {
    sel.value = regFilter;
    sel.addEventListener("change", (e) => { regFilter = e.target.value; renderRegister(); });
  }
  document.getElementById("reg-search")?.addEventListener("input", (e) => {
    regSearch = e.target.value.toLowerCase();
    renderRegister();
  });
}

/* ── List ────────────────────────────────────────────────────────────────── */
function visibleRegisterLines() {
  let v = registerLines.slice();
  if (regFilter === "unverified") v = v.filter((l) => l.amount_unverified);
  else if (regFilter === "proposed") v = v.filter((l) => l.proposal);
  else if (regFilter === "risk") v = v.filter((l) => RISK_STATUSES.has((l.status || "").toUpperCase()));
  else if (regFilter === "active") v = v.filter((l) => (l.status || "").toUpperCase().startsWith("ACTIVE"));
  if (regSearch) {
    v = v.filter((l) =>
      [l.vendor, l.what_it_is, l.entity, l.source_mailbox, l.card, l.status]
        .some((f) => (f || "").toLowerCase().includes(regSearch)));
  }
  return v;
}

function renderRegister() {
  const list = document.getElementById("reg-list");
  if (!list) return;
  renderRegisterToolbar();

  const rows = visibleRegisterLines();
  if (!rows.length) {
    list.innerHTML = `<div class="reg-empty"><div class="reg-empty-sub">No lines match.</div></div>`;
    return;
  }
  list.innerHTML = rows.map(registerRowHTML).join("");
}

function registerRowHTML(l) {
  const cls = statusClass(l.status);
  const amount = l.amount_unverified
    ? `<span class="reg-unverified" title="${escAttr(l.amount_usd)}">unverified</span>`
    : (l.amount_value !== null && l.amount_value !== undefined
        ? esc(fmt(l.amount_value, "USD")) : esc(l.amount_usd || "—"));

  // A non-USD figure the register tracked separately — worth showing, because
  // it is often the only amount anyone has for that line.
  const billed = (l.currency_as_billed && l.currency_as_billed !== "USD")
    ? `<span class="reg-billed">${esc(l.currency_as_billed)}</span>` : "";

  const docs = (l.documents || []).length;
  const docBadge = docs
    ? `<button class="reg-doc-btn" onclick="event.stopPropagation();openBillViewer('${escAttr(l.documents[0].bill_id)}')"
               title="Open the document Hermes holds for this line">📄 ${docs}</button>` : "";

  return `
    <div class="reg-row ${cls}" data-ref="${escAttr(l.ref)}">
      <div class="reg-ref">#${esc(l.ref)}</div>
      <div class="reg-main">
        <div class="reg-vendor">${esc(l.vendor)}</div>
        <div class="reg-what">${esc(l.what_it_is || "")}</div>
        <div class="reg-meta">
          <span class="reg-entity">${esc(l.entity || "—")}</span>
          <span class="reg-basis">${esc(l.billing_basis || "")}</span>
          ${l.card ? `<span class="reg-card">${esc(l.card)}</span>` : ""}
          ${l.last_evidence_date ? `<span class="reg-evi">evidence ${esc(l.last_evidence_date)}</span>` : ""}
        </div>
      </div>
      <div class="reg-right">
        <div class="reg-amount">${amount}${billed}</div>
        <div class="reg-status ${cls}">${esc(l.status || "—")}</div>
        ${docBadge}
      </div>
      ${l.proposal ? proposalHTML(l.proposal) : ""}
    </div>`;
}

function proposalHTML(p) {
  const amount = fmt(Number(p.proposed_amount), p.proposed_currency || "USD");
  // `extractor` carries the reasons the match was believed. Showing them is the
  // point: an approver should be able to judge the figure, not just accept it.
  const why = String(p.extractor || "");
  const items = Array.isArray(p.line_items) ? p.line_items : [];
  return `
    <div class="reg-proposal">
      <div class="reg-prop-head">
        <span class="reg-prop-tag">Proposed</span>
        <strong class="reg-prop-amount">${esc(amount)}</strong>
        ${p.invoice_number ? `<span class="reg-prop-inv">invoice ${esc(p.invoice_number)}</span>` : ""}
        <span class="reg-prop-conf">confidence ${esc(String(Math.round((p.match_score || 0) * 100)))}%</span>
      </div>
      <div class="reg-prop-why">${esc(why)}</div>
      ${items.length ? `<div class="reg-prop-items">${items.slice(0, 3).map((i) =>
          `<div>${esc(i.description || "")} — ${esc(String(i.total ?? ""))}</div>`).join("")}</div>` : ""}
      <div class="reg-prop-actions">
        ${p.evidence_bill_id
          ? `<button class="bv-action-btn" onclick="openBillViewer('${escAttr(p.evidence_bill_id)}')">📄 See the document</button>`
          : ""}
        <button class="bv-action-btn reg-approve" onclick="decideProposal('${escAttr(p.proposal_id)}', true)">✓ Approve &amp; write to Sheet</button>
        <button class="bv-action-btn reg-reject" onclick="decideProposal('${escAttr(p.proposal_id)}', false)">Reject</button>
      </div>
    </div>`;
}

/* ── Verification ────────────────────────────────────────────────────────── */
async function runVerification() {
  if (regBusy) return;
  regBusy = true;
  renderRegisterToolbar();
  try {
    const r = await fetch("/api/register/verify", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ limit: 15 }),
    });
    const j = await r.json();
    if (!r.ok) { showToast("Verification failed: " + (j.error || r.status)); return; }

    // Report the refusals too. "Nothing proposed" is a real, useful answer here
    // — it means no document could be tied to those lines, not that they are fine.
    const bits = [];
    if (j.proposed) bits.push(`${j.proposed} proposed`);
    if (j.needs_review) bits.push(`${j.needs_review} need a human`);
    if (j.no_document) bits.push(`${j.no_document} have no document`);
    showToast(bits.length ? `Checked ${j.checked}: ${bits.join(" · ")}` : `Checked ${j.checked}`);

    await loadRegister(true);
    renderRegisterStats();
    renderRegister();
  } catch (e) {
    console.error("runVerification:", e);
    showToast("Verification failed — check the console");
  } finally {
    regBusy = false;
    renderRegisterToolbar();
  }
}

async function decideProposal(proposalId, approve) {
  if (approve && !confirm("Write this amount into the Google Sheet and log the change?")) return;
  try {
    const r = await fetch(`/api/register/proposals/${proposalId}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ approve }),
    });
    const j = await r.json();
    if (!r.ok) { showToast((approve ? "Approve" : "Reject") + " failed: " + (j.error || r.status)); return; }
    showToast(approve ? "✓ Written to the Sheet and logged" : "Proposal rejected");
    await loadRegister(true);
    renderRegisterStats();
    renderRegister();
  } catch (e) {
    console.error("decideProposal:", e);
    showToast("Couldn't record that decision — check your connection");
  }
}

async function refreshRegister() {
  if (regLoading) return;
  const list = document.getElementById("reg-list");
  if (list && !registerLines.length) {
    list.innerHTML = `<div class="reg-empty"><div class="scan-dots"><span></span><span></span><span></span></div>
      <div class="reg-empty-sub" style="margin-top:10px">Reading the register…</div></div>`;
  }
  if (await loadRegister(true)) { renderRegisterStats(); renderRegister(); }
}

/* ── Entry point, called from bills.js initBilling() ─────────────────────── */
async function initRegister() {
  const ok = await loadRegister();
  const dc = document.getElementById("reg-count-docs");
  if (dc) dc.textContent = bills.length || "";
  if (ok) { renderRegisterStats(); renderRegister(); }
}
