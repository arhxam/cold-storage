// Save Your Shit — the Electron shell around the bundled `syt` engine.
//
// Responsibilities:
//   1. First run: `syt init` with a generated passphrase (cached in the macOS
//      Keychain by the engine) and save the Recovery Kit to the data folder.
//   2. Spawn `syt serve` on a free localhost port and show it in a window.
//   3. Accounts: sign in once per platform; the automation engine
//      (automation.js) requests, downloads, and ingests official exports on a
//      schedule — the app stays resident in the menu bar to do it.
//   4. Ingest of anything the user adds by hand (picker, drag-and-drop, or a
//      matching export appearing in ~/Downloads).
//
// Plain JS, no framework, no runtime deps beyond Electron itself.

const {
  app,
  BrowserWindow,
  Menu,
  Tray,
  dialog,
  shell,
  Notification,
  ipcMain,
  nativeImage,
} = require("electron");
const { spawn, spawnSync } = require("child_process");
const crypto = require("crypto");
const fs = require("fs");
const http = require("http");
const net = require("net");
const os = require("os");
const path = require("path");

const PLATFORMS = require("./platforms");
const automation = require("./automation");

// ---------------------------------------------------------------------------
// Paths
// ---------------------------------------------------------------------------

function sytBinary() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, "syt", "syt");
  }
  return path.join(__dirname, "..", "dist", "syt", "syt");
}

function sytHome() {
  return process.env.SYT_HOME || path.join(os.homedir(), "SaveYourShit");
}

function childEnv() {
  // Inherit the user's env; never set SYT_NO_KEYRING — the Keychain cache is
  // what makes later launches passwordless.
  return { ...process.env };
}

const stripAnsi = (s) => s.replace(/\x1b\[[0-9;]*m/g, "");

// Push a message to the web UI (no-op if the window is gone).
function sendRenderer(channel, payload) {
  if (mainWindow && !mainWindow.isDestroyed() && mainWindow.webContents) {
    mainWindow.webContents.send(channel, payload);
  }
}

// ---------------------------------------------------------------------------
// State
// ---------------------------------------------------------------------------

let mainWindow = null;
let serveChild = null;
let serveUrl = null;
let servePort = null;
let serveStderr = []; // rolling tail
let quitting = false;
let ingestRunning = false;
let tray = null;

// ---------------------------------------------------------------------------
// Settings — schedules, connection flags, prefs. Lives next to the data so it
// survives reinstalls and is visible to the user.
// ---------------------------------------------------------------------------

let settings = null;

function settingsPath() {
  return path.join(sytHome(), "app-settings.json");
}

function loadSettings() {
  if (settings) return settings;
  try {
    settings = JSON.parse(fs.readFileSync(settingsPath(), "utf8"));
  } catch {
    settings = {};
  }
  settings.accounts = settings.accounts || {};
  settings.prefs = settings.prefs || { launchAtLogin: false };
  settings.ingestedDownloads = settings.ingestedDownloads || [];
  return settings;
}

function saveSettings() {
  try {
    fs.mkdirSync(sytHome(), { recursive: true });
    fs.writeFileSync(settingsPath(), JSON.stringify(settings, null, 2));
  } catch {
    /* best effort */
  }
}

function getAccount(id) {
  const s = loadSettings();
  return s.accounts[id] || { schedule: "manual", connected: false };
}

function patchAccount(id, patch) {
  const s = loadSettings();
  s.accounts[id] = { ...(s.accounts[id] || { schedule: "manual", connected: false }), ...patch };
  saveSettings();
}

// ---------------------------------------------------------------------------
// Accounts payload + broadcast (renderer + tray stay in sync)
// ---------------------------------------------------------------------------

function accountsPayload() {
  return PLATFORMS.all().map((p) => {
    const a = getAccount(p.id);
    return {
      id: p.id,
      name: p.name,
      auto: !!p.auto,
      manualReason: p.manualReason || null,
      connected: !!a.connected,
      schedule: a.schedule || "manual",
      lastAttempt: a.lastAttempt || null,
      lastSuccess: a.lastSuccess || null,
      lastResult: a.lastResult || null,
      detail: a.detail || null,
      busy: automation.isBusy(p.id),
      attention: automation.hasAttention(p.id),
    };
  });
}

function broadcastAccounts() {
  sendRenderer("syt:accounts", accountsPayload());
  updateTrayMenu();
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function runSyt(args, timeoutMs = 120000) {
  // Synchronous helper for quick commands (init). No shell: args array.
  const res = spawnSync(sytBinary(), args, {
    env: childEnv(),
    encoding: "utf8",
    timeout: timeoutMs,
  });
  return {
    ok: res.status === 0,
    stdout: stripAnsi(res.stdout || ""),
    stderr: stripAnsi(res.stderr || ""),
  };
}

function runSytAsync(args) {
  return new Promise((resolve) => {
    const child = spawn(sytBinary(), args, { env: childEnv() });
    let out = "";
    let errOut = "";
    child.stdout.on("data", (d) => (out += d));
    child.stderr.on("data", (d) => (errOut += d));
    child.on("error", (e) => resolve({ ok: false, stdout: "", stderr: String(e) }));
    child.on("close", (code) =>
      resolve({ ok: code === 0, stdout: stripAnsi(out), stderr: stripAnsi(errOut) })
    );
  });
}

function freePort() {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.listen(0, "127.0.0.1", () => {
      const port = srv.address().port;
      srv.close(() => resolve(port));
    });
    srv.on("error", reject);
  });
}

