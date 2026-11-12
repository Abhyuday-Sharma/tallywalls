/**
 * GST Intelligence & Reconciliation Hub Frontend Engine (v2.4.0).
 * Hybrid Cloud + Local Companion Connector Architecture:
 * - Parses URL hash pairing tokens (#bridge_token=...&bridge_port=...) securely
 * - Auto-discovers local TallyWalls Connector on 127.0.0.1:9455-9459
 * - Routes Live GST Portal automation (async job polling), Downloads scanner,
 *   encrypted keystore, and Tally Port 9000 sync to local bridge in cloud mode
 * - Performs RFC-4180 CSV serialization, slimmed export payloads, and clear UI loading states
 */

let currentReconData = null;
let currentFilter = "ALL";
let searchQuery = "";
let currentPortalMode = "batch";
let currentDownloadsMatches = [];

const BRIDGE_PORTS = [9455, 9456, 9457, 9458, 9459];
const LS_BRIDGE_TOKEN = "tw_bridge_token";
const LS_BRIDGE_PORT = "tw_bridge_port";

const bridgeState = {
  mode: "detecting", // "local_desktop" | "cloud_with_bridge" | "cloud_web_only" | "detecting"
  isCloudBackend: false,
  connected: false,
  paired: false,
  port: null,
  token: "",
  tallyActive: false,
  tallyCompany: "",
};

function escHtml(str) {
  if (str === null || str === undefined) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

// Format Indian Currency (Lakhs & Crores with 2 decimals)
function formatINR(val) {
  if (val === null || val === undefined || isNaN(val)) return "—";
  const num = Number(val);
  return "₹" + num.toLocaleString("en-IN", {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
  });
}

document.addEventListener("DOMContentLoaded", async () => {
  consumeBridgeHashParams();
  setupDragAndDrop();
  initFinancialYears();
  setupEventListeners();
  await detectEnvironment();
});

window.addEventListener("hashchange", async () => {
  const hadHashToken = (window.location.hash || "").includes("bridge_token=");
  if (hadHashToken) {
    consumeBridgeHashParams();
    if (bridgeState.isCloudBackend) {
      await probeBridge();
    }
  }
});

// -------------------------------------------------------------
// Bridge Pairing & Environment Detection
// -------------------------------------------------------------

function consumeBridgeHashParams() {
  try {
    const storedToken = localStorage.getItem(LS_BRIDGE_TOKEN) || "";
    const storedPort = parseInt(localStorage.getItem(LS_BRIDGE_PORT) || "", 10);
    if (storedToken) bridgeState.token = storedToken;
    if (!isNaN(storedPort) && storedPort > 0) bridgeState.port = storedPort;

    const hash = window.location.hash || "";
    if (hash.includes("bridge_token=")) {
      const params = new URLSearchParams(hash.replace(/^#/, ""));
      const tok = params.get("bridge_token");
      const prt = parseInt(params.get("bridge_port") || "", 10);
      if (tok) {
        bridgeState.token = tok;
        localStorage.setItem(LS_BRIDGE_TOKEN, tok);
      }
      if (!isNaN(prt) && prt > 0) {
        bridgeState.port = prt;
        localStorage.setItem(LS_BRIDGE_PORT, String(prt));
      }
      // Clean URL hash immediately so token does not linger in browser history
      history.replaceState(null, "", window.location.pathname + window.location.search);
    }
  } catch (e) {
    console.warn("Hash pairing parse note:", e);
  }
}

async function fetchWithTimeout(url, options = {}, timeoutMs = 1500) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...options, signal: controller.signal });
  } finally {
    clearTimeout(timer);
  }
}

async function detectEnvironment() {
  let backendEnv = null;
  try {
    const res = await fetchWithTimeout("/api/health", {}, 2500);
    if (res.ok) {
      const info = await res.json();
      backendEnv = info.environment || null;
    }
  } catch (_) {}

  const isLocalHost = location.hostname === "localhost" || location.hostname === "127.0.0.1";
  const isCloud = backendEnv ? (backendEnv === "cloud") : !isLocalHost;
  bridgeState.isCloudBackend = isCloud;

  if (!isCloud) {
    bridgeState.mode = "local_desktop";
    updateEnvironmentUI();
    return;
  }

  await probeBridge();
}

