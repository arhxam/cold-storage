// automation.js — the background pipeline that turns "sign in once" into
// scheduled, hands-off backups.
//
// How it works, per platform:
//   1. connect(id)  — a visible window on the platform's login page, in a
//      persistent per-platform session partition. Sign in once (2FA and all);
//      the session lives in Electron's cookie jar, never in our code.
//   2. runSync(id)  — a hidden window loads the platform's official export
//      page in that same session and:
//        a. if an archive is ready  -> clicks Download; the file is captured,
//           handed to `syt ingest`, and lands in the local encrypted archive;
//        b. otherwise               -> clicks through the export request; the
//           scheduler polls again later and picks the archive up in (a);
//        c. if the platform throws a wall (2FA, password confirm, redesign)
//           -> the exact window is surfaced as "needs one click" instead of
//           failing silently. Any download the user triggers in that window
//           is still captured and ingested automatically.
//
// Nothing here talks to any server of ours. All traffic is user <-> platform.

const { BrowserWindow, Notification, session } = require("electron");
const { EventEmitter } = require("events");
const fs = require("fs");
const path = require("path");

const PLATFORMS = require("./platforms");

const events = new EventEmitter();
events.setMaxListeners(50);

let deps = null; // { sytHome, ingest, getAccount, patchAccount, broadcast }
const busy = new Set();
const attentionWins = new Map(); // id -> BrowserWindow left open for the user
const capturedParts = new Set();

function init(d) {
  deps = d;
}

const partitionFor = (id) => "persist:syt-" + id;
const sesFor = (id) => session.fromPartition(partitionFor(id));

const isBusy = (id) => busy.has(id);
const hasAttention = (id) => {
  const w = attentionWins.get(id);
  return !!(w && !w.isDestroyed());
};

// ---------------------------------------------------------------------------
// Download capture: any download started in a platform's session is saved to
// <SYT_HOME>/incoming/ and ingested. This covers both the automated click and
// anything the user clicks in a surfaced "needs attention" window.
// ---------------------------------------------------------------------------

function ensureDownloadCapture(id) {
  if (capturedParts.has(id)) return;
  capturedParts.add(id);
  sesFor(id).on("will-download", (_e, item) => {
    const dir = path.join(deps.sytHome(), "incoming");
    try {
      fs.mkdirSync(dir, { recursive: true });
    } catch {
      /* best effort */
    }
    const file = path.join(dir, `${id}-${Date.now()}-${item.getFilename()}`);
    item.setSavePath(file);
    events.emit("downloadstart:" + id);
    deps.patchAccount(id, { lastResult: "downloading", detail: "Downloading " + item.getFilename() });
    deps.broadcast();
    item.once("done", async (_ev, state) => {
      if (state !== "completed") {
        deps.patchAccount(id, { lastResult: "error", detail: "Download interrupted" });
        deps.broadcast();
        events.emit("downloaded:" + id, false);
        return;
      }
      closeAttention(id);
      const ok = await deps.ingest(file);
      if (ok) {
        deps.patchAccount(id, {
          lastResult: "synced",
          lastSuccess: new Date().toISOString(),
          detail: null,
        });
        try {
          fs.rmSync(file);
        } catch {
          /* keep it; ingest already copied what it needed */
        }
      } else {
        deps.patchAccount(id, {
          lastResult: "error",
          detail: "The downloaded export could not be backed up — see the app.",
        });
      }
      deps.broadcast();
      events.emit("downloaded:" + id, ok);
    });
  });
}

function closeAttention(id) {
  const w = attentionWins.get(id);
  if (w && !w.isDestroyed()) {
    try {
      w.close();
    } catch {
      /* already closing */
    }
  }
  attentionWins.delete(id);
}

function waitEvent(name, ms) {
  return new Promise((resolve) => {
    const t = setTimeout(() => {
      events.removeListener(name, on);
      resolve(null);
    }, ms);
    const on = (v) => {
      clearTimeout(t);
      resolve(v === undefined ? true : v);
    };
    events.once(name, on);
  });
}

// ---------------------------------------------------------------------------
// Sign in once
// ---------------------------------------------------------------------------

async function isSignedIn(p) {
  if (!p.cookie) return false;
  try {
    const cookies = await sesFor(p.id).cookies.get({ name: p.cookie.name });
    return cookies.some((c) => (c.domain || "").includes(p.cookie.domain));
  } catch {
    return false;
  }
}

