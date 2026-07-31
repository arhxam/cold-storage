// platforms.js — single source of truth for every platform we can back up.
//
// `auto: true` platforms support the automated pipeline: the user signs in
// ONCE inside the app (the session persists in its own partition), and from
// then on the app requests the official data export, polls until the platform
// has prepared it, downloads it, and ingests it into the local encrypted
// archive — on a schedule, in the background.
//
// The recipes only drive the platform's own account pages: the exact clicks
// the user would make, from their own device, in their own session. No
// scraping, no third-party APIs, no credentials ever touch our code — the
// session cookie jar lives in Electron's per-platform partition.
//
// Recipes get a `ctx` with: goto(url), sleep(ms), click(patterns, opts),
// has(textPatterns), hasSel(cssSelector), exec(js). `click` matches visible
// buttons/links/labels by text; `{exact:true}` requires an exact text match
// (so a bare "download" never hits "Download or transfer information").
// Return value contract:
//   tryDownload(ctx) -> true if a download was triggered (archive was ready)
//   tryRequest(ctx)  -> true if a new export request was submitted
//   throw Error(msg) -> "needs attention": the window is shown with `msg`.

// ---------------------------------------------------------------------------
// Meta (Instagram / Facebook) — Account Center "Download your information"
// ---------------------------------------------------------------------------

async function metaDownload(ctx) {
  // When an archive is ready the DYI page lists it with a plain "Download"
  // button. Exact match so we never hit "Download or transfer information".
  return !!(await ctx.click(["download"], { timeout: 7000, exact: true }));
}

async function metaRequest(ctx) {
  const opened = await ctx.click(
    ["download or transfer information", "download or transfer your information"],
    { timeout: 15000 }
  );
  if (!opened) return false;
  // Profile picker (if the account center manages several profiles).
  await ctx.click(["next", "continue"], { timeout: 6000 });
  await ctx.click(["all available information"], { timeout: 9000 });
  await ctx.click(["next", "continue"], { timeout: 5000 });
  await ctx.click(["download to device"], { timeout: 9000 });
  await ctx.click(["next", "continue"], { timeout: 5000 });
  // The engine parses JSON — flip the format if the option is exposed here.
  if (await ctx.click(["format"], { timeout: 5000 })) {
    await ctx.click(["json"], { timeout: 5000 });
    await ctx.click(["save", "done"], { timeout: 5000 });
  }
  const created = await ctx.click(["create files", "create file"], { timeout: 12000 });
  if (!created) {
    throw new Error("Almost there — pick JSON format and press Create files in the window.");
  }
  return true;
}

// ---------------------------------------------------------------------------
// X / Twitter
// ---------------------------------------------------------------------------

async function xDownload(ctx) {
  return !!(await ctx.click(["download archive"], { timeout: 7000, exact: true }));
}

async function xRequest(ctx) {
  if (await ctx.hasSel('input[type="password"]')) {
    throw new Error("X wants your password to confirm — enter it in the window, then press Request archive.");
  }
  const hit = await ctx.click(["request archive"], { timeout: 10000, exact: true });
  if (!hit) return false;
  await ctx.sleep(2500);
  if (await ctx.hasSel('input[type="password"]')) {
    throw new Error("X wants your password to confirm — enter it in the window, then press Request archive.");
  }
  return true;
}

// ---------------------------------------------------------------------------
// Google Takeout — downloads are fully automatic; the initial export setup is
// a one-time manual step (Takeout can then re-export on its own schedule).
// ---------------------------------------------------------------------------

async function googleDownload(ctx) {
  return !!(await ctx.click(["download"], { timeout: 7000, exact: true }));
}

async function googleRequest(_ctx) {
  return false; // handled via attentionHint — Takeout's builder is a one-time setup
}

// ---------------------------------------------------------------------------
// Snapchat / LinkedIn / Reddit
// ---------------------------------------------------------------------------

async function snapDownload(ctx) {
  return !!(await ctx.click(["download data", "download"], { timeout: 7000, exact: true }));
}
async function snapRequest(ctx) {
  return !!(await ctx.click(["submit request"], { timeout: 10000, exact: true }));
}

async function linkedinDownload(ctx) {
  return !!(await ctx.click(["download archive"], { timeout: 7000, exact: true }));
}
async function linkedinRequest(ctx) {
  await ctx.click(["download larger data archive"], { timeout: 8000 });
  return !!(await ctx.click(["request archive"], { timeout: 10000, exact: true }));
}

async function redditDownload(ctx) {
  return !!(await ctx.click(["download"], { timeout: 7000, exact: true }));
}
async function redditRequest(ctx) {
  return !!(await ctx.click(["request data", "submit"], { timeout: 10000, exact: true }));
}

