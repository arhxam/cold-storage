// store.js — crash-safe settings.
//
// This file holds the only state that cannot be rebuilt: which accounts are
// connected and on what schedule. Losing it means the user silently stops
// getting backups, so every write is atomic and every read tolerates a file
// that was truncated by a power cut.
//
// Writes go to a temp file, are fsync'd, then renamed over the target (rename
// is atomic on APFS/HFS+). Reads fall back to the previous good copy, then to
// defaults — a corrupt file is moved aside, never silently overwritten with
// less information than it had.

const fs = require("fs");
const path = require("path");

const DEFAULTS = () => ({
  accounts: {},
  prefs: { launchAtLogin: true, runInBackground: true },
  ingestedDownloads: [],
  queue: [],
});

class Store {
  constructor(dir, name = "app-settings.json") {
    this.dir = dir;
    this.file = path.join(dir, name);
    this.bak = this.file + ".bak";
    this.data = null;
    this._writeTimer = null;
    this._writing = false;
    this._dirty = false;
  }

  _parse(file) {
    const raw = fs.readFileSync(file, "utf8");
    if (!raw.trim()) throw new Error("empty");
    const obj = JSON.parse(raw);
    if (!obj || typeof obj !== "object") throw new Error("not an object");
    return obj;
  }

  load() {
    if (this.data) return this.data;
    let loaded = null;
    for (const candidate of [this.file, this.bak]) {
      try {
        loaded = this._parse(candidate);
        break;
      } catch (e) {
        if (e && e.code === "ENOENT") continue;
        // Corrupt (not merely missing): keep a copy for forensics, then try
        // the backup rather than starting from nothing.
        try {
          fs.renameSync(candidate, candidate + ".corrupt-" + Date.now());
        } catch {
          /* nothing we can do */
        }
      }
    }
    const d = DEFAULTS();
    this.data = loaded ? { ...d, ...loaded } : d;
    // Normalize shape so callers never guard.
    this.data.accounts = this.data.accounts || {};
    this.data.prefs = { ...d.prefs, ...(this.data.prefs || {}) };
    this.data.ingestedDownloads = Array.isArray(this.data.ingestedDownloads)
      ? this.data.ingestedDownloads
      : [];
    this.data.queue = Array.isArray(this.data.queue) ? this.data.queue : [];
    return this.data;
  }

  /** Atomic + durable. Synchronous on purpose: also used from will-quit. */
  flush() {
    if (!this.data) return;
    this._dirty = false;
    const tmp = this.file + ".tmp-" + process.pid;
    try {
      fs.mkdirSync(this.dir, { recursive: true });
      const body = JSON.stringify(this.data, null, 2);
      const fd = fs.openSync(tmp, "w", 0o600);
      try {
        fs.writeFileSync(fd, body);
        fs.fsyncSync(fd); // survive a power cut, not just a crash
      } finally {
        fs.closeSync(fd);
      }
      // Keep the last good copy before replacing it.
      try {
        if (fs.existsSync(this.file)) fs.copyFileSync(this.file, this.bak);
      } catch {
        /* best effort */
      }
      fs.renameSync(tmp, this.file); // atomic
      this.writeFailures = 0;
    } catch (e) {
      try {
        fs.rmSync(tmp, { force: true });
      } catch {
        /* ignore */
      }
      // A disk that is full or read-only means the user's schedules and
      // connections are not being saved. Silence there is the worst outcome:
      // everything looks fine until a restart loses it all.
      this.writeFailures = (this.writeFailures || 0) + 1;
      this.lastWriteError = String((e && e.message) || e);
      if (this.onWriteError) {
        try {
          this.onWriteError(this.lastWriteError, this.writeFailures);
        } catch {
          /* reporting must never throw */
        }
      }
    }
  }

  /** Coalesce bursts of writes; never lose the final state. */
  save() {
    this._dirty = true;
    if (this._writeTimer) return;
    this._writeTimer = setTimeout(() => {
      this._writeTimer = null;
      if (this._dirty) this.flush();
    }, 400);
    if (this._writeTimer.unref) this._writeTimer.unref();
  }

  // -- accounts -------------------------------------------------------------

  account(id) {
    const s = this.load();
    return s.accounts[id] || { schedule: "manual", connected: false };
  }

  patchAccount(id, patch) {
    const s = this.load();
    s.accounts[id] = { ...this.account(id), ...patch };
    this.save();
    return s.accounts[id];
  }

  // -- prefs ----------------------------------------------------------------

  prefs() {
    return this.load().prefs;
  }

  setPref(key, value) {
    this.load().prefs[key] = value;
    this.save();
  }
}

module.exports = { Store };