function connect(id) {
  const p = PLATFORMS.byId(id);
  if (!p || !p.auto) return Promise.resolve({ connected: false });
  ensureDownloadCapture(id);
  return new Promise((resolve) => {
    const win = new BrowserWindow({
      width: 980,
      height: 760,
      title: `Sign in to ${p.name} — Save Your Shit`,
      webPreferences: {
        partition: partitionFor(id),
        contextIsolation: true,
        nodeIntegration: false,
      },
    });
    win.webContents.setWindowOpenHandler(({ url }) => {
      win.loadURL(url);
      return { action: "deny" };
    });

    let settled = false;
    let timer = null;
    const finish = (ok) => {
      if (settled) return;
      settled = true;
      if (timer) clearInterval(timer);
      if (ok) {
        deps.patchAccount(id, { connected: true, connectedAt: new Date().toISOString() });
        deps.broadcast();
        if (!win.isDestroyed()) setTimeout(() => { try { win.close(); } catch {} }, 800);
      }
      resolve({ connected: ok });
    };

    // Cookie-based platforms: poll the jar. Pattern-based: watch navigation.
    if (p.cookie) {
      timer = setInterval(async () => {
        if (await isSignedIn(p)) finish(true);
      }, 2000);
    }
    win.webContents.on("did-navigate", (_e, url) => {
      if (p.connectedPattern && p.connectedPattern.test(url)) finish(true);
    });
    win.on("closed", () => {
      isSignedIn(p).then(
        (ok) => finish(ok || (deps.getAccount(id).connected && !p.cookie)),
        () => finish(false)
      );
    });

    win.loadURL(p.loginUrl);
  });
}

async function disconnect(id) {
  closeAttention(id);
  try {
    await sesFor(id).clearStorageData();
  } catch {
    /* nothing to clear */
  }
  deps.patchAccount(id, { connected: false, lastResult: null, detail: null });
  deps.broadcast();
}

// ---------------------------------------------------------------------------
// Page-driving helpers
// ---------------------------------------------------------------------------

// The text matcher, as a page script. Exported so tests exercise the real
// thing rather than a copy that can drift away from it.
//
// Two rules keep this safe:
//   - only genuinely visible, enabled controls are candidates (platforms keep
//     offscreen templates and zero-size a11y helpers in the DOM);
//   - `exact` requires the whole label to match, so "download" can never hit
//     "Download or transfer information".
function findClickCode(patterns, exact) {
  return `(() => {
    const P = ${JSON.stringify(patterns.map((s) => s.toLowerCase()))};
    const EXACT = ${exact ? "true" : "false"};
    const els = [...document.querySelectorAll(
      'button,[role="button"],a,[role="link"],input[type="submit"],[role="radio"],[role="checkbox"],label,[tabindex]'
    )];
    const vis = (e) => {
      if (e.disabled || e.getAttribute('aria-hidden') === 'true') return false;
      if (typeof e.checkVisibility === 'function' &&
          !e.checkVisibility({ opacityProperty: true, visibilityProperty: true, contentVisibilityAuto: true }))
        return false;
      const r = e.getBoundingClientRect();
      // A real, pressable control is bigger than this; zero-size elements that
      // only have padding/border are decoys.
      if (r.width < 8 || r.height < 8) return false;
      const s = getComputedStyle(e);
      if (s.visibility === 'hidden' || s.display === 'none') return false;
      if (parseFloat(s.opacity || '1') < 0.05) return false;
      return true;
    };
    for (const el of els) {
      if (!vis(el)) continue;
      const t = ((el.innerText || el.textContent || el.value || el.getAttribute('aria-label') || '') + '')
        .trim().toLowerCase().replace(/\\s+/g, ' ');
      if (!t || t.length > 160) continue;
      for (const p of P) {
        const hit = EXACT ? (t === p) : (t === p || t.startsWith(p + ' ') || t.includes(p));
        if (hit) { try { el.scrollIntoView({ block: 'center' }); } catch {} el.click(); return t; }
      }
    }
    return null;
  })()`;
}

