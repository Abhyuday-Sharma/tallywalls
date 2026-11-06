/**
 * GST Intelligence & Reconciliation Hub Frontend Engine (v2.3.0).
 * Handles file ingestion, live GST Portal bridge, Multi-Month YTD Batch automation (Apr-Oct),
 * Tally Prime direct sync (port 9000), local Downloads scanner, dynamic filters, and multi-sheet audit exports.
 */

let currentReconData = null;
let currentFilter = "ALL";
let searchQuery = "";
let currentPortalMode = "batch";
let currentDownloadsMatches = [];

// Format Indian Currency (Lakhs & Crores with 2 decimals)
function formatINR(val) {
  if (val === null || val === undefined || isNaN(val)) return "—";
  const num = Number(val);
  return "₹" + num.toLocaleString("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
  });
}

document.addEventListener("DOMContentLoaded", () => {
  setupDragAndDrop();
  initFinancialYears();
  setupEventListeners();
});

function setupDragAndDrop() {
  const setupZone = (dropZoneId, fileInputId, fileNameId) => {
    const zone = document.getElementById(dropZoneId);
    const input = document.getElementById(fileInputId);
    const label = document.getElementById(fileNameId);
    if (!zone || !input || !label) return;

    zone.addEventListener("click", () => input.click());

    ["dragenter", "dragover"].forEach(event => {
      zone.addEventListener(event, (e) => {
        e.preventDefault();
        e.stopPropagation();
        zone.classList.add("dragover");
      });
    });

    ["dragleave", "drop"].forEach(event => {
      zone.addEventListener(event, (e) => {
        e.preventDefault();
        e.stopPropagation();
        zone.classList.remove("dragover");
      });
    });

    zone.addEventListener("drop", (e) => {
      if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
        input.files = e.dataTransfer.files;
        updateFileLabel(input, label, zone);
      }
    });

    input.addEventListener("change", () => {
      updateFileLabel(input, label, zone);
    });
  };

  setupZone("dropZone2B", "file2B", "fileName2B");
  setupZone("dropZoneBooks", "fileBooks", "fileNameBooks");
}

function updateFileLabel(input, label, zone) {
  if (!input || !label || !zone) return;
  if (input.files && input.files.length > 0) {
    let fileDesc = "";
    if (input.files.length === 1) {
      const file = input.files[0];
      const sizeKb = (file.size / 1024).toFixed(1);
      fileDesc = `${file.name} (${sizeKb} KB)`;
    } else {
      let totalBytes = 0;
      for (let i = 0; i < input.files.length; i++) totalBytes += input.files[i].size;
      const totalKb = (totalBytes / 1024).toFixed(1);
      fileDesc = `${input.files.length} Monthly Statements (${totalKb} KB Total)`;
    }
    label.innerHTML = `
      <div style="display:flex; align-items:center; justify-content:space-between; width:100%; gap:8px;">
        <span style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap;"><span style="color:#059669; font-weight:600;">Attached:</span> ${fileDesc}</span>
        <button type="button" class="btn-clear-attachment" title="Remove attached file" style="background:#fee2e2; color:#b91c1c; border:none; border-radius:3px; padding:2px 6px; font-size:0.7rem; cursor:pointer; font-weight:700;">Remove</button>
      </div>
    `;
    const clearBtn = label.querySelector(".btn-clear-attachment");
    if (clearBtn) {
      clearBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        input.value = "";
        label.textContent = "No file attached";
        zone.classList.remove("has-file");
      });
    }
    zone.classList.add("has-file");
  } else {
    label.textContent = "No file attached";
    zone.classList.remove("has-file");
  }
}

function initFinancialYears() {
  const fySelect = document.getElementById("portalFY");
  if (!fySelect) return;

  const years = [
    { value: "2026-27", label: "2026-27 (Current FY)", selected: true },
    { value: "2025-26", label: "2025-26" },
    { value: "2024-25", label: "2024-25" },
    { value: "2023-24", label: "2023-24" },
    { value: "2022-23", label: "2022-23" }
  ];

  fySelect.innerHTML = "";
  years.forEach(y => {
    const opt = document.createElement("option");
    opt.value = y.value;
    opt.textContent = y.label;
    if (y.selected) opt.selected = true;
    fySelect.appendChild(opt);
  });

  updateReturnPeriodsForFY();
}

function updateReturnPeriodsForFY() {
  const fySelect = document.getElementById("portalFY");
  const periodSelect = document.getElementById("portalPeriod");
  if (!fySelect || !periodSelect) return;

  const selectedFY = fySelect.value || "2026-27";
  const startYear = parseInt(selectedFY.split("-")[0]) || 2026;
  const endYear = startYear + 1;

  const months = [
    { name: "April", code: `04${startYear}`, selected: true },
    { name: "May", code: `05${startYear}` },
    { name: "June", code: `06${startYear}` },
    { name: "July", code: `07${startYear}` },
    { name: "August", code: `08${startYear}` },
    { name: "September", code: `09${startYear}` },
    { name: "October", code: `10${startYear}` },
    { name: "November", code: `11${startYear}` },
    { name: "December", code: `12${startYear}` },
    { name: "January", code: `01${endYear}` },
    { name: "February", code: `02${endYear}` },
    { name: "March", code: `03${endYear}` }
  ];

  periodSelect.innerHTML = "";
  months.forEach(m => {
    const opt = document.createElement("option");
    opt.value = m.code;
    opt.textContent = `${m.name} (FY ${selectedFY})`;
    if (m.selected) opt.selected = true;
    periodSelect.appendChild(opt);
  });
}