async function probeBridge() {
  if (!bridgeState.isCloudBackend) {
    bridgeState.mode = "local_desktop";
    updateEnvironmentUI();
    return bridgeState;
  }

  const candidatePorts = [];
  if (bridgeState.port && BRIDGE_PORTS.includes(bridgeState.port)) {
    candidatePorts.push(bridgeState.port);
  }
  BRIDGE_PORTS.forEach(p => {
    if (!candidatePorts.includes(p)) candidatePorts.push(p);
  });

  bridgeState.connected = false;
  bridgeState.paired = false;
  bridgeState.tallyActive = false;
  bridgeState.tallyCompany = "";

  for (const port of candidatePorts) {
    try {
      const res = await fetchWithTimeout(`http://127.0.0.1:${port}/bridge/health`, {}, 1500);
      if (res.ok) {
        const data = await res.json();
        if (data && data.service === "TallyWalls-Connector") {
          bridgeState.connected = true;
          bridgeState.port = port;
          bridgeState.tallyActive = Boolean(data.tally_active);
          bridgeState.tallyCompany = data.tally_company || "";
          localStorage.setItem(LS_BRIDGE_PORT, String(port));
          break;
        }
      }
    } catch (_) {
      // Port closed or PNA declined; try next port
    }
  }

  // Validate saved pairing token against authenticated profile endpoint
  if (bridgeState.connected && bridgeState.token) {
    try {
      const verifyRes = await fetchWithTimeout(
        `http://127.0.0.1:${bridgeState.port}/bridge/portal/profile`,
        { headers: { "X-Bridge-Token": bridgeState.token } },
        1200
      );
      if (verifyRes.ok) {
        bridgeState.paired = true;
      } else if (verifyRes.status === 401) {
        bridgeState.paired = false;
      }
    } catch (_) {
      bridgeState.paired = false;
    }
  }

  bridgeState.mode = (bridgeState.connected && bridgeState.paired)
    ? "cloud_with_bridge"
    : "cloud_web_only";

  updateEnvironmentUI();
  return bridgeState;
}

