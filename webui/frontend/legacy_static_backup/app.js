// Legacy WebUI interaction logic.

const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

// ──────────────────────── Utilities ────────────────────────

async function api(path, opts = {}) {
  const resp = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || resp.statusText);
  return data;
}

function fmtTime(ts) {
  if (!ts) return "-";
  return new Date(ts * 1000).toLocaleString("en-US", { hour12: false });
}

function logLine(text, kind = "") {
  const box = $("#logBox");
  const div = document.createElement("div");
  div.className = "line " + kind;
  div.textContent = text;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

function classifyLog(line) {
  const l = line.toLowerCase();
  if (l.includes("error") || l.includes("\u5931\u8d25") || l.includes("\u62d2\u7edd")) return "err";
  if (l.includes("warning") || l.includes("warn")) return "warn";
  if (l.includes("\u6210\u529f") || l.includes("\u5b8c\u6210") || l.includes("\u547d\u4e2d") || l.includes("ok")) return "ok";
  return "";
}

// ──────────────────────── Statistics bar ────────────────────────

async function refreshStats() {
  try {
    const { stats } = await api("/api/stats");
    const items = [
      { v: stats.total,     cls: "" },
      { v: stats.available, cls: "ok" },
      { v: stats.in_use,    cls: "warn" },
      { v: stats.done,      cls: "done" },
      { v: stats.failed,    cls: "bad" },
    ];
    $$("#statsBar .pill").forEach((el, i) => {
      el.querySelector("b").textContent = items[i].v;
    });
  } catch (e) {
    console.error("stats:", e);
  }
}

// ──────────────────────── Import ────────────────────────

$("#btnImport").addEventListener("click", async () => {
  const text = $("#importText").value.trim();
  if (!text) {
    $("#importResult").textContent = "Enter the accounts to import";
    return;
  }
  $("#btnImport").disabled = true;
  $("#importResult").textContent = "Importing...";
  try {
    const r = await api("/api/import", {
      method: "POST",
      body: JSON.stringify({ text }),
    });
    $("#importResult").textContent =
      `✅ Parsed ${r.parsed} lines: ${r.inserted} added, ${r.updated} updated, ${r.skipped} skipped`;
    $("#importResult").className = "result ok";
    $("#importText").value = "";
    refreshStats();
    refreshPool(true);
  } catch (e) {
    $("#importResult").textContent = "❌ " + e.message;
    $("#importResult").className = "result bad";
  } finally {
    $("#btnImport").disabled = false;
  }
});

// ──────────────────────── Start registration ────────────────────────

let currentEs = null;

$("#btnRun").addEventListener("click", async () => {
  const email = $("#regEmail").value.trim();
  const opts = {
    email: email || null,
    proxy: $("#regProxy").value.trim(),
    otp_timeout: parseInt($("#regOtpTimeout").value || "10", 10),
    want_access_token: true,
    want_session_token: true,
    want_refresh_token: true,
  };
  $("#btnRun").disabled = true;
  $("#runStatus").textContent = "Starting...";
  $("#runStatus").className = "result";
  $("#logBox").innerHTML = "";

  try {
    const r = await api("/api/register", {
      method: "POST",
      body: JSON.stringify(opts),
    });
    $("#runStatus").textContent = `🚀 Started run_id=${r.run_id} email=${r.email}`;
    logLine(`[client] Registration started run_id=${r.run_id} email=${r.email}`, "evt");
    streamRun(r.run_id);
  } catch (e) {
    $("#runStatus").textContent = "❌ " + e.message;
    $("#runStatus").className = "result bad";
    $("#btnRun").disabled = false;
  }
});

function streamRun(runId) {
  if (currentEs) { try { currentEs.close(); } catch (_) {} }
  const es = new EventSource(`/api/runs/${runId}/stream`);
  currentEs = es;

  es.addEventListener("log", (e) => {
    try {
      const d = JSON.parse(e.data);
      if (!d.line) return;
      logLine(d.line, classifyLog(d.line));
    } catch (_) {}
  });

  es.addEventListener("status", (e) => {
    try {
      const d = JSON.parse(e.data);
      if (d.kind === "done") {
        const s = `✅ Registration complete: access_token=${d.access_token_len}${d.partial ? "  (partial credentials)" : ""}`;
        const buttons = [];
        if (d.access_token_len > 0)  buttons.push(`<button class="quick-copy" data-email="${d.email}" data-field="access_token">📋 Copy access_token</button>`);
        $("#runStatus").innerHTML = `<span class="ok">${s}</span>${buttons.length ? "<br>" + buttons.join(" ") : ""}`;
        logLine("[client] " + s, "evt");
      } else if (d.kind === "error") {
        $("#runStatus").textContent = "❌ " + d.message;
        $("#runStatus").className = "result bad";
        logLine("[client] ❌ " + d.message, "err");
      } else if (d.kind === "phase") {
        logLine(`[client] phase=${d.phase} email=${d.email}`, "evt");
      }
    } catch (_) {}
  });

  es.addEventListener("end", () => {
    try { es.close(); } catch (_) {}
    currentEs = null;
    $("#btnRun").disabled = false;
    refreshStats();
    refreshPool();
    refreshRegistered();
    refreshRuns();
  });

  es.onerror = () => {
    try { es.close(); } catch (_) {}
    currentEs = null;
    $("#btnRun").disabled = false;
  };
}

// Show quick-copy buttons in the status bar after registration without requiring a tab change.
$("#runStatus").addEventListener("click", async (e) => {
  const copyBtn = e.target.closest("button.quick-copy");
  if (copyBtn) {
    const email = copyBtn.dataset.email;
    const field = copyBtn.dataset.field;
    try {
      const cred = await _loadCred(email);
      const val = cred[field] || "";
      if (!val) { alert(`${field} is empty`); return; }
      await _copyText(val, copyBtn);
    } catch (err) { alert("Failed to load credentials: " + err.message); }
  }
});

// ──────────────────────── Tabs ────────────────────────

$$(".tab").forEach((t) => {
  t.addEventListener("click", () => {
    $$(".tab").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    $$(".tab-content").forEach((c) => c.classList.add("hidden"));
    $("#tab-" + t.dataset.tab).classList.remove("hidden");
    if (t.dataset.tab === "registered") refreshRegistered();
    if (t.dataset.tab === "runs") refreshRuns();
    if (t.dataset.tab === "mailcfg") loadMailConfig();
    if (t.dataset.tab === "smscfg") loadSmsConfig();
    if (t.dataset.tab === "exportcfg") loadExportConfig();
  });
});

// ──────────────────────── Account-pool list ────────────────────────

const POOL_PAGE_SIZE = 20;
let _poolPage = 1;
let _poolTotal = 0;

function _poolTotalPages() { return Math.max(1, Math.ceil(_poolTotal / POOL_PAGE_SIZE)); }

function _updatePoolPagination() {
  const pages = _poolTotalPages();
  $("#poolPageInfo").textContent = `Page ${_poolPage} of ${pages} (${_poolTotal} items)`;
  $("#poolPrevPage").disabled = _poolPage <= 1;
  $("#poolNextPage").disabled = _poolPage >= pages;
}

async function refreshPool(resetPage) {
  if (resetPage === true) _poolPage = 1;
  const status = $("#poolFilter").value;
  const offset = (_poolPage - 1) * POOL_PAGE_SIZE;
  const { items, total } = await api(`/api/accounts?status=${encodeURIComponent(status)}&limit=${POOL_PAGE_SIZE}&offset=${offset}`);
  _poolTotal = total;
  if (_poolPage > _poolTotalPages()) _poolPage = _poolTotalPages();
  const tb = $("#poolTable tbody");
  tb.innerHTML = "";
  for (const r of items) {
    const tr = document.createElement("tr");
    const canReset = (r.status === "done" || r.status === "failed");
    tr.innerHTML = `
      <td><input type="checkbox" class="pool-check" data-email="${r.email}"></td>
      <td>${r.email}</td>
      <td><span class="status ${r.status}">${r.status}</span></td>
      <td title="${r.fail_reason || ''}">${(r.fail_reason || '').slice(0, 50)}</td>
      <td>
        <button data-act="use" data-email="${r.email}">Use</button>
        ${canReset ? `<button data-act="reset" data-email="${r.email}" title="Reset to available for another registration">🔄 Reset</button>` : ""}
        <button data-act="del" data-email="${r.email}">Delete</button>
      </td>
    `;
    tb.appendChild(tr);
  }
  $("#poolSelectAll").checked = false;
  _updateSelCount();
  _updatePoolPagination();
}
$("#btnRefreshPool").addEventListener("click", () => refreshPool(false));
$("#poolPrevPage").addEventListener("click", () => { if (_poolPage > 1) { _poolPage--; refreshPool(); } });
$("#poolNextPage").addEventListener("click", () => { if (_poolPage < _poolTotalPages()) { _poolPage++; refreshPool(); } });
$("#poolFilter").addEventListener("change", () => refreshPool(true));

$("#btnResetFailed").addEventListener("click", async () => {
  if (!confirm("Reset all failed accounts to available?")) return;
  $("#poolActionResult").textContent = "Processing...";
  $("#poolActionResult").className = "result";
  try {
    const r = await api("/api/accounts/reset_failed", { method: "POST" });
    $("#poolActionResult").textContent = `✅ Reset ${r.reset} accounts to available`;
    $("#poolActionResult").className = "result ok";
    refreshPool(); refreshStats();
  } catch (e) {
    $("#poolActionResult").textContent = "❌ " + e.message;
    $("#poolActionResult").className = "result bad";
  }
});

$("#btnReleaseStale").addEventListener("click", async () => {
  $("#poolActionResult").textContent = "Processing...";
  $("#poolActionResult").className = "result";
  try {
    const r = await api("/api/accounts/release_stale", { method: "POST" });
    $("#poolActionResult").textContent = `✅ Released ${r.released} stalled accounts`;
    $("#poolActionResult").className = "result ok";
    refreshPool(); refreshStats();
  } catch (e) {
    $("#poolActionResult").textContent = "❌ " + e.message;
    $("#poolActionResult").className = "result bad";
  }
});

// ── Account pool: checkbox selection and bulk deletion ──

function _selectedEmails() {
  return Array.from(document.querySelectorAll(".pool-check:checked"))
    .map(c => c.dataset.email);
}
function _updateSelCount() {
  const n = _selectedEmails().length;
  $("#selCount").textContent = n;
  $("#selCount2").textContent = n;
  $("#btnDeleteSelected").disabled = n === 0;
  $("#btnResetSelected").disabled = n === 0;
}
$("#poolTable").addEventListener("change", (e) => {
  if (e.target.classList.contains("pool-check")) _updateSelCount();
});
$("#poolSelectAll").addEventListener("change", (e) => {
  document.querySelectorAll(".pool-check").forEach(c => c.checked = e.target.checked);
  _updateSelCount();
});

$("#btnResetSelected").addEventListener("click", async () => {
  const emails = _selectedEmails();
  if (!emails.length) return;
  if (!confirm(`Reset ${emails.length} selected accounts to available?\nSaved credentials will not change.`)) return;
  $("#poolActionResult").textContent = "Resetting...";
  $("#poolActionResult").className = "result";
  try {
    const r = await api("/api/accounts/bulk_reset", {
      method: "POST",
      body: JSON.stringify({ emails }),
    });
    $("#poolActionResult").textContent = `✅ Reset ${r.reset} accounts`;
    $("#poolActionResult").className = "result ok";
    refreshPool(); refreshStats();
  } catch (e) {
    $("#poolActionResult").textContent = "❌ " + e.message;
    $("#poolActionResult").className = "result bad";
  }
});

$("#btnDeleteSelected").addEventListener("click", async () => {
  const emails = _selectedEmails();
  if (!emails.length) return;
  if (!confirm(`Delete ${emails.length} selected accounts? This cannot be undone.`)) return;
  $("#poolActionResult").textContent = "Deleting...";
  $("#poolActionResult").className = "result";
  try {
    const r = await api("/api/accounts/bulk_delete", {
      method: "POST",
      body: JSON.stringify({ emails }),
    });
    $("#poolActionResult").textContent = `✅ Deleted ${r.deleted} accounts`;
    $("#poolActionResult").className = "result ok";
    refreshPool(); refreshStats();
  } catch (e) {
    $("#poolActionResult").textContent = "❌ " + e.message;
    $("#poolActionResult").className = "result bad";
  }
});

$("#btnBulkDelStatus").addEventListener("click", async () => {
  const status = $("#bulkDelStatus").value;
  if (!status) {
    $("#poolActionResult").textContent = "Select a status to delete first";
    $("#poolActionResult").className = "result bad";
    return;
  }
  const tip = status === "all"
    ? "⚠️ Delete every account in the pool, including unregistered accounts?"
    : `Delete all accounts with status ${status}?`;
  if (!confirm(tip)) return;
  $("#poolActionResult").textContent = "Deleting...";
  $("#poolActionResult").className = "result";
  try {
    const r = await api("/api/accounts/bulk_delete", {
      method: "POST",
      body: JSON.stringify({ status }),
    });
    $("#poolActionResult").textContent = `✅ Deleted ${r.deleted} ${status} accounts`;
    $("#poolActionResult").className = "result ok";
    $("#bulkDelStatus").value = "";
    refreshPool(); refreshStats();
  } catch (e) {
    $("#poolActionResult").textContent = "❌ " + e.message;
    $("#poolActionResult").className = "result bad";
  }
});

$("#poolTable").addEventListener("click", async (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  const email = btn.dataset.email;
  if (btn.dataset.act === "use") {
    $("#regEmail").value = email;
    window.scrollTo({ top: 0, behavior: "smooth" });
  } else if (btn.dataset.act === "reset") {
    if (!confirm(`Reset ${email} to available?\nSaved credentials will not change.`)) return;
    try {
      await api(`/api/accounts/reset/${encodeURIComponent(email)}`, { method: "POST" });
      refreshPool();
      refreshStats();
    } catch (err) {
      alert("Reset failed: " + err.message);
    }
  } else if (btn.dataset.act === "del") {
    if (!confirm(`Delete ${email}?`)) return;
    await api(`/api/accounts/${encodeURIComponent(email)}`, { method: "DELETE" });
    refreshPool();
    refreshStats();
  }
});

// ──────────────────────── Paginated registration results ────────────────────────

const _plusCache = {};

const REG_PAGE_SIZE = 20;
let _regPage = 1;
let _regTotal = 0;

function _regTotalPages() { return Math.max(1, Math.ceil(_regTotal / REG_PAGE_SIZE)); }

function _updateRegPagination() {
  const pages = _regTotalPages();
  $("#regPageInfo").textContent = `Page ${_regPage} of ${pages} (${_regTotal} items)`;
  $("#regPrevPage").disabled = _regPage <= 1;
  $("#regNextPage").disabled = _regPage >= pages;
}

async function refreshRegistered(resetPage) {
  if (resetPage === true) _regPage = 1;
  const filter = $("#regFilter").value || "all";
  const offset = (_regPage - 1) * REG_PAGE_SIZE;
  const { items, total } = await api(`/api/registered?limit=${REG_PAGE_SIZE}&offset=${offset}&filter=${filter}`);
  _regTotal = total;
  if (_regPage > _regTotalPages()) _regPage = _regTotalPages();

  const tb = $("#regTable tbody");
  tb.innerHTML = "";
  for (const r of items) {
    const tr = document.createElement("tr");
    const cached = _plusCache[r.email] || r.plus_check;
    const plusCell = cached
      ? `<span class="plus-status plus-${cached.status}">${cached.label}</span>`
      : `<span class="plus-status plus-unknown">—</span>`;
    tr.innerHTML = `
      <td><input type="checkbox" class="reg-check" data-email="${r.email}"></td>
      <td>${r.email}</td>
      <td data-plus-email="${r.email}">${plusCell}</td>
      <td>${r.at_len > 0 ? `<button class="copy-cell" data-email="${r.email}" data-field="access_token" title="Click to copy access_token">✅ ${r.at_len} 📋</button>` : "—"}</td>
      <td>${r.st_len > 0 ? `<button class="copy-cell" data-email="${r.email}" data-field="session_token" title="Click to copy session_token">✅ ${r.st_len} 📋</button>` : "—"}</td>
      <td>${r.rt_len > 0 ? `<button class="copy-cell" data-email="${r.email}" data-field="refresh_token" title="Click to copy refresh_token">✅ ${r.rt_len} 📋</button>` : "—"}</td>
      <td>${fmtTime(r.created_at)}</td>
      <td>
        <button data-act="view" data-email="${r.email}">View Credentials</button>
        <button data-act="del" data-email="${r.email}">Delete</button>
      </td>
    `;
    tb.appendChild(tr);
  }
  $("#regSelectAll").checked = false;
  _updateSelCountReg();
  _updateRegPagination();
}

$("#btnRefreshReg").addEventListener("click", () => refreshRegistered(false));
$("#regPrevPage").addEventListener("click", () => { if (_regPage > 1) { _regPage--; refreshRegistered(); } });
$("#regNextPage").addEventListener("click", () => { if (_regPage < _regTotalPages()) { _regPage++; refreshRegistered(); } });

// Return to the first page when the filter changes.
$("#regFilter").addEventListener("change", () => refreshRegistered(true));

// ── Plus trial eligibility checks ──

function _collectPlusEmails(uncheckedOnly) {
  const tds = Array.from(document.querySelectorAll("[data-plus-email]"));
  if (!uncheckedOnly) return tds.map(td => td.dataset.plusEmail);
  return tds
    .filter(td => {
      const span = td.querySelector(".plus-status");
      return !span || span.classList.contains("plus-unknown");
    })
    .map(td => td.dataset.plusEmail);
}

async function _doCheckPlus(uncheckedOnly) {
  const emails = _collectPlusEmails(uncheckedOnly);
  if (!emails.length) {
    $("#checkPlusResult").textContent = uncheckedOnly ? "No unchecked accounts on this page" : "No accounts on this page";
    $("#checkPlusResult").className = "result";
    return;
  }
  const proxy = $("#regProxy").value.trim();
  $("#btnCheckPlus").disabled = true;
  $("#btnRecheckPlus").disabled = true;
  $("#checkPlusResult").textContent = `Checking ${emails.length} accounts...`;
  try {
    const { results } = await api("/api/registered/check_plus", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ emails, proxy }),
    });
    let plusCount = 0, freeCount = 0, bannedCount = 0;
    for (const [email, info] of Object.entries(results)) {
      _plusCache[email] = info;
      const td = document.querySelector(`[data-plus-email="${email}"]`);
      if (td) td.innerHTML = `<span class="plus-status plus-${info.status}">${info.label}</span>`;
      if (info.status === "plus_eligible" || info.status === "plus_active") plusCount++;
      else if (info.status === "banned") bannedCount++;
      else if (info.status === "free") freeCount++;
    }
    $("#checkPlusResult").textContent = `Complete: ${plusCount} Plus eligible, ${freeCount} Free, ${bannedCount} banned`;
    $("#checkPlusResult").className = "result ok";
  } catch (e) {
    $("#checkPlusResult").textContent = "Check failed: " + e.message;
    $("#checkPlusResult").className = "result bad";
  } finally {
    $("#btnCheckPlus").disabled = false;
    $("#btnRecheckPlus").disabled = false;
  }
}