function setupEventListeners() {
  // Reconcile Button
  document.getElementById("btnReconcile").addEventListener("click", handleReconcile);

  // Load Enterprise Demo Button
  document.getElementById("btnLoadSample").addEventListener("click", handleLoadSample);

  // Export Excel Button
  document.getElementById("btnExportExcel").addEventListener("click", handleExportExcel);

  // Real-time Search Input
  document.getElementById("tableSearch").addEventListener("input", (e) => {
    searchQuery = e.target.value.toLowerCase().trim();
    renderTable();
  });

  // Segmented Filter Tabs
  document.querySelectorAll(".tab-pill").forEach(btn => {
    btn.addEventListener("click", (e) => {
      document.querySelectorAll(".tab-pill").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      currentFilter = btn.getAttribute("data-filter");
      renderTable();
    });
  });

  // Portal Bridge Modal Controls
  document.getElementById("btnLaunchBridge").addEventListener("click", () => handleLaunchBridge(false));
  document.getElementById("btnSimulatePortal").addEventListener("click", () => handleLaunchBridge(true));
  document.getElementById("portalFY").addEventListener("change", updateReturnPeriodsForFY);

  // Tally Modal Controls
  document.getElementById("btnSyncTallyLive").addEventListener("click", () => syncTallyVouchers(false));
  document.getElementById("btnSimulateTally").addEventListener("click", () => syncTallyVouchers(true));

  // Close modals when clicking backdrop
  ["portalModal", "downloadsModal", "tallyModal"].forEach(id => {
    const m = document.getElementById(id);
    if (m) {
      m.addEventListener("click", (e) => {
        if (e.target === m) m.style.display = "none";
      });
    }
  });
}

// -------------------------------------------------------------
// GST Portal Direct Bridge (Method 1)
// -------------------------------------------------------------

function openPortalModal() {
  initFinancialYears();
  const modal = document.getElementById("portalModal");
  if (modal) modal.style.display = "flex";
  setPortalModalMode(currentPortalMode);
  resetProgressBox();

  // Load saved credentials from local AES-256 storage (filtering out any dummy/example values)
  fetch("/api/portal/saved-creds")
    .then(res => res.json())
    .then(data => {
      const u = (data.username || "").trim();
      const p = data.password || "";
      const isDemo = !u || ["DEMO_USER", "TEST_GST_ACCOUNTANT", "DEMO", "TEST", "SAMPLE_USER", "ENTERPRISE_GST_USER"].includes(u.toUpperCase());
      const uInput = document.getElementById("portalUsername");
      const pInput = document.getElementById("portalPassword");
      if (uInput) uInput.value = isDemo ? "" : u;
      if (pInput) pInput.value = isDemo ? "" : p;
      const badge = document.getElementById("credsStatusBadge");
      if (badge) {
        badge.style.display = (!isDemo && data.has_saved) ? "flex" : "none";
      }
    })
    .catch(() => {});
}

function setPortalModalMode(mode) {
  currentPortalMode = mode;
  const tabBatch = document.getElementById("tabModeBatch");
  const tabSingle = document.getElementById("tabModeSingle");
  const bannerBatch = document.getElementById("portalBannerBatch");
  const bannerSingle = document.getElementById("portalBannerSingle");
  const grpBatch = document.getElementById("groupBatchRange");
  const grpSingle = document.getElementById("groupSingleMonth");
  const btnLaunch = document.getElementById("btnLaunchBridge");

  if (mode === "batch") {
    if (tabBatch) {
      tabBatch.style.background = "#2563eb";
      tabBatch.style.color = "#ffffff";
    }
    if (tabSingle) {
      tabSingle.style.background = "transparent";
      tabSingle.style.color = "var(--text-secondary)";
    }
    if (bannerBatch) bannerBatch.style.display = "flex";
    if (bannerSingle) bannerSingle.style.display = "none";
    if (grpBatch) grpBatch.style.display = "block";
    if (grpSingle) grpSingle.style.display = "none";
    if (btnLaunch) {
      const span = btnLaunch.querySelector("span");
      if (span) span.textContent = "Launch Batch Portal Sync (Apr - Oct)";
    }
  } else {
    if (tabBatch) {
      tabBatch.style.background = "transparent";
      tabBatch.style.color = "var(--text-secondary)";
    }
    if (tabSingle) {
      tabSingle.style.background = "#2563eb";
      tabSingle.style.color = "#ffffff";
    }
    if (bannerBatch) bannerBatch.style.display = "none";
    if (bannerSingle) bannerSingle.style.display = "flex";
    if (grpBatch) grpBatch.style.display = "none";
    if (grpSingle) grpSingle.style.display = "block";
    if (btnLaunch) {
      const span = btnLaunch.querySelector("span");
      if (span) span.textContent = "Launch Single Month Bridge";
    }
  }
}

function togglePasswordVisibility() {
  const pInput = document.getElementById("portalPassword");
  const btn = document.getElementById("btnTogglePass");
  if (!pInput || !btn) return;
  if (pInput.type === "password") {
    pInput.type = "text";
    btn.textContent = "Hide";
  } else {
    pInput.type = "password";
    btn.textContent = "Show";
  }
}

