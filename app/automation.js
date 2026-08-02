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
//           handed to `cold ingest`, and lands in the local encrypted archive;
//        b. otherwise               -> clicks through the export request; the
//           scheduler polls again later and picks the archive up in (a);
//        c. if the platform throws a wall (2FA, password confirm, redesign)
//           -> the exact window is surfaced as "needs one click" instead of
//           failing silently. Any download the user triggers in that window
//           is still captured and ingested automatically.
//
// Nothing here talks to any server of ours. All traffic is user <-> platform.

const { BrowserWindow, Notification, session, net } = require("electron");
const { EventEmitter } = require("events");
const fs = require("fs");
const path = require("path");

const PLATFORMS = require("./platforms");

const events = new EventEmitter();
events.setMaxListeners(50);

let deps = null; // { coldHome, enqueue, getAccount, patchAccount, broadcast }
const busy = new Set();
const attentionWins = new Map(); // id -> BrowserWindow left open for the user
const capturedParts = new Set();
const inFlightDownloads = new Map(); // id -> count of active transfers

// No single sync pass may outlive this. Without it a page that never settles
// keeps a hidden window (and the `busy` flag) alive forever, which silently
// stops that platform from ever syncing again.
const SYNC_HARD_TIMEOUT_MS = 15 * 60 * 1000;
// A download, once started, gets much longer: Google Takeout archives are tens
// of gigabytes and legitimately take hours on a slow line.
const DOWNLOAD_TIMEOUT_MS = 6 * 60 * 60 * 1000;
// How long a "needs a click" window may sit on screen before we reclaim it.
// It holds a live social-media page, so it is the app's biggest idle cost.
const ATTENTION_WINDOW_TTL_MS = 45 * 60 * 1000;
// Big exports arrive split into parts; fetch them all, but never loop forever.
const MAX_PARTS = 25;

function init(d) {
  deps = d;
}

const ready = () => !!deps;

// NOTE: partitions resolve to <userData>/Partitions/cold-<id>, and userData is
// derived from the product name. Renaming the app would sign every user out of
// every platform, so that rename would need a migration step here.
const partitionFor = (id) => "persist:cold-" + id;
const sesFor = (id) => session.fromPartition(partitionFor(id));

// Google (and others) refuse to sign you in from a browser whose user agent
// says "Electron" — the sign-in page shows "this browser or app may not be
// secure" and there is no way through. Present as plain Chrome instead. This is
// the same Chromium that is actually rendering the page; we are only removing
// the Electron/app tokens Chromium adds by default.
const CHROME_UA = (() => {
  const ua = process.versions.chrome || "130.0.0.0";
  return (
    `Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 ` +
    `(KHTML, like Gecko) Chrome/${ua} Safari/537.36`
  );
})();

const uaApplied = new Set();
function prepareSession(id) {
  if (uaApplied.has(id)) return sesFor(id);
  uaApplied.add(id);
  const ses = sesFor(id);
  try {
    ses.setUserAgent(CHROME_UA);
  } catch {
    /* older Electron: fall back to the per-window UA below */
  }
  return ses;
}

const isBusy = (id) => busy.has(id);
const hasAttention = (id) => {
  const w = attentionWins.get(id);
  return !!(w && !w.isDestroyed());
};

// ---------------------------------------------------------------------------
// Download capture: any download started in a platform's session is saved to
// <COLD_HOME>/incoming/ and ingested. This covers both the automated click and
// anything the user clicks in a surfaced "needs attention" window.
// ---------------------------------------------------------------------------