function updateEnvironmentUI() {
  const badge = document.getElementById("engineStatusBadge");
  const label = document.getElementById("engineStatusLabel");
  const envNotice = document.getElementById("portalEnvNotice");
  const tallyEnvNotice = document.getElementById("tallyEnvNotice");
  const dlEnvNotice = document.getElementById("downloadsEnvNotice");
  const btnLaunch = document.getElementById("btnLaunchBridge");
  const btnSyncTallyLive = document.getElementById("btnSyncTallyLive");
  const btnDisconnect = document.getElementById("btnDisconnectConnector");
  const connBanner = document.getElementById("connectorSummaryBanner");

  if (badge && label) {
    badge.classList.remove("web-offline", "bridge-connected");
    if (bridgeState.mode === "local_desktop") {
      label.textContent = "Local Engine Active";
      badge.title = "Running directly on local Windows desktop FastAPI engine.";
    } else if (bridgeState.mode === "cloud_with_bridge") {
      badge.classList.add("bridge-connected");
      label.textContent = `Desktop Bridge Connected (Port ${bridgeState.port})`;
      badge.title = "Paired with local TallyWalls Connector. Click to manage pairing.";
    } else if (bridgeState.connected && !bridgeState.paired) {
      badge.classList.add("web-offline");
      label.textContent = "Connector Detected (Enter PIN)";
      badge.title = "TallyWalls Connector found on localhost. Click to enter 6-digit PIN.";
    } else {
      badge.classList.add("web-offline");
      label.textContent = "Web Mode (Connector Offline)";
      badge.title = "Click to connect TallyWalls Local Connector for live GST Portal & Tally sync.";
    }
  }

  const webOnlyNoticeHtml = `
    <div style="background:#fffbeb; border:1px solid #fcd34d; border-radius:6px; padding:10px 12px; color:#92400e; font-size:0.78rem; margin-bottom:12px; line-height:1.45;">
      <strong>TallyWalls Connector Required for Live Sync:</strong>
      Cloud servers cannot open a browser on your desktop or reach <code>localhost:9000</code>.
      Start <strong>TallyWallsConnector.exe</strong> on your Windows PC, or use <strong>Demo / Manual Upload</strong>.
      <div style="margin-top:6px; display:flex; gap:8px; flex-wrap:wrap; align-items:center;">
        <button type="button" onclick="openConnectorModal();" class="btn-ui btn-accent" style="padding:4px 10px; font-size:0.74rem;">
          Setup / Pair Local Connector
        </button>
        <a href="/connector" target="_blank" rel="noopener noreferrer" style="font-size:0.74rem; font-weight:600; color:#b45309; text-decoration:underline;">
          Download &amp; Setup Page
        </a>
      </div>
    </div>
  `;

  const bridgeConnectedHtml = `
    <div style="background:#ecfdf5; border:1px solid #a7f3d0; border-radius:6px; padding:8px 12px; color:#065f46; font-size:0.76rem; margin-bottom:12px; line-height:1.4;">
      <strong>Desktop Bridge Active (Port ${bridgeState.port}):</strong>
      Commands execute securely on your Windows PC; credentials and files stay local.
    </div>
  `;

  if (envNotice) {
    if (bridgeState.mode === "cloud_with_bridge") envNotice.innerHTML = bridgeConnectedHtml;
    else if (bridgeState.mode === "cloud_web_only") envNotice.innerHTML = webOnlyNoticeHtml;
    else envNotice.innerHTML = "";
  }

  if (tallyEnvNotice) {
    if (bridgeState.mode === "cloud_with_bridge") tallyEnvNotice.innerHTML = bridgeConnectedHtml;
    else if (bridgeState.mode === "cloud_web_only") tallyEnvNotice.innerHTML = webOnlyNoticeHtml;
    else tallyEnvNotice.innerHTML = "";
  }

  if (dlEnvNotice) {
    if (bridgeState.mode === "cloud_web_only") dlEnvNotice.innerHTML = webOnlyNoticeHtml;
    else dlEnvNotice.innerHTML = "";
  }

  // Update Live Action buttons visually
  if (btnLaunch) {
    const span = btnLaunch.querySelector("span");
    if (bridgeState.mode === "cloud_web_only") {
      btnLaunch.classList.add("btn-connector-needed");
      if (span) span.textContent = "Connect PC Required (Setup Connector)";
    } else {
      btnLaunch.classList.remove("btn-connector-needed");
      if (span) {
        span.textContent = currentPortalMode === "batch"
          ? "Launch Batch Portal Sync (Apr - Oct)"
          : "Launch Single Month Bridge";
      }
    }
  }

  if (btnSyncTallyLive) {
    const span = btnSyncTallyLive.querySelector("span");
    if (bridgeState.mode === "cloud_web_only") {
      btnSyncTallyLive.classList.add("btn-connector-needed");
      if (span) span.textContent = "Requires Connector (Setup)";
    } else {
      btnSyncTallyLive.classList.remove("btn-connector-needed");
      if (span) span.textContent = "Sync Live Purchase Register";
    }
  }

  if (btnDisconnect) {
    btnDisconnect.style.display = bridgeState.token ? "inline-flex" : "none";
  }

  if (connBanner) {
    if (!bridgeState.isCloudBackend) {
      connBanner.style.background = "#ecfdf5";
      connBanner.style.borderColor = "#a7f3d0";
      connBanner.style.color = "#065f46";
      connBanner.innerHTML = `<strong>Local Desktop Engine Active:</strong> You are running directly on <code>${escHtml(location.origin)}</code>. All GST Portal, Downloads, and Tally Port 9000 integrations work natively without a separate Connector.`;
    } else if (bridgeState.mode === "cloud_with_bridge") {
      connBanner.style.background = "#ecfdf5";
      connBanner.style.borderColor = "#a7f3d0";
      connBanner.style.color = "#065f46";
      const tallyNote = bridgeState.tallyActive
        ? `Tally Prime Online (${escHtml(bridgeState.tallyCompany || "Port 9000")})`
        : "Tally Prime Port 9000 Offline";
      connBanner.innerHTML = `<strong>Connected &amp; Paired</strong> on <code>http://127.0.0.1:${bridgeState.port}</code> &bull; ${tallyNote}.`;
    } else if (bridgeState.connected && !bridgeState.paired) {
      connBanner.style.background = "#eff6ff";
      connBanner.style.borderColor = "#bfdbfe";
      connBanner.style.color = "#1e40af";
      connBanner.innerHTML = `<strong>TallyWalls Connector Detected</strong> on port <code>${bridgeState.port}</code>, waiting for pairing. Enter the 6-digit PIN shown in the Connector window below.`;
    } else {
      connBanner.style.background = "#fffbeb";
      connBanner.style.borderColor = "#fcd34d";
      connBanner.style.color = "#92400e";
      connBanner.innerHTML = `<strong>Connector Offline:</strong> No running TallyWalls Connector found on ports <code>9455-9459</code>. Download and launch it on your Windows PC, or continue using Direct File Upload and Enterprise Demo.`;
    }
  }
}

function openConnectorModal(customMsg = "") {
  const modal = document.getElementById("connectorModal");
  if (modal) modal.style.display = "flex";
  const statusMsg = document.getElementById("connectorStatusMsg");
  if (statusMsg) {
    statusMsg.textContent = customMsg || "";
    statusMsg.style.color = customMsg ? "#b45309" : "var(--text-secondary)";
  }
  if (bridgeState.isCloudBackend) {
    probeBridge();
  } else {
    updateEnvironmentUI();
  }
}

function closeConnectorModal() {
  const modal = document.getElementById("connectorModal");
  if (modal) modal.style.display = "none";
}