async function clearSavedCredentials() {
  try {
    const res = await fetch("/api/portal/clear-creds", { method: "POST" });
    if (res.ok) {
      const u = document.getElementById("portalUsername");
      const p = document.getElementById("portalPassword");
      if (u) u.value = "";
      if (p) p.value = "";
      const badge = document.getElementById("credsStatusBadge");
      if (badge) badge.style.display = "none";
    }
  } catch (e) {
    console.error("Failed to clear credentials:", e);
  }
}

/**
 * Safe fetch wrapper that handles JSON, non-JSON text, HTML 500s, and network drops
 * without throwing JSON.parse syntax errors.
 */
async function safeFetchJSON(url, options = {}) {
  let res;
  try {
    res = await fetch(url, options);
  } catch (netErr) {
    throw new Error(`Network connection error: ${netErr.message}`);
  }

  const contentType = res.headers.get("content-type") || "";
  let data = null;
  let rawText = "";

  if (contentType.includes("application/json")) {
    try {
      data = await res.json();
    } catch (_) {
      data = null;
    }
  } else {
    try {
      rawText = await res.text();
    } catch (_) {
      rawText = "";
    }
  }

  if (!res.ok) {
    const errorMsg = (data && (data.detail || data.message)) ||
      rawText.replace(/<[^>]+>/g, "").trim().slice(0, 300) ||
      `Server responded with HTTP ${res.status}`;
    throw new Error(errorMsg);
  }

  if (data !== null) return data;

  if (rawText) {
    try {
      return JSON.parse(rawText);
    } catch (_) {
      return { raw_content: rawText };
    }
  }

  return {};
}

async function safeParseError(res, fallback) {
  try {
    const text = await res.text();
    try {
      const parsed = JSON.parse(text);
      return parsed.detail || parsed.message || text || fallback;
    } catch (_) {
      return text || fallback;
    }
  } catch (_) {
    return fallback;
  }
}

function closePortalModal() {
  const m = document.getElementById("portalModal");
  if (m) m.style.display = "none";
}

function resetProgressBox() {
  const box = document.getElementById("portalProgressBox");
  box.style.display = "none";
  ["pStep1", "pStep2", "pStep3", "pStep4"].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.className = "progress-step-item";
  });
}

async function handleLaunchBridge(simulate = false) {
  const isBatch = currentPortalMode === "batch";
  const fy = document.getElementById("portalFY").value;
  const startMonth = isBatch ? (document.getElementById("portalStartMonth") ? document.getElementById("portalStartMonth").value : "04") : "04";
  const endMonth = isBatch ? (document.getElementById("portalEndMonth") ? document.getElementById("portalEndMonth").value : "10") : "10";
  const returnPeriod = document.getElementById("portalPeriod").value;
  const returnType = document.getElementById("portalReturnType") ? document.getElementById("portalReturnType").value : "GSTR-2B";
  const username = document.getElementById("portalUsername").value.trim();
  const password = document.getElementById("portalPassword").value;
  const saveCreds = document.getElementById("portalSaveCreds").checked;

  if (!username && !simulate) {
    alert("Please enter your GST Portal username.");
    return;
  }

  // Save credentials locally if checked
  if (saveCreds && username && password && !simulate) {
    fetch("/api/portal/save-profile", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password })
    }).catch(console.error);
  }

  // Show progress tracker
  const progressBox = document.getElementById("portalProgressBox");
  progressBox.style.display = "block";

  // Update step labels based on batch vs single
  const p3 = document.getElementById("pStep3");
  const p4 = document.getElementById("pStep4");
  if (isBatch) {
    if (p3) p3.innerHTML = `<span class="step-dot"></span> 3. Authenticated! Sequentially downloading 7 months (April to October)...`;
    if (p4) p4.innerHTML = `<span class="step-dot"></span> 4. All monthly statements downloaded & consolidated!`;
  } else {
    if (p3) p3.innerHTML = `<span class="step-dot"></span> 3. Authenticated! Navigating legitimately to Return Dashboard...`;
    if (p4) p4.innerHTML = `<span class="step-dot"></span> 4. GSTR-2B downloaded & auto-ingested into ledger!`;
  }

  const setStep = (stepId, state) => {
    const el = document.getElementById(stepId);
    if (el) el.className = `progress-step-item ${state}`;
  };

  setStep("pStep1", "active");
  if (!simulate) {
    setStep("pStep2", "active");
  }

  try {
    const data = await safeFetchJSON("/api/portal/fetch-2b", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        financial_year: fy,
        batch_mode: isBatch,
        start_month: startMonth,
        end_month: endMonth,
        return_period: returnPeriod,
        return_type: returnType,
        username,
        password,
        simulate
      })
    });

    setStep("pStep1", "done");
    setStep("pStep2", "done");
    setStep("pStep3", "done");
    setStep("pStep4", "done");

    // Convert preview data to CSV blob and attach directly as file2B input
    if (data.data_preview && data.data_preview.length > 0) {
      const outputFilename = isBatch
        ? `GSTR2B_Consolidated_Apr_Oct_${fy}.csv`
        : `${returnType}_${returnPeriod}.csv`;
      attachDataAsFile2B(data.data_preview, outputFilename, data.rows_count);
    }

    setTimeout(() => {
      closePortalModal();
      const msg = isBatch
        ? `Success: Consolidated ${data.rows_count} invoices across statutory months (April to October, FY ${fy})!`
        : `Success: ${returnType} Excel statement (${data.rows_count} invoices) captured from GST portal!`;
      alert(msg);
    }, 1000);

  } catch (err) {
    alert("Portal Automation Notice: " + (err.message || "Session ended before file download completed."));
  }
}