$("#btnCheckPlus").addEventListener("click", () => _doCheckPlus(true));
$("#btnRecheckPlus").addEventListener("click", () => _doCheckPlus(false));
$("#btnCheckSelected").addEventListener("click", () => _doCheckPlusSelected());

// ── Check selected accounts ──
async function _doCheckPlusSelected() {
  const emails = _selectedRegEmails();
  if (!emails.length) return;
  const proxy = $("#regProxy").value.trim();
  $("#btnCheckSelected").disabled = true;
  $("#btnCheckPlus").disabled = true;
  $("#btnRecheckPlus").disabled = true;
  $("#checkPlusResult").textContent = `Checking ${emails.length} accounts...`;
  try {
    const { results } = await api("/api/registered/check_plus", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ emails, proxy }),
    });
    let plusCount = 0, freeCount = 0, bannedCount = 0;
    for (const [email, info] of Object.entries(results)) {
      _plusCache[email] = info;
      const td = document.querySelector(`[data-plus-email="${email}"]`);
      if (td) td.innerHTML = `<span class="plus-status plus-${info.status}">${info.label}</span>`;
      if (info.status === "plus_eligible" || info.status === "plus_active") plusCount++;
      else if (info.status === "banned") bannedCount++;
      else if (info.status === "free") freeCount++;
    }
    $("#checkPlusResult").textContent = `Complete: ${plusCount} Plus eligible, ${freeCount} Free, ${bannedCount} banned`;
    $("#checkPlusResult").className = "result ok";
  } catch (e) {
    $("#checkPlusResult").textContent = "Check failed: " + e.message;
    $("#checkPlusResult").className = "result bad";
  } finally {
    $("#btnCheckSelected").disabled = false;
    $("#btnCheckPlus").disabled = false;
    $("#btnRecheckPlus").disabled = false;
  }
}