async function probeBridgeWithFeedback() {
  const msgEl = document.getElementById("connectorStatusMsg");
  if (msgEl) {
    msgEl.style.color = "var(--text-secondary)";
    msgEl.textContent = "Scanning localhost ports 9455-9459...";
  }
  await probeBridge();
  if (msgEl) {
    if (!bridgeState.isCloudBackend) {
      msgEl.style.color = "#059669";
      msgEl.textContent = "Local Desktop Engine is active.";
    } else if (bridgeState.mode === "cloud_with_bridge") {
      msgEl.style.color = "#059669";
      msgEl.textContent = `Connected and paired on port ${bridgeState.port}.`;
    } else if (bridgeState.connected) {
      msgEl.style.color = "#1d4ed8";
      msgEl.textContent = `Connector responding on port ${bridgeState.port}. Enter your 6-digit PIN to pair.`;
    } else {
      msgEl.style.color = "#b91c1c";
      msgEl.textContent = "Connector not detected on localhost. Make sure TallyWallsConnector is running and click Allow if your browser asks for local network permission.";
    }
  }
}

async function handleManualPinPair() {
  const input = document.getElementById("connectorPinInput");
  const msgEl = document.getElementById("connectorStatusMsg");
  const pin = (input ? input.value : "").replace(/\s+/g, "").trim();
  if (!/^\d{6}$/.test(pin)) {
    if (msgEl) {
      msgEl.style.color = "#b91c1c";
      msgEl.textContent = "Please enter the exact 6-digit numeric PIN displayed in TallyWalls Connector.";
    }
    return;
  }

  await probeBridge();
  if (!bridgeState.connected || !bridgeState.port) {
    if (msgEl) {
      msgEl.style.color = "#b91c1c";
      msgEl.textContent = "Connector is not running on ports 9455-9459. Launch TallyWallsConnector first.";
    }
    return;
  }

  try {
    const data = await safeFetchJSON(`http://127.0.0.1:${bridgeState.port}/bridge/pair`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ pin }),
    });
    if (data && data.bridge_token) {
      bridgeState.token = data.bridge_token;
      bridgeState.paired = true;
      bridgeState.mode = "cloud_with_bridge";
      localStorage.setItem(LS_BRIDGE_TOKEN, data.bridge_token);
      localStorage.setItem(LS_BRIDGE_PORT, String(bridgeState.port));
      if (input) input.value = "";
      updateEnvironmentUI();
      if (msgEl) {
        msgEl.style.color = "#059669";
        msgEl.textContent = `Paired successfully with TallyWalls Connector (Port ${bridgeState.port})!`;
      }
    }
  } catch (err) {
    if (msgEl) {
      msgEl.style.color = "#b91c1c";
      msgEl.textContent = err.message || "Pairing failed.";
    }
  }
}

function unpairConnector() {
  bridgeState.token = "";
  bridgeState.paired = false;
  localStorage.removeItem(LS_BRIDGE_TOKEN);
  bridgeState.mode = bridgeState.isCloudBackend ? "cloud_web_only" : "local_desktop";
  updateEnvironmentUI();
  const msgEl = document.getElementById("connectorStatusMsg");
  if (msgEl) {
    msgEl.style.color = "var(--text-secondary)";
    msgEl.textContent = "Browser unpaired from local Connector.";
  }
}

async function bridgeFetchJSON(bridgePath, options = {}) {
  if (!bridgeState.connected || !bridgeState.port || !bridgeState.token) {
    throw new Error("TallyWalls Local Connector is not connected or paired.");
  }
  const url = `http://127.0.0.1:${bridgeState.port}${bridgePath}`;
  const headers = {
    ...(options.headers || {}),
    "X-Bridge-Token": bridgeState.token,
  };
  try {
    return await safeFetchJSON(url, { ...options, headers });
  } catch (err) {
    if (err && /unauthorized|x-bridge-token/i.test(err.message || "")) {
      bridgeState.paired = false;
      bridgeState.mode = "cloud_web_only";
      localStorage.removeItem(LS_BRIDGE_TOKEN);
      updateEnvironmentUI();
    }
    throw err;
  }
}

async function desktopFetchJSON(apiPath, bridgePath, options = {}) {
  if (bridgeState.mode === "cloud_with_bridge") {
    return bridgeFetchJSON(bridgePath, options);
  }
  if (bridgeState.mode === "cloud_web_only") {
    throw new Error("This action requires TallyWalls Local Connector running on your Windows PC.");
  }
  return safeFetchJSON(apiPath, options);
}