function attachDataAsFile2B(records, filename, rowCount) {
  const safeName = filename.replace(/\.xlsx?$|\.zip$/i, "") + ".csv";
  const csvContent = convertJSONToCSV(records);
  const blob = new Blob([csvContent], { type: "text/csv" });
  const file = new File([blob], safeName, { type: "text/csv" });

  const dt = new DataTransfer();
  dt.items.add(file);
  const inputEl = document.getElementById("file2B");
  if (inputEl) inputEl.files = dt.files;

  const label = document.getElementById("fileName2B");
  const zone = document.getElementById("dropZone2B");
  if (!label || !zone) return;

  label.innerHTML = `
    <div style="display:flex; align-items:center; justify-content:space-between; width:100%; gap:8px;">
      <span style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap;"><span style="color:#059669; font-weight:600;">Ingested:</span> ${filename} (${rowCount} Invoices)</span>
      <button type="button" class="btn-clear-attachment" title="Remove attached statement" style="background:#fee2e2; color:#b91c1c; border:none; border-radius:3px; padding:2px 6px; font-size:0.7rem; cursor:pointer; font-weight:700;">Remove</button>
    </div>
  `;
  const clearBtn = label.querySelector(".btn-clear-attachment");
  if (clearBtn) {
    clearBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      if (inputEl) inputEl.value = "";
      label.textContent = "No file attached";
      zone.classList.remove("has-file");
    });
  }
  zone.classList.add("has-file");
}

function attachDataAsFileBooks(records, filename, rowCount) {
  const safeName = filename.replace(/\.xlsx?$|\.zip$/i, "") + ".csv";
  const csvContent = convertJSONToCSV(records);
  const blob = new Blob([csvContent], { type: "text/csv" });
  const file = new File([blob], safeName, { type: "text/csv" });

  const dt = new DataTransfer();
  dt.items.add(file);
  const inputEl = document.getElementById("fileBooks");
  if (inputEl) inputEl.files = dt.files;

  const label = document.getElementById("fileNameBooks");
  const zone = document.getElementById("dropZoneBooks");
  if (!label || !zone) return;

  label.innerHTML = `
    <div style="display:flex; align-items:center; justify-content:space-between; width:100%; gap:8px;">
      <span style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap;"><span style="color:#059669; font-weight:600;">Tally Synced:</span> ${filename} (${rowCount} Vouchers)</span>
      <button type="button" class="btn-clear-attachment" title="Remove attached ledger" style="background:#fee2e2; color:#b91c1c; border:none; border-radius:3px; padding:2px 6px; font-size:0.7rem; cursor:pointer; font-weight:700;">Remove</button>
    </div>
  `;
  const clearBtn = label.querySelector(".btn-clear-attachment");
  if (clearBtn) {
    clearBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      if (inputEl) inputEl.value = "";
      label.textContent = "No file attached";
      zone.classList.remove("has-file");
    });
  }
  zone.classList.add("has-file");
}

function convertJSONToCSV(items) {
  if (!items || !items.length) return "";
  const header = Object.keys(items[0]);
  const rows = items.map(item =>
    header.map(fieldName => JSON.stringify(item[fieldName] ?? "")).join(",")
  );
  return [header.join(","), ...rows].join("\r\n");
}

// -------------------------------------------------------------
// Downloads Folder Scanner
// -------------------------------------------------------------

function openDownloadsModal() {
  const modal = document.getElementById("downloadsModal");
  modal.style.display = "flex";
  scanRecentDownloadsFolder();
}

function closeDownloadsModal() {
  const m = document.getElementById("downloadsModal");
  if (m) m.style.display = "none";
}

async function scanRecentDownloadsFolder() {
  const scanningEl = document.getElementById("downloadsScanningState");
  const listEl = document.getElementById("downloadsListContainer");
  const emptyEl = document.getElementById("downloadsEmptyState");

  scanningEl.style.display = "block";
  listEl.style.display = "none";
  emptyEl.style.display = "none";

  try {
    const res = await fetch("/api/portal/scan-downloads");
    const data = await res.json();
    scanningEl.style.display = "none";

    const matches = data.matches || [];
    currentDownloadsMatches = matches;
    const btnAttachAll = document.getElementById("btnAttachAllDownloads");
    if (btnAttachAll) {
      btnAttachAll.style.display = matches.length > 1 ? "inline-flex" : "none";
      btnAttachAll.textContent = `Attach All (${matches.length} Statements)`;
    }

    if (matches.length === 0) {
      emptyEl.style.display = "block";
      return;
    }

    listEl.innerHTML = "";
    matches.forEach(m => {
      const isExcel = m.is_excel !== false && !m.file_name.toLowerCase().endsWith(".json");
      const badge = isExcel
        ? `<span style="background:#ecfdf5; color:#059669; font-weight:700; font-size:10px; padding:2px 6px; border-radius:4px; border:1px solid rgba(16,185,129,0.3); margin-left:6px;">EXCEL</span>`
        : `<span style="background:#f1f5f9; color:#475569; font-weight:600; font-size:10px; padding:2px 6px; border-radius:4px; margin-left:6px;">JSON</span>`;

      const row = document.createElement("div");
      row.className = "downloads-item-row";
      row.style.cssText = "display:flex; justify-content:space-between; align-items:center; padding:12px; border-bottom:1px solid var(--border-light); gap:12px;";
      row.innerHTML = `
        <div style="flex:1; min-width:0;">
          <div style="font-weight:600; font-size:0.85rem; color:var(--text-primary); display:flex; align-items:center; gap:6px;">
            <span style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${m.file_name}">${m.file_name}</span>
            ${badge}
          </div>
          <div style="font-size:0.75rem; color:var(--text-secondary); margin-top:2px;">${m.size_kb} KB • Modified ${m.modified_date}</div>
        </div>
        <div style="display:flex; align-items:center; gap:8px; flex-shrink:0;">
          <button type="button" class="btn-ui btn-primary btn-attach" style="padding:6px 12px; font-size:0.75rem;">
            Attach
          </button>
          <button type="button" class="btn-ui btn-delete" title="Delete file from downloads folder" style="padding:6px 10px; font-size:0.75rem; background:#fee2e2; color:#b91c1c; border:1px solid #fca5a5; display:inline-flex; align-items:center; gap:4px; cursor:pointer;">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
              <polyline points="3 6 5 6 21 6"/>
              <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
              <line x1="10" y1="11" x2="10" y2="17"/>
              <line x1="14" y1="11" x2="14" y2="17"/>
            </svg>
            <span>Delete</span>
          </button>
        </div>
      `;
      row.querySelector(".btn-attach").addEventListener("click", () => attachDownloadedFile(m.file_path, m.file_name));
      row.querySelector(".btn-delete").addEventListener("click", () => deleteDownloadedFile(m.file_path, m.file_name));
      listEl.appendChild(row);
    });

    listEl.style.display = "block";
  } catch (err) {
    scanningEl.style.display = "none";
    emptyEl.style.display = "block";
    emptyEl.innerHTML = `<div style="color:#ef4444; font-weight:600;">Scan Failed: ${err.message}</div>`;
  }
}