function ensureDownloadCapture(id) {
  if (capturedParts.has(id)) return;
  capturedParts.add(id);
  // Registered once per partition and never removed: the session outlives any
  // window, so a download keeps going (and still gets ingested) even if the
  // window that started it is gone.
  sesFor(id).on("will-download", (_e, item) => {
    if (!ready()) return;
    const dir = path.join(deps.coldHome(), "incoming");
    try {
      fs.mkdirSync(dir, { recursive: true });
    } catch {
      /* best effort */
    }
    const safe = String(item.getFilename() || "export.zip").replace(/[/\\]/g, "_");

    // Platforms keep a finished archive listed for days. Without this, a daily
    // schedule would re-download the same multi-gigabyte file every day until
    // it expired. Identify it by name+size and skip what we already hold.
    const key = `${safe}:${item.getTotalBytes() || 0}`;
    const acct = deps.getAccount(id);
    if (Array.isArray(acct.archiveKeys) && acct.archiveKeys.includes(key)) {
      try {
        item.cancel();
      } catch {
        /* already gone */
      }
      deps.patchAccount(id, {
        lastResult: "synced",
        detail: null,
        failures: 0,
      });
      deps.broadcast();
      events.emit("downloadstart:" + id);
      events.emit("downloaded:" + id, true);
      return;
    }

    const file = path.join(dir, `${id}-${Date.now()}-${safe}`);
    item.setSavePath(file);
    inFlightDownloads.set(id, (inFlightDownloads.get(id) || 0) + 1);
    events.emit("downloadstart:" + id);

    const total = item.getTotalBytes();
    let lastPct = -1;
    item.on("updated", (_ev, state) => {
      if (state !== "progressing" || !total) return;
      const pct = Math.floor((item.getReceivedBytes() / total) * 100);
      // Only touch settings/UI on whole-decile changes: this fires many times
      // a second on a fast connection.
      if (pct >= lastPct + 10) {
        lastPct = pct;
        deps.patchAccount(id, { lastResult: "downloading", detail: `Downloading — ${pct}%` });
        deps.broadcast();
      }
    });

    deps.patchAccount(id, { lastResult: "downloading", detail: "Downloading " + safe });
    deps.broadcast();

    item.once("done", (_ev, state) => {
      inFlightDownloads.set(id, Math.max(0, (inFlightDownloads.get(id) || 1) - 1));
      if (state !== "completed") {
        // Interrupted (network drop, quit mid-download). Leave no partial file
        // behind for the queue to choke on; the scheduler retries later.
        try {
          fs.rmSync(file, { force: true });
        } catch {
          /* ignore */
        }
        deps.patchAccount(id, {
          lastResult: "error",
          detail: "Download interrupted — will retry",
          failures: (deps.getAccount(id).failures || 0) + 1,
        });
        deps.broadcast();
        events.emit("downloaded:" + id, false);
        return;
      }
      closeAttention(id);
      // Hand off to the durable queue and return immediately: ingest order and
      // retries are its problem, and a crash can no longer lose this file.
      deps.patchAccount(id, { lastResult: "ingesting", detail: "Backing up your export…" });
      deps.broadcast();
      deps
        .enqueue(file, { platform: id, cleanup: true })
        .then((ok) => {
          const prev = deps.getAccount(id).archiveKeys;
          deps.patchAccount(
            id,
            ok
              ? {
                  lastResult: "synced",
                  lastSuccess: new Date().toISOString(),
                  detail: null,
                  failures: 0,
                  // Remember what we already hold, so tomorrow's run skips it.
                  archiveKeys: [...(Array.isArray(prev) ? prev : []), key].slice(-40),
                }
              : {
                  // Never leave a hollow "Backed up" behind: the export read as
                  // zero records. HTML and JSON both parse now, so this means a
                  // partial/incomplete export — point the user at the real fix.
                  // (The exact engine reason also arrives as a notification.)
                  lastResult: "error",
                  detail:
                    "That export couldn't be backed up — it may be incomplete. " +
                    'Re-request your data and choose "All available information".',
                  failures: (deps.getAccount(id).failures || 0) + 1,
                }
          );
          deps.broadcast();
          events.emit("downloaded:" + id, ok);
        })
        .catch(() => events.emit("downloaded:" + id, false));
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

function closeAllAttention() {
  for (const id of [...attentionWins.keys()]) closeAttention(id);
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

// A cookie merely existing is not proof of a live session: it can be expired,
// or a session cookie that will not survive a quit. We check expiry here, and
// treat "the export page redirected us to /login" during a sync as the real,
// authoritative signal (see runSync).
// Proper domain match. `includes()` would accept a cookie from netflix.com for
// x.com, or notgoogle.com for google.com — any third-party script in the
// partition could then look like a valid sign-in.
function domainMatches(cookieDomain, want) {
  const d = String(cookieDomain || "").replace(/^\./, "").toLowerCase();
  const w = String(want || "").toLowerCase();
  return d === w || d.endsWith("." + w);
}

async function authCookie(p) {
  if (!p.cookie) return null;
  try {
    const cookies = await sesFor(p.id).cookies.get({ name: p.cookie.name });
    const now = Date.now() / 1000;
    return (
      cookies.find(
        (c) =>
          domainMatches(c.domain, p.cookie.domain) &&
          !!c.value &&
          (c.session || !c.expirationDate || c.expirationDate > now + 60)
      ) || null
    );
  } catch {
    return null;
  }
}

async function isSignedIn(p) {
  return !!(await authCookie(p));
}

/**
 * The only trustworthy test of "am I signed in": load the platform's own export
 * page in this partition and see whether it bounces us to a login screen.
 *
 * A cookie merely existing proves nothing — it survives a server-side logout,
 * a password change, or "log out of all devices". Trusting it caused a
 * deadlock: reconnecting saw the stale cookie, declared success instantly, and
 * the next sync bounced to /login again, forever.
 *
 * Runs in a hidden window and always cleans it up.
 */
async function probeSignedIn(p, timeoutMs = 25000) {
  let win = null;
  try {
    prepareSession(p.id);
    win = new BrowserWindow({
      show: false,
      webPreferences: {
        partition: partitionFor(p.id),
        contextIsolation: true,
        nodeIntegration: false,
      },
    });
    const w = win;
    const settled = await Promise.race([
      (async () => {
        await new Promise((resolve) => {
          const fin = () => resolve();
          w.webContents.once("did-finish-load", fin);
          w.webContents.once("did-fail-load", fin);
          w.loadURL(p.exportUrl).catch(fin);
        });
        await new Promise((r) => setTimeout(r, 2500)); // let client redirects run
        if (w.isDestroyed()) return null;
        const url = w.webContents.getURL();
        if (p.loginRe && p.loginRe.test(url)) return false;
        // Some platforms render a login wall without changing the URL.
        const walled = await w.webContents
          .executeJavaScript(
            `(() => { const t=((document.body&&document.body.innerText)||'').toLowerCase().slice(0,4000);
               return /log in to|sign in to|enter your password|create new account/.test(t); })()`,
            true
          )
          .catch(() => false);
        return !walled;
      })(),
      new Promise((r) => setTimeout(() => r(null), timeoutMs)),
    ]);
    return settled; // true | false | null (indeterminate)
  } catch {
    return null;
  } finally {
    try {
      if (win && !win.isDestroyed()) win.destroy();
    } catch {
      /* already gone */
    }
  }
}

// Cookies live in an on-disk store that Chromium writes lazily. Without an
// explicit flush, a session established seconds before quit (or before a
// reboot) can be lost — the user would have to sign in again for no reason.
async function flushSessions() {
  await Promise.all(
    PLATFORMS.auto().map(async (p) => {
      try {
        await sesFor(p.id).cookies.flushStore();
      } catch {
        /* partition may never have been opened */
      }
    })
  );
}

async function connect(id) {
  const p = PLATFORMS.byId(id);
  if (!p || !p.auto || !ready()) return { connected: false };
  prepareSession(id);
  ensureDownloadCapture(id);

  // Already genuinely signed in (e.g. the user pressed Connect again)? Confirm
  // against the platform rather than the cookie jar, and don't make them log in
  // for no reason.
  if (await probeSignedIn(p)) {
    deps.patchAccount(id, {
      connected: true,
      connectedAt: new Date().toISOString(),
      lastResult: null,
      detail: null,
      failures: 0,
    });
    deps.broadcast();
    return { connected: true };
  }

  // Not signed in. Any leftover cookies are stale by definition — clear them so
  // the "did a NEW sign-in happen?" test below cannot be fooled by them. This
  // is what breaks the reconnect deadlock.
  try {
    await sesFor(id).clearStorageData({ storages: ["cookies"] });
  } catch {
    /* best effort */
  }
  return interactiveSignIn(p);
}

function interactiveSignIn(p) {
  const id = p.id;
  return new Promise((resolve) => {
    const win = new BrowserWindow({
      width: 980,
      height: 760,
      title: `Sign in to ${p.name} — Cold Storage`,
      webPreferences: {
        partition: partitionFor(id),
        contextIsolation: true,
        nodeIntegration: false,
      },
    });
    win.webContents.setWindowOpenHandler(({ url }) => {
      // ERR_ABORTED is routine when a navigation supersedes another (common in
      // OAuth popups). An unhandled rejection here is fatal to the main
      // process, which would take the scheduler and the tray down with it.
      win.loadURL(url).catch(() => {});
      return { action: "deny" };
    });

    let settled = false;
    let timer = null;
    let giveUp = null;

    const cleanup = () => {
      if (timer) clearInterval(timer);
      if (giveUp) clearTimeout(giveUp);
      timer = giveUp = null;
    };

    const finish = async (ok, { verify = false } = {}) => {
      if (settled) return;
      settled = true;
      cleanup();
      if (ok && verify) {
        // Confirm with the platform before promising the user it worked. A
        // cookie can appear mid-2FA (Facebook sets c_user before the
        // checkpoint completes) and would otherwise be recorded as success.
        const live = await probeSignedIn(p, 20000);
        if (live === false) ok = false;
      }
      if (ok) {
        // Write the cookie jar to disk NOW so the session survives a quit or a
        // reboot — otherwise the user could be asked to sign in again.
        try {
          await sesFor(id).cookies.flushStore();
        } catch {
          /* best effort */
        }
        deps.patchAccount(id, {
          connected: true,
          connectedAt: new Date().toISOString(),
          lastResult: null,
          detail: null,
          failures: 0,
        });
        deps.broadcast();
      }
      if (!win.isDestroyed()) {
        setTimeout(() => {
          try {
            win.destroy();
          } catch {
            /* already gone */
          }
        }, ok ? 700 : 0);
      }
      resolve({ connected: ok });
    };

    // Cookies were cleared before this window opened, so any auth cookie that
    // shows up now is genuinely new. Still verify — see finish({verify}).
    if (p.cookie) {
      timer = setInterval(() => {
        isSignedIn(p).then((ok) => ok && finish(true, { verify: true }), () => {});
      }, 2000);
      if (timer.unref) timer.unref();
    }
    win.webContents.on("did-navigate", (_e, url) => {
      if (p.connectedPattern && p.connectedPattern.test(url)) finish(true, { verify: true });
    });
    // The user may just walk away. Don't hold a window (or this promise) open
    // forever; 20 minutes is far longer than any real sign-in with 2FA.
    giveUp = setTimeout(() => finish(false), 20 * 60 * 1000);
    if (giveUp.unref) giveUp.unref();

    win.on("closed", () => {
      // Closing the window is the user saying "done". Ask the platform whether
      // it worked — a cookie check alone is wrong for platforms that have no
      // cookie configured (Snapchat), which would otherwise always report a
      // failed sign-in even when it succeeded.
      if (settled) return;
      probeSignedIn(p, 20000).then(
        (live) => {
          if (live === true) return finish(true);
          if (live === false) return finish(false);
          isSignedIn(p).then((ok) => finish(ok), () => finish(false)); // indeterminate
        },
        () => finish(false)
      );
    });

    win.loadURL(p.loginUrl).catch(() => {
      // Offline / DNS failure: fail fast with a useful message instead of a
      // blank window the user has to guess about.
      if (!win.isDestroyed()) {
        win.webContents.executeJavaScript(
          `document.body.innerHTML='<div style="font:15px -apple-system;padding:40px;color:#333">Could not reach ${p.name}. Check your internet connection, then close this window and try again.</div>'`
        ).catch(() => {});
      }
    });
  });
}

async function disconnect(id) {
  closeAttention(id);
  // A pass may be mid-flight against the session we are about to wipe; let it
  // stop rather than have it land on a login page and write "reconnect" back
  // onto the account the user just disconnected.
  busy.delete(id);
  const ses = sesFor(id);
  try {
    await ses.clearStorageData();
  } catch {
    /* nothing to clear */
  }
  // Storage alone leaves the HTTP cache (and cached authenticated responses)
  // on disk. Disconnect should mean gone.
  try {
    await ses.clearCache();
    await ses.clearAuthCache();
  } catch {
    /* best effort */
  }
  uaApplied.delete(id);
  if (!ready()) return;
  deps.patchAccount(id, {
    connected: false,
    lastResult: null,
    detail: null,
    failures: 0,
    archiveKeys: [],
  });
  deps.broadcast();
}

/**
 * Chromium's HTTP cache for these partitions grows without bound — seven
 * image-heavy SPAs over weeks becomes gigabytes the user never sees. Called
 * periodically from the scheduler; sessions (cookies) are untouched.
 */
async function trimCaches() {
  for (const p of PLATFORMS.auto()) {
    if (busy.has(p.id) || hasAttention(p.id)) continue;
    if ((inFlightDownloads.get(p.id) || 0) > 0) continue;
    try {
      await sesFor(p.id).clearCache();
    } catch {
      /* partition may not exist */
    }
  }
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
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

  // executeJavaScript does NOT reject when the frame is destroyed or navigates
  // away mid-evaluation — the promise simply never settles. A single one of
  // those inside the click loop would otherwise eat the whole pass budget.
  const exec = (code) =>
    Promise.race([
      win.isDestroyed()
        ? Promise.resolve(null)
        : win.webContents.executeJavaScript(code, true).catch(() => null),
      sleep(8000).then(() => null),
    ]);

  const goto = (url) =>
    new Promise((resolve) => {
      let done = false;
      let timer = null;
      const fin = (failed) => {
        if (done) return;
        done = true;
        if (timer) clearTimeout(timer);
        win.webContents.removeListener("did-finish-load", onLoad);
        win.webContents.removeListener("did-fail-load", onFail);
        resolve(!failed);
      };
      const onLoad = () => fin(false);
      const onFail = (_e, _code, _desc, _url, isMainFrame) => {
        // Sub-resource failures are normal and irrelevant; only a main-frame
        // failure means we are not where we think we are.
        if (isMainFrame) fin(true);
      };
      if (win.isDestroyed()) return fin(true);
      win.webContents.on("did-finish-load", onLoad);
      win.webContents.on("did-fail-load", onFail);
      timer = setTimeout(() => fin(false), 25000); // slow SPA, not a failure
      win.loadURL(url).catch(() => fin(true));
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
  if (!p || !p.auto || busy.has(id) || !ready()) return;
  const acct = deps.getAccount(id);
  if (!acct.connected) return;
  if (hasAttention(id)) return; // a window is already open waiting for the user

  // Offline is not a failure: doing nothing and retrying later is correct, and
  // counting it would push the account into a long backoff for no reason.
  if (!net.isOnline()) return;

  busy.add(id);
  const prevResult = acct.lastResult;

  let win = null;
  let outcome = null; // null = the download pipeline owns the final state
  let keepWindow = false;
  let timeoutHandle = null;
  const cleanupFns = [];

  try {
    // Everything below is inside the try: patchAccount and broadcast rebuild
    // the tray menu and reach into the renderer, so a throw there would leave
    // `busy` set forever and silently retire this platform.
    deps.patchAccount(id, {
      lastAttempt: new Date().toISOString(),
      lastResult: "running",
      detail: null,
    });
    deps.broadcast();
    prepareSession(id);
    ensureDownloadCapture(id);

    win = new BrowserWindow({
      width: 1100,
      height: 800,
      show: false, // never steal focus; only shown if the user is needed
      title: `${p.name} — Cold Storage`,
      webPreferences: {
        partition: partitionFor(id),
        contextIsolation: true,
        nodeIntegration: false,
        // Hidden windows are throttled hard by Chromium, which stalls the
        // single-page apps we have to drive. Only on while a sync runs.
        backgroundThrottling: false,
      },
    });
    const theWin = win;
    theWin.webContents.setWindowOpenHandler(({ url }) => {
      theWin.loadURL(url).catch(() => {}); // see note in interactiveSignIn
      return { action: "deny" };
    });
    theWin.on("closed", () => {
      // Identity-checked: never clear an entry another pass registered.
      if (attentionWins.get(id) === theWin) attentionWins.delete(id);
    });
    // A renderer that runs out of memory or crashes must not wedge the sync.
    theWin.webContents.on("render-process-gone", () => {
      events.emit("gone:" + id);
    });

    const ctx = makeCtx(theWin);

    // Hard ceiling on the whole pass — but as a moving deadline, not a fixed
    // timer. A large archive legitimately takes hours to transfer; a fixed
    // 15-minute race would abort it, mark the account failed, and have the
    // scheduler start a SECOND download of the same file 30 minutes later.
    let deadline = Date.now() + SYNC_HARD_TIMEOUT_MS;
    const extendDeadline = (ms) => {
      deadline = Math.max(deadline, Date.now() + ms);
    };
    const onDownloadStart = () => extendDeadline(DOWNLOAD_TIMEOUT_MS);
    events.on("downloadstart:" + id, onDownloadStart);
    cleanupFns.push(() => events.removeListener("downloadstart:" + id, onDownloadStart));

    const timeout = new Promise((resolve) => {
      timeoutHandle = setInterval(() => {
        // While bytes are moving, keep pushing the deadline out.
        if (inFlightDownloads.get(id) > 0) extendDeadline(30 * 60 * 1000);
        if (Date.now() > deadline) resolve("__timeout__");
      }, 10000);
      if (timeoutHandle.unref) timeoutHandle.unref();
    });

    // A renderer that crashes (OOM on a heavy SPA after weeks of uptime) must
    // end the pass immediately instead of burning the whole budget idle.
    const crashed = new Promise((resolve) => {
      const onGone = () => resolve({ result: "error", detail: "The page crashed — will retry." });
      events.once("gone:" + id, onGone);
      cleanupFns.push(() => events.removeListener("gone:" + id, onGone));
    });

    const work = (async () => {
      await ctx.goto(p.exportUrl);
      await ctx.sleep(3500);
      if (theWin.isDestroyed()) return { result: "error", detail: "Window closed" };
      const url = theWin.webContents.getURL();

      if (p.loginRe && p.loginRe.test(url)) {
        // Authoritative: the platform bounced us to its login page.
        return { result: "reconnect", detail: `Your ${p.name} sign-in expired — reconnect once.` };
      }

      // Phase 1 — is an archive ready? Click Download; capture takes over.
      // Meta and Google can take a minute to turn the click into a transfer
      // (server-side assembly, then redirects), so wait generously: giving up
      // early would file a *new* export request over a download about to start.
      //
      // Large exports are split into parts ("part 1 of 4"). Downloading only
      // the first and reporting success would quietly back up a fraction of
      // the user's data, so keep going until no ready part is left.
      let parts = 0;
      for (; parts < MAX_PARTS; parts++) {
        if (theWin.isDestroyed()) break;
        const clicked = p.tryDownload ? await p.tryDownload(ctx) : false;
        if (!clicked) break;
        const started = await waitEvent("downloadstart:" + id, 90000);
        if (!started) break;
        const ok = await waitEvent("downloaded:" + id, DOWNLOAD_TIMEOUT_MS);
        if (ok === null) return null; // still transferring; the queue owns it
        if (!ok) return { result: "error", detail: "The download did not complete." };
        await ctx.sleep(2500); // let the page settle before looking for the next part
        try {
          await ctx.goto(p.exportUrl); // re-read the list; the part we took is now gone
          await ctx.sleep(2500);
        } catch {
          break;
        }
      }
      if (parts > 0) return null; // the download/ingest pipeline set the state

      // Phase 2 — request a fresh export.
      const requested = p.tryRequest ? await p.tryRequest(ctx) : false;
      if (requested) {
        return {
          result: "requested",
          detail: `${p.name} is preparing your export — we check back on our own.`,
        };
      }

      // Found nothing to click. Before blaming the user, re-check whether we
      // were quietly logged out: some platforms render a login wall without
      // ever changing the URL, and some redirect later than our first sample.
      const nowUrl = theWin.isDestroyed() ? "" : theWin.webContents.getURL();
      const walled =
        (p.loginRe && p.loginRe.test(nowUrl)) ||
        (await ctx.has(["log in to", "sign in to", "enter your password", "create new account"]));
      if (walled || (p.cookie && !(await isSignedIn(p)))) {
        return { result: "reconnect", detail: `Your ${p.name} sign-in expired — reconnect once.` };
      }
      return { result: "attention", detail: p.attentionHint || "Finish this step in the window." };
    })();

    const raced = await Promise.race([work, timeout, crashed]);
    outcome =
      raced === "__timeout__"
        ? { result: "error", detail: `${p.name} did not respond in time — will retry.` }
        : raced;
  } catch (e) {
    // Only errors the recipes raise on purpose are things the USER can fix.
    // An internal fault (destroyed webContents, DNS failure) must not park a
    // window on screen and block this platform from ever syncing again.
    outcome = e && e.needsUser
      ? { result: "attention", detail: String(e.message) }
      : { result: "error", detail: String((e && e.message) || e) };
  } finally {
    if (timeoutHandle) clearInterval(timeoutHandle);
    for (const fn of cleanupFns) {
      try {
        fn();
      } catch {
        /* ignore */
      }
    }
    busy.delete(id); // released on EVERY path, including throws and timeouts
  }

  const needsUser = !!outcome && (outcome.result === "attention" || outcome.result === "reconnect");

  if (needsUser && (interactive || surfaceOnAttention) && win && !win.isDestroyed()) {
    // Surface this exact page so the user finishes in one click.
    keepWindow = true;
    attentionWins.set(id, win);
    // Throttling was off so we could drive the page; a parked window is just a
    // live social-media SPA burning battery. Hand it back to Chromium.
    try {
      win.webContents.setBackgroundThrottling(true);
      win.webContents.setAudioMuted(true);
    } catch {
      /* not fatal */
    }
    win.show();
    win.focus();
    // A window the user hides instead of closing would otherwise live forever:
    // it blocks this account from ever syncing again AND keeps a full renderer
    // alive on a live social page. Reclaim it; the state and the tray entry
    // remain, so the user can retry whenever they like.
    const reaper = setTimeout(() => {
      if (attentionWins.get(id) === win) {
        attentionWins.delete(id);
        try {
          if (!win.isDestroyed()) win.destroy();
        } catch {
          /* already gone */
        }
        if (ready()) deps.broadcast();
      }
    }, ATTENTION_WINDOW_TTL_MS);
    if (reaper.unref) reaper.unref();
  }
  if (!keepWindow && win && !win.isDestroyed()) {
    // Never tear down a window while a transfer is running: the archive we
    // came for would be cancelled at 90%.
    if (inFlightDownloads.get(id) > 0) {
      const w = win;
      const wait = setInterval(() => {
        if (!(inFlightDownloads.get(id) > 0)) {
          clearInterval(wait);
          try {
            if (!w.isDestroyed()) w.destroy();
          } catch {
            /* already gone */
          }
        }
      }, 5000);
      if (wait.unref) wait.unref();
    } else {
      try {
        win.destroy(); // destroy, not close: no beforeunload can hang it
      } catch {
        /* already gone */
      }
    }
  }

  if (outcome) {
    const failed = outcome.result === "error";
    deps.patchAccount(id, {
      lastResult: outcome.result,
      detail: outcome.detail || null,
      failures: failed ? (acct.failures || 0) + 1 : 0,
    });
    // Notify once per transition, not on every retry of the same state.
    if (needsUser && prevResult !== outcome.result) {
      notify(`${p.name} needs a quick click`, "Open Cold Storage to finish — it takes a second.");
    }
  }
  deps.broadcast();
}

function notify(title, body) {
  try {
    if (Notification.isSupported()) new Notification({ title, body }).show();
  } catch {
    /* notifications are optional */
  }
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
  flushSessions,
  closeAttention,
  closeAllAttention,
  trimCaches,
  probeSignedIn,
  domainMatches,
  findClickCode, // exported for tests
  SYNC_HARD_TIMEOUT_MS,
};