function waitForHttp(url, timeoutMs = 30000) {
  const started = Date.now();
  return new Promise((resolve, reject) => {
    const attempt = () => {
      if (serveChild === null && Date.now() - started > 1000) {
        return reject(new Error("server process exited"));
      }
      const req = http.get(url, (res) => {
        res.resume();
        if (res.statusCode === 200) return resolve();
        retry();
      });
      req.on("error", retry);
      req.setTimeout(2000, () => {
        req.destroy();
        retry();
      });
    };
    const retry = () => {
      if (Date.now() - started > timeoutMs) {
        return reject(new Error("timed out waiting for the local server"));
      }
      setTimeout(attempt, 250);
    };
    attempt();
  });
}

// ---------------------------------------------------------------------------
// First run: init + Recovery Kit
// ---------------------------------------------------------------------------

// Set during first-run init, shown once the main window exists.
let pendingFirstRunNotice = null;

function flushFirstRunNotice() {
  if (!pendingFirstRunNotice) return;
  const notice = pendingFirstRunNotice;
  pendingFirstRunNotice = null;
  // Non-blocking: the app is already usable behind this.
  dialog.showMessageBox(mainWindow, notice).catch(() => {});
}

function ensureInitialized() {
  const home = sytHome();
  const configFile = path.join(home, "config.toml");
  if (fs.existsSync(configFile)) return true;

  const passphrase = crypto.randomBytes(16).toString("hex"); // 32 hex chars
  let res = runSyt(["init", "--passphrase", passphrase]);

  if (res.ok) {
    // Persist the Recovery Kit next to the data (and surface it once).
    const codeMatch = res.stdout.match(/([A-Z2-7]{4}(?:-[A-Z2-7]{4}){5,})/);
    const kitPath = path.join(home, "RECOVERY-KIT.txt");
    const body = [
      "Save Your Shit — Recovery Kit",
      "=============================",
      "",
      codeMatch ? `Recovery code: ${codeMatch[1]}` : "(code below, in the full output)",
      "",
      "Keep a copy of this somewhere safe that is NOT this computer.",
      "It is the ONLY way to recover your encrypted backups if this",
      "machine (and its Keychain) is lost.",
      "",
      "--- full `syt init` output ---",
      res.stdout,
    ].join("\n");
    try {
      fs.writeFileSync(kitPath, body, { mode: 0o600 });
    } catch {
      /* best effort */
    }
    // Show this AFTER the window is up (see flushFirstRunNotice) so the app
    // never sits behind a modal with nothing painted underneath it.
    pendingFirstRunNotice = {
      type: "info",
      title: "Recovery Kit",
      message: "Your archive is encrypted. Save your Recovery Kit now.",
      detail:
        `A Recovery Kit was written to:\n${kitPath}\n\n` +
        (codeMatch ? `Recovery code:\n${codeMatch[1]}\n\n` : "") +
        "Copy it somewhere safe that is not this computer — it is the only " +
        "way to recover your data if this Mac is lost. Your encryption key " +
        "is stored in the macOS Keychain, so you never need to type a passphrase.",
      buttons: ["I saved it"],
    };
    return true;
  }

  // Keyring/encryption failed — fall back to an unencrypted archive rather
  // than a bricked first run.
  const res2 = runSyt(["init", "--no-encrypt"]);
  if (res2.ok) {
    pendingFirstRunNotice = {
      type: "warning",
      title: "Encryption disabled",
      message: "Set up without encryption",
      detail:
        "Setting up an encrypted archive failed (the macOS Keychain was not " +
        "available), so your local archive was created unencrypted. Your data " +
        "still never leaves this machine.\n\nDetails:\n" +
        (res.stderr || res.stdout).trim().slice(-500),
      buttons: ["OK"],
    };
    return true;
  }

  dialog.showErrorBox(
    "Setup failed",
    `Could not initialize the archive at ${home}.\n\n` +
      (res2.stderr || res.stderr || "unknown error").trim().slice(-800)
  );
  return false;
}

