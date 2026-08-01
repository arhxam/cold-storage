// Preload bridge — the ONLY channel between the served web UI (loopback HTTP)
// and the native shell. contextIsolation is on, so the renderer can't touch
// Node/Electron directly; it gets exactly this small, audited surface.
//
// Everything here is safe-by-construction: no arbitrary command execution, and
// openExternal only accepts https URLs (enforced again in the main process).

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("coldBridge", {
  isElectron: true,

  // Native "pick a file/folder and back it up" flow. Detection is automatic.
  addExport: () => ipcRenderer.invoke("cold:pickAndIngest"),

  // Open a platform's official data-download page in the user's real browser.
  openExternal: (url) => ipcRenderer.invoke("cold:openExternal", url),

  // Reveal the local data folder in Finder.
  revealDataFolder: () => ipcRenderer.invoke("cold:revealDataFolder"),

  // Open the Recovery Kit file.
  showRecoveryKit: () => ipcRenderer.invoke("cold:showRecoveryKit"),

  // --- accounts & automation ------------------------------------------------
  // Sign in once per platform; after that the app requests, downloads and
  // ingests official exports on a schedule with no further input.
  accounts: () => ipcRenderer.invoke("cold:accounts"),
  connect: (id) => ipcRenderer.invoke("cold:connect", id),
  disconnect: (id) => ipcRenderer.invoke("cold:disconnect", id),
  setSchedule: (id, schedule) => ipcRenderer.invoke("cold:setSchedule", id, schedule),
  syncNow: (id) => ipcRenderer.invoke("cold:syncNow", id),
  syncAll: () => ipcRenderer.invoke("cold:syncAll"),
  getPrefs: () => ipcRenderer.invoke("cold:getPrefs"),
  setPref: (key, value) => ipcRenderer.invoke("cold:setPref", key, value),

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
    ipcRenderer.on("cold:ingest", listener);
    return () => ipcRenderer.removeListener("cold:ingest", listener);
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
    ipcRenderer.on("cold:accounts", listener);
    return () => ipcRenderer.removeListener("cold:accounts", listener);
  },
});
