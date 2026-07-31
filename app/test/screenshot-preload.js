// Preload used ONLY for capturing README screenshots.
//
// The UI decides once, at load, whether it is running inside the desktop app
// (`window.sytBridge`). To photograph the app-mode chrome we have to provide
// that bridge before the page script runs — hence a preload rather than an
// injected script. Every method is inert: nothing connects, syncs, or touches
// a real account.

const { contextBridge } = require("electron");

const ACCOUNTS = [
  { id: "instagram", name: "Instagram", auto: true, connected: true, schedule: "daily",
    lastResult: "synced", lastSuccess: new Date(Date.now() - 41 * 60 * 1000).toISOString() },
  { id: "facebook", name: "Facebook", auto: true, connected: true, schedule: "weekly",
    lastResult: "requested", detail: "Facebook is preparing your export" },
  { id: "twitter", name: "X / Twitter", auto: true, connected: true, schedule: "weekly",
    lastResult: "downloading", detail: "Downloading — 60%" },
  { id: "google", name: "Google", auto: true, connected: true, schedule: "monthly",
    lastResult: "attention",
    detail: "One-time setup: pick an export schedule in Google Takeout." },
  { id: "snapchat", name: "Snapchat", auto: true, connected: false, schedule: "manual" },
  { id: "linkedin", name: "LinkedIn", auto: true, connected: false, schedule: "manual" },
  { id: "reddit", name: "Reddit", auto: true, connected: false, schedule: "manual" },
  { id: "discord", name: "Discord", auto: false,
    manualReason: "Discord only delivers exports by email link — add the downloaded package here." },
  { id: "telegram", name: "Telegram", auto: false,
    manualReason: "Exports come from Telegram Desktop (Settings, Advanced, Export Telegram data)." },
  { id: "whatsapp", name: "WhatsApp", auto: false,
    manualReason: "Exports come from the phone app (Settings, Chats, Export chat)." },
  { id: "slack", name: "Slack", auto: false,
    manualReason: "Workspace exports are owner-only, from Slack's admin pages." },
];

const noop = () => Promise.resolve(true);

contextBridge.exposeInMainWorld("sytBridge", {
  isElectron: true,
  accounts: () => Promise.resolve(ACCOUNTS),
  getPrefs: () => Promise.resolve({ launchAtLogin: true, runInBackground: true }),
  connect: noop,
  disconnect: noop,
  setSchedule: noop,
  syncNow: noop,
  syncAll: noop,
  setPref: noop,
  addExport: noop,
  openExternal: noop,
  revealDataFolder: noop,
  showRecoveryKit: noop,
  onIngest: () => () => {},
  onAccounts: () => () => {},
});