function makeCtx(win) {
  const exec = (code) => win.webContents.executeJavaScript(code, true).catch(() => null);
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const goto = (url) =>
    new Promise((resolve) => {
      let done = false;
      const fin = () => {
        if (!done) {
          done = true;
          resolve();
        }
      };
      win.webContents.once("did-finish-load", fin);
      setTimeout(fin, 20000); // never hang on a slow SPA
      win.loadURL(url).catch(fin);
    });

  const click = async (patterns, { timeout = 10000, exact = false, settle = 1600 } = {}) => {
    const end = Date.now() + timeout;
    while (Date.now() < end) {
      const hit = await exec(findClickCode(patterns, exact));
      if (hit) {
        await sleep(settle);
        return hit;
      }
      await sleep(600);
    }
    return null;
  };

  const has = async (patterns) =>
    !!(await exec(`(() => {
      const P = ${JSON.stringify(patterns.map((s) => s.toLowerCase()))};
      const t = ((document.body && document.body.innerText) || '').toLowerCase();
      return P.some((p) => t.includes(p));
    })()`));

  const hasSel = async (sel) => !!(await exec(`!!document.querySelector(${JSON.stringify(sel)})`));

  return { win, exec, sleep, goto, click, has, hasSel };
}

// ---------------------------------------------------------------------------
// One sync pass for one platform
// ---------------------------------------------------------------------------

async function runSync(id, { interactive = false, surfaceOnAttention = true } = {}) {
  const p = PLATFORMS.byId(id);
  if (!p || !p.auto || busy.has(id) || !deps) return;
  const acct = deps.getAccount(id);
  if (!acct.connected) return;
  if (hasAttention(id)) return; // a window is already open waiting for the user

  busy.add(id);
  const prevResult = acct.lastResult;
  deps.patchAccount(id, { lastAttempt: new Date().toISOString(), lastResult: "running", detail: null });
  deps.broadcast();
  ensureDownloadCapture(id);

  const win = new BrowserWindow({
    width: 1100,
    height: 800,
    show: interactive,
    title: `${p.name} — Save Your Shit`,
    webPreferences: {
      partition: partitionFor(id),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });
  win.webContents.setWindowOpenHandler(({ url }) => {
    win.loadURL(url);
    return { action: "deny" };
  });
  win.on("closed", () => attentionWins.delete(id));

  const ctx = makeCtx(win);
  let outcome = null; // null = handled by the download pipeline

  try {
    await ctx.goto(p.exportUrl);
    await ctx.sleep(3500);
    const url = win.webContents.getURL();

    if (p.loginRe && p.loginRe.test(url)) {
      outcome = { result: "reconnect", detail: "Session expired — reconnect to " + p.name };
    } else {
      // Phase 1: is an archive ready? Click Download and let capture take over.
      const clicked = p.tryDownload ? await p.tryDownload(ctx) : false;
      const started = clicked ? await waitEvent("downloadstart:" + id, 25000) : false;
      if (started) {
        const ok = await waitEvent("downloaded:" + id, 20 * 60 * 1000);
        outcome = ok ? null : { result: "error", detail: "The download did not complete" };
      } else {
        // Phase 2: request a fresh export.
        const requested = p.tryRequest ? await p.tryRequest(ctx) : false;
        outcome = requested
          ? { result: "requested", detail: `${p.name} is preparing your export — checked automatically` }
          : { result: "attention", detail: p.attentionHint || "Finish this step in the window" };
      }
    }
  } catch (e) {
    outcome = { result: "attention", detail: String((e && e.message) || e) };
  }

  busy.delete(id);

  if (outcome === null) {
    // Download + ingest already recorded the final state.
    if (!attentionWins.has(id) && !win.isDestroyed()) {
      try { win.close(); } catch {}
    }
    deps.broadcast();
    return;
  }

  const needsUser = outcome.result === "attention" || outcome.result === "reconnect";
  if (needsUser && (interactive || surfaceOnAttention)) {
    attentionWins.set(id, win);
    if (!win.isDestroyed()) {
      win.show();
      win.focus();
    }
  } else {
    if (!win.isDestroyed()) {
      try { win.close(); } catch {}
    }
    if (needsUser && prevResult !== outcome.result) {
      new Notification({
        title: "Save Your Shit",
        body: `${p.name} needs a quick click — open the app to finish.`,
      }).show();
    }
  }

  deps.patchAccount(id, { lastResult: outcome.result, detail: outcome.detail || null });
  deps.broadcast();
}

module.exports = {
  init,
  connect,
  disconnect,
  runSync,
  isBusy,
  hasAttention,
  isSignedIn,
  events,
  findClickCode, // exported for tests
};
