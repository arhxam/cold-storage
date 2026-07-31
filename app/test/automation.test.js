// Smoke test for the automation engine's page-driving logic.
//
// Run with:  npx electron app/test/automation.test.js
//
// The risky part of automation.js is `makeCtx`: the text matcher that decides
// which button to press on a platform's export page. A wrong match could click
// "Delete account" instead of "Download". So we drive the *real* helpers
// against local mock pages that mimic each platform's markup, and assert both
// what gets clicked and — just as importantly — what does not.

const { app, BrowserWindow } = require("electron");
const path = require("path");

// The matcher is exported from automation.js so this test drives the REAL
// code path — not a copy that could drift away from what ships.
const automation = require("../automation");
const PLATFORMS = require("../platforms");

let failures = 0;
let checks = 0;

function ok(cond, label) {
  checks++;
  if (cond) {
    console.log(`  PASS  ${label}`);
  } else {
    failures++;
    console.log(`  FAIL  ${label}`);
  }
}

const PAGES = {
  // Meta DYI page with an archive ready. The trap: "Download or transfer
  // information" must NOT be treated as the ready-archive "Download" button.
  metaReady: `<body>
    <a role="link">Download or transfer information</a>
    <div><span>Your file is ready</span><button>Download</button></div>
    <button>Delete account</button>
  </body>`,
  // Meta DYI page with nothing ready — only the request entry point.
  metaEmpty: `<body>
    <a role="link">Download or transfer information</a>
    <button>Delete account</button>
  </body>`,
  // X settings page, archive ready.
  xReady: `<body>
    <div role="button">Download archive</div>
    <div role="button">Request archive</div>
  </body>`,
  xEmpty: `<body><div role="button">Request archive</div></body>`,
  // Hidden elements must be ignored (platforms keep offscreen templates).
  hidden: `<body>
    <button style="display:none">Download</button>
    <button style="width:0;height:0;overflow:hidden">Download</button>
  </body>`,
};

async function pageCtx(win, html) {
  await win.loadURL("data:text/html;charset=utf-8," + encodeURIComponent(html));
  const exec = (code) => win.webContents.executeJavaScript(code, true).catch(() => null);
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  const findClickCode = automation.findClickCode; // the real shipping matcher
  return {
    win,
    exec,
    sleep,
    goto: async () => {},
    click: async (patterns, { timeout = 1200, exact = false } = {}) => {
      const end = Date.now() + timeout;
      while (Date.now() < end) {
        const hit = await exec(findClickCode(patterns, exact));
        if (hit) return hit;
        await sleep(150);
      }
      return null;
    },
    has: async () => false,
    hasSel: async (sel) => !!(await exec(`!!document.querySelector(${JSON.stringify(sel)})`)),
  };
}