// -------------------------------------------------------------
// File Drop & Inputs Setup
// -------------------------------------------------------------

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
        <span style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap;"><span style="color:#059669; font-weight:600;">Attached:</span> ${escHtml(fileDesc)}</span>
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
  const startYear = parseInt(selectedFY.split("-")[0], 10) || 2026;
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
  document.getElementById("btnReconcile").addEventListener("click", handleReconcile);
  document.getElementById("btnLoadSample").addEventListener("click", handleLoadSample);
  document.getElementById("btnExportExcel").addEventListener("click", handleExportExcel);

  document.getElementById("tableSearch").addEventListener("input", (e) => {
    searchQuery = e.target.value.toLowerCase().trim();
    renderTable();
  });

  document.querySelectorAll(".tab-pill").forEach(btn => {
    btn.addEventListener("click", () => {
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

  // Connector Setup Modal & Navbar Badge Controls
  const statusBadge = document.getElementById("engineStatusBadge");
  if (statusBadge) {
    statusBadge.addEventListener("click", () => openConnectorModal());
    statusBadge.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " ") {
        e.preventDefault();
        openConnectorModal();
      }
    });
  }
  const btnProbe = document.getElementById("btnProbeConnector");
  if (btnProbe) btnProbe.addEventListener("click", probeBridgeWithFeedback);

  const btnPair = document.getElementById("btnPairConnector");
  if (btnPair) btnPair.addEventListener("click", handleManualPinPair);

  const btnDisconnect = document.getElementById("btnDisconnectConnector");
  if (btnDisconnect) btnDisconnect.addEventListener("click", unpairConnector);

  const btnDirectDownload = document.getElementById("btnDirectDownloadConnector");
  if (btnDirectDownload) {
    btnDirectDownload.addEventListener("click", () => {
      const msgEl = document.getElementById("connectorStatusMsg");
      if (msgEl) {
        msgEl.style.color = "#0369a1";
        msgEl.textContent = "Downloading TallyWallsConnector_Windows.zip (109.8 MB)... Extract the zip and double-click TallyWallsConnector.exe once finished.";
      }
    });
  }

  // Close modals when clicking backdrop
  ["portalModal", "downloadsModal", "tallyModal", "connectorModal"].forEach(id => {
    const m = document.getElementById(id);
    if (m) {
      m.addEventListener("click", (e) => {
        if (e.target === m) m.style.display = "none";
      });
    }
  });
}

// -------------------------------------------------------------
// GST Portal Direct Bridge (Local Engine or Companion Connector)
// -------------------------------------------------------------

function openPortalModal() {
  initFinancialYears();
  const modal = document.getElementById("portalModal");
  if (modal) modal.style.display = "flex";
  setPortalModalMode(currentPortalMode);
  resetProgressBox();
  updateEnvironmentUI();

  if (bridgeState.mode === "cloud_web_only") {
    return;
  }

  desktopFetchJSON("/api/portal/saved-creds", "/bridge/portal/saved-creds")
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
    if (btnLaunch && bridgeState.mode !== "cloud_web_only") {
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
    if (btnLaunch && bridgeState.mode !== "cloud_web_only") {
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
    await desktopFetchJSON("/api/portal/clear-creds", "/bridge/portal/clear-creds", { method: "POST" });
    const u = document.getElementById("portalUsername");
    const p = document.getElementById("portalPassword");
    if (u) u.value = "";
    if (p) p.value = "";
    const badge = document.getElementById("credsStatusBadge");
    if (badge) badge.style.display = "none";
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

function closePortalModal() {
  const m = document.getElementById("portalModal");
  if (m) m.style.display = "none";
}

function resetProgressBox() {
  const box = document.getElementById("portalProgressBox");
  if (box) box.style.display = "none";
  ["pStep1", "pStep2", "pStep3", "pStep4"].forEach(id => {
    const el = document.getElementById(id);
    if (el) el.className = "progress-step-item";
  });
}

function updateProgressSteps(activeStep, stepMessage = "") {
  const setStep = (stepId, state) => {
    const el = document.getElementById(stepId);
    if (el) el.className = `progress-step-item ${state}`;
  };
  for (let s = 1; s <= 4; s++) {
    const id = `pStep${s}`;
    if (s < activeStep) setStep(id, "done");
    else if (s === activeStep) setStep(id, "active");
    else setStep(id, "");
  }
  if (stepMessage && activeStep >= 1 && activeStep <= 4) {
    const el = document.getElementById(`pStep${activeStep}`);
    if (el) {
      el.innerHTML = `<span class="step-dot"></span> ${activeStep}. ${escHtml(stepMessage)}`;
    }
  }
}

async function pollBridgePortalJob(jobId) {
  const maxDurationMs = 9 * 60 * 1000;
  const startedAt = Date.now();

  while (Date.now() - startedAt < maxDurationMs) {
    const job = await bridgeFetchJSON(`/bridge/portal/fetch-2b/status/${encodeURIComponent(jobId)}`);
    if (job.step) {
      updateProgressSteps(job.step, job.message || "");
    }
    if (job.status === "completed" && job.result) {
      return job.result;
    }
    if (job.status === "failed") {
      throw new Error(job.error || job.message || "GST Portal sync failed.");
    }
    await new Promise(resolve => setTimeout(resolve, 1000));
  }

  throw new Error("GST Portal session timed out after 9 minutes.");
}

async function handleLaunchBridge(simulate = false) {
  // Never silently simulate when user clicked Live while in Cloud Web-Only mode
  if (!simulate && bridgeState.mode === "cloud_web_only") {
    closePortalModal();
    openConnectorModal("Start and pair TallyWalls Connector on your Windows PC to launch live GST Portal browser sync.");
    return;
  }

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

  // Save credentials locally if explicitly checked
  if (saveCreds && username && password && !simulate) {
    desktopFetchJSON("/api/portal/save-profile", "/bridge/portal/save-profile", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password })
    }).catch(console.error);
  }

  const progressBox = document.getElementById("portalProgressBox");
  progressBox.style.display = "block";

  const p3 = document.getElementById("pStep3");
  const p4 = document.getElementById("pStep4");
  if (isBatch) {
    if (p3) p3.innerHTML = `<span class="step-dot"></span> 3. Authenticated! Sequentially downloading statutory months...`;
    if (p4) p4.innerHTML = `<span class="step-dot"></span> 4. All monthly statements downloaded &amp; consolidated!`;
  } else {
    if (p3) p3.innerHTML = `<span class="step-dot"></span> 3. Authenticated! Navigating legitimately to Return Dashboard...`;
    if (p4) p4.innerHTML = `<span class="step-dot"></span> 4. GSTR-2B downloaded &amp; auto-ingested into ledger!`;
  }

  updateProgressSteps(simulate ? 1 : 2);

  const payload = {
    financial_year: fy,
    batch_mode: isBatch,
    start_month: startMonth,
    end_month: endMonth,
    return_period: returnPeriod,
    return_type: returnType,
    username,
    password,
    save_creds: saveCreds,
    simulate
  };

  try {
    let data;
    if (!simulate && bridgeState.mode === "cloud_with_bridge") {
      const startRes = await bridgeFetchJSON("/bridge/portal/fetch-2b/start", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
      data = await pollBridgePortalJob(startRes.job_id);
    } else {
      data = await safeFetchJSON("/api/portal/fetch-2b", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload)
      });
    }

    updateProgressSteps(5); // marks steps 1..4 done

    if (data.data_preview && data.data_preview.length > 0) {
      const outputFilename = isBatch
        ? `GSTR2B_Consolidated_${startMonth}_${endMonth}_${fy}.csv`
        : `${returnType}_${returnPeriod}.csv`;
      attachDataAsFile2B(data.data_preview, outputFilename, data.rows_count);
    }

    setTimeout(() => {
      closePortalModal();
      const msg = data.message || (isBatch
        ? `Success: Consolidated ${data.rows_count} invoices across statutory months (FY ${fy})!`
        : `Success: ${returnType} statement (${data.rows_count} invoices) captured!`);
      alert(msg);
    }, 600);
  } catch (err) {
    alert("Portal Automation Notice: " + (err.message || "Session ended before file download completed."));
  }
}

