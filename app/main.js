// Cold Storage — the Electron shell around the bundled `cold` engine.
//
// Responsibilities:
//   1. First run: `cold init` with a generated passphrase (cached in the macOS
//      Keychain by the engine) and save the Recovery Kit to the data folder.
//   2. Spawn `cold serve` on a free localhost port and show it in a window.
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
  powerMonitor,
  powerSaveBlocker,
  screen,
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
const { Store } = require("./store");
const { IngestQueue } = require("./queue");

// ---------------------------------------------------------------------------
// Paths
// ---------------------------------------------------------------------------

function coldBinary() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, "cold", "cold");
  }
  return path.join(__dirname, "..", "dist", "cold", "cold");
}

function coldHome() {
  return process.env.COLD_HOME || path.join(os.homedir(), "ColdStorage");
}

/** Where the Recovery Kit goes: visible to the user, outside what gets synced. */
function recoveryKitDir() {
  const desktop = path.join(os.homedir(), "Desktop");
  try {
    if (fs.existsSync(desktop)) return desktop;
  } catch {
    /* fall through */
  }
  return os.homedir();
}

function childEnv() {
  // Inherit the user's env; never set COLD_NO_KEYRING — the Keychain cache is
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

let store = null;
let queue = null;

let warnedWriteFailure = false;

function loadSettings() {
  if (!store) {
    store = new Store(coldHome());
    store.onWriteError = (msg, count) => {
      logCrash("settings-write", msg);
      if (count >= 2 && !warnedWriteFailure) {
        warnedWriteFailure = true;
        notify(
          "Cold Storage can’t save its settings",
          "Your disk may be full or read-only. Schedules may be lost on restart."
        );
      }
    };
    const s = store.load();
    // A crash or a quit mid-sync leaves transient states on disk. Left alone
    // they make the UI lie ("Working…" forever) and confuse the scheduler's
    // idea of when the account is next due. Normalize once, at startup.
    let changed = false;
    for (const [id, a] of Object.entries(s.accounts)) {
      if (a && (a.lastResult === "running" || a.lastResult === "downloading")) {
        s.accounts[id] = { ...a, lastResult: null, detail: null };
        changed = true;
      }
    }
    if (changed) store.save();
  }
  return store.load();
}

function saveSettings() {
  if (store) store.save();
}

function getAccount(id) {
  loadSettings();
  return store.account(id);
}

function patchAccount(id, patch) {
  loadSettings();
  store.patchAccount(id, patch);
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
  sendRenderer("cold:accounts", accountsPayload());
  updateTrayMenu();
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function runSyt(args, timeoutMs = 120000) {
  // Synchronous helper for quick commands (init). No shell: args array.
  const res = spawnSync(coldBinary(), args, {
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

// Cap what we keep from a child we do not control: a runaway engine must not
// grow the main process's heap until it is killed.
const MAX_CHILD_OUTPUT = 256 * 1024;
const clip = (s, add) => {
  const next = s + add;
  return next.length > MAX_CHILD_OUTPUT ? next.slice(-MAX_CHILD_OUTPUT) : next;
};

function runSytAsync(args, timeoutMs = 2 * 60 * 60 * 1000) {
  return new Promise((resolve) => {
    let child;
    try {
      child = spawn(coldBinary(), args, { env: childEnv() });
    } catch (e) {
      return resolve({ ok: false, stdout: "", stderr: String(e) });
    }
    let out = "";
    let errOut = "";
    let settled = false;
    const done = (v) => {
      if (settled) return;
      settled = true;
      clearTimeout(killer);
      resolve(v);
    };
    // A wedged child (a keychain prompt, a stuck read on a network volume)
    // would otherwise leave this promise pending forever and stall the queue.
    const killer = setTimeout(() => {
      try {
        child.kill("SIGKILL");
      } catch {
        /* already gone */
      }
      done({ ok: false, stdout: stripAnsi(out), stderr: "timed out after " + timeoutMs + "ms" });
    }, timeoutMs);
    if (killer.unref) killer.unref();

    child.stdout.on("data", (d) => (out = clip(out, d)));
    child.stderr.on("data", (d) => (errOut = clip(errOut, d)));
    child.on("error", (e) => done({ ok: false, stdout: "", stderr: String(e) }));
    child.on("close", (code) =>
      done({ ok: code === 0, stdout: stripAnsi(out), stderr: stripAnsi(errOut) })
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
      // One probe must schedule at most ONE retry. Without this guard,
      // req.destroy() also emits 'error', so a timed-out probe retried twice
      // and the in-flight probe count doubled every couple of seconds.
      let settled = false;
      const once = () => {
        if (settled) return;
        settled = true;
        retry();
      };
      const req = http.get(url, (res) => {
        res.resume();
        if (res.statusCode === 200) {
          settled = true;
          return resolve();
        }
        once();
      });
      req.on("error", once);
      req.setTimeout(2000, () => {
        req.destroy();
        once();
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
  const home = coldHome();
  const configFile = path.join(home, "config.toml");
  if (fs.existsSync(configFile)) return true;

  const passphrase = crypto.randomBytes(16).toString("hex"); // 32 hex chars
  let res = runSyt(["init", "--passphrase", passphrase]);

  if (res.ok) {
    // Persist the Recovery Kit next to the data (and surface it once).
    const codeMatch = res.stdout.match(/([A-Z2-7]{4}(?:-[A-Z2-7]{4}){5,})/);
    // Deliberately NOT inside the archive folder. That folder is what cloud
    // sync uploads, and this file holds the master key in the clear — putting
    // it there would hand the key to anyone who reached the backup. It also
    // belongs somewhere the user will actually see it.
    const kitPath = path.join(recoveryKitDir(), "Cold Storage — Recovery Kit.txt");
    const body = [
      "Cold Storage — Recovery Kit",
      "=============================",
      "",
      codeMatch ? `Recovery code: ${codeMatch[1]}` : "(code below, in the full output)",
      "",
      "Keep a copy of this somewhere safe that is NOT this computer.",
      "It is the ONLY way to recover your encrypted backups if this",
      "machine (and its Keychain) is lost.",
      "",
      "--- full `cold init` output ---",
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
    coldBinary(),
    ["serve", "--no-open", "--port", String(servePort), "--host", "127.0.0.1"],
    // stdout is discarded rather than piped: nothing reads it, and a full
    // 64 KB pipe buffer would block the server process forever.
    { env: childEnv(), stdio: ["ignore", "ignore", "pipe"] }
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
    // Escalate: a child that ignores SIGTERM would survive as an orphan
    // holding the port and the archive lock, and the next launch would report
    // the archive as un-unlockable.
    const hard = setTimeout(() => {
      try {
        if (c.exitCode === null && c.signalCode === null) c.kill("SIGKILL");
      } catch {
        /* already gone */
      }
    }, 3000);
    if (hard.unref) hard.unref();
  }
}

// The viewer server dying must never take the app (or the scheduler) down: an
// unattended tray app cannot answer a modal dialog. Restart it quietly with
// backoff and only tell the user if it truly will not come back.
let serveRestarts = 0;
let serveRestartTimer = null;

function handleServeExit(code) {
  if (quitting) return;
  const tail = serveStderr.slice(-20).join("\n");

  if (/locked/i.test(tail)) {
    // The archive exists but cannot be unlocked; restarting will not help.
    // Backups still work, so keep running and explain when there is a window.
    const detail =
      "The encryption key was not found in the macOS Keychain. Open the " +
      "archive once from Terminal so the key gets cached:\n\n    cold status\n\n" +
      "then relaunch this app.";
    if (mainWindow && !mainWindow.isDestroyed()) {
      dialog.showMessageBox(mainWindow, {
        type: "error",
        title: "Archive locked",
        message: "Your archive is encrypted and could not be unlocked.",
        detail: detail + "\n\n" + tail.slice(-400),
        buttons: ["OK"],
      }).catch(() => {});
    } else {
      notify("Archive locked", "Open Cold Storage to finish unlocking your archive.");
    }
    return;
  }

  serveRestarts++;
  if (serveRestarts > 6) {
    notify("Cold Storage", "The archive viewer keeps stopping. Reopen the app to retry.");
    return; // give up on the VIEWER only — syncing continues regardless
  }
  const delay = Math.min(30000, 1000 * Math.pow(2, serveRestarts));
  if (serveRestartTimer) clearTimeout(serveRestartTimer);
  serveRestartTimer = setTimeout(() => {
    serveRestartTimer = null;
    startServe()
      .then(() => {
        serveRestarts = 0;
        if (mainWindow && !mainWindow.isDestroyed()) mainWindow.loadURL(serveUrl);
      })
      .catch(() => {
        /* handleServeExit fires again and backs off further */
      });
  }, delay);
  if (serveRestartTimer.unref) serveRestartTimer.unref();
}

// ---------------------------------------------------------------------------
// Ingest
// ---------------------------------------------------------------------------

function incomingDir() {
  return path.join(coldHome(), "incoming");
}

/**
 * Hand a file to the durable queue. Never drops work: two exports finishing at
 * the same second both get backed up, and a crash mid-ingest is retried on the
 * next launch.
 */
function ingestPath(p, meta = {}) {
  if (!p) return Promise.resolve(false);
  loadSettings();
  return queue.add(p, meta);
}

// The queue reports progress; this turns it into UI + notifications.
function onQueueEvent(ev) {
  ingestRunning = queue ? !!queue.current : false;
  setMenu();
  if (ev.phase === "start") {
    sendRenderer("cold:ingest", { phase: "start", path: ev.path });
    if (mainWindow && !mainWindow.isDestroyed()) {
      mainWindow.setTitle("Cold Storage — Backing up…");
    }
  } else if (ev.phase === "done") {
    notify("Cold Storage", ev.summary);
    sendRenderer("cold:ingest", { phase: "done", summary: ev.summary });
    if (mainWindow && !mainWindow.isDestroyed()) mainWindow.setTitle("Cold Storage");
  } else if (ev.phase === "retry") {
    sendRenderer("cold:ingest", {
      phase: "retry",
      error: ev.error,
      attempts: ev.attempts,
      path: ev.path,
    });
  } else if (ev.phase === "error") {
    if (ev.permanent) permanentFailures.add(ev.path);
    sendRenderer("cold:ingest", {
      phase: "error",
      error: ev.error,
      path: ev.path,
      permanent: !!ev.permanent,
      name: path.basename(ev.path),
    });
    // No window to see the toast? Say it where the user will notice, without
    // a modal that would freeze an unattended app. For a permanent failure the
    // reason IS the message — "re-download choosing JSON" is the whole fix.
    if (!mainWindow || mainWindow.isDestroyed()) {
      notify(
        ev.permanent ? `Can't read ${path.basename(ev.path)}` : "Backup failed",
        ev.permanent ? ev.error : `${path.basename(ev.path)} could not be backed up.`
      );
    }
    if (mainWindow && !mainWindow.isDestroyed()) mainWindow.setTitle("Cold Storage");
  }
  broadcastAccounts();
}

function notify(title, body) {
  try {
    if (Notification.isSupported()) new Notification({ title, body: String(body || "") }).show();
  } catch {
    /* notifications are optional */
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

const SCHEDULER_TICK = 15 * 60 * 1000; // 15 min
const INTERVALS = {
  daily: 24 * 60 * 60 * 1000,
  weekly: 7 * 24 * 60 * 60 * 1000,
  monthly: 30 * 24 * 60 * 60 * 1000,
};
// A pending request is re-checked on this cadence regardless of schedule —
// platforms take hours to prepare an archive and we want it the moment it lands.
const PENDING_RECHECK = 2 * 60 * 60 * 1000; // 2h
const MAX_BACKOFF = 24 * 60 * 60 * 1000;
let schedulerTimer = null;
let schedulerRunning = false; // a pass can outlive a tick; never overlap

/**
 * When is this account next due?
 *
 * Deliberately based on wall-clock timestamps rather than elapsed timer ticks:
 * a machine that was asleep (or shut down) for a week comes back and is
 * immediately due, which is what makes "it just keeps happening" true.
 */
function dueAt(acct) {
  const every = INTERVALS[acct.schedule];
  if (!every) return null; // manual — only on request
  const last = acct.lastAttempt ? Date.parse(acct.lastAttempt) : 0;
  if (!last || Number.isNaN(last)) return 0; // never run -> due now
  // A clock that jumped backwards (timezone change, NTP correction) must not
  // park an account in the far future.
  if (last > Date.now() + 60 * 60 * 1000) return 0;

  const failures = acct.failures || 0;
  if (failures > 0) {
    // Exponential backoff so a broken platform is not hammered every tick:
    // 15m, 30m, 1h, 2h … capped at a day, and never shorter than the schedule
    // the user asked for once it exceeds it.
    const backoff = Math.min(MAX_BACKOFF, SCHEDULER_TICK * Math.pow(2, Math.min(failures, 7)));
    return last + Math.min(Math.max(backoff, SCHEDULER_TICK), every);
  }
  const waiting =
    acct.lastResult === "requested" ||
    acct.lastResult === "downloading" ||
    acct.lastResult === "ingesting";
  return last + (waiting ? Math.min(PENDING_RECHECK, every) : every);
}

async function runDueSyncs(reason = "tick") {
  if (schedulerRunning) return;
  schedulerRunning = true;
  let blocker = null;
  try {
    const candidates = PLATFORMS.auto().filter((p) => {
      const a = getAccount(p.id);
      if (!a.connected || automation.isBusy(p.id) || automation.hasAttention(p.id)) return false;
      const due = dueAt(a);
      return due !== null && Date.now() >= due;
    });
    if (!candidates.length) return;
    // Keep the Mac awake only while we are actually working, so an idle app
    // costs nothing and a sync is not cut in half by sleep.
    try {
      blocker = powerSaveBlocker.start("prevent-app-suspension");
    } catch {
      blocker = null;
    }
    for (const p of candidates) {
      if (quitting) break;
      // Sequential on purpose: several hidden browser windows at once is heavy,
      // and platforms are happier with one session doing one thing.
      await automation.runSync(p.id, { interactive: false, surfaceOnAttention: false });
    }
  } catch {
    /* a bad pass must never kill the scheduler */
  } finally {
    if (blocker !== null && powerSaveBlocker.isStarted(blocker)) {
      try {
        powerSaveBlocker.stop(blocker);
      } catch {
        /* ignore */
      }
    }
    schedulerRunning = false;
  }
}

function startScheduler() {
  if (schedulerTimer) clearInterval(schedulerTimer);
  schedulerTimer = setInterval(() => {
    runDueSyncs("tick").catch(() => {});
  }, SCHEDULER_TICK);

  // Waking from sleep is the single most common way a scheduled run is missed:
  // timers do not fire while suspended, so re-evaluate immediately on resume.
  try {
    powerMonitor.on("resume", () => setTimeout(() => runDueSyncs("resume").catch(() => {}), 15000));
    powerMonitor.on("unlock-screen", () => runDueSyncs("unlock").catch(() => {}));
  } catch {
    /* powerMonitor is unavailable in some environments */
  }

  // Catch-up shortly after launch (let the UI settle first). This is what makes
  // a reboot resume the schedule rather than reset it.
  setTimeout(() => runDueSyncs("launch").catch(() => {}), 25000);

  // Chromium's per-platform HTTP caches grow unbounded over weeks. Trim daily
  // when nothing is running; cookies (the sign-ins) are never touched.
  const cacheTimer = setInterval(
    () => automation.trimCaches().catch(() => {}),
    24 * 60 * 60 * 1000
  );
  if (cacheTimer.unref) cacheTimer.unref();
}

// ---------------------------------------------------------------------------
// Downloads watcher — if you download an export yourself (or a platform only
// delivers by email link), dropping it in ~/Downloads is enough.
// ---------------------------------------------------------------------------

const EXPORT_RE =
  /(instagram|facebook|twitter|^x-|discord|telegram|whatsapp|reddit|snapchat|linkedin|slack|takeout|your[-_]?data|data[-_]?export)/i;
let downloadsWatcher = null;
const pendingDownloads = new Map(); // name -> timer (debounce; bounded by dir size)
// Files the engine says it can never read (e.g. an HTML export where JSON was
// needed). Retrying them on every launch would be pure noise.
const permanentFailures = new Set();

function watchDownloads() {
  const dir = path.join(os.homedir(), "Downloads");
  if (!fs.existsSync(dir)) return;
  const seen = new Set(loadSettings().ingestedDownloads);

  const remember = (name) => {
    seen.add(name);
    // Keep the live Set and the persisted list in step, so a restart cannot
    // resurrect names we already dropped from the tail.
    const keep = [...seen].slice(-300);
    seen.clear();
    for (const n of keep) seen.add(n);
    loadSettings().ingestedDownloads = keep;
    saveSettings();
  };

  const inFlight = new Set(); // being ingested right now — don't double-submit

  const consider = (name) => {
    if (!name || !name.toLowerCase().endsWith(".zip")) return;
    if (!EXPORT_RE.test(name) || seen.has(name) || inFlight.has(name)) return;
    // Partial downloads: browsers write these then rename to the real name.
    if (/\.(crdownload|part|download|tmp)$/i.test(name)) return;
    // Browsers write .zip.crdownload/.part then rename; fs.watch fires many
    // times. Debounce per-name and only act once the size stops changing.
    if (pendingDownloads.has(name)) clearTimeout(pendingDownloads.get(name));
    const full = path.join(dir, name);
    let lastSize = -1;
    let stableFor = 0;
    const check = () => {
      let size;
      try {
        const st = fs.statSync(full);
        if (!st.isFile()) throw new Error("not a file");
        size = st.size;
      } catch {
        pendingDownloads.delete(name);
        return;
      }
      // A browser still writing a sibling .crdownload means this is not done.
      const partial = fs.existsSync(full + ".crdownload") || fs.existsSync(full + ".part");
      if (size === 0 || partial || size !== lastSize) {
        lastSize = size;
        stableFor = 0;
        pendingDownloads.set(name, setTimeout(check, 4000));
        return;
      }
      // Require several consecutive equal samples: a download that merely
      // stalled for a few seconds must not be ingested half-written.
      if (++stableFor < 3) {
        pendingDownloads.set(name, setTimeout(check, 4000));
        return;
      }
      pendingDownloads.delete(name);
      if (seen.has(name) || inFlight.has(name)) return;
      inFlight.add(name);
      // cleanup:false — this is the user's own file in their Downloads folder,
      // not something we produced. Never delete it.
      // Only record it as handled once it ACTUALLY backed up; otherwise a
      // transient failure would blacklist the file forever.
      ingestPath(full, { cleanup: false })
        .then((ok) => {
          inFlight.delete(name);
          // Remember it if it worked — or if it can never work. Re-scanning an
          // unreadable file on every single launch helps nobody.
          if (ok || permanentFailures.has(full)) remember(name);
        })
        .catch(() => inFlight.delete(name));
    };
    pendingDownloads.set(name, setTimeout(check, 4000));
  };

  // Catch anything that arrived while the app was closed.
  try {
    for (const name of fs.readdirSync(dir)) consider(name);
  } catch {
    /* unreadable Downloads folder is not fatal */
  }

  const attach = () => {
    try {
      downloadsWatcher = fs.watch(dir, (_ev, name) => consider(name));
      // macOS can drop the watch (folder replaced, volume remount). Re-arm.
      downloadsWatcher.on("error", () => {
        try {
          downloadsWatcher.close();
        } catch {
          /* ignore */
        }
        downloadsWatcher = null;
        setTimeout(attach, 30000);
      });
    } catch {
      /* watching is a bonus, not a requirement */
    }
  };
  attach();
}

// ---------------------------------------------------------------------------
// Menu bar presence — the app keeps running (and syncing) after you close the
// window. That is the whole point of "it just happens in the background".
// ---------------------------------------------------------------------------

function trayIcon() {
  // A 16pt template shield, drawn inline so we ship no extra asset.
  // The container mark, flattened to one colour: a template image is a mask,
  // so macOS recolours it for light/dark menu bars and only the alpha matters.
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="16" height="16" viewBox="0 0 24 24"><polygon points="1.27,4.93 15.44,13.12 15.44,23.28 1.27,15.10" fill="black" opacity=".55"/><polygon points="15.44,13.12 22.73,8.90 22.73,19.07 15.44,23.28" fill="black" opacity=".8"/><polygon points="8.56,0.72 22.73,8.90 15.44,13.12 1.27,4.93" fill="black"/></svg>`;
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
    { label: "Open Cold Storage", click: () => showMainWindow() },
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
    { label: "Reveal Data Folder", click: () => shell.showItemInFolder(coldHome()) },
    { label: "Show Error Log", click: () => showErrorLog() },
    { type: "separator" },
    // Version is reachable without opening a window — the first thing to ask
    // a tester for is which build they are on.
    { label: `Version ${app.getVersion()}`, enabled: false },
    {
      label: "Quit Cold Storage",
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
    tray.setToolTip("Cold Storage");
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

// Idle cost control: the viewer is a separate Python process holding the
// archive open. Nothing needs it while there is no window, so shut it down
// after a grace period and bring it back on demand. A tray-only app then costs
// essentially nothing between syncs.
const SERVE_IDLE_GRACE_MS = 5 * 60 * 1000;
let serveIdleTimer = null;

function scheduleServeIdleStop() {
  if (serveIdleTimer) clearTimeout(serveIdleTimer);
  serveIdleTimer = setTimeout(() => {
    serveIdleTimer = null;
    if (quitting) return;
    if (mainWindow && !mainWindow.isDestroyed()) return; // window came back
    stopServe();
    serveUrl = null;
  }, SERVE_IDLE_GRACE_MS);
  if (serveIdleTimer.unref) serveIdleTimer.unref();
}

function cancelServeIdleStop() {
  if (serveIdleTimer) clearTimeout(serveIdleTimer);
  serveIdleTimer = null;
}

async function showMainWindow() {
  cancelServeIdleStop();
  if (mainWindow && !mainWindow.isDestroyed()) {
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
    return;
  }
  if (!serveUrl || !serveChild) {
    try {
      await startServe();
    } catch (e) {
      logCrash("showMainWindow/startServe", e);
      notify("Cold Storage", "Could not open the archive viewer. Backups still run.");
      return;
    }
  }
  createWindow();
}

function showErrorLog() {
  const log = path.join(coldHome(), "app-errors.log");
  if (fs.existsSync(log)) {
    shell.openPath(log);
    return;
  }
  const msg = {
    type: "info",
    title: "Error log",
    message: "No errors have been recorded.",
    detail:
      `Nothing has gone wrong since this archive was set up.\n\n` +
      `If something does, it is written to:\n${log}`,
    buttons: ["OK"],
  };
  if (mainWindow && !mainWindow.isDestroyed()) {
    dialog.showMessageBox(mainWindow, msg).catch(() => {});
  } else {
    dialog.showMessageBox(msg).catch(() => {});
  }
}

function showRecoveryKit() {
  // Older builds wrote it into the archive folder; check both so an existing
  // install still finds its kit.
  const candidates = [
    path.join(recoveryKitDir(), "Cold Storage — Recovery Kit.txt"),
    path.join(coldHome(), "RECOVERY-KIT.txt"),
  ];
  for (const kit of candidates) {
    if (fs.existsSync(kit)) {
      shell.showItemInFolder(kit);
      return;
    }
  }
  const msg = {
    type: "info",
    title: "Recovery Kit",
    message: "No Recovery Kit file found.",
    detail:
      `Expected at:\n${candidates[0]}\n\nIf you set this archive up with the ` +
      "`cold` command line, the recovery code was shown once during `cold init`.",
    buttons: ["OK"],
  };
  if (mainWindow && !mainWindow.isDestroyed()) {
    dialog.showMessageBox(mainWindow, msg).catch(() => {});
  } else {
    dialog.showMessageBox(msg).catch(() => {});
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
          click: () => shell.showItemInFolder(coldHome()),
        },
        {
          label: "Show Recovery Kit",
          click: () => showRecoveryKit(),
        },
        { type: "separator" },
        {
          // Testers need this: a background app is invisible when it breaks.
          label: "Show Error Log",
          click: () => showErrorLog(),
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
  ipcMain.handle("cold:pickAndIngest", () => pickAndIngest());
  ipcMain.handle("cold:revealDataFolder", () => shell.showItemInFolder(coldHome()));
  ipcMain.handle("cold:showRecoveryKit", () => showRecoveryKit());

  // --- accounts / automation --------------------------------------------
  ipcMain.handle("cold:accounts", () => accountsPayload());
  ipcMain.handle("cold:connect", async (_e, id) => {
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
  ipcMain.handle("cold:disconnect", async (_e, id) => {
    await automation.disconnect(String(id));
    return true;
  });
  ipcMain.handle("cold:setSchedule", (_e, id, schedule) => {
    const ok = ["manual", "daily", "weekly", "monthly"].includes(schedule);
    patchAccount(String(id), { schedule: ok ? schedule : "manual" });
    broadcastAccounts();
    return true;
  });
  ipcMain.handle("cold:syncNow", (_e, id) => {
    automation.runSync(String(id), { interactive: false, surfaceOnAttention: true });
    return true;
  });
  ipcMain.handle("cold:syncAll", () => {
    syncAll();
    return true;
  });
  ipcMain.handle("cold:getPrefs", () => ({
    ...loadSettings().prefs,
    launchAtLogin: app.getLoginItemSettings().openAtLogin,
  }));
  ipcMain.handle("cold:setPref", (_e, key, value) => {
    loadSettings();
    store.setPref(String(key), value);
    if (key === "launchAtLogin") {
      try {
        app.setLoginItemSettings({ openAtLogin: !!value, openAsHidden: true });
      } catch {
        return false;
      }
    }
    store.flush(); // a pref the user just toggled must survive a hard reboot
    return true;
  });
  ipcMain.handle("cold:openExternal", (_e, url) => {
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

// Remember where the user put the window. Validated against the screens that
// exist right now, so unplugging an external display cannot strand the app
// off-screen with no way to get it back.
function savedBounds() {
  const b = loadSettings().windowBounds;
  if (!b || typeof b.width !== "number" || typeof b.height !== "number") return null;
  if (typeof b.x !== "number" || typeof b.y !== "number") return { width: b.width, height: b.height };
  const visible = screen.getAllDisplays().some((d) => {
    const a = d.workArea;
    return b.x < a.x + a.width - 80 && b.x + b.width > a.x + 80 && b.y < a.y + a.height - 40 && b.y + b.height > a.y;
  });
  return visible ? b : { width: b.width, height: b.height };
}

function createWindow() {
  const b = savedBounds() || {};
  mainWindow = new BrowserWindow({
    width: b.width || 1280,
    height: b.height || 820,
    ...(typeof b.x === "number" ? { x: b.x, y: b.y } : {}),
    minWidth: 900,
    minHeight: 640,
    title: "Cold Storage",
    // Must match the UI's --bg (zinc-950), or the window flashes the old warm
    // brown for a frame before the page paints.
    backgroundColor: "#09090b",
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

  const rememberBounds = () => {
    if (!mainWindow || mainWindow.isDestroyed() || mainWindow.isMinimized()) return;
    loadSettings().windowBounds = mainWindow.getNormalBounds();
    saveSettings();
  };
  mainWindow.on("resize", rememberBounds);
  mainWindow.on("move", rememberBounds);

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

  mainWindow.on("close", rememberBounds);
  mainWindow.on("closed", () => {
    mainWindow = null;
    scheduleServeIdleStop(); // reclaim the viewer process once idle
  });

  mainWindow.loadURL(serveUrl);
  // Surface the first-run Recovery Kit once there's a window behind it.
  mainWindow.webContents.once("did-finish-load", flushFirstRunNotice);
}

// ---------------------------------------------------------------------------
// App lifecycle
// ---------------------------------------------------------------------------

/**
 * Carry over state from before the rename.
 *
 * Electron derives userData from the product name, so "Save Your Shit" →
 * "Cold Storage" moves the whole directory — including Partitions/, which holds
 * every platform sign-in. Upgrading would silently log the user out of all of
 * their connected accounts with no explanation. Move the old directory across
 * once, before anything reads it.
 *
 * Runs before app.whenReady(): getPath('userData') is valid immediately, and
 * the session layer must not touch the new path before the copy exists.
 */
function migrateLegacyUserData() {
  try {
    const current = app.getPath("userData");
    const legacy = path.join(path.dirname(current), "Save Your Shit");
    // Copy the old directory across, but only the first time — after that the
    // new location is authoritative and re-copying would resurrect stale state.
    if (legacy !== current && fs.existsSync(legacy) && !fs.existsSync(path.join(current, "Partitions"))) {
      fs.mkdirSync(current, { recursive: true });
      for (const entry of fs.readdirSync(legacy)) {
        if (fs.existsSync(path.join(current, entry))) continue;
        try {
          fs.cpSync(path.join(legacy, entry), path.join(current, entry), {
            recursive: true,
            errorOnExist: false,
            force: false,
          });
        } catch {
          /* one unreadable item must not abort the migration */
        }
      }
      // Deliberately not deleting the old directory: if anything about this
      // went wrong, the user's sign-ins are still recoverable from it.
    }
    // Always: the partition names changed too (persist:syt-<id> ->
    // persist:cold-<id>), so a copied directory alone leaves every sign-in in a
    // folder the new code never opens. This must also run on later launches,
    // because by then Partitions/ exists and the copy above is skipped.
    migrateLegacyPartitions(current);
  } catch {
    /* a failed migration means "sign in again", never a failed launch */
  }
}

/** Rename Partitions/syt-<id> to Partitions/cold-<id>, without clobbering. */
function migrateLegacyPartitions(userData) {
  const dir = path.join(userData, "Partitions");
  let entries;
  try {
    entries = fs.readdirSync(dir);
  } catch {
    return; // no partitions yet — nothing to carry over
  }
  for (const name of entries) {
    if (!name.startsWith("syt-")) continue;
    const from = path.join(dir, name);
    const to = path.join(dir, "cold-" + name.slice(4));
    try {
      // A partition Chromium created but never signed into has no cookie
      // store; that is not something worth protecting from being replaced.
      const targetHasSession =
        fs.existsSync(to) && fs.readdirSync(to).some((f) => f.startsWith("Cookies"));
      if (targetHasSession) continue;
      if (fs.existsSync(to)) fs.rmSync(to, { recursive: true, force: true });
      fs.renameSync(from, to);
    } catch {
      /* skip this one; the user re-connects that platform at worst */
    }
  }
}
migrateLegacyUserData();

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  // Re-launching the app (Finder, Dock, or the login item) should reveal the
  // window even when we were running tray-only with no window at all.
  app.on("second-instance", () => showMainWindow());

  // A background app must not die from one unexpected throw — that would
  // silently stop every future backup. Log it and keep the scheduler alive.
  process.on("uncaughtException", (err) => {
    logCrash("uncaughtException", err);
  });
  process.on("unhandledRejection", (err) => {
    logCrash("unhandledRejection", err);
  });

  app.whenReady().then(async () => {
    const bin = coldBinary();
    if (!fs.existsSync(bin)) {
      dialog.showErrorBox(
        "Cold Storage",
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

    // The whole point of this app is that it keeps running. Default the login
    // item ON at first run (the user can turn it off in the menu), and keep the
    // OS setting and our stored pref reconciled — the user may have changed it
    // in System Settings.
    const prefs = store.prefs();
    if (prefs.launchAtLogin === undefined) prefs.launchAtLogin = true;
    try {
      const os_ = app.getLoginItemSettings();
      if (prefs.firstRunLoginApplied) {
        prefs.launchAtLogin = os_.openAtLogin; // OS is the source of truth after setup
      } else {
        app.setLoginItemSettings({ openAtLogin: !!prefs.launchAtLogin, openAsHidden: true });
        prefs.firstRunLoginApplied = true;
      }
      store.save();
    } catch {
      /* login items unavailable (e.g. unsigned dev run) */
    }

    queue = new IngestQueue(store, (p) => runSytAsync(["ingest", p, "--no-snapshot"]), onQueueEvent);

    automation.init({
      coldHome,
      enqueue: (p, meta) => ingestPath(p, meta),
      getAccount,
      patchAccount,
      broadcast: broadcastAccounts,
    });
    app.setAboutPanelOptions({
      applicationName: "Cold Storage",
      applicationVersion: app.getVersion(),
      copyright: "Local-first backup for your own social-media data. MIT licensed.",
    });
    registerIpc();
    setMenu();

    // Anything downloaded but not yet backed up (crash, power cut, quit
    // mid-ingest) is picked up here, before anything new is started.
    try {
      fs.mkdirSync(incomingDir(), { recursive: true });
    } catch {
      /* best effort */
    }
    const recovered = queue.recover(incomingDir());
    if (recovered) notify("Cold Storage", `Resuming ${recovered} backup${recovered > 1 ? "s" : ""}…`);

    try {
      await startServe();
    } catch (e) {
      // The viewer failing to start must not stop backups. Carry on headless
      // and let handleServeExit's backoff keep trying.
      logCrash("startServe", e);
      notify("Cold Storage", "The archive viewer did not start — backups still run.");
    }

    // Launched by the login item? Stay out of the way: no window, just the
    // menu bar. The user asked for this to be invisible until it matters.
    const openedAtLogin = app.getLoginItemSettings().wasOpenedAtLogin;
    if (serveUrl && !openedAtLogin) createWindow();
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

  app.on("before-quit", async (e) => {
    if (quitting) return;
    quitting = true;
    // Persist sign-ins before we go, so a quit (or a reboot) never costs the
    // user a re-login, and make sure settings hit the disk.
    e.preventDefault();
    try {
      automation.closeAllAttention(); // parked windows must not block the quit
      if (store) store.flush();
      await Promise.race([
        automation.flushSessions(),
        new Promise((r) => setTimeout(r, 3000)), // never hang the quit
      ]);
    } catch {
      /* best effort */
    }
    app.quit();
  });

  app.on("will-quit", () => {
    quitting = true;
    if (schedulerTimer) clearInterval(schedulerTimer);
    if (downloadsWatcher) {
      try {
        downloadsWatcher.close();
      } catch {
        /* ignore */
      }
    }
    for (const t of pendingDownloads.values()) clearTimeout(t);
    pendingDownloads.clear();
    if (store) store.flush();
    stopServe();
  });
}

// A background app is invisible when it breaks, so leave a trail the user (or
// we) can read later. Bounded so it can never fill the disk.
function logCrash(kind, err) {
  const line = `[${new Date().toISOString()}] ${kind}: ${(err && err.stack) || err}\n`;
  try {
    const file = path.join(coldHome(), "app-errors.log");
    try {
      if (fs.statSync(file).size > 512 * 1024) fs.rmSync(file, { force: true });
    } catch {
      /* no log yet */
    }
    fs.appendFileSync(file, line);
  } catch {
    /* logging must never throw */
  }
  console.error(line);
}