// ---------------------------------------------------------------------------
// Server lifecycle
// ---------------------------------------------------------------------------

async function startServe() {
  servePort = await freePort();
  serveUrl = `http://127.0.0.1:${servePort}/`;
  serveStderr = [];

  serveChild = spawn(
    sytBinary(),
    ["serve", "--no-open", "--port", String(servePort), "--host", "127.0.0.1"],
    { env: childEnv() }
  );
  const child = serveChild;

  child.stderr.on("data", (d) => {
    for (const line of String(d).split("\n")) {
      if (line.trim()) serveStderr.push(stripAnsi(line));
    }
    if (serveStderr.length > 200) serveStderr = serveStderr.slice(-200);
  });

  child.on("close", (code) => {
    if (child !== serveChild) return; // an old child we already replaced
    serveChild = null;
    if (quitting) return;
    handleServeExit(code);
  });

  await waitForHttp(serveUrl);
}

function stopServe() {
  if (serveChild) {
    const c = serveChild;
    serveChild = null;
    try {
      c.kill("SIGTERM");
    } catch {
      /* already gone */
    }
  }
}

function handleServeExit(code) {
  const tail = serveStderr.slice(-20).join("\n");
  const locked = /locked/i.test(tail);
  if (locked) {
    // Archive exists but we can't unlock it (e.g. created by the CLI on
    // another account, Keychain entry missing). Explain; don't crash-loop.
    dialog.showMessageBoxSync({
      type: "error",
      title: "Archive locked",
      message: "Your archive is encrypted and could not be unlocked.",
      detail:
        "The encryption key was not found in the macOS Keychain. If you set " +
        "this archive up with the `syt` command line, open it once from " +
        "Terminal so the key gets cached:\n\n    syt status\n\n(enter your " +
        "passphrase when prompted), then relaunch this app.\n\n" +
        tail.slice(-400),
      buttons: ["Quit"],
    });
    app.quit();
    return;
  }
  const choice = dialog.showMessageBoxSync({
    type: "error",
    title: "Save Your Shit",
    message: `The local server stopped unexpectedly (exit code ${code}).`,
    detail: tail || "(no error output)",
    buttons: ["Restart", "Quit"],
    defaultId: 0,
  });
  if (choice === 0) {
    startServe()
      .then(() => {
        if (mainWindow) mainWindow.loadURL(serveUrl);
      })
      .catch((e) => {
        dialog.showErrorBox("Save Your Shit", `Could not restart the server: ${e.message}`);
        app.quit();
      });
  } else {
    app.quit();
  }
}