// ── Registration results: checkbox, bulk, and single-row deletion ──

function _selectedRegEmails() {
  return Array.from(document.querySelectorAll(".reg-check:checked")).map(c => c.dataset.email);
}
function _updateSelCountReg() {
  const n = _selectedRegEmails().length;
  $("#selCountReg").textContent = n;
  $("#selCountCheck").textContent = n;
  $("#btnDeleteSelectedReg").disabled = n === 0;
  $("#btnCheckSelected").disabled = n === 0;
}
$("#regTable").addEventListener("change", (e) => {
  if (e.target.classList.contains("reg-check")) _updateSelCountReg();
});
$("#regSelectAll").addEventListener("change", (e) => {
  document.querySelectorAll(".reg-check").forEach(c => c.checked = e.target.checked);
  _updateSelCountReg();
});

$("#btnDeleteSelectedReg").addEventListener("click", async () => {
  const emails = _selectedRegEmails();
  if (!emails.length) return;
  if (!confirm(`Delete ${emails.length} selected credentials? This cannot be undone.`)) return;
  $("#exportResult").textContent = "Deleting...";
  $("#exportResult").className = "result";
  try {
    const r = await api("/api/registered/bulk_delete", {
      method: "POST",
      body: JSON.stringify({ emails }),
    });
    $("#exportResult").textContent = `✅ Deleted ${r.deleted} credentials`;
    $("#exportResult").className = "result ok";
    refreshRegistered();
  } catch (e) {
    $("#exportResult").textContent = "❌ " + e.message;
    $("#exportResult").className = "result bad";
  }
});

