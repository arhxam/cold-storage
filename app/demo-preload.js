// DEMO preload — a stateful, entirely fake `coldBridge`, used ONLY when the app
// is launched with COLD_DEMO=1 (see main.js). Nothing here connects, signs in,
// or touches a real account. Connecting is simulated with a short, believable
// progression so it can be filmed. The conversation/media data the UI shows is
// real, seeded into the demo archive by tools/seed_demo.py and served by the
// engine — this file only fakes the Accounts page (connect / schedule / sync).

const { contextBridge } = require("electron");

const now = () => new Date().toISOString();
const agoISO = (mins) => new Date(Date.now() - mins * 60 * 1000).toISOString();

// Initial fleet: several already connected with history (so the dashboard looks
// lived-in), a few auto platforms left disconnected so a connection can be
// demonstrated live on camera, and the manual ones as add-by-hand.
const ACCOUNTS = [
  { id: "instagram", name: "Instagram", auto: true, connected: true, schedule: "daily",   lastSuccess: agoISO(37) },
  { id: "facebook",  name: "Facebook & Messenger", auto: true, connected: true, schedule: "weekly", lastSuccess: agoISO(190) },
  { id: "twitter",   name: "X (Twitter)", auto: true, connected: true, schedule: "weekly",  lastSuccess: agoISO(320) },
  { id: "google",    name: "Google", auto: true, connected: true, schedule: "monthly", lastSuccess: agoISO(1500) },
  { id: "snapchat",  name: "Snapchat", auto: true, connected: false, schedule: "manual" },
  { id: "linkedin",  name: "LinkedIn", auto: true, connected: false, schedule: "manual" },
  { id: "reddit",    name: "Reddit", auto: true, connected: false, schedule: "manual" },
  { id: "discord",  name: "Discord",  auto: false, manualReason: "Discord only delivers exports by email link — add the downloaded package here." },
  { id: "telegram", name: "Telegram", auto: false, manualReason: "Exports come from Telegram Desktop (Settings, Advanced, Export Telegram data)." },
  { id: "whatsapp", name: "WhatsApp", auto: false, manualReason: "Exports come from the phone app (Settings, Chats, Export chat)." },
  { id: "slack",    name: "Slack",    auto: false, manualReason: "Workspace exports are owner-only, from Slack's admin pages." },
];

const byId = (id) => ACCOUNTS.find((a) => a.id === id);
const snapshot = () => ACCOUNTS.map((a) => ({ ...a }));

// Subscribers registered by the UI via onAccounts(); we push the fleet to them
// as a simulated backup progresses, so the tile animates just like the real one.
const subs = new Set();
const fire = () => subs.forEach((cb) => { try { cb(snapshot()); } catch (_) {} });

const timers = [];
const later = (ms, fn) => timers.push(setTimeout(fn, ms));

// Simulate the real connect → prepare → download → ingest → done progression.
function runBackup(a, { firstConnect } = {}) {
  a.busy = true;
  a.lastResult = "requested";
  a.detail = a.name + " is preparing your export";
  fire();
  later(1100, () => { a.lastResult = "downloading"; a.detail = "Downloading your export — 46%"; fire(); });
  later(2100, () => { a.detail = "Downloading your export — 88%"; fire(); });
  later(2900, () => { a.lastResult = "ingesting"; a.detail = "Adding it to your encrypted archive…"; fire(); });
  later(4100, () => {
    a.busy = false;
    a.lastResult = null;
    a.detail = null;
    a.lastSuccess = now();
    fire();
  });
}

contextBridge.exposeInMainWorld("coldBridge", {
  isElectron: true,

  accounts: () => Promise.resolve(snapshot()),

  connect: (id) => {
    const a = byId(id);
    if (a) {
      a.connected = true;
      if (!a.schedule || a.schedule === "manual") a.schedule = "daily";
      runBackup(a, { firstConnect: true });
    }
    return Promise.resolve(true);
  },

  disconnect: (id) => {
    const a = byId(id);
    if (a) {
      a.connected = false;
      a.lastSuccess = null;
      a.lastResult = null;
      a.detail = null;
      a.busy = false;
      fire();
    }
    return Promise.resolve(true);
  },

  setSchedule: (id, schedule) => {
    const a = byId(id);
    if (a) { a.schedule = schedule; fire(); }
    return Promise.resolve(true);
  },

  syncNow: (id) => {
    const a = byId(id);
    if (a && a.connected) runBackup(a, {});
    return Promise.resolve(true);
  },

  syncAll: () => {
    ACCOUNTS.filter((a) => a.connected).forEach((a, i) => later(i * 250, () => runBackup(a, {})));
    return Promise.resolve(true);
  },

  getPrefs: () => Promise.resolve({ launchAtLogin: true, runInBackground: true }),
  setPref: () => Promise.resolve(true),

  // Native side-effects: inert in the demo.
  addExport: () => Promise.resolve(true),
  openExternal: () => Promise.resolve(true),
  revealDataFolder: () => Promise.resolve(true),
  showRecoveryKit: () => Promise.resolve(true),

  onIngest: () => () => {},
  onAccounts: (cb) => {
    subs.add(cb);
    return () => subs.delete(cb);
  },
});
