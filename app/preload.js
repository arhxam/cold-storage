// Preload bridge — the ONLY channel between the served web UI (loopback HTTP)
// and the native shell. contextIsolation is on, so the renderer can't touch
// Node/Electron directly; it gets exactly this small, audited surface.
//
// Everything here is safe-by-construction: no arbitrary command execution, and
// openExternal only accepts https URLs (enforced again in the main process).

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("sytBridge", {
  isElectron: true,

  // Native "pick a file/folder and back it up" flow. Detection is automatic.
  addExport: () => ipcRenderer.invoke("syt:pickAndIngest"),

  // Open a platform's official data-download page in the user's real browser.
  openExternal: (url) => ipcRenderer.invoke("syt:openExternal", url),

  // Reveal the local data folder in Finder.
  revealDataFolder: () => ipcRenderer.invoke("syt:revealDataFolder"),

  // Open the Recovery Kit file.
  showRecoveryKit: () => ipcRenderer.invoke("syt:showRecoveryKit"),

  // --- accounts & automation ------------------------------------------------
  // Sign in once per platform; after that the app requests, downloads and
  // ingests official exports on a schedule with no further input.
  accounts: () => ipcRenderer.invoke("syt:accounts"),
  connect: (id) => ipcRenderer.invoke("syt:connect", id),
  disconnect: (id) => ipcRenderer.invoke("syt:disconnect", id),
  setSchedule: (id, schedule) => ipcRenderer.invoke("syt:setSchedule", id, schedule),
  syncNow: (id) => ipcRenderer.invoke("syt:syncNow", id),
  syncAll: () => ipcRenderer.invoke("syt:syncAll"),
  getPrefs: () => ipcRenderer.invoke("syt:getPrefs"),
  setPref: (key, value) => ipcRenderer.invoke("syt:setPref", key, value),

  // Subscribe to backup progress: cb({ phase: 'start'|'done'|'error', ... }).
  // Returns an unsubscribe function.
  onIngest: (cb) => {
    const listener = (_e, payload) => {
      try {
        cb(payload);
      } catch {
        /* ignore renderer callback errors */
      }
    };
    ipcRenderer.on("syt:ingest", listener);
    return () => ipcRenderer.removeListener("syt:ingest", listener);
  },

  // Live account-state pushes (connect, sync progress, schedule changes).
  onAccounts: (cb) => {
    const listener = (_e, payload) => {
      try {
        cb(payload);
      } catch {
        /* ignore renderer callback errors */
      }
    };
    ipcRenderer.on("syt:accounts", listener);
    return () => ipcRenderer.removeListener("syt:accounts", listener);
  },
});