$("#btnDeleteAllReg").addEventListener("click", async () => {
  if (!confirm("⚠️ This deletes every credential in the registration results table.\nContinue? The account pool is not affected.")) return;
  if (!confirm("Confirm again: delete every credential? This action cannot be undone.")) return;
  $("#exportResult").textContent = "Clearing...";
  $("#exportResult").className = "result";
  try {
    const r = await api("/api/registered/bulk_delete", {
      method: "POST",
      body: JSON.stringify({ all: true }),
    });
    $("#exportResult").textContent = `✅ Cleared ${r.deleted} credentials`;
    $("#exportResult").className = "result ok";
    refreshRegistered();
  } catch (e) {
    $("#exportResult").textContent = "❌ " + e.message;
    $("#exportResult").className = "result bad";
  }
});

// Cache the most recently viewed credentials for Copy All JSON and per-field copying.
let _credCache = null;

async function _loadCred(email) {
  if (_credCache && _credCache.email === email) return _credCache;
  const { data } = await api(`/api/registered/${encodeURIComponent(email)}`);
  _credCache = data;
  return data;
}

async function _copyText(text, btn) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(text);
    } else {
      const ta = document.createElement("textarea");
      ta.value = text;
      ta.style.cssText = "position:fixed;left:-9999px";
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      document.body.removeChild(ta);
    }
    if (btn) {
      const orig = btn.textContent;
      const cls = btn.className;
      btn.textContent = "✅ Copied";
      btn.className = cls + " copied";
      setTimeout(() => { btn.textContent = orig; btn.className = cls; }, 1200);
    }
  } catch (e) {
    alert("Copy failed: " + e.message);
  }
}

$("#regTable").addEventListener("click", async (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  const email = btn.dataset.email;
  if (!email) return;

  // Inline copy for access, session, and refresh-token columns.
  if (btn.classList.contains("copy-cell")) {
    const field = btn.dataset.field;
    try {
      const cred = await _loadCred(email);
      const val = cred[field] || "";
      if (!val) { alert(`${field} is empty`); return; }
      await _copyText(val, btn);
    } catch (err) { alert("Failed to load credentials: " + err.message); }
    return;
  }

  // View Credentials opens the modal.
  if (btn.dataset.act === "view") {
    try {
      const cred = await _loadCred(email);
      _renderCredModal(email, cred);
    } catch (err) { alert("Failed to load credentials: " + err.message); }
  }

  // Delete one credential row.
  if (btn.dataset.act === "del") {
    if (!confirm(`Delete credentials for ${email}?`)) return;
    try {
      await api(`/api/registered/${encodeURIComponent(email)}`, { method: "DELETE" });
      refreshRegistered();
    } catch (err) { alert("Delete failed: " + err.message); }
  }
});