// ---------------------------------------------------------------------------
// Ingest
// ---------------------------------------------------------------------------

async function ingestPath(p) {
  if (ingestRunning || !p) return false;
  ingestRunning = true;
  setMenu(); // disable the item while running
  // Drive the in-app UI: a live toast + an in-place refresh when done. The web
  // UI owns the visible feedback now, so we no longer block on native dialogs.
  sendRenderer("syt:ingest", { phase: "start", path: p });
  if (mainWindow) mainWindow.setTitle("Save Your Shit — Backing up…");

  try {
    const res = await runSytAsync(["ingest", p, "--no-snapshot"]);
    if (res.ok) {
      const summary =
        (res.stdout.match(/Backed up .*$/m) || [res.stdout.trim() || "Done."])[0];
      new Notification({ title: "Save Your Shit", body: summary }).show();
      sendRenderer("syt:ingest", { phase: "done", summary });
      return true;
    }
    const detail = (res.stderr || res.stdout).trim().slice(-800) || "unknown error";
    sendRenderer("syt:ingest", { phase: "error", error: detail, path: p });
    if (!mainWindow) {
      dialog.showErrorBox("Backup failed", `Could not back up:\n${p}\n\n${detail}`);
    }
    return false;
  } finally {
    ingestRunning = false;
    if (mainWindow) mainWindow.setTitle("Save Your Shit");
    setMenu();
  }
}

// ---------------------------------------------------------------------------
// Scheduler — the thing that makes this hands-off.
//
// Every SCHEDULER_TICK we ask: is any connected account due? "Due" means the
// interval has elapsed since the last *attempt*. A pass either downloads a
// ready archive or (re)files the request, so polling naturally picks up an
// export the platform prepared hours later. Catch-up runs on launch, which is
// what makes "the app opens daily and gets it done" true even for a laptop
// that was asleep.
// ---------------------------------------------------------------------------

const SCHEDULER_TICK = 30 * 60 * 1000; // 30 min
const INTERVALS = {
  daily: 24 * 60 * 60 * 1000,
  weekly: 7 * 24 * 60 * 60 * 1000,
  monthly: 30 * 24 * 60 * 60 * 1000,
};
// A pending request is re-checked on this cadence regardless of schedule —
// platforms take hours to prepare an archive and we want it the moment it lands.
const PENDING_RECHECK = 3 * 60 * 60 * 1000; // 3h
let schedulerTimer = null;

function dueAt(acct) {
  const every = INTERVALS[acct.schedule];
  if (!every) return null; // manual
  const last = acct.lastAttempt ? Date.parse(acct.lastAttempt) : 0;
  if (!last) return 0; // never run -> due now
  const waiting = acct.lastResult === "requested" || acct.lastResult === "downloading";
  return last + (waiting ? Math.min(PENDING_RECHECK, every) : every);
}

async function runDueSyncs() {
  const now = Date.now();
  for (const p of PLATFORMS.auto()) {
    const a = getAccount(p.id);
    if (!a.connected || automation.isBusy(p.id) || automation.hasAttention(p.id)) continue;
    const due = dueAt(a);
    if (due === null || now < due) continue;
    // Sequential on purpose: several hidden browser windows at once is heavy,
    // and platforms are happier with one session doing one thing.
    await automation.runSync(p.id, { interactive: false, surfaceOnAttention: false });
  }
}

