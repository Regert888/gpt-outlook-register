// 团子喵的 WebUI 交互逻辑 ~

const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

// ──────────────────────── 工具 ────────────────────────

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json", ...(opts.headers || {}) };
  const resp = await fetch(path, {
    ...opts,
    headers,
  });
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.detail || resp.statusText);
  return data;
}

// ──────────────────────── 页面初始化 ────────────────────────

let _appIntervalStarted = false;

function initApp() {
  loadProxyConfig();
  refreshStats();
  refreshPool();
  _connectAutoStream();
  if (!_appIntervalStarted) {
    _appIntervalStarted = true;
    setInterval(refreshStats, 5000);
  }
}

function fmtTime(ts) {
  if (!ts) return "-";
  return new Date(ts * 1000).toLocaleString("zh-CN", { hour12: false });
}

function logLine(text, kind = "") {
  const box = $("#logBox");
  const div = document.createElement("div");
  div.className = "line " + kind;
  div.textContent = text;
  box.appendChild(div);
  box.scrollTop = box.scrollHeight;
}

$("#btnCopyLog").addEventListener("click", async (e) => {
  const text = $("#logBox").innerText || "";
  if (!text.trim()) {
    alert("当前日志为空");
    return;
  }
  await _copyText(text, e.currentTarget);
});

function _setLogExpanded(active) {
  const box = $("#logBox");
  const backdrop = $("#logBackdrop");
  const closeBtn = $("#btnCloseLogExpand");
  const btn = $("#btnFullscreenLog");
  box.classList.toggle("web-fullscreen", active);
  backdrop.classList.toggle("hidden", !active);
  closeBtn.classList.toggle("hidden", !active);
  btn.textContent = active ? "⛶ 退出放大" : "⛶ 放大日志";
  if (active) box.scrollTop = box.scrollHeight;
}

$("#btnFullscreenLog").addEventListener("click", () => {
  _setLogExpanded(!$("#logBox").classList.contains("web-fullscreen"));
});

$("#logBackdrop").addEventListener("click", () => _setLogExpanded(false));
$("#btnCloseLogExpand").addEventListener("click", () => _setLogExpanded(false));

function classifyLog(line) {
  const l = line.toLowerCase();
  if (l.includes("error") || l.includes("失败") || l.includes("拒绝")) return "err";
  if (l.includes("warning") || l.includes("warn")) return "warn";
  if (l.includes("成功") || l.includes("完成") || l.includes("命中") || l.includes("ok")) return "ok";
  return "";
}

// ──────────────────────── 统计栏 ────────────────────────

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

// ──────────────────────── 导入 ────────────────────────

$("#btnImport").addEventListener("click", async () => {
  const text = $("#importText").value.trim();
  if (!text) {
    $("#importResult").textContent = "请输入要导入的接码号";
    return;
  }
  $("#btnImport").disabled = true;
  $("#importResult").textContent = "导入中...";
  try {
    const r = await api("/api/import", {
      method: "POST",
      body: JSON.stringify({ text }),
    });
    $("#importResult").textContent =
      `✅ 解析 ${r.parsed} 行，新增 ${r.inserted}，更新 ${r.updated}，跳过 ${r.skipped}`;
    $("#importResult").className = "result ok";
    $("#importText").value = "";
    refreshStats();
    refreshPool();
  } catch (e) {
    $("#importResult").textContent = "❌ " + e.message;
    $("#importResult").className = "result bad";
  } finally {
    $("#btnImport").disabled = false;
  }
});

// ──────────────────────── 触发注册 ────────────────────────

let currentEs = null;

$("#btnRun").addEventListener("click", async () => {
  const email = $("#regEmail").value.trim();
  const opts = {
    email: email || null,
    proxy: $("#regProxy").value.trim(),
    otp_timeout: parseInt($("#regOtpTimeout").value || "180", 10),
    want_access_token: true,
    want_session_token: true,
    want_refresh_token: true,
    strict_email: $("#regStrictEmail").checked,
    proxy_pool: $("#autoProxyPool").value,
    random_proxy_from_pool: $("#regRandomProxyFromPool").checked,
  };
  $("#btnRun").disabled = true;
  $("#runStatus").textContent = "启动中...";
  $("#runStatus").className = "result";
  $("#logBox").innerHTML = "";

  try {
    const r = await api("/api/register", {
      method: "POST",
      body: JSON.stringify(opts),
    });
    $("#runStatus").textContent = `🚀 已启动 run_id=${r.run_id} email=${r.email}`;
    logLine(`[client] 启动注册 run_id=${r.run_id} email=${r.email}`, "evt");
    streamRun(r.run_id);
  } catch (e) {
    $("#runStatus").textContent = "❌ " + e.message;
    $("#runStatus").className = "result bad";
    $("#btnRun").disabled = false;
  }
});

