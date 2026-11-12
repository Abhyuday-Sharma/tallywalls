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
  setupGstr9HubListeners();
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

  const reconTabPills = document.querySelectorAll("#workspaceRecon .tab-pill[data-filter]");
  reconTabPills.forEach(btn => {
    btn.addEventListener("click", () => {
      reconTabPills.forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      currentFilter = btn.getAttribute("data-filter") || "ALL";
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
        books_line_count: r.books_line_count ?? (r.books ? (b.line_count ?? 1) : 0),
        portal_line_count: r.portal_line_count ?? (r.portal ? (p.line_count ?? 1) : 0),
        books_slabs: r.books_slabs || "",
        portal_slabs: r.portal_slabs || "",
        itc_ineligible: Boolean(r.itc_ineligible),
        is_rcm: Boolean(r.is_rcm),
        tax_head_mismatch: Boolean(r.tax_head_mismatch),
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
    raw_books_rows: sum.raw_books_rows ?? sum.total_books_invoices ?? 0,
    total_tax_2b: sum.portal_tax_total ?? 0,
    total_invoices_2b: sum.total_2b_invoices ?? 0,
    raw_2b_rows: sum.raw_2b_rows ?? sum.total_2b_invoices ?? 0,
    matched_itc: sum.matched_tax ?? 0,
    matched_count: sum.matched_count ?? 0,
    multi_rate_matched_count: sum.multi_rate_matched_count ?? 0,
    ineligible_itc: sum.ineligible_tax ?? 0,
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

  const booksInvCount = kpis.total_invoices_books ?? 0;
  const rawBooksRows = kpis.raw_books_rows ?? sum.raw_books_rows ?? booksInvCount;
  const booksFooter = (rawBooksRows > booksInvCount)
    ? `${booksInvCount} Bills (${rawBooksRows} Rate Lines)`
    : `${booksInvCount} Invoices in Ledger`;

  const portalInvCount = kpis.total_invoices_2b ?? 0;
  const rawPortalRows = kpis.raw_2b_rows ?? sum.raw_2b_rows ?? portalInvCount;
  const portalFooter = (rawPortalRows > portalInvCount)
    ? `${portalInvCount} Bills (${rawPortalRows} Rate Lines)`
    : `${portalInvCount} Invoices on Portal`;

  const ineligibleAmt = kpis.ineligible_itc ?? sum.ineligible_tax ?? 0;
  const multiRateMatched = kpis.multi_rate_matched_count ?? sum.multi_rate_matched_count ?? 0;
  let matchFooter = `${kpis.reconciliation_percentage ?? 0}% Match Efficiency`;
  if (ineligibleAmt > 0) {
    matchFooter = `${kpis.reconciliation_percentage ?? 0}% Match | Blocked: ${formatINR(ineligibleAmt)}`;
  } else if (multiRateMatched > 0) {
    matchFooter = `${kpis.reconciliation_percentage ?? 0}% Match (${multiRateMatched} Multi-Rate)`;
  }

  setText("kpiBooksTax", formatINR(kpis.total_tax_books ?? 0));
  setText("kpiBooksCount", booksFooter);

  setText("kpiPortalTax", formatINR(kpis.total_tax_2b ?? 0));
  setText("kpiPortalCount", portalFooter);

  setText("kpiMatchedTax", formatINR(kpis.matched_itc ?? 0));
  setText("kpiMatchRate", matchFooter);

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
    const activeFilter = currentFilter || "ALL";
    if (activeFilter === "MATCHED") {
      matchesCategory = ["EXACT_MATCH", "NORMALIZED_MATCH", "TYPO_MATCH", "PROBABLE_TYPO"].includes(row.match_category);
    } else if (activeFilter !== "ALL") {
      matchesCategory = (row.match_category === activeFilter) ||
                        (activeFilter === "PROBABLE_TYPO" && (row.match_category === "TYPO_MATCH" || row.status === "PROBABLE_TYPO"));
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
    if ((row.books_line_count || 0) > 1) {
      const slabInfo = row.books_slabs ? ` (${row.books_slabs})` : "";
      booksInvDisplay += `<div style="font-size:0.68rem; color:#0f766e; font-weight:600; margin-top:2px;">${row.books_line_count} lines${escHtml(slabInfo)}</div>`;
    }

    let portalInvDisplay = `<span class="code-font">${escHtml(rawPortalInv)}</span>`;
    if ((row.portal_line_count || 0) > 1) {
      const pSlabInfo = row.portal_slabs ? ` (${row.portal_slabs})` : "";
      portalInvDisplay += `<div style="font-size:0.68rem; color:#0369a1; font-weight:600; margin-top:2px;">${row.portal_line_count} lines${escHtml(pSlabInfo)}</div>`;
    }

    if (row.match_category === "TYPO_MATCH" || row.match_category === "PROBABLE_TYPO") {
      booksInvDisplay = `<span class="code-font diff-highlight" title="Books Invoice No">${escHtml(rawBooksInv)}</span>`;
      portalInvDisplay = `<span class="code-font diff-highlight" title="Portal Invoice No">${escHtml(rawPortalInv)}</span>`;
    } else if (row.match_category === "MISSING_IN_2B") {
      portalInvDisplay = `<span style="color:#ef4444; font-weight:600; font-size:0.75rem; background:rgba(239,68,68,0.12); padding:2px 8px; border-radius:4px;">Not in GSTR-2B</span>`;
    } else if (row.match_category === "MISSING_IN_BOOKS") {
      booksInvDisplay = `<span style="color:#0284c7; font-style:italic; font-size:0.75rem; background:rgba(2,132,199,0.1); padding:2px 8px; border-radius:4px;">Not in Books</span>`;
    }

    const flagTags = [];
    if (row.itc_ineligible) {
      flagTags.push(`<span style="display:inline-block; background:#fee2e2; color:#b91c1c; border:1px solid #fca5a5; font-size:0.65rem; font-weight:700; padding:1px 5px; border-radius:3px; margin-top:3px;">INELIGIBLE ITC</span>`);
    }
    if (row.is_rcm) {
      flagTags.push(`<span style="display:inline-block; background:#fef3c7; color:#b45309; border:1px solid #fcd34d; font-size:0.65rem; font-weight:700; padding:1px 5px; border-radius:3px; margin-top:3px;">RCM</span>`);
    }
    if (row.tax_head_mismatch) {
      flagTags.push(`<span style="display:inline-block; background:#ffedd5; color:#c2410c; border:1px solid #fdba74; font-size:0.65rem; font-weight:700; padding:1px 5px; border-radius:3px; margin-top:3px;">TAX HEAD SWAP</span>`);
    }
    const extraTagsHtml = flagTags.length ? `<div style="display:flex; gap:4px; flex-wrap:wrap; margin-top:2px;">${flagTags.join("")}</div>` : "";

    const taxDiff = (row.tax_diff !== null && row.tax_diff !== undefined && Math.abs(row.tax_diff) > 0.001)
      ? `<span style="font-weight:700; color:var(--status-mismatch-text);">${formatINR(row.tax_diff)}</span>`
      : `<span style="color:var(--text-tertiary);">₹0.00</span>`;

    tr.innerHTML = `
      <td>
        <span class="status-pill ${pillClass}">
          <span class="dot"></span>
          <span>${escHtml(badgeLabel)}</span>
        </span>
        ${extraTagsHtml}
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

// -------------------------------------------------------------
// GSTR-9 Annual Return Hub (v3.0)
// -------------------------------------------------------------

let currentWorkspace = "recon";
let currentGstr9Data = null;
let currentGstr9SubTab = "outward";

function switchWorkspace(ws) {
  currentWorkspace = ws === "gstr9" ? "gstr9" : "recon";
  const tabRecon = document.getElementById("wsTabRecon");
  const tabGstr9 = document.getElementById("wsTabGstr9");
  const viewRecon = document.getElementById("workspaceRecon");
  const viewGstr9 = document.getElementById("workspaceGstr9");

  if (tabRecon) {
    tabRecon.classList.toggle("active", currentWorkspace === "recon");
    tabRecon.setAttribute("aria-selected", String(currentWorkspace === "recon"));
  }
  if (tabGstr9) {
    tabGstr9.classList.toggle("active", currentWorkspace === "gstr9");
    tabGstr9.setAttribute("aria-selected", String(currentWorkspace === "gstr9"));
  }
  if (viewRecon) viewRecon.style.display = currentWorkspace === "recon" ? "block" : "none";
  if (viewGstr9) viewGstr9.style.display = currentWorkspace === "gstr9" ? "block" : "none";
}

function updateG9FileRowUI(inputId, labelId, rowId, emptyText) {
  const inp = document.getElementById(inputId);
  const lbl = document.getElementById(labelId);
  const row = document.getElementById(rowId);
  if (!inp || !lbl || !row) return;
  if (inp.files && inp.files.length > 0) {
    let totalBytes = 0;
    const names = [];
    for (let i = 0; i < inp.files.length; i++) {
      totalBytes += inp.files[i].size || 0;
      names.push(inp.files[i].name);
    }
    const kb = (totalBytes / 1024).toFixed(1);
    lbl.textContent = inp.files.length === 1
      ? `Attached: ${names[0]} (${kb} KB)`
      : `Attached: ${inp.files.length} files (${kb} KB total)`;
    lbl.style.color = "#059669";
    row.classList.add("has-file");
  } else {
    lbl.textContent = emptyText;
    lbl.style.color = "";
    row.classList.remove("has-file");
  }
}

function setupGstr9HubListeners() {
  const fileConfigs = [
    ["g9FileSales", "g9LabelSales", "g9RowSales", "No Sales Register attached"],
    ["g9FileGstr1", "g9LabelGstr1", "g9RowGstr1", "No GSTR-1 export attached"],
    ["g9FileGstr3B", "g9LabelGstr3B", "g9RowGstr3B", "No GSTR-3B file attached (or use inputs below)"],
    ["g9FilePurchase", "g9LabelPurchase", "g9RowPurchase", "No Purchase Register attached"],
    ["g9File2B", "g9Label2B", "g9Row2B", "No GSTR-2B statement attached"],
  ];

  fileConfigs.forEach(([inId, lblId, rowId, emptyText]) => {
    const inp = document.getElementById(inId);
    if (inp) {
      inp.addEventListener("change", () => updateG9FileRowUI(inId, lblId, rowId, emptyText));
    }
  });

  const btnSample = document.getElementById("btnG9LoadSample");
  if (btnSample) btnSample.addEventListener("click", handleGstr9Sample);

  const btnRun = document.getElementById("btnG9Run");
  if (btnRun) btnRun.addEventListener("click", handleGstr9Compute);

  const btnReuse = document.getElementById("btnG9ReuseActive");
  if (btnReuse) btnReuse.addEventListener("click", reuseActiveReconFilesForGstr9);

  const btnExport = document.getElementById("btnG9ExportExcel");
  if (btnExport) btnExport.addEventListener("click", handleGstr9Export);

  const btnExportJson = document.getElementById("btnG9ExportJson");
  if (btnExportJson) btnExportJson.addEventListener("click", handleGstr9ExportJson);

  setupGstr9Automation();

  document.querySelectorAll(".g9-subtab").forEach(btn => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".g9-subtab").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      currentGstr9SubTab = btn.getAttribute("data-g9tab") || "outward";
      renderGstr9Table();
    });
  });
}

function reuseActiveReconFilesForGstr9() {
  const src2B = document.getElementById("file2B");
  const srcBooks = document.getElementById("fileBooks");
  const dst2B = document.getElementById("g9File2B");
  const dstPur = document.getElementById("g9FilePurchase");
  const statusEl = document.getElementById("g9ReuseStatus");

  let copied = 0;
  try {
    if (src2B && src2B.files && src2B.files.length > 0 && dst2B) {
      const dt2b = new DataTransfer();
      for (let i = 0; i < src2B.files.length; i++) dt2b.items.add(src2B.files[i]);
      dst2B.files = dt2b.files;
      updateG9FileRowUI("g9File2B", "g9Label2B", "g9Row2B", "No GSTR-2B statement attached");
      copied += src2B.files.length;
    }
    if (srcBooks && srcBooks.files && srcBooks.files.length > 0 && dstPur) {
      const dtBk = new DataTransfer();
      for (let i = 0; i < srcBooks.files.length; i++) dtBk.items.add(srcBooks.files[i]);
      dstPur.files = dtBk.files;
      updateG9FileRowUI("g9FilePurchase", "g9LabelPurchase", "g9RowPurchase", "No Purchase Register attached");
      copied += srcBooks.files.length;
    }
  } catch (e) {
    console.warn("Reuse files note:", e);
  }

  if (statusEl) {
    if (copied > 0) {
      statusEl.style.color = "#059669";
      statusEl.textContent = `Copied ${copied} active file(s) from Reconciliation tab.`;
    } else {
      statusEl.style.color = "#b45309";
      statusEl.textContent = "Attach GSTR-2B or Purchase Register in the first tab to reuse them here.";
    }
  }
}

async function _maybeGzipUpload(file) {
  if (!file) return file;
  const name = (file.name || "").toLowerCase();
  if (name.endsWith(".gz") || name.endsWith(".zip")) return file;
  if (file.size < 1.2 * 1024 * 1024 || typeof CompressionStream === "undefined") {
    return file;
  }
  try {
    const compressedStream = file.stream().pipeThrough(new CompressionStream("gzip"));
    const compressedBlob = await new Response(compressedStream).blob();
    if (compressedBlob.size < file.size) {
      return new File([compressedBlob], `${file.name}.gz`, { type: "application/gzip" });
    }
  } catch (err) {
    console.warn("Gzip pre-compression skipped:", err);
  }
  return file;
}

function _numInputVal(id) {
  const el = document.getElementById(id);
  if (!el) return null;
  const raw = String(el.value || "").trim();
  if (raw === "") return null;
  const n = parseFloat(raw);
  return isNaN(n) ? null : n;
}

function buildGstr9ConfigPayload() {
  const fy = (document.getElementById("g9Fy") || {}).value || "2025-26";
  const cutoffMonth = parseInt((document.getElementById("g9CutoffMonth") || {}).value || "11", 10) || 11;
  const slabRaw = (document.getElementById("g9TurnoverSlab") || {}).value || "true";
  const turnoverAbove5Cr = slabRaw === "true";

  const overrides = {};
  const g3bOverride = {};

  const outTx = _numInputVal("g9_3b_outward_taxable");
  const outTax = _numInputVal("g9_3b_outward_tax");
  if (outTx !== null || outTax !== null) {
    g3bOverride.outward_3_1 = {
      a_taxable: {
        taxable: outTx !== null ? outTx : 0.0,
        igst: outTax !== null ? outTax : 0.0,
        cgst: 0.0,
        sgst: 0.0,
        cess: 0.0,
      }
    };
  }

  const itc4a5 = _numInputVal("g9_3b_itc_4a5");
  if (itc4a5 !== null) {
    g3bOverride.itc_4 = {
      "4A_5_all_other": { igst: itc4a5, cgst: 0.0, sgst: 0.0, cess: 0.0 }
    };
  }

  const cashPaid = _numInputVal("g9_3b_cash_paid");
  const itcPaid = _numInputVal("g9_3b_itc_paid");
  const portalInt = _numInputVal("g9_3b_interest_fee");
  if (cashPaid !== null || itcPaid !== null || portalInt !== null) {
    const tp = {};
    if (cashPaid !== null) tp.cash_paid = { igst: cashPaid, cgst: 0.0, sgst: 0.0, cess: 0.0 };
    if (itcPaid !== null) tp.itc_utilized = { igst: itcPaid, cgst: 0.0, sgst: 0.0, cess: 0.0 };
    if (portalInt !== null) {
      tp.interest_paid = portalInt;
      tp.late_fee_paid = 0.0;
    }
    g3bOverride.tax_paid_6_1 = tp;
  }

  if (Object.keys(g3bOverride).length > 0) {
    overrides.gstr3b = g3bOverride;
  }

  const booksInt = _numInputVal("g9_books_interest_fee");
  if (booksInt !== null) overrides.books_interest = booksInt;

  const r37 = _numInputVal("g9_rule37_tax");
  if (r37 !== null) overrides.rule37 = { igst: r37, cgst: 0.0, sgst: 0.0, cess: 0.0 };

  const r42Pct = _numInputVal("g9_rule42_pct");
  if (r42Pct !== null) overrides.rule42_exempt_ratio = Math.max(0.0, Math.min(1.0, r42Pct / 100.0));

  const r43 = _numInputVal("g9_rule43_tax");
  if (r43 !== null) overrides.rule43 = { igst: r43, cgst: 0.0, sgst: 0.0, cess: 0.0 };

  const rec6h = _numInputVal("g9_reclaimed_6h");
  if (rec6h !== null) overrides.reclaimed_rule37 = { igst: rec6h, cgst: 0.0, sgst: 0.0, cess: 0.0 };

  return {
    fy,
    cutoff_month: cutoffMonth,
    turnover_above_5cr: turnoverAbove5Cr,
    manual_overrides: overrides,
  };
}

function setGstr9Loading(isLoading, msg = "Computing Statutory GSTR-9 Tables 4-18...") {
  const el = document.getElementById("g9LoadingIndicator");
  const txt = document.getElementById("g9LoadingText");
  if (txt && msg) txt.textContent = msg;
  if (el) el.style.display = isLoading ? "block" : "none";
}

async function handleGstr9Sample() {
  const btn = document.getElementById("btnG9LoadSample");
  const orig = btn ? btn.innerHTML : "";
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = "<span>Loading FY 2025-26 Sample...</span>";
  }
  setGstr9Loading(true, "Computing 12-Month FY 2025-26 Enterprise GSTR-9 Sample...");

  try {
    const fy = (document.getElementById("g9Fy") || {}).value || "2025-26";
    const cutoff = (document.getElementById("g9CutoffMonth") || {}).value || "11";
    const slab = (document.getElementById("g9TurnoverSlab") || {}).value || "true";
    const url = `/api/gstr9/sample?fy=${encodeURIComponent(fy)}&cutoff_month=${encodeURIComponent(cutoff)}&turnover_above_5cr=${encodeURIComponent(slab)}`;
    currentGstr9Data = await safeFetchJSON(url);
    renderGstr9Dashboard(currentGstr9Data);
  } catch (err) {
    alert("GSTR-9 Sample Error: " + err.message);
  } finally {
    setGstr9Loading(false);
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = orig;
    }
  }
}

async function handleGstr9Compute() {
  const btn = document.getElementById("btnG9Run");
  const orig = btn ? btn.innerHTML : "";
  const cfg = buildGstr9ConfigPayload();

  const fileFields = [
    ["g9FileSales", "sales_files"],
    ["g9FileGstr1", "gstr1_files"],
    ["g9FileGstr3B", "gstr3b_files"],
    ["g9FilePurchase", "purchase_files"],
    ["g9File2B", "gstr2b_files"],
  ];

  const formData = new FormData();
  let attachedCount = 0;
  let totalPayloadBytes = 0;

  if (btn) {
    btn.disabled = true;
    btn.innerHTML = "<span>Computing GSTR-9...</span>";
  }
  setGstr9Loading(true, "Normalizing 12-Month Registers & Computing Tables 4-18...");

  try {
    for (const [inpId, fieldName] of fileFields) {
      const inp = document.getElementById(inpId);
      if (inp && inp.files && inp.files.length > 0) {
        for (let i = 0; i < inp.files.length; i++) {
          const uploadFile = await _maybeGzipUpload(inp.files[i]);
          totalPayloadBytes += uploadFile.size || 0;
          formData.append(fieldName, uploadFile);
          attachedCount++;
        }
      }
    }

    if (attachedCount === 0 && !cfg.manual_overrides.gstr3b) {
      throw new Error("Please upload at least one register (Sales, GSTR-1, GSTR-3B, Purchase, or GSTR-2B) or enter GSTR-3B figures, or click Load 12M FY Sample.");
    }

    if (bridgeState.isCloudBackend && totalPayloadBytes > 4.2 * 1024 * 1024) {
      const mb = (totalPayloadBytes / (1024 * 1024)).toFixed(2);
      throw new Error(`Compressed upload size (${mb} MB) exceeds the 4.5 MB cloud web limit. Please split into fewer files or use the offline desktop app.`);
    }

    formData.append("config_json", JSON.stringify(cfg));
    currentGstr9Data = await safeFetchJSON("/api/gstr9/compute", {
      method: "POST",
      body: formData,
    });
    renderGstr9Dashboard(currentGstr9Data);
  } catch (err) {
    alert("GSTR-9 Computation Notice: " + err.message);
  } finally {
    setGstr9Loading(false);
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = orig;
    }
  }
}

function renderGstr9Dashboard(data) {
  if (!data || !data.summary) return;
  const s = data.summary;

  setText("g9KpiTurnoverVar", formatINR(s.turnover_variance || 0));
  setText("g9KpiTurnoverSub", `Books: ${formatINR(s.books_turnover || 0)} | 1: ${formatINR(s.gstr1_turnover || 0)}`);

  setText("g9KpiNetItc", formatINR(s.table_7j_net_itc || 0));
  setText("g9KpiNetItcSub", `6O: ${formatINR(s.table_6o_gross_itc || 0)} | Rev 7I: ${formatINR(s.table_7i_reversals || 0)}`);

  setText("g9Kpi8dDiff", formatINR(s.table_8d_diff_itc || 0));
  setText("g9Kpi8dSub", `2B 8A: ${formatINR(s.table_8a_2b_itc || 0)} | 8C: ${formatINR(s.table_8c_next_fy_itc || 0)}`);

  setText("g9KpiTaxShortfall", formatINR(s.tax_shortfall_excess || 0));
  setText("g9KpiTaxSub", `Payable: ${formatINR(s.tax_payable_total || 0)} | Paid: ${formatINR(s.tax_paid_total || 0)}`);

  setText("g9KpiHsnErrors", String(s.hsn_total_errors || 0));
  setText("g9KpiHsnSub", `${s.reversals_count || 0} Reversal Items | ${s.drilldown_count || 0} 8D Items`);

  setText("g9VaultTitle", `GSTR-9 Statutory Schedules (FY ${s.financial_year || "2025-26"})`);
  setText(
    "g9VaultSubtitle",
    `Cut-off: ${s.cutoff_window || "Apr - Nov 30"} | HSN Rule: ${s.turnover_above_5cr ? "> Rs. 5 Cr (6-digit)" : "<= Rs. 5 Cr (4-digit)"} | Rule 42 Exempt Ratio: ${s.rule_42_exempt_ratio_pct || 0}%`
  );

  // Audit Alerts & GSTR-9C Tie-out Banner
  const alertsEl = document.getElementById("g9AuditAlerts");
  if (alertsEl) {
    const g9c = data.gstr9c_bridge || {};
    const t5 = g9c.table_5_turnover || {};
    const t12 = g9c.table_12_itc || {};
    const r88Style = s.rule88a_warning
      ? "background:#fffbeb; border:1px solid #fcd34d; color:#92400e;"
      : "background:#ecfdf5; border:1px solid #a7f3d0; color:#065f46;";
    const intDiff = Number(s.interest_late_fee_diff || 0);
    const intHtml = Math.abs(intDiff) > 0.01
      ? `<span class="g9-badge-warn">Books vs Portal Int/Late Fee Diff: ${formatINR(intDiff)}</span>`
      : `<span class="g9-badge-ok">Interest & Late Fee Reconciled</span>`;

    alertsEl.innerHTML = `
      <div style="${r88Style} border-radius:10px; padding:12px 16px; font-size:0.8rem; display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:10px;">
        <div>
          <strong>Audit &amp; Rule 88A Status:</strong> ${escHtml(s.rule88a_message || "")}
          <div style="margin-top:4px; font-size:0.75rem; opacity:0.9;">
            <strong>GSTR-9C Tie-Out:</strong> Table 5 Turnover Unreconciled Diff: ${formatINR(t5.unreconciled_turnover_diff || 0)} &bull; Table 12 ITC Unreconciled Diff: ${formatINR(t12.unreconciled_itc_diff || 0)}
          </div>
        </div>
        <div>${intHtml}</div>
      </div>
    `;
  }

  const resSec = document.getElementById("g9ResultsSection");
  if (resSec) {
    resSec.style.display = "block";
    try { resSec.scrollIntoView({ behavior: "smooth" }); } catch (_) {}
  }

  renderGstr9Table();
}

function renderGstr9Table() {
  const box = document.getElementById("g9TableContainer");
  if (!box || !currentGstr9Data || !currentGstr9Data.tables) return;
  const t = currentGstr9Data.tables;

  if (currentGstr9SubTab === "outward") {
    const t4 = t.table_4 || [];
    const t5 = t.table_5 || [];
    const renderT4Rows = (rows) => rows.map(r => {
      const cls = r.is_total ? "g9-table-total" : "";
      const bk = r.books || {};
      const g1 = r.gstr1 || {};
      const g9 = r.gstr9 || {};
      const bkTax = r.books_tax !== undefined ? r.books_tax : (Number(bk.igst || 0) + Number(bk.cgst || 0) + Number(bk.sgst || 0) + Number(bk.cess || 0));
      const g1Tax = r.gstr1_tax !== undefined ? r.gstr1_tax : (Number(g1.igst || 0) + Number(g1.cgst || 0) + Number(g1.sgst || 0) + Number(g1.cess || 0));
      const g9Tax = r.gstr9_tax !== undefined ? r.gstr9_tax : (Number(g9.igst || 0) + Number(g9.cgst || 0) + Number(g9.sgst || 0) + Number(g9.cess || 0));
      const bkTx = r.books_taxable !== undefined ? r.books_taxable : bk.taxable;
      const g1Tx = r.gstr1_taxable !== undefined ? r.gstr1_taxable : g1.taxable;
      const g9Tx = r.gstr9_taxable !== undefined ? r.gstr9_taxable : g9.taxable;
      const varTx = r.variance_books_vs_gstr1 !== undefined ? r.variance_books_vs_gstr1 : (Number(bkTx || 0) - Number(g1Tx || 0));
      return `
        <tr class="${cls}">
          <td><strong>${escHtml(r.code)}</strong></td>
          <td>${escHtml(r.desc)}</td>
          <td class="num-cell">${formatINR(bkTx)}</td>
          <td class="num-cell">${formatINR(bkTax)}</td>
          <td class="num-cell">${formatINR(g1Tx)}</td>
          <td class="num-cell">${formatINR(g1Tax)}</td>
          <td class="num-cell">${formatINR(g9Tx)}</td>
          <td class="num-cell">${formatINR(g9Tax)}</td>
          <td class="num-cell"><strong>${formatINR(varTx)}</strong></td>
        </tr>
      `;
    }).join("");

    const renderT5Rows = (rows) => rows.map(r => {
      const cls = (r.is_total || r.is_subtotal) ? "g9-table-total" : "";
      const diff = Number(r.books_taxable || 0) - Number(r.gstr1_taxable || 0);
      return `
        <tr class="${cls}">
          <td><strong>${escHtml(r.code)}</strong></td>
          <td>${escHtml(r.desc)}</td>
          <td class="num-cell">${formatINR(r.books_taxable)}</td>
          <td class="num-cell">${formatINR(r.gstr1_taxable)}</td>
          <td class="num-cell">${formatINR(r.gstr3b_taxable)}</td>
          <td class="num-cell"><strong>${formatINR(diff)}</strong></td>
        </tr>
      `;
    }).join("");

    box.innerHTML = `
      <table class="ledger-table">
        <thead>
          <tr>
            <th>Table</th>
            <th>Part II: Table 4 — Taxable Outward &amp; RCM Supplies</th>
            <th class="num-cell">Books Taxable</th>
            <th class="num-cell">Books Tax</th>
            <th class="num-cell">GSTR-1 Taxable</th>
            <th class="num-cell">GSTR-1 Tax</th>
            <th class="num-cell">GSTR-9 Rec. Taxable</th>
            <th class="num-cell">GSTR-9 Rec. Tax</th>
            <th class="num-cell">Var (Books - 1)</th>
          </tr>
        </thead>
        <tbody>${renderT4Rows(t4)}</tbody>
      </table>
      <table class="ledger-table" style="margin-top:16px;">
        <thead>
          <tr>
            <th>Table</th>
            <th>Part II: Table 5 — Zero/Exempt/Nil Turnover &amp; Annual 5N Tie-Out</th>
            <th class="num-cell">Books Taxable (₹)</th>
            <th class="num-cell">GSTR-1 Taxable (₹)</th>
            <th class="num-cell">GSTR-3B Taxable (₹)</th>
            <th class="num-cell">Var (Books - 1)</th>
          </tr>
        </thead>
        <tbody>${renderT5Rows(t5)}</tbody>
      </table>
    `;
    return;
  }

  if (currentGstr9SubTab === "itc") {
    const headSum = (r) => Number(r.igst || 0) + Number(r.cgst || 0) + Number(r.sgst || 0) + Number(r.cess || 0);
    const renderHeadRows = (rows) => rows.map(r => {
      const cls = r.is_total ? "g9-table-total" : (r.is_subtotal ? "g9-table-total" : "");
      return `
        <tr class="${cls}">
          <td><strong>${escHtml(r.code)}</strong></td>
          <td>${escHtml(r.desc)}</td>
          <td class="num-cell">${formatINR(r.igst)}</td>
          <td class="num-cell">${formatINR(r.cgst)}</td>
          <td class="num-cell">${formatINR(r.sgst)}</td>
          <td class="num-cell">${formatINR(r.cess)}</td>
          <td class="num-cell"><strong>${formatINR(headSum(r))}</strong></td>
        </tr>
      `;
    }).join("");

    box.innerHTML = `
      <table class="ledger-table">
        <thead>
          <tr>
            <th>Row</th>
            <th>ITC Description (Tables 6, 7 &amp; 8)</th>
            <th class="num-cell">IGST (₹)</th>
            <th class="num-cell">CGST (₹)</th>
            <th class="num-cell">SGST (₹)</th>
            <th class="num-cell">Cess (₹)</th>
            <th class="num-cell">Total Tax (₹)</th>
          </tr>
        </thead>
        <tbody>
          <tr><td colspan="7" style="background:#f8fafc; font-weight:700; font-size:0.75rem;">TABLE 6 — DETAILS OF ITC AVAILED DURING THE FINANCIAL YEAR</td></tr>
          ${renderHeadRows(t.table_6 || [])}
          <tr><td colspan="7" style="background:#f8fafc; font-weight:700; font-size:0.75rem;">TABLE 7 — ITC REVERSED &amp; INELIGIBLE ITC FOR THE FINANCIAL YEAR</td></tr>
          ${renderHeadRows(t.table_7 || [])}
          <tr><td colspan="7" style="background:#f8fafc; font-weight:700; font-size:0.75rem;">TABLE 8 — COMPARISON OF ITC WITH AUTO-POPULATED GSTR-2B (8A - 8B - 8C = 8D)</td></tr>
          ${renderHeadRows(t.table_8 || [])}
        </tbody>
      </table>
    `;
    return;
  }

  if (currentGstr9SubTab === "tax") {
    const t9Rows = (t.table_9 || []).map(r => `
      <tr>
        <td><strong>${escHtml(r.code)}</strong></td>
        <td>${escHtml(r.head)}</td>
        <td class="num-cell">${formatINR(r.tax_payable)}</td>
        <td class="num-cell">${formatINR(r.paid_through_cash)}</td>
        <td class="num-cell">${formatINR(r.paid_through_itc)}</td>
        <td class="num-cell">${formatINR(r.total_paid)}</td>
        <td class="num-cell"><strong>${formatINR(r.shortfall_excess)}</strong></td>
      </tr>
    `).join("");

    const t10Rows = (t.tables_10_14 || []).map(r => `
      <tr>
        <td><strong>Table ${escHtml(r.code)}</strong></td>
        <td>${escHtml(r.desc)}</td>
        <td class="num-cell">${formatINR(r.taxable)}</td>
        <td class="num-cell">${formatINR(r.igst)}</td>
        <td class="num-cell">${formatINR(r.cgst)}</td>
        <td class="num-cell">${formatINR(r.sgst)}</td>
        <td class="num-cell">${formatINR(r.cess)}</td>
      </tr>
    `).join("");

    const s = currentGstr9Data.summary || {};
    box.innerHTML = `
      <table class="ledger-table">
        <thead>
          <tr>
            <th>Tax Head</th>
            <th>Description (Table 9)</th>
            <th class="num-cell">Tax Payable (₹)</th>
            <th class="num-cell">Paid via Cash (₹)</th>
            <th class="num-cell">Paid via ITC (₹)</th>
            <th class="num-cell">Total Paid (₹)</th>
            <th class="num-cell">Shortfall / Excess (₹)</th>
          </tr>
        </thead>
        <tbody>
          ${t9Rows}
          <tr class="g9-table-total">
            <td><strong>Int/LF</strong></td>
            <td>Interest &amp; Late Fee Reconciliation (Books vs Portal)</td>
            <td class="num-cell">Books: ${formatINR(s.books_interest_late_fee)}</td>
            <td class="num-cell" colspan="2">Portal Paid: ${formatINR(s.portal_interest_late_fee)}</td>
            <td class="num-cell" colspan="2">Variance: ${formatINR(s.interest_late_fee_diff)}</td>
          </tr>
        </tbody>
      </table>
      <table class="ledger-table" style="margin-top:16px;">
        <thead>
          <tr>
            <th>Table</th>
            <th>Part V: Next-FY Statutory Cut-Off Amendments (Apr - Nov)</th>
            <th class="num-cell">Taxable (₹)</th>
            <th class="num-cell">IGST (₹)</th>
            <th class="num-cell">CGST (₹)</th>
            <th class="num-cell">SGST (₹)</th>
            <th class="num-cell">Cess (₹)</th>
          </tr>
        </thead>
        <tbody>${t10Rows}</tbody>
      </table>
    `;
    return;
  }

  if (currentGstr9SubTab === "hsn") {
    const renderHsnRows = (rows, side) => {
      if (!rows || rows.length === 0) {
        return `<tr><td colspan="9" style="text-align:center; padding:24px; color:var(--text-tertiary);">No ${side} HSN line items found.</td></tr>`;
      }
      return rows.map(r => {
        const isOk = r.status === "VALID" || r.status === "COMPLIANT";
        const badgeCls = isOk ? "g9-badge-ok" : "g9-badge-err";
        const totalTax = r.total_tax !== undefined ? r.total_tax : (Number(r.igst || 0) + Number(r.cgst || 0) + Number(r.sgst || 0) + Number(r.cess || 0));
        const effRate = r.effective_rate !== undefined ? r.effective_rate : (Math.abs(Number(r.taxable || 0)) > 0 ? (Math.abs(totalTax) / Math.abs(Number(r.taxable))) * 100 : 0);
        return `
          <tr>
            <td><strong>${escHtml(side)}</strong></td>
            <td><span class="invoice-pill">${escHtml(r.hsn || "MISSING")}</span></td>
            <td>${escHtml(r.uqc)}</td>
            <td class="num-cell">${Number(r.qty || 0).toLocaleString("en-IN")}</td>
            <td class="num-cell">${Number(r.rate || 0).toFixed(1)}%</td>
            <td class="num-cell">${formatINR(r.taxable)}</td>
            <td class="num-cell">${formatINR(totalTax)}</td>
            <td class="num-cell">${Number(effRate).toFixed(2)}%</td>
            <td><span class="${badgeCls}" title="${escHtml(r.issues || "")}">${escHtml(r.issues || r.status)}</span></td>
          </tr>
        `;
      }).join("");
    };

    box.innerHTML = `
      <table class="ledger-table">
        <thead>
          <tr>
            <th>Table / Side</th>
            <th>HSN / SAC</th>
            <th>UQC</th>
            <th class="num-cell">Total Qty</th>
            <th class="num-cell">Declared Rate</th>
            <th class="num-cell">Taxable (₹)</th>
            <th class="num-cell">Total Tax (₹)</th>
            <th class="num-cell">Effective Rate</th>
            <th>Validation Status</th>
          </tr>
        </thead>
        <tbody>
          ${renderHsnRows(t.table_17 || [], "Table 17 (Outward)")}
          ${renderHsnRows(t.table_18 || [], "Table 18 (Inward)")}
        </tbody>
      </table>
    `;
    return;
  }

  // Reversals & Table 8D Drilldown
  const rev = currentGstr9Data.reversals_schedule || [];
  const d8 = currentGstr9Data.table_8d_drilldown || [];
  const revRows = rev.length ? rev.map(r => `
    <tr>
      <td><span class="g9-badge-warn">${escHtml(r.rule)}</span></td>
      <td><strong>${escHtml(r.party)}</strong><div class="vendor-gstin">${escHtml(r.gstin)}</div></td>
      <td>${escHtml(r.inv_no)}</td>
      <td>${escHtml(r.doc_date)}</td>
      <td class="num-cell">${formatINR(r.taxable)}</td>
      <td class="num-cell"><strong>${formatINR(r.tax_amount)}</strong></td>
      <td>${escHtml(r.reason)}</td>
    </tr>
  `).join("") : `<tr><td colspan="7" style="text-align:center; padding:18px; color:var(--text-tertiary);">No statutory ITC reversals flagged.</td></tr>`;

  const d8Rows = d8.length ? d8.map(r => `
    <tr>
      <td><span class="g9-badge-err">${escHtml(r.category)}</span></td>
      <td><strong>${escHtml(r.party)}</strong><div class="vendor-gstin">${escHtml(r.gstin)}</div></td>
      <td>${escHtml(r.inv_no)}</td>
      <td class="num-cell">${formatINR(r.books_tax)}</td>
      <td class="num-cell">${formatINR(r.portal_tax)}</td>
      <td class="num-cell"><strong>${formatINR(r.diff_tax)}</strong></td>
      <td>${escHtml(r.action)}</td>
    </tr>
  `).join("") : `<tr><td colspan="7" style="text-align:center; padding:18px; color:var(--text-tertiary);">Zero Table 8D unreconciled variance items.</td></tr>`;

  box.innerHTML = `
    <table class="ledger-table">
      <thead>
        <tr>
          <th>Reversal Rule</th>
          <th>Party / Ledger</th>
          <th>Invoice / Note</th>
          <th>Date</th>
          <th class="num-cell">Taxable (₹)</th>
          <th class="num-cell">Reversed Tax (₹)</th>
          <th>Audit Justification</th>
        </tr>
      </thead>
      <tbody>
        <tr><td colspan="7" style="background:#f8fafc; font-weight:700; font-size:0.75rem;">SEC 17(5) / SEC 17(1) / RULE 37 / RULE 42 REVERSAL SCHEDULE</td></tr>
        ${revRows}
      </tbody>
    </table>
    <table class="ledger-table" style="margin-top:16px;">
      <thead>
        <tr>
          <th>Table 8D Category</th>
          <th>Counterparty</th>
          <th>Invoice #</th>
          <th class="num-cell">Books Tax (₹)</th>
          <th class="num-cell">2B Tax (₹)</th>
          <th class="num-cell">Variance (₹)</th>
          <th>Recommended CA Action</th>
        </tr>
      </thead>
      <tbody>
        <tr><td colspan="7" style="background:#f8fafc; font-weight:700; font-size:0.75rem;">TABLE 8D VENDOR-WISE UNRECONCILED ITC DRILLDOWN</td></tr>
        ${d8Rows}
      </tbody>
    </table>
  `;
}

async function handleGstr9Export() {
  if (!currentGstr9Data) {
    alert("Please run the GSTR-9 Engine or Load 12M FY Sample first.");
    return;
  }
  const btn = document.getElementById("btnG9ExportExcel");
  const orig = btn ? btn.innerHTML : "";
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = "<span>Generating 8-Sheet Working Paper...</span>";
  }

  try {
    const res = await fetch("/api/gstr9/export", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(currentGstr9Data),
    });
    if (!res.ok) throw new Error("GSTR-9 Excel export failed on server.");
    const blob = await res.blob();
    const fy = ((currentGstr9Data.summary || {}).financial_year || "2025-26").replace("/", "-");
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `TallyWalls_GSTR9_Working_Paper_FY${fy}.xlsx`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    window.URL.revokeObjectURL(url);
  } catch (err) {
    alert("GSTR-9 Export Error: " + err.message);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = orig;
    }
  }
}

const G9_BUCKET_CONFIG = {
  sales: ["g9FileSales", "g9LabelSales", "g9RowSales", "No Sales Register attached"],
  gstr1: ["g9FileGstr1", "g9LabelGstr1", "g9RowGstr1", "No GSTR-1 export attached"],
  gstr3b: ["g9FileGstr3B", "g9LabelGstr3B", "g9RowGstr3B", "No GSTR-3B file attached (or use inputs below)"],
  purchase: ["g9FilePurchase", "g9LabelPurchase", "g9RowPurchase", "No Purchase Register attached"],
  gstr2b: ["g9File2B", "g9Label2B", "g9Row2B", "No GSTR-2B statement attached"],
};

function setG9AutoStatus(msg, isError = false) {
  const el = document.getElementById("g9AutoStatus");
  if (!el) return;
  el.textContent = msg || "";
  el.style.color = isError ? "#dc2626" : "#059669";
}

function _appendFilesToG9Bucket(bucket, filesToAdd) {
  const cfg = G9_BUCKET_CONFIG[bucket];
  if (!cfg || !filesToAdd || filesToAdd.length === 0) return 0;
  const [inId, lblId, rowId, emptyText] = cfg;
  const inp = document.getElementById(inId);
  if (!inp) return 0;

  const dt = new DataTransfer();
  const seen = new Set();
  if (inp.files) {
    for (let i = 0; i < inp.files.length; i++) {
      dt.items.add(inp.files[i]);
      seen.add(inp.files[i].name);
    }
  }
  let added = 0;
  for (const f of filesToAdd) {
    if (!seen.has(f.name)) {
      dt.items.add(f);
      seen.add(f.name);
      added++;
    }
  }
  inp.files = dt.files;
  updateG9FileRowUI(inId, lblId, rowId, emptyText);
  return added;
}

function _clientGuessG9Bucket(filename) {
  const fn = (filename || "").toLowerCase();
  if (fn.includes("3b") || fn.endsWith(".pdf")) return "gstr3b";
  if (fn.includes("gstr1") || fn.includes("gstr-1") || fn.includes("_r1_")) return "gstr1";
  if (fn.includes("2b") || fn.includes("r2b")) return "gstr2b";
  if (fn.includes("sales") || fn.includes("outward")) return "sales";
  if (fn.includes("purchase") || fn.includes("inward") || fn.includes("books")) return "purchase";
  return "purchase";
}

async function classifyAndAssignG9Files(fileList) {
  if (!fileList || fileList.length === 0) return;
  const files = Array.from(fileList);
  setG9AutoStatus(`Classifying ${files.length} file(s)...`);

  let bucketMap = {};
  try {
    const fd = new FormData();
    let totalBytes = 0;
    for (const f of files) {
      const gz = await _maybeGzipUpload(f);
      totalBytes += gz.size || 0;
      fd.append("files", gz);
    }
    if (!bridgeState.isCloudBackend || totalBytes <= 4.0 * 1024 * 1024) {
      const res = await safeFetchJSON("/api/gstr9/classify", { method: "POST", body: fd });
      for (const item of (res.classified || [])) {
        bucketMap[item.file_name] = item.bucket;
      }
    }
  } catch (e) {
    console.warn("Server classify fallback to client heuristics:", e);
  }

  const grouped = { sales: [], gstr1: [], gstr3b: [], purchase: [], gstr2b: [] };
  let skipped = 0;
  for (const f of files) {
    let b = bucketMap[f.name] || _clientGuessG9Bucket(f.name);
    if (!grouped[b]) {
      b = _clientGuessG9Bucket(f.name);
    }
    if (grouped[b]) grouped[b].push(f);
    else skipped++;
  }

  let totalAdded = 0;
  const summaryParts = [];
  for (const [b, arr] of Object.entries(grouped)) {
    if (arr.length > 0) {
      const cnt = _appendFilesToG9Bucket(b, arr);
      totalAdded += cnt;
      summaryParts.push(`${b.toUpperCase()}: ${arr.length}`);
    }
  }
  const msg = totalAdded > 0
    ? `Auto-sorted ${files.length} file(s) (${summaryParts.join(", ")})`
    : "Attached file(s) already present in buckets.";
  setG9AutoStatus(skipped ? `${msg} | ${skipped} unrecognized` : msg, false);
}

function clearAllGstr9Inputs() {
  for (const [inId, lblId, rowId, emptyText] of Object.values(G9_BUCKET_CONFIG)) {
    const inp = document.getElementById(inId);
    if (inp) {
      try { inp.value = ""; } catch (_) {}
      updateG9FileRowUI(inId, lblId, rowId, emptyText);
    }
  }
  setG9AutoStatus("Cleared attached GSTR-9 files.");
}

async function handleGstr9ScanDownloads() {
  if (bridgeState.mode === "cloud_web_only") {
    openConnectorModal("Start and pair TallyWalls Connector on your Windows PC to scan Downloads and auto-assign GSTR-1, GSTR-2B, GSTR-3B & Books files.");
    return;
  }
  const btn = document.getElementById("btnG9ScanDownloads");
  const orig = btn ? btn.innerHTML : "";
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = "<span>Scanning Downloads...</span>";
  }
  setG9AutoStatus("Scanning Downloads & cached statements...");

  try {
    const scanRes = await desktopFetchJSON("/api/gstr9/scan-downloads", "/bridge/gstr9/scan-downloads");
    const matches = scanRes.matches || [];
    if (matches.length === 0) {
      setG9AutoStatus("No GSTR-1, GSTR-2B, GSTR-3B, Sales or Purchase files found in Downloads.", true);
      return;
    }

    const batchRes = await desktopFetchJSON("/api/gstr9/load-local-batch", "/bridge/gstr9/load-local-batch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file_paths: matches.slice(0, 30).map(m => m.file_path) }),
    });
    const items = batchRes.items || [];
    const grouped = { sales: [], gstr1: [], gstr3b: [], purchase: [], gstr2b: [] };

    for (const item of items) {
      if (!item.content_b64 || !grouped[item.bucket]) continue;
      const binStr = atob(item.content_b64);
      const bytes = new Uint8Array(binStr.length);
      for (let i = 0; i < binStr.length; i++) bytes[i] = binStr.charCodeAt(i);
      const fileObj = new File([bytes], item.file_name, { type: "application/octet-stream" });
      grouped[item.bucket].push(fileObj);
    }

    const parts = [];
    let attached = 0;
    for (const [b, arr] of Object.entries(grouped)) {
      if (arr.length > 0) {
        attached += _appendFilesToG9Bucket(b, arr);
        parts.push(`${b.toUpperCase()}: ${arr.length}`);
      }
    }
    setG9AutoStatus(`Auto-assigned ${items.length} downloaded file(s) -> ${parts.join(", ")}`);
  } catch (err) {
    setG9AutoStatus("Downloads Auto-Assign failed: " + err.message, true);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = orig;
    }
  }
}

async function handleGstr9TallySync() {
  if (bridgeState.mode === "cloud_web_only") {
    openConnectorModal("Start and pair TallyWalls Connector on your Windows PC to pull full-year Sales, Purchases, Interest & Rule 37 data from Tally Prime (localhost:9000).");
    return;
  }

  const btn = document.getElementById("btnG9TallySync");
  const orig = btn ? btn.innerHTML : "";
  const fy = (document.getElementById("g9Fy") || {}).value || "2025-26";
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = "<span>Syncing FY from Tally...</span>";
  }
  setG9AutoStatus(`Connecting to Tally Prime (Port 9000) for FY ${fy}...`);

  try {
    const res = await desktopFetchJSON("/api/tally/sync-gstr9", "/bridge/tally/sync-gstr9", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fy, simulate: false }),
    });

    let attached = 0;
    if (res.sales_csv) {
      const fSales = new File([res.sales_csv], `Tally_Sales_Register_FY${fy}.csv`, { type: "text/csv" });
      attached += _appendFilesToG9Bucket("sales", [fSales]);
    }
    if (res.purchase_csv) {
      const fPur = new File([res.purchase_csv], `Tally_Purchase_Register_FY${fy}.csv`, { type: "text/csv" });
      attached += _appendFilesToG9Bucket("purchase", [fPur]);
    }
    if (res.books_interest_late_fee > 0) {
      const el = document.getElementById("g9_books_interest_fee");
      if (el) el.value = String(res.books_interest_late_fee);
    }
    if (res.rule37_tax > 0) {
      const el = document.getElementById("g9_rule37_tax");
      if (el) el.value = String(res.rule37_tax);
    }
    setG9AutoStatus(res.message || `Synced Tally FY ${fy} Registers.`);
    if (attached > 0) {
      await handleGstr9Compute();
    }
  } catch (err) {
    setG9AutoStatus("Tally Sync Notice: " + err.message, true);
    alert("Tally GSTR-9 Sync Notice: " + err.message);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = orig;
    }
  }
}

async function handleGstr9ExportJson() {
  if (!currentGstr9Data) {
    alert("Please run the GSTR-9 Engine or Load 12M FY Sample first.");
    return;
  }
  const btn = document.getElementById("btnG9ExportJson");
  const orig = btn ? btn.innerHTML : "";
  if (btn) {
    btn.disabled = true;
    btn.innerHTML = "<span>Generating JSON...</span>";
  }
  try {
    const res = await fetch("/api/gstr9/export-json", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(currentGstr9Data),
    });
    if (!res.ok) throw new Error("GSTR-9 GSTN JSON export failed on server.");
    const blob = await res.blob();
    const fy = ((currentGstr9Data.summary || {}).financial_year || "2025-26").replace("/", "-");
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `TallyWalls_GSTR9_GSTN_FY${fy}.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    window.URL.revokeObjectURL(url);
  } catch (err) {
    alert("GSTR-9 JSON Export Error: " + err.message);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.innerHTML = orig;
    }
  }
}

function setupGstr9Automation() {
  const dropZone = document.getElementById("g9SmartDropZone");
  const smartInp = document.getElementById("g9FileSmartMix");
  if (dropZone && smartInp) {
    dropZone.addEventListener("click", () => smartInp.click());
    ["dragenter", "dragover"].forEach(ev => {
      dropZone.addEventListener(ev, (e) => {
        e.preventDefault();
        e.stopPropagation();
        dropZone.classList.add("dragover");
      });
    });
    ["dragleave", "drop"].forEach(ev => {
      dropZone.addEventListener(ev, (e) => {
        e.preventDefault();
        e.stopPropagation();
        dropZone.classList.remove("dragover");
      });
    });
    dropZone.addEventListener("drop", (e) => {
      if (e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files.length > 0) {
        classifyAndAssignG9Files(e.dataTransfer.files);
      }
    });
    smartInp.addEventListener("change", () => {
      if (smartInp.files && smartInp.files.length > 0) {
        classifyAndAssignG9Files(smartInp.files);
      }
    });
  }

  const btnClear = document.getElementById("btnG9ClearAll");
  if (btnClear) btnClear.addEventListener("click", clearAllGstr9Inputs);

  const btnScan = document.getElementById("btnG9ScanDownloads");
  if (btnScan) btnScan.addEventListener("click", handleGstr9ScanDownloads);

  const btnTally = document.getElementById("btnG9TallySync");
  if (btnTally) btnTally.addEventListener("click", handleGstr9TallySync);
}