function _renderCredModal(email, cred) {
  $("#credTitle").textContent = email;
  const box = $("#credFields");
  box.innerHTML = "";

  // Display primary credentials in order, with a copy button for each field.
  const KEYS = [
    ["access_token",  "access_token"],
    ["session_token", "session_token"],
    ["refresh_token", "refresh_token"],
    ["id_token",      "id_token"],
    ["device_id",     "device_id"],
    ["csrf_token",    "csrf_token"],
    ["cookie_header", "cookie_header"],
    ["password",      "password"],
  ];
  for (const [key, label] of KEYS) {
    const val = cred[key] || "";
    if (!val) continue;
    const row = document.createElement("div");
    row.className = "cred-row";
    row.innerHTML = `
      <div class="cred-row-head">
        <span class="cred-label">${label}</span>
        <span class="cred-meta">len=${val.length}</span>
        <button class="cred-copy" data-val-key="${key}">📋 Copy</button>
      </div>
      <pre class="cred-val">${escapeHtml(val)}</pre>
    `;
    box.appendChild(row);
  }

  // Extra metadata, including synchronized cookies.
  if (cred.extra && Object.keys(cred.extra).length > 0) {
    const row = document.createElement("div");
    row.className = "cred-row";
    row.innerHTML = `
      <div class="cred-row-head">
        <span class="cred-label">extra</span>
        <span class="cred-meta">${Object.keys(cred.extra).length} keys</span>
        <button class="cred-copy" data-val-key="__extra__">📋 Copy JSON</button>
      </div>
      <pre class="cred-val">${escapeHtml(JSON.stringify(cred.extra, null, 2))}</pre>
    `;
    box.appendChild(row);
  }

  $("#credModal").classList.remove("hidden");
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

// Copy a single field from the modal.
$("#credFields").addEventListener("click", async (e) => {
  const btn = e.target.closest("button.cred-copy");
  if (!btn) return;
  const key = btn.dataset.valKey;
  const val = key === "__extra__"
    ? JSON.stringify(_credCache.extra, null, 2)
    : (_credCache[key] || "");
  await _copyText(val, btn);
});

$("#credClose").addEventListener("click", () => {
  $("#credModal").classList.add("hidden");
});
$("#credCopyJson").addEventListener("click", async (e) => {
  if (!_credCache) return;
  await _copyText(JSON.stringify(_credCache, null, 2), e.currentTarget);
});

// ──────────────────────── Registration runs ────────────────────────

async function refreshRuns() {
  const { items } = await api("/api/runs");
  const tb = $("#runTable tbody");
  tb.innerHTML = "";
  for (const r of items) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><code>${r.run_id}</code></td>
      <td>${r.email}</td>
      <td><span class="status ${r.status === 'done' ? 'done' : r.status === 'failed' ? 'failed' : 'running'}">${r.status}</span></td>
      <td>${fmtTime(r.started_at)}</td>
      <td title="${r.error || ''}">${(r.error || '').slice(0, 60)}</td>
    `;
    tb.appendChild(tr);
  }
}
$("#btnRefreshRuns").addEventListener("click", refreshRuns);

// ──────────────────────── 🤖 Automatic batch loop ────────────────────────

const AUTO_BTNS = {
  start:  $("#btnAutoStart"),
  pause:  $("#btnAutoPause"),
  resume: $("#btnAutoResume"),
  stop:   $("#btnAutoStop"),
};

function _autoOptions() {
  return {
    proxy: $("#regProxy").value.trim(),
    proxy_pool: $("#autoProxyPool").value,
    concurrency: parseInt($("#autoConcurrency").value || "1", 10),
    otp_timeout: parseInt($("#regOtpTimeout").value || "10", 10),
    want_access_token: true,
    want_session_token: true,
    want_refresh_token: true,
    cool_down_seconds: parseFloat($("#autoCoolDown").value || "3") || 0,
    target_count: parseInt($("#autoTargetCount").value || "0", 10) || 0,
  };
}

async function autoStart() {
  try {
    await api("/api/auto/start", { method: "POST", body: JSON.stringify(_autoOptions()) });
  } catch (e) { alert("Failed to start: " + e.message); }
}
async function autoCall(path) {
  try { await api(path, { method: "POST" }); }
  catch (e) { alert(`${path} failed: ${e.message}`); }
}
AUTO_BTNS.start.addEventListener("click", autoStart);
AUTO_BTNS.pause.addEventListener("click", () => autoCall("/api/auto/pause"));
AUTO_BTNS.resume.addEventListener("click", () => autoCall("/api/auto/resume"));
AUTO_BTNS.stop.addEventListener("click", () => autoCall("/api/auto/stop"));

function _renderAutoStatus(s) {
  const stateLabel = {
    "stopped": "⚪ Stopped",
    "running": "🟢 Running",
    "paused":  "⏸ Paused",
  }[s.state] || s.state;
  const elapsed = s.elapsed ? Math.round(s.elapsed) + "s" : "—";
  const workers = Array.isArray(s.workers) ? s.workers : [];
  const workerRows = workers.length
    ? workers.map(w => {
        const dur = w.started_at ? Math.round(Date.now() / 1000 - w.started_at) + "s" : "";
        const px = w.proxy ? ` [${escapeHtml(w.proxy.slice(0, 30))}${w.proxy.length > 30 ? "..." : ""}]` : "";
        return `<div class="auto-worker">worker-${w.id} ▶ <code>${escapeHtml(w.email)}</code> ${dur}${px}</div>`;
      }).join("")
    : "";
  const meta = `Concurrency=${s.concurrency || 1}`
    + (s.proxy_pool_size ? ` Proxy pool=${s.proxy_pool_size}` : "")
    + (s.target_count ? ` Target=${s.target_count}` : "");
  // Show progress toward a target and its remaining count, or just the success count when unlimited.
  const okDisplay = s.target_count
    ? `<b class="ok">${s.registered_ok}</b> / ${s.target_count}`
      + (s.remaining != null ? ` <span class="auto-meta">(${s.remaining} remaining)</span>` : "")
    : `<b class="ok">${s.registered_ok}</b> successful`;
  $("#autoStatus").innerHTML = `
    <b>${stateLabel}</b>
    &nbsp;|&nbsp; Completed: ${okDisplay} / <b class="bad">${s.registered_fail}</b> failed
    &nbsp;|&nbsp; Elapsed: ${elapsed}
    &nbsp;|&nbsp; <span class="auto-meta">${meta}</span>
    ${workerRows ? "<br>" + workerRows : ""}
    <br><span class="auto-msg">${escapeHtml(s.last_message || "")}</span>
  `;
  // Button availability.
  const st = s.state;
  AUTO_BTNS.start.disabled  = (st === "running" || st === "paused");
  AUTO_BTNS.pause.disabled  = (st !== "running");
  AUTO_BTNS.resume.disabled = (st !== "paused");
  AUTO_BTNS.stop.disabled   = (st === "stopped");
}

let _autoEs = null;
function _connectAutoStream() {
  if (_autoEs) { try { _autoEs.close(); } catch (_) {} }
  const es = new EventSource("/api/auto/stream");
  _autoEs = es;
  es.addEventListener("state", (e) => {
    try { _renderAutoStatus(JSON.parse(e.data)); } catch (_) {}
  });
  es.addEventListener("run_started", (e) => {
    try {
      const d = JSON.parse(e.data);
      logLine(`[auto] ▶ Registration started for ${d.email} (run=${d.run_id})`, "evt");
      // Reuse the single-run SSE for logs and status-bar copy buttons.
      streamRun(d.run_id);
    } catch (_) {}
  });
  es.addEventListener("run_finished", (e) => {
    try {
      const d = JSON.parse(e.data);
      const tag = d.ok ? "✅" : (d.category === "network" ? "🌐 Network error (account released)" : "❌");
      logLine(`[auto] ${tag} ${d.email} finished`, d.ok ? "ok" : "err");
    } catch (_) {}
  });
  es.addEventListener("circuit_break", (e) => {
    try {
      const d = JSON.parse(e.data);
      logLine(`[auto] ⚠️ Circuit breaker: ${d.reason}`, "err");
      _showBanner(d.reason);
    } catch (_) {}
  });
  es.onerror = () => {
    // Reconnect automatically.
    try { es.close(); } catch (_) {}
    _autoEs = null;
    setTimeout(_connectAutoStream, 2000);
  };
}

// Red alert banner at the top of the page.
function _showBanner(msg) {
  const b = $("#alertBanner");
  $("#alertBannerMsg").textContent = msg;
  b.classList.remove("hidden");
}
$("#alertBannerClose").addEventListener("click", () => {
  $("#alertBanner").classList.add("hidden");
});

// ──────────────────────── Form persistence with localStorage autosave and restore ────────────────────────

const FORM_KEY = "gpt_outlook_register_form_v1";

// Element ID to value type; text is the default, while checkboxes use .checked.
const PERSIST_FIELDS = {
  regProxy:        "text",
  regOtpTimeout:   "text",
  autoCoolDown:    "text",
  autoConcurrency: "text",
  autoProxyPool:   "text",
  autoTargetCount: "text",
};

function _saveForm() {
  const data = {};
  for (const [id, kind] of Object.entries(PERSIST_FIELDS)) {
    const el = document.getElementById(id);
    if (!el) continue;
    data[id] = kind === "check" ? !!el.checked : (el.value || "");
  }
  try { localStorage.setItem(FORM_KEY, JSON.stringify(data)); } catch (_) {}
}

function _loadForm() {
  let data = {};
  try { data = JSON.parse(localStorage.getItem(FORM_KEY) || "{}"); } catch (_) { data = {}; }
  for (const [id, kind] of Object.entries(PERSIST_FIELDS)) {
    if (!(id in data)) continue;
    const el = document.getElementById(id);
    if (!el) continue;
    if (kind === "check") el.checked = !!data[id];
    else el.value = data[id] || "";
  }
}

// Autosave on input and change events.
function _bindAutoSave() {
  for (const id of Object.keys(PERSIST_FIELDS)) {
    const el = document.getElementById(id);
    if (!el) continue;
    el.addEventListener("input", _saveForm);
    el.addEventListener("change", _saveForm);
  }
}

// ──────────────────────── 📧 Email configuration ────────────────────────

function _syncCfFields(source) {
  $("#cfTempCfg").classList.toggle("hidden", source !== "cf_temp");
}

async function loadMailConfig() {
  try {
    const { config } = await api("/api/settings/mail");
    const src = config.mail_source || "outlook";
    const radio = document.querySelector(`input[name="mailSource"][value="${src}"]`);
    if (radio) radio.checked = true;
    _syncCfFields(src);
    $("#cfApiUrl").value = config.cf_api_url || "";
    $("#cfDomain").value = config.cf_domain || "";
    $("#cfAdminToken").value = "";
    if (config.cf_admin_token === "***") {
      $("#cfAdminToken").placeholder = "Already configured (leave blank to keep it)";
    } else {
      $("#cfAdminToken").placeholder = "ADMIN_PASSWORDS configured on the Worker";
    }
  } catch (e) {
    console.error("loadMailConfig:", e);
  }
}

// Show or hide provider fields when the radio selection changes.
document.querySelectorAll("input[name='mailSource']").forEach(r => {
  r.addEventListener("change", () => _syncCfFields(r.value));
});

$("#btnSaveMailCfg").addEventListener("click", async () => {
  const source = document.querySelector("input[name='mailSource']:checked")?.value || "outlook";
  const isCf = source === "cf_temp";
  const body = {
    mail_source:    source,
    cf_api_url:     isCf ? $("#cfApiUrl").value.trim() : "",
    cf_admin_token: isCf ? ($("#cfAdminToken").value.trim() || "***") : "***",
    cf_domain:      isCf ? $("#cfDomain").value.trim() : "",
  };
  try {
    await api("/api/settings/mail", { method: "POST", body: JSON.stringify(body) });
    $("#mailCfgResult").textContent = "✅ Configuration saved";
    $("#mailCfgResult").className = "result ok";
  } catch (e) {
    $("#mailCfgResult").textContent = "❌ " + e.message;
    $("#mailCfgResult").className = "result bad";
  }
  setTimeout(() => { $("#mailCfgResult").textContent = ""; }, 3000);
});

$("#btnTestMail").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  btn.disabled = true;
  btn.textContent = "⏳ Testing...";
  $("#mailCfgResult").textContent = "";
  try {
    const r = await api("/api/settings/mail/test", { method: "POST" });
    $("#mailCfgResult").textContent = "✅ " + r.message;
    $("#mailCfgResult").className = "result ok";
  } catch (err) {
    $("#mailCfgResult").textContent = "❌ " + err.message;
    $("#mailCfgResult").className = "result bad";
  } finally {
    btn.disabled = false;
    btn.textContent = "🔌 Test CF Connection";
  }
});

// ──────────────────────── 📱 SMS verification configuration ────────────────────────

// Full country list loaded dynamically by provider, including OpenAI SMS compatibility.
let _smsAllCountries = [];
let _smsSafeCountrySet = new Set();
let _smsCountriesProvider = "";

async function _loadSmsAllCountries(provider) {
  provider = provider || "smsbower";
  if (_smsAllCountries.length && _smsCountriesProvider === provider) return _smsAllCountries;
  try {
    const r = await api(`/api/settings/sms/all_countries?provider=${encodeURIComponent(provider)}`);
    _smsAllCountries = r.countries || [];
    _smsSafeCountrySet = new Set(r.openai_sms_safe || []);
    _smsCountriesProvider = provider;
  } catch (e) {
    console.error("Failed to load country list:", e);
  }
  return _smsAllCountries;
}

function _renderSmsCountrySelect(selectEl, currentValue) {
  selectEl.innerHTML = "";
  for (const c of _smsAllCountries) {
    const opt = document.createElement("option");
    opt.value = c.id;
    opt.textContent = `${c.id} - ${c.name || c.name_en || `Country ${c.id}`}`;
    selectEl.appendChild(opt);
  }
  if (currentValue) selectEl.value = currentValue;
}

function _renderSmsAllowedCountriesBox(checkedIds) {
  const box = $("#smsAllowedCountriesBox");
  box.innerHTML = "";
  const checkedSet = new Set((checkedIds || "").split(",").map(s => s.trim()).filter(Boolean));

  // Render every country first and retain data attributes for search.
  for (const c of _smsAllCountries) {
    const lab = document.createElement("label");
    lab.className = "check country-item";
    lab.style.fontSize = "12px";
    lab.style.padding = "4px 6px";
    lab.style.lineHeight = "1.4";
    lab.dataset.countryId = c.id;
    const countryName = c.name || c.name_en || `Country ${c.id}`;
    lab.dataset.countryName = countryName.toLowerCase();

    // Display ID, country name, and optional price/stock.
    const priceInfo = c.price != null && c.count != null
      ? ` <span style="color:#999;font-size:11px">(${c.price}/${c.count})</span>`
      : "";
    lab.innerHTML = `<input type="checkbox" value="${c.id}" ${checkedSet.has(c.id) ? "checked" : ""}>${c.id}·${countryName}${priceInfo}`;
    box.appendChild(lab);
  }

  _updateAllowedCountryCount();
  box.querySelectorAll("input[type=checkbox]").forEach(cb => {
    cb.addEventListener("change", _updateAllowedCountryCount);
  });

  // Bind the search box once.
  const searchInput = $("#smsCountrySearch");
  if (searchInput && !searchInput.dataset.bound) {
    searchInput.dataset.bound = "1";
    searchInput.addEventListener("input", (e) => {
      const query = e.target.value.toLowerCase().trim();
      box.querySelectorAll(".country-item").forEach(lab => {
        const id = lab.dataset.countryId || "";
        const name = lab.dataset.countryName || "";
        const match = !query || id.includes(query) || name.includes(query);
        lab.style.display = match ? "" : "none";
      });
    });
  }
}

function _updateAllowedCountryCount() {
  const checked = $("#smsAllowedCountriesBox").querySelectorAll("input[type=checkbox]:checked");
  $("#smsAllowedCountryCount").textContent = `${checked.length} countries selected`;
}

function _getAllowedCountriesValue() {
  const checked = $("#smsAllowedCountriesBox").querySelectorAll("input[type=checkbox]:checked");
  return Array.from(checked).map(cb => cb.value).join(",");
}

async function loadSmsConfig() {
  try {
    const { config } = await api("/api/settings/sms");
    const provider = config.sms_provider || "smsbower";
    await _loadSmsAllCountries(provider);
    $("#smsEnabled").checked = config.sms_enabled === "1";
    const radio = document.querySelector(`input[name="smsProvider"][value="${provider}"]`);
    if (radio) radio.checked = true;
    $("#smsApiKey").value = "";
    $("#smsApiKey").placeholder = (config.sms_api_key === "***")
      ? "Already configured (leave blank to keep it)"
      : "Paste the SMS provider API key";
    _renderSmsCountrySelect($("#smsCountry"), config.sms_country || "150");
    $("#smsService").value = config.sms_service || "dr";
    $("#smsMaxPrice").value = config.sms_max_price || "";
    $("#smsPhoneSuccessMax").value = config.sms_phone_success_max || "3";
    $("#smsReusePhone").checked = config.sms_reuse_phone === "1";
    $("#smsAutoCountry").checked = config.sms_auto_country === "1";
    $("#smsAutoMinStock").value = config.sms_auto_min_stock || "20";
    $("#smsAutoMaxPrice").value = config.sms_auto_max_price || "";
    _renderSmsAllowedCountriesBox(config.sms_allowed_countries || "");
    $("#smsMaxPhoneAttempts").value = config.sms_max_phone_attempts || "";
    $("#smsPerPhoneTimeout").value = config.sms_per_phone_timeout || "80";
  } catch (e) {
    console.error("loadSmsConfig:", e);
  }
}

// Reload countries when the SMS provider changes.
document.querySelectorAll("input[name='smsProvider']").forEach(radio => {
  radio.addEventListener("change", async (e) => {
    const newProvider = e.target.value;
    _smsAllCountries = [];
    _smsCountriesProvider = "";
    const box = $("#smsAllowedCountriesBox");
    if (box) box.innerHTML = '<em style="grid-column:1/-1;color:#aaa;font-size:12px">Loading...</em>';
    await _loadSmsAllCountries(newProvider);
    _renderSmsCountrySelect($("#smsCountry"), $("#smsCountry").value);
    _renderSmsAllowedCountriesBox(_getAllowedCountriesValue());
  });
});

$("#btnClearAllowedCountries")?.addEventListener("click", () => {
  $("#smsAllowedCountriesBox").querySelectorAll("input[type=checkbox]").forEach(cb => {
    cb.checked = false;
  });
  _updateAllowedCountryCount();
});

$("#btnSaveSmsCfg").addEventListener("click", async () => {
  const apiKeyInput = $("#smsApiKey").value.trim();
  const body = {
    sms_enabled:           $("#smsEnabled").checked ? "1" : "0",
    sms_provider:          document.querySelector("input[name='smsProvider']:checked")?.value || "smsbower",
    sms_api_key:           apiKeyInput || "***",
    sms_country:           $("#smsCountry").value.trim() || "52",
    sms_service:           $("#smsService").value.trim() || "dr",
    sms_max_price:         $("#smsMaxPrice").value.trim(),
    sms_phone_success_max: $("#smsPhoneSuccessMax").value.trim() || "3",
    sms_reuse_phone:       $("#smsReusePhone").checked ? "1" : "0",
    sms_auto_country:      $("#smsAutoCountry").checked ? "1" : "0",
    sms_allowed_countries: _getAllowedCountriesValue(),
    sms_auto_min_stock:    $("#smsAutoMinStock").value.trim() || "20",
    sms_auto_max_price:    $("#smsAutoMaxPrice").value.trim(),
    sms_max_phone_attempts: $("#smsMaxPhoneAttempts").value.trim(),
    sms_per_phone_timeout: $("#smsPerPhoneTimeout").value.trim() || "80",
  };
  try {
    await api("/api/settings/sms", { method: "POST", body: JSON.stringify(body) });
    $("#smsCfgResult").textContent = "✅ Configuration saved";
    $("#smsCfgResult").className = "result ok";
    setTimeout(loadSmsConfig, 300);
  } catch (e) {
    $("#smsCfgResult").textContent = "❌ " + e.message;
    $("#smsCfgResult").className = "result bad";
  }
  setTimeout(() => { $("#smsCfgResult").textContent = ""; }, 3500);
});

$("#btnTestSms").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  btn.disabled = true;
  btn.textContent = "⏳ Testing...";
  $("#smsCfgResult").textContent = "";
  try {
    const r = await api("/api/settings/sms/test", { method: "POST" });
    $("#smsCfgResult").textContent = "✅ " + r.message;
    $("#smsCfgResult").className = "result ok";
  } catch (err) {
    $("#smsCfgResult").textContent = "❌ " + err.message;
    $("#smsCfgResult").className = "result bad";
  } finally {
    btn.disabled = false;
    btn.textContent = "🔌 Test Balance";
  }
});

// ──────────────────────── 📤 Automatic export configuration (CPA / SUB2API) ────────────────────────

async function loadExportConfig() {
  try {
    const { config } = await api("/api/settings/export");
    // CPA
    $("#cpaEnabled").checked = config.cpa_enabled === "1";
    $("#cpaUrl").value = config.cpa_url || "";
    $("#cpaMgmtKey").value = "";
    $("#cpaMgmtKey").placeholder = config.cpa_mgmt_key === "***"
      ? "Already configured (leave blank to keep it)"
      : "Paste the CPA management key";
    $("#cpaTimeout").value = config.cpa_timeout || "30";
    // SUB2API
    $("#sub2apiEnabled").checked = config.sub2api_enabled === "1";
    $("#sub2apiUrl").value = config.sub2api_url || "";
    $("#sub2apiApiKey").value = "";
    $("#sub2apiApiKey").placeholder = config.sub2api_api_key === "***"
      ? "Already configured (leave blank to keep it)"
      : "Paste the x-api-key generated by the dashboard";
    $("#sub2apiGroupIds").value = config.sub2api_group_ids || "2";
    $("#sub2apiTimeout").value = config.sub2api_timeout || "30";
  } catch (e) {
    console.error("loadExportConfig:", e);
  }
}

$("#btnSaveExportCfg").addEventListener("click", async () => {
  const cpaKeyInput = $("#cpaMgmtKey").value.trim();
  const sub2apiKeyInput = $("#sub2apiApiKey").value.trim();
  const body = {
    // CPA
    cpa_enabled:  $("#cpaEnabled").checked ? "1" : "0",
    cpa_url:      $("#cpaUrl").value.trim(),
    cpa_mgmt_key: cpaKeyInput || "***",
    cpa_timeout:  $("#cpaTimeout").value.trim() || "30",
    // SUB2API
    sub2api_enabled:    $("#sub2apiEnabled").checked ? "1" : "0",
    sub2api_url:        $("#sub2apiUrl").value.trim(),
    sub2api_api_key:    sub2apiKeyInput || "***",
    sub2api_group_ids:  $("#sub2apiGroupIds").value.trim() || "2",
    sub2api_timeout:    $("#sub2apiTimeout").value.trim() || "30",
  };
  try {
    await api("/api/settings/export", { method: "POST", body: JSON.stringify(body) });
    $("#exportCfgResult").textContent = "✅ Configuration saved";
    $("#exportCfgResult").className = "result ok";
    setTimeout(loadExportConfig, 300);
  } catch (e) {
    $("#exportCfgResult").textContent = "❌ " + e.message;
    $("#exportCfgResult").className = "result bad";
  }
  setTimeout(() => { $("#exportCfgResult").textContent = ""; }, 3500);
});

async function _testExportTarget(target, btn, resultEl, origText) {
  btn.disabled = true;
  btn.textContent = "⏳ Testing...";
  resultEl.textContent = "";
  try {
    const r = await api("/api/settings/export/test", {
      method: "POST",
      body: JSON.stringify({ target }),
    });
    resultEl.textContent = "✅ " + (r.message || "Connection successful");
    resultEl.className = "result ok";
  } catch (e) {
    resultEl.textContent = "❌ " + e.message;
    resultEl.className = "result bad";
  } finally {
    btn.disabled = false;
    btn.textContent = origText;
  }
}

$("#btnTestCpa").addEventListener("click", (e) => {
  _testExportTarget("cpa", e.currentTarget, $("#cpaTestResult"), "🔌 Test CPA Connection");
});
$("#btnTestSub2api").addEventListener("click", (e) => {
  _testExportTarget("sub2api", e.currentTarget, $("#sub2apiTestResult"), "🔌 Test SUB2API Connection");
});

// ──────────────────────── Startup ────────────────────────

_loadForm();
_bindAutoSave();
refreshStats();
refreshPool();
_connectAutoStream();
setInterval(refreshStats, 5000);