function makeEventSource(path) {
  return new EventSource(path);
}

function streamRun(runId) {
  if (currentEs) { try { currentEs.close(); } catch (_) {} }
  const es = makeEventSource(`/api/runs/${runId}/stream`);
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
        const s = `✅ 注册完成: access_token=${d.access_token_len}${d.partial ? "  (部分凭证)" : ""}`;
        const buttons = [];
        if (d.access_token_len > 0)  buttons.push(`<button class="quick-copy" data-email="${d.email}" data-field="access_token">📋 复制 access_token</button>`);
        $("#runStatus").innerHTML = `<span class="ok">${s}</span>${buttons.length ? "<br>" + buttons.join(" ") : ""}`;
        logLine("[client] " + s, "evt");
      } else if (d.kind === "error") {
        $("#runStatus").textContent = "❌ " + d.message;
        $("#runStatus").className = "result bad";
        logLine("[client] ❌ " + d.message, "err");
      } else if (d.kind === "phase") {
        logLine(`[client] phase=${d.phase} email=${d.email || ""}`, "evt");
        if (d.phase === "email_claimed" && d.email) {
          const cur = $("#runStatus").textContent || "";
          const runPart = cur.match(/run_id=[^\s]+/) || [""];
          $("#runStatus").textContent = `🚀 已启动 ${runPart[0]} email=${d.email}`;
        }
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

// 状态栏快捷复制按钮（注册完成后直接显示在这里，不用切 Tab）
$("#runStatus").addEventListener("click", async (e) => {
  const copyBtn = e.target.closest("button.quick-copy");
  if (copyBtn) {
    const email = copyBtn.dataset.email;
    const field = copyBtn.dataset.field;
    try {
      const cred = await _loadCred(email);
      const val = cred[field] || "";
      if (!val) { alert(`${field} 为空`); return; }
      await _copyText(val, copyBtn);
    } catch (err) { alert("加载凭证失败: " + err.message); }
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

// ──────────────────────── 号池列表 ────────────────────────

async function refreshPool() {
  const status = $("#poolFilter").value;
  const { items } = await api(`/api/accounts?status=${encodeURIComponent(status)}`);
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
        <button data-act="use" data-email="${r.email}">使用</button>
        ${canReset ? `<button data-act="reset" data-email="${r.email}" title="改回 available 重新注册">🔄 重置</button>` : ""}
        <button data-act="del" data-email="${r.email}">删除</button>
      </td>
    `;
    tb.appendChild(tr);
  }
  $("#poolSelectAll").checked = false;
  _updateSelCount();
}
$("#btnRefreshPool").addEventListener("click", refreshPool);
$("#poolFilter").addEventListener("change", refreshPool);

$("#btnResetFailed").addEventListener("click", async () => {
  if (!confirm("把所有 failed 号重置为 available？")) return;
  $("#poolActionResult").textContent = "处理中...";
  $("#poolActionResult").className = "result";
  try {
    const r = await api("/api/accounts/reset_failed", { method: "POST" });
    $("#poolActionResult").textContent = `✅ 重置 ${r.reset} 个号为 available`;
    $("#poolActionResult").className = "result ok";
    refreshPool(); refreshStats();
  } catch (e) {
    $("#poolActionResult").textContent = "❌ " + e.message;
    $("#poolActionResult").className = "result bad";
  }
});

$("#btnReleaseStale").addEventListener("click", async () => {
  $("#poolActionResult").textContent = "处理中...";
  $("#poolActionResult").className = "result";
  try {
    const r = await api("/api/accounts/release_stale", { method: "POST" });
    $("#poolActionResult").textContent = `✅ 释放 ${r.released} 个卡死号`;
    $("#poolActionResult").className = "result ok";
    refreshPool(); refreshStats();
  } catch (e) {
    $("#poolActionResult").textContent = "❌ " + e.message;
    $("#poolActionResult").className = "result bad";
  }
});

// ── 号池：复选框选择 + 批量删除 ──

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
  if (!confirm(`重置选中的 ${emails.length} 个号为 available？\n（号会重新可用，已保存的凭证不变）`)) return;
  $("#poolActionResult").textContent = "重置中...";
  $("#poolActionResult").className = "result";
  try {
    const r = await api("/api/accounts/bulk_reset", {
      method: "POST",
      body: JSON.stringify({ emails }),
    });
    $("#poolActionResult").textContent = `✅ 已重置 ${r.reset} 个号`;
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
  if (!confirm(`确定删除选中的 ${emails.length} 个号？(不可恢复)`)) return;
  $("#poolActionResult").textContent = "删除中...";
  $("#poolActionResult").className = "result";
  try {
    const r = await api("/api/accounts/bulk_delete", {
      method: "POST",
      body: JSON.stringify({ emails }),
    });
    $("#poolActionResult").textContent = `✅ 已删除 ${r.deleted} 个号`;
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
    $("#poolActionResult").textContent = "请先选择要删除的状态";
    $("#poolActionResult").className = "result bad";
    return;
  }
  const tip = status === "all"
    ? "⚠️ 这会删除号池里所有号（含未注册的），确定？"
    : `确定删除全部 ${status} 状态的号？`;
  if (!confirm(tip)) return;
  $("#poolActionResult").textContent = "删除中...";
  $("#poolActionResult").className = "result";
  try {
    const r = await api("/api/accounts/bulk_delete", {
      method: "POST",
      body: JSON.stringify({ status }),
    });
    $("#poolActionResult").textContent = `✅ 已删除 ${r.deleted} 个 ${status} 号`;
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
    if (!confirm(`重置 ${email} 为 available？\n（号会重新可用，但已保存的凭证不变）`)) return;
    try {
      await api(`/api/accounts/reset/${encodeURIComponent(email)}`, { method: "POST" });
      refreshPool();
      refreshStats();
    } catch (err) {
      alert("重置失败: " + err.message);
    }
  } else if (btn.dataset.act === "del") {
    if (!confirm(`删除 ${email}？`)) return;
    await api(`/api/accounts/${encodeURIComponent(email)}`, { method: "DELETE" });
    refreshPool();
    refreshStats();
  }
});

// ──────────────────────── 注册结果列表 ────────────────────────

async function refreshRegistered() {
  const { items } = await api("/api/registered");
  const filter = document.querySelector("input[name='regFilter']:checked")?.value || "all";

  // 按筛选条件过滤
  let filtered = items;
  if (filter === "has_rt") {
    filtered = items.filter(r => r.rt_len > 0);
  } else if (filter === "no_rt") {
    filtered = items.filter(r => r.rt_len === 0);
  }

  const tb = $("#regTable tbody");
  tb.innerHTML = "";
  for (const r of filtered) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><input type="checkbox" class="reg-check" data-email="${r.email}"></td>
      <td>${r.email}</td>
      <td>${r.at_len > 0 ? `<button class="copy-cell" data-email="${r.email}" data-field="access_token" title="点击复制 access_token">✅ ${r.at_len} 📋</button>` : "—"}</td>
      <td>${r.st_len > 0 ? `<button class="copy-cell" data-email="${r.email}" data-field="session_token" title="点击复制 session_token">✅ ${r.st_len} 📋</button>` : "—"}</td>
      <td>${r.rt_len > 0 ? `<button class="copy-cell" data-email="${r.email}" data-field="refresh_token" title="点击复制 refresh_token">✅ ${r.rt_len} 📋</button>` : "—"}</td>
      <td>${fmtTime(r.created_at)}</td>
      <td>
        <button data-act="view" data-email="${r.email}">查看凭证</button>
        <button data-act="del" data-email="${r.email}">删除</button>
      </td>
    `;
    tb.appendChild(tr);
  }
  $("#regSelectAll").checked = false;
  _updateSelCountReg();
}
$("#btnRefreshReg").addEventListener("click", refreshRegistered);

// radio 切换时自动刷新
document.querySelectorAll("input[name='regFilter']").forEach(r => {
  r.addEventListener("change", refreshRegistered);
});

// ── 注册结果：复选框 + 批量删 + 单行删 ──

function _selectedRegEmails() {
  return Array.from(document.querySelectorAll(".reg-check:checked")).map(c => c.dataset.email);
}
function _updateSelCountReg() {
  const n = _selectedRegEmails().length;
  $("#selCountReg").textContent = n;
  $("#btnDeleteSelectedReg").disabled = n === 0;
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
  if (!confirm(`确定删除选中的 ${emails.length} 条凭证？(不可恢复)`)) return;
  $("#exportResult").textContent = "删除中...";
  $("#exportResult").className = "result";
  try {
    const r = await api("/api/registered/bulk_delete", {
      method: "POST",
      body: JSON.stringify({ emails }),
    });
    $("#exportResult").textContent = `✅ 已删除 ${r.deleted} 条凭证`;
    $("#exportResult").className = "result ok";
    refreshRegistered();
  } catch (e) {
    $("#exportResult").textContent = "❌ " + e.message;
    $("#exportResult").className = "result bad";
  }
});

$("#btnDeleteAllReg").addEventListener("click", async () => {
  if (!confirm("⚠️ 这会清空注册结果表里的所有凭证！\n确定继续？（号池不受影响）")) return;
  if (!confirm("再次确认：真的要删除全部凭证吗？此操作不可恢复！")) return;
  $("#exportResult").textContent = "清空中...";
  $("#exportResult").className = "result";
  try {
    const r = await api("/api/registered/bulk_delete", {
      method: "POST",
      body: JSON.stringify({ all: true }),
    });
    $("#exportResult").textContent = `✅ 已清空 ${r.deleted} 条凭证`;
    $("#exportResult").className = "result ok";
    refreshRegistered();
  } catch (e) {
    $("#exportResult").textContent = "❌ " + e.message;
    $("#exportResult").className = "result bad";
  }
});

// 缓存最近查看的凭证（用于"复制全部 JSON"按钮和单字段复制）
let _credCache = null;

async function _loadCred(email) {
  if (_credCache && _credCache.email === email) return _credCache;
  const { data } = await api(`/api/registered/${encodeURIComponent(email)}`);
  _credCache = data;
  return data;
}

async function _copyText(text, btn) {
  try {
    await navigator.clipboard.writeText(text);
    if (btn) {
      const orig = btn.textContent;
      const cls = btn.className;
      btn.textContent = "✅ 已复制";
      btn.className = cls + " copied";
      setTimeout(() => { btn.textContent = orig; btn.className = cls; }, 1200);
    }
  } catch (e) {
    alert("复制失败: " + e.message);
  }
}

$("#regTable").addEventListener("click", async (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  const email = btn.dataset.email;
  if (!email) return;

  // 行内快捷复制（access/session/refresh 列直接点）
  if (btn.classList.contains("copy-cell")) {
    const field = btn.dataset.field;
    try {
      const cred = await _loadCred(email);
      const val = cred[field] || "";
      if (!val) { alert(`${field} 为空`); return; }
      await _copyText(val, btn);
    } catch (err) { alert("加载凭证失败: " + err.message); }
    return;
  }

  // 「查看凭证」打开模态框
  if (btn.dataset.act === "view") {
    try {
      const cred = await _loadCred(email);
      _renderCredModal(email, cred);
    } catch (err) { alert("加载凭证失败: " + err.message); }
  }

  // 「删除」单行删
  if (btn.dataset.act === "del") {
    if (!confirm(`删除 ${email} 的凭证？`)) return;
    try {
      await api(`/api/registered/${encodeURIComponent(email)}`, { method: "DELETE" });
      refreshRegistered();
    } catch (err) { alert("删除失败: " + err.message); }
  }
});


function _renderCredModal(email, cred) {
  $("#credTitle").textContent = email;
  const box = $("#credFields");
  box.innerHTML = "";

  // 主要凭证按顺序展示，每项独立复制按钮
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
        <button class="cred-copy" data-val-key="${key}">📋 复制</button>
      </div>
      <pre class="cred-val">${escapeHtml(val)}</pre>
    `;
    box.appendChild(row);
  }

  // extra（含 cookie 同步等其他元数据）
  if (cred.extra && Object.keys(cred.extra).length > 0) {
    const row = document.createElement("div");
    row.className = "cred-row";
    row.innerHTML = `
      <div class="cred-row-head">
        <span class="cred-label">extra</span>
        <span class="cred-meta">${Object.keys(cred.extra).length} keys</span>
        <button class="cred-copy" data-val-key="__extra__">📋 复制 JSON</button>
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

// 模态框内单字段复制
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

// ──────────────────────── 运行记录 ────────────────────────

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

// ──────────────────────── 🤖 Auto-Loop 全自动批量 ────────────────────────

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
    otp_timeout: parseInt($("#regOtpTimeout").value || "180", 10),
    want_access_token: true,
    want_session_token: true,
    want_refresh_token: true,
    cool_down_seconds: parseFloat($("#autoCoolDown").value || "3") || 0,
    auto_rotate_proxy: $("#autoRotateProxy").checked,
    rotate_proxy_every: parseInt($("#rotateProxyEvery").value || "5", 10) || 5,
  };
}

async function autoStart() {
  try {
    await api("/api/auto/start", { method: "POST", body: JSON.stringify(_autoOptions()) });
  } catch (e) { alert("启动失败: " + e.message); }
}

async function testProxyPool() {
  const resultEl = $("#proxyTestResult");
  resultEl.textContent = "⏳ 测试中...";
  resultEl.className = "result";
  try {
    const r = await api("/api/settings/proxy/test", {
      method: "POST",
      body: JSON.stringify({
        proxy_pool: $("#autoProxyPool").value,
        proxy: $("#regProxy").value.trim(),
      }),
    });
    const failed = r.results.filter(x => !x.ok);
    let html = `✅ ${r.available}/${r.total} 条代理可用`;
    if (failed.length) {
      html += `<br>❌ 失败 ${failed.length} 条：<br>` +
        failed.map(x => `<code>${escapeHtml(x.proxy)}</code>: ${escapeHtml(x.error)}`).join("<br>");
    }
    resultEl.innerHTML = html;
    resultEl.className = "result ok";
  } catch (e) {
    resultEl.textContent = "❌ " + e.message;
    resultEl.className = "result bad";
  }
}
$("#btnTestProxyPool").addEventListener("click", testProxyPool);

async function autoCall(path) {
  try { await api(path, { method: "POST" }); }
  catch (e) { alert(`${path} 失败: ${e.message}`); }
}
AUTO_BTNS.start.addEventListener("click", autoStart);
AUTO_BTNS.pause.addEventListener("click", () => autoCall("/api/auto/pause"));
AUTO_BTNS.resume.addEventListener("click", () => autoCall("/api/auto/resume"));
AUTO_BTNS.stop.addEventListener("click", () => autoCall("/api/auto/stop"));

function _renderAutoStatus(s) {
  const stateLabel = {
    "stopped": "⚪ 未运行",
    "running": "🟢 运行中",
    "paused":  "⏸ 已暂停",
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
  let meta = `并发=${s.concurrency || 1}` + (s.proxy_pool_size ? ` 代理池=${s.proxy_pool_size}` : "");
  if (s.auto_rotate_proxy) {
    meta += ` 轮换=${s.rotate_proxy_every}`;
  }
  $("#autoStatus").innerHTML = `
    <b>${stateLabel}</b>
    &nbsp;|&nbsp; 已完成: <b class="ok">${s.registered_ok}</b> 成功 / <b class="bad">${s.registered_fail}</b> 失败
    &nbsp;|&nbsp; 运行: ${elapsed}
    &nbsp;|&nbsp; <span class="auto-meta">${meta}</span>
    ${workerRows ? "<br>" + workerRows : ""}
    <br><span class="auto-msg">${escapeHtml(s.last_message || "")}</span>
  `;
  // 按钮可用性
  const st = s.state;
  AUTO_BTNS.start.disabled  = (st === "running" || st === "paused");
  AUTO_BTNS.pause.disabled  = (st !== "running");
  AUTO_BTNS.resume.disabled = (st !== "paused");
  AUTO_BTNS.stop.disabled   = (st === "stopped");
}

let _autoEs = null;
function _connectAutoStream() {
  if (_autoEs) { try { _autoEs.close(); } catch (_) {} }
  const es = makeEventSource("/api/auto/stream");
  _autoEs = es;
  es.addEventListener("state", (e) => {
    try { _renderAutoStatus(JSON.parse(e.data)); } catch (_) {}
  });
  es.addEventListener("run_started", (e) => {
    try {
      const d = JSON.parse(e.data);
      logLine(`[auto] ▶ 开始注册 ${d.email} (run=${d.run_id})`, "evt");
      // 复用单跑的 SSE 流，自动接管日志框 + 状态栏复制按钮
      streamRun(d.run_id);
    } catch (_) {}
  });
  es.addEventListener("run_finished", (e) => {
    try {
      const d = JSON.parse(e.data);
      const tag = d.ok ? "✅" : (d.category === "network" ? "🌐 网络错误（号已 release）" : "❌");
      logLine(`[auto] ${tag} ${d.email} 完成`, d.ok ? "ok" : "err");
    } catch (_) {}
  });
  es.addEventListener("circuit_break", (e) => {
    try {
      const d = JSON.parse(e.data);
      logLine(`[auto] ⚠️ 熔断: ${d.reason}`, "err");
      _showBanner(d.reason);
    } catch (_) {}
  });
  es.onerror = () => {
    // 自动重连
    try { es.close(); } catch (_) {}
    _autoEs = null;
    setTimeout(_connectAutoStream, 2000);
  };
}

// 顶部红色告警横幅
function _showBanner(msg) {
  const b = $("#alertBanner");
  $("#alertBannerMsg").textContent = msg;
  b.classList.remove("hidden");
}
$("#alertBannerClose").addEventListener("click", () => {
  $("#alertBanner").classList.add("hidden");
});

// ──────────────────────── 表单持久化（localStorage 自动保存/恢复）────────────────────────

const FORM_KEY = "gpt_outlook_register_form_v1";

// id -> 类型（默认 text；checkbox 走 .checked）
const PERSIST_FIELDS = {
  regProxy:        "text",
  regOtpTimeout:   "text",
  regStrictEmail:  "check",
  regRandomProxyFromPool: "check",
  autoCoolDown:      "text",
  autoConcurrency:   "text",
  autoProxyPool:     "text",
  autoRotateProxy:   "check",
  rotateProxyEvery:  "text",
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

// 绑定 input/change 自动保存
function _bindAutoSave() {
  for (const id of Object.keys(PERSIST_FIELDS)) {
    const el = document.getElementById(id);
    if (!el) continue;
    el.addEventListener("input", _saveForm);
    el.addEventListener("change", _saveForm);
  }
}

// ──────────────────────── 🌐 代理配置 ────────────────────────

async function loadProxyConfig() {
  try {
    const { config } = await api("/api/settings/proxy");
    $("#regProxy").value = config.proxy || "";
    $("#autoProxyPool").value = config.proxy_pool || "";
    $("#autoRotateProxy").checked = config.auto_rotate_proxy === "1";
    $("#rotateProxyEvery").value = config.rotate_proxy_every || "5";
    $("#proxyMaxUses").value = config.proxy_max_uses || "10";
    _saveForm();
  } catch (e) {
    console.error("loadProxyConfig:", e);
  }
}

$("#btnSaveProxyCfg").addEventListener("click", async () => {
  const resultEl = $("#proxyCfgResult");
  resultEl.textContent = "保存中...";
  resultEl.className = "result";
  try {
    await api("/api/settings/proxy", {
      method: "POST",
      body: JSON.stringify({
        proxy: $("#regProxy").value.trim(),
        proxy_pool: $("#autoProxyPool").value,
        auto_rotate_proxy: $("#autoRotateProxy").checked,
        rotate_proxy_every: parseInt($("#rotateProxyEvery").value || "5", 10) || 5,
        proxy_max_uses: parseInt($("#proxyMaxUses").value || "10", 10) || 10,
      }),
    });
    resultEl.textContent = "✅ 已保存";
    resultEl.className = "result ok";
    _saveForm();
  } catch (e) {
    resultEl.textContent = "❌ " + e.message;
    resultEl.className = "result bad";
  }
  setTimeout(() => { resultEl.textContent = ""; }, 3000);
});


// ──────────────────────── 📧 邮箱配置 ────────────────────────

function _syncCfFields(source) {
  $("#cfTempCfg").classList.toggle("hidden", source !== "cf_temp");
  $("#oepCfg").classList.toggle("hidden", source !== "oep");
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
      $("#cfAdminToken").placeholder = "已设置（留空不修改）";
    } else {
      $("#cfAdminToken").placeholder = "Worker 配置的 ADMIN_PASSWORDS";
    }
    $("#oepApiUrl").value = config.oep_api_url || "";
    $("#oepProjectKey").value = config.oep_project_key || "";
    $("#oepCallerId").value = config.oep_caller_id || "gpt-outlook-register";
    $("#oepApiKey").value = "";
    if (config.oep_api_key === "***") {
      $("#oepApiKey").placeholder = "已设置（留空不修改）";
    } else {
      $("#oepApiKey").placeholder = "平台 X-API-Key";
    }
  } catch (e) {
    console.error("loadMailConfig:", e);
  }
}

// radio 切换显隐
document.querySelectorAll("input[name='mailSource']").forEach(r => {
  r.addEventListener("change", () => _syncCfFields(r.value));
});

$("#btnSaveMailCfg").addEventListener("click", async () => {
  const source = document.querySelector("input[name='mailSource']:checked")?.value || "outlook";
  const isCf = source === "cf_temp";
  const isOep = source === "oep";
  const body = {
    mail_source:    source,
    cf_api_url:     isCf ? $("#cfApiUrl").value.trim() : "",
    cf_admin_token: isCf ? ($("#cfAdminToken").value.trim() || "***") : "***",
    cf_domain:      isCf ? $("#cfDomain").value.trim() : "",
    oep_api_url:    isOep ? $("#oepApiUrl").value.trim() : "",
    oep_api_key:    isOep ? ($("#oepApiKey").value.trim() || "***") : "***",
    oep_project_key: isOep ? $("#oepProjectKey").value.trim() : "",
    oep_caller_id:  isOep ? $("#oepCallerId").value.trim() : "",
  };
  try {
    await api("/api/settings/mail", { method: "POST", body: JSON.stringify(body) });
    $("#mailCfgResult").textContent = "✅ 保存成功";
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
  btn.textContent = "⏳ 测试中...";
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
    btn.textContent = "🔌 测试连通性";
  }
});

// ──────────────────────── 📱 SMS 接码配置 ────────────────────────

// 全量国家列表（id → name_cn + openai_sms_safe）；首次加载配置时从后端拿
let _smsAllCountries = [];
let _smsSafeCountrySet = new Set();

async function _loadSmsAllCountries() {
  if (_smsAllCountries.length) return _smsAllCountries;
  try {
    const r = await api("/api/settings/sms/all_countries");
    _smsAllCountries = r.countries || [];
    _smsSafeCountrySet = new Set(r.openai_sms_safe || []);
  } catch (e) {
    console.error("加载国家列表失败:", e);
  }
  return _smsAllCountries;
}

function _renderSmsCountrySelect(selectEl, currentValue) {
  selectEl.innerHTML = "";
  for (const c of _smsAllCountries) {
    const opt = document.createElement("option");
    opt.value = c.id;
    opt.textContent = `${c.id} - ${c.name_cn}`;
    selectEl.appendChild(opt);
  }
  if (currentValue) selectEl.value = currentValue;
}

function _renderSmsAllowedCountriesBox(checkedIds) {
  const box = $("#smsAllowedCountriesBox");
  box.innerHTML = "";
  const checkedSet = new Set((checkedIds || "").split(",").map(s => s.trim()).filter(Boolean));

  // 先渲染所有国家（带 data 属性用于搜索）
  for (const c of _smsAllCountries) {
    const lab = document.createElement("label");
    lab.className = "check country-item";
    lab.style.fontSize = "12px";
    lab.style.padding = "4px 6px";
    lab.style.lineHeight = "1.4";
    lab.dataset.countryId = c.id;
    lab.dataset.countryName = c.name_cn.toLowerCase();

    // 显示：ID·国家名 (价格/库存)
    const priceInfo = c.price != null && c.count != null
      ? ` <span style="color:#999;font-size:11px">(${c.price}/${c.count})</span>`
      : "";
    lab.innerHTML = `<input type="checkbox" value="${c.id}" ${checkedSet.has(c.id) ? "checked" : ""}>${c.id}·${c.name_cn}${priceInfo}`;
    box.appendChild(lab);
  }

  _updateAllowedCountryCount();
  box.querySelectorAll("input[type=checkbox]").forEach(cb => {
    cb.addEventListener("change", _updateAllowedCountryCount);
  });

  // 绑定搜索框
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
  $("#smsAllowedCountryCount").textContent = `已选 ${checked.length} 个国家`;
}

function _getAllowedCountriesValue() {
  const checked = $("#smsAllowedCountriesBox").querySelectorAll("input[type=checkbox]:checked");
  return Array.from(checked).map(cb => cb.value).join(",");
}

function _updateSmsPerPhoneHint() {
  const interval = parseInt($("#smsResendInterval").value, 10) || 20;
  const max = parseInt($("#smsResendMax").value, 10) || 3;
  const hint = $("#smsPerPhoneHint");
  if (hint) hint.textContent = interval * max + 20;
}

async function loadSmsConfig() {
  await _loadSmsAllCountries();
  try {
    const { config } = await api("/api/settings/sms");
    $("#smsEnabled").checked = config.sms_enabled === "1";
    const provider = config.sms_provider || "smsbower";
    const radio = document.querySelector(`input[name="smsProvider"][value="${provider}"]`);
    if (radio) radio.checked = true;

    $("#smsbowerApiKey").value = "";
    $("#smsbowerApiKey").placeholder = (config.smsbower_api_key === "***")
      ? "已设置（留空不修改）"
      : "粘贴 SmsBower API Key";
    $("#herosmsApiKey").value = "";
    $("#herosmsApiKey").placeholder = (config.herosms_api_key === "***")
      ? "已设置（留空不修改）"
      : "粘贴 HeroSMS API Key";

    _renderSmsCountrySelect($("#smsCountry"), config.sms_country || "150");
    $("#smsService").value = config.sms_service || "dr";
    $("#smsMaxPrice").value = config.sms_max_price || "";
    $("#smsPhoneSuccessMax").value = config.sms_phone_success_max || "3";
    $("#smsReusePhone").checked = config.sms_reuse_phone === "1";
    $("#smsAutoCountry").checked = config.sms_auto_country === "1";
    $("#smsKeepCountry").checked = config.sms_keep_country === "1";
    $("#smsAutoMinStock").value = config.sms_auto_min_stock || "20";
    $("#smsAutoMaxPrice").value = config.sms_auto_max_price || "";
    _renderSmsAllowedCountriesBox(config.sms_allowed_countries || "");
    $("#smsMaxPhoneAttempts").value = config.sms_max_phone_attempts || "";
    $("#smsResendInterval").value = config.sms_resend_interval || "20";
    $("#smsResendMax").value = config.sms_resend_max || "3";
    _updateSmsPerPhoneHint();
    $("#smsMinBalance").value = config.sms_min_balance || "";
  } catch (e) {
    console.error("loadSmsConfig:", e);
  }
}

$("#btnSelectAllAllowedCountries")?.addEventListener("click", () => {
  $("#smsAllowedCountriesBox").querySelectorAll("input[type=checkbox]").forEach(cb => {
    cb.checked = true;
  });
  _updateAllowedCountryCount();
});

$("#btnInvertAllowedCountries")?.addEventListener("click", () => {
  $("#smsAllowedCountriesBox").querySelectorAll("input[type=checkbox]").forEach(cb => {
    cb.checked = !cb.checked;
  });
  _updateAllowedCountryCount();
});

$("#btnClearAllowedCountries")?.addEventListener("click", () => {
  $("#smsAllowedCountriesBox").querySelectorAll("input[type=checkbox]").forEach(cb => {
    cb.checked = false;
  });
  _updateAllowedCountryCount();
});

["#smsResendInterval", "#smsResendMax"].forEach(sel => {
  $(sel)?.addEventListener("input", _updateSmsPerPhoneHint);
  $(sel)?.addEventListener("change", _updateSmsPerPhoneHint);
});

$("#btnSaveSmsCfg").addEventListener("click", async () => {
  const body = {
    sms_enabled:           $("#smsEnabled").checked ? "1" : "0",
    sms_provider:          document.querySelector("input[name='smsProvider']:checked")?.value || "smsbower",
    smsbower_api_key:      $("#smsbowerApiKey").value.trim() || "***",
    herosms_api_key:       $("#herosmsApiKey").value.trim() || "***",
    sms_country:           $("#smsCountry").value.trim() || "52",
    sms_service:           $("#smsService").value.trim() || "dr",
    sms_max_price:         $("#smsMaxPrice").value.trim(),
    sms_phone_success_max: $("#smsPhoneSuccessMax").value.trim() || "3",
    sms_reuse_phone:       $("#smsReusePhone").checked ? "1" : "0",
    sms_auto_country:      $("#smsAutoCountry").checked ? "1" : "0",
    sms_keep_country:      $("#smsKeepCountry").checked ? "1" : "0",
    sms_allowed_countries: _getAllowedCountriesValue(),
    sms_auto_min_stock:    $("#smsAutoMinStock").value.trim() || "20",
    sms_auto_max_price:    $("#smsAutoMaxPrice").value.trim(),
    sms_max_phone_attempts: $("#smsMaxPhoneAttempts").value.trim(),
    sms_resend_interval:    $("#smsResendInterval").value.trim() || "20",
    sms_resend_max:         $("#smsResendMax").value.trim() || "3",
    sms_min_balance:       $("#smsMinBalance").value.trim(),
  };
  try {
    await api("/api/settings/sms", { method: "POST", body: JSON.stringify(body) });
    $("#smsCfgResult").textContent = "✅ 保存成功";
    $("#smsCfgResult").className = "result ok";
    setTimeout(loadSmsConfig, 300);
  } catch (e) {
    $("#smsCfgResult").textContent = "❌ " + e.message;
    $("#smsCfgResult").className = "result bad";
  }
  setTimeout(() => { $("#smsCfgResult").textContent = ""; }, 3500);
});

async function _testSmsProvider(provider, btn, origText) {
  btn.disabled = true;
  btn.textContent = "⏳ 测试中...";
  $("#smsCfgResult").textContent = "";
  try {
    const r = await api("/api/settings/sms/test", {
      method: "POST",
      body: JSON.stringify({ provider }),
    });
    $("#smsCfgResult").textContent = "✅ " + r.message;
    $("#smsCfgResult").className = "result ok";
  } catch (err) {
    $("#smsCfgResult").textContent = "❌ " + err.message;
    $("#smsCfgResult").className = "result bad";
  } finally {
    btn.disabled = false;
    btn.textContent = origText;
  }
}

$("#btnTestSmsbower").addEventListener("click", async (e) => {
  await _testSmsProvider("smsbower", e.currentTarget, "🔌 测试 SmsBower 余额");
});

$("#btnTestHerosms").addEventListener("click", async (e) => {
  await _testSmsProvider("herosms", e.currentTarget, "🔌 测试 HeroSMS 余额");
});

// ──────────────────────── 📤 自动导出配置 (CPA / SUB2API) ────────────────────────

async function loadExportConfig() {
  try {
    const { config } = await api("/api/settings/export");
    // CPA
    $("#cpaEnabled").checked = config.cpa_enabled === "1";
    $("#cpaUrl").value = config.cpa_url || "";
    $("#cpaMgmtKey").value = "";
    $("#cpaMgmtKey").placeholder = config.cpa_mgmt_key === "***"
      ? "已设置（留空不修改）"
      : "粘贴 CPA 管理密钥";
    $("#cpaTimeout").value = config.cpa_timeout || "30";
    // SUB2API
    $("#sub2apiEnabled").checked = config.sub2api_enabled === "1";
    $("#sub2apiUrl").value = config.sub2api_url || "";
    $("#sub2apiApiKey").value = "";
    $("#sub2apiApiKey").placeholder = config.sub2api_api_key === "***"
      ? "已设置（留空不修改）"
      : "粘贴面板里生成的 x-api-key";
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
    $("#exportCfgResult").textContent = "✅ 保存成功";
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
  btn.textContent = "⏳ 测试中...";
  resultEl.textContent = "";
  try {
    const r = await api("/api/settings/export/test", {
      method: "POST",
      body: JSON.stringify({ target }),
    });
    resultEl.textContent = "✅ " + (r.message || "连通正常");
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
  _testExportTarget("cpa", e.currentTarget, $("#cpaTestResult"), "🔌 测试 CPA 连通性");
});
$("#btnTestSub2api").addEventListener("click", (e) => {
  _testExportTarget("sub2api", e.currentTarget, $("#sub2apiTestResult"), "🔌 测试 SUB2API 连通性");
});

// ──────────────────────── 启动 ────────────────────────

_loadForm();
_bindAutoSave();
initApp();