function attachDataAsFile2B(records, filename, rowCount) {
  const safeName = filename.replace(/\.xlsx?$|\.zip$/i, "") + ".csv";
  const csvContent = convertJSONToCSV(records);
  const blob = new Blob([csvContent], { type: "text/csv;charset=utf-8" });
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
      <span style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap;"><span style="color:#059669; font-weight:600;">Ingested:</span> ${escHtml(filename)} (${rowCount} Invoices)</span>
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
  const blob = new Blob([csvContent], { type: "text/csv;charset=utf-8" });
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
      <span style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap;"><span style="color:#059669; font-weight:600;">Tally Synced:</span> ${escHtml(filename)} (${rowCount} Vouchers)</span>
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

function csvEscapeField(val) {
  const str = (val === null || val === undefined) ? "" : String(val);
  if (/[",\r\n]/.test(str)) {
    return '"' + str.replace(/"/g, '""') + '"';
  }
  return str;
}

function convertJSONToCSV(items) {
  if (!items || !items.length) return "";
  const header = Object.keys(items[0]);
  const headerLine = header.map(csvEscapeField).join(",");
  const rows = items.map(item =>
    header.map(fieldName => csvEscapeField(item[fieldName])).join(",")
  );
  return [headerLine, ...rows].join("\r\n");
}

// -------------------------------------------------------------
// Downloads Folder Scanner
// -------------------------------------------------------------

function openDownloadsModal() {
  const modal = document.getElementById("downloadsModal");
  if (modal) modal.style.display = "flex";
  updateEnvironmentUI();
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
  const btnAttachAll = document.getElementById("btnAttachAllDownloads");

  if (bridgeState.mode === "cloud_web_only") {
    scanningEl.style.display = "none";
    listEl.style.display = "none";
    emptyEl.style.display = "block";
    if (btnAttachAll) btnAttachAll.style.display = "none";
    emptyEl.innerHTML = `<div style="color:#92400e; font-weight:600;">Local Connector Offline: Connect TallyWalls Connector to scan your PC Downloads folder, or attach files manually using "Select File".</div>`;
    return;
  }

  scanningEl.style.display = "block";
  listEl.style.display = "none";
  emptyEl.style.display = "none";

  try {
    const data = await desktopFetchJSON("/api/portal/scan-downloads", "/bridge/portal/scan-downloads");
    scanningEl.style.display = "none";

    const matches = data.matches || [];
    currentDownloadsMatches = matches;
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
            <span style="overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${escHtml(m.file_name)}">${escHtml(m.file_name)}</span>
            ${badge}
          </div>
          <div style="font-size:0.75rem; color:var(--text-secondary); margin-top:2px;">${m.size_kb} KB • Modified ${escHtml(m.modified_date)}</div>
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
    emptyEl.innerHTML = `<div style="color:#ef4444; font-weight:600;">Scan Failed: ${escHtml(err.message)}</div>`;
  }
}

async function deleteDownloadedFile(filePath, fileName) {
  const confirmed = confirm(`Are you sure you want to permanently delete "${fileName}" from your downloads folder?`);
  if (!confirmed) return;

  try {
    await desktopFetchJSON("/api/portal/delete-download", "/bridge/portal/delete-download", {
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
    const data = await desktopFetchJSON("/api/portal/ingest-local", "/bridge/portal/ingest-local", {
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
    const data = await desktopFetchJSON("/api/portal/ingest-batch", "/bridge/portal/ingest-batch", {
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
  updateEnvironmentUI();
  checkTallyStatus();
}

function closeTallyModal() {
  const modal = document.getElementById("tallyModal");
  if (modal) modal.style.display = "none";
}

async function checkTallyStatus() {
  const textEl = document.getElementById("tallyStatusText");
  const badgeEl = document.getElementById("tallyStatusBadge");

  if (bridgeState.mode === "cloud_web_only") {
    if (textEl) textEl.textContent = "Connector offline. Pair TallyWalls Connector to reach Tally Port 9000 on your PC.";
    if (badgeEl) {
      badgeEl.className = "tally-status-badge inactive";
      badgeEl.textContent = "Connector Required";
    }
    return;
  }

  if (textEl) textEl.textContent = "Probing http://localhost:9000...";
  if (badgeEl) {
    badgeEl.className = "tally-status-badge inactive";
    badgeEl.textContent = "Probing...";
  }

  try {
    const data = await desktopFetchJSON("/api/tally/status", "/bridge/tally/status");
    if (data.active) {
      if (textEl) textEl.textContent = `${data.message} (${data.company})`;
      if (badgeEl) {
        badgeEl.className = "tally-status-badge active";
        badgeEl.textContent = "Online • Port 9000";
      }
    } else {
      if (textEl) textEl.textContent = data.message || "Tally Prime XML Server is offline or not running.";
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
  if (!simulate && bridgeState.mode === "cloud_web_only") {
    closeTallyModal();
    openConnectorModal("Start and pair TallyWalls Connector on your Windows PC to sync vouchers directly from Tally Prime (localhost:9000).");
    return;
  }

  const btn = simulate ? document.getElementById("btnSimulateTally") : document.getElementById("btnSyncTallyLive");
  const origText = btn ? btn.innerHTML : "";
  if (btn) btn.innerHTML = `<span>Syncing...</span>`;

  try {
    let data;
    if (simulate) {
      data = await safeFetchJSON("/api/tally/sync", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ simulate: true })
      });
    } else {
      data = await desktopFetchJSON("/api/tally/sync", "/bridge/tally/sync", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ simulate: false })
      });
    }

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

function setLoadingState(isLoading, titleText = "Processing & Multi-Tier Normalization...") {
  const loader = document.getElementById("loadingIndicator");
  const loadingText = document.getElementById("loadingText");
  if (loadingText && titleText) loadingText.textContent = titleText;
  if (loader) loader.style.display = isLoading ? "block" : "none";
}

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

  // Vercel serverless request body cap check (~4.5 MB)
  let totalUploadBytes = fileBooks.size || 0;
  for (let i = 0; i < files2B.length; i++) {
    totalUploadBytes += files2B[i].size || 0;
  }
  if (bridgeState.isCloudBackend && totalUploadBytes > 4.2 * 1024 * 1024) {
    const mb = (totalUploadBytes / (1024 * 1024)).toFixed(2);
    alert(
      `Combined upload size (${mb} MB) exceeds the 4.5 MB cloud web limit.\n` +
      `Please reconcile fewer months at a time, or use the offline TallyWalls desktop app (run_app.bat / TallyWalls.exe) for very large files.`
    );
    return;
  }

  const btn = document.getElementById("btnReconcile");
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = `<span style="display:inline-block; animation:spin 1s linear infinite;">•</span> Auditing Invoices...`;
  }
  setLoadingState(true, "Processing & Multi-Tier Normalization...");

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
    setLoadingState(false);
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = `
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
          <polygon points="5 3 19 12 5 21 5 3"/>
        </svg>
        <span>Run Reconciliation Engine</span>
      `;
    }
  }
}

async function handleLoadSample() {
  const btn = document.getElementById("btnLoadSample");
  if (btn) btn.disabled = true;
  const origText = btn ? btn.innerHTML : "";
  if (btn) btn.innerHTML = `<span>Loading Audit Data...</span>`;
  setLoadingState(true, "Running Enterprise Sample Reconciliation...");

  try {
    currentReconData = await safeFetchJSON("/api/load-sample?tolerance=1.00");
    updateUIWithReconData(currentReconData);
  } catch (err) {
    alert("Demo Load Failed: " + err.message);
  } finally {
    setLoadingState(false);
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
  currentReconData = data;

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
        total_inv_val_books: b.total_inv_val !== undefined ? b.total_inv_val : null,
        taxable_2b: p.taxable !== undefined ? p.taxable : null,
        tax_amount_2b: p.total_tax !== undefined ? p.total_tax : null,
        total_inv_val_2b: p.total_inv_val !== undefined ? p.total_inv_val : null,
        tax_diff: r.tax_diff || 0.0,
        taxable_diff: r.taxable_diff || 0.0,
        audit_remark: r.remarks || ""
      };
    });
  }

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

  const cats = kpis.category_counts || {};
  const totalLedger = (currentReconData && currentReconData.ledger) ? currentReconData.ledger.length : 0;
  setText("countAll", totalLedger);
  setText("countExact", cats.EXACT_MATCH ?? 0);
  setText("countNorm", cats.NORMALIZED_MATCH ?? 0);
  setText("countTypo", (cats.TYPO_MATCH ?? cats.PROBABLE_TYPO ?? 0));
  setText("countMismatch", cats.VALUE_MISMATCH ?? 0);
  setText("countRisk", cats.MISSING_IN_2B ?? 0);
  setText("countUnbooked", cats.MISSING_IN_BOOKS ?? 0);

  const resSection = document.getElementById("resultsSection");
  if (resSection) {
    resSection.style.display = "block";
    try { resSection.scrollIntoView({ behavior: "smooth" }); } catch (_) {}
  }

  renderTable();
}

function renderTable() {
  if (!currentReconData || !currentReconData.ledger) return;

  const tbody = document.getElementById("reconTableBody");
  if (!tbody) return;
  tbody.innerHTML = "";

  const records = currentReconData.ledger.filter(row => {
    let matchesCategory = true;
    if (currentFilter === "MATCHED") {
      matchesCategory = ["EXACT_MATCH", "NORMALIZED_MATCH", "TYPO_MATCH", "PROBABLE_TYPO"].includes(row.match_category);
    } else if (currentFilter !== "ALL") {
      matchesCategory = (row.match_category === currentFilter) ||
                        (currentFilter === "PROBABLE_TYPO" && (row.match_category === "TYPO_MATCH" || row.status === "PROBABLE_TYPO"));
    }

    if (!matchesCategory) return false;

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
    const totalCount = currentReconData.ledger.length;
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

    let booksInvDisplay = `<span class="code-font">${escHtml(rawBooksInv)}</span>`;
    let portalInvDisplay = `<span class="code-font">${escHtml(rawPortalInv)}</span>`;

    if (row.match_category === "TYPO_MATCH" || row.match_category === "PROBABLE_TYPO") {
      booksInvDisplay = `<span class="code-font diff-highlight" title="Books Invoice No">${escHtml(rawBooksInv)}</span>`;
      portalInvDisplay = `<span class="code-font diff-highlight" title="Portal Invoice No">${escHtml(rawPortalInv)}</span>`;
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
          <span>${escHtml(badgeLabel)}</span>
        </span>
      </td>
      <td>
        <div class="cpty-name" style="font-weight:600; color:var(--text-primary);">${escHtml(cptyName)}</div>
        <div class="cpty-gstin" style="font-size:0.75rem; color:var(--text-secondary); font-family:var(--font-mono);">${escHtml(cptyGstin)}</div>
      </td>
      <td>${booksInvDisplay}</td>
      <td>${escHtml(booksDate)}</td>
      <td class="num-cell" style="font-weight: 500;">${booksTaxable}</td>
      <td class="num-cell" style="font-weight: 600;">${booksTax}</td>
      <td>${portalInvDisplay}</td>
      <td>${escHtml(portalDate)}</td>
      <td class="num-cell" style="font-weight: 500;">${portalTaxable}</td>
      <td class="num-cell" style="font-weight: 600;">${portalTax}</td>
      <td class="num-cell">${taxDiff}</td>
      <td class="audit-remark">${escHtml(row.audit_remark || "")}</td>
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
    // Slim payload containing summary + flat ledger avoids Vercel request body limit
    const exportPayload = {
      summary: currentReconData.summary || {},
      ledger: currentReconData.ledger || [],
    };
    const res = await fetch("/api/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(exportPayload)
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