async function deleteDownloadedFile(filePath, fileName) {
  const confirmed = confirm(`Are you sure you want to permanently delete "${fileName}" from your downloads folder?`);
  if (!confirmed) return;

  try {
    const data = await safeFetchJSON("/api/portal/delete-download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file_path: filePath })
    });
    alert(`File deleted: ${fileName}`);
    scanRecentDownloadsFolder();
  } catch (err) {
    alert("Delete Failed: " + err.message);
  }
}

async function attachDownloadedFile(filePath, fileName) {
  try {
    const data = await safeFetchJSON("/api/portal/ingest-local", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file_path: filePath })
    });
    attachDataAsFile2B(data.data_preview, fileName, data.rows_count);
    closeDownloadsModal();
    alert(`Attached: ${fileName} (${data.rows_count} invoices) loaded from Downloads.`);
  } catch (err) {
    alert("Attachment Error: " + err.message);
  }
}

async function attachAllScannedStatements() {
  if (!currentDownloadsMatches || currentDownloadsMatches.length === 0) return;
  const fy = document.getElementById("portalFY") ? document.getElementById("portalFY").value : "2026-27";
  const btn = document.getElementById("btnAttachAllDownloads");
  const origText = btn ? btn.textContent : "";
  if (btn) btn.textContent = "Consolidating...";

  try {
    const data = await safeFetchJSON("/api/portal/ingest-batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        file_paths: currentDownloadsMatches.map(m => m.file_path),
        financial_year: fy
      })
    });
    attachDataAsFile2B(data.data_preview, `GSTR2B_Consolidated_YTD_${fy}.csv`, data.rows_count);
    closeDownloadsModal();
    alert(`Success: Consolidated ${data.rows_count} invoices across ${currentDownloadsMatches.length} statutory statements!`);
  } catch (err) {
    alert("Batch Attachment Notice: " + err.message);
  } finally {
    if (btn) btn.textContent = origText;
  }
}

// -------------------------------------------------------------
// Tally Prime Direct Synchronization (Port 9000)
// -------------------------------------------------------------

function openTallyModal() {
  const modal = document.getElementById("tallyModal");
  if (modal) modal.style.display = "flex";
  checkTallyStatus();
}

function closeTallyModal() {
  const modal = document.getElementById("tallyModal");
  if (modal) modal.style.display = "none";
}

async function checkTallyStatus() {
  const textEl = document.getElementById("tallyStatusText");
  const badgeEl = document.getElementById("tallyStatusBadge");

  if (textEl) textEl.textContent = "Probing http://localhost:9000...";
  if (badgeEl) {
    badgeEl.className = "tally-status-badge inactive";
    badgeEl.textContent = "Probing...";
  }

  try {
    const res = await fetch("/api/tally/status");
    const data = await res.json();
    if (data.active) {
      if (textEl) textEl.textContent = `${data.message} (${data.company})`;
      if (badgeEl) {
        badgeEl.className = "tally-status-badge active";
        badgeEl.textContent = "Online • Port 9000";
      }
    } else {
      if (textEl) textEl.textContent = "Tally Prime XML Server is offline or not running.";
      if (badgeEl) {
        badgeEl.className = "tally-status-badge inactive";
        badgeEl.textContent = "Offline (Port 9000)";
      }
    }
  } catch (err) {
    if (textEl) textEl.textContent = "Could not connect to local Tally port.";
    if (badgeEl) {
      badgeEl.className = "tally-status-badge inactive";
      badgeEl.textContent = "Offline";
    }
  }
}