function startScheduler() {
  if (schedulerTimer) clearInterval(schedulerTimer);
  schedulerTimer = setInterval(() => {
    runDueSyncs().catch(() => {});
  }, SCHEDULER_TICK);
  // Catch-up shortly after launch (let the UI settle first).
  setTimeout(() => runDueSyncs().catch(() => {}), 20000);
}

// ---------------------------------------------------------------------------
// Downloads watcher — if you download an export yourself (or a platform only
// delivers by email link), dropping it in ~/Downloads is enough.
// ---------------------------------------------------------------------------

const EXPORT_RE =
  /(instagram|facebook|twitter|^x-|discord|telegram|whatsapp|reddit|snapchat|linkedin|slack|takeout|your[-_]?data|data[-_]?export)/i;
let downloadsWatcher = null;

function watchDownloads() {
  const dir = path.join(os.homedir(), "Downloads");
  if (!fs.existsSync(dir)) return;
  const seen = new Set(loadSettings().ingestedDownloads);
  const consider = (name) => {
    if (!name || !name.toLowerCase().endsWith(".zip")) return;
    if (!EXPORT_RE.test(name) || seen.has(name)) return;
    const full = path.join(dir, name);
    let size = -1;
    try {
      size = fs.statSync(full).size;
    } catch {
      return;
    }
    // Wait for the file to stop growing before touching it.
    setTimeout(async () => {
      let now = -1;
      try {
        now = fs.statSync(full).size;
      } catch {
        return;
      }
      if (now !== size || now === 0 || seen.has(name)) return;
      seen.add(name);
      const s = loadSettings();
      s.ingestedDownloads = [...seen].slice(-200);
      saveSettings();
      await ingestPath(full);
    }, 5000);
  };
  try {
    downloadsWatcher = fs.watch(dir, (_ev, name) => consider(name));
  } catch {
    /* watching is a bonus, not a requirement */
  }
}

// ---------------------------------------------------------------------------
// Menu bar presence — the app keeps running (and syncing) after you close the
// window. That is the whole point of "it just happens in the background".
// ---------------------------------------------------------------------------

function trayIcon() {
  // A 16pt template shield, drawn inline so we ship no extra asset.
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="black" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round"><path d="M12 3l7 3v5c0 4.5-3 8-7 10-4-2-7-5.5-7-10V6z"/><path d="M9 12l2 2 4-4"/></svg>`;
  const img = nativeImage.createFromDataURL(
    "data:image/svg+xml;base64," + Buffer.from(svg).toString("base64")
  );
  img.setTemplateImage(true); // follows light/dark menu bar automatically
  return img;
}

function updateTrayMenu() {
  if (!tray) return;
  const accts = accountsPayload().filter((a) => a.connected);
  const items = [
    { label: "Open Save Your Shit", click: () => showMainWindow() },
    { type: "separator" },
  ];
  if (accts.length) {
    items.push({ label: "Accounts", enabled: false });
    for (const a of accts) {
      const state = a.busy
        ? "syncing…"
        : a.attention || a.lastResult === "attention" || a.lastResult === "reconnect"
          ? "needs a click"
          : a.lastResult === "requested"
            ? "waiting on export"
            : a.lastSuccess
              ? "backed up"
              : "connected";
      items.push({
        label: `   ${a.name} — ${state}`,
        click: () => {
          showMainWindow();
          automation.runSync(a.id, { interactive: true });
        },
      });
    }
    items.push(
      { type: "separator" },
      {
        label: "Back Up All Now",
        click: () => syncAll(),
      }
    );
  } else {
    items.push({ label: "No accounts connected yet", enabled: false });
  }
  items.push(
    { type: "separator" },
    { label: "Add Export…", click: () => pickAndIngest() },
    { label: "Reveal Data Folder", click: () => shell.showItemInFolder(sytHome()) },
    { type: "separator" },
    {
      label: "Quit Save Your Shit",
      click: () => {
        quitting = true;
        app.quit();
      },
    }
  );
  tray.setContextMenu(Menu.buildFromTemplate(items));
}

