// Save Your Shit — minimal Electron shell around the bundled `syt` engine.
//
// Responsibilities:
//   1. First run: `syt init` with a generated passphrase (cached in the macOS
//      Keychain by the engine) and save the Recovery Kit to the data folder.
//   2. Spawn `syt serve` on a free localhost port and show it in a window.
//   3. Native "Add Export…" (and drag-and-drop) -> `syt ingest`.
//
// Plain JS, no framework, no runtime deps beyond Electron itself.

const { app, BrowserWindow, Menu, dialog, shell, Notification } = require("electron");
const { spawn, spawnSync } = require("child_process");
const crypto = require("crypto");
const fs = require("fs");
const http = require("http");
const net = require("net");
const os = require("os");
const path = require("path");

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
    dialog.showMessageBoxSync({
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
    });
    return true;
  }

  // Keyring/encryption failed — fall back to an unencrypted archive rather
  // than a bricked first run.
  const res2 = runSyt(["init", "--no-encrypt"]);
  if (res2.ok) {
    dialog.showMessageBoxSync({
      type: "warning",
      title: "Encryption disabled",
      message: "Set up without encryption",
      detail:
        "Setting up an encrypted archive failed (the macOS Keychain was not " +
        "available), so your local archive was created unencrypted. Your data " +
        "still never leaves this machine.\n\nDetails:\n" +
        (res.stderr || res.stdout).trim().slice(-500),
      buttons: ["OK"],
    });
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
  if (ingestRunning || !p) return;
  ingestRunning = true;
  setMenu(); // disable the item while running
  const prevTitle = mainWindow ? mainWindow.getTitle() : null;
  if (mainWindow) mainWindow.setTitle("Save Your Shit — Backing up…");

  try {
    const res = await runSytAsync(["ingest", p, "--no-snapshot"]);
    if (res.ok) {
      const summary =
        (res.stdout.match(/Backed up .*$/m) || [res.stdout.trim() || "Done."])[0];
      if (mainWindow) mainWindow.webContents.reload();
      new Notification({ title: "Save Your Shit", body: summary }).show();
      await dialog.showMessageBox(mainWindow, {
        type: "info",
        title: "Backup complete",
        message: summary,
        buttons: ["OK"],
      });
    } else {
      await dialog.showMessageBox(mainWindow, {
        type: "error",
        title: "Backup failed",
        message: `Could not back up:\n${p}`,
        detail: (res.stderr || res.stdout).trim().slice(-800) || "unknown error",
        buttons: ["OK"],
      });
    }
  } finally {
    ingestRunning = false;
    if (mainWindow && prevTitle !== null) mainWindow.setTitle(prevTitle);
    setMenu();
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
          click: () => {
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
          },
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

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 800,
    title: "Save Your Shit",
    backgroundColor: "#0a0b0f",
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
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
  });

  // v1: quitting when the window closes is simpler than dock-lingering,
  // and guarantees the serve child never outlives the UI.
  app.on("window-all-closed", () => {
    stopServe();
    app.quit();
  });

  app.on("before-quit", () => {
    quitting = true;
  });

  app.on("will-quit", () => {
    quitting = true;
    stopServe();
  });
}