async function syncTallyVouchers(simulate = false) {
  const btn = simulate ? document.getElementById("btnSimulateTally") : document.getElementById("btnSyncTallyLive");
  const origText = btn ? btn.innerHTML : "";
  if (btn) btn.innerHTML = `<span>Syncing...</span>`;

  try {
    const data = await safeFetchJSON("/api/tally/sync", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ simulate })
    });

    attachDataAsFileBooks(data.data_preview, simulate ? "Tally_Purchase_Register_Demo.csv" : "Tally_Live_Purchases.csv", data.rows_count);
    closeTallyModal();
    alert(`Success: ${data.message} (${data.rows_count} vouchers loaded into internal ledger)`);
  } catch (err) {
    alert("Tally Sync Notice: " + err.message);
  } finally {
    if (btn) btn.innerHTML = origText;
  }
}

// -------------------------------------------------------------
// Core Reconciliation Handler
// -------------------------------------------------------------

async function handleReconcile() {
  const input2B = document.getElementById("file2B");
  const inputBooks = document.getElementById("fileBooks");
  const inputTol = document.getElementById("toleranceInput");

  const files2B = input2B ? input2B.files : null;
  const fileBooks = (inputBooks && inputBooks.files) ? inputBooks.files[0] : null;
  const tolerance = inputTol ? (parseFloat(inputTol.value) || 1.00) : 1.00;

  if (!files2B || files2B.length === 0 || !fileBooks) {
    alert("Please attach both the GSTR-2B statement(s) and the Purchase Register (Books) file.");
    return;
  }

  const btn = document.getElementById("btnReconcile");
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = `<span style="display:inline-block; animation:spin 1s linear infinite;">•</span> Auditing Invoices...`;
  }

  const formData = new FormData();
  for (let i = 0; i < files2B.length; i++) {
    formData.append("file_2b", files2B[i]);
  }
  formData.append("file_books", fileBooks);
  formData.append("tolerance", tolerance);

  try {
    currentReconData = await safeFetchJSON("/api/reconcile", {
      method: "POST",
      body: formData
    });
    updateUIWithReconData(currentReconData);
  } catch (err) {
    alert("Audit Notice: " + err.message);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = `
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <polygon points="5 3 19 12 5 21 5 3"/>
        </svg>
        <span>Run Reconciliation</span>
      `;
    }
  }
}

async function handleLoadSample() {
  const btn = document.getElementById("btnLoadSample");
  if (btn) btn.disabled = true;
  const origText = btn ? btn.innerHTML : "";
  if (btn) btn.innerHTML = `<span>Loading Audit Data...</span>`;

  try {
    currentReconData = await safeFetchJSON("/api/load-sample?tolerance=1.00");
    updateUIWithReconData(currentReconData);
  } catch (err) {
    alert("Demo Load Failed: " + err.message);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = origText;
    }
  }
}

function setText(id, text) {
  try {
    const el = document.getElementById(id);
    if (el) el.textContent = (text !== null && text !== undefined) ? String(text) : "0";
  } catch (_) {}
}

function updateUIWithReconData(data) {
  if (!data) return;
  if (!currentReconData) currentReconData = data;

  // Synthesize flat ledger from results if needed
  if (!currentReconData.ledger && currentReconData.results) {
    currentReconData.ledger = currentReconData.results.map(r => {
      const b = r.books || {};
      const p = r.portal || {};
      const status = r.status || "";
      return {
        match_category: status === "PROBABLE_TYPO" ? "TYPO_MATCH" : status,
        status: status,
        status_label: r.status_label || "",
        supplier_name_2b: p.party_name || "",
        supplier_name_books: b.party_name || "",
        gstin_2b: p.gstin || "",
        gstin_books: b.gstin || "",
        inv_num_2b: p.raw_inv || "",
        inv_num_books: b.raw_inv || "",
        inv_date_books: b.inv_date || "",
        inv_date_2b: p.inv_date || "",
        taxable_books: b.taxable !== undefined ? b.taxable : null,
        tax_amount_books: b.total_tax !== undefined ? b.total_tax : null,
        taxable_2b: p.taxable !== undefined ? p.taxable : null,
        tax_amount_2b: p.total_tax !== undefined ? p.total_tax : null,
        tax_diff: r.tax_diff || 0.0,
        taxable_diff: r.taxable_diff || 0.0,
        audit_remark: r.remarks || ""
      };
    });
  }

  // Fully guarded fallback supporting both data.kpis and data.summary
  const sum = (data && data.summary) ? data.summary : {};
  const kpis = (data && data.kpis) ? data.kpis : {
    total_tax_books: sum.books_tax_total ?? 0,
    total_invoices_books: sum.total_books_invoices ?? 0,
    total_tax_2b: sum.portal_tax_total ?? 0,
    total_invoices_2b: sum.total_2b_invoices ?? 0,
    matched_itc: sum.matched_tax ?? 0,
    matched_count: sum.matched_count ?? 0,
    reconciliation_percentage: sum.reconciliation_rate ?? 0,
    itc_at_risk: sum.itc_at_risk_tax ?? 0,
    itc_at_risk_count: sum.missing_in_2b_count ?? 0,
    unbooked_itc: sum.unbooked_tax ?? 0,
    unbooked_count: sum.missing_in_books_count ?? 0,
    category_counts: {
      EXACT_MATCH: sum.exact_count ?? 0,
      NORMALIZED_MATCH: sum.normalized_count ?? 0,
      TYPO_MATCH: sum.typo_count ?? 0,
      PROBABLE_TYPO: sum.typo_count ?? 0,
      VALUE_MISMATCH: sum.value_mismatch_count ?? 0,
      MISSING_IN_2B: sum.missing_in_2b_count ?? 0,
      MISSING_IN_BOOKS: sum.missing_in_books_count ?? 0
    }
  };

  // Render KPI Stat Cards (matching index.html element IDs)
  setText("kpiBooksTax", formatINR(kpis.total_tax_books ?? 0));
  setText("kpiBooksCount", `${kpis.total_invoices_books ?? 0} Invoices in Ledger`);

  setText("kpiPortalTax", formatINR(kpis.total_tax_2b ?? 0));
  setText("kpiPortalCount", `${kpis.total_invoices_2b ?? 0} Invoices on Portal`);

  setText("kpiMatchedTax", formatINR(kpis.matched_itc ?? 0));
  setText("kpiMatchRate", `${kpis.reconciliation_percentage ?? 0}% Match Efficiency`);

  setText("kpiRiskTax", formatINR(kpis.itc_at_risk ?? 0));
  setText("kpiRiskCount", `${kpis.itc_at_risk_count ?? 0} Invoices Missing in 2B`);

  setText("kpiUnbookedTax", formatINR(kpis.unbooked_itc ?? 0));
  setText("kpiUnbookedCount", `${kpis.unbooked_count ?? 0} Unclaimed Portal Bills`);

  // Render Category Breakdown Badges (matching index.html tab counts)
  const cats = kpis.category_counts || {};
  const totalLedger = (currentReconData && currentReconData.ledger) ? currentReconData.ledger.length : 0;
  setText("countAll", totalLedger);
  setText("countExact", cats.EXACT_MATCH ?? 0);
  setText("countNorm", cats.NORMALIZED_MATCH ?? 0);
  setText("countTypo", (cats.TYPO_MATCH ?? cats.PROBABLE_TYPO ?? 0));
  setText("countMismatch", cats.VALUE_MISMATCH ?? 0);
  setText("countRisk", cats.MISSING_IN_2B ?? 0);
  setText("countUnbooked", cats.MISSING_IN_BOOKS ?? 0);

  // Reveal Report Sections
  const resSection = document.getElementById("resultsSection");
  if (resSection) {
    resSection.style.display = "block";
    try { resSection.scrollIntoView({ behavior: "smooth" }); } catch (_) {}
  }

  renderTable();
}