function createTray() {
  if (tray) return;
  try {
    tray = new Tray(trayIcon());
    tray.setToolTip("Save Your Shit");
    tray.on("click", () => tray.popUpContextMenu());
    updateTrayMenu();
  } catch {
    /* tray is a nicety; the app still works without it */
  }
}

async function syncAll() {
  for (const p of PLATFORMS.auto()) {
    if (getAccount(p.id).connected) {
      await automation.runSync(p.id, { interactive: false, surfaceOnAttention: false });
    }
  }
}

function showMainWindow() {
  if (mainWindow && !mainWindow.isDestroyed()) {
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
  } else if (serveUrl) {
    createWindow();
  }
}

function showRecoveryKit() {
  const kit = path.join(sytHome(), "RECOVERY-KIT.txt");
  if (fs.existsSync(kit)) {
    shell.openPath(kit);
  } else {
    dialog.showMessageBox(mainWindow, {
      type: "info",
      title: "Recovery Kit",
      message: "No Recovery Kit file found.",
      detail:
        `Expected at:\n${kit}\n\nIf you set up with the CLI, your ` +
        "recovery code was shown during `syt init`.",
      buttons: ["OK"],
    });
  }
}

async function pickAndIngest() {
  const res = await dialog.showOpenDialog(mainWindow, {
    title: "Add a data export",
    message: "Pick a downloaded export (.zip) or an unzipped export folder",
    properties: ["openFile", "openDirectory"],
    filters: [
      { name: "Exports", extensions: ["zip"] },
      { name: "All Files", extensions: ["*"] },
    ],
  });
  if (res.canceled || res.filePaths.length === 0) return;
  await ingestPath(res.filePaths[0]);
}

// ---------------------------------------------------------------------------
// Menu
// ---------------------------------------------------------------------------