app.whenReady().then(async () => {
  const win = new BrowserWindow({ show: false, webPreferences: { offscreen: true } });

  console.log("\nExact-match discipline (the dangerous part)");
  let ctx = await pageCtx(win, PAGES.metaReady);
  ok(
    (await ctx.click(["download"], { exact: true })) === "download",
    'exact "download" hits the ready-archive button, not "Download or transfer…"'
  );
  ctx = await pageCtx(win, PAGES.metaEmpty);
  ok(
    (await ctx.click(["download"], { exact: true })) === null,
    'exact "download" finds nothing when only "Download or transfer…" exists'
  );
  ok(
    (await ctx.click(["delete account"], { exact: true })) === "delete account",
    "matcher can see destructive buttons — so recipes must never name them"
  );

  console.log("\nNo recipe ever names a destructive control");
  const SRC = require("fs").readFileSync(path.join(__dirname, "..", "platforms.js"), "utf8");
  for (const bad of ["delete", "deactivate", "remove account", "close account"]) {
    ok(!new RegExp(`["'\`][^"'\`]*${bad}`, "i").test(SRC), `platforms.js never clicks "${bad}"`);
  }

  console.log("\nPer-platform recipes");
  ctx = await pageCtx(win, PAGES.metaReady);
  ok(await PLATFORMS.byId("instagram").tryDownload(ctx), "instagram: ready archive -> download");
  ctx = await pageCtx(win, PAGES.metaEmpty);
  ok(!(await PLATFORMS.byId("instagram").tryDownload(ctx)), "instagram: nothing ready -> no download");
  ctx = await pageCtx(win, PAGES.xReady);
  ok(await PLATFORMS.byId("twitter").tryDownload(ctx), "x: ready archive -> download");
  ctx = await pageCtx(win, PAGES.xEmpty);
  ok(!(await PLATFORMS.byId("twitter").tryDownload(ctx)), "x: nothing ready -> no download");
  ok(await PLATFORMS.byId("twitter").tryRequest(ctx), "x: empty page -> request archive");

  console.log("\nInvisible elements are ignored");
  ctx = await pageCtx(win, PAGES.hidden);
  ok((await ctx.click(["download"], { exact: true })) === null, "hidden/zero-size buttons never clicked");

  console.log("\nCookie domain matching (a substring match would accept impostors)");
  const dm = automation.domainMatches;
  ok(dm("x.com", "x.com") && dm(".x.com", "x.com"), "exact and dot-prefixed match");
  ok(dm("api.x.com", "x.com"), "subdomains match");
  ok(!dm("netflix.com", "x.com"), "netflix.com does NOT satisfy x.com");
  ok(!dm("notgoogle.com", "google.com"), "notgoogle.com does NOT satisfy google.com");
  ok(!dm("evil-x.com", "x.com"), "evil-x.com does NOT satisfy x.com");

  console.log("\nUser-facing vs internal errors");
  {
    // Only errors the recipes raise on purpose may surface a window; an
    // internal fault must never park one (it would block the platform).
    const src = require("fs").readFileSync(path.join(__dirname, "..", "platforms.js"), "utf8");
    ok(/e\.needsUser\s*=\s*true/.test(src), "platforms.js tags user-facing errors");
    ok(!/throw new Error\(/.test(src), "no untagged throws remain in recipes");
    let tagged = null;
    try {
      await PLATFORMS.byId("twitter").tryRequest(
        await pageCtx(win, `<body><input type="password"></body>`)
      );
    } catch (e) {
      tagged = e;
    }
    ok(!!tagged && tagged.needsUser === true, "X password-confirm raises a user-facing error");
  }

  console.log("\nMultipart exports");
  {
    // Two parts ready: the runner must not stop after the first.
    const twoParts = `<body>
      <div><span>part 1 of 2</span><button>Download</button></div>
      <div><span>part 2 of 2</span><button>Download</button></div>
    </body>`;
    ctx = await pageCtx(win, twoParts);
    ok(await PLATFORMS.byId("instagram").tryDownload(ctx), "part 1 is clickable");
    ok(await PLATFORMS.byId("instagram").tryDownload(ctx), "part 2 is also clickable");
    const src = require("fs").readFileSync(path.join(__dirname, "..", "automation.js"), "utf8");
    ok(/MAX_PARTS/.test(src), "runSync loops over parts rather than taking only the first");
  }

  console.log("\nUser agent (Google blocks sign-in from 'Electron' browsers)");
  {
    const src = require("fs").readFileSync(path.join(__dirname, "..", "automation.js"), "utf8");
    ok(/setUserAgent/.test(src), "a user agent is set on each partition");
    ok(!/Electron/.test((src.match(/const CHROME_UA[\s\S]*?\}\)\(\);/) || [""])[0]),
      "the presented UA contains no Electron token");
  }

  console.log("\nRegistry sanity");
  const all = PLATFORMS.all();
  ok(all.length >= 10, `${all.length} platforms registered`);
  for (const p of PLATFORMS.auto()) {
    ok(
      !!(p.loginUrl && p.exportUrl && p.tryDownload),
      `${p.id}: has loginUrl + exportUrl + tryDownload`
    );
    ok(
      /^https:\/\//.test(p.loginUrl) && /^https:\/\//.test(p.exportUrl),
      `${p.id}: only https endpoints`
    );
  }
  for (const p of all.filter((x) => !x.auto)) {
    ok(!!p.manualReason, `${p.id}: manual platform explains why`);
  }
  ok(typeof automation.runSync === "function", "automation exposes runSync");

  // -- the connect flow actually opens the platform's real login page --------
  console.log("\nConnect flow (opens a real sign-in window, signs in to nothing)");
  const patched = {};
  automation.init({
    sytHome: () => require("os").tmpdir(),
    ingest: async () => true,
    getAccount: () => ({ connected: false, schedule: "manual" }),
    patchAccount: (id, p) => Object.assign((patched[id] = patched[id] || {}), p),
    broadcast: () => {},
  });
  const before = BrowserWindow.getAllWindows().length;
  automation.connect("instagram"); // resolves only once signed in / closed
  // connect() first probes the platform (hidden window) and only then opens the
  // sign-in window, so poll rather than assuming a fixed delay.
  let loginWin = null;
  for (let i = 0; i < 60 && !loginWin; i++) {
    await new Promise((r) => setTimeout(r, 1000));
    loginWin = BrowserWindow.getAllWindows().find((w) => {
      try {
        return !w.isDestroyed() && /instagram\.com/.test(w.webContents.getURL());
      } catch {
        return false;
      }
    });
  }
  ok(BrowserWindow.getAllWindows().length > before, "connect() opened a window");
  ok(!!loginWin, "a window reaches instagram.com (the real sign-in page)");
  ok(
    !!loginWin && loginWin.webContents.session !== require("electron").session.defaultSession,
    "it uses an isolated per-platform session partition"
  );
  ok(!patched.instagram || !patched.instagram.connected, "not marked connected without a sign-in");
  for (const w of BrowserWindow.getAllWindows()) {
    try {
      w.destroy();
    } catch {
      /* ignore */
    }
  }

  console.log(`\n${checks - failures}/${checks} checks passed`);
  app.exit(failures ? 1 : 0);
});