function renderTable() {
  if (!currentReconData) return;

  if (!currentReconData.ledger && currentReconData.results) {
    currentReconData.ledger = currentReconData.results.map(r => {
      const b = r.books || {};
      const p = r.portal || {};
      const status = r.status || "";
      return {
        match_category: status === "PROBABLE_TYPO" ? "TYPO_MATCH" : status,
        status: status,
        status_label: r.status_label || "",
        supplier_name_2b: p.party_name || "",
        supplier_name_books: b.party_name || "",
        gstin_2b: p.gstin || "",
        gstin_books: b.gstin || "",
        inv_num_2b: p.raw_inv || "",
        inv_num_books: b.raw_inv || "",
        inv_date_books: b.inv_date || "",
        inv_date_2b: p.inv_date || "",
        taxable_books: b.taxable !== undefined ? b.taxable : null,
        tax_amount_books: b.total_tax !== undefined ? b.total_tax : null,
        taxable_2b: p.taxable !== undefined ? p.taxable : null,
        tax_amount_2b: p.total_tax !== undefined ? p.total_tax : null,
        tax_diff: r.tax_diff || 0.0,
        taxable_diff: r.taxable_diff || 0.0,
        audit_remark: r.remarks || ""
      };
    });
  }

  if (!currentReconData.ledger) return;

  const tbody = document.getElementById("reconTableBody");
  if (!tbody) return;
  tbody.innerHTML = "";

  const records = currentReconData.ledger.filter(row => {
    // Filter pill matching
    let matchesCategory = true;
    if (currentFilter === "MATCHED") {
      matchesCategory = ["EXACT_MATCH", "NORMALIZED_MATCH", "TYPO_MATCH", "PROBABLE_TYPO"].includes(row.match_category);
    } else if (currentFilter !== "ALL") {
      matchesCategory = (row.match_category === currentFilter) ||
                        (currentFilter === "PROBABLE_TYPO" && (row.match_category === "TYPO_MATCH" || row.status === "PROBABLE_TYPO"));
    }

    if (!matchesCategory) return false;

    // Search query matching
    if (!searchQuery) return true;
    const searchString = `
      ${row.supplier_name_2b || ""}
      ${row.supplier_name_books || ""}
      ${row.gstin_2b || ""}
      ${row.gstin_books || ""}
      ${row.inv_num_2b || ""}
      ${row.inv_num_books || ""}
      ${row.audit_remark || ""}
    `.toLowerCase();

    return searchString.includes(searchQuery);
  });

  const countLabel = document.getElementById("tableCountLabel");
  if (countLabel) {
    const totalCount = (currentReconData && currentReconData.ledger) ? currentReconData.ledger.length : 0;
    countLabel.textContent = `Showing ${records.length} of ${totalCount} invoices`;
  }

  if (records.length === 0) {
    tbody.innerHTML = `
      <tr>
        <td colspan="12" style="text-align: center; padding: 48px; color: var(--text-tertiary);">
          No reconciliation records match the selected filter or search term.
        </td>
      </tr>
    `;
    return;
  }

  records.forEach(row => {
    const tr = document.createElement("tr");

    // Match Category styling & full row classification
    let rowClass = "recon-row-exact";
    let pillClass = "status-pill-exact";
    let badgeLabel = "Exact Match";

    switch (row.match_category) {
      case "NORMALIZED_MATCH":
        rowClass = "recon-row-norm";
        pillClass = "status-pill-norm";
        badgeLabel = "Format Resolved";
        break;
      case "TYPO_MATCH":
      case "PROBABLE_TYPO":
        rowClass = "recon-row-typo";
        pillClass = "status-pill-typo";
        badgeLabel = "Typo Match";
        break;
      case "VALUE_MISMATCH":
        rowClass = "recon-row-mismatch";
        pillClass = "status-pill-mismatch";
        badgeLabel = "Value Mismatch";
        break;
      case "MISSING_IN_2B":
        rowClass = "recon-row-risk";
        pillClass = "status-pill-risk";
        badgeLabel = "Missing in 2B (At Risk)";
        break;
      case "MISSING_IN_BOOKS":
        rowClass = "recon-row-unbooked";
        pillClass = "status-pill-unbooked";
        badgeLabel = "Unbooked in Books";
        break;
    }

    tr.className = `recon-row ${rowClass}`;

    const cptyName = row.supplier_name_2b || row.supplier_name_books || "Unknown Vendor";
    const cptyGstin = row.gstin_2b || row.gstin_books || "—";

    const booksTaxable = (row.taxable_books !== null && row.taxable_books !== undefined) ? formatINR(row.taxable_books) : "—";
    const booksTax = (row.tax_amount_books !== null && row.tax_amount_books !== undefined) ? formatINR(row.tax_amount_books) : "—";
    const rawBooksInv = row.inv_num_books || "—";
    const booksDate = row.inv_date_books || "—";

    const portalTaxable = (row.taxable_2b !== null && row.taxable_2b !== undefined) ? formatINR(row.taxable_2b) : "—";
    const portalTax = (row.tax_amount_2b !== null && row.tax_amount_2b !== undefined) ? formatINR(row.tax_amount_2b) : "—";
    const rawPortalInv = row.inv_num_2b || "—";
    const portalDate = row.inv_date_2b || "—";

    // Highlight invoice differences for Typo Match, or render missing badges
    let booksInvDisplay = `<span class="code-font">${rawBooksInv}</span>`;
    let portalInvDisplay = `<span class="code-font">${rawPortalInv}</span>`;

    if (row.match_category === "TYPO_MATCH" || row.match_category === "PROBABLE_TYPO") {
      booksInvDisplay = `<span class="code-font diff-highlight" title="Books Invoice No">${rawBooksInv}</span>`;
      portalInvDisplay = `<span class="code-font diff-highlight" title="Portal Invoice No">${rawPortalInv}</span>`;
    } else if (row.match_category === "MISSING_IN_2B") {
      portalInvDisplay = `<span style="color:#ef4444; font-weight:600; font-size:0.75rem; background:rgba(239,68,68,0.12); padding:2px 8px; border-radius:4px;">Not in GSTR-2B</span>`;
    } else if (row.match_category === "MISSING_IN_BOOKS") {
      booksInvDisplay = `<span style="color:#0284c7; font-style:italic; font-size:0.75rem; background:rgba(2,132,199,0.1); padding:2px 8px; border-radius:4px;">Not in Books</span>`;
    }

    const taxDiff = (row.tax_diff !== null && row.tax_diff !== undefined && Math.abs(row.tax_diff) > 0.001)
      ? `<span style="font-weight:700; color:var(--status-mismatch-text);">${formatINR(row.tax_diff)}</span>`
      : `<span style="color:var(--text-tertiary);">₹0.00</span>`;

    tr.innerHTML = `
      <td>
        <span class="status-pill ${pillClass}">
          <span class="dot"></span>
          <span>${badgeLabel}</span>
        </span>
      </td>
      <td>
        <div class="cpty-name" style="font-weight:600; color:var(--text-primary);">${cptyName}</div>
        <div class="cpty-gstin" style="font-size:0.75rem; color:var(--text-secondary); font-family:var(--font-mono);">${cptyGstin}</div>
      </td>
      <td>${booksInvDisplay}</td>
      <td>${booksDate}</td>
      <td class="num-cell" style="font-weight: 500;">${booksTaxable}</td>
      <td class="num-cell" style="font-weight: 600;">${booksTax}</td>
      <td>${portalInvDisplay}</td>
      <td>${portalDate}</td>
      <td class="num-cell" style="font-weight: 500;">${portalTaxable}</td>
      <td class="num-cell" style="font-weight: 600;">${portalTax}</td>
      <td class="num-cell">${taxDiff}</td>
      <td class="audit-remark">${row.audit_remark || ""}</td>
    `;

    tbody.appendChild(tr);
  });
}

async function handleExportExcel() {
  if (!currentReconData) {
    alert("No active reconciliation data to export. Please run an audit or load sample data first.");
    return;
  }

  const btn = document.getElementById("btnExportExcel");
  const origText = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = `<span>Exporting OpenPyXL Workbook...</span>`;

  try {
    const res = await fetch("/api/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(currentReconData)
    });

    if (!res.ok) throw new Error("Excel export failed on server.");

    const blob = await res.blob();
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `GST_Reconciliation_Report_${new Date().toISOString().split("T")[0]}.xlsx`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    window.URL.revokeObjectURL(url);
  } catch (err) {
    alert("Export Error: " + err.message);
  } finally {
    btn.disabled = false;
    btn.innerHTML = origText;
  }
}