function setMenu() {
  const template = [
    { role: "appMenu" },
    {
      label: "File",
      submenu: [
        {
          label: "Add Export…",
          accelerator: "CmdOrCtrl+O",
          enabled: !ingestRunning,
          click: () => pickAndIngest(),
        },
        { type: "separator" },
        { role: "close" },
      ],
    },
    {
      label: "Archive",
      submenu: [
        {
          label: "Reveal Data Folder",
          click: () => shell.showItemInFolder(sytHome()),
        },
        {
          label: "Show Recovery Kit",
          click: () => showRecoveryKit(),
        },
      ],
    },
    { role: "editMenu" },
    { role: "viewMenu" },
    { role: "windowMenu" },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

// ---------------------------------------------------------------------------
// Window
// ---------------------------------------------------------------------------

// Expose the small native surface the web UI drives. Registered once.
function registerIpc() {
  ipcMain.handle("syt:pickAndIngest", () => pickAndIngest());
  ipcMain.handle("syt:revealDataFolder", () => shell.showItemInFolder(sytHome()));
  ipcMain.handle("syt:showRecoveryKit", () => showRecoveryKit());

  // --- accounts / automation --------------------------------------------
  ipcMain.handle("syt:accounts", () => accountsPayload());
  ipcMain.handle("syt:connect", async (_e, id) => {
    const r = await automation.connect(String(id));
    broadcastAccounts();
    if (r.connected) {
      // A freshly connected account should start working immediately.
      const a = getAccount(id);
      if (a.schedule === "manual") patchAccount(id, { schedule: "weekly" });
      automation.runSync(String(id), { interactive: false, surfaceOnAttention: false });
    }
    return r;
  });
  ipcMain.handle("syt:disconnect", async (_e, id) => {
    await automation.disconnect(String(id));
    return true;
  });
  ipcMain.handle("syt:setSchedule", (_e, id, schedule) => {
    const ok = ["manual", "daily", "weekly", "monthly"].includes(schedule);
    patchAccount(String(id), { schedule: ok ? schedule : "manual" });
    broadcastAccounts();
    return true;
  });
  ipcMain.handle("syt:syncNow", (_e, id) => {
    automation.runSync(String(id), { interactive: false, surfaceOnAttention: true });
    return true;
  });
  ipcMain.handle("syt:syncAll", () => {
    syncAll();
    return true;
  });
  ipcMain.handle("syt:getPrefs", () => ({
    ...loadSettings().prefs,
    launchAtLogin: app.getLoginItemSettings().openAtLogin,
  }));
  ipcMain.handle("syt:setPref", (_e, key, value) => {
    const s = loadSettings();
    s.prefs[key] = value;
    saveSettings();
    if (key === "launchAtLogin") {
      app.setLoginItemSettings({ openAtLogin: !!value, openAsHidden: true });
    }
    return true;
  });
  ipcMain.handle("syt:openExternal", (_e, url) => {
    // Only ever open https:// links (a platform's data-download page) in the
    // real browser — never file://, never a local command.
    try {
      const u = new URL(String(url));
      if (u.protocol === "https:") return shell.openExternal(u.href);
    } catch {
      /* malformed url — ignore */
    }
    return false;
  });
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 800,
    minWidth: 900,
    minHeight: 640,
    title: "Save Your Shit",
    backgroundColor: "#100f0d",
    // Fold the window controls into the app itself: the traffic lights float
    // over our own top bar instead of sitting in a separate OS strip.
    titleBarStyle: "hiddenInset",
    trafficLightPosition: { x: 18, y: 15 },
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
      preload: path.join(__dirname, "preload.js"),
    },
  });

  // Drag-and-drop of a file/folder onto the window triggers a file:// navigation;
  // intercept it and ingest instead.
  mainWindow.webContents.on("will-navigate", (event, url) => {
    if (url.startsWith("file://")) {
      event.preventDefault();
      try {
        const p = decodeURIComponent(new URL(url).pathname);
        ingestPath(p);
      } catch {
        /* ignore malformed drops */
      }
    }
  });

  mainWindow.on("closed", () => {
    mainWindow = null;
  });

  mainWindow.loadURL(serveUrl);
  // Surface the first-run Recovery Kit once there's a window behind it.
  mainWindow.webContents.once("did-finish-load", flushFirstRunNotice);
}

// ---------------------------------------------------------------------------
// App lifecycle
// ---------------------------------------------------------------------------

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });

  app.whenReady().then(async () => {
    const bin = sytBinary();
    if (!fs.existsSync(bin)) {
      dialog.showErrorBox(
        "Save Your Shit",
        `The bundled engine is missing:\n${bin}\n\nReinstall the app.`
      );
      app.quit();
      return;
    }

    if (!ensureInitialized()) {
      app.quit();
      return;
    }

    loadSettings();
    automation.init({
      sytHome,
      ingest: (p) => ingestPath(p),
      getAccount,
      patchAccount,
      broadcast: broadcastAccounts,
    });
    registerIpc();
    setMenu();

    try {
      await startServe();
    } catch (e) {
      if (serveChild !== null || serveStderr.length === 0) {
        // Timed out (exit is handled by handleServeExit otherwise).
        dialog.showErrorBox(
          "Save Your Shit",
          `The local server did not start: ${e.message}\n\n` +
            serveStderr.slice(-20).join("\n")
        );
        stopServe();
        app.quit();
      }
      return;
    }

    createWindow();
    createTray();
    startScheduler();
    watchDownloads();
  });

  app.on("activate", () => showMainWindow());

  // Closing the window no longer quits: the scheduler has to keep running for
  // backups to happen on their own. The tray is the visible "still here", and
  // Quit from there (or Cmd-Q) is the real exit.
  app.on("window-all-closed", () => {
    if (process.platform !== "darwin") {
      stopServe();
      app.quit();
    }
  });

  app.on("before-quit", () => {
    quitting = true;
  });

  app.on("will-quit", () => {
    quitting = true;
    stopServe();
  });
}