// ---------------------------------------------------------------------------
// Registry
// ---------------------------------------------------------------------------

const PLATFORMS = [
  {
    id: "instagram",
    name: "Instagram",
    auto: true,
    loginUrl: "https://www.instagram.com/accounts/login/",
    exportUrl: "https://accountscenter.instagram.com/info_and_permissions/dyi/",
    cookie: { domain: "instagram.com", name: "sessionid" },
    loginRe: /(accounts\/login|\/login\/?($|\?)|checkpoint)/i,
    attentionHint: "Finish the export request in the window (choose JSON format).",
    tryDownload: metaDownload,
    tryRequest: metaRequest,
  },
  {
    id: "facebook",
    name: "Facebook",
    auto: true,
    loginUrl: "https://www.facebook.com/login/",
    exportUrl: "https://accountscenter.facebook.com/info_and_permissions/dyi/",
    cookie: { domain: "facebook.com", name: "c_user" },
    loginRe: /(\/login|checkpoint)/i,
    attentionHint: "Finish the export request in the window (choose JSON format).",
    tryDownload: metaDownload,
    tryRequest: metaRequest,
  },
  {
    id: "twitter",
    name: "X / Twitter",
    auto: true,
    loginUrl: "https://x.com/i/flow/login",
    exportUrl: "https://x.com/settings/download_your_data",
    cookie: { domain: "x.com", name: "auth_token" },
    loginRe: /(x|twitter)\.com\/(i\/flow\/)?login/i,
    attentionHint: "X asks for a password confirmation — one click in the window.",
    tryDownload: xDownload,
    tryRequest: xRequest,
  },
  {
    id: "google",
    name: "Google",
    auto: true,
    loginUrl: "https://accounts.google.com/",
    exportUrl: "https://takeout.google.com/settings/takeout/downloads",
    cookie: { domain: "google.com", name: "SID" },
    loginRe: /accounts\.google\.com\/(v3\/)?signin/i,
    attentionHint:
      "One-time setup: create an export in Google Takeout and pick " +
      '"Export every 2 months" — every future download is then automatic.',
    tryDownload: googleDownload,
    tryRequest: googleRequest,
  },
  {
    id: "snapchat",
    name: "Snapchat",
    auto: true,
    loginUrl: "https://accounts.snapchat.com/accounts/login?continue=%2Faccounts%2Fdownloadmydata",
    exportUrl: "https://accounts.snapchat.com/accounts/downloadmydata",
    connectedPattern: /accounts\.snapchat\.com\/accounts\/(downloadmydata|welcome)/i,
    loginRe: /accounts\.snapchat\.com\/accounts\/login/i,
    attentionHint: "Confirm the data request in the window.",
    tryDownload: snapDownload,
    tryRequest: snapRequest,
  },
  {
    id: "linkedin",
    name: "LinkedIn",
    auto: true,
    loginUrl: "https://www.linkedin.com/login",
    exportUrl: "https://www.linkedin.com/mypreferences/d/download-my-data",
    cookie: { domain: "linkedin.com", name: "li_at" },
    loginRe: /linkedin\.com\/(login|checkpoint|authwall|uas)/i,
    attentionHint: "Confirm the archive request in the window.",
    tryDownload: linkedinDownload,
    tryRequest: linkedinRequest,
  },
  {
    id: "reddit",
    name: "Reddit",
    auto: true,
    loginUrl: "https://www.reddit.com/login",
    exportUrl: "https://www.reddit.com/settings/data-request",
    cookie: { domain: "reddit.com", name: "reddit_session" },
    loginRe: /reddit\.com\/login/i,
    attentionHint: "Confirm the data request in the window.",
    tryDownload: redditDownload,
    tryRequest: redditRequest,
  },
  // --- no automatable web export flow exists for these -----------------------
  {
    id: "discord",
    name: "Discord",
    auto: false,
    manualReason: "Discord only delivers exports by email link — add the downloaded package here.",
  },
  {
    id: "telegram",
    name: "Telegram",
    auto: false,
    manualReason: "Exports come from Telegram Desktop (Settings, Advanced, Export Telegram data).",
  },
  {
    id: "whatsapp",
    name: "WhatsApp",
    auto: false,
    manualReason: "Exports come from the phone app (Settings, Chats, Export chat).",
  },
  {
    id: "slack",
    name: "Slack",
    auto: false,
    manualReason: "Workspace exports are owner-only, from Slack's admin pages.",
  },
];

const byId = Object.fromEntries(PLATFORMS.map((p) => [p.id, p]));

module.exports = {
  all: () => PLATFORMS,
  auto: () => PLATFORMS.filter((p) => p.auto),
  byId: (id) => byId[id] || null,
};
